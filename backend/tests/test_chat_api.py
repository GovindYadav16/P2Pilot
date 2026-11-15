"""End-to-end tests of /chat with the deterministic fake model (no network, no keys)."""

import pytest
from fastapi.testclient import TestClient

from p2pilot import sse
from p2pilot.agents.llm import FAKE_REPLY
from p2pilot.api.main import app
from p2pilot.config import Settings, get_settings


def read_events(response) -> list:
    events = []
    for frame in response.text.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data: "):
                events.append(sse.parse(line[len("data: "):]))
    return events


@pytest.fixture
def client():
    app.dependency_overrides[get_settings] = lambda: Settings(llm_fake=True, auth_disabled=True,
                                                                checkpointer="memory")
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_chat_streams_tokens_between_start_and_finish(client):
    resp = client.post("/chat", json={"message": "hello"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = read_events(resp)
    assert isinstance(events[0], sse.RunStarted)
    assert isinstance(events[-1], sse.RunFinished) and events[-1].status == "ok"
    tokens = [e.text for e in events if isinstance(e, sse.Token)]
    assert len(tokens) > 1, "reply should arrive as several tokens, not one blob"
    assert "".join(tokens) == FAKE_REPLY


def test_conversation_id_is_kept_across_turns(client):
    first = read_events(client.post("/chat", json={"message": "hi"}))
    conv = first[0].conversation_id
    second = read_events(client.post("/chat", json={"message": "again", "conversation_id": conv}))
    assert second[0].conversation_id == conv


def test_empty_message_is_rejected(client):
    assert client.post("/chat", json={"message": ""}).status_code == 422


def test_model_failure_becomes_an_explicit_error_event(client, monkeypatch):
    import httpx
    import openai

    class Boom:
        async def astream(self, *a, **k):
            raise openai.APIConnectionError(request=httpx.Request("POST", "http://litellm/v1"))
            yield  # pragma: no cover

    monkeypatch.setattr("p2pilot.api.main.build_chat_graph", lambda *a, **k: Boom())
    events = read_events(client.post("/chat", json={"message": "hello"}))

    errors = [e for e in events if isinstance(e, sse.ErrorEvent)]
    assert errors and errors[0].code == "model_unavailable" and errors[0].retryable
    assert isinstance(events[-1], sse.RunFinished) and events[-1].status == "error"


def test_unexpected_failure_does_not_leak_details(client, monkeypatch):
    class Boom:
        async def astream(self, *a, **k):
            raise RuntimeError("postgres://admin:hunter2@db/prod")
            yield  # pragma: no cover

    monkeypatch.setattr("p2pilot.api.main.build_chat_graph", lambda *a, **k: Boom())
    resp = client.post("/chat", json={"message": "hello"})
    assert "hunter2" not in resp.text
    assert any(isinstance(e, sse.ErrorEvent) and e.code == "internal_error"
               for e in read_events(resp))


def test_chat_requires_auth_when_enabled():
    app.dependency_overrides[get_settings] = lambda: Settings(
        llm_fake=True, auth_disabled=False, supabase_jwt_secret="x" * 40, checkpointer="memory"
    )
    try:
        resp = TestClient(app).post("/chat", json={"message": "hello"})
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()
