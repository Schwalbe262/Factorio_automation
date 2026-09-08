"""Bounded ordinary-inserter crossings between surveyed free belt regions."""
from __future__ import annotations

from collections import defaultdict, deque
import heapq
import json
import math

from .deterministic_state import stop_requested
from .factory_templates import DIRECTIONS, route_orthogonal


MAX_CROSSINGS = 6
MAX_CHAINS = 24
MAX_ROUTE_NODES = 100000


def _point(row):
    return row["position"]["x"], row["position"]["y"]


def _position(point):
    return {"x": point[0], "y": point[1]}


def _entity(name, point, direction=0):
    return {"name": name, "position": _position(point), "direction": direction}


def _stopped(factory):
    if stop_requested(factory.path.parent / "stop.json"):
        raise InterruptedError("operator_stop_requested")


def plan_belt_crossings(factory, source, destination, reserved, *,
                        start_direction=None, end_direction=None):
    """Survey once, then select up to six crossings without per-leg RCON.

    Region connectivity selects useful crossings before geometric route work.
    It therefore reaches enclosed producers whose exits are far down a purely
    distance-sorted candidate list. Every selected leg, arm and supplying pole
    remains subject to collision, facing and final live placement validation.
    """
    failure = {"ok": False, "reason": "no clear powered long-inserter belt crossing"}
    _stopped(factory)
    start, finish = (source["x"], source["y"]), (destination["x"], destination["y"])
    if (any(d is not None and (type(d) is not int or d not in DIRECTIONS) for d in (start_direction, end_direction))
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                       for v in (*start, *finish))
            or any(abs((finish[i] - start[i]) - round(finish[i] - start[i])) > 1e-6 for i in (0, 1))):
        return {**failure, "reason": "invalid belt crossing endpoints or directions"}
    bounds = {"min_x": min(start[0], finish[0]) - 48, "max_x": max(start[0], finish[0]) + 48,
              "min_y": min(start[1], finish[1]) - 48, "max_y": max(start[1], finish[1]) + 48}
    if (bounds["max_x"] - bounds["min_x"] + 1) * (bounds["max_y"] - bounds["min_y"] + 1) > 50000:
        return {**failure, "reason": "crossing survey exceeds 50000 tiles"}
    payload = json.dumps(json.dumps(bounds, separators=(",", ":")))
    survey = factory.game.query('''
--[[ bounded belt crossing survey ]]
local recipe=f.recipes["long-handed-inserter"]
if not recipe or not recipe.enabled then return {ok=false,reason="long inserter recipe is locked"} end
local b=helpers.json_to_table(''' + payload + ''');local blocked={};local belts={}
for x=b.min_x,b.max_x do for y=b.min_y,b.max_y do
 local p={x=x,y=y}
 if not s.can_place_entity{name="transport-belt",position=p,force=f} then blocked[#blocked+1]=p end
end end
for _,e in pairs(s.find_entities_filtered{force=f,type="transport-belt",
 area={{b.min_x-1,b.min_y-1},{b.max_x+1,b.max_y+1}}}) do
 belts[#belts+1]={name=e.name,position=pos(e.position),direction=e.direction}
end
return {ok=true,blocked=blocked,belts=belts}
''')
    if not survey.get("ok"):
        return {**failure, "reason": survey.get("reason", "crossing survey failed")}
    def point_valid(point):
        return (isinstance(point, dict) and all(isinstance(point.get(axis), (int, float))
                and not isinstance(point[axis], bool) and math.isfinite(point[axis])
                and abs((point[axis] - start[index]) - round(point[axis] - start[index])) < 1e-6
                for index, axis in enumerate(("x", "y"))))
    # Factorio serializes empty arrays as {}; absent fields are not evidence
    # that there are no foreign belts feeding into otherwise placeable tiles.
    if (any(key not in survey or not (isinstance(survey[key], list) or survey[key] == {})
            for key in ("blocked", "belts"))
            or any(not point_valid(point) for point in survey["blocked"])
            or any(not isinstance(row, dict) or row.get("name") != "transport-belt"
                   or not point_valid(row.get("position")) or type(row.get("direction")) is not int
                   or row["direction"] not in DIRECTIONS
                   for row in survey["belts"])):
        return {**failure, "reason": "incomplete or invalid belt crossing survey"}
    _stopped(factory)
    physical = factory.builder._occupied_by_plan(reserved)
    physical.update((p["x"], p["y"]) for p in survey.get("blocked", []))
    belts = {}
    for row in [*reserved, *survey.get("belts", [])]:
        if row["name"] != "transport-belt":
            continue
        point, direction = _point(row), row.get("direction", 0)
        if direction not in DIRECTIONS or (point in belts and belts[point] != direction):
            return {**failure, "reason": "crossing survey has contradictory belt directions"}
        belts[point] = direction
        physical.add(point)
    for point, direction in ((start, start_direction), (finish, end_direction)):
        if point in belts and direction is not None and belts[point] != direction:
            return {**failure, "reason": "crossing endpoint belt facing changed"}
    clearance = factory._port_clearances()
    for point, direction, sign in ((start, start_direction, 1), (finish, end_direction, -1)):
        if direction in DIRECTIONS:
            dx, dy = DIRECTIONS[direction]
            clearance.discard((point[0] + dx * sign, point[1] + dy * sign))
    occupied = physical | clearance
    for point, direction in belts.items():
        if point not in (start, finish):
            dx, dy = DIRECTIONS[direction]
            occupied.add((point[0] + dx, point[1] + dy))
    occupied.difference_update((start, finish))
    # Free components include terrain and existing belt-output exclusions.
    free = {(bounds["min_x"] + x, bounds["min_y"] + y)
            for x in range(int(bounds["max_x"] - bounds["min_x"]) + 1)
            for y in range(int(bounds["max_y"] - bounds["min_y"]) + 1)} - occupied
    components = {}
    component = 0
    for point in sorted(free):
        if point in components:
            continue
        queue = deque([point])
        components[point] = component
        while queue:
            x, y = queue.popleft()
            for dx, dy in DIRECTIONS.values():
                other = x + dx, y + dy
                if other in free and other not in components:
                    components[other] = component
                    queue.append(other)
        component += 1
    if start not in components or finish not in components:
        return failure
    raw_edges = []
    for point, facing in sorted(belts.items()):
        for direction in ((4, 12) if facing in (0, 8) else (0, 8)):
            dx, dy = DIRECTIONS[direction]
            pickup, drop = (point[0] - 3 * dx, point[1] - 3 * dy), (point[0] + dx, point[1] + dy)
            arm = point[0] - dx, point[1] - dy
            before, after = (pickup[0] - dx, pickup[1] - dy), (drop[0] + dx, drop[1] + dy)
            origin, target = components.get(pickup), components.get(drop)
            if (origin is None or target is None or origin == target or arm in occupied
                    or (pickup not in (start, finish) and pickup in physical)
                    or (drop not in (start, finish) and drop in physical)
                    or (pickup == start and start_direction not in (None, direction))
                    or (drop == finish and end_direction not in (None, direction))):
                continue
            edge = {"from": origin, "to": target, "pickup": pickup, "drop": drop,
                    "arm": arm, "direction": direction, "over": point,
                    "entry": pickup == start or components.get(before) == origin,
                    "exit": drop == finish or components.get(after) == target}
            raw_edges.append(edge)
    # Immediate handoffs across trunks four tiles apart have one shared belt.
    # Represent these as weighted composite edges: their first pickup and final
    # drop still need usable directed approaches. Merely accepting every free
    # pickup/drop would invent short graph paths through blocked belt facings.
    by_pickup = {(e["pickup"], e["direction"]): e for e in raw_edges}
    edges, reverse = defaultdict(list), defaultdict(set)
    for first in raw_edges:
        if not first["entry"]:
            continue
        chain = []
        edge = first
        for _ in range(MAX_CROSSINGS):
            chain.append(edge)
            if edge["exit"] and edge["to"] != first["from"]:
                combined = {"from": first["from"], "to": edge["to"], "crossings": tuple(chain)}
                edges[first["from"]].append(combined)
                reverse[edge["to"]].add((first["from"], len(chain)))
            edge = by_pickup.get((edge["drop"], edge["direction"]))
            if edge is None:
                break
    target_component, source_component = components[finish], components[start]
    distances, frontier = {target_component: 0}, [(0, target_component)]
    while frontier:
        distance, current = heapq.heappop(frontier)
        if distance != distances[current]:
            continue
        for previous, cost in sorted(reverse[current]):
            candidate = distance + cost
            if candidate <= MAX_CROSSINGS and candidate < distances.get(previous, math.inf):
                distances[previous] = candidate
                heapq.heappush(frontier, (candidate, previous))
    if source_component not in distances:
        return {**failure, "reason": "no route within bounded ordinary belt crossings"}
    # Keep a finite beam of short chains, all making actual component progress.
    pending, chains = [(0.0, (), source_component, start)], []
    for _ in range(MAX_CROSSINGS + 1):
        choices = []
        for score, chain, current, previous in pending:
            if current == target_component:
                chains.append((score, chain, current, previous))
                continue
            for edge in edges[current]:
                crossing_chain = edge["crossings"]
                if distances.get(edge["to"]) != distances[current] - len(crossing_chain):
                    continue
                cost = score + math.dist(previous, crossing_chain[0]["pickup"]) + 4 * len(crossing_chain)
                choices.append((cost, (*chain, *crossing_chain), edge["to"], crossing_chain[-1]["drop"]))
        choices.sort(key=lambda row: (row[0] + math.dist(row[3], finish),
                                     tuple((e["over"], e["direction"]) for e in row[1])))
        pending = choices[:MAX_CHAINS]
        if not pending:
            break
    chains.sort(key=lambda row: (row[0] + math.dist(row[3], finish),
                                tuple((e["over"], e["direction"]) for e in row[1])))
    attempts, node_budget = 0, [MAX_ROUTE_NODES]
    for _, chain, _, _ in chains[:MAX_CHAINS]:
        _stopped(factory)
        attempts += 1
        plan = _construct(factory, start, finish, chain, physical, occupied, bounds,
                          start_direction, end_direction, node_budget)
        if plan is None:
            if node_budget[0] <= 0:
                break
            continue
        if factory.builder.can_place(plan["segments"]).get("ok"):
            return {**plan, "crossing_candidates_checked": attempts}
        # An authoritative whole-plan rejection indicates changed/unsupported
        # placement; do not turn it into another RCON candidate search.
        return {**failure, "reason": "combined crossing placement changed"}
    return {**failure, "crossing_candidates_checked": attempts}


