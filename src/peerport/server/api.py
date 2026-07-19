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
from peerport.db import insert_board_post, list_board_posts, list_relationships

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
async def post_chat(request: Request) -> JSONResponse:
    """Keeper→Mate chat message; response deltas stream over `/ws` (#18)."""
    chat = getattr(request.app.state, "mate_chat", None)
    if chat is None:
        return _stub()
    body = await request.json()
    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse(status_code=422, content={"detail": "empty message"})
    await chat.handle(text)
    return JSONResponse(content={"ok": True})


@router.post("/board")
async def post_board(request: Request) -> JSONResponse:
    """Keeper posts to the Signal Tower board; peers re-decide (#21)."""
    conn = getattr(request.app.state, "db_conn", None)
    if conn is None:
        return _stub()
    body = await request.json()
    text = str(body.get("body", "")).strip()
    if not text:
        return JSONResponse(status_code=422, content={"detail": "empty post"})
    simulation = getattr(request.app.state, "simulation", None)
    ts_world = simulation.state.world_seconds if simulation is not None else 0
    post_id = insert_board_post(conn, author_id="keeper", body=text, ts_world=ts_world)
    await request.app.state.broadcaster.publish(
        {"t": "event", "kind": "board_post", "author": "keeper"}
    )
    engine = getattr(request.app.state, "decision_engine", None)
    if engine is not None:
        await engine.trigger_redecision()
    return JSONResponse(content={"ok": True, "id": post_id})


@router.get("/board")
async def get_board(request: Request) -> JSONResponse:
    """List board posts, newest first (#21)."""
    conn = getattr(request.app.state, "db_conn", None)
    if conn is None:
        return _stub()
    return JSONResponse(content={"posts": list_board_posts(conn)})


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
async def get_peer(request: Request, peer_id: str) -> JSONResponse:
    """Popup data: identity, mood, ties (no numeric scores), lately (#20)."""
    conn = getattr(request.app.state, "db_conn", None)
    personas = getattr(request.app.state, "personas", None)
    if conn is None or personas is None:
        return _stub(peer_id=peer_id)
    persona = personas.get(peer_id)
    if persona is None:
        return JSONResponse(status_code=404, content={"detail": "unknown peer"})
    engine = getattr(request.app.state, "decision_engine", None)
    mood = engine.last_mood(peer_id) if engine is not None else None
    ties = [
        {
            "peer": other,
            "label": relationship.label,
            "trend": (
                "up"
                if relationship.last_delta > 0
                else "down"
                if relationship.last_delta < 0
                else "flat"
            ),
        }
        for other, relationship in list_relationships(conn, peer_id)
    ]
    lately = [
        row[0]
        for row in conn.execute(
            "SELECT text FROM memories WHERE peer_id = ? ORDER BY id DESC LIMIT 3",
            (peer_id,),
        ).fetchall()
    ]
    return JSONResponse(
        content={
            "id": persona.id,
            "name": persona.name,
            "kind": persona.kind,
            "sprite": persona.sprite,
            "mood": mood,
            "ties": ties,
            "lately": lately,
        }
    )


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
