"""Deterministic 3-way match: invoice vs purchase order vs goods receipt.

No LLM here on purpose: tolerances and arithmetic must be exact, explainable and testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from p2pilot.invoices.models import Finding, InvoiceExtraction, MatchResult, PurchaseOrder

ONE_RUPEE = Decimal("1.00")


@dataclass(frozen=True)
class Tolerances:
    price_pct: Decimal = Decimal("2.0")   # allowed unit-price drift vs PO, percent
    amount_abs: Decimal = ONE_RUPEE       # rounding allowance on line/total arithmetic


def three_way_match(
    inv: InvoiceExtraction, po: PurchaseOrder | None, tol: Tolerances = Tolerances()
) -> MatchResult:
    findings: list[Finding] = []
    findings += _arithmetic(inv, tol)

    if po is None:
        findings.append(Finding(
            code="unknown_po", severity="block",
            message=f"Purchase order {inv.po_number or '(none printed)'} was not found."))
        return MatchResult(matched=False, po_number=inv.po_number, findings=findings)

    if inv.vendor_gstin != po.vendor_gstin.upper():
        findings.append(Finding(
            code="vendor_mismatch", severity="block",
            message=f"Invoice GSTIN {inv.vendor_gstin} does not match PO vendor "
                    f"{po.vendor_name} ({po.vendor_gstin})."))

    if not po.has_receipt:
        findings.append(Finding(
            code="not_received", severity="block",
            message=f"No goods receipt recorded for {po.po_number}; nothing to pay for yet."))

    by_sku = {line.sku: line for line in po.lines}
    for line in inv.lines:
        pol = by_sku.get(line.sku)
        if pol is None:
            findings.append(Finding(
                code="line_not_on_po", severity="block", line_no=line.line_no,
                message=f"Line {line.line_no}: {line.sku} is not on {po.po_number}."))
            continue
        if pol.unit_price > 0:
            drift = (line.unit_price - pol.unit_price) / pol.unit_price * 100
            if abs(drift) > tol.price_pct:
                findings.append(Finding(
                    code="price_variance", severity="block", line_no=line.line_no,
                    message=f"Line {line.line_no}: unit price {line.unit_price} vs PO "
                            f"{pol.unit_price} ({drift:+.1f}%, tolerance ±{tol.price_pct}%)."))
        if line.qty > pol.qty:
            findings.append(Finding(
                code="qty_exceeds_ordered", severity="block", line_no=line.line_no,
                message=f"Line {line.line_no}: invoiced {line.qty} > ordered {pol.qty}."))
        elif po.has_receipt and line.qty > pol.received_qty:
            findings.append(Finding(
                code="qty_exceeds_received", severity="block", line_no=line.line_no,
                message=f"Line {line.line_no}: invoiced {line.qty} > received "
                        f"{pol.received_qty}."))

    blocking = [f for f in findings if f.severity == "block"]
    return MatchResult(matched=not blocking, po_number=po.po_number, findings=findings)


def _arithmetic(inv: InvoiceExtraction, tol: Tolerances) -> list[Finding]:
    out: list[Finding] = []
    subtotal = Decimal(0)
    tax = Decimal(0)
    for line in inv.lines:
        expected = line.qty * line.unit_price
        if abs(expected - line.amount) > tol.amount_abs:
            out.append(Finding(
                code="arithmetic_error", severity="block", line_no=line.line_no,
                message=f"Line {line.line_no}: {line.qty} x {line.unit_price} = {expected:.2f}, "
                        f"invoice says {line.amount}."))
        subtotal += line.amount
        tax += line.amount * line.tax_rate / 100
    checks = [("subtotal", subtotal, inv.subtotal), ("tax", tax, inv.tax_total),
              ("total", inv.subtotal + inv.tax_total, inv.total)]
    for name, expected, stated in checks:
        if abs(expected - stated) > tol.amount_abs:
            out.append(Finding(
                code="arithmetic_error", severity="block",
                message=f"Invoice {name} {stated} does not add up (expected {expected:.2f})."))
    return out
