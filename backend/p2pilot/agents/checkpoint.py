"""Where LangGraph keeps run state between steps (and while a run waits for a human).

    CHECKPOINTER=auto      Postgres when P2PILOT_DB_URL is set, otherwise in-memory (default)
    CHECKPOINTER=postgres  always Postgres; fail at first use if the database is missing
    CHECKPOINTER=memory    in-process only (unit tests, quick demos); lost on restart

Why Postgres: an invoice paused for approval may wait hours. With MemorySaver an API
restart (or a second pod) loses the paused run; with Postgres any API process can resume
it. The tables live in their own `langgraph` schema, which Supabase does not expose
through PostgREST and which anon/authenticated users cannot read (see the Sprint 2.5
migration): run state contains invoice text and must never be reachable from the browser.

Implementation note: we use the *sync* PostgresSaver and run its calls in a worker thread
for the async graph API. The async saver needs psycopg's async mode, which does not work
on Windows' default ProactorEventLoop (uvicorn without --reload); the threaded wrapper
behaves the same on every OS.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.checkpoint.memory import MemorySaver

from p2pilot.config import Settings

log = logging.getLogger(__name__)

SCHEMA = "langgraph"

_memory = MemorySaver()
_postgres: BaseCheckpointSaver | None = None
_lock = threading.Lock()


def mode(settings: Settings) -> str:
    """Resolve 'auto' to the concrete backend that will be used."""
    choice = (settings.checkpointer or "auto").lower()
    if choice == "auto":
        return "postgres" if settings.p2pilot_db_url else "memory"
    if choice not in ("postgres", "memory"):
        raise ValueError(f"CHECKPOINTER must be auto, postgres or memory, not {choice!r}")
    return choice


def get_checkpointer(settings: Settings) -> BaseCheckpointSaver:
    """Process-wide checkpointer. Postgres is created (and its tables set up) on first use."""
    if mode(settings) == "memory":
        return _memory
    global _postgres
    if _postgres is None:
        with _lock:
            if _postgres is None:
                _postgres = _build_postgres(settings.p2pilot_db_url)
    return _postgres


def _build_postgres(db_url: str) -> BaseCheckpointSaver:
    if not db_url:
        raise RuntimeError("CHECKPOINTER=postgres needs P2PILOT_DB_URL")
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as exc:  # pragma: no cover - depends on install
        raise RuntimeError(
            "Postgres checkpointer not installed. Run: pip install -e \".[dev]\" "
            "(adds langgraph-checkpoint-postgres), or set CHECKPOINTER=memory.") from exc
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    # The migration creates the schema too; doing it here keeps a fresh database working.
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute(f"create schema if not exists {SCHEMA}")

    pool = ConnectionPool(
        db_url, min_size=1, max_size=8, open=True,
        # PostgresSaver requires autocommit + dict rows; search_path puts its tables
        # (checkpoints, checkpoint_blobs, checkpoint_writes, ...) in the private schema.
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row,
                "options": f"-c search_path={SCHEMA}"},
    )

    class ThreadedPostgresSaver(PostgresSaver):
        """PostgresSaver with its async methods run in a thread (see module docstring)."""

        async def aget_tuple(self, config) -> CheckpointTuple | None:
            return await asyncio.to_thread(self.get_tuple, config)

        async def alist(self, config, **kwargs) -> AsyncIterator[CheckpointTuple]:
            items = await asyncio.to_thread(lambda: list(self.list(config, **kwargs)))
            for item in items:
                yield item

        async def aput(self, config, checkpoint, metadata, new_versions):
            return await asyncio.to_thread(self.put, config, checkpoint, metadata,
                                           new_versions)

        async def aput_writes(self, config, writes, task_id, task_path: str = "") -> None:
            await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

        async def adelete_thread(self, thread_id: str) -> None:
            await asyncio.to_thread(self.delete_thread, thread_id)

    saver = ThreadedPostgresSaver(pool)
    saver.setup()  # idempotent: creates/migrates the checkpoint tables
    _lock_down(db_url)
    log.info("LangGraph checkpoints stored in Postgres schema %r", SCHEMA)
    return saver


def _lock_down(db_url: str) -> None:
    """Belt and braces next to the migration: browser roles get nothing in this schema."""
    import psycopg

    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute(f"""
            do $$
            declare r text;
            begin
              foreach r in array array['anon', 'authenticated'] loop
                if exists (select 1 from pg_roles where rolname = r) then
                  execute format('revoke all on schema {SCHEMA} from %I', r);
                  execute format('revoke all on all tables in schema {SCHEMA} from %I', r);
                  execute format('revoke all on all sequences in schema {SCHEMA} from %I', r);
                end if;
              end loop;
            end $$;""")


def describe(settings: Settings) -> dict[str, Any]:
    """For /health: which backend is configured (does not connect)."""
    return {"checkpointer": mode(settings)}


__all__ = ["get_checkpointer", "mode", "describe", "SCHEMA"]
