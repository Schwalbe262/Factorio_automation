"""Tile geometry shared by the site package.

Coordinate conventions (used everywhere in ``factorio_agent.site``):

* A *tile* is a pair of integers ``(x, y)``. Tile ``(x, y)`` covers the
  Factorio map square ``[x, x + 1) x [y, y + 1)``; its centre is
  ``(x + 0.5, y + 0.5)``. ``y`` grows southwards, as in the game.
* A :class:`Rect` is a half-open box of whole tiles: it contains every tile
  with ``x0 <= x < x1`` and ``y0 <= y < y1``.
* A *chunk* is the game's 32 x 32 tile block. Chunk ``(cx, cy)`` holds tiles
  ``32 * cx <= x < 32 * cx + 32`` (and likewise for ``y``).
* Entity directions use the 2.x 16-way encoding: 0 north, 4 east, 8 south,
  12 west.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterator

CHUNK_TILES = 32
Tile = tuple[int, int]

# Footprint (width, height) in tiles when facing north. Anything not listed is
# 1 x 1. Plan rows may override with "_width" / "_height", as the builder does.
FOOTPRINTS = {
    "boiler": (3, 2),
    "steam-engine": (3, 5),
    "burner-mining-drill": (2, 2),
    "stone-furnace": (2, 2),
    "steel-furnace": (2, 2),
    "electric-furnace": (3, 3),
    "electric-mining-drill": (3, 3),
    "lab": (3, 3),
    "assembling-machine-1": (3, 3),
    "assembling-machine-2": (3, 3),
    "assembling-machine-3": (3, 3),
    "oil-refinery": (5, 5),
    "chemical-plant": (3, 3),
    "pumpjack": (3, 3),
    "storage-tank": (3, 3),
    "radar": (3, 3),
    "beacon": (3, 3),
    "centrifuge": (3, 3),
    "big-electric-pole": (2, 2),
    "substation": (2, 2),
    "roboport": (4, 4),
    "splitter": (2, 1),
    "offshore-pump": (1, 2),
    "rocket-silo": (9, 9),
    "gun-turret": (2, 2),
    "solar-panel": (3, 3),
    "accumulator": (2, 2),
}

_EPS = 1e-3


@dataclass(frozen=True)
class Rect:
    """Half-open tile rectangle ``x0 <= x < x1``, ``y0 <= y < y1``."""

    x0: int
    y0: int
    x1: int
    y1: int

    def __post_init__(self) -> None:
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError(f"inverted rect {self}")

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def area(self) -> int:
        return self.width * self.height

    def contains(self, x: int, y: int) -> bool:
        return self.x0 <= x < self.x1 and self.y0 <= y < self.y1

    def intersects(self, other: Rect) -> bool:
        return self.x0 < other.x1 and other.x0 < self.x1 and self.y0 < other.y1 and other.y0 < self.y1

    def tiles(self) -> Iterator[Tile]:
        """Every tile in the rect, row by row (north to south, west to east)."""
        for y in range(self.y0, self.y1):
            for x in range(self.x0, self.x1):
                yield (x, y)

    def anchor(self) -> Tile:
        """The tile that "owns" this rect when merging survey slices.

        It is the tile at the rect's centre (rounded towards the south-east),
        so a feature crossing a slice boundary belongs to exactly one slice.
        """
        return (self.x0 + self.width // 2, self.y0 + self.height // 2)

    def centre(self) -> tuple[float, float]:
        """Centre in map coordinates (floats)."""
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)

    def to_list(self) -> list[int]:
        return [self.x0, self.y0, self.x1, self.y1]


def rect_from_box(left: float, top: float, right: float, bottom: float) -> Rect:
    """Tiles touched by a map-coordinate bounding box (floats).

    A box that only grazes a tile edge (by less than 0.001) does not cover it.
    """
    x0 = math.floor(left + _EPS)
    y0 = math.floor(top + _EPS)
    x1 = max(x0 + 1, math.ceil(right - _EPS))
    y1 = max(y0 + 1, math.ceil(bottom - _EPS))
    return Rect(x0, y0, x1, y1)


def plan_rect(entity: dict) -> Rect:
    """Tiles covered by one entity-plan row (the ``board.entity`` dict format).

    ``position`` is the entity centre in map coordinates; east/west facing
    (direction 4 or 12) swaps width and height.
    """
    width, height = FOOTPRINTS.get(entity["name"], (1, 1))
    width = entity.get("_width", width)
    height = entity.get("_height", height)
    if entity.get("direction") in (4, 12):
        width, height = height, width
    x = entity["position"]["x"]
    y = entity["position"]["y"]
    return rect_from_box(x - width / 2, y - height / 2, x + width / 2, y + height / 2)


def chunk_of(x: int, y: int) -> tuple[int, int]:
    """Chunk coordinates of a tile (floor division, so negative tiles work)."""
    return (x // CHUNK_TILES, y // CHUNK_TILES)


def chunks_in(rect: Rect) -> Iterator[tuple[int, int]]:
    """Every chunk that the rect touches."""
    if rect.area == 0:
        return
    cx0, cy0 = chunk_of(rect.x0, rect.y0)
    cx1, cy1 = chunk_of(rect.x1 - 1, rect.y1 - 1)
    for cy in range(cy0, cy1 + 1):
        for cx in range(cx0, cx1 + 1):
            yield (cx, cy)


def chunk_aligned(rect: Rect) -> Rect:
    """Smallest chunk-aligned rect containing ``rect``."""
    c = CHUNK_TILES
    return Rect(
        (rect.x0 // c) * c,
        (rect.y0 // c) * c,
        -((-rect.x1) // c) * c,
        -((-rect.y1) // c) * c,
    )


def split_rect(rect: Rect, step: int) -> Iterator[Rect]:
    """Cut ``rect`` into slices of at most ``step`` x ``step`` tiles, row by row."""
    if step <= 0:
        raise ValueError("step must be positive")
    for y in range(rect.y0, rect.y1, step):
        for x in range(rect.x0, rect.x1, step):
            yield Rect(x, y, min(x + step, rect.x1), min(y + step, rect.y1))
