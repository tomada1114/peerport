"""Tests for peerport.server.ws (WebSocket wire protocol)."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient
from peerport.server.app import create_app

from peerport.config import Config, WorldConfig
from peerport.server.state import PeerPosition

FAST_TICK_CONFIG = Config(world=WorldConfig(tick_ms=5))


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
