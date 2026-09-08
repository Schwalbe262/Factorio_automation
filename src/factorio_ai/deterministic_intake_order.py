"""Order finite turret intake alternatives by current local entrance space."""
from collections import deque
import json
import math

from .factory_templates import DIRECTIONS


RADIUS = 6


def reusable_poles(driver, turret):
    """Current powered poles already reserved by another owned plan."""
    key = "armaments:" + driver._key(turret)
    wanted = {}
    for group in ("blocks", "links", "power_links"):
        for owner, plan in sorted(driver.factory.state.get(group, {}).items()):
            if owner == key:
                continue
            for e in plan.get("entities", []):
                p = e["position"]
                if e["name"] == "small-electric-pole" and max(abs(p[axis] - turret["position"][axis]) for axis in ("x", "y")) <= 4:
                    wanted.setdefault((p["x"], p["y"]), {"position": p, "owner_group": group, "owner_key": owner})
    if not wanted:
        return {}
    payload = {"world": driver.state.get("world_id"), "unit": turret.get("unit_number"),
               "position": turret["position"], "poles": list(wanted.values())}
    encoded = json.dumps(json.dumps(payload, separators=(",", ":")))
    result = driver.game.query('''
local x=helpers.json_to_table(''' + encoded + ''');local t=target(x.position,"gun-turret")
if not d or d.world_id~=x.world or not t or t.force~=f or t.unit_number~=x.unit
 or t.position.x~=x.position.x or t.position.y~=x.position.y then return {ok=false} end
local networks={};for _,e in pairs(s.find_entities_filtered{force=f,type="generator"}) do
 if e.energy>0 and e.electric_network_id then networks[e.electric_network_id]=true end
end
local poles={};for _,p in ipairs(x.poles) do local e=target(p.position,"small-electric-pole")
 if e and e.force==f and networks[e.electric_network_id] and e.position.x==p.position.x and e.position.y==p.position.y then
  poles[#poles+1]={position=pos(e.position),unit_number=e.unit_number,reach=e.prototype.get_supply_area_distance(e.quality)}
 end
end
return {ok=true,poles=poles,tick=game.tick}
''')
    if (not isinstance(result, dict) or result.get("ok") is not True or type(result.get("tick")) is not int
            or result["tick"] < driver.state.get("last_tick", 0)
            or (not isinstance(result.get("poles"), list) and result.get("poles") != {})):
        return {}
    found = {}
    for row in result["poles"]:
        if (not isinstance(row, dict) or not isinstance(row.get("position"), dict)
                or type(row.get("unit_number")) is not int or row["unit_number"] <= 0
                or type(row.get("reach")) not in (int, float) or not math.isfinite(row["reach"]) or row["reach"] <= 0):
            return {}
        point = row["position"].get("x"), row["position"].get("y")
        if point not in wanted:
            return {}
        found[point] = {**wanted[point], **row, "world_id": driver.state["world_id"],
                        "catalog_fingerprint": driver.catalog.fingerprint}
    return found


def inherited_pole(driver, obs, planned, actual, intake_key=None):
    proof = planned.get("_shared_power")
    if (planned.get("name") != "small-electric-pole" or not isinstance(proof, dict)
            or proof.get("world_id") != obs.get("world_id")
            or proof.get("catalog_fingerprint") != driver.catalog.fingerprint
            or proof.get("owner_group") not in ("blocks", "links", "power_links")
            or proof.get("owner_key") == intake_key
            or proof.get("unit_number") != actual.get("unit_number")
            or type(proof.get("unit_number")) is not int or proof["unit_number"] <= 0):
        return False
    owner = driver.factory.state.get(proof.get("owner_group"), {}).get(proof.get("owner_key"), {})
    return any(e["name"] == planned["name"] and e["position"] == planned["position"] for e in owner.get("entities", []))


