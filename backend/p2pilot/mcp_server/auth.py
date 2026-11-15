"""Who is calling an MCP tool: derived from the caller's token, never from tool arguments.

Before Sprint 2.5 every MCP tool took `tenant_id` as an argument and trusted it, so any
client (or a prompt-injected agent) could read another tenant's purchase orders by
changing one string. Now the client sends the end user's Supabase JWT on the HTTP
request (`Authorization: Bearer ...`, plus `X-Tenant` if the user has several workspaces)
and the server resolves tenant and role exactly like the REST API does.

The token travels in transport headers, not in tool arguments, so the model never sees it
and cannot alter it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from p2pilot.auth import AuthError, Principal, bearer_token, verify_token
from p2pilot.config import Settings

ANY_MEMBER = {"ap_clerk", "approver", "auditor", "tenant_admin"}
CAN_SUBMIT = {"ap_clerk", "approver", "tenant_admin"}
CAN_APPROVE = {"approver", "tenant_admin"}


class AccessDenied(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code          # "forbidden" | "invalid" (tool envelope codes)
        self.message = message


def _verify(token: str, settings: Settings) -> Principal:
    jwks_url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
    return verify_token(token, audience=settings.jwt_audience,
                        hs256_secret=settings.supabase_jwt_secret, jwks_url=jwks_url)


def _memberships(user_id: str) -> list[dict]:
    from p2pilot import db

    return db.memberships(user_id)


def authorize(headers: Mapping[str, str], settings: Settings, allowed: set[str], *,
              verify: Callable[[str, Settings], Principal] = _verify,
              memberships: Callable[[str], list[dict]] = _memberships) -> tuple[str, str, str]:
    """Return (user_id, tenant_id, role) or raise AccessDenied. Fails closed."""
    try:
        principal = verify(bearer_token(headers.get("authorization")), settings)
    except AuthError as exc:
        raise AccessDenied("forbidden", f"Not authenticated: {exc}") from exc
    rows = memberships(principal.user_id)
    tenant = headers.get("x-tenant")
    if tenant:
        rows = [r for r in rows if tenant in (r["tenant_id"], r.get("slug"))]
    if not rows:
        raise AccessDenied("forbidden", "You are not a member of that workspace.")
    if len(rows) > 1:
        raise AccessDenied("invalid", "You belong to several workspaces; send X-Tenant.")
    row = rows[0]
    if row["role"] not in allowed:
        raise AccessDenied("forbidden", f"Your role ({row['role']}) cannot use this tool.")
    return principal.user_id, row["tenant_id"], row["role"]
