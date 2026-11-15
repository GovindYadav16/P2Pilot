"""Evaluate the invoice-processing skill against the planted-anomaly invoices.

    python -m p2pilot.evals.invoice_eval                  # real model via LiteLLM ("fast")
    python -m p2pilot.evals.invoice_eval --extractor rules   # no model: tests the pipeline
    python -m p2pilot.evals.invoice_eval --min-pass-rate 0.9  # exit 1 below this (CI gate)

Every PDF in data/invoices has a known answer in ground_truth.json. Each one runs through
the *same* LangGraph state machine the API uses (tools backed by the seeded dataset in
memory, see evals/tools.py), in file order, so the duplicate case sees the earlier
invoice. A run stops at the approval pause; that is enough to score it.

Scored per invoice:
  extraction  invoice number, PO number and total read correctly (total to the paisa)
  decision    auto_post vs review matches the expected decision
  findings    every expected finding code was raised (extra findings are reported)
A case passes only if all three hold. Results go to data/evals/ as JSON and Markdown.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver

from p2pilot.agents.invoice_graph import build_invoice_graph
from p2pilot.config import REPO_ROOT, Settings, get_settings
from p2pilot.data_gen.generate import generate
from p2pilot.evals.tools import DatasetTools
from p2pilot.tools.base import ToolFailed

DEFAULT_DIR = REPO_ROOT / "data" / "invoices"
REPORT_DIR = REPO_ROOT / "data" / "evals"


@dataclass
class CaseResult:
    file: str
    scenario: str
    expected_decision: str
    decision: str | None = None
    expected_findings: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    extraction_ok: bool = False
    extraction_errors: list[str] = field(default_factory=list)
    decision_ok: bool = False
    findings_ok: bool = False
    extra_findings: list[str] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0
    tokens: int = 0

    @property
    def passed(self) -> bool:
        return self.extraction_ok and self.decision_ok and self.findings_ok and not self.error


def _same_amount(a, b) -> bool:
    try:
        return abs(Decimal(str(a)) - Decimal(str(b))) < Decimal("0.01")
    except (InvalidOperation, TypeError):
        return False


def score(case: CaseResult, truth: dict, state: dict) -> None:
    inv = state.get("invoice") or {}
    for key in ("invoice_number", "po_number"):
        if (inv.get(key) or "").strip() != truth[key]:
            case.extraction_errors.append(f"{key}: got {inv.get(key)!r}, want {truth[key]!r}")
    if not _same_amount(inv.get("total"), truth["total"]):
        case.extraction_errors.append(f"total: got {inv.get('total')}, want {truth['total']}")
    case.extraction_ok = not case.extraction_errors

    case.decision = state.get("decision")
    case.decision_ok = case.decision == truth["expected_decision"]
    case.findings = sorted({f["code"] for f in state.get("findings", [])})
    case.findings_ok = set(truth["expected_findings"]) <= set(case.findings)
    case.extra_findings = sorted(set(case.findings) - set(truth["expected_findings"]))


def _usage_counter():
    """Token counter for the model calls inside one invoice (None if unavailable)."""
    try:
        from langchain_core.callbacks import get_usage_metadata_callback
    except ImportError:
        return None
    return get_usage_metadata_callback()


async def run_eval(settings: Settings, pdf_dir: Path, *, seed: int = 42,
                   limit: int | None = None, llm=None) -> list[CaseResult]:
    truth_rows = json.loads((pdf_dir / "ground_truth.json").read_text())[:limit]
    tools = DatasetTools(generate(seed=seed))
    graph = build_invoice_graph(tools, llm, settings=settings, checkpointer=MemorySaver())
    results = []
    for truth in truth_rows:
        case = CaseResult(file=truth["file"], scenario=truth["scenario"],
                          expected_decision=truth["expected_decision"],
                          expected_findings=truth["expected_findings"])
        file_id = tools.add_file(pdf_dir / truth["file"])
        config = {"configurable": {"thread_id": f"eval-{truth['file']}"},
                  "metadata": {"skill": "invoice-eval", "file": truth["file"]}}
        started = time.perf_counter()
        counter = _usage_counter()
        try:
            if counter is not None:
                with counter as usage:
                    await graph.ainvoke({"tenant_id": tools.tenant_id, "user_id": "eval",
                                         "file_id": file_id}, config)
                case.tokens = sum(u.get("total_tokens", 0)
                                  for u in usage.usage_metadata.values())
            else:
                await graph.ainvoke({"tenant_id": tools.tenant_id, "user_id": "eval",
                                     "file_id": file_id}, config)
            state = (await graph.aget_state(config)).values
            score(case, truth, state)
        except ToolFailed as exc:
            case.error = f"{exc.tool}: {exc.error.user_message}"
        except Exception as exc:  # noqa: BLE001 - one bad case must not stop the eval
            case.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        case.seconds = round(time.perf_counter() - started, 2)
        results.append(case)
        mark = "PASS" if case.passed else "FAIL"
        print(f"  {mark}  {case.file:<32} {case.decision or '-':<10} {case.seconds:>6.2f}s")
    return results


def summarize(results: list[CaseResult], extractor: str) -> dict:
    n = len(results) or 1
    secs = [r.seconds for r in results] or [0.0]
    return {
        "when": datetime.now().isoformat(timespec="seconds"),
        "extractor": extractor,
        "cases": len(results),
        "passed": sum(r.passed for r in results),
        "pass_rate": round(sum(r.passed for r in results) / n, 3),
        "extraction_accuracy": round(sum(r.extraction_ok for r in results) / n, 3),
        "decision_accuracy": round(sum(r.decision_ok for r in results) / n, 3),
        "findings_recall": round(sum(r.findings_ok for r in results) / n, 3),
        "errors": sum(bool(r.error) for r in results),
        # The money-safety number: a risky invoice that was auto-posted.
        "unsafe_auto_posts": sum(r.expected_decision == "review" and r.decision == "auto_post"
                                 for r in results),
        "seconds_p50": round(statistics.median(secs), 2),
        "seconds_max": round(max(secs), 2),
        "total_tokens": sum(r.tokens for r in results),
    }


def to_markdown(summary: dict, results: list[CaseResult]) -> str:
    s = summary
    out = [f"# Invoice eval: {s['passed']}/{s['cases']} passed ({s['extractor']} extractor)",
           "", f"Run at {s['when']}.", "",
           "| Metric | Value |", "|---|---|"]
    for key in ("pass_rate", "extraction_accuracy", "decision_accuracy", "findings_recall",
                "unsafe_auto_posts", "errors", "seconds_p50", "seconds_max", "total_tokens"):
        out.append(f"| {key.replace('_', ' ')} | {s[key]} |")
    out += ["", "| File | Expected | Got | Findings | Result | Notes |",
            "|---|---|---|---|---|---|"]
    for r in results:
        notes = r.error or "; ".join(r.extraction_errors
                                     + ([f"missing {sorted(set(r.expected_findings) - set(r.findings))}"]
                                        if not r.findings_ok else [])
                                     + ([f"extra {r.extra_findings}"] if r.extra_findings else []))
        out.append(f"| {r.file} | {r.expected_decision} | {r.decision or '-'} | "
                   f"{', '.join(r.findings) or '-'} | {'pass' if r.passed else 'FAIL'} | "
                   f"{notes} |")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--extractor", choices=["llm", "rules"], default="llm")
    p.add_argument("--dir", default=str(DEFAULT_DIR), help="folder with PDFs + ground_truth.json")
    p.add_argument("--seed", type=int, default=42, help="must match the seed the PDFs used")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--min-pass-rate", type=float, default=1.0)
    p.add_argument("--out", default=str(REPORT_DIR))
    args = p.parse_args(argv)

    pdf_dir = Path(args.dir)
    if not (pdf_dir / "ground_truth.json").exists():
        from p2pilot.data_gen.invoices import write_all
        print(f"No invoices in {pdf_dir}; generating them (seed {args.seed}).")
        write_all(pdf_dir, generate(seed=args.seed))

    settings = get_settings().model_copy(update={"extractor": args.extractor,
                                                 "llm_fake": False})
    llm = None
    if args.extractor == "llm":
        from p2pilot.agents.llm import make_chat_model
        llm = make_chat_model(settings, alias="fast", metadata={"skill": "invoice-eval"})

    print(f"Evaluating {pdf_dir} with the {args.extractor} extractor")
    results = asyncio.run(run_eval(settings, pdf_dir, seed=args.seed, limit=args.limit,
                                   llm=llm))
    summary = summarize(results, args.extractor)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"invoice_eval_{stamp}.json").write_text(json.dumps(
        {"summary": summary, "cases": [asdict(r) | {"passed": r.passed} for r in results]},
        indent=2))
    report = to_markdown(summary, results)
    (out_dir / f"invoice_eval_{stamp}.md").write_text(report, encoding="utf-8")
    (out_dir / "latest.md").write_text(report, encoding="utf-8")

    print(f"\n{summary['passed']}/{summary['cases']} passed  |  extraction "
          f"{summary['extraction_accuracy']:.0%}  decision {summary['decision_accuracy']:.0%}  "
          f"findings {summary['findings_recall']:.0%}  unsafe auto-posts "
          f"{summary['unsafe_auto_posts']}  |  p50 {summary['seconds_p50']}s  "
          f"tokens {summary['total_tokens']}")
    print(f"Report: {(out_dir / 'latest.md').resolve()}")
    ok = summary["pass_rate"] >= args.min_pass_rate and summary["unsafe_auto_posts"] == 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
