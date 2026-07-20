"""Tests for `peerport.memory` (stream writes, importance batch, retrieval)."""

from __future__ import annotations

import asyncio
import contextlib
import math
from typing import TYPE_CHECKING

import pytest

from peerport.config import Config
from peerport.db import open_db
from peerport.errors import InvalidMemoryKindError
from peerport.llm.budget import BudgetGuard
from peerport.llm.client import LLMClient, TransportReply
from peerport.memory.recall import RecallResult, cosine_similarity, retrieve
from peerport.memory.stream import (
    MEMORY_KINDS,
    MemoryStream,
    clamp_importance,
    run_scoring_loop,
    unpack_embedding,
)
from tests.test_llm_client import FakeTransport

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "test.db")
    yield connection
    connection.close()


class FakeEmbedder:
    """Deterministic embedding stand-in; can be told to fail."""

    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self.vectors = vectors or {}
        self.calls: list[str] = []
        self.fail = False

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.fail:
            message = "embedding backend down"
            raise RuntimeError(message)
        return self.vectors.get(text, [1.0, 0.0, 0.0])


def make_llm(conn: sqlite3.Connection, transport: FakeTransport) -> LLMClient:
    async def no_sleep(_seconds: float) -> None:
        return

    return LLMClient(
        config=Config(),
        conn=conn,
        budget=BudgetGuard(conn),
        transport=transport,
        sleep=no_sleep,
    )


def make_stream(
    conn: sqlite3.Connection, embedder: FakeEmbedder | None = None
) -> tuple[MemoryStream, FakeEmbedder]:
    resolved = embedder or FakeEmbedder()
    return MemoryStream(conn, resolved), resolved


