"""Extraction from real generated PDFs, and the prompt-injection detector."""

import json
from decimal import Decimal

import pytest

pytest.importorskip("faker")
pytest.importorskip("reportlab")
pytest.importorskip("pypdf")

from p2pilot.data_gen.generate import generate  # noqa: E402
from p2pilot.data_gen.invoices import build_invoices, write_pdf  # noqa: E402
from p2pilot.invoices.extract import (  # noqa: E402
    ExtractionError, detect_suspicious_instructions, extract_with_rules, pdf_to_text,
)


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("invoices")
    invoices = build_invoices(generate(seed=42))
    for inv in invoices:
        write_pdf(inv, out / inv.file, "Acme Components Pvt Ltd")
    return out, invoices


def test_every_scenario_is_present(generated):
    _, invoices = generated
    scenarios = {i.scenario for i in invoices}
    assert {"clean", "price_variance", "qty_exceeds_received", "duplicate", "unknown_po",
            "not_received", "over_approval_limit", "prompt_injection", "vendor_mismatch",
            "arithmetic_error"} <= scenarios


def test_rules_extractor_reads_every_generated_pdf_exactly(generated):
    out, invoices = generated
    for inv in invoices:
        got = extract_with_rules(pdf_to_text((out / inv.file).read_bytes()))
        assert got.invoice_number == inv.invoice_number, inv.file
        assert got.po_number == inv.po_number, inv.file
        assert got.vendor_gstin == inv.vendor_gstin, inv.file
        assert len(got.lines) == len(inv.lines), inv.file
        assert got.total == inv.total, inv.file
        assert got.lines[0].unit_price == inv.lines[0].unit_price, inv.file


def test_ground_truth_is_serialisable(generated):
    _, invoices = generated
    json.dumps([{"file": i.file, "total": str(i.total)} for i in invoices])
    assert all(i.total > 0 for i in invoices)
    assert any(i.total > Decimal("200000") for i in invoices if i.scenario ==
               "over_approval_limit")


def test_injection_is_detected_only_in_the_planted_invoice(generated):
    out, invoices = generated
    flagged = [i.scenario for i in invoices
               if detect_suspicious_instructions(pdf_to_text((out / i.file).read_bytes()))]
    assert flagged == ["prompt_injection"]


def test_non_pdf_is_rejected():
    with pytest.raises(ExtractionError):
        pdf_to_text(b"hello, not a pdf")


def test_missing_fields_fail_loudly():
    with pytest.raises(ExtractionError, match="Could not find"):
        extract_with_rules("TAX INVOICE\nVendor: Someone\n")


def test_amounts_printed_with_separators_and_currency_are_accepted():
    from p2pilot.invoices.models import InvoiceLine

    line = InvoiceLine(line_no=1, sku="SKU-1", qty="1,000", unit_price="₹ 2,272.50",
                       tax_rate="18", amount="INR 2,272,500.00")
    assert line.unit_price == Decimal("2272.50") and line.amount == Decimal("2272500.00")


def test_non_numbers_are_still_rejected():
    from pydantic import ValidationError

    from p2pilot.invoices.models import InvoiceLine

    with pytest.raises(ValidationError):
        InvoiceLine(line_no=1, sku="SKU-1", qty="ten", unit_price="1", tax_rate="18", amount="1")


def test_validation_errors_are_summarised_for_users():
    from pydantic import ValidationError

    from p2pilot.invoices.extract import _short
    from p2pilot.invoices.models import InvoiceLine

    try:
        InvoiceLine(line_no=1, sku="S", qty="x", unit_price="y", tax_rate="z", amount="w")
    except ValidationError as exc:
        msg = _short(exc)
    assert msg.startswith("4 invalid field(s): amount, qty, tax_rate") and "+1 more" in msg
    assert "pydantic.dev" not in msg
