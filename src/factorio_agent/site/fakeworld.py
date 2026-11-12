"""Deterministic synthetic maps for tests and offline planning.

``make_world(seed)`` returns a fully surveyed :class:`WorldMap` centred on
tile (0, 0) with blobby ore patches, crude-oil wells, lakes, tree clusters,
rocks and short cliff lines. The start area around (0, 0) is always clear.
Every random choice comes from ``random.Random(seed)``, so a seed always
produces the same map and different seeds produce different geography.
"""

from __future__ import annotations

import math
import random

from .geometry import CHUNK_TILES, Rect, Tile
from .worldmap import LAND, WATER, Feature, Well, WorldMap

ORES = ("iron-ore", "copper-ore", "coal", "stone")
ORE_RICHNESS = {"iron-ore": 1500, "copper-ore": 1300, "coal": 1100, "stone": 900}
OIL = "crude-oil"
TREE = "tree-01"
ROCKS = (("big-rock", 2, 2), ("huge-rock", 3, 3))


class _Painter:
    """Holds the map being generated plus the bounds and start-area rule."""

    def __init__(self, rng: random.Random, area: Rect, start_radius: int):
        self.rng = rng
        self.area = area
        self.start_radius = start_radius
        self.world = WorldMap()
        self.world.mark_surveyed(area)
        self.centres: list[tuple[int, int, int]] = []

    def in_start(self, x: int, y: int) -> bool:
        return math.hypot(x + 0.5, y + 0.5) < self.start_radius

    def usable(self, x: int, y: int) -> bool:
        """Inside the map, outside the start area, on land."""
        if not self.area.contains(x, y) or self.in_start(x, y):
            return False
        return self.world.terrain(x, y) == LAND

    def empty(self, rect: Rect) -> bool:
        """Every tile usable, without ore, wells or features."""
        for x, y in rect.tiles():
            if not self.usable(x, y) or self.world.resource_at(x, y) or self.world.features_at(x, y):
                return False
        return True

    def pick_centre(self, radius: int) -> tuple[int, int] | None:
        """A random centre away from the start area and earlier blobs."""
        margin = radius + 2
        for _ in range(60):
            x = self.rng.randint(self.area.x0 + margin, self.area.x1 - margin - 1)
            y = self.rng.randint(self.area.y0 + margin, self.area.y1 - margin - 1)
            if math.hypot(x, y) < self.start_radius + radius + 4:
                continue
            if all(math.hypot(x - cx, y - cy) > radius + r + 3 for cx, cy, r in self.centres):
                self.centres.append((x, y, radius))
                return (x, y)
        return None

    def blob(self, cx: int, cy: int, radius: float) -> list[tuple[Tile, float]]:
        """Tiles of a wobbly disc with a depth 1 at the centre and 0 at the edge."""
        p1 = self.rng.uniform(0, 2 * math.pi)
        p2 = self.rng.uniform(0, 2 * math.pi)
        reach = int(radius * 1.5) + 1
        tiles = []
        for y in range(cy - reach, cy + reach + 1):
            for x in range(cx - reach, cx + reach + 1):
                angle = math.atan2(y - cy, x - cx)
                edge = radius * (1 + 0.25 * math.sin(3 * angle + p1) + 0.15 * math.sin(5 * angle + p2))
                distance = math.hypot(x - cx, y - cy)
                if distance < edge:
                    tiles.append(((x, y), 1 - distance / edge))
        return tiles


def _paint_lakes(p: _Painter, count: int) -> None:
    for _ in range(count):
        radius = p.rng.randint(6, 16)
        centre = p.pick_centre(radius)
        if centre is None:
            continue
        for (x, y), _depth in p.blob(centre[0], centre[1], radius):
            if p.usable(x, y):
                p.world.set_terrain(x, y, WATER)


def _paint_ores(p: _Painter, per_ore: int) -> None:
    for ore in ORES:
        for _ in range(per_ore):
            radius = p.rng.randint(7, 14)
            centre = p.pick_centre(radius)
            if centre is None:
                continue
            for (x, y), depth in p.blob(centre[0], centre[1], radius):
                if p.usable(x, y) and p.world.resource_at(x, y) is None:
                    amount = int(ORE_RICHNESS[ore] * (0.3 + depth))
                    p.world.set_ore(x, y, ore, amount)


def _paint_oil(p: _Painter, fields: int) -> None:
    for _ in range(fields):
        centre = p.pick_centre(8)
        if centre is None:
            continue
        for _ in range(p.rng.randint(3, 6)):
            x = centre[0] + p.rng.randint(-7, 7)
            y = centre[1] + p.rng.randint(-7, 7)
            well = Well(OIL, (x, y), p.rng.randint(150_000, 600_000))
            if p.empty(well.rect):
                p.world.add_well(well)


def _paint_rocks(p: _Painter, count: int) -> None:
    for _ in range(count):
        name, width, height = p.rng.choice(ROCKS)
        x = p.rng.randint(p.area.x0, p.area.x1 - width)
        y = p.rng.randint(p.area.y0, p.area.y1 - height)
        rect = Rect(x, y, x + width, y + height)
        if p.empty(rect):
            p.world.add_feature(Feature("rock", name, rect))


def _paint_cliffs(p: _Painter, count: int) -> None:
    for _ in range(count):
        x = p.rng.randint(p.area.x0, p.area.x1 - 24)
        y = p.rng.randint(p.area.y0 + 2, p.area.y1 - 4)
        for _ in range(p.rng.randint(3, 6)):
            rect = Rect(x, y, x + 4, y + 2)
            if p.empty(rect):
                p.world.add_feature(Feature("cliff", "cliff", rect))
            x += 4
            y = max(p.area.y0, min(p.area.y1 - 2, y + p.rng.randint(-1, 1)))


def _paint_forests(p: _Painter, count: int, density: float) -> None:
    for _ in range(count):
        radius = p.rng.randint(8, 20)
        x = p.rng.randint(p.area.x0, p.area.x1 - 1)
        y = p.rng.randint(p.area.y0, p.area.y1 - 1)
        for (tx, ty), depth in p.blob(x, y, radius):
            if p.rng.random() < density * (0.4 + depth) and p.empty(Rect(tx, ty, tx + 1, ty + 1)):
                p.world.add_feature(Feature("tree", TREE, Rect(tx, ty, tx + 1, ty + 1)))


def make_world(
    seed: int,
    *,
    size: int = 256,
    patches: int = 2,
    oil_fields: int = 2,
    lakes: int = 3,
    forests: int = 5,
    forest_density: float = 0.35,
    rocks: int = 12,
    cliffs: int = 2,
    start_radius: int = 24,
) -> WorldMap:
    """Build a synthetic ``size`` x ``size`` tile map centred on (0, 0).

    ``size`` must be a multiple of 64 so the map is whole chunks.
    ``patches`` is the number of patches per ore (iron, copper, coal, stone).
    ``forest_density`` (0..1) is the chance of a tree on a forest tile.
    """
    if size <= 0 or size % (2 * CHUNK_TILES):
        raise ValueError(f"size must be a positive multiple of {2 * CHUNK_TILES}")
    half = size // 2
    painter = _Painter(random.Random(seed), Rect(-half, -half, half, half), start_radius)
    _paint_lakes(painter, lakes)
    _paint_ores(painter, patches)
    _paint_oil(painter, oil_fields)
    _paint_rocks(painter, rocks)
    _paint_cliffs(painter, cliffs)
    _paint_forests(painter, forests, forest_density)
    return painter.world
