"""Generate placeholder pixel-art assets and the master color palette (#28).

Per decision D-G5 (pre-authorized in issue #28's implementation notes),
every asset in this ticket is generated programmatically with Pillow rather
than hand-drawn or sourced from the web: self-made art is CC0 by
construction, and that license is recorded in `assets/CREDITS.md` (also
generated here, so it can never drift from the actual file set).

No color is ever invented: the master palette and every tile/character
pixel reuse — verbatim — the hex values already shipped in
`static/css/tokens.css` (the 8 design tokens) and `static/js/world.js`
(`TILE_COLORS`, `PEER_COLORS`, `BAND_TINTS`, `BEAM_COLOR`). See
prototype-design.md §2.2/§9 for the palette/asset spec this fulfills.

Run directly: `uv run python scripts/generate_placeholders.py`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "src" / "peerport" / "server" / "static"
TOKENS_CSS_PATH = STATIC_DIR / "css" / "tokens.css"
WORLD_JS_PATH = STATIC_DIR / "js" / "world.js"
ASSETS_DIR = STATIC_DIR / "assets"
PALETTE_DIR = ASSETS_DIR / "palette"
TILES_DIR = ASSETS_DIR / "tiles"
CHARACTERS_DIR = ASSETS_DIR / "characters"
CREDITS_PATH = ASSETS_DIR / "CREDITS.md"

TILE = 16
SPRITE_W = 16
SPRITE_H = 24
MAX_PALETTE_COLORS = 32

# Manual cool/warm/neon zoning (prototype-design.md §2.2 REQ-002) for the 16
# hexes already shipped in tokens.css/world.js. Every hex this script draws
# with MUST be a key here — a KeyError means an unreviewed color slipped
# into the frontend and needs a human zoning pass before this script can
# bless it as part of the master palette.
COLOR_ZONES: dict[str, str] = {
    "#0B141B": "cool",
    "#101D26": "cool",
    "#16323E": "cool",
    "#24485A": "cool",
    "#2B5468": "cool",
    "#0E2733": "cool",
    "#9FB8BE": "cool",
    "#1C3A48": "cool",
    "#1B3A66": "cool",
    "#FFB454": "warm",
    "#E5735A": "warm",
    "#FFD9A8": "warm",
    "#FF9868": "warm",
    "#E8F1F2": "neutral",
    "#FFFFFF": "neutral",
    "#3FD2C7": "neon",
}

TOKEN_RE = re.compile(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})")
GLOBAL_HEX_RE = re.compile(r"0x([0-9a-fA-F]{6})\b")
JS_OBJECT_ENTRY_RE = re.compile(r'(?:"([^"]+)"|(\w+))\s*:\s*0x([0-9a-fA-F]{6})')

# Biome-layer tile sheets (REQ-003), each a horizontal strip of flat,
# on-palette 16x16 tiles keyed by the existing TILE_COLORS legend chars from
# world.js. No collision/zone data is baked in — that lives in
# data/map/port.json (#12) — these are pixel-only.
GROUND_LEGEND: tuple[str, ...] = ("#", "=", "D", "1")
WATER_LEGEND: tuple[str, ...] = (",", ".", "~", "M")
PROPS_LEGEND: tuple[str, ...] = ("o", "T", "L", "2")

PEER_ORDER: tuple[str, ...] = ("beacon", "tug", "bell", "echo")
RIMLIGHT_PEER = "echo"
RIMLIGHT_COLOR = "#3FD2C7"  # --signal-cyan; neon, reserved for Echo's rim-light.


@dataclass(frozen=True, slots=True)
class SilhouetteProfile:
    """Per-peer body proportions, so the 4 map-visible peers stay silhouette-distinct."""

    head_w: int
    body_w: int


@dataclass(frozen=True, slots=True)
class FramePose:
    """One authored frame in a character sheet's walk/idle/emote cycle."""

    name: str
    leg_offset: int
    arm_up: bool
    bob: int


