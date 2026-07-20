"""Tests for peerport.server.ws (WebSocket wire protocol)."""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from peerport.config import Config, WorldConfig
from peerport.db import UsageRecord, insert_usage, open_db
from peerport.llm.budget import BudgetGuard
from peerport.peers.personas import load_personas
from peerport.server.app import create_app
from peerport.server.state import PeerPosition
from peerport.world.clock import WorldClock
from peerport.world.sim import Simulation
from peerport.world.worldmap import WorldMap

if TYPE_CHECKING:
    import sqlite3

FAST_TICK_CONFIG = Config(world=WorldConfig(tick_ms=5))
REPO_ROOT = Path(__file__).parent.parent


def build_sim() -> Simulation:
    """A real `Simulation` over the repo's map/personas for snapshot tests."""
    worldmap = WorldMap.load(REPO_ROOT / "data" / "map" / "port.json")
    personas = load_personas(REPO_ROOT / "personas")
    return Simulation(
        worldmap=worldmap,
        personas=personas,
        rng=random.Random(1),  # noqa: S311 -- deterministic test seed, not security
        clock=WorldClock(),
    )


def build_conn(tmp_path: Path) -> sqlite3.Connection:
    """A fresh on-disk DB for `BudgetGuard`-backed snapshot tests."""
    return open_db(tmp_path / "snapshot.db")


class TestSnapshotOnConnect:
    def test_first_message_is_snapshot(self) -> None:
        app = create_app(FAST_TICK_CONFIG)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            message = ws.receive_json()

        assert message["t"] == "snapshot"
        assert "clock" in message
        assert "peers" in message
        assert "events" in message

    def test_snapshot_includes_all_currently_known_peers(self) -> None:
        app = create_app(FAST_TICK_CONFIG)
        with TestClient(app) as client:
            app.state.world_state.peers["beacon"] = PeerPosition(pos_x=1, pos_y=2)
            with client.websocket_connect("/ws") as ws:
                message = ws.receive_json()

        assert message["peers"] == {"beacon": {"pos_x": 1, "pos_y": 2}}

    def test_reconnect_always_yields_a_fresh_snapshot(self) -> None:
        app = create_app(FAST_TICK_CONFIG)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws1:
                first = ws1.receive_json()
            with client.websocket_connect("/ws") as ws2:
                second = ws2.receive_json()

        assert first["t"] == "snapshot"
        assert second["t"] == "snapshot"


class TestDiffStream:
    def test_server_side_position_mutation_produces_a_diff(self) -> None:
        app = create_app(FAST_TICK_CONFIG)
        with TestClient(app) as client:
            app.state.world_state.peers["beacon"] = PeerPosition(pos_x=0, pos_y=0)
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()  # snapshot
                app.state.world_state.peers["beacon"] = PeerPosition(pos_x=3, pos_y=4)
                diff = ws.receive_json()

        assert diff["t"] == "diff"
        assert diff["peers"]["beacon"] == {"pos_x": 3, "pos_y": 4}


class TestMalformedMessages:
    def test_malformed_json_is_ignored_and_connection_stays_open(self) -> None:
        app = create_app(FAST_TICK_CONFIG)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # snapshot
            ws.send_text("not valid json{{{")

            # Connection must still be usable afterward: a fresh mutation
            # still produces a diff instead of the socket having closed.
            app.state.world_state.peers["beacon"] = PeerPosition(pos_x=9, pos_y=9)
            diff = ws.receive_json()

        assert diff["t"] == "diff"
        assert diff["peers"]["beacon"] == {"pos_x": 9, "pos_y": 9}


class TestZeroClientSimulation:
    def test_world_clock_advances_with_no_clients_connected(self) -> None:
        app = create_app(Config(world=WorldConfig(tick_ms=5)))
        with TestClient(app):
            deadline = time.monotonic() + 2.0
            while (
                app.state.world_state.world_seconds < 1 and time.monotonic() < deadline
            ):
                time.sleep(0.01)

            assert app.state.world_state.world_seconds >= 1

            with TestClient(app).websocket_connect("/ws") as _ws:
                pass  # connecting does not reset an already-running world

        assert app.state.world_state.world_seconds >= 1


class TestSnapshotDegradedStateResync:
    """Snapshot resync of fog/hard-stop/low-power on every (re)connect (finding).

    Live `state` frames only ever reach clients connected *while* a
    transition fires; a Keeper who reconnects during an outage or after a
    hard-cap trip previously had no way to learn the current status. The
    snapshot (the guaranteed first message on every connect, per
    `net.js`'s header comment) now carries it too.
    """

    def test_snapshot_includes_active_fog_state_from_simulation(self) -> None:
        sim = build_sim()
        sim.fog_active = True
        sim.fog_status = 503
        app = create_app(FAST_TICK_CONFIG, simulation=sim)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            message = ws.receive_json()

        assert message["fog"] == {"active": True, "status": 503}

    def test_snapshot_reports_inactive_fog_by_default(self) -> None:
        app = create_app(FAST_TICK_CONFIG, simulation=build_sim())
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            message = ws.receive_json()

        assert message["fog"] == {"active": False, "status": None}

    def test_snapshot_includes_hard_stop_state_from_simulation(self) -> None:
        sim = build_sim()
        sim.hard_stop = True
        app = create_app(FAST_TICK_CONFIG, simulation=sim)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            message = ws.receive_json()

        assert message["hard_stop"] is True

    def test_reconnect_picks_up_a_fog_state_that_changed_meanwhile(self) -> None:
        sim = build_sim()
        app = create_app(FAST_TICK_CONFIG, simulation=sim)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as ws1:
                first = ws1.receive_json()
            sim.fog_active = True
            sim.fog_status = 500
            with client.websocket_connect("/ws") as ws2:
                second = ws2.receive_json()

        assert first["fog"]["active"] is False
        assert second["fog"] == {"active": True, "status": 500}

    def test_snapshot_omits_degraded_keys_without_a_simulation(self) -> None:
        """Backward compatible with the pre-#13 bare-`WorldState` skeleton."""
        app = create_app(FAST_TICK_CONFIG)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            message = ws.receive_json()

        assert "fog" not in message
        assert "hard_stop" not in message
        assert "low_power" not in message

    def test_snapshot_includes_low_power_from_a_wired_budget_guard(
        self, tmp_path: Path
    ) -> None:
        conn = build_conn(tmp_path)
        insert_usage(
            conn,
            UsageRecord(
                model="gpt-5-nano",
                role="background",
                purpose="test",
                input_tokens=0,
                cached_tokens=0,
                output_tokens=0,
                est_cost_usd=0.75,
                status="ok",
            ),
        )
        app = create_app(FAST_TICK_CONFIG, simulation=build_sim())
        app.state.budget_guard = BudgetGuard(conn, soft_cap_usd=0.50)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            message = ws.receive_json()

        conn.close()
        assert message["low_power"] is True

    def test_snapshot_reports_low_power_false_under_the_soft_cap(
        self, tmp_path: Path
    ) -> None:
        conn = build_conn(tmp_path)
        app = create_app(FAST_TICK_CONFIG, simulation=build_sim())
        app.state.budget_guard = BudgetGuard(conn, soft_cap_usd=0.50)
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            message = ws.receive_json()

        conn.close()
        assert message["low_power"] is False