def _construct(factory, start, finish, chain, physical, occupied, bounds, start_direction, end_direction, node_budget):
    equipment = {}
    for edge in chain:
        for name, point, direction in (("long-handed-inserter", edge["arm"], (edge["direction"] + 8) % 16),
                                      ("transport-belt", edge["pickup"], edge["direction"]),
                                      ("transport-belt", edge["drop"], edge["direction"])):
            entity = _entity(name, point, direction)
            if point in equipment and equipment[point] != entity:
                return None
            equipment[point] = entity
    blocked = occupied | set(equipment)
    legs = []
    current, departure = start, start_direction
    for edge in (*chain, None):
        _stopped(factory)
        if node_budget[0] <= 0:
            return None
        target, arrival = (edge["pickup"], edge["direction"]) if edge else (finish, end_direction)
        route = route_orthogonal(current, target, occupied=blocked - {current, target}, bounds=bounds,
                                 max_nodes=min(25000, node_budget[0]), start_direction=departure, end_direction=arrival)
        node_budget[0] -= int(route.get("visited", 0))
        if not route.get("ok"):
            return None
        legs.append(route)
        for row in route["segments"]:
            point = _point(row)
            blocked.add(point)
            dx, dy = DIRECTIONS[row["direction"]]
            blocked.add((point[0] + dx, point[1] + dy))
        if edge:
            current, departure = edge["drop"], edge["direction"]
    rows = []
    for index, leg in enumerate(legs):
        rows.extend({"name": "transport-belt", **row} for row in leg["segments"])
        if index < len(chain):
            rows.append(equipment[chain[index]["arm"]])
    unique = {}
    for row in rows:
        point = _point(row)
        if point in unique and unique[point] != row:
            return None
        unique[point] = row
    # Pick poles after every directed leg is reserved. Their footprints cannot
    # cut the selected belt path or an existing/future port approach.
    unavailable = blocked | physical | set(unique)
    poles = []
    for edge in chain:
        candidates = factory._intake_poles(_position(edge["arm"]), list(unique.values()) + poles)
        pole = next((row for row in candidates if _point(row) not in unavailable), None)
        if pole is None:
            return None
        poles.append(pole)
        unavailable.add(_point(pole))
    # Keep destination last, matching the existing material-route contract.
    destination_row = unique.pop(finish)
    segments = [*unique.values(), *poles, destination_row]
    return {"ok": True, "path": [p for leg in legs for p in leg["path"]], "segments": segments,
            "crossings": [{"kind": "long-handed-inserter", "over": _position(edge["over"])} for edge in chain],
            "flow_verified": False}
