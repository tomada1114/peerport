"""Tests for `peerport.llm.client` (gateway, retries, usage) and prompts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from peerport.config import Config, ModelsConfig
from peerport.db import open_db
from peerport.errors import BudgetExceededError, LLMCallError
from peerport.llm.budget import BudgetGuard
from peerport.llm.client import (
    LLMClient,
    PromptParts,
    ToolCall,
    TransportRateLimitedError,
    TransportReply,
    TransportUnavailableError,
)
from peerport.llm.prompts import WORLD_RULES, assemble_prompt, build_fixed_prefix

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


class Verdict(BaseModel):
    mood: str
    score: int


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(tmp_path / "test.db")
    yield connection
    connection.close()


class FakeTransport:
    """Canned in-memory stand-in for the OpenAI Responses API boundary."""

    def __init__(self, replies: list[object] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict[str, object]] = []

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        schema: dict[str, object] | None,
        max_output_tokens: int,
        tools: list[dict[str, object]] | None = None,
    ) -> TransportReply:
        self.calls.append(
            {
                "model": model,
                "prompt": prompt,
                "schema": schema,
                "max_output_tokens": max_output_tokens,
                "tools": tools,
            }
        )
        reply = self.replies.pop(0) if self.replies else TransportReply(text="ok")
        if isinstance(reply, Exception):
            raise reply
        assert isinstance(reply, TransportReply)
        return reply


def make_client(
    conn: sqlite3.Connection,
    transport: FakeTransport,
    config: Config | None = None,
    sleeps: list[float] | None = None,
) -> LLMClient:
    async def fake_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return LLMClient(
        config=config or Config(),
        conn=conn,
        budget=BudgetGuard(conn),
        transport=transport,
        sleep=fake_sleep,
    )


class TestPrompts:
    def test_fixed_prefix_is_placed_first(self) -> None:
        prompt = assemble_prompt("FIXED-PART", "variable tail")
        assert prompt.startswith("FIXED-PART")
        assert prompt.index("variable tail") > 0

    def test_fixed_prefix_byte_stable_across_calls(self) -> None:
        first = build_fixed_prefix("persona body text", "en")
        second = build_fixed_prefix("persona body text", "en")
        assert first == second
        assert first.startswith(WORLD_RULES)

    def test_world_rules_covers_the_required_ground(self) -> None:
        lowered = WORLD_RULES.lower()
        assert len(WORLD_RULES.split()) <= 300
        for needle in ("port", "persona", "instructions", "language"):
            assert needle in lowered, f"world rules missing: {needle}"


class TestModelResolution:
    @pytest.mark.anyio
    async def test_roles_resolve_to_default_models(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport()
        client = make_client(conn, transport)
        await client.call(role="background", prompt=PromptParts("f", "v"))
        await client.call(role="mate", prompt=PromptParts("f", "v"))
        assert transport.calls[0]["model"] == "gpt-5-nano"
        assert transport.calls[1]["model"] == "gpt-5-mini"

    @pytest.mark.anyio
    async def test_config_overrides_role_model(self, conn: sqlite3.Connection) -> None:
        transport = FakeTransport()
        config = Config(models=ModelsConfig(mate="gpt-5-nano"))
        client = make_client(conn, transport, config=config)
        await client.call(role="mate", prompt=PromptParts("f", "v"))
        assert transport.calls[0]["model"] == "gpt-5-nano"

    @pytest.mark.anyio
    async def test_unknown_role_rejected(self, conn: sqlite3.Connection) -> None:
        client = make_client(conn, FakeTransport())
        with pytest.raises(LLMCallError, match="unknown role"):
            await client.call(role="gpt-5-nano", prompt=PromptParts("f", "v"))


class TestUsageRecording:
    @pytest.mark.anyio
    async def test_success_writes_one_usage_row(self, conn: sqlite3.Connection) -> None:
        transport = FakeTransport(
            [TransportReply(text="hi", input_tokens=612, output_tokens=48)]
        )
        client = make_client(conn, transport)
        await client.call(
            role="background", prompt=PromptParts("f", "v"), purpose="decide"
        )
        rows = conn.execute(
            "SELECT model, role, purpose, input_tokens, output_tokens,"
            " est_cost_usd, status FROM usage_log"
        ).fetchall()
        assert len(rows) == 1
        model, role, purpose, input_tokens, output_tokens, cost, status = rows[0]
        assert (model, role, purpose) == ("gpt-5-nano", "background", "decide")
        assert (input_tokens, output_tokens) == (612, 48)
        assert cost > 0
        assert status == "ok"

    @pytest.mark.anyio
    async def test_spend_accumulates_into_budget(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport(
            [TransportReply(text="x", input_tokens=10_000_000, output_tokens=1_000_000)]
        )
        client = make_client(conn, transport)
        await client.call(role="mate", prompt=PromptParts("f", "v"))
        assert client.budget.low_power is True


class TestHardCapRefusal:
    @pytest.mark.anyio
    async def test_hard_cap_refuses_before_dispatch(
        self, conn: sqlite3.Connection
    ) -> None:
        pauses: list[str] = []
        transport = FakeTransport()
        client = make_client(conn, transport)
        client.budget.on_hard_cap = lambda: pauses.append("pause")
        conn.execute(
            "INSERT INTO usage_log (ts_real, model, role, purpose, input_tokens,"
            " cached_tokens, output_tokens, est_cost_usd, status)"
            " VALUES (strftime('%s','now'), 'gpt-5-nano', 'background', 'seed',"
            " 0, 0, 0, 2.5, 'ok')"
        )
        conn.commit()
        with pytest.raises(BudgetExceededError):
            await client.call(role="background", prompt=PromptParts("f", "v"))
        assert transport.calls == []
        assert pauses == ["pause"]
        assert client.budget.notice_active is True


class TestRetries:
    @pytest.mark.anyio
    async def test_transient_errors_backoff_1_2_4_then_raise(
        self, conn: sqlite3.Connection
    ) -> None:
        sleeps: list[float] = []
        transport = FakeTransport(
            [
                TransportUnavailableError("boom"),
                TransportUnavailableError("boom"),
                TransportUnavailableError("boom"),
                TransportUnavailableError("boom"),
            ]
        )
        client = make_client(conn, transport, sleeps=sleeps)
        with pytest.raises(LLMCallError):
            await client.call(role="background", prompt=PromptParts("f", "v"))
        assert sleeps == [1, 2, 4]
        assert len(transport.calls) == 4

    @pytest.mark.anyio
    async def test_transient_error_then_success_recovers(
        self, conn: sqlite3.Connection
    ) -> None:
        sleeps: list[float] = []
        transport = FakeTransport(
            [TransportUnavailableError("blip"), TransportReply(text="ok")]
        )
        client = make_client(conn, transport, sleeps=sleeps)
        result = await client.call(role="background", prompt=PromptParts("f", "v"))
        assert result.text == "ok"
        assert sleeps == [1]

    @pytest.mark.anyio
    async def test_rate_limit_skips_without_retry(
        self, conn: sqlite3.Connection
    ) -> None:
        sleeps: list[float] = []
        transport = FakeTransport([TransportRateLimitedError("429")])
        client = make_client(conn, transport, sleeps=sleeps)
        result = await client.call(role="mate", prompt=PromptParts("f", "v"))
        assert result.skipped is True
        assert sleeps == []
        assert len(transport.calls) == 1
        status = conn.execute("SELECT status FROM usage_log").fetchone()[0]
        assert status == "skipped_rate_limit"


class TestStructuredOutputs:
    @pytest.mark.anyio
    async def test_schema_call_sends_strict_schema(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport([TransportReply(text='{"mood": "calm", "score": 3}')])
        client = make_client(conn, transport)
        result = await client.call(
            role="background", prompt=PromptParts("f", "v"), schema=Verdict
        )
        sent_schema = transport.calls[0]["schema"]
        assert isinstance(sent_schema, dict)
        assert sent_schema["strict"] is True
        assert result.parsed == Verdict(mood="calm", score=3)

    @pytest.mark.anyio
    async def test_schema_violation_retries_once_then_skips(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport(
            [TransportReply(text="not json"), TransportReply(text='{"bad": 1}')]
        )
        client = make_client(conn, transport)
        result = await client.call(
            role="background", prompt=PromptParts("f", "v"), schema=Verdict
        )
        assert len(transport.calls) == 2
        assert result.skipped is True
        assert result.parsed is None

    @pytest.mark.anyio
    async def test_schema_violation_then_valid_retry_parses(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport(
            [
                TransportReply(text="garbage"),
                TransportReply(text='{"mood": "bright", "score": 8}'),
            ]
        )
        client = make_client(conn, transport)
        result = await client.call(
            role="background", prompt=PromptParts("f", "v"), schema=Verdict
        )
        assert result.parsed == Verdict(mood="bright", score=8)


class TestToolCalling:
    @pytest.mark.anyio
    async def test_tools_included_in_request(self, conn: sqlite3.Connection) -> None:
        transport = FakeTransport([TransportReply(text="done")])
        client = make_client(conn, transport)
        tools = [{"type": "web_search"}]

        result = await client.call_with_tools(
            role="mate", prompt=PromptParts("f", "v"), tools=tools
        )

        assert transport.calls[0]["tools"] == tools
        assert result.text == "done"
        assert result.tool_calls == []

    @pytest.mark.anyio
    async def test_tool_calls_propagate_to_result(
        self, conn: sqlite3.Connection
    ) -> None:
        call = ToolCall(id="call_1", name="create", arguments={"title": "Tides"})
        transport = FakeTransport([TransportReply(text="", tool_calls=[call])])
        client = make_client(conn, transport)

        result = await client.call_with_tools(
            role="mate",
            prompt=PromptParts("f", "v"),
            tools=[{"type": "function", "name": "create"}],
        )

        assert result.tool_calls == [call]

    @pytest.mark.anyio
    async def test_no_tools_call_omits_tools_key(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport([TransportReply(text="ok")])
        client = make_client(conn, transport)

        await client.call(role="background", prompt=PromptParts("f", "v"))

        assert transport.calls[0]["tools"] is None


class TestPromptDiscipline:
    @pytest.mark.anyio
    async def test_outgoing_prompt_places_fixed_first(
        self, conn: sqlite3.Connection
    ) -> None:
        transport = FakeTransport()
        client = make_client(conn, transport)
        fixed = build_fixed_prefix("I am Beacon.", "en")
        await client.call(role="background", prompt=PromptParts(fixed, "now: morning"))
        prompt = transport.calls[0]["prompt"]
        assert isinstance(prompt, str)
        assert prompt.startswith(fixed)
        assert prompt.endswith("now: morning")
