"""FastAPI entrypoint.

Run locally:  uvicorn p2pilot.api.main:app --reload --port 8000
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated

import httpx
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel, Field

from p2pilot import __version__
from p2pilot.agents import checkpoint, invoice_graph
from p2pilot.agents.graph import build_chat_graph
from p2pilot.agents.llm import make_chat_model
from p2pilot.agents.runner import stream_run
from p2pilot.api import deps
from p2pilot.api.deps import (
    CAN_APPROVE, CAN_SUBMIT, ActorDep, CheckpointerDep, PrincipalDep, SettingsDep,
    ToolsFactoryDep, require_role,
)
from p2pilot.config import get_settings

app = FastAPI(title="P2Pilot API", version=__version__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = None
    skill: str = "general"
    file_id: str | None = None


class ResumeRequest(BaseModel):
    approve: bool
    comment: str = Field(default="", max_length=2000)


@app.get("/health")
def health(settings: SettingsDep) -> dict:
    return {"status": "ok", "version": __version__, **checkpoint.describe(settings)}


@app.get("/me")
async def me(principal: PrincipalDep, settings: SettingsDep) -> dict:
    """Who am I and which tenants/roles do I have?

    Reads memberships through PostgREST *with the caller's own JWT*, so Postgres RLS
    decides what comes back - the API does not filter by tenant itself.
    """
    if settings.auth_disabled or not principal.token:
        return {"user_id": principal.user_id, "email": principal.email, "memberships": []}
    url = f"{settings.supabase_url.rstrip('/')}/rest/v1/memberships"
    headers = {"apikey": settings.supabase_anon_key, "Authorization": f"Bearer {principal.token}"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params={"select": "tenant_id,role"}, headers=headers)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not load memberships")
    return {"user_id": principal.user_id, "email": principal.email, "memberships": resp.json()}


# ---------------------------------------------------------------- files
@app.post("/files")
async def upload_file(actor: ActorDep, settings: SettingsDep, make_tools: ToolsFactoryDep,
                      file: Annotated[UploadFile, File()]) -> dict:
    """Store a chat attachment (PDF invoice). Bytes -> object storage, pointer -> Postgres."""
    require_role(actor, CAN_SUBMIT, "upload invoices")
    limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File is larger than {settings.max_upload_mb} MB.")
    if not data.startswith(b"%PDF-"):          # check the bytes, not the client's label
        raise HTTPException(415, "Only PDF invoices are accepted.")
    tools = make_tools()
    key = f"attachments/{actor.tenant_id}/{uuid.uuid4()}.pdf"
    tools.store.put(key, data, "application/pdf")
    file_id = tools.register_upload(
        tenant_id=actor.tenant_id, owner_id=actor.user_id, object_key=key,
        filename=(file.filename or "invoice.pdf")[:200], mime_type="application/pdf",
        size=len(data), sha256=hashlib.sha256(data).hexdigest())
    return {"file_id": file_id, "filename": file.filename, "size_bytes": len(data)}


# ---------------------------------------------------------------- chat + skills
@app.post("/chat")
async def chat(req: ChatRequest, principal: PrincipalDep, settings: SettingsDep,
               make_tools: ToolsFactoryDep, saver: CheckpointerDep,
               x_tenant: Annotated[str | None, Header()] = None) -> StreamingResponse:
    conversation_id = req.conversation_id or str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    llm = make_chat_model(
        settings,
        alias="fast",
        metadata={"user_id": principal.user_id, "skill": req.skill, "run_id": run_id},
    )

    if req.skill == invoice_graph.SKILL:
        actor = deps.resolve_actor(principal, settings, x_tenant)
        require_role(actor, CAN_SUBMIT, "submit invoices")
        if not req.file_id:
            raise HTTPException(422, "invoice-processing needs a file_id from POST /files.")
        tools = make_tools()
        if not tools.file_owned_by(file_id=req.file_id, tenant_id=actor.tenant_id,
                                   owner_id=actor.user_id):
            raise HTTPException(404, "File not found.")
        graph = invoice_graph.build_invoice_graph(tools, llm, settings=settings,
                                                  checkpointer=saver)
        stream = stream_run(graph, conversation_id=conversation_id, skill=req.skill,
                            run_id=run_id, input={"tenant_id": actor.tenant_id,
                                                  "user_id": actor.user_id,
                                                  "file_id": req.file_id})
    else:
        graph = build_chat_graph(llm, checkpointer=saver)
        stream = stream_run(graph, conversation_id=conversation_id, skill=req.skill,
                            run_id=run_id, message=req.message)
    return StreamingResponse(stream, media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/approvals")
def pending_approvals(actor: ActorDep, make_tools: ToolsFactoryDep) -> list[dict]:
    """Invoices paused for review in the caller's workspace."""
    return make_tools().pending_reviews(tenant_id=actor.tenant_id)


@app.post("/runs/{conversation_id}/resume")
async def resume_run(conversation_id: str, req: ResumeRequest, actor: ActorDep,
                     settings: SettingsDep, make_tools: ToolsFactoryDep,
                     saver: CheckpointerDep) -> StreamingResponse:
    """Approve or reject a paused invoice run. Approvers only; never the submitter."""
    require_role(actor, CAN_APPROVE, "approve or reject invoices")
    llm = make_chat_model(settings, alias="fast", metadata={"user_id": actor.user_id,
                                                            "skill": invoice_graph.SKILL})
    graph = invoice_graph.build_invoice_graph(make_tools(), llm, settings=settings,
                                              checkpointer=saver)
    config = {"configurable": {"thread_id": conversation_id}}
    snapshot = await graph.aget_state(config)
    values = snapshot.values or {}
    if values.get("tenant_id") != actor.tenant_id:
        hint = (" (Run state is in memory, so an API restart loses paused runs.)"
                if checkpoint.mode(settings) == "memory" else "")
        raise HTTPException(404, "Run not found." + hint)
    if not snapshot.next:
        raise HTTPException(409, "This run is not waiting for a decision.")
    if values.get("user_id") == actor.user_id:
        raise HTTPException(403, "The person who submitted an invoice cannot approve it.")
    command = Command(resume={"approve": req.approve, "comment": req.comment,
                              "by": actor.user_id})
    return StreamingResponse(
        stream_run(graph, conversation_id=conversation_id, skill=invoice_graph.SKILL,
                   input=command),
        media_type="text/event-stream", headers=SSE_HEADERS)
