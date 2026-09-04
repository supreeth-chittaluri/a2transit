"""Polling from inside the API process, for hosts with no worker tier.

`realtime.poller` runs as its own process, which is the right shape: six feeds
polled once, by one consumer, whatever the API is doing. It is what fly.toml
declares and what a machine with a worker plan should run.

Render's free plan has no worker. Given that, the choice is between dropping
realtime from the public demo and polling inside the web process, and the second
is better than it first sounds:

  * The free tier sleeps when idle. A separate poller hitting two agencies every
    20 seconds around the clock to keep a sleeping API's data warm is work
    nobody benefits from — the data has expired by the time anyone arrives.
  * Polling in-process means the feeds are fetched exactly when somebody is
    looking, which is the only time freshness matters.

What makes it safe here is that the API runs one worker per instance, for
reasons that predate this (the timetable cache is per-process and a service date
costs ~120 MB). So "one poller per process" is still one poller. If that ever
stops being true, this must not be enabled — hence the explicit setting rather
than a guess based on the environment: two workers means two pollers means twice
the request rate at somebody else's unauthenticated endpoint.

Enabled with REALTIME_INLINE_POLL=true. Off by default, so a deployment with a
real worker cannot accidentally poll twice.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI

from a2transit.config import get_settings
from a2transit.realtime import store
from a2transit.realtime.poller import poll_once

logger = logging.getLogger("a2transit.realtime.inline")

#: How long to wait before the first poll, so startup answers /ready promptly
#: rather than holding the port while six feeds are fetched. A platform that
#: health-checks on boot should see a listening socket immediately.
STARTUP_DELAY_SECONDS = 2.0

#: Backoff after a cycle in which every feed failed. Retrying a dead endpoint
#: every 20 s from a host somebody else is paying for is rude.
FAILURE_BACKOFF_SECONDS = 120.0


async def _poll_forever() -> None:
    settings = get_settings()
    interval = settings.realtime_poll_seconds

    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    logger.info("inline realtime poller started (every %ss)", interval)

    while True:
        delay = interval
        try:
            # Both are blocking: httpx.Client and redis-py. Run the cycle on a
            # worker thread so a slow agency endpoint cannot stall the event
            # loop and, with it, every request the API is serving.
            result = await asyncio.to_thread(_cycle)
            if result is None:
                delay = FAILURE_BACKOFF_SECONDS
            else:
                logger.debug("inline poll: %s", result)
                if result.failures and not (result.vehicles or result.trips or result.alerts):
                    delay = FAILURE_BACKOFF_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception:
            # A cycle already swallows feed failures, so reaching here means
            # something structural — Redis gone, most likely. Keep the loop
            # alive; routing degrades to schedule-only on its own.
            logger.exception("inline poll cycle failed")
            delay = FAILURE_BACKOFF_SECONDS

        await asyncio.sleep(delay)


def _cycle():
    # client_or_none is a context manager, not a client — it owns closing the
    # connection, and yields None rather than raising when Redis is unreachable.
    with store.client_or_none() as client:
        if client is None:
            logger.debug("no Redis; skipping inline poll")
            return None
        with httpx.Client(timeout=15.0, follow_redirects=True) as http:
            return poll_once(client, http=http)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the inline poller if configured, and stop it cleanly on shutdown."""
    settings = get_settings()
    task: asyncio.Task | None = None

    if settings.realtime_inline_poll:
        task = asyncio.create_task(_poll_forever(), name="a2transit-inline-poller")
    else:
        logger.debug("inline polling disabled")

    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            # Awaited rather than abandoned: without this the process can exit
            # mid-cycle holding a Redis connection, and the next boot inherits
            # a half-written snapshot.
            with suppress(asyncio.CancelledError):
                await task
            logger.info("inline realtime poller stopped")
