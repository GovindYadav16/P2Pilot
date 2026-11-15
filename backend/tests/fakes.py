"""In-memory stand-ins for the tool layer, so graph and API tests need no database.

FakeTools mirrors P2PTools' interface and uses the same @tool envelope decorator, so error
handling behaves exactly like production.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from p2pilot.data_gen.invoices import Invoice, Line, write_pdf
from p2pilot.invoices.extract import pdf_to_text
from p2pilot.storage import LocalObjectStore
from p2pilot.tools.base import ToolProblem, tool

TENANT = "11111111-1111-4111-8111-111111111111"
CLERK = "22222222-2222-4222-8222-222222222222"
APPROVER = "33333333-3333-4333-8333-333333333333"
GSTIN = "27ABCDE1234F1Z5"
VENDOR = "Iyer Traders"


def make_invoice(scenario: str = "clean", **over) -> Invoice:
    lines = [Line(1, "SKU-11111", "Hex bolt M8x40", Decimal(100), Decimal("25.00"), Decimal(18)),
             Line(2, "SKU-22222", "Lock nut M10", Decimal(50), Decimal("10.00"), Decimal(18))]
    inv = Invoice(file=f"{scenario}.pdf", scenario=scenario, vendor_name=VENDOR,
                  vendor_gstin=GSTIN, invoice_number=over.pop("invoice_number", "VI-0001"),
                  invoice_date=date(2026, 11, 2).isoformat(), po_number="PO-T-00001",
                  lines=lines, expected_decision="auto_post")
    for k, v in over.items():
        setattr(inv, k, v)
    return inv


def pdf_bytes(inv: Invoice, tmp: Path) -> bytes:
    path = tmp / f"{uuid.uuid4().hex}.pdf"
    write_pdf(inv, path, "Test Tenant Pvt Ltd")
    return path.read_bytes()


class FakeTools:
    def __init__(self, root: Path):
        self.store = LocalObjectStore(root)
        self.files: dict[str, dict] = {}
        self.invoices: dict[str, dict] = {}
        self.postings: dict[str, str] = {}
        self.tickets: list[dict] = []
        self.calls: list[str] = []
        self.fail: dict[str, str] = {}          # tool name -> error code to simulate
        self.po_lines = [
            dict(line_no=1, sku="SKU-11111", description="Hex bolt M8x40", qty=Decimal(100),
                 unit_price=Decimal("25.00"), tax_rate=Decimal(18), received_qty=Decimal(100)),
            dict(line_no=2, sku="SKU-22222", description="Lock nut M10", qty=Decimal(50),
                 unit_price=Decimal("10.00"), tax_rate=Decimal(18), received_qty=Decimal(50)),
        ]

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            code = self.fail[name]
            raise ToolProblem(code, "Simulated failure", retryable=code == "unavailable")

    # ---- API helpers
    def register_upload(self, *, tenant_id, owner_id, object_key, filename, mime_type, size,
                        sha256) -> str:
        file_id = str(uuid.uuid4())
        self.files[file_id] = dict(tenant_id=tenant_id, owner_id=owner_id, key=object_key,
                                   filename=filename)
        return file_id

    def add_pdf(self, data: bytes, owner_id: str = CLERK) -> str:
        key = f"attachments/{TENANT}/{uuid.uuid4()}.pdf"
        self.store.put(key, data, "application/pdf")
        return self.register_upload(tenant_id=TENANT, owner_id=owner_id, object_key=key,
                                    filename="invoice.pdf", mime_type="application/pdf",
                                    size=len(data), sha256="x")

    def file_owned_by(self, *, file_id, tenant_id, owner_id) -> bool:
        f = self.files.get(file_id)
        return bool(f and f["tenant_id"] == tenant_id and f["owner_id"] == owner_id)

    def pending_reviews(self, *, tenant_id) -> list[dict]:
        return [{"invoice_id": k, **v} for k, v in self.invoices.items()
                if v["status"] == "in_review"]

    # ---- tools
    @tool("files_read_text")
    def read_file_text(self, tenant_id, file_id):
        self._maybe_fail("files_read_text")
        f = self.files.get(file_id)
        if not f or f["tenant_id"] != tenant_id:
            raise ToolProblem("not_found", "That file does not exist in this workspace.")
        text = pdf_to_text(self.store.get(f["key"]))
        return {"filename": f["filename"], "text": text, "chars": len(text)}

    @tool("erp_save_invoice")
    def save_invoice(self, tenant_id, file_id, invoice, thread_id=None):
        self._maybe_fail("erp_save_invoice")
        invoice_id = str(uuid.uuid4())
        self.invoices[invoice_id] = dict(tenant_id=tenant_id, status="extracted",
                                         invoice_number=invoice["invoice_number"],
                                         gstin=invoice["vendor_gstin"],
                                         total=Decimal(invoice["total"]), thread_id=thread_id)
        return {"invoice_id": invoice_id, "vendor_known": True, "po_known": True}

    @tool("erp_get_purchase_order")
    def get_purchase_order(self, tenant_id, po_number):
        self._maybe_fail("erp_get_purchase_order")
        if po_number != "PO-T-00001":
            return None
        return dict(id="po1", po_number=po_number, status="open", vendor_id="v1",
                    vendor_name=VENDOR, vendor_gstin=GSTIN, has_receipt=True,
                    lines=[dict(x) for x in self.po_lines])

    @tool("erp_find_possible_duplicates")
    def find_possible_duplicates(self, tenant_id, invoice_id, vendor_gstin, invoice_number,
                                 total):
        self._maybe_fail("erp_find_possible_duplicates")
        return [{"id": k, "invoice_number": v["invoice_number"], "total": str(v["total"]),
                 "status": v["status"]}
                for k, v in self.invoices.items()
                if k != invoice_id and v["gstin"] == vendor_gstin and v["status"] != "rejected"
                and v["invoice_number"] == invoice_number]

    @tool("erp_record_decision")
    def record_decision(self, tenant_id, invoice_id, decision, findings):
        self._maybe_fail("erp_record_decision")
        status = "in_review" if decision == "review" else "approved"
        self.invoices[invoice_id].update(status=status, decision=decision, findings=findings)
        return {"status": status}

    @tool("erp_record_review")
    def record_review(self, tenant_id, invoice_id, reviewer_id, approve, comment=""):
        self._maybe_fail("erp_record_review")
        inv = self.invoices[invoice_id]
        if inv["status"] != "in_review":
            raise ToolProblem("invalid", "This invoice is not waiting for review.")
        inv.update(status="approved" if approve else "rejected", reviewed_by=reviewer_id)
        return {"status": inv["status"]}

    @tool("erp_post_invoice")
    def post_invoice(self, tenant_id, invoice_id, idempotency_key, posted_by):
        self._maybe_fail("erp_post_invoice")
        inv = self.invoices[invoice_id]
        if inv["status"] not in ("approved", "posted"):
            raise ToolProblem("forbidden", "Only approved invoices can be posted.")
        already = idempotency_key in self.postings
        self.postings.setdefault(idempotency_key, f"ERP-TEST-{len(self.postings) + 1:04d}")
        inv["status"] = "posted"
        return {"erp_document_number": self.postings[idempotency_key], "already_posted": already}

    @tool("tickets_create")
    def create_ticket(self, tenant_id, invoice_id, title, body, priority="medium",
                      created_by=None):
        self._maybe_fail("tickets_create")
        number = f"TCK-{len(self.tickets) + 1:05d}"
        self.tickets.append(dict(number=number, invoice_id=invoice_id, title=title))
        if invoice_id:
            self.invoices[invoice_id]["status"] = "exception"
        return {"ticket_id": str(uuid.uuid4()), "ticket_number": number}
