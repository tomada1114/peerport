"""WebSocket endpoint: full snapshot on connect, then a diff stream.

Per `docs/design/architecture.md` §4, the wire protocol is downstream-only
over `/ws` — Keeper commands go over REST (`server/api.py`). Any inbound
WS frame is tolerated but never acted on: malformed JSON is logged and
ignored (REQ-011) rather than closing the connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from peerport.server.state import snapshot

if TYPE_CHECKING:
    from starlette.datastructures import State

    from peerport.server.state import Broadcaster, WorldState

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Accept a WS connection: send a snapshot, then stream diffs.

    Args:
        websocket: The inbound WebSocket connection. `app.state.world_state`
            and `app.state.broadcaster` must already be set (done by
            `server/app.py`'s lifespan).
    """
    await websocket.accept()
    state: WorldState = websocket.app.state.world_state
    broadcaster: Broadcaster = websocket.app.state.broadcaster

    await websocket.send_json(_build_snapshot(websocket.app.state, state))
    simulation = getattr(websocket.app.state, "simulation", None)
    if simulation is not None:
        await websocket.send_json(simulation.clock_frame())

    queue: asyncio.Queue[dict[str, Any]] = broadcaster.subscribe()
    try:
        await _serve(websocket, queue)
    finally:
        broadcaster.unsubscribe(queue)


def _build_snapshot(app_state: State, state: WorldState) -> dict[str, Any]:
    """Build the snapshot frame, folding in current degraded-state status.

    Every (re)connect gets a fresh snapshot as its first message
    (`net.js`'s header comment); merging fog/hard-stop/low-power into it
    lets a reconnecting client resync those Bridge banners immediately
    instead of only ever learning about them from a live `state` frame it
    may have missed while disconnected (finding).

    Args:
        app_state: The FastAPI app's `state`. `simulation`/`budget_guard`
            are read via `getattr` because both are optional: the pre-#13
            bare-`WorldState` skeleton some tests still exercise has
            neither wired.
        state: Current world state, forwarded to `snapshot()`.

    Returns:
        The `{"t": "snapshot", ...}` payload, extended with `fog`/
        `hard_stop`/`low_power` keys whenever a simulation/budget guard
        is actually wired.
    """
    payload = snapshot(state)
    simulation = getattr(app_state, "simulation", None)
    if simulation is not None:
        payload["fog"] = {
            "active": simulation.fog_active,
            "status": simulation.fog_status,
        }
        payload["hard_stop"] = simulation.hard_stop
    budget_guard = getattr(app_state, "budget_guard", None)
    if budget_guard is not None:
        payload["low_power"] = budget_guard.low_power
    return payload


async def _serve(websocket: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
    """Run the forward-diffs and receive-frames loops until either ends."""
    forward_task = asyncio.create_task(_forward_diffs(websocket, queue))
    receive_task = asyncio.create_task(_receive_client_frames(websocket))
    done, pending = await asyncio.wait(
        {forward_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    for task in done:
        error = task.exception()
        if error is not None and not isinstance(error, WebSocketDisconnect):
            raise error


async def _forward_diffs(
    websocket: WebSocket, queue: asyncio.Queue[dict[str, Any]]
) -> None:
    """Forward every published diff to this client until disconnect."""
    while True:
        message = await queue.get()
        await websocket.send_json(message)


async def _receive_client_frames(websocket: WebSocket) -> None:
    """Drain inbound frames, tolerating malformed JSON (REQ-011)."""
    while True:
        raw = await websocket.receive_text()
        try:
            json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("ignoring malformed WS message: %r", raw)
