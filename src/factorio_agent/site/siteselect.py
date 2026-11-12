"""Choosing patches, the main bus, smelting and district strips.

Pure functions over a :class:`WorldMap` and an :class:`Occupancy` snapshot.
Lower scores are better everywhere. Distances are Euclidean, in tiles.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from .busframe import COMPASS, Bus, dominant_compass, frame_rect
from .geometry import Rect
from .patches import Patch
from .planmodel import PatchRef, ResourcePlan, SiteConfig, Strip
from .planzones import Zone
from .tilegrid import Occupancy
from .worldmap import WorldMap


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _start_point(start: tuple[int, int]) -> tuple[float, float]:
    """Map point at the centre of the start tile."""
    return (start[0] + 0.5, start[1] + 0.5)


# ----- patches -------------------------------------------------------------
def patch_ref(patch: Patch, start: tuple[int, int], config: SiteConfig) -> PatchRef:
    """Score one patch: ``distance + small_patch_penalty * deficit``.

    ``deficit = max(0, 1 - min(tiles / early_patch_tiles, amount /
    early_patch_amount))`` is 0 for a patch big enough for the early game and
    approaches 1 for a tiny one. Well (fluid) patches score distance only.
    """
    distance = _distance(_start_point(start), patch.centroid)
    if patch.wells:
        score = distance
    else:
        size = min(patch.tile_count / config.early_patch_tiles, patch.amount / config.early_patch_amount)
        score = distance + config.small_patch_penalty * max(0.0, 1.0 - size)
    return PatchRef(
        patch.name,
        patch.tile_count,
        patch.amount,
        patch.bbox,
        patch.centroid,
        round(distance, 2),
        round(score, 2),
        len(patch.wells),
    )


def rank_patches(world: WorldMap, start: tuple[int, int], config: SiteConfig) -> dict[str, list[PatchRef]]:
    """Every patch on the map, grouped by resource name, best score first."""
    ranked: dict[str, list[PatchRef]] = {}
    for patch in world.resource_patches():
        ranked.setdefault(patch.name, []).append(patch_ref(patch, start, config))
    for refs in ranked.values():
        refs.sort(key=lambda r: (r.score, r.bbox.y0, r.bbox.x0))
    return ranked


def _inflate(rect: Rect, margin: int, bounds: Rect) -> Rect:
    return Rect(
        max(bounds.x0, rect.x0 - margin),
        max(bounds.y0, rect.y0 - margin),
        min(bounds.x1, rect.x1 + margin),
        min(bounds.y1, rect.y1 + margin),
    )


def mining_rect(ref: PatchRef, occupancy: Occupancy, blocked: list[Rect], config: SiteConfig) -> Rect | None:
    """Patch bbox plus the largest margin (config, shrinking to 0) that does
    not overlap taken zones or ``blocked`` rects (zones already on the map)."""
    for margin in range(config.mining_margin, -1, -1):
        rect = _inflate(ref.bbox, margin, occupancy.grid.bounds)
        others = occupancy.taken + blocked
        if not any(rect.intersects(other) for other in others):
            return rect
    return None


def choose_resources(
    world: WorldMap, occupancy: Occupancy, start: tuple[int, int], config: SiteConfig, warnings: list[str]
) -> tuple[list[ResourcePlan], list[Zone]]:
    """Pick the primary patch of every required solid and its mining zone.

    Required solids are handled in config order; each takes the best-scored
    patch whose mining zone fits. Other resources (fluids, uranium, ...) are
    listed ranked without a primary. Mining zones are added to ``occupancy``.
    """
    ranked = rank_patches(world, start, config)
    existing = [rect for _owner, rect in world.zones()]
    plans = []
    zones = []
    for name in config.required_solids:
        refs = ranked.get(name, [])
        primary = None
        for ref in refs:
            rect = mining_rect(ref, occupancy, existing, config)
            if rect is not None:
                primary = ref
                growth = dominant_compass(ref.centroid[0] - start[0], ref.centroid[1] - start[1])
                zones.append(Zone("mining", "mining:" + name, rect, growth))
                occupancy.taken.append(rect)
                break
        if primary is None:
            warnings.append(f"no usable {name} patch on the surveyed map")
        others = tuple(r for r in refs if r is not primary)
        plans.append(ResourcePlan(name, primary, others))
    for name in sorted(ranked):
        if name not in config.required_solids:
            plans.append(ResourcePlan(name, None, tuple(ranked[name])))
    return plans, zones


# ----- main bus ------------------------------------------------------------
@dataclass(frozen=True)
class BusChoice:
    """A scored bus candidate. ``side_clean`` is (left, right) clean share."""

    origin: tuple[int, int]
    direction: str
    length: int
    score: float
    side_clean: tuple[float, float]
    smelting_clean: bool


def _lengths(config: SiteConfig) -> list[int]:
    lengths = list(range(config.bus_min_length, config.bus_target_length, config.bus_length_step))
    lengths.append(config.bus_target_length)
    return lengths


def longest_clean_length(occupancy: Occupancy, origin: tuple[int, int], direction: str, config: SiteConfig) -> int:
    """Longest candidate length (multiple of the step, capped at the target)
    whose bus rect is clean, or 0 when even the minimum is not."""
    half = config.bus_width // 2
    lengths = _lengths(config)
    low = 0
    high = len(lengths) - 1
    best = 0
    while low <= high:
        middle = (low + high) // 2
        rect = frame_rect(origin, direction, 0, lengths[middle], -half, half)
        if occupancy.clean(rect):
            best = lengths[middle]
            low = middle + 1
        else:
            high = middle - 1
    return best


def smelting_rect_ideal(origin: tuple[int, int], direction: str, config: SiteConfig) -> Rect:
    """Smelting zone directly behind the bus head, beyond the head gap."""
    a0 = -config.head_gap - config.smelting_depth
    half = config.smelting_width // 2
    return frame_rect(origin, direction, a0, -config.head_gap, -half, half)


def header_rect(origin: tuple[int, int], direction: str, config: SiteConfig) -> Rect:
    """Gap between smelting zone and bus head, the full bus width."""
    half = config.bus_width // 2
    return frame_rect(origin, direction, -config.head_gap, 0, -half, half)


def score_bus(
    occupancy: Occupancy,
    origin: tuple[int, int],
    direction: str,
    length: int,
    start: tuple[int, int],
    feeds: list[tuple[float, float]],
    config: SiteConfig,
    depth: int,
    mining: list[Rect],
) -> BusChoice | None:
    """Score a clean bus of ``length``; None if a side has too little room.

    score = (target_length - length)
          + side_weight * ((1 - left_clean) + (1 - right_clean))
          + feed_weight * sum(distance(feed patch centroid, smelting centre))
          + start_weight * distance(start, head)
          + clear_weight * tree/rock tiles in the bus
          + smelting_penalty if the ideal smelting zone + head gap is not clean
          + band_penalty * number of mining zones inside the district envelope
            (bus plus both full-depth side bands), which would split districts
    """
    bus = Bus(origin, direction, config.bus_width, length)
    far = config.district_setback + depth
    side_clean = tuple(occupancy.clean_fraction(bus.side_rect(side, 0, length, 0, far)) for side in ("left", "right"))
    if min(side_clean) < config.district_min_clean:
        return None
    smelting = smelting_rect_ideal(origin, direction, config)
    smelting_clean = occupancy.clean(smelting) and occupancy.clean(header_rect(origin, direction, config))
    centre = smelting.centre()
    score = config.bus_target_length - length
    score += config.side_weight * (2.0 - side_clean[0] - side_clean[1])
    score += config.feed_weight * sum(_distance(feed, centre) for feed in feeds)
    score += config.start_weight * _distance(_start_point(start), origin)
    score += config.clear_weight * occupancy.grid.clearable(bus.rect())
    if not smelting_clean:
        score += config.smelting_penalty
    envelope = bus.local_rect(0, length, -bus.half - far, bus.half + far)
    score += config.band_penalty * sum(1 for rect in mining if rect.intersects(envelope))
    return BusChoice(origin, direction, length, score, side_clean, smelting_clean)


def search_bus(
    occupancy: Occupancy, start: tuple[int, int], feeds: list[tuple[float, float]], config: SiteConfig, depth: int
) -> BusChoice | None:
    """Best bus over all four directions and heads within ``bus_search_radius``
    of the start tile centre.

    Heads are the map points whose coordinates are multiples of
    ``bus_search_step``; that lattice maps onto itself when the map is
    mirrored or rotated about the origin, so mirrored maps get mirrored plans.
    Exact score ties are broken by (direction order N, E, S, W; head y; head x).
    Mining zones are the rects already taken when the search starts.
    """
    mining = list(occupancy.taken)
    radius = config.bus_search_radius
    step = config.bus_search_step
    centre = _start_point(start)
    best = None
    best_key = None
    low_y = math.ceil((centre[1] - radius) / step) * step
    low_x = math.ceil((centre[0] - radius) / step) * step
    for oy in range(low_y, int(centre[1] + radius) + 1, step):
        for ox in range(low_x, int(centre[0] + radius) + 1, step):
            if _distance((ox, oy), centre) > radius:
                continue
            origin = (ox, oy)
            for order, direction in enumerate(COMPASS):
                length = longest_clean_length(occupancy, origin, direction, config)
                if length == 0:
                    continue
                choice = score_bus(occupancy, origin, direction, length, start, feeds, config, depth, mining)
                if choice is None:
                    continue
                key = (choice.score, order, origin[1], origin[0])
                if best_key is None or key < best_key:
                    best = choice
                    best_key = key
    return best


# ----- smelting and district strips ---------------------------------------
def place_smelting(
    occupancy: Occupancy, bus: Bus, config: SiteConfig, feeds: list[tuple[float, float]]
) -> tuple[Rect, bool] | None:
    """Smelting rect and whether it is in the ideal spot behind the head.

    When the ideal spot is not clean the zone slides further back and/or
    sideways (4-tile steps, smallest displacement first; equal displacements
    prefer the rect closest to the feed patch centroids ``feeds``).
    """
    ideal = smelting_rect_ideal(bus.origin, bus.direction, config)
    if occupancy.clean(ideal) and occupancy.clean(header_rect(bus.origin, bus.direction, config)):
        return ideal, True
    half = config.smelting_width // 2
    near = -config.head_gap
    options = []
    for back in range(0, config.smelting_depth + 1, 4):
        for shift in range(-config.smelting_width, config.smelting_width + 1, 4):
            a0 = near - config.smelting_depth - back
            rect = bus.local_rect(a0, near - back, -half + shift, half + shift)
            feed = sum(_distance(point, rect.centre()) for point in feeds)
            options.append((back + abs(shift), feed, back, shift, rect))
    for option in sorted(options, key=lambda o: o[:4]):
        if occupancy.clean(option[4]):
            return option[4], False
    return None


def strip_runs(occupancy: Occupancy, bus: Bus, side: str, depth: int, config: SiteConfig) -> list[Strip]:
    """Runs of clean bus-local rows on one side, each at least ``district_min_run`` long.

    A row is the 1-tile slice from the bus edge out to setback + depth.
    """
    far = config.district_setback + depth
    runs = []
    start = None
    for along in range(bus.length + 1):
        good = along < bus.length and occupancy.clean(bus.side_rect(side, along, along + 1, 0, far))
        if good and start is None:
            start = along
        if not good and start is not None:
            if along - start >= config.district_min_run:
                runs.append(Strip(side, start, along, bus.side_rect(side, start, along, 0, far)))
            start = None
    return runs
