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

    def test_bridge_css_applies_the_body_baseline_to_long_form_text(self) -> None:
        """15px / line-height 1.7 (ja-comfortable) body baseline.

        Per prototype-design.md §2.4, this applies to anything read for
        minutes -- chat, mail, notes, BBS, logbook.
        """
        css = (STATIC / "css" / "bridge.css").read_text()
        for selector in (
            ".chat-bubble",
            ".mail-detail p",
            ".notes-content",
            ".logbook-entry",
            ".board-post p",
        ):
            block = css.split(f"{selector} {{", 1)[1].split("}", 1)[0]
            assert "font-size: 15px;" in block, f"{selector} missing 15px baseline"
            assert "line-height: 1.7;" in block, f"{selector} missing 1.7 line-height"


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

    def test_bridge_js_resolves_logbook_strings_via_catalog(self) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        for key in ("logbook.empty", "logbook.while_away", "logbook.chronicle"):
            assert key in source, f"bridge.js never references catalog key {key}"

    def test_bridge_js_hud_clock_uses_the_dedicated_world_date_key(self) -> None:
        """prototype-design.md §8.4: world-dates are "Day 12, Dusk"/「12日目・夕」.

        `_refreshHud()` must resolve through `date.world_day` (finding)
        rather than hand-assembling the label with a hardcoded separator.
        """
        source = (STATIC / "js" / "bridge.js").read_text()
        assert 't("date.world_day", {' in source

    def test_bridge_js_resolves_onboarding_locale_labels_via_catalog(self) -> None:
        """Locale-picker labels must not bypass the catalog (finding)."""
        source = (STATIC / "js" / "bridge.js").read_text()
        assert "onboarding.locale.${loc}" in source
        for locale in ("en", "ja"):
            catalog = json.loads((REPO_ROOT / "locales" / f"{locale}.json").read_text())
            assert catalog[f"onboarding.locale.{locale}"]

    def test_locale_catalogs_have_identical_key_sets(self) -> None:
        en = json.loads((REPO_ROOT / "locales" / "en.json").read_text())
        ja = json.loads((REPO_ROOT / "locales" / "ja.json").read_text())
        assert set(en) == set(ja)

    def test_i18n_js_falls_back_to_english(self) -> None:
        source = (STATIC / "js" / "i18n.js").read_text()
        assert "en" in source
        assert "fallback" in source.lower()


class TestChatStreamResetOnDisconnect:
    """A dropped WS mid-stream must not splice into the next reply (finding).

    `chat_done` for an in-flight response never arrives once the socket
    drops, so `streamingBubble` has to be cleared right away rather than
    staying a stale reference the next `chat_delta` stream appends onto.
    """

    def test_bridge_js_defines_a_streaming_chat_reset(self) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        assert "resetStreamingChat() {" in source
        assert "this.streamingBubble = null;" in source

    def test_index_resets_streaming_chat_on_disconnect(self) -> None:
        html = (STATIC / "index.html").read_text()
        assert "bridge.resetStreamingChat()" in html


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

    def test_world_js_tint_crossfade_snaps_instantly_under_reduced_motion(
        self,
    ) -> None:
        """prototype-design.md §8.3: reduced motion -> tint crossfades instant."""
        source = (STATIC / "js" / "world.js").read_text()
        assert "this.tint.alpha = this.tintTo.alpha;" in source

    def test_world_js_speech_bubble_skips_pop_in_under_reduced_motion(self) -> None:
        """prototype-design.md §8.3: reduced motion -> bubbles/idle anims static."""
        source = (STATIC / "js" / "world.js").read_text()
        assert source.count("reducedMotion") >= 6


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


class TestLogbookFrontendContract:
    def test_index_dispatches_digest_and_logbook_updated_frames(self) -> None:
        html = (STATIC / "index.html").read_text()
        assert "applyDigest" in html
        assert "logbook_updated" in html
        assert "peerport:logbook-updated" in html

    def test_bridge_js_refreshes_logbook_via_api(self) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        assert "/api/logbook" in source
        assert "peerport:logbook-updated" in source
        assert "applyDigest" in source


