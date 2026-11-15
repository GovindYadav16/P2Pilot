from datetime import date
from decimal import Decimal as D

import pytest

from p2pilot.invoices.match import Tolerances, three_way_match
from p2pilot.invoices.models import InvoiceExtraction, InvoiceLine, PoLine, PurchaseOrder

GSTIN = "27ABCDE1234F1Z5"


def po(**over) -> PurchaseOrder:
    base = dict(id="po1", po_number="PO-ACME-00001", vendor_id="v1", vendor_name="Iyer Traders",
                vendor_gstin=GSTIN, status="open", has_receipt=True,
                lines=[PoLine(line_no=1, sku="SKU-11111", description="Hex bolt", qty=D(100),
                              unit_price=D("25.00"), tax_rate=D(18), received_qty=D(100)),
                       PoLine(line_no=2, sku="SKU-22222", description="Nut", qty=D(50),
                              unit_price=D("10.00"), tax_rate=D(18), received_qty=D(50))])
    base.update(over)
    return PurchaseOrder(**base)


def invoice(lines=None, **over) -> InvoiceExtraction:
    lines = lines or [(1, "SKU-11111", 100, "25.00", 18), (2, "SKU-22222", 50, "10.00", 18)]
    ils = [InvoiceLine(line_no=n, sku=s, qty=D(q), unit_price=D(p), tax_rate=D(t),
                       amount=D(q) * D(p)) for n, s, q, p, t in lines]
    sub = sum((x.amount for x in ils), D(0))
    tax = sum((x.amount * x.tax_rate / 100 for x in ils), D(0))
    base = dict(vendor_name="Iyer Traders", vendor_gstin=GSTIN, invoice_number="VI-1",
                invoice_date=date(2026, 11, 1), po_number="PO-ACME-00001", lines=ils,
                subtotal=sub, tax_total=tax, total=sub + tax)
    base.update(over)
    return InvoiceExtraction(**base)


def codes(result):
    return sorted(f.code for f in result.findings)


def test_clean_invoice_matches():
    r = three_way_match(invoice(), po())
    assert r.matched and r.findings == []


def test_unknown_po_blocks():
    r = three_way_match(invoice(), None)
    assert not r.matched and codes(r) == ["unknown_po"]


def test_price_within_tolerance_passes_and_beyond_blocks():
    ok = three_way_match(invoice(lines=[(1, "SKU-11111", 100, "25.40", 18)]), po())
    assert "price_variance" not in codes(ok)          # +1.6% is inside ±2%
    bad = three_way_match(invoice(lines=[(1, "SKU-11111", 100, "27.00", 18)]), po())
    assert "price_variance" in codes(bad) and not bad.matched


def test_qty_over_received_blocks():
    partial = po(lines=[PoLine(line_no=1, sku="SKU-11111", description="x", qty=D(100),
                               unit_price=D("25.00"), tax_rate=D(18), received_qty=D(80))])
    r = three_way_match(invoice(lines=[(1, "SKU-11111", 100, "25.00", 18)]), partial)
    assert codes(r) == ["qty_exceeds_received"]


def test_qty_over_ordered_blocks():
    r = three_way_match(invoice(lines=[(1, "SKU-11111", 120, "25.00", 18)]), po())
    assert "qty_exceeds_ordered" in codes(r)


def test_no_receipt_blocks():
    r = three_way_match(invoice(), po(has_receipt=False))
    assert "not_received" in codes(r)


def test_vendor_mismatch_blocks():
    r = three_way_match(invoice(vendor_gstin="29ZZZZZ9999Z1Z9"), po())
    assert "vendor_mismatch" in codes(r)


def test_line_not_on_po_blocks():
    r = three_way_match(invoice(lines=[(1, "SKU-99999", 1, "5.00", 18)]), po())
    assert "line_not_on_po" in codes(r)


def test_total_that_does_not_add_up_blocks():
    inv = invoice()
    bad = inv.model_copy(update={"total": inv.total + 500})
    r = three_way_match(bad, po())
    assert "arithmetic_error" in codes(r)


def test_rounding_within_one_rupee_is_tolerated():
    inv = invoice()
    r = three_way_match(inv.model_copy(update={"total": inv.total + D("0.40")}), po())
    assert r.matched


@pytest.mark.parametrize("pct,should_flag", [("1.99", False), ("2.01", True)])
def test_tolerance_is_configurable(pct, should_flag):
    price = (D("25.00") * (1 + D(pct) / 100)).quantize(D("0.0001"))
    r = three_way_match(invoice(lines=[(1, "SKU-11111", 100, str(price), 18)]), po(),
                        Tolerances(price_pct=D("2.0")))
    assert ("price_variance" in codes(r)) is should_flag
