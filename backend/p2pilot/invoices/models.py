"""Typed models for invoices, purchase orders and match results.

The LLM fills `InvoiceExtraction`; everything after that (matching, decisions) is plain
Python over these models, so the money math never depends on the model.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, field_validator

_NUMBER_NOISE = re.compile(r"(?i)(inr|rs\.?|₹|,|\s)")


def _clean_number(value: Any) -> Any:
    """Accept numbers as documents print them: "2,272.50", "₹ 72,720.00", "INR 1,000".

    Only separators and currency markers are removed; anything else still fails validation,
    so a model (or a document) can never sneak a non-number through.
    """
    if isinstance(value, str):
        return _NUMBER_NOISE.sub("", value)
    return value


# A Decimal that tolerates thousands separators and currency symbols.
Amount = Annotated[Decimal, BeforeValidator(_clean_number)]
Money = Amount


class InvoiceLine(BaseModel):
    line_no: int = Field(ge=1)
    sku: str
    description: str = ""
    qty: Amount = Field(gt=0)
    unit_price: Money = Field(ge=0)
    tax_rate: Amount = Field(ge=0, le=100, description="GST percent, e.g. 18")
    amount: Money = Field(ge=0, description="qty x unit_price, before tax")


class InvoiceExtraction(BaseModel):
    """What we need from an invoice document. Field names are the extraction contract."""

    vendor_name: str
    vendor_gstin: str = Field(description="15-character GSTIN of the supplier")
    invoice_number: str
    invoice_date: date
    po_number: str | None = Field(default=None, description="Purchase order number, if printed")
    currency: str = "INR"
    lines: list[InvoiceLine] = Field(min_length=1)
    subtotal: Money
    tax_total: Money
    total: Money

    @field_validator("vendor_gstin")
    @classmethod
    def _gstin_shape(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 15 or not v[:2].isdigit():
            raise ValueError("GSTIN must be 15 characters starting with a 2-digit state code")
        return v


class PoLine(BaseModel):
    line_no: int
    sku: str
    description: str
    qty: Decimal
    unit_price: Money
    tax_rate: Decimal
    received_qty: Decimal = Decimal(0)


class PurchaseOrder(BaseModel):
    id: str
    po_number: str
    vendor_id: str
    vendor_name: str
    vendor_gstin: str
    status: str
    has_receipt: bool
    lines: list[PoLine]


FindingCode = Literal[
    "unknown_po",
    "vendor_mismatch",
    "line_not_on_po",
    "price_variance",
    "qty_exceeds_ordered",
    "qty_exceeds_received",
    "not_received",
    "arithmetic_error",
    "possible_duplicate",
    "suspicious_instructions",
    "over_approval_limit",
]


class Finding(BaseModel):
    code: FindingCode
    severity: Literal["block", "warn"]
    message: str
    line_no: int | None = None


class MatchResult(BaseModel):
    matched: bool
    po_number: str | None
    findings: list[Finding] = Field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "block"]
