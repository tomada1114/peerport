"""Tests for the placeholder pixel-asset generator and palette (#28).

Per D-G5, assets are generated programmatically by
`scripts/generate_placeholders.py` (Pillow) from the color palette already
shipped in `static/css/tokens.css` and `static/js/world.js`. These tests
cover both the generator's pure logic (palette dedupe/cap, sheet
dimensions) and the already-committed output (files exist with the exact
pixel dimensions, and the server serves them).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from peerport.server.app import create_app

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).parent.parent
STATIC = REPO_ROOT / "src" / "peerport" / "server" / "static"
ASSETS = STATIC / "assets"
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate_placeholders.py"

CHARACTER_PEERS = ("beacon", "tug", "bell", "echo")


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_placeholders", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        message = f"cannot load generator spec from {SCRIPT_PATH}"
        raise RuntimeError(message)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    return _load_generator()


@pytest.fixture(scope="module")
def css_text() -> str:
    return (STATIC / "css" / "tokens.css").read_text()


@pytest.fixture(scope="module")
def world_js_text() -> str:
    return (STATIC / "js" / "world.js").read_text()


class TestPaletteReusesShippedHexes:
    def test_parse_tokens_extracts_all_8_design_tokens(
        self, generator: ModuleType, css_text: str
    ) -> None:
        tokens = generator.parse_tokens(css_text)
        assert tokens["beacon-amber"] == "#FFB454"
        assert tokens["signal-cyan"] == "#3FD2C7"
        assert len(tokens) == 8

    def test_build_palette_reuses_exact_hexes_and_stays_under_cap(
        self, generator: ModuleType, css_text: str, world_js_text: str
    ) -> None:
        tokens = generator.parse_tokens(css_text)
        world_hexes = generator.all_world_hexes(world_js_text)
        palette = generator.build_palette(tokens, world_hexes)
        hexes = {str(entry["hex"]) for entry in palette}

        assert "#FFB454" in hexes  # amber
        assert "#E5735A" in hexes  # coral
        assert "#3FD2C7" in hexes  # cyan-teal
        assert "#1B3A66" in hexes  # night navy (BAND_TINTS, not a CSS token)
        assert len(hexes) <= 32

    def test_build_palette_tags_every_color_cool_warm_or_neon(
        self, generator: ModuleType, css_text: str, world_js_text: str
    ) -> None:
        tokens = generator.parse_tokens(css_text)
        world_hexes = generator.all_world_hexes(world_js_text)
        palette = generator.build_palette(tokens, world_hexes)
        for entry in palette:
            assert entry["zone"] in {"cool", "warm", "neon", "neutral"}

    def test_build_palette_rejects_more_than_32_colors(
        self, generator: ModuleType
    ) -> None:
        oversized_tokens = {f"c{i}": f"#{i:06X}" for i in range(33)}
        with pytest.raises(ValueError, match="exceeds cap"):
            generator.build_palette(oversized_tokens, [])


class TestTileSheetGeneration:
    @pytest.mark.parametrize(
        "legend_name",
        ["GROUND_LEGEND", "WATER_LEGEND", "PROPS_LEGEND"],
    )
    def test_tile_sheet_is_16px_grid_aligned(
        self, generator: ModuleType, world_js_text: str, legend_name: str
    ) -> None:
        tile_colors = generator.parse_js_object(world_js_text, "TILE_COLORS")
        legend = getattr(generator, legend_name)
        sheet = generator.draw_tile_sheet(legend, tile_colors)

        assert sheet.height == generator.TILE == 16
        assert sheet.width == generator.TILE * len(legend)
        assert sheet.width % generator.TILE == 0


class TestCharacterSheetGeneration:
    @pytest.mark.parametrize("peer", CHARACTER_PEERS)
    def test_character_sheet_has_9_frames_at_16x24(
        self, generator: ModuleType, world_js_text: str, peer: str
    ) -> None:
        peer_colors = generator.parse_js_object(world_js_text, "PEER_COLORS")
        sheet = generator.draw_character_sheet(peer, peer_colors[peer])

        assert len(generator.FRAME_POSES) >= 9
        assert sheet.height == generator.SPRITE_H == 24
        assert sheet.width == generator.SPRITE_W * len(generator.FRAME_POSES)
        assert sheet.width % generator.SPRITE_W == 0

    def test_echo_rimlight_is_a_single_16x24_frame_in_one_color(
        self, generator: ModuleType
    ) -> None:
        rim = generator.draw_rimlight_frame(
            generator.RIMLIGHT_COLOR, generator.PEER_SILHOUETTES["echo"]
        )

        assert rim.size == (generator.SPRITE_W, generator.SPRITE_H)
        opaque_colors = {
            rim.getpixel((x, y))
            for x in range(rim.width)
            for y in range(rim.height)
            if rim.getpixel((x, y))[3] > 0
        }
        assert len(opaque_colors) == 1

    def test_no_sheet_exists_for_friends_kai_or_mia(self) -> None:
        for friend in ("kai", "mia"):
            assert not (ASSETS / "characters" / f"{friend}.png").exists()


class TestCommittedAssetsExistWithExpectedDimensions:
    @pytest.mark.parametrize(
        ("relative_path", "expected_size"),
        [
            ("tiles/ground.png", (16 * 4, 16)),
            ("tiles/water.png", (16 * 4, 16)),
            ("tiles/props.png", (16 * 4, 16)),
            ("characters/beacon.png", (16 * 9, 24)),
            ("characters/tug.png", (16 * 9, 24)),
            ("characters/bell.png", (16 * 9, 24)),
            ("characters/echo.png", (16 * 9, 24)),
            ("characters/echo_rimlight.png", (16, 24)),
        ],
    )
    def test_committed_png_has_exact_pixel_dimensions(
        self, relative_path: str, expected_size: tuple[int, int]
    ) -> None:
        path = ASSETS / relative_path
        assert path.exists(), f"missing committed asset {relative_path}"
        with Image.open(path) as image:
            assert image.size == expected_size
            assert image.width % 16 == 0
            assert image.height % 16 == 0 or image.height == 24

    def test_palette_json_exists_and_is_within_cap(self) -> None:
        data = json.loads((ASSETS / "palette" / "palette.json").read_text())
        hexes = {entry["hex"] for entry in data["colors"]}

        assert len(data["colors"]) <= 32
        assert "#FFB454" in hexes
        assert "#3FD2C7" in hexes

    def test_palette_png_swatch_matches_palette_json_color_count(self) -> None:
        data = json.loads((ASSETS / "palette" / "palette.json").read_text())
        with Image.open(ASSETS / "palette" / "palette.png") as swatch:
            assert swatch.height == 16
            assert swatch.width == 16 * len(data["colors"])

    def test_credits_md_lists_every_committed_png_as_cc0(self) -> None:
        credits_text = (ASSETS / "CREDITS.md").read_text()
        for path in ASSETS.rglob("*.png"):
            rel = path.relative_to(STATIC).as_posix()
            assert f"static/{rel}" in credits_text, (
                f"CREDITS.md missing entry for {rel}"
            )
        assert "CC0" in credits_text


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


class TestServerServesGeneratedAssets:
    @pytest.mark.parametrize(
        "path",
        [
            "/static/assets/palette/palette.json",
            "/static/assets/palette/palette.png",
            "/static/assets/tiles/ground.png",
            "/static/assets/tiles/water.png",
            "/static/assets/tiles/props.png",
            "/static/assets/characters/beacon.png",
            "/static/assets/characters/tug.png",
            "/static/assets/characters/bell.png",
            "/static/assets/characters/echo.png",
            "/static/assets/characters/echo_rimlight.png",
            "/static/assets/CREDITS.md",
        ],
    )
    def test_asset_served_200(self, client: TestClient, path: str) -> None:
        assert client.get(path).status_code == 200
