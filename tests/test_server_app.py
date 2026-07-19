"""Tests for peerport.server.app (app factory, static hosting, lifespan)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from peerport.config import Config, ServerConfig
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
