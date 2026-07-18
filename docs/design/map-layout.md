# Map Layout — The Port (40×30)

> Status: v1.0 (2026-07-18) — `peerport-designer` output. Topology and
> zoning are normative; individual tiles may shift by a tile or two during
> implementation as long as node connectivity is preserved.

## Geography in one paragraph

The town faces the data-sea to the **south**. A full-width quay lines the
waterfront, with two piers and an eastern breakwater that trails off into
the **mist bank** where Echo appears. The **lighthouse** stands west near
the water; **Beacon's berth** sits above it. **Dock Square** is the center
of everything. The **Signal Tower** watches from the northeast with
**Bell's berth** beside it; **Tug's berth** fronts the southeast waterfront.
One main street crosses the town east-west on each of the north (row 11),
middle (row 14), and waterfront (row 21) lines.

## Tile grid (1 char = 1 tile, 40 cols × 30 rows)

```
########################################   0
########################################   1
##,,,,,,,,,,,,,,,,,,..,,,,,,,,,,,,,,,,##   2
##,,,,,,,,,,,,,,,,,,..,,,,,TTTT,,,,,,,##   3
##,,,,,,,,,,,,,,,,,,..,,,,,TTTT,,,,,,,##   4
##,,,,,,,,,,,,,,,,,,..,,,,,TTTT,,,,,,,##   5
##,,,,,,,,,,,,,,,,,,..,,,,,TDDT,,,,,,,##   6
##,,,,,,,,,,,,,,,,,,..,,,,,,..,,,,,,,,##   7
##,,1111,,,,,,,,,,,,..,,,,,,..,3333,,,##   8
##,,1111,,,,,,,,,,,,..,,,,,,..,3333,,,##   9
##,,1D11,,,,,,,,,,,,..,,,,,,..,3D33,,,##  10
##....................................##  11
##,,,,,,,,,,,............,,,,,,,,,,,,,##  12
##,,,,,,,,,,,............,,,,,,,,,,,,,##  13
##................o...................##  14
##,LLLL,,,,,,............,,,,,,,,,,,,,##  15
##,LLLLD,,,,,............,,,,,,,,,,,,,##  16
##,LLLL,,,,,,............,,,,,,,,,,,,,##  17
##,LLLL,,,,,,,,,,,..,,,,,2222,,,,,,,,,##  18
##,,,,,,,,,,,,,,,,,..,,,,2222,,,,,,,,,##  19
##,,,,,,,,,,,,,,,,,..,,,,2D22,,,,,,,,,##  20
##....................................##  21
========================================  22
~~~~~~~~~~==~~~~~~==~~~~~~~~~~======MMM~  23
~~~~~~~~~~==~~~~~~==~~~~~~~~~~~~~~~~~~~~  24
~~~~~~~~~~==~~~~~~==~~~~~~~~~~~~~~~~~~~~  25
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~  26
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~  27
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~  28
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~  29
```

Legend: `#` wall/blocked · `,` ground (walkable, decorable) · `.` street/
plaza · `=` quay & piers (walkable) · `~` data-sea (blocked) · `o` square
lamp (blocked prop; beam glints here at night) · `T` Signal Tower ·
`L` lighthouse · `1/2/3` berth buildings (Beacon/Tug/Bell) · `D` door tile
(walkable, the building's interaction point) · `M` mist bank (Echo-only
spawn/despawn tiles).

Notes: the cols 20–21 north alley dead-ends into the wall — flavor, and a
quiet spot peers can idle in. The breakwater (row 23, cols 30–35) is
walkable; only Echo may stand on `M`.

## Location nodes (waypoint graph)

| Node id | Anchor tile (col,row) | Notes |
|---|---|---|
| `dock_square` | 18,14 (lamp adjacency) | social hub; largest idle capacity |
| `signal_tower` | 28,6 (door) | BBS post/read actions happen here |
| `lighthouse` | 7,16 (door) | Bridge's world-side; Mate's favorite spot |
| `berth_beacon` | 5,10 (door) | |
| `berth_tug` | 26,20 (door) | |
| `berth_bell` | 32,10 (door) | |
| `pier_main` | 18,24 | aligned with the south road |
| `pier_west` | 10,24 | quiet fishing-spot flavor |
| `breakwater` | 33,23 | Echo's usual haunt |
| `mist_gate` | 37,23 | Echo spawn/despawn point (M tiles) |
| `north_alley` | 20,3 | dead-end idle spot |

**Edges** (bidirectional, along streets): `dock_square` ↔ every node via
the three east-west streets (rows 11/14/21) and the north-south connectors
(cols 20–21, cols 28–29, cols 18–19). Concretely: square↔north_alley,
square↔signal_tower, square↔berth_bell (via row 11), square↔berth_beacon
(via row 11/14), square↔lighthouse (via row 14/16), square↔berth_tug (via
row 21), square↔pier_main, pier_main↔pier_west (via quay), berth_tug↔
breakwater↔mist_gate (via quay). Tile-level pathfinding (A* on walkable
tiles) runs *within* an edge; the waypoint graph only picks destinations.

## Map data format (decision)

**Custom JSON, not Tiled** (recorded as D-019): `data/map/port.json` with
`ground` (tile-id grid), `collision` (derived from legend), `zones` (named
rects/tile-lists per node above), `waypoints` (nodes+edges). Rationale: one
small hand-authored map, no build chain, no Tiled runtime parsing; this
ASCII grid is the authoring source and a script (or careful hand pass)
transcribes it. Revisit only if the map count ever grows.

## Simulation hooks (for the implementation design)

- Peers idle at nodes, not tiles: each node has 2–4 "idle tiles" nearby so
  two peers at the square stand apart naturally.
- `rest` action = go to own berth door; Echo rests at `breakwater`.
- Beacon's research flavor run targets `signal_tower`.
- Fog (LLM outage) visually pools over `~` and `M` tiles first — the sea
  fogs before the town does.
