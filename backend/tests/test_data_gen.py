import pytest

pytest.importorskip("faker")

from p2pilot.data_gen.generate import WATCHLIST, generate  # noqa: E402


@pytest.fixture(scope="module")
def ds():
    return generate(seed=7, vendors_per_tenant=30, pos_per_tenant=60)


def test_same_seed_gives_same_data():
    a = generate(seed=7, vendors_per_tenant=10, pos_per_tenant=10)
    b = generate(seed=7, vendors_per_tenant=10, pos_per_tenant=10)
    assert a.vendors == b.vendors and a.po_lines == b.po_lines


def test_every_row_belongs_to_a_known_tenant(ds):
    tenant_ids = {t["id"] for t in ds.tenants}
    for table in ["vendors", "purchase_orders", "po_lines", "goods_receipts", "grn_lines"]:
        assert {r["tenant_id"] for r in getattr(ds, table)} <= tenant_ids


def test_gstins_are_unique_per_tenant_and_well_formed(ds):
    seen = set()
    for v in ds.vendors:
        key = (v["tenant_id"], v["gstin"])
        assert key not in seen
        seen.add(key)
        assert len(v["gstin"]) == 15 and v["gstin"][:2].isdigit()


def test_planted_shared_bank_accounts_match_ground_truth(ds):
    by_id = {v["id"]: v for v in ds.vendors}
    for slug, truth in ds.ground_truth.items():
        assert len(truth["shared_bank_account"]) == 2
        for a, b in truth["shared_bank_account"]:
            assert by_id[a]["bank_account_hash"] == by_id[b]["bank_account_hash"]


def test_planted_watchlisted_parents_match_ground_truth(ds):
    flagged = {v["id"] for v in ds.vendors if v["parent_company"] in WATCHLIST}
    truth = {vid for t in ds.ground_truth.values() for vid in t["watchlisted_parent"]}
    assert flagged == truth


def test_grn_lines_never_exceed_ordered_qty(ds):
    ordered = {line["id"]: line["qty"] for line in ds.po_lines}
    for g in ds.grn_lines:
        assert g["qty_received"] <= ordered[g["po_line_id"]]
