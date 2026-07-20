"""Absence reports, weekly summaries, and the Logbook data source (#22).

Per requirements.md §4.7, launching after a ≥30-minute real-world absence
triggers exactly one background-model call that narrates 3-10 events
(log-scaled to elapsed time, hard-capped at 10). Accepted events are
written to `events` (type="logbook") so the Logbook tab's "While you were
away" (latest batch) and "Chronicle" (full history, grouped by world day)
sections read from one source of truth, plus a `kind=logbook` memory per
involved peer and a relationship delta for multi-peer events.

Divergence: `LogbookEvent` (llm/prompts.py) is fixed to `peer_ids` + `text`
with no delta field, and REQ-002 caps generation at exactly one LLM call,
so a second call to price the relationship change (as #20's conversation
outcome does) is not available here. Multi-peer accepted events instead
apply a fixed `LOGBOOK_RELATIONSHIP_DELTA` through the same
`get_relationship`/`save_relationship` clamp mechanism #20 uses.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from peerport.db import (
    EventRecord,
    Relationship,
    get_relationship,
    get_world_state,
    insert_event,
    list_events_by_type,
    load_last_shutdown_ts_real,
    save_relationship,
    set_world_state,
)
from peerport.llm.client import PromptParts
from peerport.llm.prompts import WORLD_RULES, LogbookEvent, LogbookEvents
from peerport.peers.converse import SCORE_MAX, SCORE_MIN
from peerport.peers.personas import MAP_KINDS

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping, Sequence
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

ABSENCE_THRESHOLD_SECONDS = 1800
MIN_EVENTS = 3
MAX_EVENTS = 10
EVENT_TYPE = "logbook"
BATCH_ABSENCE = "absence"
BATCH_WEEKLY = "weekly"
WEEKLY_SUMMARY_INTERVAL_DAYS = 7
WEEKLY_SUMMARY_DAY_KEY = "logbook_weekly_summary_last_day"
LOGBOOK_RELATIONSHIP_DELTA = 2
MULTI_PEER_EVENT_THRESHOLD = 2
DIGEST_OPENING_KEY = "logbook.digest_opening"
DEFAULT_DIGEST_OPENING = "Welcome back. While you were away..."

GENERATE_INSTRUCTIONS = (
    "Generate exactly {count} short third-person events that happened in "
    "the port while the Keeper was away for about {minutes:.0f} minutes. "
    "Only reference these known peers: {peer_ids}. Only reference these "
    "known places: {locations}. Never invent new characters, places, or "
    "events that contradict the peer status below."
)

WEEKLY_INSTRUCTIONS = (
    "Write a short 'this week in port' summary as 1-3 short third-person "
    "entries covering the recent world state below. Only reference these "
    "known peers: {peer_ids}. Only reference these known places: "
    "{locations}. Never invent new characters or places."
)


def event_count_for_minutes(minutes_away: float) -> int:
    """Log-scaled event count: `clamp(round(3 + log2(m/30+1)*2), 3, 10)`."""
    raw = round(3 + math.log2(minutes_away / 30 + 1) * 2)
    return max(MIN_EVENTS, min(MAX_EVENTS, raw))


def _digest_opening(locale: str) -> str:
    """The localized "While you were away..." digest opener.

    Read fresh from the locale catalog (not cached, mirrors
    `mate.chat._fallback_text`) so a missing/renamed key or catalog file
    degrades to `DEFAULT_DIGEST_OPENING` instead of raising.
    """
    catalog_path = Path("locales") / f"{locale}.json"
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_DIGEST_OPENING
    text = catalog.get(DIGEST_OPENING_KEY)
    return text if isinstance(text, str) else DEFAULT_DIGEST_OPENING


@dataclass(slots=True)
class LogbookService:
    """Generates absence reports/weekly summaries and reads the Logbook."""

    llm: LLMClient
    conn: sqlite3.Connection
    memory: MemoryStream
    personas: Mapping[str, Persona]
    locations: Sequence[str]
    clock: WorldClock
    now_world: Callable[[], int]
    now_real: Callable[[], float] = field(default=time.time)
    locale: str = "en"

    def _map_peer_ids(self) -> list[str]:
        """Ids of map-eligible personas only (excludes `kind="friend"`).

        Friend personas (Kai, Mia) never appear on the map (requirements
        §4.6); a generated absence/weekly event may not reference them,
        so both the prompt's "known peers" list and validation must use
        this narrower set instead of the full persona registry.
        """
        return sorted(
            peer_id
            for peer_id, persona in self.personas.items()
            if persona.kind in MAP_KINDS
        )

    async def maybe_generate_absence_report(self) -> list[LogbookEvent]:
        """Generate the absence report when the elapsed real time qualifies.

        Returns:
            The accepted events (empty when no report was due, the call
            was skipped, or every candidate event was rejected).
        """
        last_shutdown = load_last_shutdown_ts_real(self.conn)
        if last_shutdown is None:
            return []
        elapsed = self.now_real() - last_shutdown
        if elapsed < ABSENCE_THRESHOLD_SECONDS:
            return []
        minutes_away = elapsed / 60
        count = event_count_for_minutes(minutes_away)
        instructions = GENERATE_INSTRUCTIONS.format(
            count=count,
            minutes=minutes_away,
            peer_ids=", ".join(self._map_peer_ids()),
            locations=", ".join(sorted(self.locations)),
        )
        variable = f"{instructions}\n\nPeer status before the absence:\n{self._peer_status_summary()}"
        events = await self._generate(variable)
        await self._persist(events, batch=BATCH_ABSENCE)
        return events

    async def maybe_generate_weekly_summary(
        self, *, enabled: bool
    ) -> list[LogbookEvent]:
        """Generate the weekly 'this week in port' summary when it is due.

        Args:
            enabled: The resolved `config.logbook.weekly_summary` toggle.

        Returns:
            The accepted events (empty when disabled, not yet due, or
            every candidate event was rejected).
        """
        if not enabled:
            return []
        current_day = self.clock.day(self.now_world())
        last_day_raw = get_world_state(self.conn, WEEKLY_SUMMARY_DAY_KEY)
        last_day = int(last_day_raw) if last_day_raw is not None else 0
        if current_day - last_day < WEEKLY_SUMMARY_INTERVAL_DAYS:
            return []
        instructions = WEEKLY_INSTRUCTIONS.format(
            peer_ids=", ".join(self._map_peer_ids()),
            locations=", ".join(sorted(self.locations)),
        )
        variable = f"{instructions}\n\nPeer status:\n{self._peer_status_summary()}"
        events = await self._generate(variable)
        await self._persist(events, batch=BATCH_WEEKLY)
        set_world_state(self.conn, WEEKLY_SUMMARY_DAY_KEY, str(current_day))
        return events

    def digest_text(self, events: list[LogbookEvent]) -> str:
        """Render the Mate-tab 'While you were away...' digest opening."""
        if not events:
            return ""
        narration = " ".join(event.text for event in events)
        return f"{_digest_opening(self.locale)} {narration}"

    def read_logbook(self) -> dict[str, object]:
        """The Logbook tab's backing data: latest digest + full chronicle."""
        rows = list_events_by_type(self.conn, EVENT_TYPE)
        absence_rows = [r for r in rows if r["payload"]["batch"] == BATCH_ABSENCE]  # type: ignore[index]
        while_away: list[dict[str, object]] = []
        if absence_rows:
            latest_ts_real = max(r["ts_real"] for r in absence_rows)  # type: ignore[type-var]
            while_away = [
                {"text": r["payload"]["text"], "ts_world": r["ts_world"]}  # type: ignore[index]
                for r in absence_rows
                if r["ts_real"] == latest_ts_real
            ]
        chronicle_by_day: dict[int, list[str]] = {}
        for row in rows:
            day = self.clock.day(row["ts_world"])  # type: ignore[arg-type]
            chronicle_by_day.setdefault(day, []).append(row["payload"]["text"])  # type: ignore[index]
        chronicle = [
            {"day": day, "entries": entries}
            for day, entries in sorted(chronicle_by_day.items())
        ]
        return {"while_away": while_away, "chronicle": chronicle}

    def _fixed_prefix(self) -> str:
        """The locale-tagged fixed prefix for generation calls.

        `WORLD_RULES` itself must stay a byte-stable, frozen constant
        (llm/prompts.py), so the locale line is appended here rather
        than interpolated into it — otherwise the model is never told
        what language to narrate in and defaults to English regardless
        of `config.locale` (finding).
        """
        return f"{WORLD_RULES}\n\nLocale: {self.locale}\n"

    async def _generate(self, variable: str) -> list[LogbookEvent]:
        result = await self.llm.call(
            role="background",
            prompt=PromptParts(self._fixed_prefix(), variable),
            schema=LogbookEvents,
            purpose="logbook",
        )
        if result.parsed is None or not isinstance(result.parsed, LogbookEvents):
            return []
        return self._filter_valid(result.parsed.events)

    def _filter_valid(self, events: list[LogbookEvent]) -> list[LogbookEvent]:
        valid_ids = set(self._map_peer_ids())
        accepted = [
            event
            for event in events
            if all(peer_id in valid_ids for peer_id in event.peer_ids)
        ]
        return accepted[:MAX_EVENTS]

    async def _persist(self, events: list[LogbookEvent], *, batch: str) -> None:
        if not events:
            return
        ts_real = int(self.now_real())
        ts_world = self.now_world()
        for event in events:
            insert_event(
                self.conn,
                EventRecord(
                    ts_world=ts_world,
                    kind=EVENT_TYPE,
                    actors=event.peer_ids,
                    payload=json.dumps({"text": event.text, "batch": batch}),
                    ts_real=ts_real,
                ),
            )
            for peer_id in event.peer_ids:
                await self.memory.write(
                    peer_id=peer_id, ts_world=ts_world, kind="logbook", text=event.text
                )
            if len(event.peer_ids) >= MULTI_PEER_EVENT_THRESHOLD:
                self._apply_relationship_delta(event.peer_ids[0], event.peer_ids[1])

    def _apply_relationship_delta(self, peer_a: str, peer_b: str) -> None:
        previous = get_relationship(self.conn, peer_a, peer_b)
        new_score = max(
            SCORE_MIN, min(SCORE_MAX, previous.score + LOGBOOK_RELATIONSHIP_DELTA)
        )
        save_relationship(
            self.conn,
            (peer_a, peer_b),
            Relationship(
                score=new_score,
                label=previous.label,
                last_delta=LOGBOOK_RELATIONSHIP_DELTA,
            ),
        )

    def _peer_status_summary(self) -> str:
        lines = []
        for peer_id in self._map_peer_ids():
            row = self.conn.execute(
                "SELECT text FROM memories WHERE peer_id = ? ORDER BY id DESC LIMIT 1",
                (peer_id,),
            ).fetchone()
            status = row[0] if row is not None else "no notable recent activity"
            lines.append(f"- {peer_id}: {status}")
        return "\n".join(lines)


async def run_boot_generation(
    service: LogbookService, broadcaster: Publisher, *, weekly_enabled: bool
) -> None:
    """Boot-time absence report + weekly summary, broadcasting the results.

    Publishes a `digest` frame with the "While you were away..." text for
    the Mate tab (#18 owns rendering it) when an absence report generated,
    and a `logbook_updated` event so the Bridge can refresh the Logbook
    tab and light its unread dot.
    """
    absence_events = await service.maybe_generate_absence_report()
    weekly_events = await service.maybe_generate_weekly_summary(enabled=weekly_enabled)
    if absence_events:
        await broadcaster.publish(
            {"t": "digest", "text": service.digest_text(absence_events)}
        )
    if absence_events or weekly_events:
        await broadcaster.publish({"t": "event", "kind": "logbook_updated"})
