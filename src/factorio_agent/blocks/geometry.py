"""Tile geometry shared by the row generator and its auditor.

Coordinates: world x grows east, y grows south. Entity positions are entity
CENTRES (``board.entity``): a 1x1 entity sits at .5 coordinates, an odd-sized
square machine (3x3) also at .5 coordinates. Directions are 0/4/8/12 =
N/E/S/W with unit vectors from ``routing.VECTORS``.

Row-local frame: ``u`` runs along the flow, ``v`` runs to the RIGHT of the
flow. For an east-flowing row u = +x and v = +y (south). A ``Frame`` maps the
canonical east-flowing layout to any of the four flow directions by rotating
clockwise in quarter turns around ``origin``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..routing import VECTORS

FLOW_NAMES = {"north": 0, "east": 4, "south": 8, "west": 12}
CANONICAL_FLOW = 4


def flow_direction(flow: str | int) -> int:
    """Accept a compass name or a 0/4/8/12 direction."""
    if isinstance(flow, str):
        if flow not in FLOW_NAMES:
            raise ValueError(f"flow must be one of {sorted(FLOW_NAMES)}")
        return FLOW_NAMES[flow]
    if flow not in VECTORS:
        raise ValueError("flow direction must be 0, 4, 8 or 12")
    return int(flow)


def rotate(du: float, dv: float, turns: int) -> tuple[float, float]:
    """Rotate an offset clockwise (screen coordinates, y down) by quarter turns."""
    for _ in range(turns % 4):
        du, dv = -dv, du
    return du, dv


@dataclass(frozen=True)
class Frame:
    """Maps canonical (u, v, direction) to world (x, y, direction)."""

    origin: tuple[float, float]
    flow: int

    @property
    def turns(self) -> int:
        return ((self.flow - CANONICAL_FLOW) % 16) // 4

    def point(self, u: float, v: float) -> tuple[float, float]:
        dx, dy = rotate(u, v, self.turns)
        return self.origin[0] + dx, self.origin[1] + dy

    def direction(self, canonical: int) -> int:
        return (canonical + 4 * self.turns) % 16


def require_tile_centre(origin: Iterable[float]) -> tuple[float, float]:
    """A row origin must be a 1x1 tile centre (both coordinates end in .5)."""
    x, y = (float(v) for v in origin)
    if not all(math.isfinite(v) and v % 1 == 0.5 for v in (x, y)):
        raise ValueError("row origin must be a tile centre with .5 coordinates")
    return x, y


def tile_size(catalog: Any, name: str) -> tuple[int, int]:
    """Footprint in tiles from the catalog collision box; 1x1 when not exported."""
    box = (catalog.entities.get(name) or {}).get("collision_box")
    if not isinstance(box, Mapping):
        return 1, 1
    low, high = box["left_top"], box["right_bottom"]
    width = max(1, math.ceil(float(high["x"]) - float(low["x"]) - 1e-6))
    height = max(1, math.ceil(float(high["y"]) - float(low["y"]) - 1e-6))
    return width, height


def xy(entity: Mapping[str, Any]) -> tuple[float, float]:
    p = entity["position"]
    return float(p["x"]), float(p["y"])


def footprint(catalog: Any, entity: Mapping[str, Any]) -> set[tuple[float, float]]:
    """Tile centres covered by an entity; east/west rotation swaps width and height."""
    width, height = tile_size(catalog, entity["name"])
    if entity.get("direction") in (4, 12):
        width, height = height, width
    x, y = xy(entity)
    tiles = set()
    for tx in range(math.floor(x - width / 2 + 1e-6), math.ceil(x + width / 2 - 1e-6)):
        for ty in range(math.floor(y - height / 2 + 1e-6), math.ceil(y + height / 2 - 1e-6)):
            tiles.add((tx + 0.5, ty + 0.5))
    return tiles


def step(p: tuple[float, float], direction: int, distance: int = 1) -> tuple[float, float]:
    vx, vy = VECTORS[direction]
    return p[0] + distance * vx, p[1] + distance * vy


def bounding_box(tiles: Iterable[tuple[float, float]], margin: float = 0) -> list[list[float]]:
    """Tile-edge box ``[[x0, y0], [x1, y1]]`` around tile centres, grown by ``margin``."""
    tiles = list(tiles)
    xs = [t[0] for t in tiles]
    ys = [t[1] for t in tiles]
    return [[min(xs) - 0.5 - margin, min(ys) - 0.5 - margin], [max(xs) + 0.5 + margin, max(ys) + 0.5 + margin]]
