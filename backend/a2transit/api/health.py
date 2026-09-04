"""Liveness and readiness endpoints.

Two endpoints, because they answer different questions and callers act on them
differently:

  /health  — is the process up? Always 200 if the app can respond. This is what
             a platform's process supervisor should poll; failing it because
             Postgres blipped would restart a perfectly healthy container.

  /ready   — can the app actually serve traffic? 503 when a *required*
             dependency is down, so a load balancer stops routing to it.

Only Postgres is required. Redis holds realtime, and M7 made that an
enhancement by construction: predictions expire, and planning falls back to the
schedule on its own. Failing readiness because Redis is unreachable would pull
a perfectly serviceable planner out of the load balancer over the loss of a
feature it is designed to work without — so a Redis outage reports `degraded`
with a 200, which is visible to a human reading /ready and invisible to a
health check that only looks at the status code.
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
    status: Literal["ready", "degraded", "unavailable"]
    checks: dict[str, DependencyCheck]
    #: What is lost while degraded, in words, for whoever is reading this at
    #: 3am wondering whether it matters.
    note: str | None = None


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


#: Dependencies without which the app cannot answer at all.
REQUIRED = ("database",)


@router.get("/ready", response_model=ReadyResponse)
def ready(response: Response) -> ReadyResponse:
    checks = {"database": _check_database(), "redis": _check_redis()}

    if any(checks[name].status != "ok" for name in REQUIRED):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(
            status="unavailable",
            checks=checks,
            note="No database: nothing can be planned.",
        )

    if checks["redis"].status != "ok":
        return ReadyResponse(
            status="degraded",
            checks=checks,
            note="No Redis: planning from the schedule, without live delays or vehicles.",
        )

    return ReadyResponse(status="ready", checks=checks)
