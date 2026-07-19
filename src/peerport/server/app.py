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
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from peerport.config import Config
from peerport.server.api import router as api_router
from peerport.server.state import Broadcaster, WorldState, tick_state
from peerport.server.ws import router as ws_router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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
        diff = tick_state(state, tick_ms)
        if diff is not None:
            await broadcaster.publish(diff)


def create_app(config: Config | None = None) -> FastAPI:
    """Build the PeerPort FastAPI application.

    Args:
        config: Resolved configuration; defaults to `Config()` (all
            documented defaults) when omitted.

    Returns:
        A FastAPI app with `/`, `/static/*`, `/ws`, and `/api/*` wired,
        and a background tick task started/stopped via its lifespan.
    """
    resolved_config = config or Config()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.config = resolved_config
        app.state.world_state = WorldState()
        app.state.broadcaster = Broadcaster()
        tick_task = asyncio.create_task(
            _tick_loop(
                app.state.world_state,
                app.state.broadcaster,
                resolved_config.world.tick_ms,
            )
        )
        try:
            yield
        finally:
            tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick_task

    app = FastAPI(title="PeerPort", lifespan=lifespan)
    app.include_router(ws_router)
    app.include_router(api_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        """Serve the single-page PixiJS-hosting shell."""
        return FileResponse(STATIC_DIR / "index.html")

    return app
