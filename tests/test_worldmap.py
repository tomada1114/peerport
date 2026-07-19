"""Tests for map data (`data/map/port.json`) and `peerport.world.worldmap`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from peerport.errors import MapDataError
from peerport.world.worldmap import WorldMap

MAP_PATH = Path(__file__).parent.parent / "data" / "map" / "port.json"

NODE_ANCHORS = {
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

EDGES = {
    frozenset(pair)
    for pair in [
        ("dock_square", "north_alley"),
        ("dock_square", "signal_tower"),
        ("dock_square", "berth_bell"),
        ("dock_square", "berth_beacon"),
        ("dock_square", "lighthouse"),
        ("dock_square", "berth_tug"),
        ("dock_square", "pier_main"),
        ("pier_main", "pier_west"),
        ("berth_tug", "breakwater"),
        ("breakwater", "mist_gate"),
    ]
}


@pytest.fixture(scope="module")
def raw_map() -> dict[str, object]:
    with MAP_PATH.open() as f:
        data: dict[str, object] = json.load(f)
    return data


@pytest.fixture(scope="module")
def worldmap() -> WorldMap:
    return WorldMap.load(MAP_PATH)


class TestPortJson:
    def test_port_json_has_four_top_level_keys(
        self, raw_map: dict[str, object]
    ) -> None:
        assert set(raw_map) >= {"ground", "collision", "zones", "waypoints"}

    def test_ground_grid_is_30_rows_by_40_cols(
        self, raw_map: dict[str, object]
    ) -> None:
        ground = raw_map["ground"]
        assert isinstance(ground, list)
        assert len(ground) == 30
        assert all(len(row) == 40 for row in ground)

    def test_zones_has_exactly_11_node_keys(self, raw_map: dict[str, object]) -> None:
        zones = raw_map["zones"]
        assert isinstance(zones, dict)
        assert set(zones) == set(NODE_ANCHORS)


class TestCollision:
    @pytest.mark.parametrize(
        ("col", "row"),
        [
            pytest.param(0, 0, id="wall"),
            pytest.param(18, 14, id="square-lamp"),
            pytest.param(20, 26, id="data-sea"),
        ],
    )
    def test_legend_blocked_tiles_are_blocked(
        self, worldmap: WorldMap, col: int, row: int
    ) -> None:
        assert worldmap.is_blocked(col, row)

    @pytest.mark.parametrize(
        ("col", "row"),
        [
            pytest.param(33, 23, id="quay-breakwater"),
            pytest.param(20, 12, id="plaza"),
            pytest.param(5, 10, id="berth-door"),
            pytest.param(37, 23, id="mist-base-layer"),
        ],
    )
    def test_legend_walkable_tiles_are_walkable(
        self, worldmap: WorldMap, col: int, row: int
    ) -> None:
        assert not worldmap.is_blocked(col, row)


class TestWaypoints:
    def test_nodes_match_anchor_coordinates(self, worldmap: WorldMap) -> None:
        assert worldmap.nodes == NODE_ANCHORS

    def test_edges_exactly_ten_bidirectional(self, worldmap: WorldMap) -> None:
        assert worldmap.edges == EDGES

    def test_node_path_returns_node_ids_only(self, worldmap: WorldMap) -> None:
        path = worldmap.node_path("dock_square", "mist_gate")
        assert path == ["dock_square", "berth_tug", "breakwater", "mist_gate"]

    def test_node_path_unknown_node_raises(self, worldmap: WorldMap) -> None:
        with pytest.raises(MapDataError, match="unknown node"):
            worldmap.node_path("dock_square", "atlantis")


class TestMistRule:
    def test_mist_tile_walkable_only_for_drifter(self, worldmap: WorldMap) -> None:
        assert worldmap.is_walkable_for(37, 23, "drifter")
        assert not worldmap.is_walkable_for(37, 23, "peer")
        assert not worldmap.is_walkable_for(37, 23, "mate")

    def test_peer_path_to_mist_gate_excludes_mist_tiles(
        self, worldmap: WorldMap
    ) -> None:
        start = worldmap.standable_tile("berth_tug", "peer")
        path = worldmap.path_to_node(start, "mist_gate", "peer")
        assert path is not None
        forbidden = {(c, 23) for c in range(36, 40)}
        assert not forbidden.intersection(path)

    def test_drifter_path_to_mist_gate_reaches_mist_tile(
        self, worldmap: WorldMap
    ) -> None:
        start = worldmap.standable_tile("breakwater", "drifter")
        path = worldmap.path_to_node(start, "mist_gate", "drifter")
        assert path is not None
        assert path[-1] == (37, 23)
        assert worldmap.standable_tile("mist_gate", "drifter") == (38, 23) or any(
            worldmap.ground[r][c] == "M" for (c, r) in path
        )


class TestAStar:
    def test_path_dock_square_to_berth_beacon_ends_at_door(
        self, worldmap: WorldMap
    ) -> None:
        start = worldmap.standable_tile("dock_square", "mate")
        path = worldmap.path_to_node(start, "berth_beacon", "mate")
        assert path is not None
        assert path[-1] == (5, 10)
        assert all(worldmap.is_walkable_for(c, r, "mate") for (c, r) in path)

    def test_all_nodes_pairwise_reachable(self, worldmap: WorldMap) -> None:
        names = list(NODE_ANCHORS)
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                start = worldmap.standable_tile(a, "drifter")
                path = worldmap.path_to_node(start, b, "drifter")
                assert path is not None, f"no path {a} -> {b}"

    def test_boxed_in_tile_returns_none(self) -> None:
        ground = ["#####", "#,#,#", "#####"]
        boxed = WorldMap.from_layers(
            ground=ground,
            zones={"a": [(1, 1)], "b": [(3, 1)]},
            nodes={"a": (1, 1), "b": (3, 1)},
            edges=[("a", "b")],
        )
        assert boxed.tile_path((1, 1), (3, 1), "peer") is None

    def test_tile_path_same_start_and_goal(self, worldmap: WorldMap) -> None:
        assert worldmap.tile_path((20, 12), (20, 12), "peer") == [(20, 12)]


class TestLoadErrors:
    def test_load_missing_file_raises_map_data_error(self, tmp_path: Path) -> None:
        with pytest.raises(MapDataError, match=r"port\.json"):
            WorldMap.load(tmp_path / "port.json")

    def test_load_malformed_json_raises_map_data_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "port.json"
        bad.write_text("{not json")
        with pytest.raises(MapDataError, match="malformed"):
            WorldMap.load(bad)

    def test_load_missing_key_raises_map_data_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "port.json"
        bad.write_text(json.dumps({"ground": []}))
        with pytest.raises(MapDataError, match="missing"):
            WorldMap.load(bad)
