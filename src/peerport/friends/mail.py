"""Friend mail and hearsay (#23, requirements §4.6).

Friends (Kai paired with Tug, Mia paired with Bell) never appear on the
map; they exist only through mail and hearsay. Each friend's `mood`,
`recent_topics`, `last_letter_summary`, and `last_updated_world_day` are
the single source of truth for what they know, persisted as one JSON
blob per friend in `world_state` (key `friend_state:<friend_id>`).
Generation is eligible on three triggers - an in-world event involving
the paired peer, a Keeper reply, or `cadence_days` world-days since the
friend's last letter - capped at `SESSION_MAIL_CAP` letters per app
session (an in-memory counter that resets on restart by design).

Divergence: requirements.md §4.3 fixes the memory-kind enum with no
`mail` kind (same divergence rule as #21's board->observation and #22's
logbook mapping; see decisions.md D-G12). Mail exchanges write
`kind="conversation"` memories.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from peerport.db import (
    NewMail,
    get_mail,
    get_world_state,
    insert_mail,
    set_world_state,
)
from peerport.llm.client import PromptParts
from peerport.llm.prompts import WORLD_RULES, MailLetter, build_fixed_prefix

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping
    from typing import Protocol

    from peerport.llm.client import LLMClient
    from peerport.memory.stream import MemoryStream
    from peerport.peers.personas import Persona
    from peerport.world.clock import WorldClock

    class Publisher(Protocol):
        """Anything with an async publish(frame) method."""

        async def publish(self, message: dict[str, object]) -> None:
            """Fan a frame out to connected clients."""
            ...


logger = logging.getLogger(__name__)

SESSION_MAIL_CAP = 3
DEFAULT_CADENCE_DAYS = 3
HEARSAY_PREFIX = "my Keeper said"
FRIEND_STATE_KEY_PREFIX = "friend_state:"

GENERATE_INSTRUCTIONS = (
    "Write one short letter from yourself to your Keeper reacting to the "
    "trigger below, in your own voice. Also report your updated mood in "
    "one word, 1-3 recent topics on your mind, and a one-sentence summary "
    "of this letter for your own records."
)


@dataclass(slots=True)
class FriendState:
    """A friend's current state - the single source of truth for hearsay."""

    mood: str = "neutral"
    recent_topics: list[str] = field(default_factory=list)
    last_letter_summary: str = ""
    last_updated_world_day: int = 0


