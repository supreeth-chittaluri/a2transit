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


def test_losing_redis_is_degraded_but_still_serving(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Realtime is an enhancement, so its absence must not empty the pool.

    M7 made this concrete: predictions expire and planning falls back to the
    schedule on its own. A 503 here would pull a working planner out of the
    load balancer because a feature it is designed to work without went away.
    """
    monkeypatch.setattr(health, "_check_database", lambda: DependencyCheck(status="ok"))
    monkeypatch.setattr(
        health,
        "_check_redis",
        lambda: DependencyCheck(status="unavailable", detail="Connection refused"),
    )

    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["redis"] == {"status": "unavailable", "detail": "Connection refused"}
    assert "schedule" in body["note"]


def test_losing_the_database_is_a_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without it there is nothing to plan on, so traffic should go elsewhere."""
    monkeypatch.setattr(
        health,
        "_check_database",
        lambda: DependencyCheck(status="unavailable", detail="Connection refused"),
    )
    monkeypatch.setattr(health, "_check_redis", lambda: DependencyCheck(status="ok"))

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


def test_a_real_connection_failure_is_reported_not_raised(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead dependency must surface as a status, never as a 500 traceback."""
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
        assert response.json()["status"] == "unavailable"
        assert response.json()["checks"]["database"]["status"] == "unavailable"
    finally:
        for cached in (get_settings, get_engine, get_session_factory):
            cached.cache_clear()


def test_openapi_schema_is_served(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert set(response.json()["paths"]) >= {"/health", "/ready"}
