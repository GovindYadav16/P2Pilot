"""The eval harness itself: all 15 planted invoices, rules extractor (no model, no DB).

This doubles as a regression test for the whole pipeline: if a change to matching or
checks breaks one scenario, the matching invoice fails here.
"""

import json

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("reportlab")
pytest.importorskip("faker")

from p2pilot.config import Settings  # noqa: E402
from p2pilot.data_gen.generate import generate  # noqa: E402
from p2pilot.data_gen.invoices import write_all  # noqa: E402
from p2pilot.evals import invoice_eval  # noqa: E402


@pytest.fixture(scope="module")
def pdf_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("invoices")
    write_all(d, generate(seed=42))
    return d


async def test_every_planted_invoice_gets_the_expected_outcome(pdf_dir):
    settings = Settings(extractor="rules", llm_fake=False, checkpointer="memory")
    results = await invoice_eval.run_eval(settings, pdf_dir, seed=42)
    failed = {r.file: (r.error, r.extraction_errors, r.decision, r.findings)
              for r in results if not r.passed}
    assert len(results) == 15
    assert not failed, failed
    summary = invoice_eval.summarize(results, "rules")
    assert summary["pass_rate"] == 1.0 and summary["unsafe_auto_posts"] == 0


def test_scoring_catches_wrong_total_and_missed_finding():
    truth = {"invoice_number": "VI-1", "po_number": "PO-1", "total": "100.00",
             "expected_decision": "review", "expected_findings": ["price_variance"]}
    case = invoice_eval.CaseResult(file="x.pdf", scenario="price_variance",
                                   expected_decision="review",
                                   expected_findings=["price_variance"])
    invoice_eval.score(case, truth, {
        "invoice": {"invoice_number": "VI-1", "po_number": "PO-1", "total": "100.50"},
        "decision": "auto_post", "findings": []})
    assert not case.extraction_ok and not case.decision_ok and not case.findings_ok
    assert not case.passed
    s = invoice_eval.summarize([case], "rules")
    assert s["unsafe_auto_posts"] == 1


def test_cli_writes_reports_and_fails_below_threshold(pdf_dir, tmp_path, monkeypatch):
    rows = json.loads((pdf_dir / "ground_truth.json").read_text())
    rows[0]["expected_decision"] = "review"          # make one case fail on purpose
    bad = tmp_path / "bad"
    bad.mkdir()
    for r in rows:
        (bad / r["file"]).write_bytes((pdf_dir / r["file"]).read_bytes())
    (bad / "ground_truth.json").write_text(json.dumps(rows[:3]))
    out = tmp_path / "reports"
    code = invoice_eval.main(["--extractor", "rules", "--dir", str(bad), "--out", str(out)])
    assert code == 1
    assert (out / "latest.md").read_text(encoding="utf-8").startswith("# Invoice eval: 2/3")
