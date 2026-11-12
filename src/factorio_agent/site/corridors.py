"""Rect arithmetic and side-constrained corridor routing (up to two elbows).

A corridor of width ``w`` leaves source rect ``a`` through edge ``s`` and
enters target rect ``b`` through edge ``t`` (compass sides, see
:mod:`site.planzones`). With at most one elbow only two cases exist:

* ``t`` opposite ``s``: one straight leg, travelling in direction ``s``;
* ``t`` perpendicular to ``s``: a leg travelling ``s`` then a leg
  travelling opposite ``t`` (into ``b``).

When neither exists, :func:`route_double` is the fallback: a Z (``t``
opposite ``s``) or U (``t`` equal to ``s``) with two elbows.

Legs start exactly at the source edge and end exactly at the target edge, so
they never overlap either zone. Vertical-first shapes are computed as
horizontal-first shapes on the transposed (x <-> y) geometry. All units are
tiles; every band position is tried (step 1) so mirrored maps get mirrored
candidate sets.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from .geometry import Rect
from .planzones import Segment
from .tilegrid import Occupancy

OPPOSITE_SIDE = {"N": "S", "S": "N", "E": "W", "W": "E"}
# Transposing x <-> y maps east (1, 0) to south (0, 1) and north to west.
TRANSPOSE_SIDE = {"N": "W", "W": "N", "S": "E", "E": "S"}


def subtract_rects(rect: Rect, holes: list[Rect]) -> list[Rect]:
    """Cover ``rect`` minus ``holes`` with disjoint rects (deterministic order).

    The rect is cut into vertical slabs at every hole edge; each slab keeps
    the rows no hole covers; neighbouring slabs with identical rows merge.
    """
    inner = [h for h in holes if h.intersects(rect)]
    if not inner:
        return [rect] if rect.area else []
    cuts = {rect.x0, rect.x1}
    for hole in inner:
        cuts.add(min(max(hole.x0, rect.x0), rect.x1))
        cuts.add(min(max(hole.x1, rect.x0), rect.x1))
    xs = sorted(cuts)
    open_rects: dict[tuple[int, int], Rect] = {}
    done: list[Rect] = []
    for xa, xb in zip(xs, xs[1:]):
        rows = _free_rows(rect, [h for h in inner if h.x0 < xb and xa < h.x1])
        for key in list(open_rects):
            if key not in rows:
                done.append(open_rects.pop(key))
        for y0, y1 in rows:
            current = open_rects.get((y0, y1))
            if current is None:
                open_rects[(y0, y1)] = Rect(xa, y0, xb, y1)
            else:
                open_rects[(y0, y1)] = Rect(current.x0, y0, xb, y1)
    done.extend(open_rects.values())
    return sorted(done, key=lambda r: (r.y0, r.x0, r.y1, r.x1))


def _free_rows(rect: Rect, holes: list[Rect]) -> list[tuple[int, int]]:
    """Row intervals of ``rect`` not covered by any of ``holes``."""
    spans = sorted((max(h.y0, rect.y0), min(h.y1, rect.y1)) for h in holes)
    free = []
    y = rect.y0
    for y0, y1 in spans:
        if y0 > y:
            free.append((y, y0))
        y = max(y, y1)
    if y < rect.y1:
        free.append((y, rect.y1))
    return free


def transpose_rect(rect: Rect) -> Rect:
    return Rect(rect.y0, rect.x0, rect.y1, rect.x1)


@dataclass(frozen=True)
class Route:
    """A candidate corridor. ``exit_band`` / ``entry_band`` are the tile
    interval the corridor occupies along the source / target edge."""

    segments: tuple[Segment, ...]
    exit_band: tuple[int, int]
    entry_band: tuple[int, int]

    @property
    def rects(self) -> tuple[Rect, ...]:
        return tuple(s.rect for s in self.segments)

    def length(self, width: int) -> int:
        return sum(r.area for r in self.rects) // width


def _make_route(legs: list[tuple[Rect, str]], exit_band, entry_band) -> Route:
    segments = tuple(Segment(rect, direction) for rect, direction in legs if rect.area)
    return Route(segments, exit_band, entry_band)


def _straight_east_west(a: Rect, s: str, b: Rect, width: int) -> list[Route]:
    """Straight legs leaving ``a`` east or west into ``b``'s opposite edge."""
    if s == "E" and b.x0 >= a.x1:
        x0, x1 = a.x1, b.x0
    elif s == "W" and b.x1 <= a.x0:
        x0, x1 = b.x1, a.x0
    else:
        return []
    routes = []
    for y in range(max(a.y0, b.y0), min(a.y1, b.y1) - width + 1):
        routes.append(_make_route([(Rect(x0, y, x1, y + width), s)], (y, y + width), (y, y + width)))
    return routes