class TestWrites:
    @pytest.mark.anyio
    async def test_write_populates_all_columns_after_scoring(
        self, conn: sqlite3.Connection
    ) -> None:
        stream, embedder = make_stream(conn)
        await stream.write(
            peer_id="tug",
            ts_world=1200,
            kind="observation",
            text="Kai opened a new stall at Dock Square",
        )
        transport = FakeTransport([TransportReply(text='{"scores": [6]}')])
        await stream.score_pending_importance(make_llm(conn, transport), "tug")

        row = conn.execute(
            "SELECT peer_id, ts_world, ts_real, kind, text, importance, embedding"
            " FROM memories"
        ).fetchone()
        assert all(column is not None for column in row)
        assert row[5] == 6
        assert unpack_embedding(row[6]) == [1.0, 0.0, 0.0]
        assert embedder.calls == ["Kai opened a new stall at Dock Square"]

    @pytest.mark.anyio
    async def test_invalid_kind_rejected_no_row(self, conn: sqlite3.Connection) -> None:
        stream, _ = make_stream(conn)
        with pytest.raises(InvalidMemoryKindError, match="rumor"):
            await stream.write(peer_id="tug", ts_world=0, kind="rumor", text="x")
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0

    def test_memory_kinds_are_the_five_allowed(self) -> None:
        assert (
            frozenset(
                {"observation", "conversation", "reflection", "logbook", "keeper_note"}
            )
            == MEMORY_KINDS
        )

    @pytest.mark.anyio
    async def test_hearsay_framed_and_kind_conversation(
        self, conn: sqlite3.Connection
    ) -> None:
        stream, _ = make_stream(conn)
        await stream.write_hearsay(
            peer_id="mia", ts_world=100, fact="Bell went to the dock this morning"
        )
        kind, text = conn.execute("SELECT kind, text FROM memories").fetchone()
        assert kind == "conversation"
        assert text.startswith("I heard")

    @pytest.mark.anyio
    async def test_embed_failure_stores_row_without_embedding(
        self, conn: sqlite3.Connection
    ) -> None:
        stream, embedder = make_stream(conn)
        embedder.fail = True
        await stream.write(
            peer_id="tug", ts_world=0, kind="observation", text="quiet morning"
        )
        embedding = conn.execute("SELECT embedding FROM memories").fetchone()[0]
        assert embedding is None

    @pytest.mark.anyio
    async def test_embed_failure_logs_a_traceback(
        self, conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Finding: the degrade path used logger.warning (no traceback).

        That's indistinguishable in the logs from a real embedder bug;
        it must use logger.exception so unexpected failures still
        surface one.
        """
        stream, embedder = make_stream(conn)
        embedder.fail = True

        with caplog.at_level("WARNING"):
            await stream.write(
                peer_id="tug", ts_world=0, kind="observation", text="quiet morning"
            )

        records = [r for r in caplog.records if "embedding failed" in r.message]
        assert len(records) == 1
        assert records[0].exc_info is not None


class TestImportanceBatch:
    @pytest.mark.anyio
    async def test_five_pending_scored_in_one_call(
        self, conn: sqlite3.Connection
    ) -> None:
        stream, _ = make_stream(conn)
        for i in range(5):
            await stream.write(
                peer_id="tug", ts_world=i, kind="observation", text=f"event {i}"
            )
        transport = FakeTransport([TransportReply(text='{"scores": [3, 5, 7, 2, 9]}')])
        await stream.score_pending_importance(make_llm(conn, transport), "tug")
        assert len(transport.calls) == 1
        scores = [
            row[0]
            for row in conn.execute(
                "SELECT importance FROM memories ORDER BY id"
            ).fetchall()
        ]
        assert scores == [3, 5, 7, 2, 9]

    @pytest.mark.anyio
    async def test_single_pending_memory_still_batched(
        self, conn: sqlite3.Connection
    ) -> None:
        stream, _ = make_stream(conn)
        await stream.write(peer_id="echo", ts_world=0, kind="observation", text="mist")
        transport = FakeTransport([TransportReply(text='{"scores": [4]}')])
        await stream.score_pending_importance(make_llm(conn, transport), "echo")
        assert len(transport.calls) == 1

    @pytest.mark.anyio
    async def test_raw_scores_clamped_to_1_10(self, conn: sqlite3.Connection) -> None:
        stream, _ = make_stream(conn)
        for text in ("festival", "dust"):
            await stream.write(peer_id="tug", ts_world=0, kind="observation", text=text)
        transport = FakeTransport([TransportReply(text='{"scores": [12, -1]}')])
        await stream.score_pending_importance(make_llm(conn, transport), "tug")
        scores = sorted(
            row[0] for row in conn.execute("SELECT importance FROM memories")
        )
        assert scores == [1, 10]

    def test_clamp_importance_bounds_and_bias(self) -> None:
        assert clamp_importance(12) == 10
        assert clamp_importance(0) == 1
        assert clamp_importance(9, bias=2) == 10
        assert clamp_importance(5, bias=2) == 7


class TestOnlyIdsScoping:
    """Finding: a batched scoring call had no way to target specific rows.

    `MateChat._summarize` needs to apply the Keeper +2 bias to only the
    memory row it just wrote, not the peer's whole pending batch (which
    could include an unrelated pending memory from an ordinary
    peer-to-peer conversation, since the Mate is itself a map peer).
    """

    @pytest.mark.anyio
    async def test_only_ids_scores_just_the_given_row(
        self, conn: sqlite3.Connection
    ) -> None:
        stream, _ = make_stream(conn)
        first_id = await stream.write(
            peer_id="tug", ts_world=0, kind="observation", text="first"
        )
        await stream.write(peer_id="tug", ts_world=1, kind="observation", text="second")
        transport = FakeTransport([TransportReply(text='{"scores": [7]}')])

        scored = await stream.score_pending_importance(
            make_llm(conn, transport), "tug", only_ids=[first_id]
        )

        assert scored == 1
        rows = dict(conn.execute("SELECT text, importance FROM memories").fetchall())
        assert rows["first"] == 7
        assert rows["second"] is None

    @pytest.mark.anyio
    async def test_only_ids_scopes_bias_away_from_unrelated_pending_rows(
        self, conn: sqlite3.Connection
    ) -> None:
        stream, _ = make_stream(conn)
        biased_id = await stream.write(
            peer_id="tug", ts_world=0, kind="observation", text="keeper exchange"
        )
        await stream.write(
            peer_id="tug", ts_world=1, kind="observation", text="unrelated chat"
        )
        transport = FakeTransport([TransportReply(text='{"scores": [5]}')])

        await stream.score_pending_importance(
            make_llm(conn, transport), "tug", bias=2, only_ids=[biased_id]
        )

        rows = dict(conn.execute("SELECT text, importance FROM memories").fetchall())
        assert rows["keeper exchange"] == 7  # 5 + bias 2
        assert rows["unrelated chat"] is None  # untouched by the scoped call

    @pytest.mark.anyio
    async def test_empty_only_ids_makes_no_call(self, conn: sqlite3.Connection) -> None:
        stream, _ = make_stream(conn)
        await stream.write(peer_id="tug", ts_world=0, kind="observation", text="x")
        transport = FakeTransport([])

        scored = await stream.score_pending_importance(
            make_llm(conn, transport), "tug", only_ids=[]
        )

        assert scored == 0
        assert transport.calls == []


class TestScoringLoop:
    """Finding: `score_pending_importance` was only ever invoked from Mate chat.

    `MateChat._summarize`, scoped to the Mate, was the only call site --
    every other peer's memories stayed `importance IS NULL` forever.
    `run_scoring_loop` mirrors `run_reflection_loop`/`run_forgetting_loop`'s
    polling pattern.
    """

    @pytest.mark.anyio
    async def test_scoring_loop_scores_every_peer(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stream, _ = make_stream(conn)
        await stream.write(peer_id="tug", ts_world=0, kind="observation", text="a")
        await stream.write(peer_id="bell", ts_world=0, kind="observation", text="b")
        transport = FakeTransport(
            [
                TransportReply(text='{"scores": [4]}'),
                TransportReply(text='{"scores": [6]}'),
            ]
        )
        llm = make_llm(conn, transport)
        real_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

        async def fast_sleep(_seconds: float) -> None:
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fast_sleep)

        task = asyncio.ensure_future(run_scoring_loop(llm, stream, ["tug", "bell"]))
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        importances = dict(
            conn.execute("SELECT peer_id, importance FROM memories").fetchall()
        )
        assert importances["tug"] is not None
        assert importances["bell"] is not None

    @pytest.mark.anyio
    async def test_scoring_loop_survives_one_bad_peer(
        self,
        conn: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stream, _ = make_stream(conn)
        llm = make_llm(conn, FakeTransport())
        calls: list[str] = []

        async def flaky_score(
            self: MemoryStream,
            llm: LLMClient,
            peer_id: str,
            bias: int = 0,
            only_ids: object = None,
        ) -> int:
            del self, llm, bias, only_ids
            calls.append(peer_id)
            if peer_id == "bad":
                message = "boom"
                raise RuntimeError(message)
            return 0

        monkeypatch.setattr(MemoryStream, "score_pending_importance", flaky_score)
        real_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

        async def fast_sleep(_seconds: float) -> None:
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fast_sleep)

        task = asyncio.ensure_future(run_scoring_loop(llm, stream, ["bad", "good"]))
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert "good" in calls  # the loop kept going past "bad"'s failure


class TestRetrieval:
    @pytest.mark.anyio
    async def test_relevant_recent_important_memory_ranks_first(
        self, conn: sqlite3.Connection
    ) -> None:
        embedder = FakeEmbedder(
            {
                "the keeper fixed the beam": [1.0, 0.0, 0.0],
                "old festival story": [0.0, 1.0, 0.0],
                "dust on the quay": [0.0, 0.0, 1.0],
                "lighthouse repair?": [1.0, 0.0, 0.0],
            }
        )
        stream, _ = make_stream(conn, embedder)
        rows = [
            ("the keeper fixed the beam", 7000, 9),
            ("old festival story", 100, 9),
            ("dust on the quay", 7000, 1),
        ]
        for text, ts_world, importance in rows:
            await stream.write(
                peer_id="beacon",
                ts_world=ts_world,
                kind="observation",
                text=text,
                importance=importance,
            )
        results = await retrieve(
            stream, peer_id="beacon", query="lighthouse repair?", now_world=7200
        )
        assert results[0].text == "the keeper fixed the beam"
        assert results[0].score > results[1].score

    @pytest.mark.anyio
    async def test_recency_breaks_ties(self, conn: sqlite3.Connection) -> None:
        stream, _ = make_stream(conn)
        for text, ts_world in (("earlier", 0), ("later", 5000)):
            await stream.write(
                peer_id="tug",
                ts_world=ts_world,
                kind="observation",
                text=text,
                importance=5,
            )
        results = await retrieve(stream, peer_id="tug", query="any", now_world=5000)
        assert [r.text for r in results] == ["later", "earlier"]

    @pytest.mark.anyio
    async def test_top_k_is_10_and_smaller_pools_return_all(
        self, conn: sqlite3.Connection
    ) -> None:
        stream, _ = make_stream(conn)
        for i in range(25):
            await stream.write(
                peer_id="bell",
                ts_world=i,
                kind="observation",
                text=f"m{i}",
                importance=5,
            )
        for i in range(4):
            await stream.write(
                peer_id="echo",
                ts_world=i,
                kind="observation",
                text=f"e{i}",
                importance=5,
            )
        many = await retrieve(stream, peer_id="bell", query="q", now_world=30)
        few = await retrieve(stream, peer_id="echo", query="q", now_world=30)
        assert len(many) == 10
        scores = [result.score for result in many]
        assert scores == sorted(scores, reverse=True)
        assert len(few) == 4

    @pytest.mark.anyio
    async def test_embedding_failure_falls_back_to_two_axes(
        self, conn: sqlite3.Connection
    ) -> None:
        stream, embedder = make_stream(conn)
        for i in range(12):
            await stream.write(
                peer_id="bell",
                ts_world=i * 100,
                kind="observation",
                text=f"m{i}",
                importance=i % 10 + 1,
            )
        embedder.fail = True
        results = await retrieve(stream, peer_id="bell", query="q", now_world=1200)
        assert len(results) == 10
        assert all(result.relevance_used is False for result in results)

    @pytest.mark.anyio
    async def test_query_embedding_failure_logs_a_traceback(
        self, conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Finding: same logger.warning-without-traceback issue as write()."""
        stream, embedder = make_stream(conn)
        embedder.fail = True

        with caplog.at_level("WARNING"):
            await retrieve(stream, peer_id="bell", query="q", now_world=0)

        records = [r for r in caplog.records if "query embedding failed" in r.message]
        assert len(records) == 1
        assert records[0].exc_info is not None

    def test_cosine_similarity_pure_python(self) -> None:
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
        assert cosine_similarity([1.0, 1.0], [1.0, 1.0]) == pytest.approx(1.0)
        assert cosine_similarity([], [1.0]) == 0.0

    def test_recall_result_exposes_memory_fields(self) -> None:
        result = RecallResult(
            memory_id=1,
            text="t",
            kind="observation",
            importance=5,
            score=1.0,
            relevance_used=True,
        )
        assert math.isfinite(result.score)
