"""Tests for `peerport.mate.chat` (streaming chat, summary memory, +2 bias)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from peerport.config import Config, WorldConfig
from peerport.db import open_db
from peerport.llm.budget import BudgetGuard
from peerport.llm.client import LLMClient, TransportReply
from peerport.mate.chat import KEEPER_BIAS, MateChat
from peerport.memory.stream import MemoryStream
from peerport.server.app import create_app
from tests.test_llm_client import FakeTransport
from tests.test_memory import FakeEmbedder

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from peerport.server.state import Broadcaster


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "chat.db")
    yield connection
    connection.close()


class FakeStreamingTransport(FakeTransport):
    """FakeTransport plus token-streaming for the chat path."""

    def __init__(
        self,
        stream_tokens: list[str] | None = None,
        replies: list[object] | None = None,
    ) -> None:
        super().__init__(replies)
        self.stream_tokens = stream_tokens or ["Quiet ", "morning ", "here."]
        self.stream_calls: list[dict[str, object]] = []

    async def stream_complete(
        self,
        *,
        model: str,
        prompt: str,
        max_output_tokens: int,
        on_delta: Callable[[str], object],
    ) -> TransportReply:
        self.stream_calls.append(
            {"model": model, "prompt": prompt, "max_output_tokens": max_output_tokens}
        )
        for token in self.stream_tokens:
            result = on_delta(token)
            if hasattr(result, "__await__"):
                await result
        return TransportReply(
            text="".join(self.stream_tokens), input_tokens=100, output_tokens=10
        )


def make_chat(
    conn: sqlite3.Connection,
    broadcaster: Broadcaster,
    transport: FakeStreamingTransport,
) -> MateChat:
    async def no_sleep(_s: float) -> None:
        return

    llm = LLMClient(
        config=Config(),
        conn=conn,
        budget=BudgetGuard(conn),
        transport=transport,
        sleep=no_sleep,
    )
    return MateChat(
        llm=llm,
        memory=MemoryStream(conn, FakeEmbedder()),
        broadcaster=broadcaster,
        mate_id="beacon",
        fixed_prefix="PERSONA-PREFIX",
        now_world=lambda: 1234,
    )


class TestChatEndToEnd:
    def test_post_chat_streams_chat_delta_frames_over_ws(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeStreamingTransport(
            replies=[
                TransportReply(text="We talked about the town."),
                TransportReply(text='{"scores": [7]}'),
            ]
        )
        app = create_app(Config(world=WorldConfig(tick_ms=60000)))
        with TestClient(app) as client:
            app.state.mate_chat = make_chat(conn, app.state.broadcaster, transport)
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()  # snapshot
                response = client.post("/api/chat", json={"text": "How's the town?"})
                assert response.status_code == 200
                frames = [ws.receive_json() for _ in range(4)]

        deltas = [f["text"] for f in frames if f["t"] == "chat_delta"]
        assert deltas == ["Quiet ", "morning ", "here."]
        done = [f for f in frames if f["t"] == "chat_done"]
        assert len(done) == 1
        assert done[0]["text"] == "Quiet morning here."

    def test_summary_written_with_keeper_bias(self, conn: sqlite3.Connection) -> None:
        transport = FakeStreamingTransport(
            replies=[
                TransportReply(text="Keeper checked in about the harbor."),
                TransportReply(text='{"scores": [7]}'),
            ]
        )
        app = create_app(Config(world=WorldConfig(tick_ms=60000)))
        with TestClient(app) as client:
            app.state.mate_chat = make_chat(conn, app.state.broadcaster, transport)
            client.post("/api/chat", json={"text": "hello beacon"})

        kind, text, importance = conn.execute(
            "SELECT kind, text, importance FROM memories"
        ).fetchone()
        assert kind == "conversation"
        assert text == "Keeper checked in about the harbor."
        assert importance == 7 + KEEPER_BIAS  # base 7, +2 keeper bias

    def test_keeper_bias_clamps_at_10(self, conn: sqlite3.Connection) -> None:
        transport = FakeStreamingTransport(
            replies=[
                TransportReply(text="A heartfelt talk about trust."),
                TransportReply(text='{"scores": [9]}'),
            ]
        )
        app = create_app(Config(world=WorldConfig(tick_ms=60000)))
        with TestClient(app) as client:
            app.state.mate_chat = make_chat(conn, app.state.broadcaster, transport)
            client.post("/api/chat", json={"text": "do you trust me?"})

        importance = conn.execute("SELECT importance FROM memories").fetchone()[0]
        assert importance == 10

    def test_chat_uses_mate_model_and_summary_uses_background(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeStreamingTransport(
            replies=[
                TransportReply(text="summary"),
                TransportReply(text='{"scores": [5]}'),
            ]
        )
        app = create_app(Config(world=WorldConfig(tick_ms=60000)))
        with TestClient(app) as client:
            app.state.mate_chat = make_chat(conn, app.state.broadcaster, transport)
            client.post("/api/chat", json={"text": "hi"})

        assert transport.stream_calls[0]["model"] == "gpt-5-mini"
        assert transport.calls[0]["model"] == "gpt-5-nano"
        prompt = transport.stream_calls[0]["prompt"]
        assert isinstance(prompt, str)
        assert prompt.startswith("PERSONA-PREFIX")

    def test_empty_message_rejected(self, conn: sqlite3.Connection) -> None:
        app = create_app(Config(world=WorldConfig(tick_ms=60000)))
        with TestClient(app) as client:
            app.state.mate_chat = make_chat(
                conn, app.state.broadcaster, FakeStreamingTransport()
            )
            assert client.post("/api/chat", json={"text": "  "}).status_code == 422

    def test_chat_without_mate_wired_returns_501(self) -> None:
        app = create_app(Config(world=WorldConfig(tick_ms=60000)))
        with TestClient(app) as client:
            assert client.post("/api/chat", json={"text": "hi"}).status_code == 501
