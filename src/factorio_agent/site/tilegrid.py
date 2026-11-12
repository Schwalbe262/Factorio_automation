"""Constant-time rectangle counts over a :class:`WorldMap` snapshot.

The planner tests thousands of candidate rectangles. Asking the WorldMap tile
by tile would be slow, so :class:`TileGrid` takes one snapshot of the
surveyed bounds and builds 2-D prefix sums for three layers:

* ``hard``  - tiles that can never be built on: water, unbuildable terrain,
  cliffs, fixed obstacles, built entities, plans, tiles already reserved on
  the map, unsurveyed tiles and anything outside the surveyed bounds;
* ``ore``   - tiles covered by a resource (ore or a well footprint);
* ``clear`` - tiles covered by trees or rocks (buildable after mining).

The snapshot does not follow later changes to the map; the planner tracks
its own new reservations separately.
"""

from __future__ import annotations

from itertools import accumulate

from .geometry import CHUNK_TILES, Rect
from .worldmap import WorldMap


def _prefix(rows: list[bytearray], width: int) -> list[list[int]]:
    """Summed-area table: ``table[y][x]`` = count in rows < y, columns < x."""
    table = [[0] * (width + 1)]
    previous = table[0]
    for row in rows:
        running = [0] + list(accumulate(row))
        current = [a + b for a, b in zip(previous, running)]
        table.append(current)
        previous = current
    return table


class TileGrid:
    """Prefix-sum snapshot of the surveyed bounds of ``world``."""

    def __init__(self, world: WorldMap):
        bounds = world.bounds()
        if bounds is None:
            raise ValueError("the world map has no surveyed tiles")
        self.bounds = bounds
        width = bounds.width
        height = bounds.height
        hard = [bytearray(width) for _ in range(height)]
        ore = [bytearray(width) for _ in range(height)]
        clear = [bytearray(width) for _ in range(height)]
        self._mark_unsurveyed(world, hard)
        for (x, y), _terrain in world.terrain_tiles().items():
            self._set(hard, x, y)
        for x, y in world.ore_tiles():
            self._set(ore, x, y)
        for well in world.wells():
            for x, y in well.rect.tiles():
                self._set(ore, x, y)
        for feature in world.features():
            layer = clear if feature.clearable else hard
            for x, y in feature.rect.tiles():
                self._set(layer, x, y)
        for _owner, rect in world.zones():
            for x, y in rect.tiles():
                self._set(hard, x, y)
        self._hard = _prefix(hard, width)
        self._ore = _prefix(ore, width)
        self._clear = _prefix(clear, width)

    def _set(self, layer: list[bytearray], x: int, y: int) -> None:
        if self.bounds.contains(x, y):
            layer[y - self.bounds.y0][x - self.bounds.x0] = 1

    def _mark_unsurveyed(self, world: WorldMap, hard: list[bytearray]) -> None:
        surveyed = world.surveyed_chunks()
        b = self.bounds
        for cy in range(b.y0 // CHUNK_TILES, b.y1 // CHUNK_TILES):
            for cx in range(b.x0 // CHUNK_TILES, b.x1 // CHUNK_TILES):
                if (cx, cy) in surveyed:
                    continue
                chunk = Rect(cx * CHUNK_TILES, cy * CHUNK_TILES, (cx + 1) * CHUNK_TILES, (cy + 1) * CHUNK_TILES)
                for x, y in chunk.tiles():
                    self._set(hard, x, y)

    def _clip(self, rect: Rect) -> Rect | None:
        b = self.bounds
        x0 = max(rect.x0, b.x0)
        y0 = max(rect.y0, b.y0)
        x1 = min(rect.x1, b.x1)
        y1 = min(rect.y1, b.y1)
        if x0 >= x1 or y0 >= y1:
            return None
        return Rect(x0, y0, x1, y1)

    def _count(self, table: list[list[int]], rect: Rect) -> int:
        clipped = self._clip(rect)
        if clipped is None:
            return 0
        x0 = clipped.x0 - self.bounds.x0
        x1 = clipped.x1 - self.bounds.x0
        y0 = clipped.y0 - self.bounds.y0
        y1 = clipped.y1 - self.bounds.y0
        return table[y1][x1] - table[y0][x1] - table[y1][x0] + table[y0][x0]

    def inside(self, rect: Rect) -> bool:
        """True when the whole rect lies inside the snapshot bounds."""
        clipped = self._clip(rect)
        return clipped is not None and clipped == rect

    def hard(self, rect: Rect) -> int:
        """Never-buildable tiles in ``rect``; tiles outside the bounds count."""
        clipped = self._clip(rect)
        outside = rect.area - (0 if clipped is None else clipped.area)
        return self._count(self._hard, rect) + outside

    def ore(self, rect: Rect) -> int:
        """Resource-covered tiles in ``rect`` (inside the bounds)."""
        return self._count(self._ore, rect)

    def clearable(self, rect: Rect) -> int:
        """Tree/rock tiles in ``rect`` (inside the bounds)."""
        return self._count(self._clear, rect)

    def bad(self, rect: Rect) -> int:
        """Tiles that are hard-blocked or carry a resource (may double count)."""
        return self.hard(rect) + self.ore(rect)


class Occupancy:
    """A TileGrid plus the rectangles the planner reserved since the snapshot.

    ``clean(rect)`` is the planner's buildability test: no hard tile, no ore
    and no overlap with any rect in ``taken``.
    """

    def __init__(self, grid: TileGrid):
        self.grid = grid
        self.taken: list[Rect] = []

    def overlap(self, rect: Rect) -> int:
        """Tiles of ``rect`` covered by taken rects (may double count)."""
        total = 0
        for other in self.taken:
            if rect.intersects(other):
                width = min(rect.x1, other.x1) - max(rect.x0, other.x0)
                height = min(rect.y1, other.y1) - max(rect.y0, other.y0)
                total += width * height
        return total

    def clean(self, rect: Rect) -> bool:
        if rect.area == 0:
            return True
        if self.grid.bad(rect):
            return False
        return not any(rect.intersects(other) for other in self.taken)

    def clean_fraction(self, rect: Rect) -> float:
        """Approximate share (0..1) of ``rect`` that is clean."""
        if rect.area == 0:
            return 1.0
        bad = self.grid.bad(rect) + self.overlap(rect)
        return max(0.0, 1.0 - bad / rect.area)
