"""The single LLM gateway (architecture.md §5).

Everything network-bound goes through `LLMClient.call()`: model-role
resolution from config (never literal model names at call sites), budget
gating, prompt-discipline assembly, retries with exponential backoff,
rate-limit skips, Structured Outputs validation with one re-ask, and
usage/cost recording. The `Transport` protocol is the only network
touchpoint; tests inject a fake (architecture.md §6).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ValidationError

from peerport.db import UsageRecord, insert_usage
from peerport.errors import LLMCallError
from peerport.llm.prompts import assemble_prompt

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Awaitable, Callable

    from peerport.config import Config
    from peerport.llm.budget import BudgetGuard
    from peerport.llm.outage import OutageTracker

logger = logging.getLogger(__name__)

ROLES = ("background", "mate")
BACKOFF_DELAYS: tuple[float, ...] = (1, 2, 4)
DEFAULT_MAX_OUTPUT_TOKENS = 250

# USD per 1M tokens (input, cached input, output). Config-resolved model
# names map here; unknown models cost $0 so the guard fails safe-open.
PRICING_PER_MTOK: dict[str, tuple[float, float, float]] = {
    "gpt-5-nano": (0.05, 0.005, 0.40),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "text-embedding-3-small": (0.02, 0.02, 0.0),
}


class TransportUnavailableError(Exception):
    """Transient transport failure (5xx, timeout); retried with backoff."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Store the failure message and, when known, its HTTP status.

        Args:
            message: Human-readable failure detail.
            status: The failing call's HTTP status code, when the
                failure came from a real HTTP response; `None` for
                connection/timeout failures with no status (fed to the
                outage tracker's `state.fog.detail` line by #27).
        """
        super().__init__(message)
        self.status = status


class TransportRateLimitedError(Exception):
    """HTTP 429; the call is skipped without retry (requirements §4.9)."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One function-tool invocation the model requested (architecture.md §5)."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class TransportReply:
    """Raw model output plus token accounting from one API call."""

    text: str
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)


class Transport(Protocol):
    """The one network boundary: a single completion call."""

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        schema: dict[str, Any] | None,
        max_output_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> TransportReply:
        """Send one request; raise the Transport errors on failure."""
        ...


@dataclass(slots=True)
class LLMResult:
    """Outcome of a gateway call."""

    text: str | None = None
    parsed: BaseModel | None = None
    skipped: bool = False
    reason: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PromptParts:
    """The two prompt segments: cache-stable fixed prefix, variable tail."""

    fixed: str
    variable: str


@dataclass(frozen=True, slots=True)
class _Dispatch:
    """Everything needed to (re)send one concrete request."""

    model: str
    role: str
    purpose: str
    prompt: str
    schema_payload: dict[str, Any] | None
    max_output_tokens: int
    tools: list[dict[str, Any]] | None = None


def estimate_cost_usd(
    model: str, input_tokens: int, cached_tokens: int, output_tokens: int
) -> float:
    """Estimate a call's cost from the pricing table."""
    input_rate, cached_rate, output_rate = PRICING_PER_MTOK.get(model, (0.0, 0.0, 0.0))
    fresh_tokens = max(input_tokens - cached_tokens, 0)
    return (
        fresh_tokens * input_rate
        + cached_tokens * cached_rate
        + output_tokens * output_rate
    ) / 1_000_000


