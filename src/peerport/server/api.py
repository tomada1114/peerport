"""REST endpoint stubs for Keeper→server Bridge commands.

Per `docs/design/architecture.md` §4, upstream Keeper commands are
request/response-shaped REST, while downstream world updates flow over
`/ws` (see `server/ws.py`). This ticket (#10) only fixes the route
surface; each stub's real behavior is implemented by its owning issue
(noted per-route below).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from peerport.config import VALID_LOCALES

router = APIRouter(prefix="/api")

VALID_SPEEDS = (1, 2)


def _stub(**extra: object) -> JSONResponse:
    """Return the shared "not implemented yet" stub response.

    Args:
        extra: Additional identifying fields to echo back (e.g. a path
            param), useful for verifying routing without real behavior.
    """
    return JSONResponse(
        status_code=501, content={"detail": "not implemented yet", **extra}
    )


@router.post("/chat")
async def post_chat() -> JSONResponse:
    """Keeper→Mate chat message; response deltas stream over `/ws`. See #18."""
    return _stub()


@router.post("/board")
async def post_board() -> JSONResponse:
    """Post to the Signal Tower bulletin board. See #21."""
    return _stub()


@router.post("/mail/{mail_id}/reply")
async def post_mail_reply(mail_id: str) -> JSONResponse:
    """Reply to a friend's mail. See #23."""
    return _stub(mail_id=mail_id)


@router.post("/world")
async def post_world(request: Request) -> JSONResponse:
    """Pause/resume/speed control over the running simulation (#13).

    Body: `{"action": "pause" | "resume" | "speed", "speed": 1 | 2}`
    (`speed` only for the `speed` action). Responds with the resulting
    `paused`/`speed` state; pause/resume also broadcast a `state` frame.
    """
    simulation = getattr(request.app.state, "simulation", None)
    if simulation is None:
        return _stub()
    body = await request.json()
    action = body.get("action")
    if action == "pause":
        simulation.paused = True
        await request.app.state.broadcaster.publish({"t": "state", "state": "paused"})
    elif action == "resume":
        simulation.paused = False
        await request.app.state.broadcaster.publish({"t": "state", "state": "resumed"})
    elif action == "speed":
        speed = body.get("speed")
        if speed not in VALID_SPEEDS:
            return JSONResponse(
                status_code=422,
                content={"detail": f"speed must be one of {VALID_SPEEDS}"},
            )
        simulation.speed = speed
    else:
        return JSONResponse(
            status_code=422, content={"detail": f"unknown action: {action}"}
        )
    return JSONResponse(
        content={"ok": True, "paused": simulation.paused, "speed": simulation.speed}
    )


@router.get("/notes")
async def get_notes() -> JSONResponse:
    """List Mate's notes. See #25."""
    return _stub()


@router.post("/notes")
async def post_notes() -> JSONResponse:
    """Create or update a Mate note. See #25."""
    return _stub()


@router.get("/logbook")
async def get_logbook() -> JSONResponse:
    """Fetch the absence/weekly chronicle log. See #22."""
    return _stub()


@router.get("/usage")
async def get_usage() -> JSONResponse:
    """Fetch today's LLM spend. See #16/#27."""
    return _stub()


@router.post("/settings")
async def post_settings() -> JSONResponse:
    """Update runtime settings. See #29."""
    return _stub()


@router.get("/peer/{peer_id}")
async def get_peer(peer_id: str) -> JSONResponse:
    """Fetch popup data for a peer. See #20."""
    return _stub(peer_id=peer_id)


@router.get("/onboarding")
async def get_onboarding() -> JSONResponse:
    """Onboarding status/state. See #29."""
    return _stub()


@router.get("/map")
async def get_map() -> JSONResponse:
    """Serve `data/map/port.json` for the PixiJS renderer (#14)."""
    map_path = Path("data") / "map" / "port.json"
    if not map_path.exists():
        return JSONResponse(status_code=404, content={"detail": "map data not found"})
    return JSONResponse(content=json.loads(map_path.read_text()))


@router.get("/locales/{locale}")
async def get_locale_catalog(locale: str) -> JSONResponse:
    """Serve a UI copy catalog (`locales/{en,ja}.json`) for the client (#15)."""
    if locale not in VALID_LOCALES:
        return JSONResponse(status_code=404, content={"detail": "unknown locale"})
    catalog_path = Path("locales") / f"{locale}.json"
    if not catalog_path.exists():
        return JSONResponse(status_code=404, content={"detail": "catalog not found"})
    return JSONResponse(content=json.loads(catalog_path.read_text()))


@router.get("/config")
async def get_config(request: Request) -> JSONResponse:
    """Expose client-relevant configuration (currently the active locale)."""
    config = getattr(request.app.state, "config", None)
    locale = config.locale if config is not None else "en"
    return JSONResponse(content={"locale": locale})
