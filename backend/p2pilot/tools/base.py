"""The tool error envelope: every tool returns {ok, data, error}, never raises to the agent.

Rules (ADR 0003): no silent drops (a failure is never an empty result), no leaked internals
(user_message is safe to show; details go to logs), and `retryable` tells the caller whether
trying again can help.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Literal

from pydantic import BaseModel

log = logging.getLogger("p2pilot.tools")

ErrorCode = Literal["not_found", "invalid", "forbidden", "unavailable", "internal_error"]


class ToolError(BaseModel):
    code: ErrorCode
    retryable: bool = False
    user_message: str


class ToolResult(BaseModel):
    ok: bool
    data: Any = None
    error: ToolError | None = None


class ToolProblem(Exception):
    """Raise inside a tool for an expected, user-explainable failure."""

    def __init__(self, code: ErrorCode, user_message: str, retryable: bool = False):
        super().__init__(user_message)
        self.error = ToolError(code=code, retryable=retryable, user_message=user_message)


class ToolFailed(Exception):
    """Raised by callers (e.g. the graph) when a tool returned ok=False."""

    def __init__(self, tool: str, error: ToolError):
        super().__init__(f"{tool}: {error.code}")
        self.tool = tool
        self.error = error


def tool(name: str):
    """Wrap a tool function so it always returns a ToolResult."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> ToolResult:
            try:
                return ToolResult(ok=True, data=fn(*args, **kwargs))
            except ToolProblem as exc:
                return ToolResult(ok=False, error=exc.error)
            except Exception as exc:  # noqa: BLE001 - convert everything into the envelope
                if _is_connection_error(exc):
                    log.warning("tool %s unavailable: %s", name, type(exc).__name__)
                    return ToolResult(ok=False, error=ToolError(
                        code="unavailable", retryable=True,
                        user_message="A backend system is temporarily unavailable."))
                log.exception("tool %s failed", name)
                return ToolResult(ok=False, error=ToolError(
                    code="internal_error", user_message="The tool failed unexpectedly."))

        wrapper.tool_name = name
        return wrapper

    return deco


def _is_connection_error(exc: Exception) -> bool:
    try:
        import psycopg
        import psycopg_pool

        if isinstance(exc, (psycopg.OperationalError, psycopg_pool.PoolTimeout)):
            return True
    except ImportError:
        pass
    return isinstance(exc, (ConnectionError, TimeoutError))