def _enforce_strict_object_nodes(node: dict[str, Any]) -> None:
    """Recursively force `additionalProperties: false` + full `required`.

    OpenAI's strict Structured Outputs mode requires every object node
    in the schema to forbid extra properties and list every one of its
    properties as required (nullable fields opt out via `anyOf` null,
    not by omission) -- and that applies to nested models placed under
    `$defs`, not just the schema's top-level object.
    """
    if node.get("type") == "object" and "properties" in node:
        node["additionalProperties"] = False
        node["required"] = list(node["properties"].keys())
    for value in node.values():
        if isinstance(value, dict):
            _enforce_strict_object_nodes(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _enforce_strict_object_nodes(item)


def strict_schema_for(model_cls: type[BaseModel]) -> dict[str, Any]:
    """Build the strict Structured Outputs schema payload for a model."""
    json_schema = model_cls.model_json_schema()
    _enforce_strict_object_nodes(json_schema)
    return {"name": model_cls.__name__, "strict": True, "schema": json_schema}


class LLMClient:
    """Role-addressed, budget-gated gateway over a `Transport`."""

    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        budget: BudgetGuard,
        transport: Transport,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Wire the gateway.

        Args:
            config: Resolved app config (model role mapping).
            conn: DB connection for usage recording.
            budget: The daily budget guard consulted before dispatch.
            transport: The network boundary implementation.
            sleep: Awaitable delay, injectable for backoff tests.
        """
        self._config = config
        self._conn = conn
        self.budget = budget
        self._transport = transport
        self._sleep = sleep
        # Unset by default (keeps this constructor at 5 params); boot
        # wiring assigns a shared `OutageTracker` afterward so every call
        # site here can report success/failure without a network fake
        # having to know about it (#27).
        self.outage: OutageTracker | None = None

    def resolve_model(self, role: str) -> str:
        """Map a role identifier to the configured model name.

        Raises:
            LLMCallError: For any role other than `background`/`mate`.
        """
        if role == "background":
            return self._config.models.background
        if role == "mate":
            return self._config.models.mate
        message = f"unknown role: {role!r} (expected one of {ROLES})"
        raise LLMCallError(message)

    async def call(
        self,
        *,
        role: str,
        prompt: PromptParts,
        schema: type[BaseModel] | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        purpose: str = "",
    ) -> LLMResult:
        """Run one gated, retried, usage-recorded LLM call.

        Raises:
            BudgetExceededError: When the daily hard cap refuses dispatch
                (no usage row is written; nothing was spent).
            LLMCallError: After transient failures exhaust the backoff
                budget, or for an unknown role.
        """
        model = self.resolve_model(role)
        self.budget.check_hard_cap()
        dispatch = _Dispatch(
            model=model,
            role=role,
            purpose=purpose,
            prompt=assemble_prompt(prompt.fixed, prompt.variable),
            schema_payload=strict_schema_for(schema) if schema is not None else None,
            max_output_tokens=max_output_tokens,
        )

        reply = await self._attempt_with_backoff(dispatch)
        if reply is None:
            return LLMResult(skipped=True, reason="rate_limited")

        if schema is None:
            self.record_usage(dispatch, reply, "ok")
            return LLMResult(text=reply.text)
        return await self._validate_with_reask(schema, reply, dispatch)

    async def call_with_tools(
        self,
        *,
        role: str,
        prompt: PromptParts,
        tools: list[dict[str, Any]],
        purpose: str = "",
    ) -> LLMResult:
        """Run one gated, retried, usage-recorded call offering tool use.

        No Structured Outputs schema is applied. Inspect
        `result.tool_calls` for any function-tool the model chose to
        invoke, or `result.text` for a plain reply (e.g. after a hosted
        tool like `web_search` already resolved server-side).

        Raises:
            BudgetExceededError: When the daily hard cap refuses dispatch.
            LLMCallError: After transient failures exhaust the backoff
                budget, or for an unknown role.
        """
        model = self.resolve_model(role)
        self.budget.check_hard_cap()
        dispatch = _Dispatch(
            model=model,
            role=role,
            purpose=purpose,
            prompt=assemble_prompt(prompt.fixed, prompt.variable),
            schema_payload=None,
            max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            tools=tools,
        )
        reply = await self._attempt_with_backoff(dispatch)
        if reply is None:
            return LLMResult(skipped=True, reason="rate_limited")
        self.record_usage(dispatch, reply, "ok")
        return LLMResult(text=reply.text, tool_calls=reply.tool_calls)

    async def _attempt_with_backoff(self, dispatch: _Dispatch) -> TransportReply | None:
        """Dispatch with 1s/2s/4s backoff; `None` means rate-limit skip.

        Reports each outcome to `self.outage`, when wired: a rate limit or
        a final (retries-exhausted) failure counts as one failed dispatch;
        any reply that comes back at all counts as a success (#27).
        """
        last_error: Exception | None = None
        for attempt in range(len(BACKOFF_DELAYS) + 1):
            try:
                reply = await self._transport.complete(
                    model=dispatch.model,
                    prompt=dispatch.prompt,
                    tools=dispatch.tools,
                    schema=dispatch.schema_payload,
                    max_output_tokens=dispatch.max_output_tokens,
                )
            except TransportRateLimitedError:
                logger.warning(
                    "rate limited; skipping %s call (%s)",
                    dispatch.role,
                    dispatch.purpose,
                )
                self.record_usage(
                    dispatch, TransportReply(text=""), "skipped_rate_limit"
                )
                self.note_outage_failure(status=429)
                return None
            except TransportUnavailableError as error:
                last_error = error
                if attempt < len(BACKOFF_DELAYS):
                    await self._sleep(BACKOFF_DELAYS[attempt])
            else:
                self.note_outage_success()
                return reply
        self.record_usage(dispatch, TransportReply(text=""), "failed")
        self.note_outage_failure(status=getattr(last_error, "status", None))
        message = f"LLM call failed after {len(BACKOFF_DELAYS)} retries: {last_error}"
        raise LLMCallError(message) from last_error

    def note_outage_success(self) -> None:
        """Report a successful dispatch to the outage tracker, if wired.

        Public (like `record_usage`) so the module-level `call_stream`
        driver can report outcomes too, without reaching into a private
        member from outside the class.
        """
        if self.outage is not None:
            self.outage.report_success()

    def note_outage_failure(self, status: int | None) -> None:
        """Report a failed dispatch to the outage tracker, if wired."""
        if self.outage is not None:
            self.outage.report_failure(status)

    async def _validate_with_reask(
        self,
        schema: type[BaseModel],
        reply: TransportReply,
        dispatch: _Dispatch,
    ) -> LLMResult:
        """Validate Structured Outputs; re-ask once, then skip (§5.2)."""
        parsed = self._try_parse(schema, reply, dispatch)
        if parsed is not None:
            return LLMResult(text=reply.text, parsed=parsed)
        retry_reply = await self._attempt_with_backoff(dispatch)
        if retry_reply is None:
            return LLMResult(skipped=True, reason="rate_limited")
        parsed = self._try_parse(schema, retry_reply, dispatch)
        if parsed is not None:
            return LLMResult(text=retry_reply.text, parsed=parsed)
        logger.warning(
            "schema validation failed twice; skipping %s call (%s)",
            dispatch.role,
            dispatch.purpose,
        )
        return LLMResult(skipped=True, reason="schema_invalid")

    def _try_parse(
        self,
        schema: type[BaseModel],
        reply: TransportReply,
        dispatch: _Dispatch,
    ) -> BaseModel | None:
        try:
            parsed = schema.model_validate_json(reply.text)
        except ValidationError:
            self.record_usage(dispatch, reply, "schema_invalid")
            return None
        self.record_usage(dispatch, reply, "ok")
        return parsed

    def transport_for_streaming(self) -> StreamingTransport:
        """Return the transport, asserting it supports streaming.

        Raises:
            LLMCallError: If the wired transport cannot stream.
        """
        if not hasattr(self._transport, "stream_complete"):
            message = "transport does not support streaming"
            raise LLMCallError(message)
        return self._transport  # type: ignore[return-value]

    def record_usage(
        self, dispatch: _Dispatch, reply: TransportReply, status: str
    ) -> None:
        """Write one `usage_log` row for a completed or skipped call."""
        insert_usage(
            self._conn,
            UsageRecord(
                model=dispatch.model,
                role=dispatch.role,
                purpose=dispatch.purpose,
                input_tokens=reply.input_tokens,
                cached_tokens=reply.cached_tokens,
                output_tokens=reply.output_tokens,
                est_cost_usd=estimate_cost_usd(
                    dispatch.model,
                    reply.input_tokens,
                    reply.cached_tokens,
                    reply.output_tokens,
                ),
                status=status,
            ),
        )


def _extract_tool_calls(
    response: Any,
) -> list[ToolCall]:  # pragma: no cover - network boundary
    """Parse function-tool calls out of one Responses API result."""
    calls: list[ToolCall] = []
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "function_call":
            continue
        try:
            arguments = json.loads(item.arguments)
        except (TypeError, ValueError):
            logger.warning(
                "malformed tool-call JSON for %s (call_id=%s): %r",
                item.name,
                item.call_id,
                item.arguments,
            )
            arguments = {}
        calls.append(ToolCall(id=item.call_id, name=item.name, arguments=arguments))
    return calls


class OpenAITransport:  # pragma: no cover - the real network boundary
    """Responses API transport; constructed lazily so tests never touch it."""

    def __init__(self) -> None:
        """Create the SDK client (reads OPENAI_API_KEY from the env)."""
        from openai import AsyncOpenAI  # noqa: PLC0415 - heavy import kept lazy

        self._client = AsyncOpenAI()

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        schema: dict[str, Any] | None,
        max_output_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> TransportReply:
        """Send one Responses API request, mapping SDK errors to ours."""
        import openai  # noqa: PLC0415 - heavy import kept lazy

        kwargs: dict[str, Any] = {
            "model": model,
            "input": prompt,
            "max_output_tokens": max_output_tokens,
        }
        if schema is not None:
            kwargs["text"] = {"format": {"type": "json_schema", **schema}}
        if tools is not None:
            kwargs["tools"] = tools
        try:
            response = await self._client.responses.create(**kwargs)
        except openai.RateLimitError as error:
            raise TransportRateLimitedError(str(error)) from error
        except (openai.APIError, openai.APITimeoutError) as error:
            status = getattr(error, "status_code", None)
            raise TransportUnavailableError(str(error), status=status) from error
        usage = response.usage
        cached = (
            usage.input_tokens_details.cached_tokens
            if usage is not None and usage.input_tokens_details is not None
            else 0
        )
        return TransportReply(
            text=response.output_text,
            input_tokens=usage.input_tokens if usage is not None else 0,
            cached_tokens=cached,
            output_tokens=usage.output_tokens if usage is not None else 0,
            tool_calls=_extract_tool_calls(response),
        )


class StreamingTransport(Protocol):
    """Optional streaming variant of the network boundary (Mate chat)."""

    async def stream_complete(
        self,
        *,
        model: str,
        prompt: str,
        max_output_tokens: int,
        on_delta: Callable[[str], Awaitable[None]],
        tools: list[dict[str, Any]] | None = None,
    ) -> TransportReply:
        """Stream one request, invoking *on_delta* per token chunk."""
        ...


DEFAULT_CHAT_MAX_OUTPUT_TOKENS = 1000

# Every Mate chat turn offers the hosted web_search tool so the model can
# judge search necessity itself (requirements.md §4.5 REQ-001) — the sole
# caller of `call_stream` is Mate chat, so this is unconditional rather
# than a new parameter every future caller would have to thread through.
MATE_TOOLS: list[dict[str, Any]] = [{"type": "web_search"}]


async def call_stream(
    client: LLMClient,
    *,
    role: str,
    prompt: PromptParts,
    on_delta: Callable[[str], Awaitable[None]],
    purpose: str = "chat",
) -> LLMResult:
    """Run one budget-gated streaming call through the gateway.

    Single attempt (no backoff — a broken stream mid-way cannot be
    transparently retried); rate limits and transient failures degrade
    to a skipped result rather than raising.
    """
    model = client.resolve_model(role)
    client.budget.check_hard_cap()
    transport = client.transport_for_streaming()
    dispatch = _Dispatch(
        model=model,
        role=role,
        purpose=purpose,
        prompt=assemble_prompt(prompt.fixed, prompt.variable),
        schema_payload=None,
        max_output_tokens=DEFAULT_CHAT_MAX_OUTPUT_TOKENS,
        tools=MATE_TOOLS if role == "mate" else None,
    )
    try:
        reply = await transport.stream_complete(
            model=model,
            prompt=dispatch.prompt,
            max_output_tokens=dispatch.max_output_tokens,
            on_delta=on_delta,
            tools=dispatch.tools,
        )
    except TransportRateLimitedError:
        client.record_usage(dispatch, TransportReply(text=""), "skipped_rate_limit")
        client.note_outage_failure(status=429)
        return LLMResult(skipped=True, reason="rate_limited")
    except TransportUnavailableError as error:
        client.record_usage(dispatch, TransportReply(text=""), "failed")
        client.note_outage_failure(status=getattr(error, "status", None))
        return LLMResult(skipped=True, reason="unavailable")
    client.record_usage(dispatch, reply, "ok")
    client.note_outage_success()
    return LLMResult(text=reply.text)


class OpenAIStreamingTransport(OpenAITransport):  # pragma: no cover - network boundary
    """Adds Responses API streaming for the Mate chat path."""

    async def stream_complete(
        self,
        *,
        model: str,
        prompt: str,
        max_output_tokens: int,
        on_delta: Callable[[str], Awaitable[None]],
        tools: list[dict[str, Any]] | None = None,
    ) -> TransportReply:
        """Stream one request, forwarding output-text deltas."""
        import openai  # noqa: PLC0415 - heavy import kept lazy

        kwargs: dict[str, Any] = {
            "model": model,
            "input": prompt,
            "max_output_tokens": max_output_tokens,
            "stream": True,
        }
        if tools is not None:
            kwargs["tools"] = tools
        try:
            stream = await self._client.responses.create(**kwargs)
            text_parts: list[str] = []
            usage_in = usage_cached = usage_out = 0
            async for event in stream:
                if event.type == "response.output_text.delta":
                    text_parts.append(event.delta)
                    await on_delta(event.delta)
                elif event.type == "response.completed":
                    usage = event.response.usage
                    if usage is not None:
                        usage_in = usage.input_tokens
                        usage_out = usage.output_tokens
                        if usage.input_tokens_details is not None:
                            usage_cached = usage.input_tokens_details.cached_tokens
        except openai.RateLimitError as error:
            raise TransportRateLimitedError(str(error)) from error
        except (openai.APIError, openai.APITimeoutError) as error:
            status = getattr(error, "status_code", None)
            raise TransportUnavailableError(str(error), status=status) from error
        return TransportReply(
            text="".join(text_parts),
            input_tokens=usage_in,
            cached_tokens=usage_cached,
            output_tokens=usage_out,
        )
