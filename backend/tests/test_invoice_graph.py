"""The invoice state machine end to end, with fake tools (no DB, no model)."""

from decimal import Decimal

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("reportlab")

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from p2pilot import sse  # noqa: E402
from p2pilot.agents.invoice_graph import SKILL, build_invoice_graph  # noqa: E402
from p2pilot.agents.runner import stream_run  # noqa: E402
from p2pilot.config import Settings  # noqa: E402
from p2pilot.data_gen.invoices import INJECTION_TEXT  # noqa: E402

from .fakes import APPROVER, CLERK, TENANT, FakeTools, make_invoice, pdf_bytes  # noqa: E402

SETTINGS = Settings(llm_fake=True, auth_disabled=True, approval_threshold=200000)


@pytest.fixture
def env(tmp_path):
    tools = FakeTools(tmp_path / "objects")
    graph = build_invoice_graph(tools, llm=None, settings=SETTINGS, checkpointer=MemorySaver())
    return tools, graph, tmp_path


async def run(graph, conv, *, input):
    events = []
    async for frame in stream_run(graph, conversation_id=conv, skill=SKILL, input=input):
        for line in frame.splitlines():
            if line.startswith("data: "):
                events.append(sse.parse(line[6:]))
    return events


def start(tools, tmp, inv):
    file_id = tools.add_pdf(pdf_bytes(inv, tmp))
    return {"tenant_id": TENANT, "user_id": CLERK, "file_id": file_id}


def of(events, kind):
    return [e for e in events if isinstance(e, kind)]


def text(events):
    return "".join(e.text for e in of(events, sse.Token))


async def test_clean_invoice_is_auto_posted(env):
    tools, graph, tmp = env
    events = await run(graph, "c1", input=start(tools, tmp, make_invoice()))
    assert events[-1].status == "ok"
    assert [e.tool for e in of(events, sse.ToolFinished)] == [
        "files_read_text", "extract_invoice", "erp_save_invoice", "erp_get_purchase_order",
        "erp_find_possible_duplicates", "erp_record_decision", "erp_post_invoice"]
    assert all(e.ok for e in of(events, sse.ToolFinished))
    assert "posted to the ERP as ERP-TEST-0001" in text(events)
    assert len(tools.postings) == 1 and not of(events, sse.Interrupt)


async def test_price_variance_pauses_for_review_then_approval_posts(env):
    tools, graph, tmp = env
    inv = make_invoice("price_variance")
    inv.lines[0].unit_price = Decimal("27.00")            # +8% vs PO
    first = await run(graph, "c2", input=start(tools, tmp, inv))
    assert first[-1].status == "interrupted"
    intr = of(first, sse.Interrupt)[0]
    assert intr.kind == "invoice_approval"
    assert "price_variance" in [f["code"] for f in intr.payload["findings"]]
    assert not tools.postings                              # nothing posted while paused
    assert "needs a human review" in text(first)

    second = await run(graph, "c2", input=Command(resume={"approve": True, "by": APPROVER}))
    assert second[-1].status == "ok"
    assert "approved by a reviewer" in text(second)
    assert len(tools.postings) == 1


async def test_rejection_opens_a_ticket_and_never_posts(env):
    tools, graph, tmp = env
    inv = make_invoice("wrong_po", po_number="PO-T-99999")
    first = await run(graph, "c3", input=start(tools, tmp, inv))
    assert first[-1].status == "interrupted"
    second = await run(graph, "c3", input=Command(resume={"approve": False, "by": APPROVER,
                                                          "comment": "Unknown PO"}))
    assert second[-1].status == "ok"
    assert not tools.postings and tools.tickets[0]["number"] == "TCK-00001"
    assert "TCK-00001" in text(second)


async def test_prompt_injection_cannot_get_an_invoice_auto_approved(env):
    tools, graph, tmp = env
    inv = make_invoice("prompt_injection", footer=INJECTION_TEXT)
    events = await run(graph, "c4", input=start(tools, tmp, inv))
    assert events[-1].status == "interrupted"
    codes = [f["code"] for f in of(events, sse.Interrupt)[0].payload["findings"]]
    assert codes == ["suspicious_instructions"] and not tools.postings


async def test_second_submission_of_same_invoice_is_flagged_as_duplicate(env):
    tools, graph, tmp = env
    await run(graph, "c5", input=start(tools, tmp, make_invoice()))
    events = await run(graph, "c6", input=start(tools, tmp, make_invoice()))
    codes = [f["code"] for f in of(events, sse.Interrupt)[0].payload["findings"]]
    assert "possible_duplicate" in codes and len(tools.postings) == 1


async def test_over_limit_invoice_needs_a_human_even_when_it_matches(env):
    tools, graph, tmp = env
    graph = build_invoice_graph(tools, None, settings=Settings(llm_fake=True,
                                approval_threshold=1000), checkpointer=MemorySaver())
    events = await run(graph, "c7", input=start(tools, tmp, make_invoice()))
    codes = [f["code"] for f in of(events, sse.Interrupt)[0].payload["findings"]]
    assert codes == ["over_approval_limit"]


async def test_tool_outage_is_an_explicit_retryable_error(env):
    tools, graph, tmp = env
    tools.fail["erp_get_purchase_order"] = "unavailable"
    events = await run(graph, "c8", input=start(tools, tmp, make_invoice()))
    err = of(events, sse.ErrorEvent)[0]
    assert err.code == "tool_failed" and err.retryable
    assert events[-1].status == "error" and not tools.postings
    failed = [e for e in of(events, sse.ToolFinished) if not e.ok]
    assert failed[0].tool == "erp_get_purchase_order"


async def test_tool_events_never_carry_the_invoice_text_or_tenant(env):
    tools, graph, tmp = env
    events = await run(graph, "c9", input=start(tools, tmp, make_invoice()))
    for e in of(events, sse.ToolStarted):
        assert "tenant_id" not in e.args and "text" not in e.args and "invoice" not in e.args
