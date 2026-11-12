"""The planning pipeline behind :func:`site.masterplan.plan_site`.

Order of decisions (each step only sees what earlier steps reserved):

1. primary patches and their mining zones (``siteselect.choose_resources``);
2. the bus, degrading the district depth if needed;
3. smelting behind the head, its output edge and the head-gap corridor;
4. district strips along both bus sides;
5. smelting input edge and feeders from every smelting-feed mining zone;
6. power on a shore (avoiding the feeders), its water and output edges;
7. the power corridor to smelting (it may move the power output edge);
8. coal goes to the nearer consumer (smelting or power); final sides/ports.

Orientation rules are documented in :mod:`site.orientation`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math

from .busframe import Bus, lane_table, opposite
from .corridors import route_double, route_sided
from .orientation import (
    Feeder,
    best_feeder,
    choose_input_side,
    edge_point,
    entry_port,
    exit_port,
    facing_side,
    feeder_corridor,
    route_into_edge,
    sides_facing,
)
from .planmodel import SiteConfig, SiteError, Strip
from .planzones import Corridor, Port, Segment, Zone
from .powersite import place_power
from .siteselect import BusChoice, choose_resources, header_rect, place_smelting, search_bus, strip_runs
from .tilegrid import Occupancy
from .worldmap import WorldMap


@dataclass
class Draft:
    """Working state while planning. ``zones`` maps zone name to Zone."""

    world: WorldMap
    config: SiteConfig
    start: tuple[int, int]
    occupancy: Occupancy
    warnings: list[str] = field(default_factory=list)
    zones: dict[str, Zone] = field(default_factory=dict)
    strips: list[Strip] = field(default_factory=list)
    header: Corridor | None = None
    feeders: dict[str, tuple[Feeder, str]] = field(default_factory=dict)
    power_corridor: Corridor | None = None
    resources: list = field(default_factory=list)
    water: object = None
    bus: Bus | None = None
    depth: int = 0

    def add_zone(self, zone: Zone) -> None:
        self.zones[zone.name] = zone
        self.occupancy.taken.append(zone.rect)


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def choose_bus(draft: Draft, feeds) -> tuple[BusChoice, int]:
    """Search the bus, degrading the district depth step by step."""
    config = draft.config
    depths = [config.district_depth] + [d for d in config.district_depth_fallbacks if d < config.district_depth]
    for depth in depths:
        choice = search_bus(draft.occupancy, draft.start, feeds, config, depth)
        if choice is None:
            continue
        if depth < config.district_depth:
            draft.warnings.append(f"district depth reduced from {config.district_depth} to {depth} tiles")
        if choice.length < config.bus_target_length:
            draft.warnings.append(f"bus shortened to {choice.length} of {config.bus_target_length} target tiles")
        if not choice.smelting_clean:
            draft.warnings.append("no clean smelting site directly behind the bus head")
        return choice, depth
    raise SiteError(
        f"no straight {config.bus_width}-wide bus of at least {config.bus_min_length} tiles, free of ore, water "
        f"and cliffs, with district room on both sides, within {config.bus_search_radius} tiles of {draft.start}"
    )


def plan_bus(draft: Draft, choice: BusChoice) -> None:
    config = draft.config
    lanes = lane_table(config.bus_width, config.lane_order, config.lane_group, config.lane_group_gap)
    bus = Bus(choice.origin, choice.direction, config.bus_width, choice.length, lanes)
    draft.bus = bus
    ports = (Port("plates", "in", bus.input_side, bus.head_point()),)
    draft.add_zone(Zone("bus", "bus", bus.rect(), bus.direction, bus.input_side, bus.output_side, ports))


def header_corridor(from_side: str, to_side: str, width: int, segments) -> Corridor:
    """Corridor carrying plates from the smelting output edge into the bus head."""
    return Corridor(
        name="corridor:smelting->bus",
        kind="header",
        material="plates",
        source="smelting",
        target="bus",
        width=width,
        from_side=from_side,
        to_side=to_side,
        segments=segments,
    )


def plan_smelting(draft: Draft, feeds) -> None:
    """Smelting zone, its output edge (facing the head) and the header corridor."""
    bus = draft.bus
    config = draft.config
    placed = place_smelting(draft.occupancy, bus, config, feeds)
    if placed is None:
        draft.warnings.append("no room for the smelting zone near the bus head")
        return
    rect, ideal = placed
    if ideal:
        header = header_rect(bus.origin, bus.direction, config)
        segments = (Segment(header, bus.direction),)
        draft.header = header_corridor(bus.direction, bus.input_side, config.bus_width, segments)
        output_side = bus.direction
        draft.occupancy.taken.append(header)
    else:
        draft.warnings.append("smelting zone displaced from the bus head")
        output_side = _route_header(draft, rect)
    zone = Zone("smelting", "smelting", rect, opposite(bus.direction), "", output_side)
    draft.add_zone(zone)


def _route_header(draft: Draft, rect) -> str:
    """Header corridor from a displaced smelting zone; returns its output edge."""
    bus = draft.bus
    head = bus.head_point()
    order = sides_facing(rect, head)
    for side in order:
        found = route_sided(draft.occupancy, rect, side, bus.rect(), bus.input_side, draft.config.corridor_width)
        if found is not None:
            segments = found[0].segments
            draft.header = header_corridor(side, bus.input_side, draft.config.corridor_width, segments)
            return side
    draft.warnings.append("corridor:smelting->bus: no straight or single-elbow corridor to the bus head")
    return order[0]


def plan_strips(draft: Draft) -> None:
    for side in ("left", "right"):
        draft.strips.extend(strip_runs(draft.occupancy, draft.bus, side, draft.depth, draft.config))
    draft.occupancy.taken.extend(strip.rect for strip in draft.strips)


def _hub(draft: Draft) -> Zone:
    return draft.zones.get("smelting") or draft.zones["bus"]


def plan_feeders(draft: Draft) -> None:
    """Choose the smelting input edge and route the smelting feeders to it."""
    config = draft.config
    sources = []
    for material in config.smelting_feeds:
        zone = draft.zones.get("mining:" + material)
        if zone is not None:
            sources.append((material, zone))
    hub = _hub(draft)
    if hub.kind == "smelting":
        side, feeders = choose_input_side(draft.occupancy, hub.rect, hub.output_side, sources, config)
        draft.zones["smelting"] = replace(hub, input_side=side)
    else:
        feeders, _total = route_into_edge(draft.occupancy, sources, hub.rect, hub.input_side, config)
    for feeder in feeders:
        draft.feeders[feeder.material] = (feeder, hub.name)


def _feeder_legs(draft: Draft) -> list:
    return [leg for feeder, _t in draft.feeders.values() if feeder.route is not None for leg in feeder.route.rects]


def plan_power(draft: Draft) -> None:
    """Power zone on a shore; water edge from the shore, output edge facing the head."""
    legs = _feeder_legs(draft)
    draft.occupancy.taken.extend(legs)
    placed = place_power(draft.world, draft.occupancy, draft.bus.head_point(), draft.config)
    for leg in legs:
        draft.occupancy.taken.remove(leg)
    if placed is None:
        draft.warnings.append("no room for the power zone")
        return
    rect, shore, water_side = placed
    if shore is None:
        draft.warnings.append("no usable shore near the bus head; power zone placed without water")
    draft.water = shore
    exclude = (water_side,) if water_side else ()
    output_side = facing_side(rect, draft.bus.head_point(), exclude=exclude)
    growth = opposite(water_side) if water_side else ""
    draft.add_zone(Zone("power", "power", rect, growth, water_side, output_side))


def plan_fuel(draft: Draft) -> None:
    """Send coal to power instead of smelting when power is the nearer consumer."""
    fuel = draft.config.fuel_resource
    power = draft.zones.get("power")
    mining = draft.zones.get("mining:" + fuel)
    if power is None or mining is None:
        return
    hub = _hub(draft)
    centre = mining.rect.centre()
    if fuel in draft.feeders and _distance(centre, hub.rect.centre()) <= _distance(centre, power.rect.centre()):
        return
    others = [f.route.rects for f, _t in draft.feeders.values() if f.route is not None and f.material != fuel]
    best = None
    for side in sides_facing(power.rect, centre):
        if side == power.output_side:
            continue
        feeder = best_feeder(draft.occupancy, mining, fuel, power.rect, side, None, others, draft.config)
        cost = feeder.cost + (draft.config.turn_penalty if side == power.input_side else 0.0)
        if feeder.route is not None and (best is None or cost < best[0]):
            best = (cost, feeder)
    if best is not None:
        draft.feeders[fuel] = (best[1], "power")


def plan_power_corridor(draft: Draft) -> None:
    """Electricity leaves power towards the hub.

    The output edge facing the bus head is preferred; another non-water edge
    is used (costing ``turn_penalty``) only when it routes more cheaply,
    typically when the facing edge cannot be routed at all. The hub may be
    entered on any edge except its output edge.
    """
    power = draft.zones.get("power")
    if power is None:
        return
    hub = _hub(draft)
    config = draft.config
    best = None
    for from_side in sides_facing(power.rect, draft.bus.head_point()):
        if from_side == power.input_side:
            continue
        penalty = 0.0 if from_side == power.output_side else config.turn_penalty
        for to_side in sides_facing(hub.rect, power.rect.centre()):
            if to_side == hub.output_side:
                continue
            extra = 0.0
            found = route_sided(draft.occupancy, power.rect, from_side, hub.rect, to_side, config.corridor_width)
            if found is None:
                found = route_double(draft.occupancy, power.rect, from_side, hub.rect, to_side, config.corridor_width)
                extra = config.turn_penalty
            if found is not None and (best is None or found[1] + penalty + extra < best[0]):
                best = (found[1] + penalty + extra, from_side, to_side, found[0])
    name = f"corridor:power->{hub.name}"
    if best is None:
        draft.warnings.append(f"{name}: no corridor with at most two elbows from power to {hub.name}")
        return
    _cost, from_side, to_side, route = best
    draft.zones["power"] = replace(power, output_side=from_side)
    draft.power_corridor = Corridor(
        name=name,
        kind="power",
        material="electricity",
        source="power",
        target=hub.name,
        width=config.corridor_width,
        from_side=from_side,
        to_side=to_side,
        segments=route.segments,
    )


def finish_zones(draft: Draft) -> None:
    """Write output sides and ports into mining, smelting and power zones."""
    hub_ports: dict[str, list[Port]] = {}
    for material, (feeder, target) in draft.feeders.items():
        source = draft.zones[feeder.source.name]
        draft.zones[source.name] = replace(source, output_side=feeder.from_side, ports=(exit_port(feeder),))
        target_rect = draft.zones[target].rect
        hub_ports.setdefault(target, []).append(entry_port(feeder, target_rect))
        if feeder.route is None:
            draft.warnings.append(
                f"corridor:{material}->{target}: no corridor with at most two elbows from {source.name} to {target}"
            )
    smelting = draft.zones.get("smelting")
    if smelting is not None:
        ports = sorted(hub_ports.get("smelting", []), key=lambda p: (p.point[0], p.point[1]))
        out = Port("plates", "out", smelting.output_side, edge_point(smelting.rect, smelting.output_side))
        draft.zones["smelting"] = replace(smelting, ports=tuple(ports) + (out,))
    power = draft.zones.get("power")
    if power is not None:
        draft.zones["power"] = replace(power, **_power_sides(draft, power, hub_ports.get("power", [])))


def _power_sides(draft: Draft, power: Zone, coal_ports: list[Port]) -> dict:
    ports = []
    if draft.water is not None:
        ports.append(Port("water", "in", power.input_side, edge_point(power.rect, power.input_side)))
    if coal_ports:
        ports.extend(coal_ports)
    else:
        mining = draft.zones.get("mining:" + draft.config.fuel_resource)
        if mining is not None:
            exclude = (power.output_side, power.input_side)
            side = facing_side(power.rect, mining.rect.centre(), exclude=exclude)
            ports.append(Port(draft.config.fuel_resource, "in", side, edge_point(power.rect, side)))
    band = None
    if draft.power_corridor is not None and draft.power_corridor.segments:
        first = draft.power_corridor.segments[0].rect
        band = (first.x0, first.x1) if power.output_side in ("N", "S") else (first.y0, first.y1)
    ports.append(Port("electricity", "out", power.output_side, edge_point(power.rect, power.output_side, band)))
    input_side = power.input_side or (ports[0].side if ports[0].role == "in" else "")
    return {"input_side": input_side, "ports": tuple(ports)}


def corridors_of(draft: Draft) -> list[Corridor]:
    corridors = [draft.header] if draft.header is not None else []
    for material in draft.config.smelting_feeds + (draft.config.fuel_resource,):
        if material in draft.feeders and not any(c.material == material for c in corridors):
            feeder, target = draft.feeders[material]
            corridor = feeder_corridor(feeder, target, draft.config.corridor_width)
            if corridor is not None:
                corridors.append(corridor)
    if draft.power_corridor is not None:
        corridors.append(draft.power_corridor)
    return corridors


def run_pipeline(draft: Draft) -> None:
    """Steps 1-8 of the module docstring, filling ``draft``."""
    config = draft.config
    resources, mining = choose_resources(draft.world, draft.occupancy, draft.start, config, draft.warnings)
    draft.resources = resources
    for zone in mining:
        draft.zones[zone.name] = zone
    feeds = [r.primary.centroid for r in resources if r.name in config.feed_resources and r.primary is not None]
    choice, draft.depth = choose_bus(draft, feeds)
    plan_bus(draft, choice)
    plan_smelting(draft, feeds)
    plan_strips(draft)
    plan_feeders(draft)
    plan_power(draft)
    plan_power_corridor(draft)
    plan_fuel(draft)
    finish_zones(draft)