def _elbow_east_west(a: Rect, s: str, b: Rect, t: str, width: int) -> list[Route]:
    """Horizontal leg leaving ``a`` (side E/W), vertical leg entering ``b`` (side N/S)."""
    routes = []
    for yc in range(a.y0, a.y1 - width + 1):
        # The vertical leg needs at least one tile so material really enters
        # through the target's north/south edge.
        if t == "N" and b.y0 <= yc + width:
            continue
        if t == "S" and b.y1 >= yc:
            continue
        for xc in range(b.x0, b.x1 - width + 1):
            if s == "E" and xc >= a.x1:
                leg1 = Rect(a.x1, yc, xc + width, yc + width)
            elif s == "W" and xc + width <= a.x0:
                leg1 = Rect(xc, yc, a.x0, yc + width)
            else:
                continue
            if t == "N":
                leg2 = Rect(xc, yc + width, xc + width, b.y0)
            else:
                leg2 = Rect(xc, b.y1, xc + width, yc)
            legs = [(leg1, s), (leg2, OPPOSITE_SIDE[t])]
            routes.append(_make_route(legs, (yc, yc + width), (xc, xc + width)))
    return routes


def _horizontal_first(a: Rect, s: str, b: Rect, t: str, width: int) -> list[Route]:
    if t == OPPOSITE_SIDE[s]:
        return _straight_east_west(a, s, b, width)
    if t in ("N", "S"):
        return _elbow_east_west(a, s, b, t, width)
    return []


def _transpose_route(route: Route) -> Route:
    segments = tuple(Segment(transpose_rect(g.rect), TRANSPOSE_SIDE[g.direction]) for g in route.segments)
    return Route(segments, route.exit_band, route.entry_band)


@lru_cache(maxsize=512)
def sided_candidates(a: Rect, s: str, b: Rect, t: str, width: int) -> tuple[Route, ...]:
    """Every corridor shape leaving ``a`` through ``s`` and entering ``b`` through ``t``.

    Pure function of its (immutable) arguments, so results are cached: the
    planner asks for the same zone pair many times with different slots.
    """
    if s in ("E", "W"):
        return tuple(_horizontal_first(a, s, b, t, width))
    flipped = _horizontal_first(transpose_rect(a), TRANSPOSE_SIDE[s], transpose_rect(b), TRANSPOSE_SIDE[t], width)
    return tuple(_transpose_route(route) for route in flipped)


def symmetric_samples(low: int, high: int, step: int) -> list[int]:
    """Every ``step``-th integer of ``[low, high]`` counted from both ends.

    The set is the same when the interval is mirrored, so sampled shapes on a
    mirrored map are the mirror of the original shapes.
    """
    if high < low:
        return []
    return sorted(set(range(low, high + 1, step)) | set(range(high, low - 1, -step)))


def _double_params(a: Rect, s: str, b: Rect, t: str, width: int, step: int) -> list[tuple[int, int, int, int]]:
    """``(length, centring, y1, y2, xm)`` of two-elbow shapes for ``s``, ``t`` in E/W.

    Leg 1 leaves ``a`` horizontally in band ``y1``; leg 2 runs vertically in
    column ``[xm, xm + width)`` from band ``y1`` to band ``y2``; leg 3 runs
    horizontally in band ``y2`` into ``b``. ``t == opposite(s)`` gives a Z,
    ``t == s`` a U. Every leg is at least one tile long.
    """
    travel = OPPOSITE_SIDE[t]
    reach = 2 * width + 32
    columns = []
    for xm in symmetric_samples(min(a.x0, b.x0) - reach, max(a.x1, b.x1) + reach - width, step):
        leg1_ok = xm > a.x1 if s == "E" else xm + width < a.x0
        leg3_ok = b.x0 > xm + width if travel == "E" else b.x1 < xm
        if leg1_ok and leg3_ok:
            columns.append(xm)
    params = []
    for y1 in symmetric_samples(a.y0, a.y1 - width, step):
        for y2 in symmetric_samples(b.y0, b.y1 - width, step):
            if y1 == y2:
                continue
            for xm in columns:
                leg1 = (xm - a.x1) if s == "E" else (a.x0 - xm - width)
                leg3 = (b.x0 - xm - width) if travel == "E" else (xm - b.x1)
                length = leg1 + leg3 + abs(y2 - y1) + width
                centring = abs(y1 + width / 2 - (a.y0 + a.y1) / 2) + abs(y2 + width / 2 - (b.y0 + b.y1) / 2)
                params.append((length, centring, y1, y2, xm))
    params.sort()
    return params


