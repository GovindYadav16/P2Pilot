"""MCP server exposing the P2P tools.

    python -m p2pilot.mcp_server.server            # streamable HTTP on 127.0.0.1:8100/mcp
    npx @modelcontextprotocol/inspector            # connect to http://127.0.0.1:8100/mcp
                                                   # and add header Authorization: Bearer <jwt>

The invoice state machine calls the same tool functions in-process (ADR 0003). This
server is for agents that choose tools dynamically (policy-qa, vendor-research in Sprint 3)
and for inspection/testing.

Security (Sprint 2.5): tools no longer take a tenant_id. Every call must carry the end
user's Supabase JWT in the Authorization header (and X-Tenant if they have several
workspaces); tenant and role come from the token and the memberships table, exactly as in
the REST API (see mcp_server/auth.py). Calls without a valid token get a "forbidden"
envelope. The server still binds to localhost only.
"""

from __future__ import annotations

from mcp.server.fastmcp import Context, FastMCP

from p2pilot.api.deps import build_store
from p2pilot.config import get_settings
from p2pilot.db import get_pool
from p2pilot.mcp_server.auth import ANY_MEMBER, CAN_APPROVE, CAN_SUBMIT, AccessDenied, authorize
from p2pilot.tools.base import ToolError, ToolResult
from p2pilot.tools.p2p import P2PTools

settings = get_settings()
mcp = FastMCP("p2pilot-tools", host="127.0.0.1", port=8100)
_tools = P2PTools(get_pool(), build_store(settings))


def _headers(ctx: Context) -> dict[str, str]:
    """HTTP headers of the current MCP request (empty on transports without HTTP)."""
    request = getattr(ctx.request_context, "request", None)
    headers = getattr(request, "headers", None) or {}
    return {k.lower(): v for k, v in headers.items()}


def _caller(ctx: Context, allowed: set[str]) -> tuple[str, str, str] | dict:
    """(user_id, tenant_id, role), or a ready-to-return error envelope. Fails closed."""
    try:
        return authorize(_headers(ctx), settings, allowed)
    except AccessDenied as exc:
        return ToolResult(ok=False, error=ToolError(code=exc.code, user_message=exc.message)
                          ).model_dump(mode="json")


@mcp.tool()
def erp_get_purchase_order(po_number: str, ctx: Context) -> dict:
    """Look up a purchase order (in the caller's workspace) with its lines and received
    quantities. Returns data=null if the PO does not exist."""
    who = _caller(ctx, ANY_MEMBER)
    if isinstance(who, dict):
        return who
    _, tenant_id, _ = who
    return _tools.get_purchase_order(tenant_id, po_number).model_dump(mode="json")


@mcp.tool()
def erp_find_possible_duplicates(invoice_id: str, vendor_gstin: str, invoice_number: str,
                                 total: str, ctx: Context) -> dict:
    """Find earlier invoices from the same vendor with the same number, or the same total
    within 30 days. At most 5 results."""
    who = _caller(ctx, ANY_MEMBER)
    if isinstance(who, dict):
        return who
    _, tenant_id, _ = who
    return _tools.find_possible_duplicates(tenant_id, invoice_id, vendor_gstin,
                                           invoice_number, total).model_dump(mode="json")


@mcp.tool()
def erp_post_invoice(invoice_id: str, idempotency_key: str, ctx: Context) -> dict:
    """Post an APPROVED invoice to the ERP. Approvers only. Idempotent: reusing
    idempotency_key never double-posts. Refuses invoices that are not approved."""
    who = _caller(ctx, CAN_APPROVE)
    if isinstance(who, dict):
        return who
    user_id, tenant_id, _ = who
    return _tools.post_invoice(tenant_id, invoice_id, idempotency_key,
                               user_id).model_dump(mode="json")


@mcp.tool()
def tickets_create(title: str, body: str, ctx: Context, priority: str = "medium",
                   invoice_id: str | None = None) -> dict:
    """Open an exception ticket (priority: low | medium | high)."""
    who = _caller(ctx, CAN_SUBMIT)
    if isinstance(who, dict):
        return who
    user_id, tenant_id, _ = who
    return _tools.create_ticket(tenant_id, invoice_id, title, body, priority,
                                user_id).model_dump(mode="json")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
