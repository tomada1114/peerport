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
import logging
from dataclasses import dataclass
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


class TransportRateLimitedError(Exception):
    """HTTP 429; the call is skipped without retry (requirements §4.9)."""


@dataclass(slots=True)
class TransportReply:
    """Raw model output plus token accounting from one API call."""

    text: str
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0


class Transport(Protocol):
    """The one network boundary: a single completion call."""

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        schema: dict[str, Any] | None,
        max_output_tokens: int,
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


def strict_schema_for(model_cls: type[BaseModel]) -> dict[str, Any]:
    """Build the strict Structured Outputs schema payload for a model."""
    json_schema = model_cls.model_json_schema()
    json_schema["additionalProperties"] = False
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
            self._record(dispatch, reply, status="ok")
            return LLMResult(text=reply.text)
        return await self._validate_with_reask(schema, reply, dispatch)

    async def _attempt_with_backoff(self, dispatch: _Dispatch) -> TransportReply | None:
        """Dispatch with 1s/2s/4s backoff; `None` means rate-limit skip."""
        last_error: Exception | None = None
        for attempt in range(len(BACKOFF_DELAYS) + 1):
            try:
                return await self._transport.complete(
                    model=dispatch.model,
                    prompt=dispatch.prompt,
                    schema=dispatch.schema_payload,
                    max_output_tokens=dispatch.max_output_tokens,
                )
            except TransportRateLimitedError:
                logger.warning(
                    "rate limited; skipping %s call (%s)",
                    dispatch.role,
                    dispatch.purpose,
                )
                self._record(
                    dispatch, TransportReply(text=""), status="skipped_rate_limit"
                )
                return None
            except TransportUnavailableError as error:
                last_error = error
                if attempt < len(BACKOFF_DELAYS):
                    await self._sleep(BACKOFF_DELAYS[attempt])
        self._record(dispatch, TransportReply(text=""), status="failed")
        message = f"LLM call failed after {len(BACKOFF_DELAYS)} retries: {last_error}"
        raise LLMCallError(message) from last_error

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
            self._record(dispatch, reply, status="schema_invalid")
            return None
        self._record(dispatch, reply, status="ok")
        return parsed

    def _record(self, dispatch: _Dispatch, reply: TransportReply, status: str) -> None:
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
        try:
            response = await self._client.responses.create(**kwargs)
        except openai.RateLimitError as error:
            raise TransportRateLimitedError(str(error)) from error
        except (openai.APIError, openai.APITimeoutError) as error:
            raise TransportUnavailableError(str(error)) from error
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
        )
