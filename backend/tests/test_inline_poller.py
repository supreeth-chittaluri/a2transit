"""The in-process poller Render's free tier needs.

Its whole risk is polling more than once per deployment — one poller per uvicorn
worker would multiply the request rate at two unauthenticated endpoints that
neither agency has promised us anything about. So the tests here are mostly
about it staying off unless explicitly asked for, and stopping cleanly.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

from a2transit.config import get_settings
from a2transit.realtime import inline


@pytest.fixture(autouse=True)
def _fast_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two-second startup delay is real behaviour, not something to wait for."""
    monkeypatch.setattr(inline, "STARTUP_DELAY_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _RecordingCycle:
    """Stands in for a real poll cycle, counting how often it is called."""

    def __init__(self, result=None, raises: Exception | None = None) -> None:
        self.calls = 0
        self._result = result
        self._raises = raises

    def __call__(self):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._result


@pytest.mark.asyncio
async def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment with a real worker must not also poll per web process."""
    monkeypatch.delenv("REALTIME_INLINE_POLL", raising=False)
    cycle = _RecordingCycle()
    monkeypatch.setattr(inline, "_cycle", cycle)

    async with inline.lifespan(FastAPI()):
        await asyncio.sleep(0.1)

    assert cycle.calls == 0


@pytest.mark.asyncio
async def test_polls_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REALTIME_INLINE_POLL", "true")
    monkeypatch.setenv("REALTIME_POLL_SECONDS", "1")
    cycle = _RecordingCycle(result=None)
    monkeypatch.setattr(inline, "_cycle", cycle)

    async with inline.lifespan(FastAPI()):
        for _ in range(50):
            if cycle.calls:
                break
            await asyncio.sleep(0.02)

    assert cycle.calls >= 1


@pytest.mark.asyncio
async def test_a_failing_cycle_does_not_kill_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis going away must degrade to schedule-only, not stop the API."""
    monkeypatch.setenv("REALTIME_INLINE_POLL", "true")
    monkeypatch.setattr(inline, "FAILURE_BACKOFF_SECONDS", 0.02)
    cycle = _RecordingCycle(raises=RuntimeError("redis gone"))
    monkeypatch.setattr(inline, "_cycle", cycle)

    async with inline.lifespan(FastAPI()):
        for _ in range(60):
            if cycle.calls >= 2:
                break
            await asyncio.sleep(0.02)

    # Still going after an exception, rather than a dead task nobody noticed.
    assert cycle.calls >= 2


@pytest.mark.asyncio
async def test_shutdown_awaits_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelled and awaited, so the process cannot exit mid-cycle."""
    monkeypatch.setenv("REALTIME_INLINE_POLL", "true")
    started = asyncio.Event()

    async def _forever() -> None:
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(inline, "_poll_forever", _forever)

    async with inline.lifespan(FastAPI()):
        await asyncio.wait_for(started.wait(), timeout=1.0)

    # Nothing left running once the context has exited.
    remaining = [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "a2transit-inline-poller" and not task.done()
    ]
    assert remaining == []


def test_no_redis_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deploying without Upstash configured should quietly do nothing.

    Points REDIS_URL at a closed port and runs the real `_cycle`, rather than
    replacing `store.client_or_none`. The first version of this test stubbed
    that function to return None — which meant it asserted against its own
    mock and happily passed while `_cycle` used the context manager as if it
    were a client. The container caught it; the test had not.
    """
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    get_settings.cache_clear()

    assert inline._cycle() is None


@pytest.mark.network
def test_a_real_cycle_stores_something(monkeypatch: pytest.MonkeyPatch) -> None:
    """The path the container actually runs: real Redis, real feeds.

    Marked `network` so it is opt-in, but it is the test that would have caught
    the context-manager bug, because it is the only one that calls `_cycle`
    with a Redis it can really talk to.
    """
    pytest.importorskip("redis")
    from a2transit.realtime import store as real_store

    with real_store.client_or_none() as client:
        if client is None:
            pytest.skip("Redis unavailable; run docker compose up -d")

    result = inline._cycle()

    assert result is not None
    assert result.vehicles or result.trips or result.alerts
