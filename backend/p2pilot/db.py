"""Service-level Postgres access for the API, tools and workers.

User-facing reads that RLS should decide go through PostgREST with the user's JWT (see
/me). Server-side code here uses the service connection and therefore must filter by
tenant_id explicitly in every query (ADR 0001).
"""

from __future__ import annotations

from functools import lru_cache

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from p2pilot.config import get_settings


@lru_cache
def get_pool() -> ConnectionPool:
    url = get_settings().p2pilot_db_url
    if not url:
        raise RuntimeError("P2PILOT_DB_URL is not set")
    return ConnectionPool(url, min_size=1, max_size=8, open=True,
                          kwargs={"row_factory": dict_row, "autocommit": False})


def memberships(user_id: str) -> list[dict]:
    with get_pool().connection() as conn:
        return conn.execute(
            "select m.tenant_id::text, m.role::text, t.slug from public.memberships m "
            "join public.tenants t on t.id = m.tenant_id where m.user_id = %s order by t.slug",
            (user_id,)).fetchall()


def user_id_by_email(email: str) -> str | None:
    with get_pool().connection() as conn:
        row = conn.execute("select id::text from auth.users where email = %s",
                           (email,)).fetchone()
    return row["id"] if row else None