def _double_route(a: Rect, s: str, b: Rect, t: str, width: int, y1: int, y2: int, xm: int) -> Route:
    leg1 = Rect(a.x1, y1, xm, y1 + width) if s == "E" else Rect(xm + width, y1, a.x0, y1 + width)
    leg2 = Rect(xm, min(y1, y2), xm + width, max(y1, y2) + width)
    travel = OPPOSITE_SIDE[t]
    leg3 = Rect(xm + width, y2, b.x0, y2 + width) if travel == "E" else Rect(b.x1, y2, xm, y2 + width)
    legs = [(leg1, s), (leg2, "S" if y2 > y1 else "N"), (leg3, travel)]
    return _make_route(legs, (y1, y1 + width), (y2, y2 + width))


def route_double(
    occupancy: Occupancy,
    a: Rect,
    s: str,
    b: Rect,
    t: str,
    width: int,
    *,
    others: list[tuple[Rect, ...]] | None = None,
    crossing_weight: float = 0.0,
    step: int = 4,
) -> tuple[Route, float] | None:
    """Cheapest clean two-elbow corridor (Z or U) with ``s`` parallel to ``t``.

    Fallback for when no straight or single-elbow route exists. Positions are
    sampled every ``step`` tiles. Candidates are scanned by length; crossings
    are added lazily and the scan stops once length alone exceeds the best
    cost. Returns ``(route, cost)`` or None.
    """
    if (s in ("E", "W")) != (t in ("E", "W")):
        return None
    transposed = s in ("N", "S")
    if transposed:
        a, b = transpose_rect(a), transpose_rect(b)
        s, t = TRANSPOSE_SIDE[s], TRANSPOSE_SIDE[t]
    others = others or []
    best = None
    for length, _centring, y1, y2, xm in _double_params(a, s, b, t, width, step):
        if best is not None and length > best[1]:
            break
        route = _double_route(a, s, b, t, width, y1, y2, xm)
        rects = tuple(transpose_rect(r) for r in route.rects) if transposed else route.rects
        if not all(occupancy.clean(rect) for rect in rects):
            continue
        cost = length + crossing_weight * count_crossings(rects, others)
        if best is None or cost < best[1]:
            best = (_transpose_route(route) if transposed else route, cost)
    return best


def _edge_middle(rect: Rect, side: str) -> float:
    """Middle of an edge along its own axis (x for N/S, y for E/W)."""
    if side in ("N", "S"):
        return (rect.x0 + rect.x1) / 2
    return (rect.y0 + rect.y1) / 2


def count_crossings(rects: tuple[Rect, ...], others: list[tuple[Rect, ...]]) -> int:
    """How many of the ``others`` corridors the legs ``rects`` intersect."""
    return sum(1 for other in others if any(r.intersects(o) for r in rects for o in other))


def route_sided(
    occupancy: Occupancy,
    a: Rect,
    s: str,
    b: Rect,
    t: str,
    width: int,
    *,
    entry: tuple[int, int] | None = None,
    others: list[tuple[Rect, ...]] | None = None,
    crossing_weight: float = 0.0,
) -> tuple[Route, float] | None:
    """Cheapest clean corridor from edge ``s`` of ``a`` to edge ``t`` of ``b``.

    cost = length (tiles) + ``crossing_weight`` x number of ``others``
    corridors crossed. ``entry`` limits the entry band to a sub-interval of
    ``b``'s edge (an entry slot). Legs must be clean in ``occupancy``. Equal
    costs prefer bands nearest the middle of the exit edge and of the entry
    slot (a mirror-invariant tie-break). Returns ``(route, cost)`` or None.
    """
    others = others or []
    scored = []
    exit_mid = _edge_middle(a, s)
    entry_mid = sum(entry) / 2 if entry is not None else _edge_middle(b, t)
    for order, route in enumerate(sided_candidates(a, s, b, t, width)):
        if entry is not None and not (entry[0] <= route.entry_band[0] and route.entry_band[1] <= entry[1]):
            continue
        cost = route.length(width)
        if others:
            cost += crossing_weight * count_crossings(route.rects, others)
        centring = abs(sum(route.exit_band) / 2 - exit_mid) + abs(sum(route.entry_band) / 2 - entry_mid)
        scored.append((cost, centring, order, route))
    scored.sort(key=lambda item: item[:3])
    for cost, _centring, _order, route in scored:
        if all(occupancy.clean(rect) for rect in route.rects):
            return route, cost
    return None
