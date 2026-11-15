"""HTTP-level tests: upload validation, role checks, maker-checker, full approve flow."""

import pytest

pytest.importorskip("reportlab")

from fastapi.testclient import TestClient  # noqa: E402

from p2pilot import sse  # noqa: E402
from p2pilot.api import deps  # noqa: E402
from p2pilot.api.main import app  # noqa: E402
from p2pilot.config import Settings, get_settings  # noqa: E402

from .fakes import APPROVER, CLERK, TENANT, FakeTools, make_invoice, pdf_bytes  # noqa: E402

AUDITOR = "44444444-4444-4444-8444-444444444444"
APPROVER_2 = "55555555-5555-4555-8555-555555555555"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    tools = FakeTools(tmp_path / "objects")
    who = {"user": CLERK, "role": "ap_clerk"}

    def fake_actor(principal, settings, header):
        return deps.Actor(user_id=who["user"], email=None, tenant_id=TENANT, role=who["role"])

    monkeypatch.setattr(deps, "resolve_actor", fake_actor)
    app.dependency_overrides[get_settings] = lambda: Settings(llm_fake=True, auth_disabled=True,
                                                                checkpointer="memory")
    app.dependency_overrides[deps.get_tools_factory] = lambda: (lambda: tools)
    yield TestClient(app), tools, who, tmp_path
    app.dependency_overrides.clear()


def events_of(resp):
    out = []
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            out.append(sse.parse(line[6:]))
    return out


def upload(client, data, name="inv.pdf"):
    return client.post("/files", files={"file": (name, data, "application/pdf")})


def test_upload_rejects_non_pdf_even_if_labelled_pdf(ctx):
    client, *_ = ctx
    assert upload(client, b"MZ\x90\x00 not a pdf").status_code == 415


def test_upload_rejects_too_large(ctx, monkeypatch):
    client, *_ = ctx
    app.dependency_overrides[get_settings] = lambda: Settings(llm_fake=True, auth_disabled=True,
                                                              max_upload_mb=0)
    assert upload(client, b"%PDF-1.4 tiny").status_code == 413


def test_auditor_cannot_upload(ctx):
    client, _, who, tmp = ctx
    who.update(user=AUDITOR, role="auditor")
    assert upload(client, b"%PDF-1.4").status_code == 403


def test_cannot_process_someone_elses_file(ctx):
    client, tools, who, tmp = ctx
    file_id = tools.add_pdf(pdf_bytes(make_invoice(), tmp), owner_id=APPROVER)
    resp = client.post("/chat", json={"message": "process", "skill": "invoice-processing",
                                      "file_id": file_id})
    assert resp.status_code == 404


def test_full_flow_submit_then_maker_checker_then_approve(ctx):
    client, tools, who, tmp = ctx
    inv = make_invoice("price_variance")
    from decimal import Decimal
    inv.lines[0].unit_price = Decimal("27.00")
    r = upload(client, pdf_bytes(inv, tmp))
    assert r.status_code == 200
    file_id = r.json()["file_id"]

    resp = client.post("/chat", json={"message": "process", "skill": "invoice-processing",
                                      "file_id": file_id})
    events = events_of(resp)
    assert events[-1].status == "interrupted"
    conv = events[0].conversation_id

    # A clerk cannot approve.
    assert client.post(f"/runs/{conv}/resume", json={"approve": True}).status_code == 403

    # An approver who submitted the invoice cannot approve their own submission.
    who.update(user=CLERK, role="approver")
    assert client.post(f"/runs/{conv}/resume", json={"approve": True}).status_code == 403

    # A different approver can; the run finishes and posts exactly once.
    who.update(user=APPROVER_2, role="approver")
    assert len(client.get("/approvals").json()) == 1
    done = events_of(client.post(f"/runs/{conv}/resume", json={"approve": True,
                                                               "comment": "ok, price agreed"}))
    assert done[-1].status == "ok" and len(tools.postings) == 1

    # Resuming again is refused: the run is no longer waiting.
    assert client.post(f"/runs/{conv}/resume", json={"approve": True}).status_code == 409


def test_resume_unknown_run_is_404(ctx):
    client, _, who, _ = ctx
    who.update(user=APPROVER, role="approver")
    assert client.post("/runs/nope/resume", json={"approve": True}).status_code == 404


def test_invoice_skill_requires_a_file(ctx):
    client, *_ = ctx
    r = client.post("/chat", json={"message": "x", "skill": "invoice-processing"})
    assert r.status_code == 422