class TestMailFrontendContract:
    def test_index_dispatches_mail_updated_frame(self) -> None:
        html = (STATIC / "index.html").read_text()
        assert "mail_received" in html
        assert "peerport:mail-updated" in html

    def test_bridge_js_resolves_mail_strings_via_catalog(self) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        for key in ("mail.empty", "mail.reply.placeholder", "mail.send"):
            assert key in source, f"bridge.js never references catalog key {key}"

    def test_bridge_js_lists_and_replies_via_api(self) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        assert "/api/mail" in source
        assert "/reply" in source
        assert "/read" in source
        assert "peerport:mail-updated" in source

    def test_mail_css_has_distinct_sender_edge_colors(self) -> None:
        css = (STATIC / "css" / "bridge.css").read_text()
        assert ".sender-kai" in css
        assert ".sender-mia" in css


class TestNotesFrontendContract:
    def test_index_dispatches_notes_updated_and_search_events(self) -> None:
        html = (STATIC / "index.html").read_text()
        assert "notes_updated" in html
        assert "peerport:notes-updated" in html
        assert "applySearchFlavor" in html

    def test_bridge_js_resolves_notes_strings_via_catalog(self) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        for key in (
            "notes.empty",
            "notes.filed_by",
            "notes.delete",
            "notes.delete.confirm",
            "mate.searching",
            "mate.filed_note",
        ):
            assert key in source, f"bridge.js never references catalog key {key}"

    def test_bridge_js_lists_reads_and_deletes_via_api(self) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        assert "/api/notes" in source
        assert "DELETE" in source
        assert "peerport:notes-updated" in source

    def test_notes_delete_button_uses_ember_token(self) -> None:
        css = (STATIC / "css" / "bridge.css").read_text()
        assert ".notes-delete-button" in css
        assert "var(--ember)" in css


class TestDegradedStatesFrontend:
    """Proxy verification for #27's diegetic fog/low-power/hard-stop UI."""

    def test_index_dispatches_state_frames_to_world_and_bridge(self) -> None:
        html = (STATIC / "index.html").read_text()
        assert 'frame.t === "state"' in html
        assert "world.applyStateFrame(frame)" in html
        assert "bridge.applyStateFrame(frame)" in html

    def test_bridge_js_resolves_degraded_state_strings_via_catalog(self) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        for key in (
            "state.fog",
            "state.fog.detail",
            "state.low_power",
            "state.hard_stop",
            "tab.settings",
        ):
            assert key in source, f"bridge.js never references catalog key {key}"

    def test_bridge_js_toggles_spend_chip_low_power_and_hard_stop_classes(
        self,
    ) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        assert 'classList.toggle("low-power"' in source
        assert 'classList.toggle("hard-stop"' in source

    def test_bridge_js_hard_stop_banner_links_to_settings_tab(self) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        assert 'this.switchTab("settings")' in source
        assert "hardStopBanner" in source

    def test_bridge_js_fog_detail_substitutes_status(self) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        assert 't("state.fog.detail", {' in source
        assert "status: String(status" in source

    def test_world_js_fog_overlay_uses_spec_color_and_alpha(self) -> None:
        source = (STATIC / "js" / "world.js").read_text()
        assert "0x5a7a82" in source.lower()
        assert "FOG_TARGET_ALPHA = 0.4" in source

    def test_world_js_fog_pools_over_sea_and_mist_tiles_first(self) -> None:
        source = (STATIC / "js" / "world.js").read_text()
        assert 'FOG_SEA_TILES = new Set(["~", "M"])' in source
        assert "FOG_SEA_RAMP_MS" in source
        assert "FOG_TOWN_RAMP_MS" in source

    def test_world_js_hard_stop_locks_to_night_tint(self) -> None:
        source = (STATIC / "js" / "world.js").read_text()
        assert "hardStop" in source
        assert "BAND_TINTS.night" in source

    def test_world_js_applies_state_frames(self) -> None:
        source = (STATIC / "js" / "world.js").read_text()
        assert "applyStateFrame(frame)" in source

    def test_index_resyncs_degraded_state_from_every_snapshot(self) -> None:
        """A reconnecting client resyncs fog/hard-stop/low-power (finding).

        The snapshot is the guaranteed first message on every (re)connect
        (net.js), so `onSnapshot` must feed it to both renderers instead
        of only ever reacting to a live `state` frame that may have been
        missed while disconnected.
        """
        html = (STATIC / "index.html").read_text()
        assert "world.applyDegradedSnapshot(frame)" in html
        assert "bridge.applyDegradedSnapshot(frame)" in html

    def test_bridge_js_applies_degraded_snapshot_via_existing_handlers(self) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        assert "applyDegradedSnapshot(frame)" in source
        assert "frame.fog" in source
        assert "frame.hard_stop" in source

    def test_world_js_applies_degraded_snapshot_via_existing_handlers(self) -> None:
        source = (STATIC / "js" / "world.js").read_text()
        assert "applyDegradedSnapshot(frame)" in source

    def test_no_blocking_modal_introduced_for_degraded_states(self) -> None:
        """REQ-009: the harbor stays visible — never a blocking modal.

        Checks for an actual modal/backdrop *implementation* (a CSS
        selector or a className literal), not just the word "modal"
        showing up in an explanatory code comment.
        """
        css = (STATIC / "css" / "bridge.css").read_text().lower()
        assert ".modal" not in css
        assert ".backdrop" not in css
        for path in ("js/bridge.js", "js/world.js"):
            source = (STATIC / path).read_text().lower()
            assert '"modal' not in source
            assert '"backdrop' not in source

    def test_locale_catalogs_carry_every_degraded_state_key(self) -> None:
        for locale in ("en", "ja"):
            catalog = json.loads((REPO_ROOT / "locales" / f"{locale}.json").read_text())
            for key in (
                "state.fog",
                "state.fog.detail",
                "state.reconnecting",
                "state.low_power",
                "state.hard_stop",
            ):
                assert key in catalog, f"{locale}.json missing {key}"


