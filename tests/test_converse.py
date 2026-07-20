"""Tests for `peerport.peers.converse` (#20: conversations, relationships, popup)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from peerport.config import Config, WorldConfig
from peerport.db import Relationship, get_relationship, open_db, save_relationship
from peerport.llm.budget import BudgetGuard
from peerport.llm.client import LLMClient, TransportReply
from peerport.memory.stream import MemoryStream
from peerport.peers.converse import PROXIMITY_TILES, ConversationEngine
from peerport.peers.personas import load_personas
from peerport.server.app import create_app
from peerport.world.clock import WorldClock
from peerport.world.sim import Simulation
from peerport.world.worldmap import WorldMap
from tests.test_llm_client import FakeTransport
from tests.test_memory import FakeEmbedder

if TYPE_CHECKING:
    import random
    import sqlite3
    from collections.abc import Callable, Iterator

REPO_ROOT = Path(__file__).parent.parent


def turn(text: str, wants_to_end: bool = False) -> TransportReply:
    return TransportReply(text=json.dumps({"text": text, "wants_to_end": wants_to_end}))


OUTCOME = TransportReply(
    text=json.dumps(
        {
            "summary": "Tug and Bell talked about the tide.",
            "delta": 3,
            "label": "tide buddies",
        }
    )
)


class FakeBroadcaster:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def publish(self, message: dict[str, Any]) -> None:
        self.frames.append(message)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "conv.db")
    yield connection
    connection.close()


def make_engine(
    conn: sqlite3.Connection,
    rng: random.Random,
    transport: FakeTransport,
) -> tuple[ConversationEngine, FakeBroadcaster]:
    async def no_sleep(_s: float) -> None:
        return

    personas = load_personas(REPO_ROOT / "personas")
    worldmap = WorldMap.load(REPO_ROOT / "data" / "map" / "port.json")
    sim = Simulation(worldmap=worldmap, personas=personas, rng=rng, clock=WorldClock())
    llm = LLMClient(
        config=Config(),
        conn=conn,
        budget=BudgetGuard(conn),
        transport=transport,
        sleep=no_sleep,
    )
    broadcaster = FakeBroadcaster()
    engine = ConversationEngine(
        llm=llm,
        sim=sim,
        memory=MemoryStream(conn, FakeEmbedder()),
        broadcaster=broadcaster,
        conn=conn,
        personas=personas,
    )
    return engine, broadcaster


class TestEligibility:
    @pytest.mark.parametrize(
        ("distance", "eligible"),
        [pytest.param(1, True), pytest.param(2, True), pytest.param(3, False)],
    )
    def test_proximity_threshold_inclusive(
        self,
        conn: sqlite3.Connection,
        make_rng: Callable[[int], random.Random],
        distance: int,
        eligible: bool,
    ) -> None:
        engine, _ = make_engine(conn, make_rng(1), FakeTransport())
        engine.sim.peers["tug"].tile = (10, 12)
        engine.sim.peers["bell"].tile = (10 + distance, 12)
        assert engine.eligible("tug", "bell") is eligible
        assert PROXIMITY_TILES == 2

    @pytest.mark.anyio
    async def test_busy_target_rejected_with_zero_side_effects(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        transport = FakeTransport()
        engine, broadcaster = make_engine(conn, make_rng(2), transport)
        engine.sim.peers["tug"].tile = (10, 12)
        engine.sim.peers["bell"].tile = (11, 12)
        engine.busy.add("bell")
        started = await engine.start("tug", "bell")
        assert started is False
        assert transport.calls == []
        assert broadcaster.frames == []


class TestConversationFlow:
    @pytest.mark.anyio
    async def test_six_turn_cap_forces_termination(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        replies: list[object] = [turn(f"line {i}") for i in range(6)]
        replies.append(OUTCOME)
        transport = FakeTransport(replies)
        engine, broadcaster = make_engine(conn, make_rng(3), transport)
        engine.sim.peers["tug"].tile = (10, 12)
        engine.sim.peers["bell"].tile = (11, 12)
        await engine.start("tug", "bell")

        speech = [f for f in broadcaster.frames if f["t"] == "speech"]
        assert len(speech) == 6
        assert [f["peer_id"] for f in speech] == ["tug", "bell"] * 3
        assert len(transport.calls) == 7  # 6 turns + 1 outcome

    @pytest.mark.anyio
    async def test_both_wants_to_end_ends_early(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        transport = FakeTransport(
            [
                turn("a1"),
                turn("b2"),
                turn("a3", wants_to_end=True),
                turn("b4", wants_to_end=True),
                OUTCOME,
            ]
        )
        engine, broadcaster = make_engine(conn, make_rng(4), transport)
        engine.sim.peers["tug"].tile = (10, 12)
        engine.sim.peers["bell"].tile = (11, 12)
        await engine.start("tug", "bell")

        speech = [f for f in broadcaster.frames if f["t"] == "speech"]
        assert len(speech) == 4
        assert len(transport.calls) == 5  # 4 turns + outcome, turn 5 never runs

    @pytest.mark.anyio
    async def test_full_text_in_events_only_summary_in_both_memories(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        transport = FakeTransport(
            [turn("secret line one"), turn("b", True), turn("a", True), OUTCOME]
        )
        engine, _ = make_engine(conn, make_rng(5), transport)
        engine.sim.peers["tug"].tile = (10, 12)
        engine.sim.peers["bell"].tile = (11, 12)
        await engine.start("tug", "bell")

        payload = conn.execute(
            "SELECT payload FROM events WHERE type = 'conversation'"
        ).fetchone()[0]
        assert "secret line one" in payload
        memories = conn.execute(
            "SELECT peer_id, kind, text FROM memories ORDER BY peer_id"
        ).fetchall()
        assert [(m[0], m[1]) for m in memories] == [
            ("bell", "conversation"),
            ("tug", "conversation"),
        ]
        assert all("secret line one" not in m[2] for m in memories)
        assert all(m[2] == "Tug and Bell talked about the tide." for m in memories)

    @pytest.mark.anyio
    async def test_relationship_updated_and_clamped(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        save_relationship(
            conn, ("tug", "bell"), Relationship(score=98, label="old", last_delta=0)
        )
        transport = FakeTransport([turn("a", True), turn("b", True), OUTCOME])
        engine, _ = make_engine(conn, make_rng(6), transport)
        engine.sim.peers["tug"].tile = (10, 12)
        engine.sim.peers["bell"].tile = (11, 12)
        await engine.start("tug", "bell")

        rel = get_relationship(conn, "tug", "bell")
        assert rel.score == 100  # 98 + 3 clamped
        assert rel.label == "tide buddies"
        assert rel.last_delta == 3

    @pytest.mark.anyio
    async def test_on_peer_event_called_for_both_participants(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        transport = FakeTransport([turn("a", True), turn("b", True), OUTCOME])
        engine, _ = make_engine(conn, make_rng(6), transport)
        engine.sim.peers["tug"].tile = (10, 12)
        engine.sim.peers["bell"].tile = (11, 12)
        notified: list[str] = []

        async def on_peer_event(peer_id: str) -> None:
            notified.append(peer_id)

        engine.on_peer_event = on_peer_event
        await engine.start("tug", "bell")

        assert set(notified) == {"tug", "bell"}

    @pytest.mark.anyio
    async def test_next_conversation_prompt_includes_score_and_label(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        save_relationship(
            conn,
            ("tug", "bell"),
            Relationship(score=45, label="arm-wrestle rivals", last_delta=3),
        )
        transport = FakeTransport([turn("a", True), turn("b", True), OUTCOME])
        engine, _ = make_engine(conn, make_rng(7), transport)
        engine.sim.peers["tug"].tile = (10, 12)
        engine.sim.peers["bell"].tile = (11, 12)
        await engine.start("tug", "bell")
        prompt = transport.calls[0]["prompt"]
        assert isinstance(prompt, str)
        assert "arm-wrestle rivals" in prompt
        assert "45" in prompt


class TestLocale:
    """Finding: conversation turns/outcomes always passed locale "en"."""

    @pytest.mark.anyio
    async def test_configured_locale_reaches_turn_and_outcome_prompts(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        transport = FakeTransport([turn("a", True), turn("b", True), OUTCOME])
        engine, _ = make_engine(conn, make_rng(9), transport)
        engine.locale = "ja"
        engine.sim.peers["tug"].tile = (10, 12)
        engine.sim.peers["bell"].tile = (11, 12)

        await engine.start("tug", "bell")

        turn_prompt = transport.calls[0]["prompt"]
        outcome_prompt = transport.calls[-1]["prompt"]
        assert isinstance(turn_prompt, str)
        assert isinstance(outcome_prompt, str)
        assert "Locale: ja" in turn_prompt
        assert "Locale: ja" in outcome_prompt


class TestPeerPopupApi:
    def test_api_peer_returns_ties_and_lately(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        engine, _ = make_engine(conn, make_rng(8), FakeTransport())
        save_relationship(
            conn,
            ("tug", "bell"),
            Relationship(score=45, label="tide buddies", last_delta=3),
        )
        save_relationship(
            conn,
            ("tug", "beacon"),
            Relationship(score=10, label="new faces", last_delta=-2),
        )
        for i in range(5):
            conn.execute(
                "INSERT INTO memories (peer_id, ts_world, ts_real, kind, text,"
                " importance) VALUES ('tug', ?, ?, 'observation', ?, 5)",
                (i, i, f"memory {i}"),
            )
        conn.commit()

        app = create_app(Config(world=WorldConfig(tick_ms=60000)))
        with TestClient(app) as client:
            app.state.db_conn = conn
            app.state.personas = engine.personas
            response = client.get("/api/peer/tug")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Tug"
        assert data["kind"] == "peer"
        ties = {t["peer"]: t for t in data["ties"]}
        assert ties["bell"]["label"] == "tide buddies"
        assert ties["bell"]["trend"] == "up"
        assert ties["beacon"]["trend"] == "down"
        assert all("score" not in t for t in data["ties"])
        assert data["lately"] == ["memory 4", "memory 3", "memory 2"]

    def test_api_peer_unknown_id_404(self, conn: sqlite3.Connection) -> None:
        app = create_app(Config(world=WorldConfig(tick_ms=60000)))
        with TestClient(app) as client:
            app.state.db_conn = conn
            app.state.personas = load_personas(REPO_ROOT / "personas")
            assert client.get("/api/peer/nobody").status_code == 404
