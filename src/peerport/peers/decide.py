"""Option-Action decision loop (#19, requirements §4.2).

Each map peer decides its next action at `activity_interval` ± 20%
jitter, or immediately on events (spoken to, new board post, Keeper
instruction) via `trigger_redecision`. Decisions go through the LLM
gateway as strict Structured Outputs with `max_output_tokens=250`, no
reasoning field, the last five actions as anti-repeat history, and a
hard server-side exclusion of any action chosen three times in a row.
Failures always degrade to `rest` — a peer is never left undecided.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, create_model

from peerport.db import insert_board_post, list_board_posts
from peerport.errors import BudgetExceededError, LLMCallError
from peerport.llm.budget import LOW_POWER_ACTIVITY_BOUNDS
from peerport.llm.client import PromptParts
from peerport.llm.prompts import ActionDecision, build_fixed_prefix

if TYPE_CHECKING:
    import random
    from collections.abc import Awaitable, Callable, Mapping
    from sqlite3 import Connection
    from typing import Protocol

    from peerport.memory.stream import MemoryStream

    class Publisher(Protocol):
        """Anything with an async publish(frame) method."""

        async def publish(self, message: dict[str, object]) -> None:
            """Fan a frame out to connected clients."""
            ...

    from peerport.llm.client import LLMClient
    from peerport.peers.personas import Persona
    from peerport.world.sim import Simulation

logger = logging.getLogger(__name__)

ACTIONS = ("move", "talk", "post_board", "read_board", "rest", "emote")
JITTER_MIN = 0.8
JITTER_MAX = 1.2
HISTORY_WINDOW = 5
REPEAT_EXCLUSION_RUN = 3
DECISION_MAX_OUTPUT_TOKENS = 250
FALLBACK_ACTION = ActionDecision(action="rest", mood="neutral")

DECIDE_INSTRUCTIONS = (
    "Decide your next action. Avoid repeating your recent actions; vary "
    "your day. Choose a waypoint node for move targets, a peer id for "
    "talk targets, and write board posts in your own voice."
)


@lru_cache(maxsize=len(ACTIONS) + 1)
def action_schema_excluding(excluded: str | None) -> type[BaseModel]:
    """Build the ActionDecision schema, optionally without one action.

    The exclusion is a hard schema-level filter (requirements §4.2), not
    a prompt hint — the model cannot pick the excluded action at all.
    """
    if excluded is None:
        return ActionDecision
    allowed = tuple(a for a in ACTIONS if a != excluded)
    return create_model(
        f"ActionDecisionNo{excluded.title().replace('_', '')}",
        action=(Literal[allowed], ...),
        target=(str | None, None),
        content=(str | None, None),
        mood=(str, ...),
    )


@dataclass(slots=True)
class DecisionEngine:
    """Runs the per-peer decision cycle and routes decided actions."""

    llm: LLMClient
    sim: Simulation
    personas: Mapping[str, Persona]
    rng: random.Random
    on_talk: Callable[[str, str], Awaitable[None]] | None = None
    on_post_board: Callable[[str, str], Awaitable[None]] | None = None
    on_read_board: Callable[[str], Awaitable[None]] | None = None
    hearsay_provider: Callable[[str], str | None] | None = None
    history: dict[str, deque[ActionDecision]] = field(
        default_factory=lambda: defaultdict(lambda: deque(maxlen=32))
    )

    def next_interval(self, peer_id: str) -> float:
        """Seconds until the peer's next scheduled decision (base ± 20%).

        Doubled when the budget guard's soft cap has engaged low-power
        mode (requirements §4.9), by consulting
        `BudgetGuard.activity_interval_bounds()` rather than assuming a
        fixed multiplier.
        """
        base = self.personas[peer_id].activity_interval or 90
        low, _high = self.llm.budget.activity_interval_bounds()
        if low == LOW_POWER_ACTIVITY_BOUNDS[0]:
            base *= 2
        return base * self.rng.uniform(JITTER_MIN, JITTER_MAX)

    def record_action(self, peer_id: str, decision: ActionDecision) -> None:
        """Append a decided action to the peer's history."""
        self.history[peer_id].append(decision)

    def last_mood(self, peer_id: str) -> str | None:
        """The peer's most recent action mood (popup data, #20)."""
        tail = self.history[peer_id]
        return tail[-1].mood if tail else None

    def _excluded_action(self, peer_id: str) -> str | None:
        tail = list(self.history[peer_id])[-REPEAT_EXCLUSION_RUN:]
        if len(tail) == REPEAT_EXCLUSION_RUN and len({d.action for d in tail}) == 1:
            return tail[0].action
        return None

    async def decide(self, peer_id: str) -> ActionDecision:
        """Run one decision for a peer; always resolves to some action."""
        persona = self.personas[peer_id]
        schema = action_schema_excluding(self._excluded_action(peer_id))
        recent = list(self.history[peer_id])[-HISTORY_WINDOW:]
        history_lines = (
            "\n".join(f"- {d.action} (mood: {d.mood})" for d in recent)
            or "- (no recent actions)"
        )
        peer = self.sim.peers.get(peer_id)
        situation = (
            f"You are at tile {peer.tile}." if peer is not None else "You are away."
        )
        variable = (
            f"{DECIDE_INSTRUCTIONS}\n\nYour last actions:\n{history_lines}\n\n"
            f"{situation}\nWaypoints: {', '.join(sorted(self.sim.worldmap.nodes))}\n"
            f"Peers in town: {', '.join(sorted(self.sim.peers))}"
        )
        hearsay = self.hearsay_provider(peer_id) if self.hearsay_provider else None
        if hearsay:
            variable = f"{variable}\n\n{hearsay}"
        try:
            result = await self.llm.call(
                role="background",
                prompt=PromptParts(build_fixed_prefix(persona.body, "en"), variable),
                schema=schema,
                max_output_tokens=DECISION_MAX_OUTPUT_TOKENS,
                purpose="decide",
            )
        except (LLMCallError, BudgetExceededError):
            logger.exception(
                "decision call failed for %s; falling back to rest", peer_id
            )
            result = None
        decision = FALLBACK_ACTION
        if result is not None and result.parsed is not None:
            decision = ActionDecision.model_validate(result.parsed.model_dump())
        elif result is not None and result.skipped:
            logger.warning(
                "decision skipped for %s (%s); falling back to rest",
                peer_id,
                result.reason,
            )
        self.record_action(peer_id, decision)
        await self._route(peer_id, decision)
        return decision

    async def trigger_redecision(self, peer_ids: list[str] | None = None) -> None:
        """Immediate re-decision for the given peers (or every map peer).

        Invoked on events: spoken to (#20), new board post (#21), Keeper
        instruction — bypassing the normal timers.
        """
        for peer_id in peer_ids or list(self.sim.peers):
            await self.decide(peer_id)

    async def run_peer(self, peer_id: str) -> None:  # pragma: no cover - async driver
        """Scheduler loop for one peer (thin driver over `decide`)."""
        import asyncio  # noqa: PLC0415 - driver-only dependency

        while True:
            await asyncio.sleep(self.next_interval(peer_id))
            await self.decide(peer_id)

    async def _route(self, peer_id: str, decision: ActionDecision) -> None:
        if decision.action == "move":
            target = decision.target
            if target in self.sim.worldmap.nodes:
                self.sim.assign_destination(peer_id, target)
        elif decision.action == "rest":
            home = f"berth_{peer_id}"
            if home in self.sim.worldmap.nodes and peer_id in self.sim.peers:
                self.sim.assign_destination(peer_id, home)
        elif decision.action == "talk":
            if self.on_talk is not None and decision.target:
                await self.on_talk(peer_id, decision.target)
        elif decision.action == "post_board":
            if self.on_post_board is not None and decision.content:
                await self.on_post_board(peer_id, decision.content)
        elif decision.action == "read_board" and self.on_read_board is not None:
            await self.on_read_board(peer_id)
        # emote is handled locally: it is pure flavor, nothing to route.


READ_BOARD_WINDOW = 5


def make_board_hooks(
    conn: Connection,
    memory: MemoryStream,
    broadcaster: Publisher,
    now_world: Callable[[], int],
) -> tuple[Callable[[str, str], Awaitable[None]], Callable[[str], Awaitable[None]]]:
    """Build the post_board/read_board callbacks for the decision engine.

    read_board summaries are stored as `kind=observation`: requirements
    §4.3 fixes the memory kind enum and has no `board` kind (issue #21's
    `kind=board` diverges from the spec; requirements.md wins).
    """

    async def post_board(peer_id: str, content: str) -> None:
        insert_board_post(conn, author_id=peer_id, body=content, ts_world=now_world())
        await broadcaster.publish(
            {"t": "event", "kind": "board_post", "author": peer_id}
        )

    async def read_board(peer_id: str) -> None:
        posts = list_board_posts(conn, limit=READ_BOARD_WINDOW)
        if not posts:
            return
        listing = "; ".join(f"{p['author_id']}: {p['body']}" for p in posts)
        await memory.write(
            peer_id=peer_id,
            ts_world=now_world(),
            kind="observation",
            text=f"I read the Signal Tower board. {listing}",
        )

    return post_board, read_board
