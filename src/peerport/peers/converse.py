"""Peer↔peer conversations and relationship updates (#20, requirements §4.2).

Eligibility is pure rule-based (Chebyshev distance ≤ 2 tiles, target not
busy) — no LLM involved. Turns alternate, one background call each, up
to the budget guard's turn limit (6 normally, 4 in low power), ending
early once both peers' latest turns want to end. The full transcript
lands only in `events`; a single outcome call produces the 1-2 sentence
summary (written to both memories), the relationship delta, and the
refreshed label injected into the pair's next conversation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel

from peerport.db import (
    Relationship,
    get_relationship,
    insert_event,
    save_relationship,
)
from peerport.llm.client import PromptParts
from peerport.llm.prompts import ConversationTurn, build_fixed_prefix

if TYPE_CHECKING:
    from collections.abc import Mapping
    from sqlite3 import Connection

    from peerport.llm.client import LLMClient
    from peerport.memory.stream import MemoryStream
    from peerport.peers.personas import Persona
    from peerport.world.sim import Simulation

logger = logging.getLogger(__name__)

PROXIMITY_TILES = 2
SCORE_MIN = -100
SCORE_MAX = 100


class ConversationOutcome(BaseModel):
    """Summary + relationship judgement from one end-of-conversation call."""

    summary: str
    delta: int
    label: str


@dataclass(slots=True)
class ConversationEngine:
    """Runs bounded peer conversations and persists their outcomes."""

    llm: LLMClient
    sim: Simulation
    memory: MemoryStream
    broadcaster: object
    conn: Connection
    personas: Mapping[str, Persona]
    busy: set[str] = field(default_factory=set)

    def eligible(self, a: str, b: str) -> bool:
        """Rule-based `talk` eligibility: proximity ≤ 2 tiles, both free."""
        peer_a = self.sim.peers.get(a)
        peer_b = self.sim.peers.get(b)
        if peer_a is None or peer_b is None:
            return False
        if a in self.busy or b in self.busy:
            return False
        distance = max(
            abs(peer_a.tile[0] - peer_b.tile[0]),
            abs(peer_a.tile[1] - peer_b.tile[1]),
        )
        return distance <= PROXIMITY_TILES

    async def start(self, a: str, b: str) -> bool:
        """Run a full conversation between *a* and *b* if eligible.

        Returns False (with zero side effects — no session, no LLM call,
        no speech frame) when eligibility fails.
        """
        if not self.eligible(a, b):
            return False
        self.busy |= {a, b}
        try:
            turns = await self._run_turns(a, b)
            await self._finish(a, b, turns)
        finally:
            self.busy -= {a, b}
        return True

    async def _run_turns(self, a: str, b: str) -> list[tuple[str, str]]:
        turns: list[tuple[str, str]] = []
        wants_end = {a: False, b: False}
        relationship = get_relationship(self.conn, a, b)
        cap = self.llm.budget.conversation_turn_limit()
        for index in range(cap):
            speaker = a if index % 2 == 0 else b
            other = b if speaker == a else a
            transcript = "\n".join(f"{s}: {t}" for s, t in turns) or "(you speak first)"
            variable = (
                f"You are talking with {other}. Your relationship with them:"
                f' score {relationship.score} of 100, "{relationship.label}".\n'
                f"Conversation so far:\n{transcript}\n\n"
                "Say your next line. Set wants_to_end true once you are ready"
                " to wrap up."
            )
            result = await self.llm.call(
                role="background",
                prompt=PromptParts(
                    build_fixed_prefix(self.personas[speaker].body, "en"), variable
                ),
                schema=ConversationTurn,
                purpose="converse",
            )
            if result.parsed is None or not isinstance(result.parsed, ConversationTurn):
                logger.warning("conversation turn skipped; ending early")
                break
            turn = result.parsed
            turns.append((speaker, turn.text))
            await self.broadcaster.publish(  # type: ignore[attr-defined]
                {"t": "speech", "peer_id": speaker, "text": turn.text}
            )
            wants_end[speaker] = turn.wants_to_end
            if wants_end[a] and wants_end[b]:
                break
        return turns

    async def _finish(self, a: str, b: str, turns: list[tuple[str, str]]) -> None:
        if not turns:
            return
        now_world = self.sim.state.world_seconds
        insert_event(
            self.conn,
            ts_world=now_world,
            kind="conversation",
            actors=[a, b],
            payload=json.dumps(turns),
        )
        transcript = "\n".join(f"{s}: {t}" for s, t in turns)
        result = await self.llm.call(
            role="background",
            prompt=PromptParts(
                build_fixed_prefix(self.personas[a].body, "en"),
                "Summarize this conversation in 1-2 sentences, judge how it"
                " changed the relationship as an integer delta (-10..10), and"
                f" give a fresh short relationship label.\n\n{transcript}",
            ),
            schema=ConversationOutcome,
            purpose="summarize",
        )
        if result.parsed is None or not isinstance(result.parsed, ConversationOutcome):
            return
        outcome = result.parsed
        for peer_id in (a, b):
            await self.memory.write(
                peer_id=peer_id,
                ts_world=now_world,
                kind="conversation",
                text=outcome.summary,
            )
        previous = get_relationship(self.conn, a, b)
        save_relationship(
            self.conn,
            (a, b),
            Relationship(
                score=max(SCORE_MIN, min(SCORE_MAX, previous.score + outcome.delta)),
                label=outcome.label,
                last_delta=outcome.delta,
            ),
        )
