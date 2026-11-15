"""Tool layer for evaluation: same interface and envelope as P2PTools, backed by the
synthetic dataset in memory instead of Postgres.

Why not the real database: an eval must be repeatable. Against the database, the second
run would see the first run's invoices and flag every file as a duplicate, and posting
would pile up ERP documents in the demo tenant. Here every run starts from the same
state, and PO / goods-receipt data comes from the same seed the PDFs were built from.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

from p2pilot.data_gen.generate import Dataset
from p2pilot.invoices.extract import pdf_to_text
from p2pilot.tools.base import ToolProblem, tool


class DatasetTools:
    def __init__(self, ds: Dataset, tenant_slug: str = "acme"):
        tenant = next(t for t in ds.tenants if t["slug"] == tenant_slug)
        self.tenant_id = tenant["id"]
        tid = self.tenant_id
        self.vendors = {v["id"]: v for v in ds.vendors if v["tenant_id"] == tid}
        self.pos = {p["po_number"]: p for p in ds.purchase_orders if p["tenant_id"] == tid}
        self.lines_by_po: dict[str, list[dict]] = {}
        for ln in ds.po_lines:
            if ln["tenant_id"] == tid:
                self.lines_by_po.setdefault(ln["po_id"], []).append(ln)
        self.received: dict[str, Decimal] = {}
        for g in ds.grn_lines:
            if g["tenant_id"] == tid:
                self.received[g["po_line_id"]] = (self.received.get(g["po_line_id"], Decimal(0))
                                                  + g["qty_received"])
        self.has_grn = {g["po_id"] for g in ds.goods_receipts if g["tenant_id"] == tid}
        self.files: dict[str, Path] = {}
        self.invoices: dict[str, dict] = {}
        self.postings: dict[str, str] = {}

    def add_file(self, path: Path) -> str:
        file_id = str(uuid.uuid4())
        self.files[file_id] = path
        return file_id

    # ---- tools used by the invoice graph
    @tool("files_read_text")
    def read_file_text(self, tenant_id, file_id):
        path = self.files.get(file_id)
        if path is None:
            raise ToolProblem("not_found", "That file does not exist in this workspace.")
        text = pdf_to_text(path.read_bytes())
        return {"filename": path.name, "text": text, "chars": len(text)}

    @tool("erp_save_invoice")
    def save_invoice(self, tenant_id, file_id, invoice, thread_id=None):
        invoice_id = str(uuid.uuid4())
        self.invoices[invoice_id] = dict(status="extracted", invoice_number=invoice["invoice_number"],
                                         gstin=invoice["vendor_gstin"],
                                         total=Decimal(str(invoice["total"])))
        return {"invoice_id": invoice_id}

    @tool("erp_get_purchase_order")
    def get_purchase_order(self, tenant_id, po_number):
        po = self.pos.get(po_number or "")
        if po is None:
            return None
        vendor = self.vendors[po["vendor_id"]]
        lines = [dict(line_no=ln["line_no"], sku=ln["sku"], description=ln["description"],
                      qty=ln["qty"], unit_price=ln["unit_price"], tax_rate=ln["tax_rate"],
                      received_qty=self.received.get(ln["id"], Decimal(0)))
                 for ln in sorted(self.lines_by_po[po["id"]], key=lambda x: x["line_no"])]
        return dict(id=po["id"], po_number=po["po_number"], status=po["status"],
                    vendor_id=vendor["id"], vendor_name=vendor["name"],
                    vendor_gstin=vendor["gstin"], has_receipt=po["id"] in self.has_grn,
                    lines=lines)

    @tool("erp_find_possible_duplicates")
    def find_possible_duplicates(self, tenant_id, invoice_id, vendor_gstin, invoice_number,
                                 total):
        total = Decimal(str(total))
        return [{"id": k, "invoice_number": v["invoice_number"], "total": str(v["total"]),
                 "status": v["status"]}
                for k, v in self.invoices.items()
                if k != invoice_id and v["gstin"] == vendor_gstin and v["status"] != "rejected"
                and (v["invoice_number"] == invoice_number or v["total"] == total)]

    @tool("erp_record_decision")
    def record_decision(self, tenant_id, invoice_id, decision, findings):
        status = "in_review" if decision == "review" else "approved"
        self.invoices[invoice_id].update(status=status)
        return {"status": status}

    @tool("erp_post_invoice")
    def post_invoice(self, tenant_id, invoice_id, idempotency_key, posted_by):
        already = idempotency_key in self.postings
        self.postings.setdefault(idempotency_key, f"ERP-EVAL-{len(self.postings) + 1:04d}")
        self.invoices[invoice_id]["status"] = "posted"
        return {"erp_document_number": self.postings[idempotency_key], "already_posted": already}

    # Review/ticket tools are never reached: the eval stops at the approval pause.
    @tool("erp_record_review")
    def record_review(self, *args, **kwargs):
        raise ToolProblem("invalid", "Not used in evaluation.")

    @tool("tickets_create")
    def create_ticket(self, *args, **kwargs):
        raise ToolProblem("invalid", "Not used in evaluation.")
