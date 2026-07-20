"""Reflection and forgetting (#26, requirements §4.3).

Each peer reflects once it accumulates 50 unreflected memories, or once
the world enters the night light band for the first time that night -
whichever is due first (`maybe_reflect` runs at most one reflection per
call, so a tick where both conditions are eligible still only reflects
once). A reflection run reads the peer's still-unreflected memories,
asks the "background" role for 2-3 insights plus an explicit
persona-drift self-check, and stores each insight as a `kind=reflection`
memory (requirements.md §4.3 fixes the memory-kind enum; there is no
dedicated "summary" kind, so forgetting also writes `kind=reflection`).

Once a peer's memory table exceeds 2,000 rows, one forgetting evaluation
folds its oldest, lowest-importance 100-row cluster into a single
summary memory and deletes the originals - but only after that summary
write is confirmed, so a failed summarization call never loses data
(Summarize-and-Forget). Each evaluation removes at most one cluster;
if the peer is still over the threshold afterward, a later evaluation
removes the next cluster (requirements §4.3, REQ-014's "defer to next
tick" design decision).

There is no band-transition event (`WorldClock.band()` is a pure
function of world seconds), so both `maybe_reflect` and `forget_once`
are meant to be polled periodically for each peer - see
`run_reflection_loop`/`run_forgetting_loop` below, which follow the same
thin-driver pattern as `DecisionEngine.run_peer`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from peerport.db import get_world_state, set_world_state
from peerport.errors import LLMCallError
from peerport.llm.client import PromptParts
from peerport.llm.prompts import (
    WORLD_RULES,
    MemoryClusterSummary,
    ReflectionInsight,
    ReflectionInsights,
    build_fixed_prefix,
)
from peerport.memory.stream import clamp_importance

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping

    from peerport.llm.client import LLMClient
    from peerport.memory.stream import MemoryStream
    from peerport.peers.personas import Persona
    from peerport.world.clock import WorldClock

logger = logging.getLogger(__name__)

UNREFLECTED_THRESHOLD = 50
MIN_INSIGHTS = 2
MAX_INSIGHTS = 3
FORGET_THRESHOLD = 2000
FORGET_CLUSTER_SIZE = 100
NIGHT_BAND = "night"
LAST_REFLECTION_DAY_KEY_PREFIX = "last_reflection_day:"

REFLECTION_INSTRUCTIONS = (
    "Reflect on your memories listed below. Write 2 to 3 short, concrete "
    "insights - things you now understand or notice about yourself, "
    "other peers, or the town - grounded in these memories. Give each "
    "insight its own importance score from 1 (mundane) to 10 "
    "(life-changing)."
)
DRIFT_SELF_CHECK_INSTRUCTION = (
    "Separately from your insights, self-check: has your recent behavior "
    "drifted from the core persona described above? Answer that check in "
    "`drift_notes`, distinct from your insights."
)
FORGET_INSTRUCTIONS = (
    "The numbered memories below are your oldest, least important ones. "
    "Fold them into one short summary capturing anything still worth "
    "remembering, and give that summary its own importance score from 1 "
    "(mundane) to 10 (life-changing)."
)


@dataclass(slots=True)
class ReflectionEngine:
    """Runs per-peer reflection and Summarize-and-Forget evaluations."""

    llm: LLMClient
    conn: sqlite3.Connection
    memory: MemoryStream
    personas: Mapping[str, Persona]
    clock: WorldClock
    now_world: Callable[[], int]
    locale: str = "en"

    def unreflected_count(self, peer_id: str) -> int:
        """Count of *peer_id*'s memories written since its last reflection."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE peer_id = ? AND reflected = 0",
            (peer_id,),
        ).fetchone()
        return int(row[0])

    async def maybe_reflect(self, peer_id: str) -> bool:
        """Trigger reflection for *peer_id* if either condition is due.

        Returns:
            Whether a reflection run happened. At most one run happens
            per call even when the night-band and unreflected-count
            triggers are both eligible on the same tick (REQ-004).
        """
        band = self.clock.band(self.now_world())
        night_due = band == NIGHT_BAND and not self._reflected_tonight(peer_id)
        count_due = self.unreflected_count(peer_id) >= UNREFLECTED_THRESHOLD
        if not (night_due or count_due):
            return False
        await self._reflect(peer_id)
        return True

    def _reflected_tonight(self, peer_id: str) -> bool:
        """Whether *peer_id* already reflected during the current world-day."""
        stored = get_world_state(
            self.conn, f"{LAST_REFLECTION_DAY_KEY_PREFIX}{peer_id}"
        )
        return stored is not None and int(stored) == self.clock.day(self.now_world())

    def _mark_reflected_tonight(self, peer_id: str) -> None:
        """Persist the current world-day as *peer_id*'s last reflection day."""
        set_world_state(
            self.conn,
            f"{LAST_REFLECTION_DAY_KEY_PREFIX}{peer_id}",
            str(self.clock.day(self.now_world())),
        )

    async def _reflect(self, peer_id: str) -> list[ReflectionInsight]:
        """Run one reflection: generate insights, store them, reset state."""
        persona = self.personas[peer_id]
        pending = self._pending_memories(peer_id)
        prompt = self._reflection_prompt(persona, pending)
        insights = await self._request_insights(prompt)
        if len(insights) < MIN_INSIGHTS:
            insights = await self._request_insights(prompt)
        insights = insights[:MAX_INSIGHTS]
        new_ids = [
            await self.memory.write(
                peer_id=peer_id,
                ts_world=self.now_world(),
                kind="reflection",
                text=insight.text,
                importance=clamp_importance(insight.importance),
            )
            for insight in insights
        ]
        # The insights themselves are already the product of this
        # reflection - mark them reflected too so REQ-009's "resets to 0"
        # holds exactly, rather than the run immediately re-queuing its
        # own output for the next reflection.
        self._mark_ids_reflected([memory_id for memory_id, _ in pending] + new_ids)
        self._mark_reflected_tonight(peer_id)
        return insights

    def _pending_memories(self, peer_id: str) -> list[tuple[int, str]]:
        """The peer's memories written since its last reflection, oldest first."""
        return self.conn.execute(
            "SELECT id, text FROM memories"
            " WHERE peer_id = ? AND reflected = 0 ORDER BY id",
            (peer_id,),
        ).fetchall()

    def _reflection_prompt(
        self, persona: Persona, pending: list[tuple[int, str]]
    ) -> PromptParts:
        """Assemble the reflection prompt: persona (fixed) + memories + drift check."""
        listing = (
            "\n".join(f"{i + 1}. {text}" for i, (_, text) in enumerate(pending))
            or "(no memories yet)"
        )
        variable = (
            f"{REFLECTION_INSTRUCTIONS}\n\n{DRIFT_SELF_CHECK_INSTRUCTION}\n\n"
            f"Recent memories:\n{listing}"
        )
        return PromptParts(build_fixed_prefix(persona.body, self.locale), variable)

    async def _request_insights(self, prompt: PromptParts) -> list[ReflectionInsight]:
        """One background-role call for insights; empty list on skip/failure."""
        result = await self.llm.call(
            role="background",
            prompt=prompt,
            schema=ReflectionInsights,
            purpose="reflect",
        )
        if result.skipped or not isinstance(result.parsed, ReflectionInsights):
            return []
        return result.parsed.insights

    def _mark_ids_reflected(self, memory_ids: list[int]) -> None:
        """Flip `reflected` on exactly the given memory rows."""
        params = [(memory_id,) for memory_id in memory_ids]
        if not params:
            return
        with self.conn:
            self.conn.executemany(
                "UPDATE memories SET reflected = 1 WHERE id = ?", params
            )

    def _peer_memory_count(self, peer_id: str) -> int:
        """Total `memories` row count for *peer_id*, reflected or not."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE peer_id = ?", (peer_id,)
        ).fetchone()
        return int(row[0])

    def _forget_cluster(self, peer_id: str) -> list[tuple[int, str]]:
        """The oldest, lowest-importance `FORGET_CLUSTER_SIZE` rows for the peer."""
        return self.conn.execute(
            "SELECT id, text FROM memories WHERE peer_id = ?"
            " ORDER BY importance ASC, ts_world ASC LIMIT ?",
            (peer_id, FORGET_CLUSTER_SIZE),
        ).fetchall()

    async def forget_once(self, peer_id: str) -> bool:
        """Run one Summarize-and-Forget evaluation for *peer_id*.

        Folds the oldest, lowest-importance 100-row cluster into one
        `kind=reflection` summary memory and deletes the originals, but
        only once that summary write is confirmed - a failed or skipped
        summarization call leaves every original row untouched
        (requirements §4.3 REQ-013) so the evaluation can simply be
        retried on a later eligible tick.

        Returns:
            Whether a cluster was folded away this evaluation.
        """
        if self._peer_memory_count(peer_id) <= FORGET_THRESHOLD:
            return False
        cluster = self._forget_cluster(peer_id)
        if not cluster:
            return False
        summary = await self._summarize_cluster(peer_id, cluster)
        if summary is None:
            return False
        await self.memory.write(
            peer_id=peer_id,
            ts_world=self.now_world(),
            kind="reflection",
            text=summary.text,
            importance=clamp_importance(summary.importance),
        )
        ids = [(memory_id,) for memory_id, _ in cluster]
        with self.conn:
            self.conn.executemany("DELETE FROM memories WHERE id = ?", ids)
        return True

    async def _summarize_cluster(
        self, peer_id: str, cluster: list[tuple[int, str]]
    ) -> MemoryClusterSummary | None:
        """Call the background role to fold *cluster* into one summary.

        Returns:
            `None` when the call fails outright or is skipped/invalid,
            so the caller never deletes anything it cannot confirm.
        """
        persona = self.personas.get(peer_id)
        fixed = (
            build_fixed_prefix(persona.body, self.locale)
            if persona is not None
            else WORLD_RULES
        )
        listing = "\n".join(f"{i + 1}. {text}" for i, (_, text) in enumerate(cluster))
        try:
            result = await self.llm.call(
                role="background",
                prompt=PromptParts(fixed, f"{FORGET_INSTRUCTIONS}\n\n{listing}"),
                schema=MemoryClusterSummary,
                purpose="forget",
            )
        except LLMCallError:
            logger.exception(
                "forgetting summarization failed for %s; deferring", peer_id
            )
            return None
        if result.skipped or not isinstance(result.parsed, MemoryClusterSummary):
            logger.warning(
                "forgetting summarization skipped for %s; deferring", peer_id
            )
            return None
        return result.parsed


REFLECTION_CHECK_INTERVAL_SECONDS = 30
FORGETTING_CHECK_INTERVAL_SECONDS = 1800


async def run_reflection_loop(
    engine: ReflectionEngine, peer_ids: list[str]
) -> None:  # pragma: no cover - async driver
    """Periodically evaluate reflection triggers for every peer, forever."""
    import asyncio  # noqa: PLC0415 - driver-only dependency

    while True:
        await asyncio.sleep(REFLECTION_CHECK_INTERVAL_SECONDS)
        for peer_id in peer_ids:
            await engine.maybe_reflect(peer_id)


async def run_forgetting_loop(
    engine: ReflectionEngine, peer_ids: list[str]
) -> None:  # pragma: no cover - async driver
    """Periodically evaluate forgetting eligibility for every peer, forever."""
    import asyncio  # noqa: PLC0415 - driver-only dependency

    while True:
        await asyncio.sleep(FORGETTING_CHECK_INTERVAL_SECONDS)
        for peer_id in peer_ids:
            await engine.forget_once(peer_id)
