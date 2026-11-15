"""Real-Postgres tests for the tool layer: idempotent posting, tickets, tenant filtering.

Creates a throwaway tenant and deletes it afterwards (cascade), so seeded data is untouched.
"""

import uuid
from decimal import Decimal

import pytest

psycopg_pool = pytest.importorskip("psycopg_pool")

from p2pilot.config import Settings  # noqa: E402

DB_URL = Settings().p2pilot_db_url
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not DB_URL, reason="P2PILOT_DB_URL not set")]


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    from psycopg.rows import dict_row

    from p2pilot.storage import LocalObjectStore
    from p2pilot.tools.p2p import P2PTools

    pool = psycopg_pool.ConnectionPool(DB_URL, min_size=1, max_size=2, open=True,
                                       kwargs={"row_factory": dict_row})
    yield P2PTools(pool, LocalObjectStore(tmp_path_factory.mktemp("objects")))
    pool.close()


@pytest.fixture
def world(tools):
    t, other, v, po, inv = (str(uuid.uuid4()) for _ in range(5))
    with tools.pool.connection() as c:
        c.execute("insert into tenants (id, slug, name) values (%s, %s, 'T'), (%s, %s, 'O')",
                  (t, f"t-{t[:8]}", other, f"o-{other[:8]}"))
        c.execute("insert into vendors (id, tenant_id, name, gstin, bank_account_hash) "
                  "values (%s, %s, 'V', '27ABCDE1234F1Z5', 'h')", (v, t))
        c.execute("insert into purchase_orders (id, tenant_id, vendor_id, po_number, ordered_at) "
                  "values (%s, %s, %s, %s, current_date)", (po, t, v, f"PO-T-{t[:5]}"))
        c.execute("insert into po_lines (tenant_id, po_id, line_no, sku, description, qty, "
                  "unit_price) values (%s, %s, 1, 'SKU-1', 'x', 10, 5)", (t, po))
        c.execute("insert into invoices (id, tenant_id, vendor_id, invoice_number, total, status) "
                  "values (%s, %s, %s, 'VI-1', 59, 'approved')", (inv, t, v))
    yield {"tenant": t, "other": other, "po_number": f"PO-T-{t[:5]}", "invoice": inv}
    with tools.pool.connection() as c:
        c.execute("delete from tenants where id in (%s, %s)", (t, other))


def test_posting_twice_with_same_key_posts_once(tools, world):
    first = tools.post_invoice(world["tenant"], world["invoice"], f"post:{world['invoice']}", None)
    again = tools.post_invoice(world["tenant"], world["invoice"], f"post:{world['invoice']}", None)
    assert first.ok and again.ok
    assert again.data["already_posted"] and not first.data["already_posted"]
    assert first.data["erp_document_number"] == again.data["erp_document_number"]


def test_unapproved_invoice_cannot_be_posted(tools, world):
    with tools.pool.connection() as c:
        c.execute("update invoices set status = 'in_review' where id = %s", (world["invoice"],))
    res = tools.post_invoice(world["tenant"], world["invoice"], "k1", None)
    assert not res.ok and res.error.code == "forbidden"


def test_other_tenant_cannot_see_or_post_the_invoice(tools, world):
    res = tools.post_invoice(world["other"], world["invoice"], "k2", None)
    assert not res.ok and res.error.code == "not_found"
    assert tools.get_purchase_order(world["other"], world["po_number"]).data is None


def test_purchase_order_lookup_includes_received_qty(tools, world):
    po = tools.get_purchase_order(world["tenant"], world["po_number"]).data
    assert po["has_receipt"] is False and po["lines"][0]["received_qty"] == Decimal(0)


def test_ticket_marks_invoice_as_exception(tools, world):
    res = tools.create_ticket(world["tenant"], world["invoice"], "t", "b", "high")
    assert res.ok and res.data["ticket_number"].startswith("TCK-")
    with tools.pool.connection() as c:
        status = c.execute("select status from invoices where id = %s",
                           (world["invoice"],)).fetchone()["status"]
    assert status == "exception"


def test_bad_priority_is_an_invalid_envelope_not_an_exception(tools, world):
    res = tools.create_ticket(world["tenant"], None, "t", "b", "urgent!!")
    assert not res.ok and res.error.code == "invalid" and not res.error.retryable
