"""Bounded, additive underground escapes for owned item consumer routes."""
from collections import defaultdict, deque
from copy import deepcopy
from itertools import islice
import json
import math

from .factory_templates import DIRECTIONS, route_orthogonal
from .deterministic_underground_geometry import underground_edges
from .deterministic_state import stop_requested
from .deterministic_underground import UNDERGROUND_NAMES

MAX_INLETS = 32
MAX_CANDIDATES = 24
MAX_ROUTE_NODES = 100000
MAX_COMPONENT_EDGES = 8192
SURFACE_NAMES = frozenset({"transport-belt", "fast-transport-belt", "express-transport-belt", "turbo-transport-belt"})


def _point(row):
    p = row.get("position", row)
    return p["x"], p["y"]


def _entity(point, direction, role=None):
    row = {"name": "underground-belt" if role else "transport-belt",
           "position": {"x": point[0], "y": point[1]}, "direction": direction}
    if role:
        row["belt_to_ground_type"] = role
    return row


def _stopped(factory):
    if stop_requested(factory.path.parent / "stop.json"):
        raise InterruptedError("operator_stop_requested")


def _component_chains(free, components, start, finish, clear_mouth, maximum):
    """Bounded shortest chains of at most three separated underground pairs."""
    edges, reverse = defaultdict(list), defaultdict(set)
    edge_count = 0
    for point in sorted(free):
        origin = components[point]
        for direction, (dx, dy) in DIRECTIONS.items():
            if ((point[0] + dx, point[1] + dy) in free
                    or components.get((point[0] - dx, point[1] - dy)) != origin
                    or not clear_mouth(point, direction)):
                continue
            for span in range(2, maximum + 1):
                outlet = point[0] + span * dx, point[1] + span * dy
                target = components.get(outlet)
                if (target is None or target == origin
                        or components.get((outlet[0] + dx, outlet[1] + dy)) != target
                        or not clear_mouth(outlet, direction)):
                    continue
                edges[origin].append((point, outlet, direction))
                reverse[target].add(origin)
                edge_count += 1
                if edge_count > MAX_COMPONENT_EDGES:
                    return []
    target, origin = components[finish], components[start]
    distances, queue = {target: 0}, deque([target])
    while queue:
        current = queue.popleft()
        if distances[current] >= 3:
            continue
        for previous in sorted(reverse[current]):
            if previous not in distances:
                distances[previous] = distances[current] + 1
                queue.append(previous)
    if origin not in distances or origin == target:
        return []
    pending = [(0.0, [], origin, start)]
    for _ in range(distances[origin]):
        choices = []
        for cost, chain, current, previous in pending:
            useful = [edge for edge in edges[current]
                      if distances.get(components[edge[1]]) == distances[current] - 1]
            useful.sort(key=lambda edge: (math.dist(previous, edge[0]), math.dist(edge[1], finish), edge))
            inlets = set()
            for edge in useful:
                inlet = edge[0], edge[2]
                if inlet not in inlets and len(inlets) >= MAX_INLETS:
                    continue
                inlets.add(inlet)
                score = cost + math.dist(previous, edge[0]) + math.dist(edge[0], edge[1])
                choices.append((score, chain + [edge], components[edge[1]], edge[1]))
        choices.sort(key=lambda row: (row[0] + math.dist(row[3], finish), row[1]))
        pending = choices[:MAX_CANDIDATES]
    return [chain for _, chain, current, _ in pending if current == target]


