"""Bounded, normally built oil wells feeding the original crude output bus."""
from collections import Counter
from copy import deepcopy
import json
import math

from .deterministic_fluids import _report
from .factory_templates import DIRECTIONS


PRIMARY = "raw:crude-oil"
PREFIX = PRIMARY + ":capacity:"
MAX_WELLS = 16
SEARCH_RADIUS = 128
MAX_CANDIDATES = 24
SURVEY_LIMIT = 256


def _machine(plan):
    machines = [e for e in plan.get("entities", []) if e["name"] == "pumpjack"]
    if len(machines) != 1:
        raise ValueError("oil capacity cell requires exactly one pumpjack")
    machine = machines[0]
    position = machine["position"]
    if any(type(position.get(axis)) not in (float, int) or not math.isfinite(position[axis]) for axis in ("x", "y")):
        raise ValueError("oil capacity cell has invalid well coordinates")
    if machine.get("direction", 0) not in DIRECTIONS:
        raise ValueError("oil capacity cell requires cardinal pumpjack geometry")
    return machine


def _point(position):
    return position["x"], position["y"]


def _outlet(plan):
    ports = [p for p in plan.get("ports", []) if p.get("kind") == "fluid"
             and p.get("item") == "crude-oil" and p.get("direction") == "output"]
    if len(ports) != 1:
        raise ValueError("oil capacity cell requires one typed crude output")
    return ports[0]


def _survey(fluids, observation, plans, *, discover=False):
    rows = [{"key": key, **_machine(plan)} for key, plan in plans]
    payload = json.dumps(json.dumps({"rows": rows, "discover": discover,
        "radius": SEARCH_RADIUS, "limit": SURVEY_LIMIT, "candidates": MAX_CANDIDATES}, separators=(",", ":")))
    result = fluids.game.query('''
--[[ oil_well_capacity: read live yield, never set resource amounts or bonuses. ]]
local args=helpers.json_to_table(''' + payload + ''');local owned={};local used={};local candidates={}
local pump=prototypes.entity.pumpjack
if not pump then return {ok=false,reason="live pumpjack prototype missing"} end
local function capacity(well,e)
 local proto=well.prototype;local mine=proto.mineable_properties;local normal=proto.normal_resource_amount
 if not proto.infinite_resource or not normal or normal<=0 or not mine or not mine.mining_time or mine.mining_time<=0 then return nil end
 local product=0
 for _,row in pairs(mine.products or {}) do
  if row.type=="fluid" and row.name=="crude-oil" then
   if not row.amount or (row.probability and row.probability~=1) then return nil end
   product=product+row.amount
  end
 end
 local speed=(e and e.prototype or pump).mining_speed
 if e then speed=speed*math.max(0,1+math.min(0,e.speed_bonus))*math.max(0,1+math.min(0,e.productivity_bonus)) end
 return speed*60/mine.mining_time*product*well.amount/normal
end
local function identity(p) return p.x..":"..p.y end
for _,row in ipairs(args.rows) do
 local id=identity(row.position)
 if used[id] then return {ok=false,reason="duplicate saved oil well"} end
 used[id]=true
 local wells=s.find_entities_filtered{position=row.position,radius=0.1,name="crude-oil"}
 if #wells~=1 then return {ok=false,reason="saved pumpjack has no unique live crude well"} end
 local e=target(row.position,"pumpjack")
 if e and (e.force~=f or e.direction~=(row.direction or 0)) then return {ok=false,reason="saved oil well has a foreign or rotated pumpjack"} end
 local rate=capacity(wells[1],e)
 if not rate or rate<=0 then return {ok=false,reason="unsupported live oil yield geometry"} end
 owned[#owned+1]={key=row.key,position=pos(wells[1].position),built=e~=nil,unit_number=e and e.unit_number,
  resource_amount=wells[1].amount,nominal_capacity_per_minute=rate}
end
local scanned=0;local truncated=false
if args.discover then
 local origin=args.rows[1].position
 local wells=s.find_entities_filtered{position=origin,radius=args.radius,name="crude-oil",limit=args.limit+1}
 scanned=#wells;truncated=#wells>args.limit
 for i=1,math.min(#wells,args.limit) do
  local well=wells[i];local p=well.position;local id=identity(p)
  if not used[id] and f.is_chunk_charted(s,{math.floor(p.x/32),math.floor(p.y/32)})
   and #s.find_entities_filtered{position=p,radius=0.1,name="pumpjack"}==0 then
   local exact=s.find_entities_filtered{position=p,radius=0.1,name="crude-oil"}
   local rate=capacity(well)
   if #exact==1 and rate and rate>0 then
    used[id]=true;candidates[#candidates+1]={position=pos(p),resource_amount=well.amount,nominal_capacity_per_minute=rate}
   end
  end
 end
 table.sort(candidates,function(a,b)
  local da=(a.position.x-origin.x)^2+(a.position.y-origin.y)^2;local db=(b.position.x-origin.x)^2+(b.position.y-origin.y)^2
  if da~=db then return da<db end
  if a.position.x~=b.position.x then return a.position.x<b.position.x end
  return a.position.y<b.position.y
 end)
 if #candidates>args.candidates then truncated=true end
 while #candidates>args.candidates do table.remove(candidates) end
end
return {ok=true,world_id=d and d.world_id,owned=owned,candidates=candidates,scanned=scanned,search_truncated=truncated}
''')
    if not result.get("ok") or result.get("world_id") != observation["world_id"]:
        raise ValueError(result.get("reason", "oil capacity survey world mismatch"))
    # Factorio encodes an empty Lua sequence as {}, including normal surveys
    # without discovery and a patch with no remaining candidate wells.
    for field, maximum in (("owned", MAX_WELLS), ("candidates", MAX_CANDIDATES)):
        if result.get(field) == {}:
            result[field] = []
        if not isinstance(result.get(field), list) or len(result[field]) > maximum:
            raise ValueError("oil capacity survey exceeded its bounded row contract")
    expected = {key: _point(_machine(plan)["position"]) for key, plan in plans}
    seen = set()
    for row in result.get("owned", []):
        key = row.get("key")
        if key not in expected or key in seen or _point(row["position"]) != expected[key] or type(row.get("built")) is not bool:
            raise ValueError("oil capacity survey did not match unique saved wells")
        seen.add(key)
    if seen != set(expected):
        raise ValueError("oil capacity survey omitted a saved well")
    positions = set(expected.values())
    for row in result.get("owned", []) + result.get("candidates", []):
        rate = row.get("nominal_capacity_per_minute")
        if type(rate) not in (float, int) or not math.isfinite(rate) or rate <= 0:
            raise ValueError("live oil nominal capacity is unavailable")
    for row in result.get("candidates", []):
        p = _point(row["position"])
        if p in positions or not all(type(v) in (float, int) and math.isfinite(v) for v in p):
            raise ValueError("oil survey returned duplicate or invalid candidate wells")
        positions.add(p)
    return result


