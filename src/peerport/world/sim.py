"""Deterministic world simulation: movement stepping and wander decisions.

`Simulation.tick()` is a pure function of state driven by a thin async
timer in `server/app.py` (architecture.md §6). It never performs I/O and
never calls an LLM; destination choices use a random-wander placeholder
until the Option-Action decision loop (#19) takes over. All randomness
flows from the single injected `random.Random`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from peerport.server.state import PeerPosition, WorldState, tick_state
from peerport.world.clock import WorldClock

if TYPE_CHECKING:
    import random
    from collections.abc import Mapping

    from peerport.peers.personas import Persona
    from peerport.world.worldmap import Tile, WorldMap

MAP_KINDS = frozenset({"mate", "peer"})
DRIFTER_KIND = "drifter"
DRIFTER_HOME_NODE = "mist_gate"
HOME_NODE_PREFIX = "berth_"
FALLBACK_NODE = "dock_square"


@dataclass(slots=True)
class SimPeer:
    """Mutable per-peer movement state inside the simulation."""

    id: str
    kind: str
    tile: Tile
    destination: str | None = None
    path: list[Tile] = field(default_factory=list)
    path_index: int = 0
    needs_decision: bool = True
    excluded_destination: str | None = None


class Simulation:
    """Owns peer movement, wander decisions, and the world clock feed."""

    def __init__(
        self,
        worldmap: WorldMap,
        personas: Mapping[str, Persona],
        rng: random.Random,
        clock: WorldClock | None = None,
        initial_world_seconds: int = 0,
    ) -> None:
        """Place map peers at their home berths and prime the clock.

        Args:
            worldmap: The loaded port map.
            personas: Persona registry keyed by id; only `mate`/`peer`
                kinds are placed on the map (the drifter spawns later,
                friends never appear).
            rng: The single seeded randomness source (architecture.md §6).
            clock: World clock; defaults to the 120-minute day.
            initial_world_seconds: Persisted clock value to resume from.
        """
        self.worldmap = worldmap
        self.rng = rng
        self.clock = clock or WorldClock()
        self.state = WorldState()
        self.state.world_seconds = initial_world_seconds
        self.state.broadcast_seconds = initial_world_seconds
        self.paused = False
        self.speed = 1
        # Degraded-state flags (#27/finding): mirrored here (rather than
        # only living transiently inside the fire-and-forget broadcast
        # callbacks in `__main__.py`) so a reconnecting client's snapshot
        # can resync the fog/hard-stop banners instead of only ever
        # learning about them from a live `state` frame it may have
        # missed while disconnected.
        self.hard_stop = False
        self.fog_active = False
        self.fog_status: int | None = None
        self.peers: dict[str, SimPeer] = {}
        self._drifter_id = next(
            (p.id for p in personas.values() if p.kind == DRIFTER_KIND), None
        )
        for persona in personas.values():
            if persona.kind not in MAP_KINDS:
                continue
            home = HOME_NODE_PREFIX + persona.id
            node = home if home in worldmap.nodes else FALLBACK_NODE
            tile = worldmap.standable_tile(node, persona.kind)
            peer = SimPeer(id=persona.id, kind=persona.kind, tile=tile)
            self.peers[persona.id] = peer
            self._sync_state(peer)
        self.state.broadcast_peers = dict(self.state.peers)
        self._last_band = self.clock.band(self.state.world_seconds)
        self._last_day = self.clock.day(self.state.world_seconds)

    def tick(self, tick_ms: int) -> list[dict[str, Any]]:
        """Advance one tick: decisions, movement, clock; return wire frames.

        At speed 2 each tick performs two movement steps and advances the
        clock twice as fast. Returns an empty list while paused.
        """
        if self.paused:
            return []
        for _ in range(self.speed):
            self._step_movement()
        frames: list[dict[str, Any]] = []
        diff = tick_state(self.state, tick_ms * self.speed)
        if diff is not None:
            frames.append(diff)
        band = self.clock.band(self.state.world_seconds)
        day = self.clock.day(self.state.world_seconds)
        if band != self._last_band or day != self._last_day:
            self._last_band = band
            self._last_day = day
            frames.append(self.clock_frame())
        return frames

    def clock_frame(self) -> dict[str, Any]:
        """Build the `{"t": "clock"}` wire frame for the current time."""
        return {
            "t": "clock",
            "band": self.clock.band(self.state.world_seconds),
            "day": self.clock.day(self.state.world_seconds),
            "day_length_real_minutes": self.clock.day_length_real_minutes,
        }

    def assign_destination(self, peer_id: str, node: str) -> None:
        """Send a peer toward a waypoint node (used by tests and #19).

        A peer with no reachable path, or already standing on *node*'s
        tile (a length-1 path), stays put and is flagged for
        re-decision -- mirroring `_decide()`'s own exclusion of
        same-tile candidates, so a peer routed to where it already is
        doesn't get stuck with `needs_decision` frozen `False` forever
        (per requirements.md §4.1's blocked-path edge case).
        """
        peer = self.peers[peer_id]
        path = self.worldmap.path_to_node(peer.tile, node, peer.kind)
        if path is None or len(path) <= 1:
            peer.needs_decision = True
            return
        peer.destination = node
        peer.path = path
        peer.path_index = 1
        peer.needs_decision = False

    def spawn_drifter(self) -> None:
        """Make the drifter appear at its mist-gate spawn point."""
        if self._drifter_id is None or self._drifter_id in self.peers:
            return
        tile = self.worldmap.standable_tile(DRIFTER_HOME_NODE, DRIFTER_KIND)
        peer = SimPeer(id=self._drifter_id, kind=DRIFTER_KIND, tile=tile)
        self.peers[self._drifter_id] = peer
        self._sync_state(peer)

    def despawn_drifter(self) -> None:
        """Remove the drifter from the world; broadcast as a peer removal."""
        if self._drifter_id is None or self._drifter_id not in self.peers:
            return
        del self.peers[self._drifter_id]
        self.state.peers.pop(self._drifter_id, None)

    def _step_movement(self) -> None:
        for peer in self.peers.values():
            if peer.needs_decision:
                self._decide(peer)
        for peer in self.peers.values():
            self._step_peer(peer)

    def _decide(self, peer: SimPeer) -> None:
        excluded = {peer.destination, peer.excluded_destination}
        candidates = [n for n in sorted(self.worldmap.nodes) if n not in excluded]
        self.rng.shuffle(candidates)
        for node in candidates:
            path = self.worldmap.path_to_node(peer.tile, node, peer.kind)
            if path is not None and len(path) > 1:
                peer.destination = node
                peer.path = path
                peer.path_index = 1
                peer.needs_decision = False
                peer.excluded_destination = None
                return

    def _step_peer(self, peer: SimPeer) -> None:
        if not peer.path or peer.path_index >= len(peer.path):
            return
        next_tile = peer.path[peer.path_index]
        if self._occupied(next_tile, by_other_than=peer):
            peer.needs_decision = True
            peer.excluded_destination = peer.destination
            peer.path = []
            peer.path_index = 0
            return
        peer.tile = next_tile
        peer.path_index += 1
        if peer.path_index >= len(peer.path):
            peer.needs_decision = True
        self._sync_state(peer)

    def _occupied(self, tile: Tile, by_other_than: SimPeer) -> bool:
        return any(
            other.tile == tile
            for other in self.peers.values()
            if other is not by_other_than
        )

    def _sync_state(self, peer: SimPeer) -> None:
        self.state.peers[peer.id] = PeerPosition(pos_x=peer.tile[0], pos_y=peer.tile[1])
