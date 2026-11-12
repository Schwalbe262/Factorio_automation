"""Resource patches and shoreline search over a :class:`WorldMap`.

Pure functions; no game access. Coordinates follow site.geometry.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from .geometry import Rect, Tile

# Wells (crude oil) are scattered a few tiles apart. Two wells whose centre
# tiles are at most this many tiles apart (Chebyshev distance) share a patch.
WELL_JOIN_TILES = 12

_NEIGHBOURS_8 = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]
# (dx, dy) from water to land, and the direction from that land tile towards
# the water (2.x 16-way: 0 north, 4 east, 8 south, 12 west).
_SHORE_SIDES = [((0, 1), 0), ((-1, 0), 4), ((0, -1), 8), ((1, 0), 12)]


@dataclass(frozen=True)
class Patch:
    """One connected resource patch.

    ``tiles`` are the tiles covered by the resource (ore tiles, or every
    footprint tile of the wells). ``amount`` is the summed raw game amount.
    ``centroid`` is the mean of tile centres (or well centres) in map
    coordinates. ``wells`` lists well centre tiles; it is empty for ores.
    """

    name: str
    tiles: frozenset[Tile]
    amount: int
    bbox: Rect
    centroid: tuple[float, float]
    wells: tuple[Tile, ...] = ()

    @property
    def tile_count(self) -> int:
        return len(self.tiles)

    def contains(self, x: int, y: int) -> bool:
        return (x, y) in self.tiles


@dataclass(frozen=True)
class Shore:
    """A water tile next to buildable land, for an offshore pump.

    ``direction`` points from ``land`` towards ``water``.
    """

    water: Tile
    land: Tile
    direction: int


def _bbox(tiles) -> Rect:
    xs = [x for x, _ in tiles]
    ys = [y for _, y in tiles]
    return Rect(min(xs), min(ys), max(xs) + 1, max(ys) + 1)


def _components(tiles: set[Tile]) -> list[set[Tile]]:
    """8-connected components of a tile set (diagonal tiles connect)."""
    left = set(tiles)
    groups = []
    while left:
        start = left.pop()
        group = {start}
        stack = [start]
        while stack:
            x, y = stack.pop()
            for dx, dy in _NEIGHBOURS_8:
                tile = (x + dx, y + dy)
                if tile in left:
                    left.remove(tile)
                    group.add(tile)
                    stack.append(tile)
        groups.append(group)
    return groups


def _ore_patches(world, name: str | None) -> list[Patch]:
    by_name: dict[str, dict[Tile, int]] = {}
    for tile, (ore, amount) in world.ore_tiles().items():
        if name is None or ore == name:
            by_name.setdefault(ore, {})[tile] = amount
    patches = []
    for ore, amounts in by_name.items():
        for group in _components(set(amounts)):
            cx = sum(x + 0.5 for x, _ in group) / len(group)
            cy = sum(y + 0.5 for _, y in group) / len(group)
            total = sum(amounts[t] for t in group)
            patches.append(Patch(ore, frozenset(group), total, _bbox(group), (cx, cy)))
    return patches


def _well_groups(wells) -> list[list]:
    left = list(wells)
    groups = []
    while left:
        group = [left.pop()]
        index = 0
        while index < len(group):
            cx, cy = group[index].centre
            near = [w for w in left if max(abs(w.centre[0] - cx), abs(w.centre[1] - cy)) <= WELL_JOIN_TILES]
            for well in near:
                left.remove(well)
                group.append(well)
            index += 1
        groups.append(group)
    return groups


def _well_patches(world, name: str | None) -> list[Patch]:
    by_name: dict[str, list] = {}
    for well in world.wells():
        if name is None or well.name == name:
            by_name.setdefault(well.name, []).append(well)
    patches = []
    for fluid, wells in by_name.items():
        for group in _well_groups(wells):
            tiles = frozenset(t for w in group for t in w.rect.tiles())
            cx = sum(w.centre[0] + 0.5 for w in group) / len(group)
            cy = sum(w.centre[1] + 0.5 for w in group) / len(group)
            centres = tuple(sorted(w.centre for w in group))
            total = sum(w.amount for w in group)
            patches.append(Patch(fluid, tiles, total, _bbox(tiles), (cx, cy), centres))
    return patches


def find_patches(world, name: str | None = None) -> list[Patch]:
    """All patches (or those of one resource), largest amount first per name."""
    patches = _ore_patches(world, name) + _well_patches(world, name)
    return sorted(patches, key=lambda p: (p.name, -p.amount, p.bbox.y0, p.bbox.x0))


def _distance(x: int, y: int, tile: Tile) -> float:
    return math.hypot(tile[0] - x, tile[1] - y)


def water_tiles_near(world, x: int, y: int, radius: int) -> list[Tile]:
    """Surveyed water tiles whose distance from tile (x, y) is <= radius.

    Distance is Euclidean between tile indices. Nearest first; ties by row
    then column. Unsurveyed tiles are skipped, not reported as dry land.
    """
    found = []
    for ty in range(y - radius, y + radius + 1):
        for tx in range(x - radius, x + radius + 1):
            if world.peek_terrain(tx, ty) == "water" and _distance(x, y, (tx, ty)) <= radius:
                found.append((tx, ty))
    return sorted(found, key=lambda t: (_distance(x, y, t), t[1], t[0]))


def nearest_shore(world, x: int, y: int, radius: int = 64) -> Shore | None:
    """Nearest water tile with a 4-adjacent buildable land tile, or None.

    Land may hold trees or rocks (clearable) but no cliff, entity or plan.
    """
    for water in water_tiles_near(world, x, y, radius):
        for (dx, dy), direction in _SHORE_SIDES:
            land = (water[0] + dx, water[1] + dy)
            if world.peek_terrain(*land) == "land" and world.is_buildable(*land):
                return Shore(water, land, direction)
    return None
