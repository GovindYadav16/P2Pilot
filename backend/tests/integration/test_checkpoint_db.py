"""Paused runs survive an API restart (Postgres checkpointer), and run state stays private.

Simulates a restart by throwing away the process-wide checkpointer and building a new one
(new connection pool) before resuming.
"""

import uuid

import pytest

pytest.importorskip("langgraph.checkpoint.postgres")
pytest.importorskip("reportlab")

from langgraph.types import Command  # noqa: E402

from p2pilot.agents import checkpoint  # noqa: E402
from p2pilot.agents.invoice_graph import build_invoice_graph  # noqa: E402
from p2pilot.config import Settings  # noqa: E402

from ..fakes import APPROVER, CLERK, TENANT, FakeTools, make_invoice, pdf_bytes  # noqa: E402

SETTINGS = Settings(llm_fake=True, auth_disabled=True, approval_threshold=1000,
                    checkpointer="postgres")
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not SETTINGS.p2pilot_db_url, reason="P2PILOT_DB_URL not set")]


def _restart() -> None:
    checkpoint._postgres = None   # next get_checkpointer() opens a fresh pool, like a new process


async def test_paused_run_resumes_after_restart(tmp_path):
    tools = FakeTools(tmp_path / "objects")
    file_id = tools.add_pdf(pdf_bytes(make_invoice(), tmp_path))   # total > 1000 -> review
    thread = f"it-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread}}

    graph = build_invoice_graph(tools, None, settings=SETTINGS,
                                checkpointer=checkpoint.get_checkpointer(SETTINGS))
    await graph.ainvoke({"tenant_id": TENANT, "user_id": CLERK, "file_id": file_id}, config)
    assert (await graph.aget_state(config)).next == ("approval",)

    _restart()
    graph = build_invoice_graph(tools, None, settings=SETTINGS,
                                checkpointer=checkpoint.get_checkpointer(SETTINGS))
    snap = await graph.aget_state(config)
    assert snap.next == ("approval",), "paused run should be loaded from Postgres"
    assert snap.values["decision"] == "review"

    await graph.ainvoke(Command(resume={"approve": True, "comment": "ok", "by": APPROVER}),
                        config)
    assert (await graph.aget_state(config)).next == ()
    assert len(tools.postings) == 1


def test_run_state_is_not_readable_by_browser_roles():
    import psycopg

    checkpoint.get_checkpointer(SETTINGS)   # ensures the schema and tables exist
    with psycopg.connect(SETTINGS.p2pilot_db_url) as conn:
        for role in ("anon", "authenticated"):
            usage = conn.execute("select has_schema_privilege(%s, 'langgraph', 'USAGE')",
                                 (role,)).fetchone()[0]
            read = conn.execute("select has_table_privilege(%s, 'langgraph.checkpoints', "
                                "'SELECT')", (role,)).fetchone()[0]
            assert not usage and not read, f"{role} must not reach run state"