def plan_underground_route(factory, source, destination, reserved, *, start_direction, end_direction):
    """Prefer adjacent pairs, then at most three pairs with surface connectors.

    The live survey proves ordinary underground range and excludes all existing
    mouths near a candidate axis. Every surface leg retains the ordinary belt
    collision/output exclusions, followed by whole-plan live placement.
    """
    failure = {"ok": False, "reason": "no bounded additive underground input route"}
    _stopped(factory)
    start, finish = _point(source), _point(destination)
    if (any(type(d) is not int or d not in DIRECTIONS for d in (start_direction, end_direction))
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                   for v in (*start, *finish))
            or any(abs(finish[i] - start[i] - round(finish[i] - start[i])) > 1e-6 for i in (0, 1))):
        return {**failure, "reason": "unsupported underground route endpoints"}
    bounds = {"min_x": min(start[0], finish[0]) - 48, "max_x": max(start[0], finish[0]) + 48,
              "min_y": min(start[1], finish[1]) - 48, "max_y": max(start[1], finish[1]) + 48}
    if (bounds["max_x"] - bounds["min_x"] + 1) * (bounds["max_y"] - bounds["min_y"] + 1) > 50000:
        return {**failure, "reason": "underground route survey exceeds 50000 tiles"}
    world = factory.state.get("world_id")
    payload = json.dumps(json.dumps({"bounds": bounds, "world": world}, separators=(",", ":")))
    survey = factory.game.query('''
--[[ additive underground input survey: no world mutations. ]]
local x=helpers.json_to_table(''' + payload + ''');local b=x.bounds
if not d or d.world_id~=x.world then return {ok=false,reason="underground route world changed"} end
local recipe=f.recipes["underground-belt"]
if not recipe or not recipe.enabled then return {ok=false,reason="underground belts are locked"} end
local maximum=prototypes.entity["underground-belt"].max_underground_distance
if not maximum or maximum<2 or maximum>16 then return {ok=false,reason="unsupported underground belt range"} end
local blocked={};local belts={};local mouths={}
for px=b.min_x,b.max_x do for py=b.min_y,b.max_y do
 local p={x=px,y=py}
 if not s.can_place_entity{name="transport-belt",position=p,force=f} then blocked[#blocked+1]=p end
end end
for _,e in pairs(s.find_entities_filtered{type={"transport-belt","underground-belt"},
 area={{b.min_x-maximum,b.min_y-maximum},{b.max_x+maximum,b.max_y+maximum}}}) do
 local row={name=e.name,position=pos(e.position),direction=e.direction}
 if e.type=="underground-belt" then row.belt_to_ground_type=e.belt_to_ground_type;mouths[#mouths+1]=row
 else belts[#belts+1]=row end
end
return {ok=true,world_id=d.world_id,tick=game.tick,maximum=maximum,blocked=blocked,belts=belts,mouths=mouths}
''')
    if not survey.get("ok"):
        return {**failure, "reason": survey.get("reason", "underground route survey failed")}
    maximum = survey.get("maximum")
    if (survey.get("world_id") != world or type(survey.get("tick")) is not int
            or survey["tick"] < max(0, factory.state.get("last_tick", 0))
            or type(maximum) is not int or not 2 <= maximum <= 16):
        return {**failure, "reason": "invalid underground route world, tick or range"}
    def valid_point(p):
        return isinstance(p, dict) and all(isinstance(p.get(axis), (int, float))
            and not isinstance(p[axis], bool) and math.isfinite(p[axis])
            and abs(p[axis] - start[i] - round(p[axis] - start[i])) < 1e-6
            for i, axis in enumerate(("x", "y")))
    if (any(key not in survey or not (isinstance(survey[key], list) or survey[key] == {})
            for key in ("blocked", "belts", "mouths"))
            or any(not valid_point(p) for p in survey["blocked"])
            or any(not isinstance(row, dict) or not valid_point(row.get("position"))
                   or type(row.get("direction")) is not int or row["direction"] not in DIRECTIONS
                   for row in [*survey["belts"], *survey["mouths"]])
            or any(row.get("name") not in SURFACE_NAMES for row in survey["belts"])
            or any(row.get("name") not in UNDERGROUND_NAMES or row.get("belt_to_ground_type") not in {"input", "output"}
                   for row in survey["mouths"])):
        return {**failure, "reason": "incomplete underground obstacle survey"}
    _stopped(factory)
    physical = factory.builder._occupied_by_plan(reserved) | {_point(p) for p in survey["blocked"]}
    belts = {}
    mouths = list(survey["mouths"])
    for row in [*reserved, *survey["belts"]]:
        if row["name"] in SURFACE_NAMES:
            point, direction = _point(row), row.get("direction", 0)
            if direction not in DIRECTIONS or point in belts and belts[point] != direction:
                return {**failure, "reason": "underground route belt facing conflict"}
            belts[point] = direction
        elif row["name"] in UNDERGROUND_NAMES:
            mouths.append(row)
    if any(not valid_point(row.get("position")) or type(row.get("direction")) is not int
           or row["direction"] not in DIRECTIONS or row.get("belt_to_ground_type") not in {"input", "output"}
           for row in mouths):
        return {**failure, "reason": "invalid reserved underground mouth"}
    if any(point in belts and belts[point] != facing
           for point, facing in ((start, start_direction), (finish, end_direction))):
        return {**failure, "reason": "underground route endpoint facing changed"}
    occupied = physical | set(belts) | factory._port_clearances()
    for point, direction in belts.items():
        if point not in (start, finish):
            dx, dy = DIRECTIONS[direction]
            occupied.add((point[0] + dx, point[1] + dy))
    for mouth in mouths:
        point = _point(mouth)
        occupied.add(point)
        if mouth.get("belt_to_ground_type") == "output" and mouth.get("direction") in DIRECTIONS:
            dx, dy = DIRECTIONS[mouth["direction"]]
            occupied.add((point[0] + dx, point[1] + dy))
    for point, facing, sign in ((start, start_direction, 1), (finish, end_direction, -1)):
        dx, dy = DIRECTIONS[facing]
        clearance = point[0] + sign * dx, point[1] + sign * dy
        if clearance not in physical and clearance not in belts:
            # Only the caller's own port approach may be opened, never live
            # foreign belt outputs or another underground outlet.
            foreign_front = any((p[0] + DIRECTIONS[d][0], p[1] + DIRECTIONS[d][1]) == clearance
                                for p, d in belts.items() if p not in (start, finish))
            if not foreign_front and not any(_point(m) == clearance or
                    (m.get("belt_to_ground_type") == "output" and
                     (_point(m)[0] + DIRECTIONS[m["direction"]][0], _point(m)[1] + DIRECTIONS[m["direction"]][1]) == clearance)
                    for m in mouths):
                occupied.discard(clearance)
    occupied.difference_update((start, finish))
    free = {(bounds["min_x"] + x, bounds["min_y"] + y)
            for x in range(int(bounds["max_x"] - bounds["min_x"]) + 1)
            for y in range(int(bounds["max_y"] - bounds["min_y"]) + 1)} - occupied
    components = {}
    for point in sorted(free):
        if point in components:
            continue
        label = point
        queue = deque([point]); components[point] = label
        while queue:
            x, y = queue.popleft()
            for dx, dy in DIRECTIONS.values():
                other = x + dx, y + dy
                if other in free and other not in components:
                    components[other] = label; queue.append(other)
    if start not in components or finish not in components:
        return failure
    def clear_mouth(point, direction):
        if point in (start, finish) or point not in free:
            return False
        dx, dy = DIRECTIONS[direction]
        return not any(abs((p[0] - point[0]) * dy - (p[1] - point[1]) * dx) < .1
                       and abs((p[0] - point[0]) * dx + (p[1] - point[1]) * dy) <= maximum
                       for p in map(_point, mouths))
    inlets = [(p, direction) for p in free if components[p] == components[start]
              for direction, (dx, dy) in DIRECTIONS.items()
              if clear_mouth(p, direction) and components.get((p[0] - dx, p[1] - dy)) == components[start]
              and (p[0] + dx, p[1] + dy) not in free]
    inlets.sort(key=lambda row: (math.dist(start, row[0]), math.dist(row[0], finish), row))
    candidates = []
    for inlet, direction in inlets[:MAX_INLETS]:
        dx, dy = DIRECTIONS[direction]
        pending = [([], inlet)]
        for _ in range(2):
            following = []
            for pairs, entry in pending:
                if not clear_mouth(entry, direction):
                    continue
                for span in range(2, maximum + 1):
                    outlet = entry[0] + span * dx, entry[1] + span * dy
                    after = outlet[0] + dx, outlet[1] + dy
                    if not clear_mouth(outlet, direction) or after not in free:
                        continue
                    rows = pairs + [(entry, outlet, direction)]
                    if components[outlet] == components[finish] and components[after] == components[finish]:
                        candidates.append(rows)
                    following.append((rows, after))
            pending = following
    candidates.sort(key=lambda rows: (len(rows), math.dist(start, rows[0][0]) + math.dist(rows[-1][1], finish), rows))
    def ordered_candidates():
        yield from candidates
        if len(candidates) < MAX_CANDIDATES:
            _stopped(factory)
            yield from _component_chains(free, components, start, finish, clear_mouth, maximum)
    budget = MAX_ROUTE_NODES
    for pairs in islice(ordered_candidates(), MAX_CANDIDATES):
        _stopped(factory)
        endpoints = [e for inlet, outlet, direction in pairs
                     for e in (_entity(inlet, direction, "input"), _entity(outlet, direction, "output"))]
        records = [{"input": endpoints[i], "output": endpoints[i + 1], "max_distance": maximum}
                   for i in range(0, len(endpoints), 2)]
        try:
            underground_edges({"entities": endpoints, "underground_pairs": records})
        except ValueError:
            continue
        mouth_points = {_point(e) for e in endpoints}
        blocked = occupied | mouth_points
        legs = []
        connections = [(start, pairs[0][0], start_direction, pairs[0][2])]
        for previous, following in zip(pairs, pairs[1:]):
            dx, dy = DIRECTIONS[previous[2]]
            if previous[2] != following[2] or following[0] != (previous[1][0] + dx, previous[1][1] + dy):
                connections.append((previous[1], following[0], previous[2], following[2]))
        connections.append((pairs[-1][1], finish, pairs[-1][2], end_direction))
        for a, b, departure, arrival in connections:
            if budget <= 0:
                return {**failure, "reason": "underground route node budget exhausted"}
            leg = route_orthogonal(a, b, occupied=blocked - {a, b}, bounds=bounds,
                max_nodes=min(25000, budget), start_direction=departure, end_direction=arrival)
            budget -= int(leg.get("visited", 0))
            if not leg.get("ok"):
                break
            legs.append(leg)
            for row in leg["segments"]:
                p = _point(row); vx, vy = DIRECTIONS[row["direction"]]
                blocked.update((p, (p[0] + vx, p[1] + vy)))
        if len(legs) != len(connections):
            continue
        rows = [{"name": "transport-belt", **row} for leg in legs for row in leg["segments"]]
        unique = {_point(row): row for row in rows}
        if len(unique) != len(rows):
            continue
        for endpoint in endpoints:
            unique[_point(endpoint)] = endpoint
        terminal = unique.pop(finish)
        plan = {"ok": True, "segments": [*unique.values(), terminal],
                "underground_pairs": deepcopy(records), "flow_verified": False}
        try:
            underground_edges({"entities": plan["segments"], "underground_pairs": records})
        except ValueError:
            continue
        if factory.builder.can_place(plan["segments"]).get("ok"):
            return plan
        return {**failure, "reason": "combined underground placement changed"}
    return failure
