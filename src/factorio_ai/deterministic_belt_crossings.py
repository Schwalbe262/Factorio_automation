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


def _owned_drop_valid(factory, destination, proof):
    """An explicit same-item output suffix owns the existing final drop belt."""
    from .deterministic_input_links import _geometry, _path, _powered_prefix
    try:
        port, bus = proof["port"], proof["bus_port"]
        if (proof["world_id"] != factory.state.get("world_id") or not proof["world_id"]
                or factory.state.get("catalog_fingerprint") != factory.catalog.fingerprint
                or factory._fingerprint != factory.catalog.fingerprint
                or type(proof["tick"]) is not int or proof["tick"] < factory.state.get("last_tick", 0)
                or proof["tick"] < 0 or type(proof["unit_number"]) is not int or proof["unit_number"] < 1
                or port["position"] != destination or type(port["facing"]) is not int or port["facing"] not in DIRECTIONS
                or not port["item"] or port["item"] != bus["item"]
                or any(p.get("kind") != "item" or p.get("direction") != "output" for p in (port, bus))
                or proof["category"] not in ("blocks", "links")):
            return False
        owner = factory.state[proof["category"]][proof["key"]]
        if proof["category"] == "blocks":
            if bus not in owner.get("ports", []):
                return False
        elif owner.get("consumer_port") != bus or (owner.get("source_port") or {}).get("item") != port["item"]:
            return False
        belts, edges, _ = _geometry(owner)
        start, end = (destination["x"], destination["y"]), (bus["position"]["x"], bus["position"]["y"])
        path = _path(belts, edges, start, end)
        if not path or belts[start]["direction"] != port["facing"] or belts[end]["direction"] != bus["facing"]:
            return False
        _powered_prefix(owner, path)
        foreign = [e for plan in factory.state["links"].values()
                   if any((plan.get(field) or {}).get("item") not in (None, port["item"])
                          for field in ("source_port", "consumer_port")) for e in plan.get("entities", [])]
        if factory.builder._occupied_by_plan(path) & factory.builder._occupied_by_plan(foreign):
            return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def plan_belt_crossings(factory, source, destination, reserved, *,
                        start_direction=None, end_direction=None, owned_drop=None):
    """Survey once, then select up to six crossings without per-leg RCON.

    Region connectivity selects useful crossings before geometric route work.
    It therefore reaches enclosed producers whose exits are far down a purely
    distance-sorted candidate list. Every selected leg, arm and supplying pole
    remains subject to collision, facing and final live placement validation.
    """
    failure = {"ok": False, "reason": "no clear powered long-inserter belt crossing"}
    _stopped(factory)
    if owned_drop is not None and not _owned_drop_valid(factory, destination, owned_drop):
        return {**failure, "reason": "invalid owned output drop proof"}
    # An enclosed owned output may need later chains after short directed legs
    # fail. Keep the same total routing-node and crossing limits.
    chain_limit = MAX_CHAINS * (2 if owned_drop is not None else 1)
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
    payload = json.dumps(json.dumps({"bounds": bounds, "owned_drop": owned_drop}, separators=(",", ":")))
    survey = factory.game.query('''
--[[ bounded belt crossing survey ]]
local recipe=f.recipes["long-handed-inserter"]
if not recipe or not recipe.enabled then return {ok=false,reason="long inserter recipe is locked"} end
local args=helpers.json_to_table(''' + payload + ''');local b=args.bounds;local blocked={};local belts={}
local owned=args.owned_drop
if owned then
 local belt=target(owned.port.position,"transport-belt")
 if not d or d.world_id~=owned.world_id or game.tick<owned.tick or not belt or belt.force~=f
  or belt.unit_number~=owned.unit_number or belt.direction~=owned.port.facing then
  return {ok=false,reason="owned output drop identity changed"}
 end
 for lane=1,2 do for _,row in pairs(belt.get_transport_line(lane).get_contents()) do
  if row.name~=owned.port.item and row.count>0 then return {ok=false,reason="owned output drop carries another material"} end
 end end
end
for x=b.min_x,b.max_x do for y=b.min_y,b.max_y do
 local p={x=x,y=y}
 if not s.can_place_entity{name="transport-belt",position=p,force=f} then blocked[#blocked+1]=p end
end end
for _,e in pairs(s.find_entities_filtered{force=f,type="transport-belt",
 area={{b.min_x-1,b.min_y-1},{b.max_x+1,b.max_y+1}}}) do
 belts[#belts+1]={name=e.name,position=pos(e.position),direction=e.direction}
end
return {ok=true,blocked=blocked,belts=belts,owned_drop_verified=owned~=nil}
''')
    if not survey.get("ok"):
        return {**failure, "reason": survey.get("reason", "crossing survey failed")}
    if owned_drop is not None and survey.get("owned_drop_verified") is not True:
        return {**failure, "reason": "owned output drop was not verified"}
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
    if owned_drop is not None and (belts.get(finish) != owned_drop["port"]["facing"]
            or not any(e.get("name") == "transport-belt" and e.get("position") == destination
                       and e.get("direction") == owned_drop["port"]["facing"] for e in reserved)):
        return {**failure, "reason": "owned output drop reservation or survey changed"}
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
            # The crossed belt may lie between the arm and its drop OR its
            # pickup. Both use the same ordinary two-tile inserter reach.
            for offset in (3, 1):
                pickup = point[0] - offset * dx, point[1] - offset * dy
                arm = pickup[0] + 2 * dx, pickup[1] + 2 * dy
                drop = pickup[0] + 4 * dx, pickup[1] + 4 * dy
                pickup_direction = direction
                if offset == 1:
                    # A belt facing the crossed trunk would spill its material
                    # into it before the arm can collect everything. Receive
                    # from the ordinary approach but turn this pickup sideways
                    # toward a reserved empty tile instead.
                    pickup_direction = next((side for side in ((direction + 4) % 16, (direction + 12) % 16)
                        if (pickup[0] + DIRECTIONS[side][0], pickup[1] + DIRECTIONS[side][1]) not in occupied), None)
                    if pickup_direction is None or pickup == start:
                        continue
                px, py = DIRECTIONS[pickup_direction]
                if (pickup[0] + px, pickup[1] + py) in belts:
                    continue
                before, after = (pickup[0] - dx, pickup[1] - dy), (drop[0] + dx, drop[1] + dy)
                origin, target = components.get(pickup), components.get(drop)
                if (origin is None or target is None or origin == target or arm in occupied
                        or (pickup not in (start, finish) and pickup in physical)
                        or (drop not in (start, finish) and drop in physical)
                        or (pickup == start and start_direction not in (None, direction))
                        or (drop == finish and end_direction not in (None, direction))):
                    continue
                edge = {"from": origin, "to": target, "pickup": pickup, "drop": drop,
                        "arm": arm, "direction": direction, "pickup_direction": pickup_direction, "over": point,
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
        pending = choices[:chain_limit]
        if not pending:
            break
    chains.sort(key=lambda row: (row[0] + math.dist(row[3], finish),
                                tuple((e["over"], e["direction"]) for e in row[1])))
    attempts, node_budget = 0, [MAX_ROUTE_NODES]
    for _, chain, _, _ in chains[:chain_limit]:
        _stopped(factory)
        attempts += 1
        plan = _construct(factory, start, finish, chain, physical, occupied, bounds,
                          start_direction, end_direction, node_budget,
                          owned_drop["port"]["facing"] if owned_drop is not None else None)
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


def _construct(factory, start, finish, chain, physical, occupied, bounds, start_direction, end_direction, node_budget,
               owned_drop_direction=None):
    equipment = {}
    pickup_fronts = set()
    for index, edge in enumerate(chain):
        pickup_direction = edge.get("pickup_direction", edge["direction"])
        following = chain[index + 1] if index + 1 < len(chain) else None
        drop_direction = (following.get("pickup_direction", following["direction"])
                          if following and edge["drop"] == following["pickup"] else edge["direction"])
        # An arm deposits into the existing belt independently of its facing.
        # Ordinary belt arrivals and intermediate crossing belts keep their rules.
        if owned_drop_direction is not None and following is None and edge["drop"] == finish:
            drop_direction = owned_drop_direction
        for name, point, direction in (("long-handed-inserter", edge["arm"], (edge["direction"] + 8) % 16),
                                      ("transport-belt", edge["pickup"], pickup_direction),
                                      ("transport-belt", edge["drop"], drop_direction)):
            entity = _entity(name, point, direction)
            if point in equipment and equipment[point] != entity:
                return None
            equipment[point] = entity
        if pickup_direction != edge["direction"]:
            dx, dy = DIRECTIONS[pickup_direction]
            pickup_fronts.add((edge["pickup"][0] + dx, edge["pickup"][1] + dy))
    if pickup_fronts & (set(equipment) | {start, finish}):
        return None
    blocked = occupied | set(equipment) | pickup_fronts
    legs = []
    current, departure = start, start_direction
    for edge in (*chain, None):
        _stopped(factory)
        if node_budget[0] <= 0:
            return None
        target, arrival = (edge["pickup"], edge["direction"]) if edge else (finish, end_direction)
        if current == target and current in equipment:
            route = {"ok": True, "path": [current], "segments": [dict(equipment[current])], "visited": 0}
        else:
            route = route_orthogonal(current, target, occupied=blocked - {current, target}, bounds=bounds,
                                     max_nodes=min(25000, node_budget[0]), start_direction=departure, end_direction=arrival)
        node_budget[0] -= int(route.get("visited", 0))
        if not route.get("ok"):
            return None
        if edge:
            route["segments"][-1]["direction"] = equipment[edge["pickup"]]["direction"]
        legs.append(route)
        for row in route["segments"]:
            point = _point(row)
            blocked.add(point)
            dx, dy = DIRECTIONS[row["direction"]]
            blocked.add((point[0] + dx, point[1] + dy))
        if edge:
            current, departure = edge["drop"], equipment[edge["drop"]]["direction"]
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
    for edge in chain:
        if edge.get("pickup_direction", edge["direction"]) != edge["direction"]:
            # Future block placement must preserve this dead-end discharge
            # tile too; otherwise a later input belt could collect this item.
            unique[edge["pickup"]]["_keep_output_clear"] = True
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
    if owned_drop_direction is not None and (not chain or chain[-1]["drop"] != finish):
        # An adjacent surface belt can side-feed this exact owned output too.
        # Keep its live facing only when the arrival cannot feed head-on.
        incoming = destination_row["direction"]
        dx, dy = DIRECTIONS[incoming]
        previous = unique.get((finish[0] - dx, finish[1] - dy))
        if (incoming == (owned_drop_direction + 8) % 16 or previous is None
                or previous["name"] != "transport-belt" or previous["direction"] != incoming):
            return None
        destination_row["direction"] = owned_drop_direction
    segments = [*unique.values(), *poles, destination_row]
    return {"ok": True, "path": [p for leg in legs for p in leg["path"]], "segments": segments,
            "crossings": [{"kind": "long-handed-inserter", "over": _position(edge["over"])} for edge in chain],
            "flow_verified": False}
