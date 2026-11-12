"""Placing the power zone on a shore.

The power zone (``power_width`` x ``power_height`` tiles, either
orientation) must hold a shore land tile on its water-facing edge so an
offshore pump can sit on the water tile next to it. Among all such clean
rects within ``shore_search_radius`` of the bus head, the one whose centre is
closest to the head wins. Every rect position is tried (step 1) so a mirrored
map yields the mirrored zone. Distances are Euclidean, in tiles.
"""

from __future__ import annotations

import math

from .geometry import Rect
from .patches import Shore
from .planmodel import SiteConfig
from .tilegrid import Occupancy
from .worldmap import WATER, WorldMap

# (dx, dy) from water to land, and the 16-way direction from land to water.
SHORE_SIDES = (((0, 1), 0), ((-1, 0), 4), ((0, -1), 8), ((1, 0), 12))
DIRECTION_COMPASS = {0: "N", 4: "E", 8: "S", 12: "W"}


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def shore_candidates(
    world: WorldMap, occupancy: Occupancy, head: tuple[float, float], config: SiteConfig
) -> list[tuple[float, Shore]]:
    """``(distance, shore)`` for water tiles with a clean 4-adjacent land tile
    within ``shore_search_radius`` of ``head``, nearest first."""
    found = []
    terrain_tiles = world.terrain_tiles()
    for (x, y), terrain in terrain_tiles.items():
        if terrain != WATER:
            continue
        distance = _distance(head, (x + 0.5, y + 0.5))
        if distance > config.shore_search_radius:
            continue
        for (dx, dy), direction in SHORE_SIDES:
            land = (x + dx, y + dy)
            if land in terrain_tiles:
                continue
            if occupancy.clean(Rect(land[0], land[1], land[0] + 1, land[1] + 1)):
                found.append((distance, y, x, direction, land))
    found.sort()
    return [(distance, Shore((x, y), land, direction)) for distance, y, x, direction, land in found]


def power_rects_at(shore: Shore, config: SiteConfig) -> list[Rect]:
    """Power rects (both orientations) whose water-facing edge holds the land tile."""
    lx, ly = shore.land
    rects = []
    for width, height in ((config.power_width, config.power_height), (config.power_height, config.power_width)):
        if shore.direction in (0, 8):
            y0 = ly if shore.direction == 0 else ly + 1 - height
            for x0 in range(lx - width + 1, lx + 1):
                rects.append(Rect(x0, y0, x0 + width, y0 + height))
        else:
            x0 = lx + 1 - width if shore.direction == 4 else lx
            for y0 in range(ly - height + 1, ly + 1):
                rects.append(Rect(x0, y0, x0 + width, y0 + height))
    return rects


def place_power(
    world: WorldMap, occupancy: Occupancy, head: tuple[float, float], config: SiteConfig
) -> tuple[Rect, Shore | None, str] | None:
    """Power zone on a shore, centre closest to ``head``.

    Returns ``(rect, shore, water_side)`` where ``water_side`` is the zone
    edge facing the water. Shores are scanned nearest first and the scan stops
    once no rect touching a farther shore can beat the best one. Falls back
    to a clean rect near the head with ``shore`` None and side "".
    """
    reach = math.hypot(config.power_width, config.power_height) / 2
    best = None
    for distance, shore in shore_candidates(world, occupancy, head, config):
        if best is not None and distance - reach - 1 > best[0][0]:
            break
        for rect in power_rects_at(shore, config):
            # Ties prefer the pump in the middle of the edge (mirror invariant).
            pump = (shore.water[0] + 0.5, shore.water[1] + 0.5)
            key = (_distance(rect.centre(), head), _distance(rect.centre(), pump), rect.y0, rect.x0)
            if (best is None or key < best[0]) and occupancy.clean(rect):
                best = (key, rect, shore)
    if best is not None:
        return best[1], best[2], DIRECTION_COMPASS[best[2].direction]
    rect = nearest_clean_rect(occupancy, head, config.power_width, config.power_height, config.shore_search_radius)
    if rect is None:
        return None
    return rect, None, ""


def nearest_clean_rect(
    occupancy: Occupancy, centre: tuple[float, float], width: int, height: int, radius: int
) -> Rect | None:
    """Clean ``width`` x ``height`` rect whose centre is nearest ``centre`` (4-tile grid)."""
    cx = int(math.floor(centre[0])) - width // 2
    cy = int(math.floor(centre[1])) - height // 2
    options = []
    for dy in range(-radius, radius + 1, 4):
        for dx in range(-radius, radius + 1, 4):
            options.append((math.hypot(dx, dy), dy, dx))
    for _distance_, dy, dx in sorted(options):
        rect = Rect(cx + dx, cy + dy, cx + dx + width, cy + dy + height)
        if occupancy.clean(rect):
            return rect
    return None
