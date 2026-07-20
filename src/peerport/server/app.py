"""FastAPI application factory and lifespan wiring for the PeerPort server.

Per `docs/design/architecture.md` §1/§7: the tick task advances the world
clock and flushes position diffs every `config.world.tick_ms`, and never
awaits an LLM call. Peer scheduling, LLM workers, and DB persistence are
wired in by later issues (#13+); this ticket (#10) establishes the server
skeleton — WS wire protocol, REST stub routes, and static hosting — over
an in-memory, initially-empty `WorldState`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from peerport.config import Config
from peerport.friends.mail import run_cadence_loop
from peerport.logbook import run_boot_generation
from peerport.memory.reflect import run_forgetting_loop, run_reflection_loop
from peerport.memory.stream import run_scoring_loop
from peerport.server.api import router as api_router
from peerport.server.state import Broadcaster, WorldState, tick_state
from peerport.server.ws import router as ws_router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from peerport.world.sim import Simulation

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


async def _tick_loop(state: WorldState, broadcaster: Broadcaster, tick_ms: int) -> None:
    """Advance the world every `tick_ms` and publish diffs, forever.

    Per architecture.md §1, this task never awaits an LLM call — it only
    advances the clock/peer table and flushes the resulting diff.

    Args:
        state: World state to advance in place.
        broadcaster: Fan-out target for resulting diffs.
        tick_ms: Tick duration in milliseconds.
    """
    interval = tick_ms / 1000
    while True:
        await asyncio.sleep(interval)
        try:
            diff = tick_state(state, tick_ms)
            if diff is not None:
                await broadcaster.publish(diff)
        except Exception:
            # One bad tick must not silently freeze the world clock
            # forever with the WS connection still looking healthy.
            logger.exception("tick loop iteration failed; continuing")


async def _tick_loop_simulation(
    simulation: Simulation, broadcaster: Broadcaster, tick_ms: int
) -> None:
    """Drive the full simulation every `tick_ms` and publish its frames."""
    interval = tick_ms / 1000
    while True:
        await asyncio.sleep(interval)
        try:
            for frame in simulation.tick(tick_ms):
                await broadcaster.publish(frame)
        except Exception:
            logger.exception("simulation tick loop iteration failed; continuing")


def create_app(
    config: Config | None = None, simulation: Simulation | None = None
) -> FastAPI:
    """Build the PeerPort FastAPI application.

    Args:
        config: Resolved configuration; defaults to `Config()` (all
            documented defaults) when omitted.
        simulation: The world simulation to drive from the tick loop.
            When omitted, the app falls back to a bare in-memory
            `WorldState` (the pre-#13 skeleton behavior kept for tests).

    Returns:
        A FastAPI app with `/`, `/static/*`, `/ws`, and `/api/*` wired,
        and a background tick task started/stopped via its lifespan.
    """
    resolved_config = config or Config()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.config = resolved_config
        app.state.simulation = simulation
        app.state.world_state = (
            simulation.state if simulation is not None else WorldState()
        )
        if simulation is not None:
            tick_coro = _tick_loop_simulation(
                simulation, app.state.broadcaster, resolved_config.world.tick_ms
            )
        else:
            tick_coro = _tick_loop(
                app.state.world_state,
                app.state.broadcaster,
                resolved_config.world.tick_ms,
            )
        tick_task = asyncio.create_task(tick_coro)
        engine = getattr(app.state, "decision_engine", None)
        scheduler_tasks = (
            [
                asyncio.create_task(engine.run_peer(peer_id))
                for peer_id in engine.sim.peers
            ]
            if engine is not None
            else []
        )
        logbook_service = getattr(app.state, "logbook_service", None)
        logbook_tasks = (
            [
                asyncio.create_task(
                    run_boot_generation(
                        logbook_service,
                        app.state.broadcaster,
                        weekly_enabled=resolved_config.logbook.weekly_summary,
                    )
                )
            ]
            if logbook_service is not None
            else []
        )
        mail_service = getattr(app.state, "mail_service", None)
        mail_tasks = []
        if mail_service is not None:
            mail_tasks = [asyncio.create_task(run_cadence_loop(mail_service))]
        reflection_engine = getattr(app.state, "reflection_engine", None)
        reflection_tasks = (
            [
                asyncio.create_task(
                    run_reflection_loop(reflection_engine, list(simulation.peers))
                ),
                asyncio.create_task(
                    run_forgetting_loop(reflection_engine, list(simulation.peers))
                ),
            ]
            if reflection_engine is not None and simulation is not None
            else []
        )
        scoring_llm = getattr(app.state, "memory_scoring_llm", None)
        scoring_memory = getattr(app.state, "memory_scoring_memory", None)
        personas = getattr(app.state, "personas", None)
        # Every persona kind (including friends, unlike the map-only
        # reflection/forgetting loops above), since friends/mail.py's
        # `_maybe_generate` also leaves pending memories that need
        # scoring (finding).
        scoring_tasks = (
            [
                asyncio.create_task(
                    run_scoring_loop(scoring_llm, scoring_memory, list(personas))
                )
            ]
            if scoring_llm is not None and scoring_memory is not None and personas
            else []
        )
        try:
            yield
        finally:
            for task in (
                *scheduler_tasks,
                *logbook_tasks,
                *mail_tasks,
                *reflection_tasks,
                *scoring_tasks,
                tick_task,
            ):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="PeerPort", lifespan=lifespan)
    # Created at construction time (not in the lifespan) because the boot
    # wiring in `__main__.py` hands it to LLM-gated services before uvicorn
    # ever runs; `Broadcaster()` needs no running event loop.
    app.state.broadcaster = Broadcaster()
    app.include_router(ws_router)
    app.include_router(api_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        """Serve the single-page PixiJS-hosting shell."""
        return FileResponse(STATIC_DIR / "index.html")

    return app
