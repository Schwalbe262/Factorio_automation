"""Rail reservations of the master plan (data only: nothing here builds rails).

Rules (all geometric, no fixed compass default):

* **Outpost direction** ``D``: the compass edge of the smelting zone that
  best faces the mean centre of the ranked non-primary patches (future
  outposts) of the required solids; with no such patch, the edge opposite
  the smelting output (away from the bus).
* **Unload station** ``rail:unload`` (trains -> ore into smelting): on a
  smelting edge that is neither the output edge nor an entry edge of a
  primary feeder, edges facing ``D`` first (only when every such edge
  fails, an edge shared with feeders, missing their corridors, with a
  warning: four feeds often use all three non-output edges). The station is
  ``rail_unload_width`` along that edge by ``rail_unload_length`` outwards,
  so its track axis (the zone's ``growth``) points away from smelting and
  the bus; positions along the edge are tried from the centre outwards.
  Smelting gets an ``ore`` entry port on that edge.
* **Trunk** ``rail:trunk``: a straight or single-elbow right-of-way
  ``rail_trunk_width`` wide from the station's far edge to the survey
  boundary in direction ``D`` (other directions only as a fallback, with a
  warning). It must be clean (no ore, water, cliffs, entities) and may not
  cross any zone or strip; feeder and power corridors may be crossed and
  each crossing is recorded (``rail_crossings``).
* **Loading stations** ``rail:load:<resource>:<n>``: for the nearest
  ``rail_outposts_max`` ranked non-primary patches with at least
  ``rail_outpost_min_tiles`` tiles, the patch bbox + ``mining_margin`` is
  reserved as ``mining:<resource>:<n>`` (kind "outpost") and a
  ``rail_load_length`` x ``rail_load_width`` station beside it, on the edge
  facing the trunk, outside the ore. Each primary mining zone may also get
  ``rail:load:<resource>:primary`` on its edge facing away from smelting
  when that land is clean (optional, no warning).
"""

from __future__ import annotations

from dataclasses import replace
import math

from .corridors import OPPOSITE_SIDE, route_sided
from .geometry import Rect
from .orientation import edge_point, edge_range, sides_facing
from .planzones import Corridor, Port, Zone

SIDE_VECTOR = {"N": (0, -1), "E": (1, 0), "S": (0, 1), "W": (-1, 0)}
SLIDE_STEP = 8
SLIDES = 3  # positions each way from the centre of an edge


def _beside(rect: Rect, side: str, along: int, depth: int, start: int) -> Rect:
    """``along`` x ``depth`` rect just outside edge ``side``, starting at ``start`` along it."""
    if side == "N":
        return Rect(start, rect.y0 - depth, start + along, rect.y0)
    if side == "S":
        return Rect(start, rect.y1, start + along, rect.y1 + depth)
    if side == "W":
        return Rect(rect.x0 - depth, start, rect.x0, start + along)
    return Rect(rect.x1, start, rect.x1 + depth, start + along)


