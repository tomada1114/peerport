"""Tests for `peerport.logbook` (absence reports, weekly summaries, #22)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from peerport.config import Config
from peerport.db import (
    get_relationship,
    open_db,
    save_last_shutdown_ts_real,
)
from peerport.llm.budget import BudgetGuard
from peerport.llm.client import LLMClient, TransportReply
from peerport.logbook import (
    ABSENCE_THRESHOLD_SECONDS,
    MAX_EVENTS,
    LogbookService,
    event_count_for_minutes,
)
from peerport.memory.stream import MemoryStream
from peerport.peers.personas import load_personas
from peerport.world.clock import WorldClock
from tests.test_llm_client import FakeTransport
from tests.test_memory import FakeEmbedder

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).parent.parent
LOCATIONS = ["dock_square", "signal_tower", "lighthouse", "pier_main"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "logbook.db")
    yield connection
    connection.close()


def make_service(
    conn: sqlite3.Connection,
    transport: FakeTransport,
    *,
    now_real: int = 1_700_010_000,
    world_seconds: int = 0,
) -> LogbookService:
    personas = load_personas(REPO_ROOT / "personas")
    llm = LLMClient(
        config=Config(), conn=conn, budget=BudgetGuard(conn), transport=transport
    )
    return LogbookService(
        llm=llm,
        conn=conn,
        memory=MemoryStream(conn, FakeEmbedder()),
        personas=personas,
        locations=LOCATIONS,
        clock=WorldClock(day_length_real_minutes=120),
        now_world=lambda: world_seconds,
        now_real=lambda: now_real,
    )


def events_reply(events: list[dict[str, object]]) -> TransportReply:
    return TransportReply(text=json.dumps({"events": events}))


class TestEventCountForMinutes:
    @pytest.mark.parametrize(
        ("minutes", "expected"),
        [(0, 3), (30, 5), (10080, 10), (100_000, 10)],
    )
    def test_matches_formula(self, minutes: float, expected: int) -> None:
        assert event_count_for_minutes(minutes) == expected


class TestAbsenceReportTrigger:
    @pytest.mark.anyio
    async def test_no_last_shutdown_generates_nothing(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport([])
        service = make_service(conn, transport)

        events = await service.maybe_generate_absence_report()

        assert events == []
        assert transport.calls == []

    @pytest.mark.anyio
    async def test_elapsed_1799s_generates_nothing(
        self, conn: sqlite3.Connection
    ) -> None:
        now = 1_700_010_000
        save_last_shutdown_ts_real(conn, now - (ABSENCE_THRESHOLD_SECONDS - 1))
        transport = FakeTransport([])
        service = make_service(conn, transport, now_real=now)

        events = await service.maybe_generate_absence_report()

        assert events == []
        assert transport.calls == []

    @pytest.mark.anyio
    async def test_elapsed_1800s_generates_exactly_one_call(
        self, conn: sqlite3.Connection
    ) -> None:
        now = 1_700_010_000
        save_last_shutdown_ts_real(conn, now - ABSENCE_THRESHOLD_SECONDS)
        transport = FakeTransport(
            [
                events_reply(
                    [
                        {"peer_ids": ["tug"], "text": "Tug tidied the pier."},
                        {"peer_ids": ["bell"], "text": "Bell read at Dock Square."},
                        {
                            "peer_ids": ["tug", "bell"],
                            "text": "Tug and Bell fixed the railing together.",
                        },
                    ]
                )
            ]
        )
        service = make_service(conn, transport, now_real=now)

        events = await service.maybe_generate_absence_report()

        assert len(transport.calls) == 1
        assert transport.calls[0]["model"] == Config().models.background
        assert len(events) == 3

    @pytest.mark.anyio
    async def test_hard_caps_at_ten_events_on_extreme_absence(
        self, conn: sqlite3.Connection
    ) -> None:
        now = 1_700_010_000
        save_last_shutdown_ts_real(conn, now - 30 * 86400)  # 30 days away
        raw_events = [{"peer_ids": ["tug"], "text": f"event {i}"} for i in range(15)]
        transport = FakeTransport([events_reply(raw_events)])
        service = make_service(conn, transport, now_real=now)

        events = await service.maybe_generate_absence_report()

        assert len(events) == MAX_EVENTS


class TestRejectUnknownEntities:
    @pytest.mark.anyio
    async def test_drops_events_referencing_unknown_peer(
        self, conn: sqlite3.Connection
    ) -> None:
        now = 1_700_010_000
        save_last_shutdown_ts_real(conn, now - ABSENCE_THRESHOLD_SECONDS)
        transport = FakeTransport(
            [
                events_reply(
                    [
                        {"peer_ids": ["tug"], "text": "Tug tidied the pier."},
                        {
                            "peer_ids": ["harbor_festival_mascot"],
                            "text": "The Harbor Festival mascot paraded through.",
                        },
                    ]
                )
            ]
        )
        service = make_service(conn, transport, now_real=now)

        events = await service.maybe_generate_absence_report()

        assert len(events) == 1
        assert events[0].peer_ids == ["tug"]
        row = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE peer_id = 'harbor_festival_mascot'"
        ).fetchone()
        assert row[0] == 0


class TestMemoryAndRelationshipWrites:
    @pytest.mark.anyio
    async def test_writes_kind_logbook_memory_per_involved_peer(
        self, conn: sqlite3.Connection
    ) -> None:
        now = 1_700_010_000
        save_last_shutdown_ts_real(conn, now - ABSENCE_THRESHOLD_SECONDS)
        transport = FakeTransport(
            [
                events_reply(
                    [
                        {
                            "peer_ids": ["tug", "bell"],
                            "text": "Tug and Bell fixed the railing.",
                        }
                    ]
                )
            ]
        )
        service = make_service(conn, transport, now_real=now)

        await service.maybe_generate_absence_report()

        for peer_id in ("tug", "bell"):
            row = conn.execute(
                "SELECT kind FROM memories WHERE peer_id = ?", (peer_id,)
            ).fetchone()
            assert row is not None
            assert row[0] == "logbook"

    @pytest.mark.anyio
    async def test_applies_nonzero_relationship_delta_for_multi_peer_event(
        self, conn: sqlite3.Connection
    ) -> None:
        now = 1_700_010_000
        save_last_shutdown_ts_real(conn, now - ABSENCE_THRESHOLD_SECONDS)
        transport = FakeTransport(
            [
                events_reply(
                    [
                        {
                            "peer_ids": ["tug", "bell"],
                            "text": "Tug and Bell fixed the railing.",
                        }
                    ]
                )
            ]
        )
        service = make_service(conn, transport, now_real=now)

        await service.maybe_generate_absence_report()

        relationship = get_relationship(conn, "tug", "bell")
        assert relationship.score != 0

    @pytest.mark.anyio
    async def test_single_peer_event_does_not_touch_relationships(
        self, conn: sqlite3.Connection
    ) -> None:
        now = 1_700_010_000
        save_last_shutdown_ts_real(conn, now - ABSENCE_THRESHOLD_SECONDS)
        transport = FakeTransport(
            [events_reply([{"peer_ids": ["tug"], "text": "Tug tidied the pier."}])]
        )
        service = make_service(conn, transport, now_real=now)

        await service.maybe_generate_absence_report()

        relationship = get_relationship(conn, "tug", "bell")
        assert relationship.score == 0


class TestWeeklySummary:
    @pytest.mark.anyio
    async def test_disabled_generates_nothing(self, conn: sqlite3.Connection) -> None:
        transport = FakeTransport([])
        service = make_service(conn, transport, world_seconds=7 * 7200)

        events = await service.maybe_generate_weekly_summary(enabled=False)

        assert events == []
        assert transport.calls == []

    @pytest.mark.anyio
    async def test_before_seven_days_generates_nothing(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport([])
        service = make_service(conn, transport, world_seconds=5 * 7200)  # day 6

        events = await service.maybe_generate_weekly_summary(enabled=True)

        assert events == []
        assert transport.calls == []

    @pytest.mark.anyio
    async def test_at_seven_days_generates_exactly_one_call(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport(
            [events_reply([{"peer_ids": ["kai"], "text": "Quiet week at the pier."}])]
        )
        service = make_service(conn, transport, world_seconds=6 * 7200)  # day 7

        events = await service.maybe_generate_weekly_summary(enabled=True)

        assert len(transport.calls) == 1
        assert len(events) == 1


class TestReadLogbook:
    def test_fresh_db_returns_empty_sections(self, conn: sqlite3.Connection) -> None:
        transport = FakeTransport([])
        service = make_service(conn, transport)

        data = service.read_logbook()

        assert data == {"while_away": [], "chronicle": []}

    @pytest.mark.anyio
    async def test_while_away_shows_only_latest_batch(
        self, conn: sqlite3.Connection
    ) -> None:
        now = 1_700_010_000
        save_last_shutdown_ts_real(conn, now - ABSENCE_THRESHOLD_SECONDS)
        transport = FakeTransport(
            [
                events_reply([{"peer_ids": ["tug"], "text": "First absence event."}]),
                events_reply([{"peer_ids": ["bell"], "text": "Second absence event."}]),
            ]
        )
        service = make_service(conn, transport, now_real=now)
        await service.maybe_generate_absence_report()

        save_last_shutdown_ts_real(conn, now + 100 - ABSENCE_THRESHOLD_SECONDS)
        service2 = make_service(conn, transport, now_real=now + 100)
        await service2.maybe_generate_absence_report()

        data = service2.read_logbook()

        assert len(data["while_away"]) == 1
        assert data["while_away"][0]["text"] == "Second absence event."

    @pytest.mark.anyio
    async def test_chronicle_groups_by_world_day(
        self, conn: sqlite3.Connection
    ) -> None:
        now = 1_700_010_000
        save_last_shutdown_ts_real(conn, now - ABSENCE_THRESHOLD_SECONDS)
        transport = FakeTransport(
            [events_reply([{"peer_ids": ["tug"], "text": "Tug tidied the pier."}])]
        )
        service = make_service(conn, transport, now_real=now, world_seconds=7200)

        await service.maybe_generate_absence_report()
        data = service.read_logbook()

        assert data["chronicle"] == [{"day": 2, "entries": ["Tug tidied the pier."]}]


class TestDigestText:
    def test_empty_events_yields_empty_digest(self, conn: sqlite3.Connection) -> None:
        service = make_service(conn, FakeTransport([]))
        assert service.digest_text([]) == ""

    @pytest.mark.anyio
    async def test_nonempty_events_yields_while_away_framing(
        self, conn: sqlite3.Connection
    ) -> None:
        now = 1_700_010_000
        save_last_shutdown_ts_real(conn, now - ABSENCE_THRESHOLD_SECONDS)
        transport = FakeTransport(
            [events_reply([{"peer_ids": ["tug"], "text": "Tug tidied the pier."}])]
        )
        service = make_service(conn, transport, now_real=now)

        events = await service.maybe_generate_absence_report()
        digest = service.digest_text(events)

        assert "Tug tidied the pier." in digest
        assert digest.lower().startswith("welcome back")