def _candidate_plan(fluids, observation, well, primary):
    """Reserve no well until a paid, typed pipe route fits with its entire cell."""
    destination = _outlet(primary)
    reserved = fluids.factory._reserved()
    approaches = [{"name": "reserved-port-approach", "position": {"x": x, "y": y}}
                  for x, y in fluids.factory._port_clearances()]
    occupied = fluids.builder._occupied_by_plan(reserved + approaches)
    taps = None
    for direction in DIRECTIONS:
        plan = fluids._utility_plan("pumpjack", well["position"], direction, "crude-oil")
        for entity in plan["entities"]:
            if entity["name"] == "pipe":
                entity["_fluid"] = "crude-oil"
        if fluids.builder._occupied_by_plan(plan["entities"]) & occupied:
            continue
        if not fluids.builder.can_place(plan["entities"]).get("ok"):
            continue
        source = _outlet(plan)
        obstacles = reserved + approaches + plan["entities"]
        obstacles += fluids._machine_connection_obstacles(observation.get("entities", []) + obstacles)
        route = fluids.builder.route(source["position"], destination["position"], "pipe", obstacles)
        if not route.get("ok"):
            if taps is None:
                taps = fluids._network_taps(destination, destination).get("taps", [])
            for tap in sorted(taps, key=lambda p: math.dist(_point(source["position"]), _point(p)))[:8]:
                route = fluids.builder.route(source["position"], tap, "pipe", obstacles)
                if route.get("ok"):
                    break
        if not route.get("ok"):
            continue
        pipes = [{"name": "pipe", "position": p, "direction": 0, "_fluid": "crude-oil"} for p in route["path"]]
        by_position = {(e["name"], *_point(e["position"])): e for e in plan["entities"] + pipes}
        plan["entities"] = list(by_position.values())
        if not fluids.builder.can_place(plan["entities"]).get("ok"):
            continue
        plan["required_items"] = dict(Counter(e["name"] for e in plan["entities"]))
        plan["oil_merge"] = {"ok": True, "entities": pipes, "ports": []}
        return plan
    return None


