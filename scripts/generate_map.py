"""Generate `data/map/port.json` from the ASCII grid in `docs/design/map-layout.md`.

The ASCII grid in the design doc is the authoring source (D-019); this script
transcribes it into the runtime map data: ground, collision, zones, waypoints.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = ROOT / "docs" / "design" / "map-layout.md"
OUT_PATH = ROOT / "data" / "map" / "port.json"

GRID_WIDTH = 40
GRID_HEIGHT = 30
BLOCKED_CHARS = frozenset({"#", "~", "o"})
LEGEND_CHARS = frozenset(
    {"#", ",", ".", "=", "~", "o", "T", "L", "1", "2", "3", "D", "M"}
)
ZONE_RADIUS = 2

NODES: dict[str, tuple[int, int]] = {
    "dock_square": (18, 14),
    "signal_tower": (28, 6),
    "lighthouse": (7, 16),
    "berth_beacon": (5, 10),
    "berth_tug": (26, 20),
    "berth_bell": (32, 10),
    "pier_main": (18, 24),
    "pier_west": (10, 24),
    "breakwater": (33, 23),
    "mist_gate": (37, 23),
    "north_alley": (20, 3),
}

EDGES: list[list[str]] = [
    ["dock_square", "north_alley"],
    ["dock_square", "signal_tower"],
    ["dock_square", "berth_bell"],
    ["dock_square", "berth_beacon"],
    ["dock_square", "lighthouse"],
    ["dock_square", "berth_tug"],
    ["dock_square", "pier_main"],
    ["pier_main", "pier_west"],
    ["berth_tug", "breakwater"],
    ["breakwater", "mist_gate"],
]

# Anchor tiles that must carry these exact legend characters in the grid;
# a mismatch means the doc grid and the node table drifted apart.
ANCHOR_LEGEND: dict[str, str] = {
    "dock_square": "o",
    "signal_tower": "D",
    "lighthouse": "D",
    "berth_beacon": "D",
    "berth_tug": "D",
    "berth_bell": "D",
    "pier_main": "=",
    "pier_west": "=",
    "breakwater": "=",
    "mist_gate": "M",
    "north_alley": ".",
}


def fail(reason: str) -> None:
    message = f"generate_map: {reason}"
    raise SystemExit(message)


def extract_grid(doc_text: str) -> list[str]:
    rows: list[str] = []
    in_fence = False
    for line in doc_text.splitlines():
        if line.startswith("```"):
            if in_fence:
                break
            in_fence = True
            continue
        if not in_fence or not line.strip():
            continue
        cell = line.split()[0]
        if len(cell) != GRID_WIDTH or set(cell) - LEGEND_CHARS:
            fail(f"unexpected grid line: {line!r}")
        rows.append(cell)
    if len(rows) != GRID_HEIGHT:
        fail(f"expected {GRID_HEIGHT} grid rows, found {len(rows)}")
    return rows


def validate_anchors(grid: list[str]) -> None:
    for node, (col, row) in NODES.items():
        expected = ANCHOR_LEGEND[node]
        actual = grid[row][col]
        if actual != expected:
            fail(f"anchor {node} at ({col},{row}) is {actual!r}, expected {expected!r}")


def zone_tiles(grid: list[str], col: int, row: int) -> list[list[int]]:
    rows = range(max(0, row - ZONE_RADIUS), min(GRID_HEIGHT, row + ZONE_RADIUS + 1))
    cols = range(max(0, col - ZONE_RADIUS), min(GRID_WIDTH, col + ZONE_RADIUS + 1))
    return [[c, r] for r in rows for c in cols if grid[r][c] not in BLOCKED_CHARS]


def main() -> None:
    grid = extract_grid(DOC_PATH.read_text())
    validate_anchors(grid)
    data = {
        "ground": grid,
        "collision": [[ch in BLOCKED_CHARS for ch in row] for row in grid],
        "zones": {node: zone_tiles(grid, c, r) for node, (c, r) in NODES.items()},
        "waypoints": {
            "nodes": {node: [c, r] for node, (c, r) in NODES.items()},
            "edges": EDGES,
        },
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, indent=1) + "\n")
    print(f"wrote {OUT_PATH.relative_to(ROOT)} ({GRID_WIDTH}x{GRID_HEIGHT})")


if __name__ == "__main__":
    main()
