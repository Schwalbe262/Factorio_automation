"""Fixed-port NORTH material buses; planning and native preflight only.

Each declared active line is a real continuous belt, not an upstream splitter
masquerading as extra production. Spare columns are reservations, never built.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

from factorio_agent.core.deterministic_builder import _plan
from .board import entity
from .planning import preflight
from .routing import route_trunks
from .transport import compile_crossings, xy


YELLOW_BELT_PER_MINUTE = 900.0
YELLOW_LANE_PER_MINUTE = 450.0
PRIORITY_OUTPUT = {"critical": "right", "normal": "none", "surplus": "left"}


@dataclass(frozen=True)
class BusMaterial:
    """Rightmost line and independent declared supply for each active line.

    Supply is a design contract, not a measured production claim. Index zero
    stays fixed; additional lines occupy reserved columns to its west.
    """

    item: str
    x: float
    source_rates: tuple[float, ...]
    reserved_lines: int = 1
    line_pitch: int = 4

    def line_x(self, line: int) -> float:
        return self.x - self.line_pitch * line


@dataclass(frozen=True)
class ConsumerTap:
    """A NORTH splitter at ``y`` with a fixed EAST outlet at ``y - 1``.

    Priority is local native splitter behavior. It cannot reserve an absolute
    rate or let a later critical consumer preempt an earlier consumer.
    """

    name: str
    item: str
    y: float
    priority: str = "normal"
    line: int = 0
    demand_per_minute: float = 0


@dataclass(frozen=True)
class SourceMerge:
    """Native two-input merge into the existing left/through bus output.

    The second source enters the right input at (line_x + 1, y + 1), facing
    NORTH. ``input_priority`` is left for existing supply, right for new supply,
    or none for native sharing. Both sources share ONE output belt's capacity.
    """

    name: str
    item: str
    y: float
    input_priority: str = "left"
    line: int = 0


def _half_tile(value: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value % 1 == .5


def _rate(value: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _port(name, item, x, y, direction, facing, **extra):
    return {"name": name, "kind": "item", "item": item, "direction": direction,
            "position": {"x": x, "y": y}, "facing": facing,
            "lane_policy": "either-or-both lanes, same item", **extra}


def _splitter(x, y, input_priority, output_priority):
    # NORTH prototype width is two tiles; the half-integer column is its left.
    return dict(entity("splitter", x + .5, y, 0), _width=2, _height=1,
                splitter_input_priority=input_priority,
                splitter_output_priority=output_priority)


def _specs(materials, taps, merges, north_y, south_y, port_x):
    if not materials or not all(_half_tile(v) for v in (north_y, south_y, port_x)) or north_y >= south_y:
        raise ValueError("nonempty bus and half-tile NORTH bounds/EAST port column required")
    by_item = {}
    reserved_columns = []
    for material in materials:
        if not material.item or material.item in by_item or not _half_tile(material.x):
            raise ValueError("bus materials require unique typed items and half-tile columns")
        if (not material.source_rates or not all(_rate(rate) for rate in material.source_rates)
                or type(material.reserved_lines) is not int
                or material.reserved_lines < len(material.source_rates)
                or type(material.line_pitch) is not int or material.line_pitch < 4):
            raise ValueError("active supply lines must fit reserved columns with pitch at least four")
        by_item[material.item] = material
        reserved_columns.extend(material.line_x(i) for i in range(material.reserved_lines))
    columns = sorted(reserved_columns)
    if any(b - a < 4 for a, b in zip(columns, columns[1:])):
        raise ValueError("material groups or future columns overlap their four-tile corridors")
    if port_x - max(columns) < 4:
        raise ValueError("fixed EAST outlets need three clear crossing/exit tiles")
    names = set()
    stations = []
    for station in (*taps, *merges):
        material = by_item.get(station.item)
        if not station.name or station.name in names or material is None:
            raise ValueError("station names must be unique and reference a typed material")
        if type(station.line) is not int or not 0 <= station.line < len(material.source_rates):
            raise ValueError("station must use an active, independently supplied line")
        if not _half_tile(station.y) or not north_y + 2 <= station.y <= south_y - 2:
            raise ValueError("splitters need straight approach and exit within the bus")
        if isinstance(station, ConsumerTap):
            if station.priority not in PRIORITY_OUTPUT or not _rate(station.demand_per_minute):
                raise ValueError("tap requires critical/normal/surplus priority and finite demand")
        elif station.input_priority not in {"left", "right", "none"}:
            raise ValueError("native source input priority must be left/right/none")
        names.add(station.name)
        stations.append(station.y)
    # Shared outlet rows stay clear of all splitter footprints and input stubs.
    if any(b - a < 3 for a, b in zip(sorted(stations), sorted(stations)[1:])):
        raise ValueError("bus stations must be separated by at least three tiles")
    return by_item


def capacity_report(materials: Iterable[BusMaterial], taps: Iterable[ConsumerTap] = ()) -> dict:
    """Declared source/demand budgets, with producer deficits kept visible."""
    taps = tuple(taps)
    result = {}
    for material in materials:
        lines = []
        for index, supply in enumerate(material.source_rates):
            demand = sum(t.demand_per_minute for t in taps if t.item == material.item and t.line == index)
            deliverable = min(supply, YELLOW_BELT_PER_MINUTE)
            lines.append({"line": index, "x": material.line_x(index),
                          "declared_source_per_minute": supply,
                          "belt_capacity_per_minute": YELLOW_BELT_PER_MINUTE,
                          "single_lane_capacity_per_minute": YELLOW_LANE_PER_MINUTE,
                          "supply_ceiling_per_minute": deliverable,
                          "declared_demand_per_minute": demand,
                          "producer_deficit_per_minute": max(0, demand - supply),
                          "belt_deficit_per_minute": max(0, demand - YELLOW_BELT_PER_MINUTE),
                          "source_exceeds_belt_per_minute": max(0, supply - YELLOW_BELT_PER_MINUTE)})
        result[material.item] = {"active_lines": len(lines), "reserved_lines": material.reserved_lines,
                                "lines": lines, "supply_ceiling_per_minute": sum(l["supply_ceiling_per_minute"] for l in lines)}
    return {"basis": "declared design rates; not live throughput; two occupied lanes required above 450/min",
            "materials": result}


def plan_main_bus(materials: Iterable[BusMaterial], taps: Iterable[ConsumerTap], *,
                  north_y: float, south_y: float, port_x: float,
                  merges: Iterable[SourceMerge] = (), foreign_entities=(), maximum_span=5) -> dict:
    """Compile actual shared trunks and bounded splitter branches, read-only.

    Existing foreign belts are crossed through the normal proven underground
    compiler. Conflicting stations fail; this planner never searches maze paths.
    Source/consumer adapters remain separate ``transport.plan_connection`` plans.
    """
    materials, taps, merges = tuple(materials), tuple(taps), tuple(merges)
    by_item = _specs(materials, taps, merges, north_y, south_y, port_x)
    foreign = list(foreign_entities)
    trunks, ports, pairs, trunk_rows = [], [], [], []
    for material in materials:
        for index in range(len(material.source_rates)):
            x = material.line_x(index)
            route = route_trunks((x, south_y), (x, north_y), start_direction=0, end_direction=0)
            compiled = compile_crossings(route, foreign, maximum_span=maximum_span)
            if not compiled.get("ok"):
                return {**compiled, "stage": "trunk", "item": material.item, "line": index}
            trunks.extend(compiled["entities"])
            pairs.extend(compiled.get("underground_pairs", []))
            ports += [_port(f"{material.item}:{index}:input", material.item, x, south_y, "input", 0, line=index),
                      _port(f"{material.item}:{index}:continuation", material.item, x, north_y, "output", 0, line=index)]
            trunk_rows.append({"item": material.item, "line": index, "x": x,
                               "south_y": south_y, "north_y": north_y})
    splitters, stations = [], []
    for station in (*taps, *merges):
        x = by_item[station.item].line_x(station.line)
        old = next((e for e in trunks if xy(e) == (x, station.y)), None)
        if old is None or old["name"] != "transport-belt":
            return {"ok": False, "reason": "bus_station_intersects_underground_crossing", "station": station.name}
        trunks.remove(old)
        if isinstance(station, ConsumerTap):
            splitter = _splitter(x, station.y, "left", PRIORITY_OUTPUT[station.priority])
            stations.append({"name": station.name, "kind": "consumer", "item": station.item,
                             "line": station.line, "splitter": splitter, "priority": station.priority})
        else:
            splitter = _splitter(x, station.y, station.input_priority, "left")
            trunks.append(entity("transport-belt", x + 1, station.y + 1, 0))
            ports.append(_port(station.name, station.item, x + 1, station.y + 1, "input", 0, line=station.line))
            stations.append({"name": station.name, "kind": "source_merge", "item": station.item,
                             "line": station.line, "splitter": splitter})
        splitters.append(splitter)
    branches, branch_rows = [], []
    for tap in taps:
        x, y = by_item[tap.item].line_x(tap.line) + 1, tap.y - 1
        # Build crossings over reserved future columns now, so activation of a
        # westward parallel line leaves existing branches and fixed ports intact.
        future_crossings = [entity("transport-belt", material.line_x(i), y, 0)
                            for material in materials
                            for i in range(len(material.source_rates), material.reserved_lines)
                            if x < material.line_x(i) < port_x]
        # An explicit complete horizontal corridor, never an A* fallback.
        route = route_trunks((x, y), (port_x, y), start_direction=4, end_direction=4)
        compiled = compile_crossings(route, foreign + trunks + branches + future_crossings, maximum_span=maximum_span)
        if not compiled.get("ok"):
            return {**compiled, "stage": "tap", "station": tap.name}
        branches.extend(compiled["entities"])
        pairs.extend(compiled.get("underground_pairs", []))
        ports.append(_port(tap.name, tap.item, port_x, y, "output", 4, line=tap.line,
                           priority=tap.priority, demand_per_minute=tap.demand_per_minute))
        branch_rows.append({"name": tap.name, "item": tap.item, "line": tap.line,
                            "route_metrics": compiled["route_metrics"]})
    reserves = [{"item": material.item,
                 "area": [[material.line_x(material.reserved_lines - 1) - .5, north_y - .5],
                          [material.x + 2, south_y + .5]],
                 "columns": [material.line_x(i) for i in range(material.reserved_lines)],
                 "future_columns": [material.line_x(i) for i in range(len(material.source_rates), material.reserved_lines)]}
                for material in materials]
    # Native priorities are local. Expose geographical inversions instead of
    # implying that a north/downstream critical tap can preempt an earlier tap.
    inversions = []
    rank = {"critical": 0, "normal": 1, "surplus": 2}
    for material in materials:
        for index in range(len(material.source_rates)):
            ordered = sorted((t for t in taps if t.item == material.item and t.line == index), key=lambda t: -t.y)
            inversions.extend({"earlier": a.name, "later": b.name}
                              for i, a in enumerate(ordered) for b in ordered[i + 1:] if rank[a.priority] > rank[b.priority])
    return _plan(trunks + splitters + branches, ports, template="shared_north_main_bus",
                 layout_policy="pcb-shared-main-bus-v1", flow_direction=0,
                 trunks=trunk_rows, stations=stations, branches=branch_rows,
                 reservations=reserves, underground_pairs=pairs,
                 capacity=capacity_report(materials, taps), priority_inversions=inversions,
                 priority_scope="native splitter local input/output priority; no absolute-rate reservation",
                 migration_scope="replace exact owned dedicated corridors only after source and consumer adapters pass preflight")


def preflight_main_bus(game, builder, obs, plan, *, ignored_units=()):
    """Read-only prospective collision checks and exact-unit virtual removal.

    The caller must prove ownership of every ignored unit and remove those
    entities normally during cutover; ignored units do not authorize deletion.
    """
    if not plan.get("ok"):
        return plan
    if plan.get("template") != "shared_north_main_bus":
        return {"ok": False, "reason": "not_a_shared_main_bus_plan"}
    return preflight(game, builder, obs, plan["entities"], ignored_units=ignored_units)