def order_intakes(driver, turret, plans, previous=None):
    """A ranking hint only: keep every candidate and all final route checks."""
    fallback = ([(plan, None) for plan in plans], None)
    if not plans:
        return fallback
    key = "armaments:" + driver._key(turret)
    probes = [*plans, *([previous] if previous else [])]
    def entrance(plan):
        port = plan["ports"][0]
        dx, dy = DIRECTIONS[port["facing"]]
        return port["position"]["x"] - dx, port["position"]["y"] - dy
    try:
        points = [entrance(plan) for plan in probes]
        bounds = {"min_x": min(p[0] for p in points) - RADIUS, "max_x": max(p[0] for p in points) + RADIUS,
                  "min_y": min(p[1] for p in points) - RADIUS, "max_y": max(p[1] for p in points) + RADIUS}
        if (bounds["max_x"] - bounds["min_x"] + 1) * (bounds["max_y"] - bounds["min_y"] + 1) > 1024:
            return fallback
        payload = {"bounds": bounds, "world": driver.state["world_id"], "unit": turret["unit_number"],
                   "position": turret["position"]}
        encoded = json.dumps(json.dumps(payload, separators=(",", ":")))
        survey = driver.game.query('''
--[[ Local intake ordering is not a material route or placement authorization. ]]
local x=helpers.json_to_table(''' + encoded + ''');local t=target(x.position,"gun-turret")
if not d or d.world_id~=x.world or not t or t.force~=f or t.unit_number~=x.unit
 or t.position.x~=x.position.x or t.position.y~=x.position.y then return {ok=false} end
local b=x.bounds;local blocked={};local fronts={};local vectors={[0]={0,-1},[4]={1,0},[8]={0,1},[12]={-1,0}}
for x=b.min_x,b.max_x do for y=b.min_y,b.max_y do
 if not s.can_place_entity{name="transport-belt",position={x=x,y=y},force=f} then blocked[#blocked+1]={x=x,y=y} end
end end
for _,e in pairs(s.find_entities_filtered{type="transport-belt",force=f,
 area={{b.min_x-1,b.min_y-1},{b.max_x+1,b.max_y+1}}}) do
 local v=vectors[e.direction];if v then fronts[#fronts+1]={x=e.position.x+v[1],y=e.position.y+v[2]} end
end
return {ok=true,blocked=blocked,fronts=fronts,tick=game.tick}
''')
        if (not isinstance(survey, dict) or survey.get("ok") is not True
                or type(survey.get("tick")) is not int or survey["tick"] < driver.state.get("last_tick", 0)):
            return fallback
        physical = set()
        for name in ("blocked", "fronts"):
            rows = survey.get(name)
            if not isinstance(rows, list) and rows != {}:
                return fallback
            for point in rows:
                if (not isinstance(point, dict) or any(type(point.get(axis)) not in (int, float)
                        or not math.isfinite(point[axis])
                        or abs(point[axis] - points[0][i] - round(point[axis] - points[0][i])) > 1e-6
                        for i, axis in enumerate(("x", "y")))):
                    return fallback
                physical.add((point["x"], point["y"]))
        reserved = driver.factory._reserved(exclude=key)
        physical |= driver.builder._occupied_by_plan(reserved) | driver.factory._port_clearances(exclude=key)
        for e in reserved:
            if e["name"] == "transport-belt":
                dx, dy = DIRECTIONS[e.get("direction", 0)]
                physical.add((e["position"]["x"] + dx, e["position"]["y"] + dy))
        def score(plan):
            start = entrance(plan)
            blocked = physical | driver.builder._occupied_by_plan(plan["entities"])
            if start in blocked:
                return (0, 0)
            seen, queue, edge = {start}, deque([start]), False
            while queue:
                x, y = queue.popleft()
                edge |= abs(x - start[0]) == RADIUS or abs(y - start[1]) == RADIUS
                for dx, dy in DIRECTIONS.values():
                    point = x + dx, y + dy
                    if (point not in seen and point not in blocked and abs(point[0] - start[0]) <= RADIUS
                            and abs(point[1] - start[1]) <= RADIUS):
                        seen.add(point)
                        queue.append(point)
            return (int(edge), len(seen))
        groups = {}
        for index, plan in enumerate(plans):
            port = plan["ports"][0]
            group = (port["position"]["x"], port["position"]["y"], port["facing"])
            groups.setdefault(group, []).append((plan, score(plan), index))
        for group in groups.values():
            group.sort(key=lambda row: (-row[1][0], -row[1][1], row[2]))
        ordered_groups = sorted(groups.values(), key=lambda group: (-group[0][1][0], -group[0][1][1], group[0][2]))
        # Try each entrance before spending another search on its pole variants.
        ordered = [(group[index][0], group[index][1]) for index in range(max(map(len, ordered_groups)))
                   for group in ordered_groups if index < len(group)]
        return ordered, score(previous) if previous else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return fallback
