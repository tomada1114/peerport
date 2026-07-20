"""Tests for `peerport.friends.mail` (friend mail + hearsay, #23)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from peerport.config import Config
from peerport.db import NewMail, get_mail, insert_mail, list_mails, open_db
from peerport.friends.mail import (
    DEFAULT_CADENCE_DAYS,
    FRIEND_PAIRS,
    SESSION_MAIL_CAP,
    MailService,
    run_cadence_loop,
)
from peerport.llm.budget import BudgetGuard
from peerport.llm.client import LLMClient, TransportReply
from peerport.memory.stream import MemoryStream
from peerport.peers.personas import load_personas
from peerport.world.clock import WorldClock
from tests.test_llm_client import FakeTransport
from tests.test_memory import FakeEmbedder

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Awaitable, Callable, Iterator

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "mail.db")
    yield connection
    connection.close()


def make_service(
    conn: sqlite3.Connection,
    transport: FakeTransport,
    *,
    world_seconds: int = 0,
    cadence_days: int = DEFAULT_CADENCE_DAYS,
) -> MailService:
    personas = load_personas(REPO_ROOT / "personas")
    llm = LLMClient(
        config=Config(), conn=conn, budget=BudgetGuard(conn), transport=transport
    )
    return MailService(
        llm=llm,
        conn=conn,
        memory=MemoryStream(conn, FakeEmbedder()),
        personas=personas,
        clock=WorldClock(day_length_real_minutes=120),
        now_world=lambda: world_seconds,
        cadence_days=cadence_days,
    )


def letter_reply(
    *,
    subject: str = "Hey",
    body: str = "Just checking in.",
    mood: str = "cheerful",
    recent_topics: list[str] | None = None,
    summary: str = "A quick hello.",
) -> TransportReply:
    return TransportReply(
        text=json.dumps(
            {
                "subject": subject,
                "body": body,
                "mood": mood,
                "recent_topics": recent_topics or ["exam week"],
                "summary": summary,
            }
        )
    )


class TestNotifyEvent:
    @pytest.mark.anyio
    async def test_event_involving_tug_generates_mail_from_kai(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport([letter_reply()])
        service = make_service(conn, transport)

        generated = await service.notify_event("tug", "Tug fixed the pier railing.")

        assert generated is True
        assert len(transport.calls) == 1
        assert transport.calls[0]["model"] == Config().models.background
        mails = list_mails(conn)
        assert len(mails) == 1
        assert mails[0].friend_id == "kai"
        assert mails[0].direction == "in"

    @pytest.mark.anyio
    async def test_event_involving_unpaired_peer_generates_nothing(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport([])
        service = make_service(conn, transport)

        generated = await service.notify_event("beacon", "Beacon did something.")

        assert generated is False
        assert transport.calls == []


class TestSessionCap:
    @pytest.mark.anyio
    async def test_fourth_trigger_in_one_session_generates_nothing(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport([letter_reply() for _ in range(3)])
        service = make_service(conn, transport)

        for _ in range(SESSION_MAIL_CAP):
            assert await service.notify_event("tug", "event") is True

        fourth = await service.notify_event("tug", "another event")

        assert fourth is False
        assert len(transport.calls) == SESSION_MAIL_CAP
        assert len(list_mails(conn)) == SESSION_MAIL_CAP

    @pytest.mark.anyio
    async def test_counter_resets_on_new_service_instance(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport([letter_reply() for _ in range(4)])
        service = make_service(conn, transport)
        for _ in range(SESSION_MAIL_CAP):
            await service.notify_event("tug", "event")
        assert await service.notify_event("tug", "capped") is False

        restarted = make_service(conn, transport)
        generated = await restarted.notify_event("tug", "fresh session")

        assert generated is True


class TestFriendState:
    @pytest.mark.anyio
    async def test_generation_populates_all_four_state_fields(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport(
            [
                letter_reply(
                    mood="proud",
                    recent_topics=["exam week", "Tug's pier fix"],
                    summary="Kai is proud of Tug.",
                )
            ]
        )
        service = make_service(conn, transport, world_seconds=7200)  # day 2

        await service.notify_event("tug", "Tug fixed the pier.")
        state = service.friend_state("kai")

        assert state.mood == "proud"
        assert state.recent_topics == ["exam week", "Tug's pier fix"]
        assert state.last_letter_summary == "Kai is proud of Tug."
        assert state.last_updated_world_day == 2

    def test_default_state_before_any_letter(self, conn: sqlite3.Connection) -> None:
        service = make_service(conn, FakeTransport([]))
        state = service.friend_state("kai")

        assert state.mood == "neutral"
        assert state.recent_topics == []
        assert state.last_letter_summary == ""
        assert state.last_updated_world_day == 0


class TestCadenceTrigger:
    @pytest.mark.anyio
    async def test_two_days_since_last_letter_generates_nothing(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport([])
        service = make_service(conn, transport, world_seconds=7200)  # day 2

        generated = await service.maybe_generate_cadence_mail("kai")

        assert generated is False
        assert transport.calls == []

    @pytest.mark.anyio
    async def test_three_days_since_last_letter_generates_mail(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport([letter_reply()])
        service = make_service(conn, transport, world_seconds=2 * 7200)  # day 3

        generated = await service.maybe_generate_cadence_mail("kai")

        assert generated is True
        assert len(transport.calls) == 1


class TestSimultaneousTriggersDedup:
    @pytest.mark.anyio
    async def test_event_then_cadence_same_tick_generates_only_one_mail(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport([letter_reply()])
        service = make_service(conn, transport, world_seconds=3 * 7200)  # day 4

        first = await service.notify_event("tug", "An event.")
        second = await service.maybe_generate_cadence_mail("kai")

        assert first is True
        assert second is False
        assert len(transport.calls) == 1


class TestHearsay:
    def test_no_letter_yet_returns_none(self, conn: sqlite3.Connection) -> None:
        service = make_service(conn, FakeTransport([]))
        assert service.hearsay_text("tug") is None

    def test_unpaired_peer_returns_none(self, conn: sqlite3.Connection) -> None:
        service = make_service(conn, FakeTransport([]))
        assert service.hearsay_text("beacon") is None

    @pytest.mark.anyio
    async def test_after_letter_references_keeper_and_summary(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport(
            [letter_reply(recent_topics=["exam week"], summary="Kai aced the exam.")]
        )
        service = make_service(conn, transport)
        await service.notify_event("tug", "trigger")

        hearsay = service.hearsay_text("tug")

        assert hearsay is not None
        assert "my Keeper said" in hearsay
        assert "Kai aced the exam." in hearsay


class TestMemoryWrite:
    @pytest.mark.anyio
    async def test_writes_conversation_kind_memory_for_friend(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport([letter_reply(body="Hello from Kai.")])
        service = make_service(conn, transport)

        await service.notify_event("tug", "trigger")

        row = conn.execute(
            "SELECT kind, text FROM memories WHERE peer_id = 'kai'"
        ).fetchone()
        assert row is not None
        assert row[0] == "conversation"
        assert row[1] == "Hello from Kai."


class TestReply:
    @pytest.mark.anyio
    async def test_reply_persists_outgoing_mail_with_parent_id(
        self, conn: sqlite3.Connection
    ) -> None:
        letter_id = insert_mail(
            conn,
            NewMail(friend_id="kai", direction="in", subject="Hi", body="body"),
        )
        transport = FakeTransport([letter_reply()])
        service = make_service(conn, transport)

        await service.reply(letter_id, "Glad to hear it!")

        reply_row = next(m for m in list_mails(conn) if m.direction == "out")
        assert reply_row.parent_id == letter_id
        assert reply_row.body == "Glad to hear it!"
        assert reply_row.friend_id == "kai"

    @pytest.mark.anyio
    async def test_reply_triggers_a_follow_up_letter(
        self, conn: sqlite3.Connection
    ) -> None:
        letter_id = insert_mail(
            conn,
            NewMail(friend_id="kai", direction="in", subject="Hi", body="body"),
        )
        transport = FakeTransport([letter_reply(subject="Re: Hi")])
        service = make_service(conn, transport)

        generated = await service.reply(letter_id, "Glad to hear it!")

        assert generated is True
        assert len(transport.calls) == 1
        follow_up = next(m for m in list_mails(conn) if m.direction == "in")
        assert follow_up.subject == "Re: Hi"

    @pytest.mark.anyio
    async def test_reply_to_unknown_mail_id_returns_false(
        self, conn: sqlite3.Connection
    ) -> None:
        service = make_service(conn, FakeTransport([]))

        generated = await service.reply(999, "text")

        assert generated is False
        assert get_mail(conn, 999) is None


class TestCadenceLoopResilience:
    """Finding: one bad friend used to kill the cadence loop forever.

    `run_cadence_loop` had no per-iteration exception handling, so an
    unguarded error for a single friend silently stopped cadence mail
    for every friend for the rest of the process's life.
    """

    @pytest.mark.anyio
    async def test_cadence_loop_survives_one_bad_friend(
        self,
        conn: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = make_service(conn, FakeTransport())
        calls: list[str] = []
        bad_friend_id = next(iter(FRIEND_PAIRS))

        async def flaky_cadence_mail(self: MailService, friend_id: str) -> bool:
            del self
            calls.append(friend_id)
            if friend_id == bad_friend_id:
                message = "boom"
                raise RuntimeError(message)
            return False

        monkeypatch.setattr(
            MailService, "maybe_generate_cadence_mail", flaky_cadence_mail
        )
        real_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

        async def fast_sleep(_seconds: float) -> None:
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fast_sleep)

        task = asyncio.ensure_future(run_cadence_loop(service))
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # Every friend after the bad one in FRIEND_PAIRS was still
        # reached in the same iteration; the loop kept going.
        assert set(calls) == set(FRIEND_PAIRS)
