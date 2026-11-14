"""Independent re-check of a master plan against the world map.

It deliberately does not reuse the planner's prefix-sum grid: every check
reads the :class:`WorldMap` tile by tile, so a planner bug cannot hide itself.
Each problem is one human-readable string; an empty list means the plan holds.
"""

from __future__ import annotations

from .corridors import OPPOSITE_SIDE
from .geometry import Rect, chunks_in
from .planzones import edge_touches
from .worldmap import WATER, WorldMap


def _label_rects(plan) -> tuple[list[tuple[str, Rect]], list[tuple[str, Rect]], list[tuple[str, Rect]]]:
    """(solid zones + strips, corridor legs, allocations) as (label, rect) lists."""
    solid = [(zone.name, zone.rect) for zone in plan.zones]
    solid += [(f"{strip.owner}[{strip.along0}:{strip.along1}]", strip.rect) for strip in plan.strips]
    legs = [(corridor.name, leg) for corridor in plan.corridors for leg in corridor.rects]
    blocks = [(allocation.owner, allocation.rect) for allocation in plan.allocations]
    return solid, legs, blocks


def _check_surveyed(world: WorldMap, items: list[tuple[str, Rect]]) -> list[str]:
    surveyed = world.surveyed_chunks()
    problems = []
    for label, rect in items:
        if any(chunk not in surveyed for chunk in chunks_in(rect)):
            problems.append(f"{label} {rect.to_list()} reaches unsurveyed tiles")
    return problems


def _check_disjoint(solid, legs, blocks) -> list[str]:
    problems = []
    for index, (label, rect) in enumerate(solid):
        for other_label, other in solid[index + 1 :]:
            if rect.intersects(other):
                problems.append(f"{label} overlaps {other_label}")
    for label, rect in legs:
        for other_label, other in solid:
            if rect.intersects(other):
                problems.append(f"{label} overlaps {other_label}")
    for index, (label, rect) in enumerate(blocks):
        for other_label, other in blocks[index + 1 :]:
            if rect.intersects(other):
                problems.append(f"{label} overlaps {other_label}")
        for other_label, other in legs:
            if rect.intersects(other):
                problems.append(f"{label} overlaps {other_label}")
    return problems


def _check_ground(world: WorldMap, items: list[tuple[str, Rect]]) -> list[str]:
    """Water and resource tiles under rects that must stay off both."""
    problems = []
    for label, rect in items:
        water = 0
        ore = 0
        for x, y in rect.tiles():
            if world.terrain(x, y) == WATER:
                water += 1
            if world.resource_at(x, y) is not None:
                ore += 1
        if water:
            problems.append(f"{label} covers {water} water tiles")
        if ore:
            problems.append(f"{label} covers {ore} resource tiles")
    return problems


def _check_bus(plan, world: WorldMap) -> list[str]:
    bus = plan.bus
    config = plan.config
    problems = []
    zones = [zone for zone in plan.zones if zone.kind == "bus"]
    if len(zones) != 1 or zones[0].rect != bus.rect():
        problems.append("bus zone is missing or is not the single straight bus rect")
    if bus.direction not in ("N", "E", "S", "W"):
        problems.append(f"bus direction {bus.direction!r} is not a compass direction")
    if bus.length < config.bus_min_length:
        problems.append(f"bus length {bus.length} is below the minimum {config.bus_min_length}")
    if bus.width != config.bus_width:
        problems.append(f"bus width {bus.width} differs from config {config.bus_width}")
    rect = bus.rect()
    if not problems:
        report = world.free_rect(rect.x0, rect.y0, rect.x1, rect.y1, owner="bus")
        if report.blocked:
            problems.append(f"bus is blocked by {sorted(report.blocked)}")
    return problems


def _check_allocations(plan) -> list[str]:
    problems = []
    gap = plan.config.district_gap
    for allocation in plan.allocations:
        inside = any(s.side == allocation.side and _contains(s.rect, allocation.rect) for s in plan.strips)
        if not inside:
            problems.append(f"{allocation.owner} is not inside a district strip on the {allocation.side} side")
        for other in plan.allocations:
            if other.name == allocation.name or other.side != allocation.side:
                continue
            # Spans include the held growth room (Allocation.growth).
            end, other_end = allocation.along1 + allocation.growth, other.along1 + other.growth
            if other.along0 - gap < end and allocation.along0 < other_end + gap:
                problems.append(f"{allocation.owner} is within the corridor gap of {other.owner}")
    return problems


def _contains(outer: Rect, inner: Rect) -> bool:
    return outer.x0 <= inner.x0 and outer.y0 <= inner.y0 and inner.x1 <= outer.x1 and inner.y1 <= outer.y1


def _check_reservations(plan, world: WorldMap) -> list[str]:
    """Every planned tile is reserved on the map by the expected owner."""
    problems = []
    block_owner = {}
    for allocation in plan.allocations:
        for tile in allocation.rect.tiles():
            block_owner[tile] = allocation.owner
    for zone in plan.zones:
        wrong = sum(1 for x, y in zone.rect.tiles() if world.reserved_by(x, y) != zone.name)
        if wrong:
            problems.append(f"{wrong} tiles of {zone.name} are not reserved for it")
    for strip in plan.strips:
        wrong = 0
        for x, y in strip.rect.tiles():
            expected = block_owner.get((x, y), strip.owner)
            if world.reserved_by(x, y) != expected:
                wrong += 1
        if wrong:
            problems.append(f"{wrong} tiles of {strip.owner}[{strip.along0}:{strip.along1}] are not reserved")
    for corridor in plan.corridors:
        for leg in corridor.rects:
            wrong = sum(
                1 for x, y in leg.tiles() if not str(world.reserved_by(x, y)).startswith(("corridor:", "rail:trunk"))
            )
            if wrong:
                problems.append(f"{wrong} tiles of {corridor.name} are not reserved for a corridor")
    return problems


