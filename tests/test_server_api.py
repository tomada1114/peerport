"""Tests for peerport.server.api (REST stub routes)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from peerport.server.app import create_app

ROUTES: list[tuple[str, str]] = [
    ("POST", "/api/chat"),
    ("POST", "/api/board"),
    ("POST", "/api/mail/mail-1/reply"),
    ("POST", "/api/world"),
    ("GET", "/api/notes"),
    ("POST", "/api/notes"),
    ("GET", "/api/logbook"),
    ("GET", "/api/usage"),
    ("POST", "/api/settings"),
    ("GET", "/api/peer/beacon"),
    ("GET", "/api/onboarding"),
]


class TestApiStubs:
    @pytest.mark.parametrize(("method", "path"), ROUTES)
    def test_stub_route_responds_without_404(self, method: str, path: str) -> None:
        app = create_app()
        with TestClient(app) as client:
            response = client.request(method, path)

        assert response.status_code != 404
        assert response.status_code == 501
        assert "detail" in response.json()

    def test_path_param_routes_echo_the_id_in_the_stub_body(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            response = client.get("/api/peer/beacon")

        assert response.json()["peer_id"] == "beacon"
