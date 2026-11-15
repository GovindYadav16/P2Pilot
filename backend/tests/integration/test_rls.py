"""Row-level security tests against the real local Supabase Postgres.

Run with the local Supabase stack up (P2PILOT_DB_URL is read from .env):
    pytest -m integration

Each test runs in a transaction that is rolled back, so the database is left untouched.
"""

import json
import os
import uuid
from contextlib import contextmanager

import pytest

psycopg = pytest.importorskip("psycopg")

def _db_url() -> str:
    # Env var wins; otherwise the repo's .env (via Settings), like the API and seeder.
    from p2pilot.config import Settings
    return os.environ.get("P2PILOT_DB_URL") or Settings().p2pilot_db_url


DB_URL = _db_url()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DB_URL, reason="P2PILOT_DB_URL not set (local Supabase not running)"),
]


@pytest.fixture
def db():
    conn = psycopg.connect(DB_URL, autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def world(db):
    """Two tenants, one user in each (plus an approver in A), one vendor and invoice each."""
    ids = {k: str(uuid.uuid4()) for k in
           ["tenant_a", "tenant_b", "clerk_a", "approver_a", "clerk_b",
            "vendor_a", "vendor_b", "invoice_a", "invoice_b"]}
    with db.cursor() as cur:
        for user in ["clerk_a", "approver_a", "clerk_b"]:
            cur.execute(
                "insert into auth.users (id, email, aud, role) values (%s, %s, 'authenticated', 'authenticated')",
                (ids[user], f"{user}-{ids[user][:8]}@rls.test"),
            )
        cur.execute("insert into public.tenants (id, slug, name) values (%s, %s, 'A'), (%s, %s, 'B')",
                    (ids["tenant_a"], f"a-{ids['tenant_a'][:8]}", ids["tenant_b"], f"b-{ids['tenant_b'][:8]}"))
        cur.execute(
            "insert into public.memberships (tenant_id, user_id, role) values "
            "(%s, %s, 'ap_clerk'), (%s, %s, 'approver'), (%s, %s, 'ap_clerk')",
            (ids["tenant_a"], ids["clerk_a"], ids["tenant_a"], ids["approver_a"],
             ids["tenant_b"], ids["clerk_b"]),
        )
        for t in ["a", "b"]:
            cur.execute(
                "insert into public.vendors (id, tenant_id, name, gstin, bank_account_hash) "
                "values (%s, %s, %s, %s, 'h')",
                (ids[f"vendor_{t}"], ids[f"tenant_{t}"], f"Vendor {t}", f"GST{t}{ids['vendor_' + t][:6]}"),
            )
            cur.execute(
                "insert into public.invoices (id, tenant_id, vendor_id, invoice_number, total) "
                "values (%s, %s, %s, %s, 100)",
                (ids[f"invoice_{t}"], ids[f"tenant_{t}"], ids[f"vendor_{t}"], f"INV-{t}"),
            )
    return ids


@contextmanager
def as_user(db, user_id: str | None):
    """Act as a Supabase-authenticated user (or anon when user_id is None) inside a savepoint."""
    with db.transaction():
        with db.cursor() as cur:
            if user_id is None:
                cur.execute("set local role anon")
                claims = {"role": "anon"}
            else:
                cur.execute("set local role authenticated")
                claims = {"sub": user_id, "role": "authenticated"}
            cur.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),))
            yield cur
            cur.execute("reset role")


def test_user_only_sees_own_tenant_vendors(db, world):
    with as_user(db, world["clerk_a"]) as cur:
        cur.execute("select id::text from public.vendors where id in (%s, %s)",
                    (world["vendor_a"], world["vendor_b"]))
        assert {r[0] for r in cur.fetchall()} == {world["vendor_a"]}


def test_user_cannot_read_other_tenant_invoice_even_by_id(db, world):
    with as_user(db, world["clerk_a"]) as cur:
        cur.execute("select count(*) from public.invoices where id = %s", (world["invoice_b"],))
        assert cur.fetchone()[0] == 0


def test_user_cannot_insert_into_other_tenant(db, world):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_user(db, world["clerk_a"]) as cur:
            cur.execute(
                "insert into public.invoices (tenant_id, invoice_number) values (%s, 'SNEAKY-1')",
                (world["tenant_b"],),
            )


def test_clerk_cannot_approve_but_approver_can(db, world):
    with as_user(db, world["clerk_a"]) as cur:
        cur.execute("update public.invoices set status = 'approved' where id = %s",
                    (world["invoice_a"],))
        assert cur.rowcount == 0  # RLS hides the row from the update

    with as_user(db, world["approver_a"]) as cur:
        cur.execute("update public.invoices set status = 'approved' where id = %s",
                    (world["invoice_a"],))
        assert cur.rowcount == 1


def test_anon_sees_nothing(db, world):
    with as_user(db, None) as cur:
        cur.execute("select count(*) from public.vendors where id = %s", (world["vendor_a"],))
        assert cur.fetchone()[0] == 0
