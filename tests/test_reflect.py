"""Tests for `peerport.memory.reflect` (#26: reflection and forgetting)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from peerport.db import get_world_state, open_db
from peerport.llm.client import TransportReply, TransportUnavailableError
from peerport.memory.reflect import (
    DRIFT_SELF_CHECK_INSTRUCTION,
    FORGET_CLUSTER_SIZE,
    FORGET_THRESHOLD,
    REFLECTION_INSTRUCTIONS,
    UNREFLECTED_THRESHOLD,
    ReflectionEngine,
)
from peerport.memory.stream import MemoryStream
from peerport.peers.personas import load_personas
from peerport.world.clock import WorldClock
from tests.test_llm_client import FakeTransport
from tests.test_memory import FakeEmbedder, make_llm

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator

REPO_ROOT = Path(__file__).parent.parent

# WorldClock() defaults to a 120-minute day (7200 world seconds) split into
# four equal 1800-second bands: morning, day, dusk, night.
DAY_BAND_TS = 2000
NIGHT_BAND_TS = 6000


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "reflect.db")
    yield connection
    connection.close()


def make_engine(
    conn: sqlite3.Connection,
    transport: FakeTransport,
    now_world: Callable[[], int],
) -> ReflectionEngine:
    personas = load_personas(REPO_ROOT / "personas")
    return ReflectionEngine(
        llm=make_llm(conn, transport),
        conn=conn,
        memory=MemoryStream(conn, FakeEmbedder()),
        personas=personas,
        clock=WorldClock(),
        now_world=now_world,
    )


@dataclass(slots=True)
class SeedMemory:
    """Fields for one directly-inserted `memories` row (fast test seeding)."""

    peer_id: str
    ts_world: int = 0
    importance: int | None = 5
    text: str = "something happened"
    kind: str = "observation"
    reflected: int = 1


def seed_memory(conn: sqlite3.Connection, memory: SeedMemory) -> int:
    with conn:
        cursor = conn.execute(
            "INSERT INTO memories (peer_id, ts_world, ts_real, kind, text,"
            " importance, reflected) VALUES (?, ?, 0, ?, ?, ?, ?)",
            (
                memory.peer_id,
                memory.ts_world,
                memory.kind,
                memory.text,
                memory.importance,
                memory.reflected,
            ),
        )
    return int(cursor.lastrowid or 0)


def insights_reply(count: int, importance: int = 6) -> TransportReply:
    payload = {
        "insights": [
            {"text": f"insight {i}", "importance": importance} for i in range(count)
        ],
        "drift_notes": "still myself",
    }
    return TransportReply(text=json.dumps(payload))


def summary_reply(
    text: str = "a folded-away summary", importance: int = 3
) -> TransportReply:
    return TransportReply(text=json.dumps({"text": text, "importance": importance}))


class TestUnreflectedCount:
    def test_counts_only_unreflected_rows(self, conn: sqlite3.Connection) -> None:
        seed_memory(conn, SeedMemory(peer_id="tug", reflected=0))
        seed_memory(conn, SeedMemory(peer_id="tug", reflected=0))
        seed_memory(conn, SeedMemory(peer_id="tug", reflected=1))
        engine = make_engine(conn, FakeTransport(), lambda: 0)
        assert engine.unreflected_count("tug") == 2


class TestReflectionTriggers:
    @pytest.mark.anyio
    async def test_below_threshold_no_night_band_does_not_trigger(
        self, conn: sqlite3.Connection
    ) -> None:
        for _ in range(30):
            seed_memory(conn, SeedMemory(peer_id="tug", reflected=0))
        transport = FakeTransport()
        engine = make_engine(conn, transport, lambda: DAY_BAND_TS)
        fired = await engine.maybe_reflect("tug")
        assert fired is False
        assert transport.calls == []
        assert engine.unreflected_count("tug") == 30

    @pytest.mark.anyio
    async def test_night_band_triggers_for_every_unreflected_peer(
        self, conn: sqlite3.Connection
    ) -> None:
        seed_memory(conn, SeedMemory(peer_id="tug", reflected=0))
        seed_memory(conn, SeedMemory(peer_id="bell", reflected=0))
        transport = FakeTransport([insights_reply(3), insights_reply(2)])
        engine = make_engine(conn, transport, lambda: NIGHT_BAND_TS)
        assert await engine.maybe_reflect("tug") is True
        assert await engine.maybe_reflect("bell") is True
        assert len(transport.calls) == 2

    @pytest.mark.anyio
    async def test_50th_unreflected_memory_triggers_immediately_in_day_band(
        self, conn: sqlite3.Connection
    ) -> None:
        for _ in range(UNREFLECTED_THRESHOLD):
            seed_memory(conn, SeedMemory(peer_id="echo", reflected=0))
        transport = FakeTransport([insights_reply(3)])
        engine = make_engine(conn, transport, lambda: DAY_BAND_TS)
        fired = await engine.maybe_reflect("echo")
        assert fired is True
        assert engine.unreflected_count("echo") == 0

    @pytest.mark.anyio
    async def test_both_triggers_eligible_same_tick_runs_exactly_once(
        self, conn: sqlite3.Connection
    ) -> None:
        for _ in range(UNREFLECTED_THRESHOLD):
            seed_memory(conn, SeedMemory(peer_id="bell", reflected=0))
        transport = FakeTransport([insights_reply(3)])
        engine = make_engine(conn, transport, lambda: NIGHT_BAND_TS)
        fired = await engine.maybe_reflect("bell")
        assert fired is True
        assert len(transport.calls) == 1


class TestReflectionRun:
    @pytest.mark.anyio
    async def test_three_insights_write_three_reflection_rows_all_columns_set(
        self, conn: sqlite3.Connection
    ) -> None:
        for _ in range(5):
            seed_memory(conn, SeedMemory(peer_id="tug", reflected=0))
        transport = FakeTransport([insights_reply(3)])
        engine = make_engine(conn, transport, lambda: NIGHT_BAND_TS)
        await engine.maybe_reflect("tug")
        rows = conn.execute(
            "SELECT peer_id, ts_world, ts_real, kind, text, importance, embedding"
            " FROM memories WHERE kind = 'reflection'"
        ).fetchall()
        assert len(rows) == 3
        assert all(all(col is not None for col in row) for row in rows)

    @pytest.mark.anyio
    async def test_two_insights_write_two_reflection_rows(
        self, conn: sqlite3.Connection
    ) -> None:
        seed_memory(conn, SeedMemory(peer_id="tug", reflected=0))
        transport = FakeTransport([insights_reply(2)])
        engine = make_engine(conn, transport, lambda: NIGHT_BAND_TS)
        await engine.maybe_reflect("tug")
        count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE kind = 'reflection'"
        ).fetchone()[0]
        assert count == 2

    @pytest.mark.anyio
    async def test_prompt_includes_persona_body_and_recent_memories(
        self, conn: sqlite3.Connection
    ) -> None:
        seed_memory(
            conn,
            SeedMemory(peer_id="tug", reflected=0, text="Kai opened a new dock stall"),
        )
        transport = FakeTransport([insights_reply(2)])
        engine = make_engine(conn, transport, lambda: NIGHT_BAND_TS)
        await engine.maybe_reflect("tug")
        prompt = transport.calls[0]["prompt"]
        assert isinstance(prompt, str)
        assert engine.personas["tug"].body in prompt
        assert "Kai opened a new dock stall" in prompt

    @pytest.mark.anyio
    async def test_prompt_contains_explicit_drift_self_check_distinct_from_insights(
        self, conn: sqlite3.Connection
    ) -> None:
        seed_memory(conn, SeedMemory(peer_id="tug", reflected=0))
        transport = FakeTransport([insights_reply(2)])
        engine = make_engine(conn, transport, lambda: NIGHT_BAND_TS)
        await engine.maybe_reflect("tug")
        prompt = transport.calls[0]["prompt"]
        assert isinstance(prompt, str)
        assert DRIFT_SELF_CHECK_INSTRUCTION in prompt
        assert REFLECTION_INSTRUCTIONS in prompt
        assert DRIFT_SELF_CHECK_INSTRUCTION != REFLECTION_INSTRUCTIONS

    @pytest.mark.anyio
    async def test_after_reflection_counter_resets_and_night_flag_set(
        self, conn: sqlite3.Connection
    ) -> None:
        seed_memory(conn, SeedMemory(peer_id="tug", reflected=0))
        transport = FakeTransport([insights_reply(2)])
        engine = make_engine(conn, transport, lambda: NIGHT_BAND_TS)
        await engine.maybe_reflect("tug")
        assert engine.unreflected_count("tug") == 0
        stored_day = get_world_state(conn, "last_reflection_day:tug")
        assert stored_day == str(engine.clock.day(NIGHT_BAND_TS))
        # Reflected-tonight flag suppresses a second night-band trigger the
        # same night, with no new unreflected memories to force it either.
        fired_again = await engine.maybe_reflect("tug")
        assert fired_again is False
        assert len(transport.calls) == 1


class TestInsightCountBoundaries:
    @pytest.mark.anyio
    async def test_four_insights_truncated_to_three(
        self, conn: sqlite3.Connection
    ) -> None:
        seed_memory(conn, SeedMemory(peer_id="tug", reflected=0))
        transport = FakeTransport([insights_reply(4)])
        engine = make_engine(conn, transport, lambda: NIGHT_BAND_TS)
        await engine.maybe_reflect("tug")
        count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE kind = 'reflection'"
        ).fetchone()[0]
        assert count == 3
        assert len(transport.calls) == 1

    @pytest.mark.anyio
    async def test_single_insight_retries_once_then_accepts_retry_result(
        self, conn: sqlite3.Connection
    ) -> None:
        seed_memory(conn, SeedMemory(peer_id="tug", reflected=0))
        transport = FakeTransport([insights_reply(1), insights_reply(2)])
        engine = make_engine(conn, transport, lambda: NIGHT_BAND_TS)
        await engine.maybe_reflect("tug")
        count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE kind = 'reflection'"
        ).fetchone()[0]
        assert count == 2
        assert len(transport.calls) == 2

    @pytest.mark.anyio
    async def test_single_insight_retry_still_under_count_accepts_one(
        self, conn: sqlite3.Connection
    ) -> None:
        seed_memory(conn, SeedMemory(peer_id="tug", reflected=0))
        transport = FakeTransport([insights_reply(1), insights_reply(1)])
        engine = make_engine(conn, transport, lambda: NIGHT_BAND_TS)
        await engine.maybe_reflect("tug")
        count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE kind = 'reflection'"
        ).fetchone()[0]
        assert count == 1
        assert len(transport.calls) == 2


class TestForgettingThreshold:
    @pytest.mark.anyio
    async def test_exactly_2000_rows_does_not_trigger(
        self, conn: sqlite3.Connection
    ) -> None:
        for i in range(FORGET_THRESHOLD):
            seed_memory(conn, SeedMemory(peer_id="mia", ts_world=i, importance=5))
        transport = FakeTransport()
        engine = make_engine(conn, transport, lambda: 0)
        assert await engine.forget_once("mia") is False
        assert transport.calls == []

    @pytest.mark.anyio
    async def test_2001_rows_triggers(self, conn: sqlite3.Connection) -> None:
        for i in range(FORGET_THRESHOLD + 1):
            seed_memory(conn, SeedMemory(peer_id="mia", ts_world=i, importance=5))
        transport = FakeTransport([summary_reply()])
        engine = make_engine(conn, transport, lambda: 0)
        assert await engine.forget_once("mia") is True
        assert len(transport.calls) == 1


class TestForgettingCluster:
    @pytest.mark.anyio
    async def test_cluster_is_100_lowest_importance_oldest_ts_world(
        self, conn: sqlite3.Connection
    ) -> None:
        # Two importance tiers so ties are broken by ts_world: the 100
        # oldest, lowest-importance rows must be exactly the ones removed.
        forgettable_ids = [
            seed_memory(
                conn,
                SeedMemory(peer_id="kai", ts_world=i, importance=1, text=f"low {i}"),
            )
            for i in range(150)
        ]
        for i in range(1851):
            seed_memory(
                conn,
                SeedMemory(
                    peer_id="kai", ts_world=1000 + i, importance=9, text=f"high {i}"
                ),
            )
        transport = FakeTransport([summary_reply()])
        engine = make_engine(conn, transport, lambda: 0)
        assert await engine.forget_once("kai") is True

        remaining_ids = {
            row[0]
            for row in conn.execute(
                "SELECT id FROM memories WHERE peer_id = 'kai' AND kind != 'reflection'"
            ).fetchall()
        }
        expected_removed = set(forgettable_ids[:FORGET_CLUSTER_SIZE])
        assert expected_removed.isdisjoint(remaining_ids)
        assert set(forgettable_ids[FORGET_CLUSTER_SIZE:]).issubset(remaining_ids)


class TestForgettingWriteThenDelete:
    @pytest.mark.anyio
    async def test_summary_written_and_originals_deleted_on_success(
        self, conn: sqlite3.Connection
    ) -> None:
        for i in range(FORGET_THRESHOLD + 1):
            seed_memory(conn, SeedMemory(peer_id="mia", ts_world=i, importance=5))
        transport = FakeTransport([summary_reply()])
        engine = make_engine(conn, transport, lambda: 0)
        await engine.forget_once("mia")

        total = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE peer_id = 'mia'"
        ).fetchone()[0]
        assert total == FORGET_THRESHOLD + 1 - FORGET_CLUSTER_SIZE + 1
        summary_count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE peer_id = 'mia' AND kind = 'reflection'"
        ).fetchone()[0]
        assert summary_count == 1

    @pytest.mark.anyio
    async def test_failed_summarization_leaves_all_originals_intact(
        self, conn: sqlite3.Connection
    ) -> None:
        for i in range(FORGET_THRESHOLD + 1):
            seed_memory(conn, SeedMemory(peer_id="bell", ts_world=i, importance=5))
        transport = FakeTransport([TransportUnavailableError("network down")] * 4)
        engine = make_engine(conn, transport, lambda: 0)

        async def no_sleep(_seconds: float) -> None:
            return None

        engine.llm._sleep = no_sleep  # noqa: SLF001 - avoid real backoff sleeps
        result = await engine.forget_once("bell")
        assert result is False

        total = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE peer_id = 'bell'"
        ).fetchone()[0]
        assert total == FORGET_THRESHOLD + 1
        summary_count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE peer_id = 'bell' AND kind = 'reflection'"
        ).fetchone()[0]
        assert summary_count == 0


class TestForgettingDefersToNextTick:
    @pytest.mark.anyio
    async def test_one_evaluation_removes_one_cluster_net_99(
        self, conn: sqlite3.Connection
    ) -> None:
        for i in range(2200):
            seed_memory(conn, SeedMemory(peer_id="tug", ts_world=i, importance=5))
        transport = FakeTransport([summary_reply(), summary_reply()])
        engine = make_engine(conn, transport, lambda: 0)

        assert await engine.forget_once("tug") is True
        count_after_first = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE peer_id = 'tug'"
        ).fetchone()[0]
        assert count_after_first == 2200 - FORGET_CLUSTER_SIZE + 1
        assert count_after_first > FORGET_THRESHOLD

        assert await engine.forget_once("tug") is True
        count_after_second = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE peer_id = 'tug'"
        ).fetchone()[0]
        assert count_after_second == count_after_first - FORGET_CLUSTER_SIZE + 1
