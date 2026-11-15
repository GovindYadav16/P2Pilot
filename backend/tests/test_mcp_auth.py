"""MCP caller identity comes from the token, never from tool arguments (no DB, no MCP)."""

import pytest

from p2pilot.auth import AuthError, Principal
from p2pilot.config import Settings
from p2pilot.mcp_server.auth import ANY_MEMBER, CAN_APPROVE, AccessDenied, authorize

ACME, BHARAT = "aaaaaaaa-0000-4000-8000-000000000001", "bbbbbbbb-0000-4000-8000-000000000002"
S = Settings()


def verify(token, settings):
    if token != "good":
        raise AuthError("Invalid token")
    return Principal(user_id="u1", token=token)


def one_tenant(user_id):
    return [{"tenant_id": ACME, "slug": "acme", "role": "ap_clerk"}]


def two_tenants(user_id):
    return [{"tenant_id": ACME, "slug": "acme", "role": "ap_clerk"},
            {"tenant_id": BHARAT, "slug": "bharat", "role": "approver"}]


def call(headers, allowed=ANY_MEMBER, memberships=one_tenant):
    return authorize(headers, S, allowed, verify=verify, memberships=memberships)


def test_valid_token_resolves_tenant_and_role():
    assert call({"authorization": "Bearer good"}) == ("u1", ACME, "ap_clerk")


@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer bad"},
                                     {"authorization": "Basic good"}])
def test_missing_or_bad_token_is_refused(headers):
    with pytest.raises(AccessDenied) as exc:
        call(headers)
    assert exc.value.code == "forbidden"


def test_role_is_enforced():
    with pytest.raises(AccessDenied, match="ap_clerk"):
        call({"authorization": "Bearer good"}, allowed=CAN_APPROVE)


def test_multi_tenant_user_must_choose_and_cannot_pick_foreign_tenant():
    with pytest.raises(AccessDenied) as exc:
        call({"authorization": "Bearer good"}, memberships=two_tenants)
    assert exc.value.code == "invalid"
    assert call({"authorization": "Bearer good", "x-tenant": "bharat"},
                memberships=two_tenants) == ("u1", BHARAT, "approver")
    with pytest.raises(AccessDenied, match="not a member"):
        call({"authorization": "Bearer good", "x-tenant": "someone-else"},
             memberships=two_tenants)


def test_mcp_tools_no_longer_accept_tenant_id():
    """Static check of server.py, so it runs without the MCP SDK or a database."""
    import ast
    from pathlib import Path

    src = Path(__file__).parents[1] / "p2pilot" / "mcp_server" / "server.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    tools = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.decorator_list]
    assert len(tools) == 4
    for fn in tools:
        args = [a.arg for a in fn.args.args]
        assert "tenant_id" not in args and "ctx" in args, fn.name
