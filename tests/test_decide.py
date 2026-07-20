"""Tests for `peerport.peers.decide` (Option-Action loop, #19)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from peerport.config import Config
from peerport.db import open_db
from peerport.llm.budget import BudgetGuard
from peerport.llm.client import LLMClient, TransportReply
from peerport.llm.prompts import ActionDecision
from peerport.peers.decide import (
    ACTIONS,
    HISTORY_WINDOW,
    JITTER_MAX,
    JITTER_MIN,
    DecisionEngine,
    action_schema_excluding,
)
from peerport.peers.personas import load_personas
from peerport.world.clock import WorldClock
from peerport.world.sim import Simulation
from peerport.world.worldmap import WorldMap
from tests.test_llm_client import FakeTransport

if TYPE_CHECKING:
    import random
    import sqlite3
    from collections.abc import Callable, Iterator

REPO_ROOT = Path(__file__).parent.parent

DECIDE_REPLY = (
    '{"action": "move", "target": "pier_main", "content": null, "mood": "curious"}'
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "decide.db")
    yield connection
    connection.close()


def make_engine(
    conn: sqlite3.Connection,
    rng: random.Random,
    transport: FakeTransport,
) -> DecisionEngine:
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
    return DecisionEngine(llm=llm, sim=sim, personas=personas, rng=rng)


class TestIntervals:
    def test_base_interval_read_from_persona_frontmatter(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        engine = make_engine(conn, make_rng(1), FakeTransport())
        for peer_id, base in (
            ("beacon", 75),
            ("tug", 90),
            ("bell", 100),
            ("echo", 120),
        ):
            for _ in range(200):
                interval = engine.next_interval(peer_id)
                assert base * JITTER_MIN <= interval <= base * JITTER_MAX

    def test_jitter_band_is_plus_minus_20_percent(self) -> None:
        assert (JITTER_MIN, JITTER_MAX) == (0.8, 1.2)

    def test_low_power_doubles_the_activity_interval(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        engine = make_engine(conn, make_rng(1), FakeTransport())
        # A soft cap of 0.0 means today's ($0.00) spend already meets it,
        # so the guard is permanently in low-power mode for this test.
        engine.llm.budget.soft_cap_usd = 0.0
        base = engine.personas["tug"].activity_interval or 90
        for _ in range(200):
            interval = engine.next_interval("tug")
            assert base * 2 * JITTER_MIN <= interval <= base * 2 * JITTER_MAX


class TestSchema:
    def test_full_schema_has_six_actions_and_no_reasoning(self) -> None:
        schema = action_schema_excluding(None).model_json_schema()
        assert tuple(schema["properties"]["action"]["enum"]) == ACTIONS
        assert "reasoning" not in str(schema).lower()
        assert "rationale" not in str(schema).lower()

    def test_excluding_removes_one_action(self) -> None:
        schema = action_schema_excluding("rest").model_json_schema()
        assert "rest" not in schema["properties"]["action"]["enum"]
        assert len(schema["properties"]["action"]["enum"]) == 5


class TestDecide:
    @pytest.mark.anyio
    async def test_valid_decision_is_applied_to_the_sim(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        transport = FakeTransport([TransportReply(text=DECIDE_REPLY)])
        engine = make_engine(conn, make_rng(2), transport)
        decision = await engine.decide("tug")
        assert decision.action == "move"
        assert engine.sim.peers["tug"].destination == "pier_main"
        assert transport.calls[0]["max_output_tokens"] == 250

    @pytest.mark.anyio
    async def test_three_same_actions_excluded_server_side(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        transport = FakeTransport([TransportReply(text=DECIDE_REPLY)])
        engine = make_engine(conn, make_rng(3), transport)
        for _ in range(3):
            engine.record_action("bell", ActionDecision(action="rest", mood="calm"))
        await engine.decide("bell")
        sent = transport.calls[0]["schema"]
        assert isinstance(sent, dict)
        enum = sent["schema"]["properties"]["action"]["enum"]
        assert "rest" not in enum

    @pytest.mark.anyio
    async def test_two_same_actions_do_not_exclude(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        transport = FakeTransport([TransportReply(text=DECIDE_REPLY)])
        engine = make_engine(conn, make_rng(4), transport)
        for _ in range(2):
            engine.record_action("bell", ActionDecision(action="rest", mood="calm"))
        await engine.decide("bell")
        sent = transport.calls[0]["schema"]
        assert isinstance(sent, dict)
        assert "rest" in sent["schema"]["properties"]["action"]["enum"]

    @pytest.mark.anyio
    async def test_prompt_contains_last_five_actions_and_anti_repeat(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        transport = FakeTransport([TransportReply(text=DECIDE_REPLY)])
        engine = make_engine(conn, make_rng(5), transport)
        moods = ["m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8"]
        for mood in moods:
            engine.record_action("tug", ActionDecision(action="emote", mood=mood))
        await engine.decide("tug")
        prompt = transport.calls[0]["prompt"]
        assert isinstance(prompt, str)
        for mood in moods[-HISTORY_WINDOW:]:
            assert mood in prompt
        for mood in moods[:-HISTORY_WINDOW]:
            assert mood not in prompt
        assert "repeat" in prompt.lower()

    @pytest.mark.anyio
    async def test_hearsay_provider_text_appended_when_present(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        transport = FakeTransport([TransportReply(text=DECIDE_REPLY)])
        engine = make_engine(conn, make_rng(5), transport)
        engine.hearsay_provider = lambda peer_id: (
            "my Keeper said Kai aced the exam." if peer_id == "tug" else None
        )
        await engine.decide("tug")
        prompt = transport.calls[0]["prompt"]
        assert isinstance(prompt, str)
        assert "my Keeper said Kai aced the exam." in prompt

    @pytest.mark.anyio
    async def test_hearsay_provider_none_omits_nothing_extra(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        transport = FakeTransport([TransportReply(text=DECIDE_REPLY)])
        engine = make_engine(conn, make_rng(5), transport)
        engine.hearsay_provider = lambda _peer_id: None
        await engine.decide("tug")
        assert len(transport.calls) == 1

    @pytest.mark.anyio
    async def test_schema_violation_twice_falls_back_to_rest(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        transport = FakeTransport(
            [TransportReply(text="not json"), TransportReply(text="{}")]
        )
        engine = make_engine(conn, make_rng(6), transport)
        decision = await engine.decide("beacon")
        assert len(transport.calls) == 2
        assert decision.action == "rest"
        assert decision.mood == "neutral"

    @pytest.mark.anyio
    async def test_talk_routes_to_hook(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        reply = '{"action": "talk", "target": "bell", "content": null, "mood": "warm"}'
        transport = FakeTransport([TransportReply(text=reply)])
        engine = make_engine(conn, make_rng(7), transport)
        talks: list[tuple[str, str]] = []

        async def on_talk(speaker: str, target: str) -> None:
            talks.append((speaker, target))

        engine.on_talk = on_talk
        await engine.decide("tug")
        assert talks == [("tug", "bell")]


class TestRedecisionTriggers:
    @pytest.mark.anyio
    async def test_trigger_redecision_fires_immediately(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        transport = FakeTransport(
            [TransportReply(text=DECIDE_REPLY), TransportReply(text=DECIDE_REPLY)]
        )
        engine = make_engine(conn, make_rng(8), transport)
        await engine.trigger_redecision(["tug", "bell"])
        assert len(transport.calls) == 2

    @pytest.mark.anyio
    async def test_trigger_without_ids_hits_all_map_peers(
        self, conn: sqlite3.Connection, make_rng: Callable[[int], random.Random]
    ) -> None:
        transport = FakeTransport([TransportReply(text=DECIDE_REPLY) for _ in range(3)])
        engine = make_engine(conn, make_rng(9), transport)
        await engine.trigger_redecision()
        assert len(transport.calls) == 3
