"""Tests for peerport.server.app (app factory, static hosting, lifespan)."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from peerport.config import Config, ServerConfig
from peerport.llm.prompts import LogbookEvent
from peerport.server.app import create_app


class TestIndexPage:
    def test_root_returns_200_html(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            response = client.get("/")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_root_references_net_js(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            response = client.get("/")

        assert "net.js" in response.text


class TestVendoredStaticAssets:
    def test_pixi_min_js_returns_200_from_repo(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            response = client.get("/static/vendor/pixi.min.js")

        assert response.status_code == 200
        assert b"pixi.js" in response.content[:200]

    def test_net_js_is_served(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            response = client.get("/static/js/net.js")

        assert response.status_code == 200
        assert "WebSocket" in response.text


class TestAppFactoryDefaults:
    def test_uses_default_config_when_none_given(self) -> None:
        app = create_app()
        with TestClient(app):
            assert app.state.config.server.port == 8712

    def test_honors_a_supplied_config(self) -> None:
        app = create_app(Config(server=ServerConfig(port=9999)))
        with TestClient(app):
            assert app.state.config.server.port == 9999

    def test_lifespan_starts_and_stops_cleanly(self) -> None:
        app = create_app()
        with TestClient(app):
            assert app.state.world_state is not None
        # No assertion beyond "no exception raised on teardown".


class FakeLogbookService:
    def __init__(self) -> None:
        self.events = [LogbookEvent(peer_ids=["tug"], text="Tug tidied the pier.")]

    async def maybe_generate_absence_report(self) -> list[LogbookEvent]:
        # Real generation is one LLM round trip; a short delay here mirrors
        # that boundary latency so the boot task cannot outrace the test's
        # own WS handshake (a real network call never would).
        await asyncio.sleep(0.05)
        return self.events

    async def maybe_generate_weekly_summary(
        self,
        *,
        enabled: bool,  # noqa: ARG002 -- fake matches the real service's signature
    ) -> list[LogbookEvent]:
        return []

    def digest_text(self, events: list[LogbookEvent]) -> str:
        return f"Welcome back. While you were away... {events[0].text}"


class TestLogbookBoot:
    def test_boot_broadcasts_digest_and_logbook_updated_over_ws(self) -> None:
        app = create_app()
        app.state.logbook_service = FakeLogbookService()
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # snapshot
            frames = [ws.receive_json() for _ in range(2)]

        types = {frame["t"] for frame in frames}
        assert "digest" in types
        assert any(
            frame["t"] == "event" and frame["kind"] == "logbook_updated"
            for frame in frames
        )
        digest_frame = next(frame for frame in frames if frame["t"] == "digest")
        assert "Tug tidied the pier." in digest_frame["text"]
