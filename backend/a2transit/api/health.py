"""Liveness and readiness endpoints.

Two endpoints, because they answer different questions and callers act on them
differently:

  /health  — is the process up? Always 200 if the app can respond. This is what
             a platform's process supervisor should poll; failing it because
             Postgres blipped would restart a perfectly healthy container.

  /ready   — can the app actually serve traffic? 503 when a dependency is down,
             so a load balancer stops routing to it.
"""

from __future__ import annotations

from typing import Literal

import redis
from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from a2transit.config import get_settings
from a2transit.db.session import get_engine

router = APIRouter(tags=["health"])

CheckStatus = Literal["ok", "unavailable"]


class DependencyCheck(BaseModel):
    status: CheckStatus
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str = "a2transit"
    version: str


class ReadyResponse(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, DependencyCheck]


def _check_database() -> DependencyCheck:
    try:
        with get_engine().connect() as connection:
            # Confirm PostGIS is installed, not just that Postgres answers —
            # an ingest into a PostGIS-less database fails much later and less
            # legibly than it does here.
            connection.execute(text("SELECT PostGIS_Version()"))
    except Exception as exc:
        return DependencyCheck(status="unavailable", detail=_summarise(exc))
    return DependencyCheck(status="ok")


def _check_redis() -> DependencyCheck:
    client = None
    try:
        client = redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=2)
        client.ping()
    except Exception as exc:
        return DependencyCheck(status="unavailable", detail=_summarise(exc))
    finally:
        if client is not None:
            client.close()
    return DependencyCheck(status="ok")


def _summarise(exc: Exception) -> str:
    """First line of an exception, truncated — connection errors are paragraphs."""
    return str(exc).strip().splitlines()[0][:200] or exc.__class__.__name__


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    from a2transit import __version__

    return HealthResponse(status="ok", version=__version__)


@router.get("/ready", response_model=ReadyResponse)
def ready(response: Response) -> ReadyResponse:
    checks = {"database": _check_database(), "redis": _check_redis()}
    degraded = any(check.status != "ok" for check in checks.values())
    if degraded:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(status="degraded" if degraded else "ready", checks=checks)
