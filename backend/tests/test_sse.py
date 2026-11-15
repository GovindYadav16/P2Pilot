import json

import pytest
from pydantic import ValidationError

from p2pilot import sse


def test_encode_produces_event_stream_frame():
    frame = sse.encode(sse.Token(text="hi"))
    assert frame == 'event: token\ndata: {"type":"token","text":"hi"}\n\n'


def test_encode_drops_none_fields():
    frame = sse.encode(sse.Citation(chunk_id="c1", doc_title="Policy"))
    payload = json.loads(frame.split("data: ", 1)[1])
    assert "page" not in payload and "section" not in payload


def test_parse_round_trips_every_event_type():
    events = [
        sse.RunStarted(run_id="r", conversation_id="c", skill="general"),
        sse.Token(text="x"),
        sse.ToolStarted(call_id="1", tool="erp.get_purchase_order", args={"po": "PO-1"}),
        sse.ToolFinished(call_id="1", tool="erp.get_purchase_order", ok=True),
        sse.Citation(chunk_id="c1", doc_title="Policy", page=3),
        sse.TodoUpdated(todos=[{"content": "check spend", "status": "pending"}]),
        sse.Interrupt(interrupt_id="i1", kind="invoice_approval", payload={"total": 10}),
        sse.ArtifactCreated(file_id="f1", title="Report", mime_type="text/markdown"),
        sse.ErrorEvent(code="model_unavailable", message="down", retryable=True),
        sse.RunFinished(run_id="r", status="ok"),
    ]
    for event in events:
        assert sse.parse(event.model_dump_json()) == event


def test_unknown_event_type_is_rejected():
    with pytest.raises(ValidationError):
        sse.parse({"type": "surprise"})


def test_unknown_error_code_is_rejected():
    with pytest.raises(ValidationError):
        sse.ErrorEvent(code="oops", message="x")


def test_extra_fields_are_rejected_so_the_contract_cannot_drift_silently():
    with pytest.raises(ValidationError):
        sse.parse({"type": "token", "text": "x", "secret": "leak"})


def test_schema_lists_every_event_type():
    schema = json.dumps(sse.json_schema())
    for name in ["run_started", "token", "tool_started", "tool_finished", "citation",
                 "todo_updated", "interrupt", "artifact_created", "error", "run_finished"]:
        assert f'"{name}"' in schema
