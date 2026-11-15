"""P2P tools over the mock ERP (Postgres), object storage and ticketing.

Every method: explicit tenant_id in every query, ToolResult envelope, small outputs.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from p2pilot.invoices.extract import ExtractionError, pdf_to_text
from p2pilot.storage import ObjectNotFound, ObjectStore
from p2pilot.tools.base import ToolProblem, ToolResult, tool


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str)


class P2PTools:
    """Database-backed tools. `pool` is a psycopg ConnectionPool (dict rows)."""

    def __init__(self, pool, store: ObjectStore):
        self.pool = pool
        self.store = store

    # ------------------------------------------------------------ files
    @tool("files_read_text")
    def read_file_text(self, tenant_id: str, file_id: str) -> dict:
        with self.pool.connection() as conn:
            row = conn.execute(
                "select object_key, filename from public.files where id = %s and tenant_id = %s",
                (file_id, tenant_id)).fetchone()
        if not row:
            raise ToolProblem("not_found", "That file does not exist in this workspace.")
        try:
            text = pdf_to_text(self.store.get(row["object_key"]))
        except ObjectNotFound as exc:
            raise ToolProblem("not_found", "The file's contents are missing from storage.") from exc
        except ExtractionError as exc:
            raise ToolProblem("invalid", str(exc)) from exc
        return {"filename": row["filename"], "text": text, "chars": len(text)}

    # ------------------------------------------------------------ invoices
    # ------------------------------------------------------------ API helpers (not agent tools)
    def register_upload(self, *, tenant_id: str, owner_id: str, object_key: str, filename: str,
                        mime_type: str, size: int, sha256: str) -> str:
        with self.pool.connection() as conn:
            row = conn.execute(
                "insert into public.files (tenant_id, owner_id, kind, object_key, filename, "
                "mime_type, size_bytes, sha256, expires_at) values (%s, %s, 'attachment', %s, "
                "%s, %s, %s, %s, now() + interval '30 days') returning id::text",
                (tenant_id, owner_id, object_key, filename, mime_type, size, sha256)).fetchone()
        return row["id"]

    def file_owned_by(self, *, file_id: str, tenant_id: str, owner_id: str) -> bool:
        with self.pool.connection() as conn:
            row = conn.execute(
                "select 1 from public.files where id = %s and tenant_id = %s and owner_id = %s",
                (file_id, tenant_id, owner_id)).fetchone()
        return row is not None

    def pending_reviews(self, *, tenant_id: str) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "select i.id::text as invoice_id, i.invoice_number, i.total, i.findings, "
                "i.thread_id, i.created_at, v.name as vendor_name "
                "from public.invoices i left join public.vendors v on v.id = i.vendor_id "
                "where i.tenant_id = %s and i.status = 'in_review' and i.thread_id is not null "
                "order by i.created_at desc limit 50", (tenant_id,)).fetchall()
        return [dict(r) for r in rows]

    @tool("erp_save_invoice")
    def save_invoice(self, tenant_id: str, file_id: str, invoice: dict,
                     thread_id: str | None = None) -> dict:
        with self.pool.connection() as conn:
            vendor = conn.execute(
                "select id::text from public.vendors where tenant_id = %s and gstin = %s",
                (tenant_id, invoice["vendor_gstin"])).fetchone()
            po = conn.execute(
                "select id::text from public.purchase_orders where tenant_id = %s "
                "and po_number = %s", (tenant_id, invoice.get("po_number"))).fetchone()
            row = conn.execute(
                "insert into public.invoices (tenant_id, vendor_id, po_id, file_id, "
                "invoice_number, invoice_date, subtotal, tax_total, total, status, extracted, "
                "thread_id) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'extracted', %s::jsonb, %s) "
                "returning id::text",
                (tenant_id, vendor and vendor["id"], po and po["id"], file_id,
                 invoice["invoice_number"], invoice["invoice_date"], invoice["subtotal"],
                 invoice["tax_total"], invoice["total"], _json(invoice), thread_id)).fetchone()
        return {"invoice_id": row["id"], "vendor_known": vendor is not None,
                "po_known": po is not None}

    @tool("erp_get_purchase_order")
    def get_purchase_order(self, tenant_id: str, po_number: str | None) -> dict | None:
        if not po_number:
            return None
        with self.pool.connection() as conn:
            po = conn.execute(
                "select po.id::text, po.po_number, po.status, v.id::text as vendor_id, "
                "v.name as vendor_name, v.gstin as vendor_gstin, "
                "exists(select 1 from public.goods_receipts g where g.po_id = po.id) as has_receipt "
                "from public.purchase_orders po join public.vendors v on v.id = po.vendor_id "
                "where po.tenant_id = %s and po.po_number = %s", (tenant_id, po_number)).fetchone()
            if not po:
                return None
            lines = conn.execute(
                "select l.line_no, l.sku, l.description, l.qty, l.unit_price, l.tax_rate, "
                "coalesce(sum(gl.qty_received), 0) as received_qty "
                "from public.po_lines l left join public.grn_lines gl on gl.po_line_id = l.id "
                "where l.po_id = %s and l.tenant_id = %s group by l.id order by l.line_no",
                (po["id"], tenant_id)).fetchall()
        return {**po, "lines": [dict(r) for r in lines]}

    @tool("erp_find_possible_duplicates")
    def find_possible_duplicates(self, tenant_id: str, invoice_id: str, vendor_gstin: str,
                                 invoice_number: str, total: Decimal | str) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "select i.id::text, i.invoice_number, i.total, i.status, i.created_at "
                "from public.invoices i join public.vendors v on v.id = i.vendor_id "
                "where i.tenant_id = %s and i.id <> %s and v.gstin = %s "
                "and i.status not in ('rejected') "
                "and (i.invoice_number = %s or "
                "     (i.total = %s and i.created_at > now() - interval '30 days')) "
                "order by i.created_at desc limit 5",
                (tenant_id, invoice_id, vendor_gstin, invoice_number, total)).fetchall()
        return [dict(r) for r in rows]

    @tool("erp_record_decision")
    def record_decision(self, tenant_id: str, invoice_id: str, decision: str,
                        findings: list[dict]) -> dict:
        status = "in_review" if decision == "review" else "approved"
        with self.pool.connection() as conn:
            n = conn.execute(
                "update public.invoices set decision = %s, findings = %s::jsonb, status = %s "
                "where id = %s and tenant_id = %s",
                (decision, _json(findings), status, invoice_id, tenant_id)).rowcount
        if n != 1:
            raise ToolProblem("not_found", "Invoice not found in this workspace.")
        return {"status": status}

    @tool("erp_record_review")
    def record_review(self, tenant_id: str, invoice_id: str, reviewer_id: str, approve: bool,
                      comment: str = "") -> dict:
        status = "approved" if approve else "rejected"
        with self.pool.connection() as conn:
            n = conn.execute(
                "update public.invoices set status = %s, reviewed_by = %s, reviewed_at = now(), "
                "review_comment = %s where id = %s and tenant_id = %s and status = 'in_review'",
                (status, reviewer_id, comment, invoice_id, tenant_id)).rowcount
        if n != 1:
            raise ToolProblem("invalid", "This invoice is not waiting for review.")
        return {"status": status}

    @tool("erp_post_invoice")
    def post_invoice(self, tenant_id: str, invoice_id: str, idempotency_key: str,
                     posted_by: str | None) -> dict:
        """Idempotent: the same idempotency_key never creates a second posting."""
        with self.pool.connection() as conn:
            inv = conn.execute(
                "select status from public.invoices where id = %s and tenant_id = %s",
                (invoice_id, tenant_id)).fetchone()
            if not inv:
                raise ToolProblem("not_found", "Invoice not found in this workspace.")
            if inv["status"] not in ("approved", "posted"):
                raise ToolProblem("forbidden", "Only approved invoices can be posted.")
            row = conn.execute(
                "insert into public.erp_postings (tenant_id, invoice_id, idempotency_key, "
                "erp_document_number, posted_by) values (%s, %s, %s, "
                "'ERP-' || to_char(now(), 'YYYYMMDD') || '-' || upper(substr(md5(%s), 1, 6)), %s) "
                "on conflict (idempotency_key) do nothing returning erp_document_number",
                (tenant_id, invoice_id, idempotency_key, idempotency_key, posted_by)).fetchone()
            already = row is None
            if already:
                row = conn.execute(
                    "select erp_document_number from public.erp_postings "
                    "where idempotency_key = %s and tenant_id = %s",
                    (idempotency_key, tenant_id)).fetchone()
            conn.execute("update public.invoices set status = 'posted' where id = %s",
                         (invoice_id,))
        return {"erp_document_number": row["erp_document_number"], "already_posted": already}

    # ------------------------------------------------------------ tickets
    @tool("tickets_create")
    def create_ticket(self, tenant_id: str, invoice_id: str | None, title: str, body: str,
                      priority: str = "medium", created_by: str | None = None) -> dict:
        if priority not in ("low", "medium", "high"):
            raise ToolProblem("invalid", "priority must be low, medium or high")
        with self.pool.connection() as conn:
            row = conn.execute(
                "insert into public.tickets (tenant_id, invoice_id, title, body, priority, "
                "created_by) values (%s, %s, %s, %s, %s, %s) returning id::text, number",
                (tenant_id, invoice_id, title[:200], body[:4000], priority, created_by)).fetchone()
            if invoice_id:
                conn.execute("update public.invoices set status = 'exception' where id = %s "
                             "and tenant_id = %s", (invoice_id, tenant_id))
        return {"ticket_id": row["id"], "ticket_number": row["number"]}


TOOL_NAMES = [
    "files_read_text", "erp_save_invoice", "erp_get_purchase_order",
    "erp_find_possible_duplicates", "erp_record_decision", "erp_record_review",
    "erp_post_invoice", "tickets_create",
]