@dataclass(slots=True)
class MailService:
    """Generates friend mail on trigger, tracks state, exposes hearsay."""

    llm: LLMClient
    conn: sqlite3.Connection
    memory: MemoryStream
    personas: Mapping[str, Persona]
    clock: WorldClock
    now_world: Callable[[], int]
    broadcaster: Publisher | None = None
    cadence_days: int = DEFAULT_CADENCE_DAYS
    session_mails_sent: int = 0
    locale: str = "en"
    friend_pairs: dict[str, str] = field(init=False, default_factory=dict)
    peer_to_friend: dict[str, str] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        """Derive friend<->peer pairing from the persona registry's `pair` field.

        Persona files are officially user-editable (personas.py's module
        docstring, requirements §3.2); pairing must follow whatever
        `pair:` each friend persona file currently declares, not a
        hardcoded mapping that ignores edits to it (finding).
        """
        self.friend_pairs = {
            persona.id: persona.pair
            for persona in self.personas.values()
            if persona.kind == "friend" and persona.pair is not None
        }
        self.peer_to_friend = {
            peer_id: friend_id for friend_id, peer_id in self.friend_pairs.items()
        }

    def friend_state(self, friend_id: str) -> FriendState:
        """The friend's current state, or the neutral default if unset."""
        raw = get_world_state(self.conn, f"{FRIEND_STATE_KEY_PREFIX}{friend_id}")
        if raw is None:
            return FriendState()
        return FriendState(**json.loads(raw))

    def hearsay_text(self, peer_id: str) -> str | None:
        """Hearsay text *peer_id* can relay from their paired friend.

        Returns:
            `None` when *peer_id* has no paired friend, or that friend
            has never written a letter yet.
        """
        friend_id = self.peer_to_friend.get(peer_id)
        if friend_id is None:
            return None
        state = self.friend_state(friend_id)
        if not state.last_letter_summary:
            return None
        topics = ", ".join(state.recent_topics) if state.recent_topics else "life"
        friend_name = friend_id.title()
        return (
            f"{HEARSAY_PREFIX} {friend_name} wrote about {topics}: "
            f"{state.last_letter_summary}"
        )

    async def notify_event(self, peer_id: str, trigger_context: str) -> bool:
        """Try to generate mail from *peer_id*'s paired friend on an event.

        Returns:
            Whether a letter was generated.
        """
        friend_id = self.peer_to_friend.get(peer_id)
        if friend_id is None:
            return False
        return await self._maybe_generate(friend_id, trigger_context)

    async def maybe_generate_cadence_mail(self, friend_id: str) -> bool:
        """Generate a routine check-in once `cadence_days` have elapsed."""
        state = self.friend_state(friend_id)
        current_day = self.clock.day(self.now_world())
        if current_day - state.last_updated_world_day < self.cadence_days:
            return False
        return await self._maybe_generate(
            friend_id, "It has been a while since your last letter."
        )

    async def reply(self, mail_id: int, text: str) -> bool:
        """Persist the Keeper's reply, then generate a follow-up letter.

        Returns:
            Whether a follow-up letter was generated (`False` when the
            mail id is unknown or the session cap has been reached).
        """
        original = get_mail(self.conn, mail_id)
        if original is None:
            return False
        reply_id = insert_mail(
            self.conn,
            NewMail(
                friend_id=original.friend_id,
                direction="out",
                subject=f"Re: {original.subject}",
                body=text,
                parent_id=mail_id,
                ts_world=self.now_world(),
            ),
        )
        return await self._maybe_generate(
            original.friend_id,
            f"The Keeper replied to your letter: {text}",
            parent_id=reply_id,
        )

    async def _maybe_generate(
        self, friend_id: str, trigger_context: str, *, parent_id: int | None = None
    ) -> bool:
        """Generate one letter from *friend_id*, if eligible.

        Args:
            friend_id: The friend persona writing the letter.
            trigger_context: What prompted this letter (an event, a
                cadence check-in, or the Keeper's reply text).
            parent_id: The Keeper reply mail id this letter answers, so
                the thread can be reconstructed past one exchange
                (`reply()` passes the id of the reply it just inserted;
                every other trigger leaves this letter root-level).

        Returns:
            Whether a letter was generated.
        """
        if self.session_mails_sent >= SESSION_MAIL_CAP:
            return False
        state = self.friend_state(friend_id)
        persona = self.personas.get(friend_id)
        fixed = (
            build_fixed_prefix(persona.body, self.locale)
            if persona is not None
            else WORLD_RULES
        )
        variable = (
            f"{GENERATE_INSTRUCTIONS}\n\nTrigger: {trigger_context}\n\n"
            f"Your current mood: {state.mood}. Recent topics: "
            f"{', '.join(state.recent_topics) or 'nothing notable yet'}. "
            f"Last letter summary: {state.last_letter_summary or '(none yet)'}."
        )
        result = await self.llm.call(
            role="background",
            prompt=PromptParts(fixed, variable),
            schema=MailLetter,
            purpose="mail",
        )
        if result.parsed is None or not isinstance(result.parsed, MailLetter):
            return False
        letter = result.parsed
        insert_mail(
            self.conn,
            NewMail(
                friend_id=friend_id,
                direction="in",
                subject=letter.subject,
                body=letter.body,
                parent_id=parent_id,
                ts_world=self.now_world(),
            ),
        )
        await self.memory.write(
            peer_id=friend_id,
            ts_world=self.now_world(),
            kind="conversation",
            text=letter.body,
        )
        set_world_state(
            self.conn,
            f"{FRIEND_STATE_KEY_PREFIX}{friend_id}",
            json.dumps(
                asdict(
                    FriendState(
                        mood=letter.mood,
                        recent_topics=letter.recent_topics,
                        last_letter_summary=letter.summary,
                        last_updated_world_day=self.clock.day(self.now_world()),
                    )
                )
            ),
        )
        self.session_mails_sent += 1
        if self.broadcaster is not None:
            await self.broadcaster.publish(
                {"t": "event", "kind": "mail_received", "friend_id": friend_id}
            )
        return True


CADENCE_CHECK_INTERVAL_SECONDS = 300


async def run_cadence_loop(
    service: MailService,
) -> None:  # pragma: no cover - async driver
    """Periodically check every friend's cadence-mail eligibility, forever."""
    import asyncio  # noqa: PLC0415 - driver-only dependency

    while True:
        await asyncio.sleep(CADENCE_CHECK_INTERVAL_SECONDS)
        for friend_id in service.friend_pairs:
            try:
                await service.maybe_generate_cadence_mail(friend_id)
            except Exception:
                # One bad friend/state must not silently stop cadence
                # mail for every friend for the rest of the process's
                # life.
                logger.exception("cadence mail failed for %s; continuing", friend_id)
