"""Choosing where run state lives (no database needed)."""

import pytest

pytest.importorskip("langgraph")

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from p2pilot.agents import checkpoint  # noqa: E402
from p2pilot.config import Settings  # noqa: E402


@pytest.mark.parametrize("choice, url, expected", [
    ("auto", "postgresql://x", "postgres"),
    ("auto", "", "memory"),
    ("memory", "postgresql://x", "memory"),
    ("postgres", "", "postgres"),
])
def test_mode_resolution(choice, url, expected):
    assert checkpoint.mode(Settings(checkpointer=choice, p2pilot_db_url=url)) == expected


def test_memory_mode_returns_shared_memory_saver():
    s = Settings(checkpointer="memory")
    assert isinstance(checkpoint.get_checkpointer(s), MemorySaver)
    assert checkpoint.get_checkpointer(s) is checkpoint.get_checkpointer(s)


def test_postgres_without_url_fails_clearly(monkeypatch):
    monkeypatch.setattr(checkpoint, "_postgres", None)
    with pytest.raises(RuntimeError, match="P2PILOT_DB_URL"):
        checkpoint.get_checkpointer(Settings(checkpointer="postgres", p2pilot_db_url=""))


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        checkpoint.mode(Settings(checkpointer="redis"))


def test_health_reports_backend():
    from fastapi.testclient import TestClient

    from p2pilot.api.main import app
    from p2pilot.config import get_settings

    app.dependency_overrides[get_settings] = lambda: Settings(checkpointer="memory")
    try:
        assert TestClient(app).get("/health").json()["checkpointer"] == "memory"
    finally:
        app.dependency_overrides.clear()