def _stations(rect: Rect, side: str, along: int, depth: int, k: int) -> list[Rect]:
    """Rects beside edge ``side`` slid ``k * SLIDE_STEP`` either way from its centre.

    An odd slack has no exact centre: both roundings are offered, so the set
    is the mirror of the set on a mirrored map.
    """
    lo, hi = edge_range(rect, side)
    slack = lo + hi - along
    centres = {slack // 2, -(-slack // 2)}
    starts = sorted({c + sign * k * SLIDE_STEP for c in centres for sign in (-1, 1)})
    return [_beside(rect, side, along, depth, start) for start in starts]


def _boundary_strip(bounds: Rect, direction: str) -> Rect:
    """One-tile strip just outside the survey boundary in ``direction``."""
    if direction == "N":
        return Rect(bounds.x0, bounds.y0 - 1, bounds.x1, bounds.y0)
    if direction == "S":
        return Rect(bounds.x0, bounds.y1, bounds.x1, bounds.y1 + 1)
    if direction == "W":
        return Rect(bounds.x0 - 1, bounds.y0, bounds.x0, bounds.y1)
    return Rect(bounds.x1, bounds.y0, bounds.x1 + 1, bounds.y1)


def outpost_patches(draft) -> list:
    """(resource, rank, patch) of worthwhile non-primary patches, nearest first."""
    config = draft.config
    found = []
    for resource in draft.resources:
        for rank, patch in enumerate(resource.others, start=1):
            if patch.tile_count >= config.rail_outpost_min_tiles:
                found.append((patch.distance, resource.name, rank, patch))
    found.sort(key=lambda row: (row[0], row[1], row[2]))
    return [(name, rank, patch) for _d, name, rank, patch in found[: config.rail_outposts_max]]


def outpost_direction(draft, smelting: Zone) -> str:
    patches = [patch for _name, _rank, patch in outpost_patches(draft)]
    if not patches:
        patches = [p for r in draft.resources for p in r.others]
    if not patches:
        return OPPOSITE_SIDE[smelting.output_side] if smelting.output_side else "N"
    cx, cy = smelting.rect.centre()
    mx = sum(p.centroid[0] for p in patches) / len(patches)
    my = sum(p.centroid[1] for p in patches) / len(patches)
    return sides_facing(smelting.rect, (mx, my))[0] if (mx, my) != (cx, cy) else smelting.output_side


def _corridor_legs(draft) -> list[tuple[str, Rect]]:
    legs = []
    for feeder, target in draft.feeders.values():
        if feeder.route is not None:
            legs += [(f"corridor:{feeder.material}->{target}", r) for r in feeder.route.rects]
    if draft.power_corridor is not None:
        legs += [(draft.power_corridor.name, r) for r in draft.power_corridor.rects]
    return legs


def _clean(draft, rect: Rect, legs) -> bool:
    bounds = draft.occupancy.grid.bounds
    inside = bounds.x0 <= rect.x0 and bounds.y0 <= rect.y0 and rect.x1 <= bounds.x1 and rect.y1 <= bounds.y1
    return inside and draft.occupancy.clean(rect) and not any(rect.intersects(leg) for _n, leg in legs)


def _trunk(draft, station: Rect, side: str, direction: str, legs):
    config = draft.config
    target = _boundary_strip(draft.occupancy.grid.bounds, direction)
    others = [(leg,) for _name, leg in legs]
    found = route_sided(
        draft.occupancy,
        station,
        side,
        target,
        OPPOSITE_SIDE[direction],
        config.rail_trunk_width,
        others=others,
        crossing_weight=config.crossing_weight,
    )
    return found


def _place_unload(draft, smelting: Zone, direction: str, legs):
    """Best (cost, side, station, route, direction) or the first clean station without a trunk."""
    config = draft.config
    used = {p.side for p in smelting.ports if p.role == "in"}
    facing = sides_facing(smelting.rect, _toward(smelting.rect, direction))
    sides = [s for s in facing if s != smelting.output_side]
    # Free edges first; an edge shared with feeders only as a fallback (the
    # station must still miss every corridor).
    groups = [[s for s in sides if s not in used], [s for s in sides if s in used]]
    sides = groups[0] + groups[1]
    directions = [direction] + [d for d in facing if d != direction]
    clean = []
    for rank, side in enumerate(sides):
        for k in range(SLIDES + 1):
            for station in _stations(smelting.rect, side, config.rail_unload_width, config.rail_unload_length, k):
                if _clean(draft, station, legs):
                    clean.append(((rank, k, _distance(station.centre(), draft.start)), side, station))
    for group in groups:
        for preferred, trunk_direction in enumerate(directions):
            for side in group:
                options = []
                for key, station_side, station in clean:
                    if station_side != side:
                        continue
                    found = _trunk(draft, station, side, trunk_direction, legs)
                    if found is not None:
                        # Ties: nearer the edge centre, then nearer the start (mirror invariant).
                        option = (found[1], side, station, found[0], trunk_direction)
                        options.append(((found[1],) + key[1:], option))
                if options:
                    return min(options, key=lambda row: row[0])[1], preferred
    if not clean:
        return None, None
    _key, side, station = min(clean, key=lambda row: row[0])
    return (side, station), None


def _toward(rect: Rect, direction: str) -> tuple[float, float]:
    cx, cy = rect.centre()
    dx, dy = SIDE_VECTOR[direction]
    return (cx + 1000 * dx, cy + 1000 * dy)


def _crossings(route, legs) -> list[dict]:
    rows = []
    for name, leg in legs:
        for rect in route.rects:
            if rect.intersects(leg):
                piece = Rect(max(rect.x0, leg.x0), max(rect.y0, leg.y0), min(rect.x1, leg.x1), min(rect.y1, leg.y1))
                rows.append({"corridor": name, "rect": piece.to_list()})
    return rows


def plan_unload(draft) -> None:
    """``rail:unload``, its smelting entry port and ``rail:trunk`` (module rules)."""
    smelting = draft.zones.get("smelting")
    if smelting is None:
        draft.warnings.append("rail:unload: no smelting zone to unload into")
        return
    direction = outpost_direction(draft, smelting)
    legs = _corridor_legs(draft)
    best, preferred = _place_unload(draft, smelting, direction, legs)
    if best is None:
        draft.warnings.append("rail:unload: no clean station land beside smelting off its output edge")
        return
    if preferred is None:
        side, station = best
        route = None
        draft.warnings.append(f"rail:trunk: no clean straight/one-elbow right-of-way to the map edge from {side}")
    else:
        _cost, side, station, route, trunk_direction = best
        if preferred:
            draft.warnings.append(f"rail:trunk: leaves {trunk_direction}, outposts lie {direction}")
    if any(p.side == side for p in smelting.ports if p.role == "in"):
        draft.warnings.append(f"rail:unload: every non-output edge has feeders; shares the {side} edge")
    lo, hi = edge_range(station, side)
    port = Port("ore", "in", side, edge_point(smelting.rect, side, (lo, hi)))
    draft.zones["smelting"] = replace(smelting, ports=smelting.ports + (port,))
    draft.add_zone(Zone("rail", "rail:unload", station, side, side, OPPOSITE_SIDE[side]))
    if route is None:
        return
    draft.rail_trunk = Corridor(
        "rail:trunk",
        "rail",
        "train",
        "rail:unload",
        "boundary",
        draft.config.rail_trunk_width,
        side,
        OPPOSITE_SIDE[trunk_direction],
        route.segments,
    )
    draft.rail_crossings = _crossings(route, legs)
    draft.occupancy.taken.extend(route.rects)


def _trunk_point(draft, rect: Rect) -> tuple[float, float]:
    trunk = getattr(draft, "rail_trunk", None)
    if trunk is None:
        return draft.zones["smelting"].rect.centre() if "smelting" in draft.zones else rect.centre()
    return min(
        (r.centre() for r in trunk.rects), key=lambda c: abs(c[0] - rect.centre()[0]) + abs(c[1] - rect.centre()[1])
    )


def _distance(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _load_station(draft, zone: Rect, sides, legs) -> Rect | None:
    """First clean station along ``sides``; at equal slide distance the one nearer the start."""
    config = draft.config
    for side in sides:
        for k in range(SLIDES + 1):
            stations = _stations(zone, side, config.rail_load_length, config.rail_load_width, k)
            clean = [s for s in stations if _clean(draft, s, legs)]
            if clean:
                return min(clean, key=lambda s: _distance(s.centre(), draft.start))
    return None


def plan_outposts(draft) -> None:
    """Reserve ``mining:<resource>:<n>`` (kind "outpost") for worthwhile far patches.

    Done before the trunk so the right-of-way never runs through future
    mining land. A patch whose bbox + margin touches anything planned is skipped.
    """
    config = draft.config
    legs = _corridor_legs(draft)
    bounds = draft.occupancy.grid.bounds
    m = config.mining_margin
    for name, rank, patch in outpost_patches(draft):
        box = patch.bbox
        zone = Rect(
            max(bounds.x0, box.x0 - m),
            max(bounds.y0, box.y0 - m),
            min(bounds.x1, box.x1 + m),
            min(bounds.y1, box.y1 + m),
        )
        blocked = any(zone.intersects(other) for other in draft.occupancy.taken)
        if blocked or any(zone.intersects(leg) for _name, leg in legs):
            continue
        draft.add_zone(Zone("outpost", f"mining:{name}:{rank}", zone))


def plan_loading(draft) -> None:
    """Loading stations beside outpost zones (facing the trunk) and, optionally, primaries."""
    legs = _corridor_legs(draft)
    outposts = [zone for zone in draft.zones.values() if zone.kind == "outpost"]
    for zone in outposts:
        station = _load_station(draft, zone.rect, sides_facing(zone.rect, _trunk_point(draft, zone.rect)), legs)
        if station is not None:
            suffix = zone.name[len("mining:") :]
            draft.add_zone(Zone("rail", f"rail:load:{suffix}", station, "", "", ""))
    smelting = draft.zones.get("smelting")
    for resource in draft.resources:
        mining = draft.zones.get("mining:" + resource.name)
        if mining is None or smelting is None:
            continue
        far = sides_facing(mining.rect, smelting.rect.centre())[-1]
        station = _load_station(draft, mining.rect, [far], legs)
        if station is not None:
            draft.add_zone(Zone("rail", f"rail:load:{resource.name}:primary", station, "", "", ""))


def plan_rail(draft) -> None:
    """Outpost land, then the unload station and trunk, then loading stations."""
    plan_outposts(draft)
    plan_unload(draft)
    plan_loading(draft)
