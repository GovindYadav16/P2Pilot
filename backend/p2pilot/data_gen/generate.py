"""Deterministic synthetic P2P data with planted anomalies.

Pure function of the seed: the same seed always produces the same rows, so evaluation
ground truth is stable. Nothing here touches a database (see seed.py for that).

Sprint 1 plants vendor-level anomalies (shared bank accounts, watch-listed parents).
Sprint 2 adds invoice PDFs with duplicates, price variance, split invoices and a
prompt-injection footer.

All GSTINs, names and accounts are fake and only shaped like real ones.
"""

from __future__ import annotations

import hashlib
import random
import string
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from faker import Faker

TENANTS = [
    ("acme", "Acme Components Pvt Ltd"),
    ("bharat", "Bharat Distributors LLP"),
]

STATE_CODES = {"07": "Delhi", "09": "Uttar Pradesh", "27": "Maharashtra", "29": "Karnataka",
               "33": "Tamil Nadu", "24": "Gujarat", "06": "Haryana"}

CATEGORIES = {
    "Fasteners": ["Hex bolt M8x40", "Lock nut M10", "Spring washer 12mm", "Rivet 4x10"],
    "Bearings": ["Ball bearing 6204", "Taper roller 30205", "Pillow block UCP205"],
    "Packaging": ["Corrugated box 5-ply", "Stretch film 23 micron", "Pallet 1200x1000"],
    "Electrical": ["Copper cable 2.5 sqmm", "MCB 32A", "Contactor 25A", "LED panel 40W"],
    "Raw material": ["MS sheet 2mm", "Aluminium rod 25mm", "HDPE granules", "SS pipe 1 inch"],
    "MRO": ["Cutting oil 20L", "Safety gloves", "Grinding wheel 4in", "Welding rod 3.15mm"],
}

# Fictional "watch list" of parent companies, used to plant a graph-risk anomaly.
WATCHLIST = ["Sunrise Global Holdings", "Orion Trade Ventures"]


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _gstin(rng: random.Random, state_code: str) -> str:
    pan = ("".join(rng.choices(string.ascii_uppercase, k=5))
           + "".join(rng.choices(string.digits, k=4))
           + rng.choice(string.ascii_uppercase))
    return f"{state_code}{pan}1Z{rng.choice(string.ascii_uppercase + string.digits)}"


def _account_hash(rng: random.Random) -> str:
    account = "".join(rng.choices(string.digits, k=12))
    return hashlib.sha256(account.encode()).hexdigest()


@dataclass
class Dataset:
    tenants: list[dict] = field(default_factory=list)
    vendors: list[dict] = field(default_factory=list)
    purchase_orders: list[dict] = field(default_factory=list)
    po_lines: list[dict] = field(default_factory=list)
    goods_receipts: list[dict] = field(default_factory=list)
    grn_lines: list[dict] = field(default_factory=list)
    ground_truth: dict = field(default_factory=dict)


def generate(seed: int = 42, vendors_per_tenant: int = 100, pos_per_tenant: int = 1000) -> Dataset:
    rng = random.Random(seed)
    fake = Faker("en_IN")
    fake.seed_instance(seed)
    ds = Dataset()
    today = date(2026, 11, 1)

    for slug, name in TENANTS:
        tenant_id = _uuid(rng)
        ds.tenants.append({"id": tenant_id, "slug": slug, "name": name})
        truth: dict = {"shared_bank_account": [], "watchlisted_parent": []}

        # ---- vendors
        vendors = []
        for _ in range(vendors_per_tenant):
            code = rng.choice(list(STATE_CODES))
            vendors.append({
                "id": _uuid(rng),
                "tenant_id": tenant_id,
                "name": f"{fake.last_name()} {rng.choice(['Industries', 'Traders', 'Enterprises', 'Engineering', 'Supplies'])}",
                "gstin": _gstin(rng, code),
                "state": STATE_CODES[code],
                "category": rng.choice(list(CATEGORIES)),
                "parent_company": None,
                "bank_account_hash": _account_hash(rng),
            })

        # Planted: two pairs of different vendors paid into the same bank account.
        picks = rng.sample(vendors, 6)
        for a, b in [(picks[0], picks[1]), (picks[2], picks[3])]:
            b["bank_account_hash"] = a["bank_account_hash"]
            truth["shared_bank_account"].append(sorted([a["id"], b["id"]]))
        # Planted: two vendors owned by a watch-listed parent company.
        for v, parent in zip(picks[4:6], WATCHLIST):
            v["parent_company"] = parent
            truth["watchlisted_parent"].append(v["id"])
        ds.vendors.extend(vendors)

        # ---- purchase orders, lines, goods receipts
        for n in range(1, pos_per_tenant + 1):
            vendor = rng.choice(vendors)
            po_id = _uuid(rng)
            ordered_at = today - timedelta(days=rng.randint(5, 365))
            ds.purchase_orders.append({
                "id": po_id, "tenant_id": tenant_id, "vendor_id": vendor["id"],
                "po_number": f"PO-{slug.upper()}-{n:05d}", "status": "open",
                "currency": "INR", "ordered_at": ordered_at,
            })
            lines = []
            for line_no in range(1, rng.randint(1, 5) + 1):
                line = {
                    "id": _uuid(rng), "tenant_id": tenant_id, "po_id": po_id, "line_no": line_no,
                    "sku": f"SKU-{rng.randint(10000, 99999)}",
                    "description": rng.choice(CATEGORIES[vendor["category"]]),
                    "qty": Decimal(rng.choice([10, 25, 50, 100, 200, 500])),
                    "unit_price": Decimal(rng.randint(20, 5000)) + Decimal(rng.choice([0, 50])) / 100,
                    "tax_rate": Decimal(rng.choice([5, 12, 18, 28])),
                }
                lines.append(line)
            ds.po_lines.extend(lines)

            if rng.random() < 0.9:  # 10% of POs not yet received
                grn_id = _uuid(rng)
                ds.goods_receipts.append({
                    "id": grn_id, "tenant_id": tenant_id, "po_id": po_id,
                    "grn_number": f"GRN-{slug.upper()}-{n:05d}",
                    "received_at": ordered_at + timedelta(days=rng.randint(2, 20)),
                })
                for line in lines:
                    partial = rng.random() < 0.08
                    qty = (line["qty"] * Decimal("0.8")).quantize(Decimal("1")) if partial else line["qty"]
                    ds.grn_lines.append({
                        "id": _uuid(rng), "tenant_id": tenant_id, "grn_id": grn_id,
                        "po_line_id": line["id"], "qty_received": qty,
                    })

        ds.ground_truth[slug] = truth

    return ds
