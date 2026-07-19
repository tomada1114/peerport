"""Load `data/map/port.json`: collision, zones, waypoint graph, tile A*.

The waypoint graph only picks destination node sequences; tile-level A*
produces the actual walkable path (map-layout.md). Mist-bank ``M`` tiles are
walkable in the base collision layer but rejected per peer unless the peer's
kind is ``drifter``.
"""

from __future__ import annotations

import heapq
import json
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from peerport.errors import MapDataError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

Tile = tuple[int, int]

BLOCKED_CHARS = frozenset({"#", "~", "o"})
MIST_CHAR = "M"
DRIFTER_KIND = "drifter"
REQUIRED_KEYS = ("ground", "collision", "zones", "waypoints")
NEIGHBOR_STEPS = ((0, -1), (0, 1), (-1, 0), (1, 0))


@dataclass(frozen=True, slots=True)
class WorldMap:
    """Immutable view of the port map: tiles, zones, and the waypoint graph."""

    ground: tuple[str, ...]
    collision: tuple[tuple[bool, ...], ...]
    zones: dict[str, list[Tile]]
    nodes: dict[str, Tile]
    edges: set[frozenset[str]]

    @classmethod
    def load(cls, path: Path) -> WorldMap:
        """Load and validate a map JSON file.

        Raises:
            MapDataError: If the file is missing, malformed, or lacks a
                required top-level key.
        """
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError as exc:
            message = f"map file not found: {path}"
            raise MapDataError(message) from exc
        except json.JSONDecodeError as exc:
            message = f"malformed JSON in map file {path}: {exc}"
            raise MapDataError(message) from exc
        missing = [key for key in REQUIRED_KEYS if key not in raw]
        if missing:
            message = f"map file {path} missing required keys: {', '.join(missing)}"
            raise MapDataError(message)
        waypoints = raw["waypoints"]
        return cls(
            ground=tuple(raw["ground"]),
            collision=tuple(tuple(row) for row in raw["collision"]),
            zones={
                zone: [(c, r) for c, r in tiles] for zone, tiles in raw["zones"].items()
            },
            nodes={node: (c, r) for node, (c, r) in waypoints["nodes"].items()},
            edges={frozenset(pair) for pair in waypoints["edges"]},
        )

    @classmethod
    def from_layers(
        cls,
        ground: list[str],
        zones: dict[str, list[Tile]],
        nodes: dict[str, Tile],
        edges: list[tuple[str, str]],
    ) -> WorldMap:
        """Build a map directly from layer data, deriving collision from legend."""
        return cls(
            ground=tuple(ground),
            collision=tuple(tuple(ch in BLOCKED_CHARS for ch in row) for row in ground),
            zones=zones,
            nodes=nodes,
            edges={frozenset(pair) for pair in edges},
        )

    @property
    def width(self) -> int:
        """Number of tile columns."""
        return len(self.ground[0])

    @property
    def height(self) -> int:
        """Number of tile rows."""
        return len(self.ground)

    def in_bounds(self, col: int, row: int) -> bool:
        """Return whether the tile lies inside the grid."""
        return 0 <= col < self.width and 0 <= row < self.height

    def is_blocked(self, col: int, row: int) -> bool:
        """Return the base collision value (out-of-bounds counts as blocked)."""
        if not self.in_bounds(col, row):
            return True
        return self.collision[row][col]

    def is_walkable_for(self, col: int, row: int, kind: str) -> bool:
        """Return whether a peer of `kind` may stand on the tile.

        Mist-bank tiles are reserved for the drifter (map-layout.md).
        """
        if self.is_blocked(col, row):
            return False
        return self.ground[row][col] != MIST_CHAR or kind == DRIFTER_KIND

    def node_path(self, start: str, goal: str) -> list[str] | None:
        """Return the shortest node-id sequence between two waypoint nodes.

        The result contains node ids only; tile-level movement is a separate
        A* step (`tile_path`).

        Raises:
            MapDataError: If either node id is not in the waypoint graph.
        """
        for node in (start, goal):
            if node not in self.nodes:
                message = f"unknown node: {node}"
                raise MapDataError(message)
        adjacency: dict[str, list[str]] = {node: [] for node in self.nodes}
        for edge in self.edges:
            a, b = sorted(edge)
            adjacency[a].append(b)
            adjacency[b].append(a)
        queue = deque([start])
        came_from: dict[str, str | None] = {start: None}
        while queue:
            current = queue.popleft()
            if current == goal:
                return _walk_back(came_from, goal)
            for neighbor in adjacency[current]:
                if neighbor not in came_from:
                    came_from[neighbor] = current
                    queue.append(neighbor)
        return None

    def standable_tile(self, node: str, kind: str) -> Tile:
        """Return the nearest tile to the node anchor a peer of `kind` can occupy.

        The anchor itself may be blocked (dock_square's lamp) or mist-only
        (mist_gate for non-drifters); breadth-first search finds the closest
        standable substitute.

        Raises:
            MapDataError: If the node id is unknown or no standable tile exists.
        """
        if node not in self.nodes:
            message = f"unknown node: {node}"
            raise MapDataError(message)
        anchor = self.nodes[node]
        queue = deque([anchor])
        seen = {anchor}
        while queue:
            col, row = queue.popleft()
            if self.is_walkable_for(col, row, kind):
                return (col, row)
            for ncol, nrow in self._neighbors(col, row):
                if (ncol, nrow) not in seen:
                    seen.add((ncol, nrow))
                    queue.append((ncol, nrow))
        message = f"no standable tile near node {node} for kind {kind}"
        raise MapDataError(message)

    def path_to_node(self, start: Tile, node: str, kind: str) -> list[Tile] | None:
        """Return a walkable tile path from `start` to the node's standable tile."""
        return self.tile_path(start, self.standable_tile(node, kind), kind)

    def tile_path(self, start: Tile, goal: Tile, kind: str) -> list[Tile] | None:
        """A* over walkable tiles; returns None when no path exists."""
        if not self.is_walkable_for(*start, kind) or not self.is_walkable_for(
            *goal, kind
        ):
            return None
        if start == goal:
            return [start]
        open_set: list[tuple[int, int, Tile]] = [(_manhattan(start, goal), 0, start)]
        g_score: dict[Tile, int] = {start: 0}
        came_from: dict[Tile, Tile | None] = {start: None}
        while open_set:
            _, cost, current = heapq.heappop(open_set)
            if current == goal:
                return _walk_back(came_from, goal)
            if cost > g_score[current]:
                continue
            for neighbor in self._neighbors(*current):
                if not self.is_walkable_for(*neighbor, kind):
                    continue
                tentative = g_score[current] + 1
                if tentative < g_score.get(neighbor, tentative + 1):
                    g_score[neighbor] = tentative
                    came_from[neighbor] = current
                    heapq.heappush(
                        open_set,
                        (tentative + _manhattan(neighbor, goal), tentative, neighbor),
                    )
        return None

    def _neighbors(self, col: int, row: int) -> Iterator[Tile]:
        for dcol, drow in NEIGHBOR_STEPS:
            ncol, nrow = col + dcol, row + drow
            if self.in_bounds(ncol, nrow):
                yield (ncol, nrow)


def _manhattan(a: Tile, b: Tile) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _walk_back[T](came_from: dict[T, T | None], goal: T) -> list[T]:
    path = [goal]
    while (previous := came_from[path[-1]]) is not None:
        path.append(previous)
    path.reverse()
    return path