# 2 walk-down + 2 walk-up + 2 walk-side (mirrored at runtime, not authored
# twice) + 2-frame idle loop + 1 emote-base frame = 9 frames (REQ-004).
FRAME_POSES: tuple[FramePose, ...] = (
    FramePose("walk_down_0", 0, False, 0),
    FramePose("walk_down_1", 1, False, 0),
    FramePose("walk_up_0", 0, False, 0),
    FramePose("walk_up_1", 1, False, 0),
    FramePose("walk_side_0", 0, False, 0),
    FramePose("walk_side_1", 1, False, 0),
    FramePose("idle_0", 0, False, 0),
    FramePose("idle_1", 0, False, -1),
    FramePose("emote", 0, True, 0),
)

# Flat-silhouette placeholder proportions (placeholder-honest: on-grid,
# on-palette, minimal). Widths vary per peer so the 4 map-visible peers stay
# distinguishable by silhouette alone even with peer-identifying color
# removed.
PEER_SILHOUETTES: dict[str, SilhouetteProfile] = {
    "beacon": SilhouetteProfile(head_w=6, body_w=8),
    "tug": SilhouetteProfile(head_w=10, body_w=12),
    "bell": SilhouetteProfile(head_w=8, body_w=10),
    "echo": SilhouetteProfile(head_w=6, body_w=6),
}


def parse_tokens(css_text: str) -> dict[str, str]:
    """Parse `--name: #HEX;` design tokens out of tokens.css.

    Args:
        css_text: Raw contents of `static/css/tokens.css`.

    Returns:
        Mapping of token name (without the `--` prefix) to uppercase hex.
    """
    return {name: value.upper() for name, value in TOKEN_RE.findall(css_text)}


def parse_js_object(js_text: str, const_name: str) -> dict[str, str]:
    """Parse a `const NAME = { key: 0xHEX, ... };` color object out of world.js.

    Args:
        js_text: Raw contents of `static/js/world.js`.
        const_name: The JS constant name to locate (e.g. `"TILE_COLORS"`).

    Returns:
        Mapping of each object key to its uppercase `#HEX` color.

    Raises:
        ValueError: If no `const {const_name} = {{ ... }};` block is found.
    """
    pattern = re.compile(rf"const {const_name} = \{{(.*?)\}};", re.DOTALL)
    match = pattern.search(js_text)
    if match is None:
        message = f"{const_name} block not found in world.js"
        raise ValueError(message)
    entries: dict[str, str] = {}
    for quoted_key, bare_key, hex_value in JS_OBJECT_ENTRY_RE.findall(match.group(1)):
        key = quoted_key or bare_key
        entries[key] = f"#{hex_value.upper()}"
    return entries


def all_world_hexes(js_text: str) -> list[str]:
    """Collect every distinct `0xHEX` literal in world.js, in first-seen order.

    Args:
        js_text: Raw contents of `static/js/world.js`.

    Returns:
        Deduplicated list of uppercase `#HEX` colors (tiles, peers, day-band
        tints, and the beam color together).
    """
    seen: dict[str, None] = {}
    for hex_value in GLOBAL_HEX_RE.findall(js_text):
        seen.setdefault(f"#{hex_value.upper()}", None)
    return list(seen)


def build_palette(
    tokens: dict[str, str], world_hexes: list[str]
) -> list[dict[str, str | None]]:
    """Build the deduplicated, capped master palette (REQ-001/REQ-002).

    Args:
        tokens: Token name -> hex, from `parse_tokens`.
        world_hexes: Deduplicated hex list, from `all_world_hexes`.

    Returns:
        One entry per distinct color: `hex`, the owning design-token `name`
        (or `None` for world-only colors), and its cool/warm/neon `zone`.

    Raises:
        ValueError: If the union exceeds `MAX_PALETTE_COLORS`.
    """
    reverse_tokens = {value: name for name, value in tokens.items()}
    ordered: dict[str, str | None] = {}
    for hex_value in (*tokens.values(), *world_hexes):
        ordered.setdefault(hex_value, reverse_tokens.get(hex_value))
    if len(ordered) > MAX_PALETTE_COLORS:
        message = (
            f"palette has {len(ordered)} colors, exceeds cap of {MAX_PALETTE_COLORS}"
        )
        raise ValueError(message)
    return [
        {"hex": hex_value, "token": token_name, "zone": COLOR_ZONES[hex_value]}
        for hex_value, token_name in ordered.items()
    ]


