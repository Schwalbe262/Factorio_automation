"""Find free building sites near given points on a surveyed map.

This replaces lists of fixed map coordinates: a block asks for rectangles of
its footprint size, nearest first, that hold no ore, water, cliffs or
entities. Trees and rocks never reject or move a site (``site.nature``): the
builder clears them in batches, and their count only breaks distance ties.
"""

from __future__ import annotations

from collections.abc import Iterable

from . import nature
from .geometry import Rect
from .survey import survey_area
from .worldmap import WorldMap

SEARCH_MARGIN_TILES = 128


def free_sites(
    world: WorldMap,
    around: Iterable[tuple[float, float]],
    width: int,
    height: int,
    *,
    step: int = 8,
    limit: int = 16,
    margin: int = 2,
) -> list[Rect]:
    """Free ``width`` x ``height`` tile rectangles, nearest to ``around`` first.

    Candidates lie on a ``step``-tile grid inside the surveyed bounds. A site is
    accepted when the rectangle plus ``margin`` tiles on every side has no
    blocked tile and no resource tile; trees and rocks are allowed in any number.
    Distance is the sum of Manhattan distances from the rectangle centre to
    every point in ``around``; the tree/rock count only orders equal distances.
    """
    points = list(around)
    bounds = world.bounds()
    if bounds is None or not points:
        return []
    first_x = bounds.x0 + margin + (-(bounds.x0 + margin)) % step
    first_y = bounds.y0 + margin + (-(bounds.y0 + margin)) % step
    origins = [
        (x, y)
        for x in range(first_x, bounds.x1 - width - margin + 1, step)
        for y in range(first_y, bounds.y1 - height - margin + 1, step)
    ]

    def distance(origin: tuple[int, int]) -> float:
        centre_x = origin[0] + width / 2
        centre_y = origin[1] + height / 2
        return sum(abs(centre_x - px) + abs(centre_y - py) for px, py in points)

    found: list[tuple[float, int, Rect]] = []
    for x, y in sorted(origins, key=lambda origin: (distance(origin), origin)):
        rect = Rect(x, y, x + width, y + height)
        # Overlapping neighbours fail for the same reason (the same blocked
        # belt corridor), so offer distinct places instead of one cluster.
        if any(rect.intersects(other) for _, _, other in found):
            continue
        report = world.free_rect(x - margin, y - margin, x + width + margin, y + height + margin)
        if report.free and not report.resources:
            # Trees and rocks are cleared before building; position decides.
            found.append((distance((x, y)), len(report.clearable), rect))
            if len(found) == limit:
                break
    return nature.ranked(found)


def survey_around(game, points: Iterable[tuple[float, float]], *, margin: int = SEARCH_MARGIN_TILES) -> WorldMap:
    """Survey the bounding box of ``points`` widened by ``margin`` tiles."""
    points = list(points)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    area = Rect(int(min(xs)) - margin, int(min(ys)) - margin, int(max(xs)) + margin, int(max(ys)) + margin)
    return survey_area(game, area).world
