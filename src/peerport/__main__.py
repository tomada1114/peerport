"""CLI entry point: argument parsing and the full boot sequence.

Per `docs/design/architecture.md` §7, boot proceeds: load config → open DB
→ rotate backups → (persona/map loading is wired in by later issues
#11-#12) → start the FastAPI/uvicorn server. `--fresh` archives the
existing database before starting a brand-new world.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

from peerport.config import Config, load_config
from peerport.db import (
    DEFAULT_BACKUP_KEEP,
    backup_db,
    load_world_seconds,
    open_db,
    reset_fresh,
    rotate_backups,
    save_last_shutdown_ts_real,
    save_world_seconds,
)
from peerport.errors import ConfigError, MapDataError, PersonaValidationError
from peerport.friends.mail import MailService
from peerport.llm.budget import BudgetGuard
from peerport.llm.client import LLMClient, OpenAIStreamingTransport, OpenAITransport
from peerport.llm.outage import OutageTracker
from peerport.llm.prompts import build_fixed_prefix
from peerport.logbook import LogbookService
from peerport.mate.chat import MateChat
from peerport.mate.notes import NotesStore
from peerport.memory.reflect import ReflectionEngine
from peerport.memory.stream import MemoryStream, OpenAIEmbedder
from peerport.peers.converse import ConversationEngine
from peerport.peers.decide import DecisionEngine, make_board_hooks
from peerport.peers.personas import load_personas
from peerport.server.app import create_app
from peerport.world.clock import WorldClock
from peerport.world.sim import Simulation
from peerport.world.worldmap import WorldMap

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Coroutine, Sequence

    from fastapi import FastAPI

    from peerport.peers.personas import Persona

logger = logging.getLogger(__name__)

# Fire-and-forget broadcasts (#27's degraded-state signals fire from sync
# callbacks with no caller to await them) need a strong reference kept
# somewhere - `asyncio.create_task` alone only holds a weak one, so an
# unreferenced task can be garbage-collected mid-flight and silently drop
# the WS broadcast.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


def _fire_and_forget(coro: Coroutine[object, object, None]) -> None:
    """Schedule *coro* on the running loop, keeping it alive until done."""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


@dataclass(slots=True)
class WireContext:
    """Boot-time inputs shared by every `_wire_*` helper below.

    Bundled into one object per `.claude/rules/python.md`'s "more than 5
    args → dataclass" rule, once #27 needed to thread a shared outage
    tracker and hard-cap signal through every LLM-gated `_wire_*` helper
    alongside the original five (`app`/`config`/`conn`/`personas`/
    `simulation`).
    """

    app: FastAPI
    config: Config
    conn: sqlite3.Connection
    personas: dict[str, Persona]
    simulation: Simulation
    outage: OutageTracker
    on_hard_cap: Callable[[], None]


def make_outage_handler(app: FastAPI) -> Callable[[bool, int | None], None]:
    """Build the shared `OutageTracker.on_change` callback (#27).

    Broadcasts the diegetic fog state as a `{"t": "state", "state": "fog"}`
    frame. `app.state.broadcaster` is read lazily inside the returned
    closure rather than captured now, because `_wire_*` helpers run
    before the app's lifespan sets it (the broadcaster only exists once
    uvicorn actually starts serving).

    Args:
        app: The FastAPI app whose broadcaster will exist by the time an
            outage actually flips.

    Returns:
        A sync callback matching `OutageTracker`'s `on_change` signature.
    """

    def on_change(active: bool, status: int | None) -> None:
        frame: dict[str, object] = {"t": "state", "state": "fog", "active": active}
        if status is not None:
            frame["status"] = status
        _fire_and_forget(app.state.broadcaster.publish(frame))

    return on_change


def make_hard_cap_handler(app: FastAPI, simulation: Simulation) -> Callable[[], None]:
    """Build the shared `BudgetGuard.on_hard_cap` signal (#27).

    Pauses the world and broadcasts `{"t": "state", "state": "hard_stop"}`
    exactly once per trip: every LLM-gated `_wire_*` helper below attaches
    this same handler to its own `BudgetGuard`, and `check_hard_cap()`
    re-fires it on every gated call once the cap is reached, so the
    `simulation.paused` guard keeps a whole hard-cap day from re-pausing
    or re-broadcasting on each subsequent call site.

    Args:
        app: The FastAPI app whose broadcaster will exist by trip time
            (see `make_outage_handler`'s docstring for why this is lazy).
        simulation: The world simulation to pause.

    Returns:
        A sync callback matching `BudgetGuard`'s `on_hard_cap` signature.
    """

    def on_hard_cap() -> None:
        if simulation.paused:
            return
        simulation.paused = True
        _fire_and_forget(
            app.state.broadcaster.publish({"t": "state", "state": "hard_stop"})
        )

    return on_hard_cap


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the `peerport` entry point.

    Args:
        argv: Argument list to parse; defaults to `sys.argv[1:]`.

    Returns:
        Namespace with `fresh: bool` and `debug: bool`.
    """
    parser = argparse.ArgumentParser(prog="peerport")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Archive the existing world and start a brand-new one.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging, including per-call LLM prompts/responses.",
    )
    return parser.parse_args(argv)


def _setup_logging(*, debug: bool) -> None:
    """Configure root logging verbosity for the process."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )


def boot(
    *, fresh: bool, data_dir: Path, config_path: Path
) -> tuple[Config, sqlite3.Connection]:
    """Run the data-layer boot sequence: config, DB open, backup rotation.

    Args:
        fresh: If `True`, archive and empty the existing database.
        data_dir: Directory holding `peerport.db` and `backups/`.
        config_path: Location of `config.toml`.

    Returns:
        The resolved `Config` and an open, schema-applied DB connection.

    Raises:
        SystemExit: If `config.toml` is invalid; the reason is logged.
    """
    try:
        config = load_config(config_path)
    except ConfigError:
        logger.exception("invalid config.toml")
        raise SystemExit(1) from None

    db_path = data_dir / "peerport.db"
    backups_dir = data_dir / "backups"

    if fresh:
        reset_fresh(db_path, backups_dir)
    else:
        backup_db(db_path, backups_dir)

    conn = open_db(db_path)
    rotate_backups(backups_dir, keep=DEFAULT_BACKUP_KEEP)

    return config, conn


def _wire_mate_chat(ctx: WireContext) -> None:
    """Attach the Mate chat pipeline when an API key is available.

    Without OPENAI_API_KEY the world still runs LLM-less (boot §7);
    /api/chat then answers 501 and the fog UI takes over (#27).
    """
    notes_store = NotesStore(Path("data") / "notes")
    ctx.app.state.notes_store = notes_store
    if not os.environ.get("OPENAI_API_KEY"):
        logger.info("OPENAI_API_KEY not set; Mate chat disabled (LLM-less world)")
        return
    mate = next((p for p in ctx.personas.values() if p.kind == "mate"), None)
    if mate is None:
        return
    budget = BudgetGuard(
        ctx.conn,
        ctx.config.budget.soft_cap_usd,
        ctx.config.budget.hard_cap_usd,
        on_hard_cap=ctx.on_hard_cap,
    )
    llm = LLMClient(
        config=ctx.config,
        conn=ctx.conn,
        budget=budget,
        transport=OpenAIStreamingTransport(),
    )
    llm.outage = ctx.outage
    ctx.app.state.mate_chat = MateChat(
        llm=llm,
        memory=MemoryStream(ctx.conn, OpenAIEmbedder()),
        broadcaster=ctx.app.state.broadcaster,
        notes=notes_store,
        mate_id=mate.id,
        fixed_prefix=build_fixed_prefix(mate.body, ctx.config.locale),
        now_world=lambda: ctx.simulation.state.world_seconds,
        locale=ctx.config.locale,
    )


def _wire_friends(ctx: WireContext) -> MailService | None:
    """Attach the friend mail service when an API key is available."""
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    budget = BudgetGuard(
        ctx.conn,
        ctx.config.budget.soft_cap_usd,
        ctx.config.budget.hard_cap_usd,
        on_hard_cap=ctx.on_hard_cap,
    )
    llm = LLMClient(
        config=ctx.config, conn=ctx.conn, budget=budget, transport=OpenAITransport()
    )
    llm.outage = ctx.outage
    service = MailService(
        llm=llm,
        conn=ctx.conn,
        memory=MemoryStream(ctx.conn, OpenAIEmbedder()),
        personas=ctx.personas,
        clock=ctx.simulation.clock,
        now_world=lambda: ctx.simulation.state.world_seconds,
        cadence_days=ctx.config.mail.cadence_days,
    )
    ctx.app.state.mail_service = service
    return service


def _wire_peer_society(ctx: WireContext) -> None:
    """Attach the decision and conversation engines when a key exists.

    Reads `app.state.mail_service` (set by `_wire_friends`, called first)
    to wire hearsay into decisions and peer-event notifications into
    conversations, when a mail service was actually wired.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        return
    mail_service: MailService | None = getattr(ctx.app.state, "mail_service", None)
    budget = BudgetGuard(
        ctx.conn,
        ctx.config.budget.soft_cap_usd,
        ctx.config.budget.hard_cap_usd,
        on_hard_cap=ctx.on_hard_cap,
    )
    llm = LLMClient(
        config=ctx.config, conn=ctx.conn, budget=budget, transport=OpenAITransport()
    )
    llm.outage = ctx.outage
    memory = MemoryStream(ctx.conn, OpenAIEmbedder())
    engine = DecisionEngine(
        llm=llm,
        sim=ctx.simulation,
        personas=ctx.personas,
        rng=ctx.simulation.rng,
        hearsay_provider=mail_service.hearsay_text if mail_service else None,
    )
    conversations = ConversationEngine(
        llm=llm,
        sim=ctx.simulation,
        memory=memory,
        broadcaster=ctx.app.state.broadcaster,
        conn=ctx.conn,
        personas=ctx.personas,
    )

    async def on_talk(speaker: str, target: str) -> None:
        started = await conversations.start(speaker, target)
        if started:
            await engine.trigger_redecision([target])

    if mail_service is not None:

        async def on_peer_event(peer_id: str) -> None:
            await mail_service.notify_event(peer_id, f"An event involving {peer_id}.")

        conversations.on_peer_event = on_peer_event

    post_hook, read_hook = make_board_hooks(
        ctx.conn,
        memory,
        ctx.app.state.broadcaster,
        now_world=lambda: ctx.simulation.state.world_seconds,
    )
    engine.on_talk = on_talk
    engine.on_post_board = post_hook
    engine.on_read_board = read_hook
    ctx.app.state.decision_engine = engine
    ctx.app.state.conversation_engine = conversations


def _wire_logbook(ctx: WireContext) -> None:
    """Attach the Logbook service when an API key is available.

    Without OPENAI_API_KEY, `/api/logbook` answers 501 like the other
    LLM-gated routes; historical entries still persist across restarts
    once a key becomes available.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        return
    budget = BudgetGuard(
        ctx.conn,
        ctx.config.budget.soft_cap_usd,
        ctx.config.budget.hard_cap_usd,
        on_hard_cap=ctx.on_hard_cap,
    )
    llm = LLMClient(
        config=ctx.config, conn=ctx.conn, budget=budget, transport=OpenAITransport()
    )
    llm.outage = ctx.outage
    ctx.app.state.logbook_service = LogbookService(
        llm=llm,
        conn=ctx.conn,
        memory=MemoryStream(ctx.conn, OpenAIEmbedder()),
        personas=ctx.personas,
        locations=list(ctx.simulation.worldmap.nodes),
        clock=ctx.simulation.clock,
        now_world=lambda: ctx.simulation.state.world_seconds,
    )


def _wire_reflection(ctx: WireContext) -> None:
    """Attach the reflection/forgetting engine when an API key is available.

    `server/app.py`'s lifespan starts the actual polling loops
    (`run_reflection_loop`/`run_forgetting_loop`) once it finds
    `app.state.reflection_engine` set here (#26).
    """
    if not os.environ.get("OPENAI_API_KEY"):
        return
    budget = BudgetGuard(
        ctx.conn,
        ctx.config.budget.soft_cap_usd,
        ctx.config.budget.hard_cap_usd,
        on_hard_cap=ctx.on_hard_cap,
    )
    llm = LLMClient(
        config=ctx.config, conn=ctx.conn, budget=budget, transport=OpenAITransport()
    )
    llm.outage = ctx.outage
    ctx.app.state.reflection_engine = ReflectionEngine(
        llm=llm,
        conn=ctx.conn,
        memory=MemoryStream(ctx.conn, OpenAIEmbedder()),
        personas=ctx.personas,
        clock=ctx.simulation.clock,
        now_world=lambda: ctx.simulation.state.world_seconds,
        locale=ctx.config.locale,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point (console script `peerport`).

    Args:
        argv: Argument list to parse; defaults to `sys.argv[1:]`.

    Returns:
        Process exit code: `0` on success, non-zero on a boot failure.
    """
    args = parse_args(argv)
    _setup_logging(debug=args.debug)

    try:
        config, conn = boot(
            fresh=args.fresh,
            data_dir=Path("data"),
            config_path=Path("config.toml"),
        )
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1

    try:
        personas = load_personas(Path("personas"))
    except PersonaValidationError:
        logger.exception("invalid persona file")
        conn.close()
        return 1
    try:
        worldmap = WorldMap.load(Path("data") / "map" / "port.json")
    except MapDataError:
        logger.exception("invalid map data")
        conn.close()
        return 1

    simulation = Simulation(
        worldmap=worldmap,
        personas=personas,
        rng=random.Random(),  # noqa: S311 -- sim wander randomness, not security
        clock=WorldClock(day_length_real_minutes=config.world.day_length_real_minutes),
        initial_world_seconds=load_world_seconds(conn),
    )

    app = create_app(config, simulation=simulation)
    app.state.db_conn = conn
    app.state.personas = personas
    ctx = WireContext(
        app=app,
        config=config,
        conn=conn,
        personas=personas,
        simulation=simulation,
        outage=OutageTracker(on_change=make_outage_handler(app)),
        on_hard_cap=make_hard_cap_handler(app, simulation),
    )
    _wire_mate_chat(ctx)
    _wire_friends(ctx)
    _wire_peer_society(ctx)
    _wire_logbook(ctx)
    _wire_reflection(ctx)
    try:
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=config.server.port,
            log_level="debug" if args.debug else "info",
        )
    finally:
        # Runs on a clean shutdown *and* on a startup/run-time exception
        # (e.g. the configured port already being in use) so the world
        # clock and last-shutdown timestamp are never lost and the DB
        # connection is never leaked.
        save_world_seconds(conn, simulation.state.world_seconds)
        save_last_shutdown_ts_real(conn, int(time.time()))
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
