"""Tests for `peerport.world.sim` (tick stepping, movement, drifter)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from peerport.config import Config, WorldConfig
from peerport.peers.personas import load_personas
from peerport.server.app import create_app
from peerport.server.state import PeerPosition
from peerport.world.clock import WorldClock
from peerport.world.sim import Simulation
from peerport.world.worldmap import WorldMap

if TYPE_CHECKING:
    import random
    from collections.abc import Callable

REPO_ROOT = Path(__file__).parent.parent
TICK_MS = 500


@pytest.fixture(scope="module")
def worldmap() -> WorldMap:
    return WorldMap.load(REPO_ROOT / "data" / "map" / "port.json")


def build_sim(
    worldmap: WorldMap,
    rng: random.Random,
    clock: WorldClock | None = None,
    initial_world_seconds: int = 0,
) -> Simulation:
    personas = load_personas(REPO_ROOT / "personas")
    return Simulation(
        worldmap=worldmap,
        personas=personas,
        rng=rng,
        clock=clock or WorldClock(),
        initial_world_seconds=initial_world_seconds,
    )


@pytest.fixture
def sim(worldmap: WorldMap, make_rng: Callable[[int], random.Random]) -> Simulation:
    return build_sim(worldmap, make_rng(42))


class TestInitialPlacement:
    def test_map_peers_start_at_their_berths(self, sim: Simulation) -> None:
        assert sim.peers["beacon"].tile == (5, 10)
        assert sim.peers["tug"].tile == (26, 20)
        assert sim.peers["bell"].tile == (32, 10)

    def test_friends_and_drifter_not_on_map_initially(self, sim: Simulation) -> None:
        assert set(sim.peers) == {"beacon", "tug", "bell"}
        assert set(sim.state.peers) == {"beacon", "tug", "bell"}


class TestMovement:
    def test_assigned_path_advances_one_tile_per_tick(self, sim: Simulation) -> None:
        sim.assign_destination("tug", "pier_main")
        path = list(sim.peers["tug"].path)
        for expected_tile in path[1:4]:
            sim.tick(TICK_MS)
            assert sim.peers["tug"].tile == expected_tile

    def test_state_positions_track_sim_tiles(self, sim: Simulation) -> None:
        sim.assign_destination("bell", "dock_square")
        sim.tick(TICK_MS)
        col, row = sim.peers["bell"].tile
        assert sim.state.peers["bell"] == PeerPosition(pos_x=col, pos_y=row)

    def test_arrival_reaches_destination_anchor(self, sim: Simulation) -> None:
        sim.assign_destination("tug", "breakwater")
        for _ in range(len(sim.peers["tug"].path) - 1):
            sim.tick(TICK_MS)
        assert sim.peers["tug"].tile == (33, 23)
        assert sim.peers["tug"].needs_decision


class TestBlockedPath:
    def test_blocked_peer_stays_and_flags_re_decision(self, sim: Simulation) -> None:
        sim.assign_destination("bell", "dock_square")
        next_tile = sim.peers["bell"].path[1]
        sim.peers["tug"].tile = next_tile
        sim.peers["tug"].path = []
        sim.peers["tug"].needs_decision = False
        before = sim.peers["bell"].tile

        sim.tick(TICK_MS)

        assert sim.peers["bell"].tile == before
        assert sim.peers["bell"].needs_decision

    def test_blocked_peer_re_decides_a_different_destination(
        self, sim: Simulation
    ) -> None:
        sim.assign_destination("bell", "dock_square")
        next_tile = sim.peers["bell"].path[1]
        sim.peers["tug"].tile = next_tile
        sim.peers["tug"].path = []
        sim.peers["tug"].needs_decision = False

        sim.tick(TICK_MS)  # blocked
        sim.tick(TICK_MS)  # re-decision at next decision point

        assert sim.peers["bell"].destination is not None
        assert sim.peers["bell"].destination != "dock_square"


class TestRandomWander:
    def test_peers_pick_destinations_and_move_on_their_own(
        self, sim: Simulation
    ) -> None:
        start = {peer_id: peer.tile for peer_id, peer in sim.peers.items()}
        for _ in range(10):
            sim.tick(TICK_MS)
        moved = [
            peer_id
            for peer_id, peer in sim.peers.items()
            if peer.tile != start[peer_id]
        ]
        assert moved, "no peer moved in 10 ticks of autonomous wandering"


class TestDrifter:
    def test_spawn_places_echo_at_mist_gate(self, sim: Simulation) -> None:
        sim.spawn_drifter()
        assert sim.peers["echo"].tile == (37, 23)
        assert sim.state.peers["echo"] == PeerPosition(pos_x=37, pos_y=23)

    def test_despawn_removes_echo_from_state(self, sim: Simulation) -> None:
        sim.spawn_drifter()
        sim.despawn_drifter()
        assert "echo" not in sim.peers
        assert "echo" not in sim.state.peers

    def test_despawn_is_broadcast_as_removal(self, sim: Simulation) -> None:
        sim.spawn_drifter()
        sim.tick(TICK_MS)
        sim.despawn_drifter()
        frames = sim.tick(TICK_MS)
        peer_frames = [f for f in frames if "peers" in f]
        assert any(f["peers"].get("echo", "kept") is None for f in peer_frames)


class TestClockIntegration:
    def test_world_seconds_advance_one_per_real_second(self, sim: Simulation) -> None:
        for _ in range(4):
            sim.tick(TICK_MS)
        assert sim.state.world_seconds == 2

    def test_initial_world_seconds_resume_from_persisted_value(
        self, worldmap: WorldMap, make_rng: Callable[[int], random.Random]
    ) -> None:
        sim = build_sim(worldmap, make_rng(1), initial_world_seconds=7200)
        assert sim.state.world_seconds == 7200
        assert sim.clock.day(sim.state.world_seconds) == 2

    def test_clock_frame_emitted_on_band_change(
        self, worldmap: WorldMap, make_rng: Callable[[int], random.Random]
    ) -> None:
        sim = build_sim(
            worldmap, make_rng(1), clock=WorldClock(day_length_real_minutes=1)
        )
        bands_seen: list[str] = []
        for _ in range(4 * 15 * 2):
            bands_seen.extend(
                frame["band"] for frame in sim.tick(TICK_MS) if frame["t"] == "clock"
            )
        assert bands_seen == ["day", "dusk", "night", "morning"]


class TestPauseAndSpeed:
    def test_pause_freezes_clock_and_movement(self, sim: Simulation) -> None:
        sim.assign_destination("tug", "pier_main")
        sim.paused = True
        before = sim.peers["tug"].tile
        frames = sim.tick(TICK_MS)
        assert frames == []
        assert sim.peers["tug"].tile == before
        assert sim.state.world_seconds == 0

    def test_resume_continues_from_paused_state(self, sim: Simulation) -> None:
        sim.paused = True
        sim.tick(TICK_MS)
        sim.paused = False
        sim.tick(TICK_MS)
        sim.tick(TICK_MS)
        assert sim.state.world_seconds == 1

    def test_double_speed_moves_two_tiles_and_two_world_seconds(
        self, sim: Simulation
    ) -> None:
        sim.assign_destination("tug", "pier_main")
        path = list(sim.peers["tug"].path)
        sim.speed = 2
        sim.tick(TICK_MS)
        assert sim.peers["tug"].tile == path[2]
        assert sim.state.world_seconds == 1


class TestNoLlmInWorld:
    def test_world_modules_import_no_llm_code(self) -> None:
        world_dir = REPO_ROOT / "src" / "peerport" / "world"
        for module in world_dir.glob("*.py"):
            for line in module.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    assert "llm" not in stripped.lower(), (
                        f"LLM import in {module.name}: {stripped}"
                    )


class TestServerIntegration:
    def test_app_with_simulation_streams_pos_diffs(
        self, worldmap: WorldMap, make_rng: Callable[[int], random.Random]
    ) -> None:
        sim = build_sim(worldmap, make_rng(7))
        app = create_app(Config(world=WorldConfig(tick_ms=5)), simulation=sim)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            snap = ws.receive_json()
            assert snap["t"] == "snapshot"
            assert set(snap["peers"]) == {"beacon", "tug", "bell"}
            clock_frame = ws.receive_json()
            assert clock_frame["t"] == "clock"
            assert clock_frame["band"] in ("morning", "day", "dusk", "night")
            for _ in range(20):
                frame = ws.receive_json()
                if frame.get("peers"):
                    break
            else:
                pytest.fail("no pos diff arrived over 20 frames")


class TestWorldControlApi:
    def test_pause_resume_and_speed_via_rest(
        self, worldmap: WorldMap, make_rng: Callable[[int], random.Random]
    ) -> None:
        sim = build_sim(worldmap, make_rng(3))
        app = create_app(Config(world=WorldConfig(tick_ms=5)), simulation=sim)
        with TestClient(app) as client:
            assert client.post("/api/world", json={"action": "pause"}).json() == {
                "ok": True,
                "paused": True,
                "speed": 1,
            }
            assert sim.paused
            client.post("/api/world", json={"action": "speed", "speed": 2})
            assert sim.speed == 2
            client.post("/api/world", json={"action": "resume"})
            assert not sim.paused
            assert (
                client.post("/api/world", json={"action": "warp"}).status_code == 422
            )
            assert (
                client.post(
                    "/api/world", json={"action": "speed", "speed": 5}
                ).status_code
                == 422
            )
