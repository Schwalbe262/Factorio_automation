"""Lane-preserving belt graph for audits (port of ``compact_green._transport``).

Lanes are named relative to a belt's own direction: "left" is the left-hand
lane when looking downstream. A straight continuation keeps lanes; a lone
perpendicular feeder bends the recipient (lanes kept, native curve); an inline
tail or two opposite feeders make a side-load onto the approached lane.
Underground belts and splitters are not modelled (rows do not use them).

Inserter drop lane (``lanes.inserter_output_lane``): an inserter beside a
straight belt drops on the FAR lane; inserters pick up from either lane.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..routing import VECTORS
from .geometry import step, xy

Tile = tuple[float, float]
LANES = ("left", "right")


def belt_tiles(catalog: Any, entities: list[dict]) -> dict[Tile, dict]:
    return {xy(e): e for e in entities if (catalog.entities.get(e["name"]) or {}).get("type") == "transport-belt"}


def belt_graph(belts: dict[Tile, dict]) -> dict[Tile, tuple[Tile, dict[str, str]]]:
    """``graph[p] = (q, lane_map)``: items on lane k of p continue on lane_map[k] of q."""
    incoming: dict[Tile, list[int]] = defaultdict(list)
    for p, e in belts.items():
        q = step(p, e["direction"])
        if q in belts and (e["direction"] - belts[q]["direction"]) % 16 != 8:
            incoming[q].append(e["direction"])
    graph = {}
    for p, e in belts.items():
        q = step(p, e["direction"])
        other = belts.get(q)
        if other is None or (e["direction"] - other["direction"]) % 16 == 8:
            continue
        if e["direction"] == other["direction"] or len(set(incoming[q])) == 1:
            graph[p] = (q, {"left": "left", "right": "right"})
            continue
        vx, vy = VECTORS[other["direction"]]
        lane = "left" if (p[0] - q[0]) * vy - (p[1] - q[1]) * vx > 0 else "right"
        graph[p] = (q, {"left": lane, "right": lane})
    return graph


def spread(lanes: dict, graph: dict) -> None:
    """Propagate item sets downstream along ``graph`` until stable (in place)."""
    changed = True
    while changed:
        changed = False
        for p, (q, mapping) in graph.items():
            for before, after in mapping.items():
                source, dest = lanes[p, before], lanes[q, after]
                if not source <= dest:
                    dest |= source
                    changed = True


def reachable(start: Tile, graph: dict) -> set[Tile]:
    seen = {start}
    while start in graph and graph[start][0] not in seen:
        start = graph[start][0]
        seen.add(start)
    return seen


def straight_walk(start: Tile, belts: dict, graph: dict, facing: int, *, backwards: bool = False) -> list[Tile]:
    """Tiles of the straight run through ``start`` that all face ``facing``."""
    preceding = {q: p for p, (q, _) in graph.items() if belts[p]["direction"] == belts[q]["direction"]}
    path = [start]
    while True:
        p = path[-1]
        q = preceding.get(p) if backwards else (graph.get(p) or (None,))[0]
        if q is None or q in path or belts[q]["direction"] != facing:
            return path
        if q != step(p, facing if not backwards else (facing + 8) % 16):
            return path
        path.append(q)
