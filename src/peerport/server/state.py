"""In-memory world state, tick stepping, and diff/broadcast plumbing.

This is a minimal placeholder for #10 (server skeleton): it tracks only
the world clock and a peer position table so the WebSocket wire protocol
has real data to snapshot and diff. Full simulation — movement, speech,
persisted events — is owned by #13 (`world/sim.py`), which will replace
`WorldState` with the real thing while keeping this wire-shape contract.

Per `docs/design/architecture.md` §6, `tick_state()` is a pure function of
state (no asyncio timing), so it is fully unit-testable; the async tick
task in `server/app.py` is just a thin timer driving it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PeerPosition:
    """A peer's map coordinates."""

    pos_x: int
    pos_y: int


@dataclass(slots=True)
class WorldState:
    """Mutable server-side world state shared by the tick loop and WS layer.

    `broadcast_seconds` and `broadcast_peers` record what was last sent to
    clients as a diff, so `tick_state()` can compute the delta. They start
    in sync with the initial `peers`/clock so the first tick does not
    re-report state already covered by the connection snapshot.

    `world_seconds` and `_ms_remainder` are accumulated as integer
    milliseconds rather than float seconds, so repeated small ticks (e.g.
    500ms) never drift from floating-point rounding error.
    """

    world_seconds: int = 0
    peers: dict[str, PeerPosition] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    broadcast_seconds: int = field(default=0)
    broadcast_peers: dict[str, PeerPosition] = field(default_factory=dict)
    ms_remainder: int = field(default=0)

    def __post_init__(self) -> None:
        """Seed broadcast bookkeeping from the initial peer set."""
        if not self.broadcast_peers and self.peers:
            self.broadcast_peers = dict(self.peers)


def snapshot(state: WorldState) -> dict[str, Any]:
    """Build the full-state snapshot sent as the first message on connect.

    Args:
        state: Current world state.

    Returns:
        A `{"t": "snapshot", ...}` payload with the full clock/peers/events.
    """
    return {
        "t": "snapshot",
        "clock": {"world_seconds": state.world_seconds},
        "peers": {
            peer_id: {"pos_x": pos.pos_x, "pos_y": pos.pos_y}
            for peer_id, pos in state.peers.items()
        },
        "events": list(state.events),
    }


def tick_state(state: WorldState, tick_ms: int) -> dict[str, Any] | None:
    """Advance *state* by one tick and compute a diff, if anything changed.

    Args:
        state: World state to mutate in place.
        tick_ms: Tick duration in milliseconds. Per requirements.md §4.1,
            1 real second = 1 world second, so `world_seconds` advances by
            one whole second every time accumulated ticks cross a 1000ms
            boundary.

    Returns:
        A `{"t": "diff", ...}` payload containing only the top-level keys
        that changed since the last diff, or `None` if nothing changed.
    """
    whole_seconds, state.ms_remainder = divmod(state.ms_remainder + tick_ms, 1000)
    state.world_seconds += whole_seconds
    current_seconds = state.world_seconds

    changed_peers = {
        peer_id: pos
        for peer_id, pos in state.peers.items()
        if state.broadcast_peers.get(peer_id) != pos
    }
    removed_peers = [
        peer_id for peer_id in state.broadcast_peers if peer_id not in state.peers
    ]
    clock_changed = current_seconds != state.broadcast_seconds

    if not changed_peers and not removed_peers and not clock_changed:
        return None

    diff: dict[str, Any] = {"t": "diff"}
    if clock_changed:
        diff["clock"] = {"world_seconds": current_seconds}
        state.broadcast_seconds = current_seconds
    if changed_peers or removed_peers:
        peers_diff: dict[str, Any] = {
            peer_id: {"pos_x": pos.pos_x, "pos_y": pos.pos_y}
            for peer_id, pos in changed_peers.items()
        }
        # A removed peer is broadcast as an explicit null so clients drop it.
        peers_diff.update(dict.fromkeys(removed_peers))
        diff["peers"] = peers_diff
        state.broadcast_peers = dict(state.peers)

    return diff


class Broadcaster:
    """Fans out diff/event messages to every connected WebSocket client."""

    def __init__(self) -> None:
        """Initialize with no subscribers."""
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a new subscriber queue.

        Returns:
            A fresh `asyncio.Queue` that will receive published messages.
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Deregister a subscriber queue.

        Args:
            queue: The queue previously returned by `subscribe()`.
        """
        self._subscribers.discard(queue)

    async def publish(self, message: dict[str, Any]) -> None:
        """Push *message* onto every currently subscribed queue.

        Args:
            message: The wire message to fan out.
        """
        for queue in list(self._subscribers):
            await queue.put(message)
