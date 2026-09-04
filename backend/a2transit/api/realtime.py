"""Live vehicles over a WebSocket, plus alerts and a status endpoint.

The socket does not poll anything. The poller writes to Redis and publishes on
one channel; each connection subscribes to that channel and forwards what
arrives. So thirty open browsers cost the agencies nothing extra, and a
connection that opens between polls gets the current snapshot immediately rather
than staring at an empty map for twenty seconds.

Redis's own client is blocking, so the subscription is drained in a worker
thread and handed to the event loop through a bounded queue. Bounded on purpose:
if a client is too slow to keep up, the right thing is to drop frames — the next
one carries the full picture anyway, since every message is a complete snapshot
rather than a diff.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
from typing import Any

import redis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from a2transit.api.schemas import ApiModel
from a2transit.realtime import store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["realtime"])

#: Frames held for a slow client before the oldest is dropped. Each frame is a
#: whole snapshot, so dropping one loses nothing a later one does not carry.
QUEUE_DEPTH = 4

#: How long the reader waits on Redis before checking whether it should stop.
POLL_TIMEOUT_SECONDS = 1.0


class AlertModel(ApiModel):
    agency: str
    id: str
    header: str
    description: str
    effect: str
    url: str | None
    route_ids: list[str]
    stop_ids: list[str]


class AlertsResponse(ApiModel):
    alerts: list[AlertModel]


class RealtimeStatus(ApiModel):
    #: True when Redis is reachable *and* holds at least one live snapshot.
    live: bool
    redis_available: bool
    #: Seconds since each feed's own header timestamp; null when absent.
    feed_ages: dict[str, int | None]
    vehicle_count: int
    prediction_count: int


@router.get("/realtime/status", response_model=RealtimeStatus, summary="Is realtime live?")
def realtime_status() -> RealtimeStatus:
    with store.client_or_none() as client:
        state = store.status(client)
        vehicles = store.read_vehicles(client)
        predictions = store.read_predictions(client)
    return RealtimeStatus(
        live=state.is_live,
        redis_available=state.available,
        feed_ages=state.ages,
        vehicle_count=len(vehicles),
        prediction_count=len(predictions),
    )


@router.get("/realtime/vehicles", summary="Current vehicle positions")
def vehicles() -> dict[str, Any]:
    """A snapshot, for callers that would rather not hold a socket open."""
    with store.client_or_none() as client:
        found = store.read_vehicles(client)
    return {"vehicles": found, "count": len(found)}


@router.get("/realtime/alerts", response_model=AlertsResponse, summary="Active service alerts")
def alerts() -> AlertsResponse:
    now = int(dt.datetime.now(dt.UTC).timestamp())
    with store.client_or_none() as client:
        found = store.read_alerts(client)
    return AlertsResponse(
        alerts=[
            AlertModel(
                agency=alert.agency.value,
                id=alert.alert_id,
                header=alert.header,
                description=alert.description,
                effect=alert.effect,
                url=alert.url,
                route_ids=list(alert.route_ids),
                stop_ids=list(alert.stop_ids),
            )
            for alert in found
            if alert.is_active_at(now)
        ]
    )


def _drain_channel(queue: asyncio.Queue, loop: asyncio.AbstractEventLoop, stop: Any) -> None:
    """Blocking Redis subscribe loop, run in a worker thread."""
    try:
        client = store.get_client()
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(store.VEHICLE_CHANNEL)
    except redis.RedisError as exc:
        logger.debug("vehicle channel unavailable: %s", exc)
        return

    try:
        while not stop.is_set():
            message = pubsub.get_message(timeout=POLL_TIMEOUT_SECONDS)
            if not message:
                continue
            payload = message.get("data")
            if not payload:
                continue
            # The queue belongs to the event loop, so it must be touched from
            # the loop's thread rather than this one.
            loop.call_soon_threadsafe(_offer, queue, payload)
    except redis.RedisError as exc:
        logger.debug("vehicle channel dropped: %s", exc)
    finally:
        with contextlib.suppress(Exception):
            pubsub.close()
            client.close()


def _offer(queue: asyncio.Queue, payload: str) -> None:
    if queue.full():
        # Drop the oldest rather than block the reader. Every frame is a full
        # snapshot, so the client loses nothing by skipping one.
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(payload)


@router.websocket("/ws/vehicles")
async def vehicle_socket(websocket: WebSocket) -> None:
    await websocket.accept()

    # The current picture first, so a client that connects between polls has
    # something to draw rather than an empty map for up to twenty seconds.
    with store.client_or_none() as client:
        snapshot = store.read_vehicles(client)
    await websocket.send_text(
        json.dumps({"type": "vehicles", "vehicles": snapshot, "snapshot": True})
    )

    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_DEPTH)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    # A threading.Event, because the reader thread cannot wait on an asyncio one.
    import threading

    thread_stop = threading.Event()
    reader = loop.run_in_executor(None, _drain_channel, queue, loop, thread_stop)

    async def watch_for_disconnect() -> None:
        """A client that closes without sending anything is only noticed on read."""
        try:
            while True:
                await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            stop.set()

    watcher = asyncio.create_task(watch_for_disconnect())
    try:
        while not stop.is_set():
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=POLL_TIMEOUT_SECONDS)
            except TimeoutError:
                continue
            await websocket.send_text(payload)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        stop.set()
        thread_stop.set()
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        with contextlib.suppress(Exception):
            await reader