def ensure_oil_capacity(fluids, observation, rate, result):
    primary = fluids.state["sources"].get(PRIMARY, {})
    evidence = {"fluid": "crude-oil", "requested_rate_per_minute": rate,
                "throughput_verified": False, "raw_source_capacity_verified": False,
                "search_radius": SEARCH_RADIUS, "maximum_wells": MAX_WELLS}
    def blocked(reason, **details):
        return fluids._decorate(_report("blocked", reason, **(evidence | details)), primary)
    try:
        _machine(primary)
        _outlet(primary)
        # register_plan writes first. Recover only this helper's exact fixed
        # cells if a crash occurred before the fluid checkpoint's atomic save.
        fluids.factory._sync(observation)
        for key, plan in fluids.factory.state.get("blocks", {}).items():
            own = key.removeprefix("fluid:")
            if own.startswith(PREFIX) and own not in fluids.state["sources"]:
                if plan.get("oil_capacity_key") != own or not plan.get("oil_merge"):
                    return blocked("unrecognized saved oil capacity reservation")
                fluids.state["sources"][own] = deepcopy(plan)
                fluids._save()
        plans = [(PRIMARY, primary)] + sorted((key, plan) for key, plan in fluids.state["sources"].items() if key.startswith(PREFIX))
        if len(plans) > MAX_WELLS or len({_point(_machine(p)["position"]) for _, p in plans}) != len(plans):
            return blocked("saved oil capacity exceeds unique well bound")
        survey = _survey(fluids, observation, plans)
        capacity = sum(row["nominal_capacity_per_minute"] for row in survey["owned"])
        evidence.update(nominal_capacity_per_minute=capacity, wells_reserved=len(plans),
                        wells_observed=sum(row["built"] for row in survey["owned"]))
        # Never cache completion: reconstruction, power and actual segment
        # checks run for all retained cells even when demand falls or ticks roll back.
        for key, plan in plans:
            if key != PRIMARY:
                if plan.get("oil_capacity_key") != key or not plan.get("oil_merge"):
                    return blocked("saved oil capacity cell is missing its reserved merge")
                restored = fluids.factory.register_plan("fluid:" + key, plan, observation)
                if not restored.get("ok"):
                    return blocked(restored.get("reason", "oil capacity reservation conflict"))
            built = fluids.builder.ensure_plan(observation, plan)
            if built.get("status") != "succeeded":
                return fluids._decorate(built, primary)
            powered = fluids.factory.ensure_power_connection(observation, "fluid:" + key, plan)
            if powered.get("status") != "succeeded":
                return fluids._decorate(powered, primary)
            if key != PRIMARY:
                link_key = key + ":output:crude-oil"
                if link_key not in fluids.state["links"]:
                    fluids.state["links"][link_key] = deepcopy(plan["oil_merge"])
                    fluids._save()
                connected = fluids._connect_pipe(observation, _outlet(plan), _outlet(primary), link_key, plan)
                if connected.get("status") != "succeeded":
                    return fluids._decorate(connected, primary)
        if not all(row["built"] for row in survey["owned"]):
            return blocked("oil capacity construction awaits live pumpjack observation")
        if capacity + 1e-9 >= rate:
            return {**result, "evidence": {**result.get("evidence", {}), **evidence, "raw_source_capacity_verified": True}}
        if len(plans) == MAX_WELLS:
            return blocked("bounded oil well capacity is insufficient")
        survey = _survey(fluids, observation, plans, discover=True)
        evidence.update(search_truncated=survey["search_truncated"], candidates_observed=len(survey["candidates"]))
        index = next(i for i in range(1, MAX_WELLS) if PREFIX + str(i) not in fluids.state["sources"])
        key = PREFIX + str(index)
        for well in survey["candidates"]:
            plan = _candidate_plan(fluids, observation, well, primary)
            if plan is None:
                continue
            plan["oil_capacity_key"] = key
            registered = fluids.factory.register_plan("fluid:" + key, plan, observation)
            if not registered.get("ok"):
                continue
            fluids.state["sources"][key] = registered
            fluids._save()
            # The normal builder supplies pumpjack/steel, poles and every pipe;
            # no projected candidate yield is credited as constructed capacity.
            built = fluids.builder.ensure_plan(observation, registered)
            if built.get("status") != "succeeded":
                return fluids._decorate(built, primary)
            return fluids._decorate(_report("waiting", "reobserve newly reserved oil capacity cell", **evidence), primary)
        return blocked("insufficient reachable unoccupied oil wells in bounded discovery")
    except (ValueError, KeyError, TypeError) as exc:
        return blocked(str(exc))
