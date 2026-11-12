"""Designed orientation: which edge material enters and leaves each zone.

Rules (all derived from the map geometry, never a fixed compass default):

* **Bus**: items flow from the head (next to smelting) towards the open
  land; ``input_side`` is the head edge, ``output_side`` the tail edge.
* **Smelting**: ``output_side`` is the edge facing the bus head.
  ``input_side`` is chosen among the other three edges by routing every
  feeder to it and taking the smallest total
  ``length + crossing_weight * crossings + unroutable_penalty * failures``,
  plus ``turn_penalty`` when the edge is not opposite the output (so ore
  prefers to flow straight through). Feeders get entry slots on the input
  edge in the lateral order of their mining zones (west to east on a
  north/south edge, north to south on an east/west edge); if feeders still
  cross, pairs of slots are swapped while that lowers the cost, so swapping
  entry order is preferred to crossing. A feeder that fits no
  straight/single-elbow shape falls back to a two-elbow Z/U shape.
* **Mining zone**: ``output_side`` is the edge its feeder leaves from
  (facing the consumer); if the feeder is unroutable, the edge facing the
  consumer's centre.
* **Power**: water enters on the shore edge, coal on the edge its feeder
  arrives at (or the edge facing the coal zone), electricity leaves on the
  edge facing the bus head.

All functions are pure; they return new :class:`Zone` / :class:`Corridor`
values. Units are tiles, points are map coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from .corridors import OPPOSITE_SIDE, Route, count_crossings, route_double, route_sided
from .geometry import Rect
from .planmodel import SiteConfig
from .planzones import SIDE_NAMES, Corridor, Port, Zone
from .tilegrid import Occupancy

SIDE_VECTOR = {"N": (0, -1), "E": (1, 0), "S": (0, 1), "W": (-1, 0)}


def edge_range(rect: Rect, side: str) -> tuple[int, int]:
    """Tile interval of an edge along its own axis (x for N/S, y for E/W)."""
    if side in ("N", "S"):
        return (rect.x0, rect.x1)
    return (rect.y0, rect.y1)


def edge_point(rect: Rect, side: str, band: tuple[int, int] | None = None) -> tuple[float, float]:
    """Map point on edge ``side`` at the middle of ``band`` (default: whole edge)."""
    lo, hi = band if band is not None else edge_range(rect, side)
    middle = (lo + hi) / 2
    if side == "N":
        return (middle, float(rect.y0))
    if side == "S":
        return (middle, float(rect.y1))
    if side == "W":
        return (float(rect.x0), middle)
    return (float(rect.x1), middle)


def facing_side(rect: Rect, point: tuple[float, float], exclude=()) -> str:
    """Edge of ``rect`` that faces ``point`` best (largest dot product of the
    edge normal with centre->point), skipping ``exclude``."""
    cx, cy = rect.centre()
    ranked = sorted(
        (-(SIDE_VECTOR[s][0] * (point[0] - cx) + SIDE_VECTOR[s][1] * (point[1] - cy)), index, s)
        for index, s in enumerate(SIDE_NAMES)
    )
    return next(s for _score, _index, s in ranked if s not in exclude)


def sides_facing(rect: Rect, point: tuple[float, float]) -> list[str]:
    """All four edges, best facing ``point`` first.

    Loops that keep the first of equally cheap options iterate in this order,
    so ties are broken by geometry rather than by a fixed compass order.
    """
    ordered = []
    for _ in SIDE_NAMES:
        ordered.append(facing_side(rect, point, exclude=ordered))
    return ordered


def entry_slots(edge: tuple[int, int], count: int) -> list[tuple[int, int]]:
    """Split an edge interval into ``count`` consecutive slots.

    Boundaries are rounded towards the nearer end, so the slots of a
    mirrored (even-length) edge are the mirrored slots.
    """
    if count <= 0:
        return []
    lo, hi = edge
    size = hi - lo
    bounds = []
    for i in range(count + 1):
        if 2 * i <= count:
            bounds.append(lo + i * size // count)
        else:
            bounds.append(hi - (count - i) * size // count)
    return list(zip(bounds, bounds[1:]))


def lateral_order(zones: list[Zone], side: str) -> list[Zone]:
    """Zones sorted by their centre along the axis of edge ``side``."""
    axis = 0 if side in ("N", "S") else 1
    return sorted(zones, key=lambda z: (z.rect.centre()[axis], z.name))


@dataclass(frozen=True)
class Feeder:
    """Result of routing one feeder; ``route`` is None when unroutable."""

    material: str
    source: Zone
    from_side: str
    to_side: str
    route: Route | None
    cost: float
    slot: tuple[int, int]


def best_feeder(
    occupancy: Occupancy,
    source: Zone,
    material: str,
    target: Rect,
    t: str,
    slot: tuple[int, int] | None,
    others: list[tuple[Rect, ...]],
    config: SiteConfig,
    allow_double: bool = True,
) -> Feeder:
    """Cheapest feeder from any usable edge of ``source`` into edge ``t``.

    Straight and single-elbow shapes come first. Only when none exists and
    ``allow_double`` is set, a two-elbow Z/U shape is tried (anywhere on the
    edge, cost + ``turn_penalty``).
    """
    best = None
    for s in sides_facing(source.rect, target.centre()):
        if s == t:
            continue
        found = route_sided(
            occupancy,
            source.rect,
            s,
            target,
            t,
            config.corridor_width,
            entry=slot,
            others=others,
            crossing_weight=config.crossing_weight,
        )
        if found is not None and (best is None or found[1] < best.cost):
            best = Feeder(material, source, s, t, found[0], found[1], slot or edge_range(target, t))
    if best is None and allow_double:
        best = _best_double(occupancy, source, material, target, t, others, config)
    if best is None:
        fallback = facing_side(source.rect, edge_point(target, t), exclude=(t,))
        return Feeder(material, source, fallback, t, None, config.unroutable_penalty, slot or edge_range(target, t))
    return best


def _best_double(occupancy, source: Zone, material: str, target: Rect, t: str, others, config) -> Feeder | None:
    best = None
    for s in sides_facing(source.rect, target.centre()):
        if s not in (OPPOSITE_SIDE[t], t):
            continue
        found = route_double(
            occupancy,
            source.rect,
            s,
            target,
            t,
            config.corridor_width,
            others=others,
            crossing_weight=config.crossing_weight,
        )
        if found is not None:
            cost = found[1] + config.turn_penalty
            if best is None or cost < best.cost:
                best = Feeder(material, source, s, t, found[0], cost, edge_range(target, t))
    return best


def route_into_edge(
    occupancy: Occupancy, sources: list[tuple[str, Zone]], target: Rect, t: str, config: SiteConfig
) -> tuple[list[Feeder], float]:
    """Route ``(material, zone)`` sources to the slots of edge ``t``.

    The i-th source gets the i-th slot. Routing happens in material-name order
    (not slot order), so a mirrored map routes - and counts crossings - in the
    same sequence. Feeders are returned in slot order.
    """
    slots = entry_slots(edge_range(target, t), len(sources))
    feeders = []
    routed: list[tuple[Rect, ...]] = []
    total = 0.0
    for (material, zone), slot in sorted(zip(sources, slots), key=lambda item: item[0][0]):
        feeder = best_feeder(occupancy, zone, material, target, t, slot, routed, config, allow_double=False)
        if feeder.route is None:
            # Its own slot is blocked: allow any entry point on the edge.
            feeder = best_feeder(occupancy, zone, material, target, t, None, routed, config)
        feeders.append(feeder)
        total += feeder.cost
        if feeder.route is not None:
            routed.append(feeder.route.rects)
    slot_of = {material: slot for (material, _zone), slot in zip(sources, slots)}
    feeders.sort(key=lambda f: slot_of[f.material])
    return feeders, total


def feeder_crossings(feeders: list[Feeder]) -> int:
    """Pairs of routed feeders whose legs intersect."""
    rects = [f.route.rects for f in feeders if f.route is not None]
    return sum(count_crossings(rects[i], rects[i + 1 :]) for i in range(len(rects)))


def choose_input_side(
    occupancy: Occupancy, smelting: Rect, output_side: str, sources: list[tuple[str, Zone]], config: SiteConfig
) -> tuple[str, list[Feeder]]:
    """Input edge of the smelting zone and its feeders, in slot order (see module rules)."""
    opposite = OPPOSITE_SIDE[output_side]
    feed_centre = _mean_centre([zone for _material, zone in sources]) or smelting.centre()
    facing = sides_facing(smelting, feed_centre)
    sides = [opposite] + [s for s in facing if s not in (opposite, output_side)]
    best = None
    for t in sides:
        ordered = _ordered_sources(sources, t)
        feeders, total = route_into_edge(occupancy, ordered, smelting, t, config)
        if t != opposite:
            total += config.turn_penalty
        if best is None or total < best[0]:
            best = (total, t, feeders)
    if not sources or feeder_crossings(best[2]) == 0:
        return best[1], best[2]
    return best[1], _untangle(occupancy, smelting, best[1], best[2], best[0], config)


def _mean_centre(zones: list[Zone]) -> tuple[float, float] | None:
    if not zones:
        return None
    xs = [zone.rect.centre()[0] for zone in zones]
    ys = [zone.rect.centre()[1] for zone in zones]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def _ordered_sources(sources: list[tuple[str, Zone]], t: str) -> list[tuple[str, Zone]]:
    by_zone = {zone.name: material for material, zone in sources}
    return [(by_zone[zone.name], zone) for zone in lateral_order([z for _m, z in sources], t)]


def _untangle(occupancy, smelting: Rect, t: str, feeders: list[Feeder], total: float, config) -> list[Feeder]:
    """Swap entry slots of two feeders at a time while that lowers the cost
    (crossings are part of the cost), so swapping is preferred to crossing."""
    order = [(f.material, f.source) for f in feeders]
    best = (total, feeders)
    improved = True
    while improved:
        improved = False
        # Pairs in material-name order, so mirrored maps try the same swaps.
        names = sorted(material for material, _zone in order)
        for first, second in combinations(names, 2):
            i = next(k for k, item in enumerate(order) if item[0] == first)
            j = next(k for k, item in enumerate(order) if item[0] == second)
            trial = list(order)
            trial[i], trial[j] = trial[j], trial[i]
            candidate, cost = route_into_edge(occupancy, trial, smelting, t, config)
            if cost < best[0]:
                best = (cost, candidate)
                order = trial
                improved = True
    return best[1]


def feeder_corridor(feeder: Feeder, target_name: str, width: int) -> Corridor | None:
    if feeder.route is None:
        return None
    return Corridor(
        name=f"corridor:{feeder.material}->{target_name}",
        kind="feeder",
        material=feeder.material,
        source=feeder.source.name,
        target=target_name,
        width=width,
        from_side=feeder.from_side,
        to_side=feeder.to_side,
        segments=feeder.route.segments,
    )


def entry_port(feeder: Feeder, target: Rect) -> Port:
    band = feeder.route.entry_band if feeder.route is not None else feeder.slot
    return Port(feeder.material, "in", feeder.to_side, edge_point(target, feeder.to_side, band))


def exit_port(feeder: Feeder) -> Port:
    band = feeder.route.exit_band if feeder.route is not None else None
    return Port(feeder.material, "out", feeder.from_side, edge_point(feeder.source.rect, feeder.from_side, band))