def _check_zone_sides(plan) -> list[str]:
    """Input and output edges of a zone differ, including every input port."""
    problems = []
    for zone in plan.zones:
        if zone.input_side and zone.input_side == zone.output_side:
            problems.append(f"{zone.name} enters and leaves on the same side {zone.input_side}")
        for port in zone.ports:
            if port.role == "in" and zone.output_side and port.side == zone.output_side:
                problems.append(f"{zone.name} input {port.material} uses its output side {port.side}")
    return problems


def _expected_to_side(corridor, target) -> str | None:
    """Edge the corridor must enter ``target`` by, or None when any edge is allowed."""
    if corridor.kind == "power":
        return None
    if corridor.kind == "header":
        return target.input_side
    port = target.port(corridor.material, "in")
    return port.side if port is not None else ""


def _check_corridor_ends(plan) -> list[str]:
    """Each corridor leaves its source's output edge and enters its target's input edge."""
    problems = []
    zones = {zone.name: zone for zone in plan.zones}
    for corridor in plan.corridors:
        if corridor.kind == "rail":
            continue  # checked by _check_rail
        source = zones.get(corridor.source)
        target = zones.get(corridor.target)
        if source is None or target is None:
            problems.append(f"{corridor.name} joins unknown zones {corridor.source!r} -> {corridor.target!r}")
            continue
        if corridor.from_side != source.output_side:
            problems.append(f"{corridor.name} leaves {source.name} on {corridor.from_side}, not its output side")
        expected = _expected_to_side(corridor, target)
        if expected is not None and corridor.to_side != expected:
            problems.append(f"{corridor.name} enters {target.name} on {corridor.to_side}, not its input side")
        problems += _check_corridor_geometry(corridor, source.rect, target.rect)
    return problems


def _check_corridor_geometry(corridor, source: Rect, target: Rect) -> list[str]:
    segments = corridor.segments
    if not segments:
        if not edge_touches(source, target, corridor.from_side):
            return [f"{corridor.name} has no segments but its zones do not touch"]
        return []
    problems = []
    first = segments[0]
    last = segments[-1]
    if not edge_touches(source, first.rect, corridor.from_side) or first.direction != corridor.from_side:
        problems.append(f"{corridor.name} does not start on the {corridor.from_side} edge of {corridor.source}")
    if not edge_touches(target, last.rect, corridor.to_side) or last.direction != OPPOSITE_SIDE[corridor.to_side]:
        problems.append(f"{corridor.name} does not end on the {corridor.to_side} edge of {corridor.target}")
    if len(segments) > 3:
        problems.append(f"{corridor.name} has {len(segments)} segments (at most two elbows)")
    for before, after in zip(segments, segments[1:]):
        if (before.direction in "EW") == (after.direction in "EW"):
            problems.append(f"{corridor.name} has consecutive parallel segments")
    return problems


def _check_allocation_flow(plan) -> list[str]:
    """Blocks take taps on the bus edge and all flow downstream on each side."""
    problems = []
    for side in ("left", "right"):
        flows = {a.flow for a in plan.allocations if a.side == side}
        if len(flows) > 1:
            problems.append(f"allocations on the {side} side flow in different directions {sorted(flows)}")
    for allocation in plan.allocations:
        if allocation.input_side != allocation.bus_edge:
            problems.append(f"{allocation.owner} input side {allocation.input_side} is not its bus edge")
        if allocation.flow != plan.bus.direction:
            problems.append(f"{allocation.owner} flows {allocation.flow}, not downstream {plan.bus.direction}")
    return problems


def validate_plan(plan, world: WorldMap) -> list[str]:
    """All invariant violations of ``plan`` on ``world`` (empty when valid)."""
    solid, legs, blocks = _label_rects(plan)
    everything = solid + legs + blocks
    problems = _check_surveyed(world, everything)
    if problems:
        return problems
    problems += _check_bus(plan, world)
    problems += _check_disjoint(solid, legs, blocks)
    mining = {zone.name for zone in plan.zones if zone.kind in ("mining", "outpost")}
    ground = [(label, rect) for label, rect in everything if label not in mining]
    problems += _check_ground(world, ground)
    problems += _check_allocations(plan)
    problems += _check_reservations(plan, world)
    problems += _check_zone_sides(plan)
    problems += _check_corridor_ends(plan)
    problems += _check_allocation_flow(plan)
    problems += _check_rail(plan)
    return problems


def _check_rail(plan) -> list[str]:
    """Unload tracks point away from the bus; the trunk reaches the survey boundary."""
    problems = []
    station = plan.zone("rail:unload")
    if station is not None:
        bx, by = plan.bus.rect().centre()
        sx, sy = station.rect.centre()
        vx, vy = {"N": (0, -1), "E": (1, 0), "S": (0, 1), "W": (-1, 0)}.get(station.growth, (0, 0))
        if vx * (sx - bx) + vy * (sy - by) <= 0:
            problems.append(f"rail:unload track axis {station.growth or '-'} does not point away from the bus")
    trunk = plan.corridor("rail:trunk")
    if trunk is not None:
        b = plan.bounds
        last = trunk.segments[-1].rect if trunk.segments else None
        reach = {"E": last.x0 == b.x0, "W": last.x1 == b.x1, "S": last.y0 == b.y0, "N": last.y1 == b.y1}
        if last is None or not reach.get(trunk.to_side, False):
            problems.append("rail:trunk does not reach the survey boundary")
    return problems
