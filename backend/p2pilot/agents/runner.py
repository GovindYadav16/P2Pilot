"""Runs a graph and turns its stream into SSE contract events.

This is the single place where LangGraph's stream format is mapped to the UI contract:
- "messages" mode -> token events, but only from nodes that produce user-facing text
  (so an extraction call's raw JSON never leaks into the chat)
- "custom" mode   -> events nodes emit themselves (tool steps, status text)
- "updates" mode  -> interrupt events when the graph pauses for a human
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessageChunk, HumanMessage
from pydantic import ValidationError

from p2pilot import sse
from p2pilot.tools.base import ToolFailed

log = logging.getLogger(__name__)

TEXT_NODES = {"respond"}


def _classify_error(exc: BaseException) -> sse.ErrorEvent:
    """Map exceptions to safe, user-facing errors. Details go to logs, never to the client."""
    if isinstance(exc, ToolFailed):
        return sse.ErrorEvent(code="tool_failed", message=exc.error.user_message,
                              retryable=exc.error.retryable)
    try:
        import openai

        if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
            return sse.ErrorEvent(
                code="model_unavailable",
                message="The language model is not reachable right now. Please try again.",
                retryable=True,
            )
        if isinstance(exc, openai.RateLimitError):
            return sse.ErrorEvent(
                code="budget_exceeded",
                message="Usage limit reached for this workspace.",
                retryable=False,
            )
    except ImportError:  # openai is optional in fake mode
        pass
    return sse.ErrorEvent(
        code="internal_error",
        message="Something went wrong while running the agent.",
        retryable=False,
    )


def _text_of(chunk: AIMessageChunk) -> str:
    content: Any = chunk.content
    if isinstance(content, str):
        return content
    # Some providers stream a list of content blocks.
    return "".join(
        part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
    )


async def stream_run(
    graph: Any,
    *,
    conversation_id: str,
    skill: str,
    message: str | None = None,
    input: Any = None,
    run_id: str | None = None,
) -> AsyncIterator[str]:
    """Yield encoded SSE strings for one turn (or one resume). Always ends with run_finished.

    Pass `message` for a chat turn, or `input` for anything else (initial skill state, or a
    langgraph Command(resume=...) to continue an interrupted run).
    """
    run_id = run_id or str(uuid.uuid4())
    yield sse.encode(sse.RunStarted(run_id=run_id, conversation_id=conversation_id, skill=skill))

    graph_input = {"messages": [HumanMessage(content=message)]} if message is not None else input
    config = {"configurable": {"thread_id": conversation_id}, "run_id": run_id}
    status = "ok"
    try:
        async for mode, chunk in graph.astream(
            graph_input, config, stream_mode=["messages", "custom", "updates"]
        ):
            if mode == "messages":
                msg, meta = chunk
                if isinstance(msg, AIMessageChunk) and meta.get("langgraph_node") in TEXT_NODES:
                    text = _text_of(msg)
                    if text:
                        yield sse.encode(sse.Token(text=text))
            elif mode == "custom":
                try:
                    yield sse.encode(sse.parse(chunk))
                except ValidationError:
                    log.warning("dropping malformed custom event from graph: %r", chunk)
            elif mode == "updates" and isinstance(chunk, dict) and "__interrupt__" in chunk:
                for intr in chunk["__interrupt__"]:
                    value = intr.value if isinstance(intr.value, dict) else {"value": intr.value}
                    yield sse.encode(sse.Interrupt(
                        interrupt_id=str(getattr(intr, "id", "") or run_id),
                        kind=str(value.get("kind", "decision")), payload=value))
                status = "interrupted"
    except Exception as exc:  # noqa: BLE001 - every failure must become an explicit event
        log.exception("run %s failed", run_id)
        status = "error"
        yield sse.encode(_classify_error(exc))

    yield sse.encode(sse.RunFinished(run_id=run_id, status=status))
