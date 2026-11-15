"""FastAPI dependencies (dependency injection keeps handlers thin and testable)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, status

from p2pilot.auth import DEV_USER_ID, AuthError, Principal, bearer_token, verify_token
from p2pilot.config import Settings, get_settings
from p2pilot.storage import LocalObjectStore, ObjectStore, SupabaseObjectStore

log = logging.getLogger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_principal(
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if settings.auth_disabled:
        return Principal(user_id=DEV_USER_ID, email=settings.dev_user_email)
    try:
        token = bearer_token(authorization)
        jwks_url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        return verify_token(
            token,
            audience=settings.jwt_audience,
            hs256_secret=settings.supabase_jwt_secret,
            jwks_url=jwks_url,
        )
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


PrincipalDep = Annotated[Principal, Depends(get_principal)]


# ---------------------------------------------------------------- tenant + role
@dataclass(frozen=True)
class Actor:
    """Who is calling, in which tenant, with which role."""

    user_id: str
    email: str | None
    tenant_id: str
    role: str


CAN_SUBMIT = {"ap_clerk", "approver", "tenant_admin"}
CAN_APPROVE = {"approver", "tenant_admin"}


def resolve_actor(principal: Principal, settings: Settings, tenant_header: str | None) -> Actor:
    from p2pilot import db

    user_id = principal.user_id
    if settings.auth_disabled:
        user_id = db.user_id_by_email(settings.dev_user_email) or ""
        if not user_id:
            raise HTTPException(503, f"Dev user {settings.dev_user_email} not found; run the seeder.")
    rows = db.memberships(user_id)
    if not rows:
        raise HTTPException(403, "You are not a member of any workspace.")
    if tenant_header:
        rows = [r for r in rows if tenant_header in (r["tenant_id"], r["slug"])]
        if not rows:
            raise HTTPException(403, "You are not a member of that workspace.")
    elif len(rows) > 1:
        raise HTTPException(400, "You belong to several workspaces; send an X-Tenant header.")
    return Actor(user_id=user_id, email=principal.email, tenant_id=rows[0]["tenant_id"],
                 role=rows[0]["role"])


def get_actor(principal: PrincipalDep, settings: SettingsDep,
              x_tenant: Annotated[str | None, Header()] = None) -> Actor:
    return resolve_actor(principal, settings, x_tenant)


ActorDep = Annotated[Actor, Depends(get_actor)]


def require_role(actor: Actor, allowed: set[str], action: str) -> None:
    if actor.role not in allowed:
        raise HTTPException(403, f"Your role ({actor.role}) cannot {action}.")


# ---------------------------------------------------------------- storage + tools
def build_store(settings: Settings) -> ObjectStore:
    if settings.object_store == "supabase":
        return SupabaseObjectStore(settings.supabase_url, settings.supabase_service_role_key,
                                   settings.object_store_bucket)
    return LocalObjectStore(settings.object_store_root)


def get_tools_factory(settings: SettingsDep) -> Callable[[], Any]:
    """Return a factory, so endpoints that never touch the DB never open a pool."""

    def make():
        from p2pilot.db import get_pool
        from p2pilot.tools.p2p import P2PTools

        return P2PTools(get_pool(), build_store(settings))

    return make


ToolsFactoryDep = Annotated[Callable[[], Any], Depends(get_tools_factory)]


# ---------------------------------------------------------------- run state
def get_checkpointer_dep(settings: SettingsDep):
    """LangGraph checkpointer (Postgres or memory; see p2pilot.agents.checkpoint)."""
    from p2pilot.agents.checkpoint import get_checkpointer

    try:
        return get_checkpointer(settings)
    except Exception as exc:  # noqa: BLE001 - fail loudly, but with a safe message
        log.error("checkpointer unavailable: %s", exc)
        raise HTTPException(503, "Run storage is unavailable. Is the database running?") from exc


CheckpointerDep = Annotated[Any, Depends(get_checkpointer_dep)]