class TestOnboardingFrontendContract:
    """Proxy verification for #29's first-run flow (order per D-018)."""

    def test_bridge_js_resolves_onboarding_strings_via_catalog(self) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        for key in (
            "onboarding.api_key.title",
            "onboarding.locale.title",
            "onboarding.keeper_name.title",
        ):
            assert key in source, f"bridge.js never references catalog key {key}"

    def test_bridge_js_drives_the_flow_via_the_onboarding_and_settings_apis(
        self,
    ) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        assert "/api/onboarding" in source
        assert "/api/settings" in source

    def test_bridge_js_dispatches_onboarding_complete_event_once_per_finish(
        self,
    ) -> None:
        """REQ-008: this ticket only triggers the beam-sweep beat as an event."""
        source = (STATIC / "js" / "bridge.js").read_text()
        assert "peerport:onboarding-complete" in source
        assert source.count("peerport:onboarding-complete") == 1

    def test_onboarding_overlay_dims_the_map_without_ever_hiding_it(self) -> None:
        """REQ-009: the map stays present/rendering, only visually dimmed."""
        css = (STATIC / "css" / "bridge.css").read_text()
        assert ".onboarding-overlay" in css
        source = (STATIC / "js" / "bridge.js").read_text()
        assert "mapPane.classList.add" not in source
        assert "mapPane.style.display" not in source

    def test_no_blocking_modal_class_introduced_for_onboarding(self) -> None:
        css = (STATIC / "css" / "bridge.css").read_text().lower()
        assert ".modal" not in css
        assert ".backdrop" not in css

    def test_mate_naming_defaults_to_beacon(self) -> None:
        """REQ-007: Beacon is the default unless the Keeper renames Mate."""
        source = (STATIC / "js" / "bridge.js").read_text()
        assert '"Beacon"' in source

    def test_i18n_js_supports_a_runtime_locale_switch(self) -> None:
        """REQ-005: the step-2 locale choice must apply before steps 3-4."""
        source = (STATIC / "js" / "i18n.js").read_text()
        assert "export async function setLocale" in source

    def test_bridge_js_switches_locale_when_choosing_it_in_onboarding(self) -> None:
        source = (STATIC / "js" / "bridge.js").read_text()
        assert "setLocale(" in source
