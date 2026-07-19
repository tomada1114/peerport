"""Proxy verification for the frontend map renderer (#14) and Bridge shell (#15).

The MVP has no browser test harness (architecture.md §6), so these tests
verify the frontend contract server-side: files served, design tokens
present, all UI copy resolved from the locale catalogs, and the map/locale
APIs the client consumes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from peerport.server.app import create_app

REPO_ROOT = Path(__file__).parent.parent
STATIC = REPO_ROOT / "src" / "peerport" / "server" / "static"

DESIGN_TOKENS = {
    "--harbor-night": "#101D26",
    "--tide-deep": "#16323E",
    "--tide-line": "#24485A",
    "--foam": "#E8F1F2",
    "--mist": "#9FB8BE",
    "--signal-cyan": "#3FD2C7",
    "--beacon-amber": "#FFB454",
    "--ember": "#E5735A",
}

# Copy that must never be hardcoded in the shell (it lives in locales/*.json).
ENGLISH_UI_LITERALS = [
    '"Signal Tower"',
    '"Logbook"',
    '"Notes"',
    '"Settings"',
    '"Pause"',
    '"Resume"',
    "signal lost",
    "The world is paused",
]


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


class TestStaticServing:
    @pytest.mark.parametrize(
        "path",
        [
            "/static/js/net.js",
            "/static/js/world.js",
            "/static/js/bridge.js",
            "/static/js/i18n.js",
            "/static/css/tokens.css",
            "/static/css/bridge.css",
            "/static/vendor/pixi.min.js",
        ],
    )
    def test_frontend_files_served_200(self, client: TestClient, path: str) -> None:
        assert client.get(path).status_code == 200

    def test_index_references_modules_and_styles(self, client: TestClient) -> None:
        html = client.get("/").text
        for ref in ("js/world.js", "js/bridge.js", "css/tokens.css", "css/bridge.css"):
            assert ref in html


class TestDesignTokens:
    def test_tokens_css_contains_all_8_exact_hex_values(self) -> None:
        css = (STATIC / "css" / "tokens.css").read_text()
        for token, value in DESIGN_TOKENS.items():
            assert f"{token}: {value}" in css, f"missing {token}: {value}"


class TestI18nDiscipline:
    def test_bridge_js_has_no_hardcoded_english_ui_literals(self) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        for literal in ENGLISH_UI_LITERALS:
            assert literal not in source, f"hardcoded UI literal: {literal}"

    def test_bridge_js_resolves_strings_via_catalog(self) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        assert "t(" in source
        for key in ("tab.mate", "hud.day", "hud.spend_today", "state.reconnecting"):
            assert key in source, f"bridge.js never references catalog key {key}"

    def test_locale_catalogs_have_identical_key_sets(self) -> None:
        en = json.loads((REPO_ROOT / "locales" / "en.json").read_text())
        ja = json.loads((REPO_ROOT / "locales" / "ja.json").read_text())
        assert set(en) == set(ja)

    def test_i18n_js_falls_back_to_english(self) -> None:
        source = (STATIC / "js" / "i18n.js").read_text()
        assert "en" in source
        assert "fallback" in source.lower()


class TestRendererContract:
    def test_world_js_uses_nearest_scale_and_band_tints(self) -> None:
        source = (STATIC / "js" / "world.js").read_text()
        assert "NEAREST" in source
        for tint in ("FFD9A8", "FF9868", "1B3A66", "FFB454"):
            assert tint.lower() in source.lower(), f"missing band/beam color {tint}"

    def test_world_js_emits_selection_events_not_popups(self) -> None:
        source = (STATIC / "js" / "world.js").read_text()
        assert "peer-selected" in source
        assert "signal-tower" in source


class TestClientApis:
    def test_api_map_serves_port_json(self, client: TestClient) -> None:
        response = client.get("/api/map")
        assert response.status_code == 200
        data = response.json()
        assert set(data) >= {"ground", "collision", "zones", "waypoints"}

    @pytest.mark.parametrize("locale", ["en", "ja"])
    def test_api_locales_serves_catalogs(self, client: TestClient, locale: str) -> None:
        response = client.get(f"/api/locales/{locale}")
        assert response.status_code == 200
        assert response.json()["tab.mate"]

    def test_api_locales_rejects_unknown_locale(self, client: TestClient) -> None:
        assert client.get("/api/locales/fr").status_code == 404

    def test_api_config_exposes_locale(self, client: TestClient) -> None:
        response = client.get("/api/config")
        assert response.status_code == 200
        assert response.json()["locale"] == "en"
