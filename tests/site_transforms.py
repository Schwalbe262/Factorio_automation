"""Mirror / rotate helpers for site planner tests.

A transform maps map *points*; a tile is mapped through its square, so
flipping x sends tile ``x`` to ``-1 - x``. Compass letters follow the
same transform. Only maps whose surveyed bounds are symmetric about the
origin stay inside their own bounds.
"""

from factorio_agent.site.geometry import Rect
from factorio_agent.site.worldmap import Feature, Well, WorldMap

POINT = {
    "flip_x": lambda x, y: (-x, y),
    "flip_y": lambda x, y: (x, -y),
    "rotate": lambda x, y: (-y, x),
}
COMPASS = {
    "flip_x": {"N": "N", "S": "S", "E": "W", "W": "E", "": ""},
    "flip_y": {"N": "S", "S": "N", "E": "E", "W": "W", "": ""},
    "rotate": {"N": "E", "E": "S", "S": "W", "W": "N", "": ""},
}


def map_point(kind, point):
    return POINT[kind](point[0], point[1])


def map_rect(kind, rect: Rect) -> Rect:
    xa, ya = map_point(kind, (rect.x0, rect.y0))
    xb, yb = map_point(kind, (rect.x1, rect.y1))
    return Rect(min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb))


def map_tile(kind, tile):
    rect = map_rect(kind, Rect(tile[0], tile[1], tile[0] + 1, tile[1] + 1))
    return (rect.x0, rect.y0)


def map_side(kind, side: str) -> str:
    return COMPASS[kind][side]


def transform_world(world: WorldMap, kind: str) -> WorldMap:
    """Copy of ``world`` with every layer mirrored or rotated (no zones)."""
    out = WorldMap()
    out.mark_surveyed(map_rect(kind, world.bounds()))
    for tile, terrain in world.terrain_tiles().items():
        out.set_terrain(*map_tile(kind, tile), terrain)
    for tile, (name, amount) in world.ore_tiles().items():
        out.set_ore(*map_tile(kind, tile), name, amount)
    for well in world.wells():
        out.add_well(Well(well.name, map_tile(kind, well.centre), well.amount, well.size))
    for feature in world.features():
        out.add_feature(Feature(feature.kind, feature.name, map_rect(kind, feature.rect), feature.owner))
    return out
