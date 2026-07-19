"""Tests for the Signal Tower BBS (#21)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from peerport.config import Config, WorldConfig
from peerport.db import insert_board_post, list_board_posts, open_db
from peerport.memory.stream import MemoryStream
from peerport.peers.decide import make_board_hooks
from peerport.server.app import create_app
from tests.test_converse import FakeBroadcaster
from tests.test_memory import FakeEmbedder

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).parent.parent
STATIC = REPO_ROOT / "src" / "peerport" / "server" / "static"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "board.db")
    yield connection
    connection.close()


class FakeDecisionEngine:
    def __init__(self) -> None:
        self.triggers: list[list[str] | None] = []

    async def trigger_redecision(self, peer_ids: list[str] | None = None) -> None:
        self.triggers.append(peer_ids)


class TestKeeperPosts:
    def test_keeper_post_inserts_and_triggers_redecision(
        self, conn: sqlite3.Connection
    ) -> None:
        engine = FakeDecisionEngine()
        app = create_app(Config(world=WorldConfig(tick_ms=60000)))
        with TestClient(app) as client:
            app.state.db_conn = conn
            app.state.decision_engine = engine
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()  # snapshot
                response = client.post(
                    "/api/board", json={"body": "No swimming past the breakwater."}
                )
                assert response.status_code == 200
                frame = ws.receive_json()

        posts = list_board_posts(conn)
        assert len(posts) == 1
        assert posts[0]["author_id"] == "keeper"
        assert posts[0]["body"] == "No swimming past the breakwater."
        assert engine.triggers == [None]  # broadcast to all active peers
        assert frame["t"] == "event"
        assert frame["kind"] == "board_post"

    def test_whitespace_post_rejected_no_side_effects(
        self, conn: sqlite3.Connection
    ) -> None:
        engine = FakeDecisionEngine()
        app = create_app(Config(world=WorldConfig(tick_ms=60000)))
        with TestClient(app) as client:
            app.state.db_conn = conn
            app.state.decision_engine = engine
            assert client.post("/api/board", json={"body": "   "}).status_code == 422
        assert list_board_posts(conn) == []
        assert engine.triggers == []

    def test_get_board_lists_newest_first(self, conn: sqlite3.Connection) -> None:
        for i in range(3):
            insert_board_post(
                conn, author_id="keeper", body=f"notice {i}", ts_world=i, created_at=i
            )
        app = create_app(Config(world=WorldConfig(tick_ms=60000)))
        with TestClient(app) as client:
            app.state.db_conn = conn
            data = client.get("/api/board").json()
        assert [p["body"] for p in data["posts"]] == [
            "notice 2",
            "notice 1",
            "notice 0",
        ]


class TestPeerBoardActions:
    @pytest.mark.anyio
    async def test_peer_post_board_persists_and_broadcasts(
        self, conn: sqlite3.Connection
    ) -> None:
        broadcaster = FakeBroadcaster()
        memory = MemoryStream(conn, FakeEmbedder())
        post_hook, _read_hook = make_board_hooks(
            conn, memory, broadcaster, now_world=lambda: 500
        )
        await post_hook("beacon", "Storm's coming, dock your boats early.")

        posts = list_board_posts(conn)
        assert posts[0]["author_id"] == "beacon"
        assert posts[0]["ts_world"] == 500
        assert any(
            f["t"] == "event" and f["kind"] == "board_post" for f in broadcaster.frames
        )

    @pytest.mark.anyio
    async def test_read_board_summarizes_latest_five_into_memory(
        self, conn: sqlite3.Connection
    ) -> None:
        for i in range(8):
            insert_board_post(
                conn, author_id="keeper", body=f"post-{i}", ts_world=i, created_at=i
            )
        broadcaster = FakeBroadcaster()
        memory = MemoryStream(conn, FakeEmbedder())
        _post_hook, read_hook = make_board_hooks(
            conn, memory, broadcaster, now_world=lambda: 900
        )
        await read_hook("tug")

        rows = conn.execute("SELECT kind, text FROM memories").fetchall()
        assert len(rows) == 1
        kind, text = rows[0]
        assert kind == "observation"
        for i in range(3, 8):
            assert f"post-{i}" in text
        for i in range(3):
            assert f"post-{i}," not in text  # oldest three not referenced


class TestBoardFrontend:
    def test_bridge_js_references_board_catalog_keys(self) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        for key in (
            "board.compose.placeholder",
            "board.post",
            "board.empty",
            "board.author.keeper",
        ):
            assert key in source, f"bridge.js missing board key {key}"

    def test_bridge_js_renders_popup_sections(self) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        for key in ("popup.ties", "popup.lately", "popup.kind."):
            assert key in source, f"bridge.js missing popup key {key}"