def hex_to_rgb(hex_value: str) -> tuple[int, int, int]:
    """Convert a `#RRGGBB` string to an `(r, g, b)` int tuple."""
    value = hex_value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def draw_palette_swatch(colors: list[dict[str, str | None]]) -> Image.Image:
    """Render a horizontal 16x16-per-swatch strip, one square per palette color."""
    image = Image.new("RGBA", (TILE * len(colors), TILE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for index, entry in enumerate(colors):
        rgb = hex_to_rgb(str(entry["hex"]))
        x0 = index * TILE
        draw.rectangle((x0, 0, x0 + TILE - 1, TILE - 1), fill=(*rgb, 255))
    return image


def draw_tile_sheet(
    legend_keys: tuple[str, ...], tile_colors: dict[str, str]
) -> Image.Image:
    """Render a flat, on-palette 16x16-per-tile horizontal strip.

    Args:
        legend_keys: TILE_COLORS legend chars to include, in sheet order.
        tile_colors: Legend char -> hex, from `parse_js_object`.

    Returns:
        One plain, single-color 16x16 tile per legend key (placeholder-honest:
        minimal, on-grid, on-palette — no collision/zone markers baked in).
    """
    image = Image.new("RGBA", (TILE * len(legend_keys), TILE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for index, key in enumerate(legend_keys):
        rgb = hex_to_rgb(tile_colors[key])
        x0 = index * TILE
        draw.rectangle((x0, 0, x0 + TILE - 1, TILE - 1), fill=(*rgb, 255))
    return image


def draw_frame(color: str, profile: SilhouetteProfile, pose: FramePose) -> Image.Image:
    """Draw a single 16x24 flat-silhouette character frame.

    Args:
        color: The peer's cast-sheet hex (from `PEER_COLORS`).
        profile: Head/body widths, distinct per peer for silhouette contrast.
        pose: Which walk/idle/emote frame to render.

    Returns:
        A 16x24 RGBA frame; transparent outside the silhouette.
    """
    image = Image.new("RGBA", (SPRITE_W, SPRITE_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    rgb = (*hex_to_rgb(color), 255)
    top = pose.bob
    head_x0 = (SPRITE_W - profile.head_w) // 2
    body_x0 = (SPRITE_W - profile.body_w) // 2
    leg_w = 3
    draw.rectangle((head_x0, top, head_x0 + profile.head_w - 1, top + 5), fill=rgb)
    draw.rectangle((body_x0, top + 6, body_x0 + profile.body_w - 1, top + 17), fill=rgb)
    left_leg_top = top + 18 + pose.leg_offset
    right_leg_top = top + 18 + (1 - pose.leg_offset)
    draw.rectangle((body_x0, left_leg_top, body_x0 + leg_w - 1, top + 23), fill=rgb)
    draw.rectangle(
        (
            body_x0 + profile.body_w - leg_w,
            right_leg_top,
            body_x0 + profile.body_w - 1,
            top + 23,
        ),
        fill=rgb,
    )
    if pose.arm_up:
        arm_x0 = min(body_x0 + profile.body_w, SPRITE_W - 3)
        draw.rectangle((arm_x0, top + 6, arm_x0 + 2, top + 9), fill=rgb)
    return image


def draw_character_sheet(peer: str, color: str) -> Image.Image:
    """Render one peer's full character sheet: all `FRAME_POSES`, side by side."""
    profile = PEER_SILHOUETTES[peer]
    frames = [draw_frame(color, profile, pose) for pose in FRAME_POSES]
    sheet = Image.new("RGBA", (SPRITE_W * len(frames), SPRITE_H), (0, 0, 0, 0))
    for index, frame in enumerate(frames):
        sheet.paste(frame, (index * SPRITE_W, 0), frame)
    return sheet


def draw_rimlight_frame(neon_hex: str, profile: SilhouetteProfile) -> Image.Image:
    """Render Echo's 1-color neon rim-light variant (REQ-006): outline only."""
    image = Image.new("RGBA", (SPRITE_W, SPRITE_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    rgb = (*hex_to_rgb(neon_hex), 255)
    head_x0 = (SPRITE_W - profile.head_w) // 2
    body_x0 = (SPRITE_W - profile.body_w) // 2
    draw.rectangle((head_x0, 0, head_x0 + profile.head_w - 1, 5), outline=rgb, width=1)
    draw.rectangle((body_x0, 6, body_x0 + profile.body_w - 1, 17), outline=rgb, width=1)
    draw.rectangle(
        (body_x0, 18, body_x0 + profile.body_w - 1, 23), outline=rgb, width=1
    )
    return image


def write_credits(asset_paths: list[Path]) -> None:
    """Write `assets/CREDITS.md`, one row per generated asset file (REQ-009).

    Args:
        asset_paths: Every generated asset file, to be listed with its
            CC0-1.0 license and self-made attribution.
    """
    lines = [
        "# Asset Credits",
        "",
        "All assets under `static/assets/` are generated programmatically by",
        "`scripts/generate_placeholders.py` (Pillow) from the color palette",
        "already shipped in `static/css/tokens.css` and `static/js/world.js`.",
        "Every asset here is self-made and licensed CC0-1.0; nothing was",
        "downloaded, scraped, or otherwise sourced from the web.",
        "",
        "No font is used by any asset in this set (D-G4: pixel font",
        "licensing is a separate, human-vetted decision).",
        "",
        "| Asset | License | Author | Source |",
        "|---|---|---|---|",
    ]
    for path in asset_paths:
        rel = path.relative_to(STATIC_DIR).as_posix()
        lines.append(
            f"| `static/{rel}` | CC0-1.0 | tomada (self-made) "
            "| generated by `scripts/generate_placeholders.py` |"
        )
    CREDITS_PATH.write_text("\n".join(lines) + "\n")


def main() -> None:
    """Generate the palette, tile sheets, character sheets, and CREDITS.md."""
    css_text = TOKENS_CSS_PATH.read_text()
    js_text = WORLD_JS_PATH.read_text()

    tokens = parse_tokens(css_text)
    tile_colors = parse_js_object(js_text, "TILE_COLORS")
    peer_colors = parse_js_object(js_text, "PEER_COLORS")
    palette = build_palette(tokens, all_world_hexes(js_text))

    PALETTE_DIR.mkdir(parents=True, exist_ok=True)
    TILES_DIR.mkdir(parents=True, exist_ok=True)
    CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)

    (PALETTE_DIR / "palette.json").write_text(
        json.dumps({"max_colors": MAX_PALETTE_COLORS, "colors": palette}, indent=1)
        + "\n"
    )
    draw_palette_swatch(palette).save(PALETTE_DIR / "palette.png")

    for name, legend in (
        ("ground", GROUND_LEGEND),
        ("water", WATER_LEGEND),
        ("props", PROPS_LEGEND),
    ):
        draw_tile_sheet(legend, tile_colors).save(TILES_DIR / f"{name}.png")

    for peer in PEER_ORDER:
        draw_character_sheet(peer, peer_colors[peer]).save(
            CHARACTERS_DIR / f"{peer}.png"
        )

    rimlight = draw_rimlight_frame(RIMLIGHT_COLOR, PEER_SILHOUETTES[RIMLIGHT_PEER])
    rimlight.save(CHARACTERS_DIR / f"{RIMLIGHT_PEER}_rimlight.png")

    generated = sorted(
        p for p in ASSETS_DIR.rglob("*") if p.is_file() and p.suffix != ".md"
    )
    write_credits(generated)

    print(f"wrote {len(palette)} palette colors to {PALETTE_DIR.relative_to(ROOT)}")
    print(f"wrote 3 tile sheets to {TILES_DIR.relative_to(ROOT)}")
    print(
        f"wrote {len(PEER_ORDER)} character sheets + 1 rim-light to "
        f"{CHARACTERS_DIR.relative_to(ROOT)}"
    )


if __name__ == "__main__":
    main()
