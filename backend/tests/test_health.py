"""Health and readiness endpoint behaviour.

These run without Postgres or Redis: /health must not touch them at all, and
/ready is exercised against stubbed dependency checks so both the healthy and
degraded branches are covered offline.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from a2transit import __version__
from a2transit.api import health
from a2transit.api.health import DependencyCheck


def test_health_returns_200_without_any_dependencies(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "a2transit", "version": __version__}


def test_ready_reports_ready_when_both_dependencies_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "_check_database", lambda: DependencyCheck(status="ok"))
    monkeypatch.setattr(health, "_check_redis", lambda: DependencyCheck(status="ok"))

    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["redis"]["status"] == "ok"


def test_ready_returns_503_and_names_the_failing_dependency(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "_check_database", lambda: DependencyCheck(status="ok"))
    monkeypatch.setattr(
        health,
        "_check_redis",
        lambda: DependencyCheck(status="unavailable", detail="Connection refused"),
    )

    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["redis"] == {"status": "unavailable", "detail": "Connection refused"}


def test_ready_degrades_rather_than_raising_when_dependencies_are_really_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real connection failure must surface as 503, never as a 500 traceback."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://nobody@127.0.0.1:1/nothing")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    # Settings, engine, and session factory are all lru_cached.
    from a2transit.config import get_settings
    from a2transit.db.session import get_engine, get_session_factory

    for cached in (get_settings, get_engine, get_session_factory):
        cached.cache_clear()

    try:
        response = client.get("/ready")

        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
        assert response.json()["checks"]["database"]["status"] == "unavailable"
    finally:
        for cached in (get_settings, get_engine, get_session_factory):
            cached.cache_clear()


def test_openapi_schema_is_served(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert set(response.json()["paths"]) >= {"/health", "/ready"}
