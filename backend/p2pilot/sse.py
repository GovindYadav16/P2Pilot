"""The streaming contract between backend and UI.

Every event the agent runtime emits is one of these models. The UI's TypeScript types
(web/src/types/events.ts) mirror this file; `python -m p2pilot.sse` prints the JSON Schema
so the two can be checked against each other.

Depends only on pydantic so it stays cheap to import and test.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter


class _Event(BaseModel):
    model_config = {"extra": "forbid"}


class RunStarted(_Event):
    type: Literal["run_started"] = "run_started"
    run_id: str
    conversation_id: str
    skill: str


class Token(_Event):
    type: Literal["token"] = "token"
    text: str


class ToolStarted(_Event):
    type: Literal["tool_started"] = "tool_started"
    call_id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolFinished(_Event):
    type: Literal["tool_finished"] = "tool_finished"
    call_id: str
    tool: str
    ok: bool
    summary: str = ""


class Citation(_Event):
    type: Literal["citation"] = "citation"
    chunk_id: str
    doc_title: str
    page: int | None = None
    section: str | None = None
    quote: str = ""


class TodoUpdated(_Event):
    type: Literal["todo_updated"] = "todo_updated"
    todos: list[dict[str, Any]]


class Interrupt(_Event):
    """The graph paused and needs a human decision (e.g. invoice approval)."""

    type: Literal["interrupt"] = "interrupt"
    interrupt_id: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ArtifactCreated(_Event):
    type: Literal["artifact_created"] = "artifact_created"
    file_id: str
    title: str
    mime_type: str


ErrorCode = Literal[
    "model_unavailable",  # gateway or provider down / timed out
    "budget_exceeded",
    "forbidden",
    "bad_request",
    "tool_failed",      # a tool returned an error envelope; message is the tool's user_message
    "internal_error",
]


class ErrorEvent(_Event):
    """Errors are always explicit events: a stream never just stops."""

    type: Literal["error"] = "error"
    code: ErrorCode
    message: str  # safe to show to the user; never a stack trace or secret
    retryable: bool = False


class RunFinished(_Event):
    type: Literal["run_finished"] = "run_finished"
    run_id: str
    status: Literal["ok", "error", "interrupted"]


SSEEvent = Annotated[
    Union[
        RunStarted,
        Token,
        ToolStarted,
        ToolFinished,
        Citation,
        TodoUpdated,
        Interrupt,
        ArtifactCreated,
        ErrorEvent,
        RunFinished,
    ],
    Field(discriminator="type"),
]

_adapter: TypeAdapter[SSEEvent] = TypeAdapter(SSEEvent)


def encode(event: _Event) -> str:
    """Serialise one event in text/event-stream format."""
    data = event.model_dump_json(exclude_none=True)
    return f"event: {event.type}\ndata: {data}\n\n"


def parse(data: str | dict[str, Any]) -> _Event:
    """Parse one event's JSON payload back into its model (used by tests and clients)."""
    raw = json.loads(data) if isinstance(data, str) else data
    return _adapter.validate_python(raw)


def json_schema() -> dict[str, Any]:
    return _adapter.json_schema()


if __name__ == "__main__":
    print(json.dumps(json_schema(), indent=2))
