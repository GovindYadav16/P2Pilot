"""invoice-processing skill: an explicit LangGraph state machine (not a free ReAct loop).

    load_file -> extract -> save -> match -> checks -> decide -+-> post ----------+-> summarize
                                                               |                 |
                                                               +-> approval -+---+
                                                                   (interrupt)   +-> escalate

Why a state machine: every invoice goes through the same auditable steps, money math is
deterministic Python, and the only judgement call (approve/reject) is a human's, taken via
a LangGraph interrupt that pauses the run until a reviewer resumes it.

State holds JSON-safe dicts only (Decimals as strings) so any checkpointer can store it.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from p2pilot.config import Settings
from p2pilot.invoices.extract import (
    ExtractionError, detect_suspicious_instructions, extract_with_llm, extract_with_rules,
)
from p2pilot.invoices.match import Tolerances, three_way_match
from p2pilot.invoices.models import Finding, InvoiceExtraction, PurchaseOrder
from p2pilot.tools.base import ToolError, ToolFailed

SKILL = "invoice-processing"


class InvoiceState(TypedDict, total=False):
    tenant_id: str
    user_id: str
    file_id: str
    filename: str
    text: str
    invoice: dict
    invoice_id: str
    findings: list[dict]
    decision: Literal["auto_post", "review"]
    review: dict
    outcome: dict


def _say(text: str) -> None:
    """Stream a line of user-facing text as token events."""
    get_stream_writer()({"type": "token", "text": text})


def _money(value: Any) -> str:
    return f"₹{Decimal(str(value)):,.2f}"


def build_invoice_graph(tools, llm, *, settings: Settings,
                        checkpointer: BaseCheckpointSaver | None = None):
    tol = Tolerances(price_pct=Decimal(str(settings.price_tolerance_pct)))
    threshold = Decimal(str(settings.approval_threshold))
    use_rules = settings.llm_fake or settings.extractor == "rules"

    async def call(name: str, fn, summary=None, **kwargs):
        """Run a sync tool off the event loop, emitting tool_started / tool_finished."""
        write = get_stream_writer()
        call_id = uuid.uuid4().hex[:8]
        shown = {k: v for k, v in kwargs.items()
                 if k not in ("invoice", "findings", "text") and not k.endswith("_id")
                 or k in ("file_id", "invoice_id")}
        write({"type": "tool_started", "call_id": call_id, "tool": name, "args": shown})
        res = await asyncio.to_thread(fn, **kwargs)
        text = (res.error.user_message if not res.ok
                else (summary(res.data) if summary else "ok"))
        write({"type": "tool_finished", "call_id": call_id, "tool": name, "ok": res.ok,
               "summary": text})
        if not res.ok:
            raise ToolFailed(name, res.error)
        return res.data

    # ---------------------------------------------------------------- nodes
    async def load_file(state: InvoiceState) -> dict:
        data = await call("files_read_text", tools.read_file_text,
                          summary=lambda d: f"{d['filename']}: {d['chars']} characters",
                          tenant_id=state["tenant_id"], file_id=state["file_id"])
        return {"text": data["text"], "filename": data["filename"]}

    async def extract(state: InvoiceState, config: RunnableConfig) -> dict:
        write = get_stream_writer()
        call_id = uuid.uuid4().hex[:8]
        method = "rules" if use_rules else "llm"
        write({"type": "tool_started", "call_id": call_id, "tool": "extract_invoice",
               "args": {"method": method}})
        try:
            inv = (extract_with_rules(state["text"]) if use_rules
                   else await extract_with_llm(llm, state["text"], config=config))
        except ExtractionError as exc:
            write({"type": "tool_finished", "call_id": call_id, "tool": "extract_invoice",
                   "ok": False, "summary": str(exc)})
            raise ToolFailed("extract_invoice", ToolError(
                code="invalid", user_message=f"Could not read the invoice: {exc}")) from exc
        write({"type": "tool_finished", "call_id": call_id, "tool": "extract_invoice",
               "ok": True, "summary": f"{inv.invoice_number}, {len(inv.lines)} line(s), "
                                      f"total {_money(inv.total)}"})
        return {"invoice": inv.model_dump(mode="json")}

    async def save(state: InvoiceState, config: RunnableConfig) -> dict:
        thread_id = config.get("configurable", {}).get("thread_id")
        data = await call("erp_save_invoice", tools.save_invoice,
                          summary=lambda d: "saved",
                          tenant_id=state["tenant_id"], file_id=state["file_id"],
                          invoice=state["invoice"], thread_id=thread_id)
        return {"invoice_id": data["invoice_id"]}

    async def match(state: InvoiceState) -> dict:
        inv = InvoiceExtraction.model_validate(state["invoice"])
        po = await call("erp_get_purchase_order", tools.get_purchase_order,
                        summary=lambda d: (f"{d['po_number']}: {len(d['lines'])} line(s), "
                                           f"{'received' if d['has_receipt'] else 'NOT received'}")
                        if d else "not found",
                        tenant_id=state["tenant_id"], po_number=inv.po_number)
        result = three_way_match(inv, PurchaseOrder.model_validate(po) if po else None, tol)
        return {"findings": [f.model_dump(mode="json") for f in result.findings]}

    async def checks(state: InvoiceState) -> dict:
        inv = InvoiceExtraction.model_validate(state["invoice"])
        findings = [Finding.model_validate(f) for f in state.get("findings", [])]
        dups = await call("erp_find_possible_duplicates", tools.find_possible_duplicates,
                          summary=lambda d: f"{len(d)} possible duplicate(s)",
                          tenant_id=state["tenant_id"], invoice_id=state["invoice_id"],
                          vendor_gstin=inv.vendor_gstin, invoice_number=inv.invoice_number,
                          total=inv.total)
        if dups:
            findings.append(Finding(
                code="possible_duplicate", severity="block",
                message=f"Same vendor already has invoice {dups[0]['invoice_number']} "
                        f"({_money(dups[0]['total'])}, status {dups[0]['status']})."))
        snippet = detect_suspicious_instructions(state["text"])
        if snippet:
            findings.append(Finding(
                code="suspicious_instructions", severity="block",
                message=f"The document contains instructions aimed at the system: "
                        f"“{snippet[:120]}”"))
        if inv.total > threshold:
            findings.append(Finding(
                code="over_approval_limit", severity="warn",
                message=f"Total {_money(inv.total)} is above the auto-approval limit "
                        f"{_money(threshold)}."))
        return {"findings": [f.model_dump(mode="json") for f in findings]}

    async def decide(state: InvoiceState) -> dict:
        findings = state.get("findings", [])
        needs_review = any(f["severity"] == "block" or f["code"] == "over_approval_limit"
                           for f in findings)
        decision = "review" if needs_review else "auto_post"
        await call("erp_record_decision", tools.record_decision,
                   summary=lambda d: d["status"], tenant_id=state["tenant_id"],
                   invoice_id=state["invoice_id"], decision=decision, findings=findings)
        inv = state["invoice"]
        if decision == "review":
            lines = "\n".join(f"- {f['message']}" for f in findings)
            _say(f"Invoice {inv['invoice_number']} from {inv['vendor_name']} "
                 f"({_money(inv['total'])}) needs a human review:\n{lines}\n")
        return {"decision": decision}

    async def approval(state: InvoiceState) -> dict:
        inv = state["invoice"]
        # Pauses the run here. Nothing below runs until a reviewer resumes with a decision;
        # on resume LangGraph re-runs this node and interrupt() returns that decision.
        review = interrupt({
            "kind": "invoice_approval", "invoice_id": state["invoice_id"],
            "invoice_number": inv["invoice_number"], "vendor_name": inv["vendor_name"],
            "po_number": inv.get("po_number"), "total": inv["total"],
            "findings": state.get("findings", []),
        })
        approve = bool(review.get("approve"))
        await call("erp_record_review", tools.record_review,
                   summary=lambda d: d["status"], tenant_id=state["tenant_id"],
                   invoice_id=state["invoice_id"], reviewer_id=review["by"], approve=approve,
                   comment=review.get("comment", ""))
        return {"review": {"approve": approve, "by": review["by"],
                           "comment": review.get("comment", "")}}

    async def post(state: InvoiceState) -> dict:
        posted_by = state.get("review", {}).get("by") or state["user_id"]
        data = await call("erp_post_invoice", tools.post_invoice,
                          summary=lambda d: d["erp_document_number"]
                          + (" (already posted)" if d["already_posted"] else ""),
                          tenant_id=state["tenant_id"], invoice_id=state["invoice_id"],
                          idempotency_key=f"post:{state['invoice_id']}", posted_by=posted_by)
        return {"outcome": {"result": "posted", **data}}

    async def escalate(state: InvoiceState) -> dict:
        inv = state["invoice"]
        reasons = "\n".join(f"- {f['message']}" for f in state.get("findings", []))
        comment = state.get("review", {}).get("comment") or "(no comment)"
        data = await call("tickets_create", tools.create_ticket,
                          summary=lambda d: d["ticket_number"], tenant_id=state["tenant_id"],
                          invoice_id=state["invoice_id"],
                          title=f"Invoice {inv['invoice_number']} rejected in review",
                          body=f"Vendor: {inv['vendor_name']}\nTotal: {inv['total']}\n"
                               f"Reviewer comment: {comment}\nFindings:\n{reasons}",
                          priority="high", created_by=state.get("review", {}).get("by"))
        return {"outcome": {"result": "exception", **data}}

    async def summarize(state: InvoiceState) -> dict:
        inv, out = state["invoice"], state.get("outcome", {})
        head = f"Invoice {inv['invoice_number']} from {inv['vendor_name']} ({_money(inv['total'])})"
        if out.get("result") == "posted":
            how = ("approved by a reviewer" if state.get("review")
                   else "matched the PO and goods receipt within tolerance")
            _say(f"{head} {how} and was posted to the ERP as {out['erp_document_number']}.")
        elif out.get("result") == "exception":
            _say(f"{head} was rejected. Exception ticket {out['ticket_number']} is open for "
                 f"follow-up.")
        return {}

    # ---------------------------------------------------------------- wiring
    g = StateGraph(InvoiceState)
    for name, fn in [("load_file", load_file), ("extract", extract), ("save", save),
                     ("match", match), ("checks", checks), ("decide", decide),
                     ("approval", approval), ("post", post), ("escalate", escalate),
                     ("summarize", summarize)]:
        g.add_node(name, fn)
    g.add_edge(START, "load_file")
    for a, b in [("load_file", "extract"), ("extract", "save"), ("save", "match"),
                 ("match", "checks"), ("checks", "decide")]:
        g.add_edge(a, b)
    g.add_conditional_edges("decide", lambda s: "approval" if s["decision"] == "review"
                            else "post", ["approval", "post"])
    g.add_conditional_edges("approval", lambda s: "post" if s["review"]["approve"]
                            else "escalate", ["post", "escalate"])
    g.add_edge("post", "summarize")
    g.add_edge("escalate", "summarize")
    g.add_edge("summarize", END)
    return g.compile(checkpointer=checkpointer)
