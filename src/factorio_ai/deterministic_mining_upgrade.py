"""Replace an exhausted owned raw drill while preserving its connected receiver.

Planning is read-only. The replacement is crafted before ordinary mining, and
normal placement plus live output/power observations remain mandatory afterward.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from typing import Any

from .factory_templates import DIRECTIONS


def _report(status: str, reason: str, **evidence: Any) -> dict:
    return {"status": status, "reason": reason, "evidence": evidence}


def _protected(factory: Any, drill: dict) -> bool:
    if factory.builder.owns_automated_burner(drill):
        return True
    key = factory._entity_key(drill)
    return any(factory._entity_key(entity) == key
               for name, plan in factory.state.get("blocks", {}).items() if name.startswith("energy:")
               for entity in plan.get("entities", []))


def _directed_belt_tail(entities: list[dict], source: dict, destination: dict) -> bool:
    belts = {(e["position"]["x"], e["position"]["y"]): e for e in entities if e["name"] == "transport-belt"}
    if len(belts) != len(entities) or not belts:
        return False
    point = source["position"]["x"], source["position"]["y"]
    end = destination["position"]["x"], destination["position"]["y"]
    if belts.get(point, {}).get("direction") != source.get("facing"):
        return False
    seen = set()
    while point in belts and point not in seen:
        seen.add(point)
        facing = belts[point].get("direction")
        if point == end:
            return facing == destination.get("facing") and len(seen) == len(belts)
        if facing not in DIRECTIONS:
            return False
        dx, dy = DIRECTIONS[facing]
        point = point[0] + dx, point[1] + dy
    return False


def _legacy_absent_source(factory: Any, observation: dict, item: str, resource: str,
                          pending: dict | None = None) -> dict | None:
    """Recover intended construction ownership from an exact legacy fuel link.

    A completed automatic-source flag is deliberately unnecessary: old workers
    could mine an exhausted starter before finishing its already reserved intake.
    No proximity-based association or authority to mine an old drill is created.
    """
    key = "source:" + item
    primary = factory.state["blocks"][key]
    receiver = primary.get("source_receiver", {})
    ports = primary.get("ports", [])
    extractors = [e for e in primary.get("entities", []) if e["name"] == "inserter"]
    if (not receiver.get("position") or len(ports) != 1 or ports[0].get("item") != item
            or len(extractors) != 1 or (primary.get("active_source") and not pending)):
        return None
    extractor = extractors[0]
    if extractor.get("direction") not in DIRECTIONS or ports[0].get("direction") != "output":
        return None
    dx, dy = DIRECTIONS[extractor["direction"]]
    outlet = {"position": {"x": extractor["position"]["x"] - dx, "y": extractor["position"]["y"] - dy},
              "facing": (extractor["direction"] + 8) % 16}
    if not _directed_belt_tail([e for e in primary["entities"] if e["name"] == "transport-belt"], outlet, ports[0]):
        return None
    candidates = []
    for fuel_key, plan in factory.state["blocks"].items():
        if not fuel_key.startswith("fuel:burner-mining-drill:"):
            continue
        try:
            x, y = map(float, fuel_key.rsplit(":", 1)[1].split(","))
        except ValueError:
            continue
        if not all(math.isfinite(v) and v.is_integer() for v in (x, y)):
            continue
        old = {"name": "burner-mining-drill", "position": {"x": x, "y": y}}
        link = factory.state.get("links", {}).get(fuel_key, {})
        intake = plan.get("ports", [])
        arms = [e for e in plan.get("entities", []) if e["name"] == "inserter"]
        if not arms and pending:
            arms = plan.get("retired_inserters", [])
        if (_protected(factory, old) or len(arms) != 1 or len(intake) != 1
                or intake[0].get("item") != "coal" or link.get("source_port") != ports[0]
                or link.get("consumer_port") != intake[0]
                or not _directed_belt_tail(link.get("entities", []), ports[0], intake[0])):
            continue
        arm = {k: deepcopy(arms[0][k]) for k in ("name", "position", "direction")}
        if arm["direction"] not in DIRECTIONS:
            continue
        dx, dy = DIRECTIONS[arm["direction"]]
        pickup = {"position": {"x": arm["position"]["x"] + dx, "y": arm["position"]["y"] + dy},
                  "facing": (arm["direction"] + 8) % 16}
        if not _directed_belt_tail([e for e in plan["entities"] if e["name"] == "transport-belt"], intake[0], pickup):
            continue
        if any(factory._entity_key(e) == factory._entity_key(arm)
               for other_key, other in factory.state["blocks"].items() if other_key != fuel_key
               for e in other.get("entities", [])):
            continue
        provenance = {"fuel_key": fuel_key, "receiver": receiver, "old_position": old["position"],
            "source_entities": primary["entities"], "source_port": ports[0], "intake_port": intake[0],
            "fuel_link_entities": link["entities"], "arm": arm}
        if pending and provenance != pending.get("legacy_provenance"):
            continue
        candidates.append((old, arm, provenance))
    if len(candidates) != 1:
        return None
    old, arm, provenance = candidates[0]
    payload = json.dumps(json.dumps({"old": old, "receiver": receiver, "extractor": extractors[0], "arm": arm}))
    proof = factory.game.query('''
--[[ legacy_source_provenance: no entities or input state are changed. ]]
local args=helpers.json_to_table(''' + payload + ''')
local receiver=target(args.receiver.position,args.receiver.name)
local extractor=target(args.extractor.position,args.extractor.name)
local arm=target(args.arm.position,args.arm.name)
if target(args.old.position,args.old.name) then return {ok=false,reason="legacy old drill reappeared"} end
if not receiver or receiver.force~=f or not extractor or extractor.force~=f
 or extractor.direction~=args.extractor.direction then return {ok=false,reason="legacy extraction identity changed"} end
local function inside(p,e)
 return math.abs(p.x-e.position.x)<e.prototype.tile_width/2 and math.abs(p.y-e.position.y)<e.prototype.tile_height/2
end
if not inside(extractor.pickup_position,receiver) then return {ok=false,reason="legacy extractor does not pick from receiver"} end
local proto=prototypes.entity[args.old.name];local v=proto.vector_to_place_result
if not v then return {ok=false,reason="legacy drill has no solid output geometry"} end
local directions={};local x,y=v.x or v[1],v.y or v[2]
for _,direction in ipairs{0,4,8,12} do
 if inside({x=args.old.position.x+x,y=args.old.position.y+y},receiver) then directions[#directions+1]=direction end
 x,y=-y,x
end
if #directions~=1 then return {ok=false,reason="legacy drill output orientation is ambiguous"} end
return {ok=true,world_id=d and d.world_id,direction=directions[1],receiver_unit=receiver.unit_number,
 extractor_unit=extractor.unit_number,arm_present=arm~=nil,arm_unit=arm and arm.unit_number}
''')
    if not proof.get("ok") or proof.get("world_id") != observation["world_id"]:
        return None
    if pending:
        if (proof.get("receiver_unit") != pending.get("legacy_receiver_unit")
                or proof.get("extractor_unit") != pending.get("legacy_extractor_unit")
                or proof["direction"] != pending["old_drill"]["direction"]):
            return None
    elif not proof.get("arm_present"):
        return None
    old["direction"] = proof["direction"]
    record = {"old_drill": old, "receiver": deepcopy(receiver), "extraction_block": key, "resource": resource,
              "created_tick": observation.get("tick", 0), "legacy_absent_old": True,
              "legacy_provenance": deepcopy(provenance), "legacy_receiver_unit": proof["receiver_unit"],
              "legacy_extractor_unit": proof["extractor_unit"]}
    # This survey proves the sole planned arm really drops into the absent old
    # drill footprint. Its exact unit is then required by ordinary mining guards.
    intake_proof = _fuel_intake_survey(factory, record, [arm])
    rows = intake_proof.get("inserters") or []
    if (not intake_proof.get("ok") or intake_proof.get("world_id") != observation["world_id"] or len(rows) != 1):
        return None
    live = rows[0]
    expected = pending["fuel_retirement"]["inserters"][0].get("unit_number") if pending else proof.get("arm_unit")
    if live.get("present") and (not live.get("owned") or not live.get("inserter") or not live.get("feeds_old_drill")
            or live.get("direction") != arm["direction"] or live.get("unit_number") != expected):
        return None
    if not pending and not live.get("present"):
        return None
    arm["unit_number"] = expected
    record["fuel_retirement"] = {"plan_key": provenance["fuel_key"], "inserters": [arm], "retired": False}
    return record


def _survey(factory: Any, record: dict, *, choose: bool = False) -> dict:
    payload = json.dumps(json.dumps({**record, "choose": choose}, separators=(",", ":")))
    return factory.game.query('''
local args=helpers.json_to_table(''' + payload + ''')
local proto=prototypes.entity["electric-mining-drill"]
local receiver=target(args.receiver.position,args.receiver.name)
if not receiver or receiver.force~=f then return {ok=false,reason="upgrade receiver is missing or foreign"} end
local old=target(args.old_drill.position,args.old_drill.name)
local ignored={}
if args.legacy_absent_old and not old and args.fuel_retirement then
 for _,row in ipairs(args.fuel_retirement.inserters) do
  local e=target(row.position,row.name)
  if e and e.force==f and e.minable and e.unit_number==row.unit_number and e.direction==row.direction then ignored[e.unit_number]=true end
 end
end
local function feeds(e)
 local p=e.drop_position
 return math.abs(p.x-receiver.position.x)<receiver.prototype.tile_width/2
  and math.abs(p.y-receiver.position.y)<receiver.prototype.tile_height/2
end
local function resources(p,r)
 local amount=0;local mixed=false;local all=0
 for _,ore in pairs(s.find_entities_filtered{area={{p.x-r,p.y-r},{p.x+r,p.y+r}},type="resource"}) do
  if math.abs(ore.position.x-p.x)<=r and math.abs(ore.position.y-p.y)<=r and ore.amount>0 then
   all=all+ore.amount
   if ore.name==args.resource then amount=amount+ore.amount else mixed=true end
  end
 end
 return amount,mixed,all
end
local old_info={present=old~=nil}
if old then
 local _,_,remaining=resources(old.position,old.prototype.mining_drill_radius)
 old_info={present=true,unit_number=old.unit_number,remaining=remaining,feeds_receiver=feeds(old),
  owned=old.force==f and old.burner~=nil and old.minable,
  exhausted=old.status==defines.entity_status.no_minable_resources}
end
local networks={};local poles={}
for _,generator in pairs(s.find_entities_filtered{force=f,type="generator"}) do
 if generator.electric_network_id then networks[generator.electric_network_id]=true end
end
for _,pole in pairs(s.find_entities_filtered{force=f,type="electric-pole"}) do
 if networks[pole.electric_network_id] then
  poles[#poles+1]={name=pole.name,position=pos(pole.position),reach=pole.prototype.get_supply_area_distance(pole.quality)}
 end
end
local function rotate(x,y,direction)
 if direction==4 then return -y,x elseif direction==8 then return -x,-y elseif direction==12 then return y,-x end
 return x,y
end
local vector=proto.vector_to_place_result
if not vector then return {ok=false,reason="replacement has no solid output geometry"} end
local function candidate(p,direction)
 local vx,vy=rotate(vector.x or vector[1],vector.y or vector[2],direction)
 if math.abs(p.x+vx-receiver.position.x)>=receiver.prototype.tile_width/2
  or math.abs(p.y+vy-receiver.position.y)>=receiver.prototype.tile_height/2 then return nil end
 local amount,mixed=resources(p,proto.mining_drill_radius)
 local box=proto.collision_box
 local x1,y1=rotate(box.left_top.x,box.left_top.y,direction)
 local x2,y2=rotate(box.right_bottom.x,box.right_bottom.y,direction)
 local left,top=p.x+math.min(x1,x2),p.y+math.min(y1,y2)
 local right,bottom=p.x+math.max(x1,x2),p.y+math.max(y1,y2)
local actual=target(p,proto.name);local blocked=false;local ground_items={};local own_actor=false
 for _,e in pairs(s.find_entities_filtered{area={{left,top},{right,bottom}}}) do
  if e.type~="resource" and e~=old and e~=actual and not ignored[e.unit_number] then
   if e==a then own_actor=true
   elseif e.type=="item-entity" and e.stack and e.stack.valid_for_read then
    ground_items[#ground_items+1]={name=e.name,position=pos(e.position),item=e.stack.name,
     quality=e.stack.quality.name,count=e.stack.count}
   else blocked=true end
  end
 end
 local terrain=true
 for x=math.floor(left),math.ceil(right)-1 do for y=math.floor(top),math.ceil(bottom)-1 do
  for layer in pairs(proto.collision_mask.layers) do
   if s.get_tile(x,y).collides_with(layer) then terrain=false end
  end
 end end
 local power=nil
 for _,pole in ipairs(poles) do
  if math.abs(p.x-pole.position.x)<=pole.reach and math.abs(p.y-pole.position.y)<=pole.reach then power=pole;break end
 end
 return {drill={name=proto.name,position=p,direction=direction,_width=proto.tile_width,_height=proto.tile_height},
  remaining=amount,mixed=mixed,terrain_clear=terrain,blocked=blocked,ground_items=ground_items,power=power,
  can_place=s.can_place_entity{name=proto.name,position=p,direction=direction,force=f},
  actor_only=own_actor and not blocked and #ground_items==0 and s.can_place_entity{name=proto.name,position=p,
   direction=direction,force=f,build_check_type=defines.build_check_type.script,forced=false},
  actual=actual and {unit_number=actual.unit_number,owned=actual.force==f,direction=actual.direction,
   feeds_receiver=feeds(actual),powered=networks[actual.electric_network_id] and actual.energy>0 or false} or nil}
end
local candidates={}
if args.choose then
 for _,direction in ipairs{0,4,8,12} do
  local vx,vy=rotate(vector.x or vector[1],vector.y or vector[2],direction)
  local w,h=proto.tile_width,proto.tile_height;if direction==4 or direction==12 then w,h=h,w end
  local rx,ry=receiver.prototype.tile_width/2,receiver.prototype.tile_height/2
  for x=math.floor(receiver.position.x-vx-rx),math.ceil(receiver.position.x-vx+rx) do
   for y=math.floor(receiver.position.y-vy-ry),math.ceil(receiver.position.y-vy+ry) do
    local row=candidate({x=x+(w%2)/2,y=y+(h%2)/2},direction)
    if row then candidates[#candidates+1]=row end
   end
  end
 end
else
 local row=candidate(args.drill.position,args.drill.direction or 0)
 if row then candidates[1]=row end
end
return {ok=true,world_id=d and d.world_id,tick=game.tick,old=old_info,candidates=candidates}
''')


def _fuel_intake_survey(factory: Any, record: dict, inserters: list[dict]) -> dict:
    payload = json.dumps(json.dumps({"old_drill": record["old_drill"], "inserters": inserters}, separators=(",", ":")))
    return factory.game.query('''
local args=helpers.json_to_table(''' + payload + ''');local old=args.old_drill
local box=prototypes.entity[old.name].collision_box
local function rotate(x,y)
 local direction=old.direction or 0
 if direction==4 then return -y,x elseif direction==8 then return -x,-y elseif direction==12 then return y,-x end
 return x,y
end
local x1,y1=rotate(box.left_top.x,box.left_top.y)
local x2,y2=rotate(box.right_bottom.x,box.right_bottom.y)
local left,right=old.position.x+math.min(x1,x2),old.position.x+math.max(x1,x2)
local top,bottom=old.position.y+math.min(y1,y2),old.position.y+math.max(y1,y2)
local rows={}
for _,planned in ipairs(args.inserters) do
 local e=target(planned.position,planned.name);local row={present=e~=nil}
 if e then
  local drop=e.type=="inserter" and e.drop_position or nil
  row={present=true,unit_number=e.unit_number,owned=e.force==f and e.minable,
   direction=e.direction,inserter=e.type=="inserter",
   feeds_old_drill=drop and drop.x>=left and drop.x<=right and drop.y>=top and drop.y<=bottom or false,
   powered=e.type=="inserter" and e.energy>0 and e.is_connected_to_electric_network() or false}
 end
 rows[#rows+1]=row
end
return {ok=true,world_id=d and d.world_id,inserters=rows}
''')


def _retire_fuel_intake(factory: Any, observation: dict, item: str, record: dict) -> dict | None:
    key = "fuel:" + factory._entity_key(record["old_drill"])
    plan = factory.state.get("blocks", {}).get(key)
    retirement = record.get("fuel_retirement")
    if plan is None:
        return _report("blocked", "pending old fuel intake plan disappeared") if retirement else None
    if retirement is None:
        # The raw burner intake template owns exactly one arm. Do not infer
        # ownership from nearby inserters or broaden this to receiver extractors.
        inserters = [deepcopy(entity) for entity in plan.get("entities", []) if entity["name"] == "inserter"]
        if len(inserters) != 1:
            return _report("blocked", "old raw fuel intake has unsupported inserter ownership", fuel_plan=key)
        retirement = {"plan_key": key, "inserters": inserters}
    inserters = retirement["inserters"]
    identities = {factory._entity_key(entity) for entity in inserters}
    if (retirement.get("plan_key") != key or len(inserters) != 1
            or any(factory._entity_key(entity) in identities
                   for other_key, other in factory.state.get("blocks", {}).items() if other_key != key
                   for entity in other.get("entities", []))):
        return _report("blocked", "old fuel inserter is shared with protected factory infrastructure")
    proof = _fuel_intake_survey(factory, record, inserters)
    rows = proof.get("inserters") or []
    if not proof.get("ok") or proof.get("world_id") != observation["world_id"] or len(rows) != len(inserters):
        return _report("blocked", "cannot prove old raw fuel intake retirement", query_error=proof.get("reason"))
    for planned, live in zip(inserters, rows):
        if not live.get("present"):
            continue
        if (not live.get("owned") or not live.get("inserter") or not live.get("feeds_old_drill")
                or live.get("direction") != planned.get("direction", 0) or not live.get("unit_number")
                or planned.get("unit_number", live["unit_number"]) != live["unit_number"]):
            return _report("blocked", "old fuel inserter identity or drop no longer matches its owned drill")
        planned.update(unit_number=live["unit_number"], powered=live.get("powered", False))
        retirement["retired"] = False
        record["fuel_retirement"] = retirement
        factory._save()
        return {"type": "mine", "name": planned["name"], "position": planned["position"], "count": 1,
                "expected_entity_unit": live["unit_number"], "expected_entity_world_id": observation["world_id"],
                "reason": "retire the proven obsolete raw drill fuel inserter before electric replacement"}
    if not retirement.get("retired") or plan.get("retired_for_upgrade") != item:
        retirement["retired"] = True
        record["fuel_retirement"] = retirement
        plan["retired_for_upgrade"] = item
        plan["retired_inserters"] = deepcopy(inserters)
        plan["entities"] = [entity for entity in plan["entities"] if factory._entity_key(entity) not in identities]
        factory._save()
    return None


def ensure_source_upgrade(factory: Any, observation: dict, item: str, resource: str) -> dict | None:
    """Return one upgrade action/status, or None when ordinary sourcing applies."""
    primary = factory.state.get("blocks", {}).get("source:" + item)
    if not primary:
        return None
    record = factory.state.get("source_upgrades", {}).get(item)
    association = primary.get("active_source") or {}
    if record is not None and record.get("state") == "retired":
        # Once another connected raw source takes over, the old reservation is
        # history. The still-existing drill remains a real placement obstacle.
        if association.get("receiver") != record["receiver"]:
            del factory.state["source_upgrades"][item]
            factory._save()
            record = None
    if record is None:
        old = association.get("drill") or {}
        if not observation.get("enabled_recipes", {}).get("electric-mining-drill"):
            return None
        if not association:
            record = _legacy_absent_source(factory, observation, item, resource)
            if record is None:
                return None
        else:
            if (old.get("name") != "burner-mining-drill" or not association.get("receiver")
                    or not factory.owns_automated_burner(old) or _protected(factory, old)):
                return None
            record = {"old_drill": deepcopy(old), "receiver": deepcopy(association["receiver"]),
                      "extraction_block": association["extraction_block"], "resource": resource,
                      "created_tick": observation.get("tick", 0)}
        survey = _survey(factory, record, choose=True)
        if not survey.get("ok"):
            return _report("blocked", "cannot survey established raw drill upgrade", query_error=survey.get("reason"))
        if survey.get("world_id") != observation["world_id"]:
            return _report("blocked", "raw drill upgrade survey belongs to another world")
        live = survey.get("old", {})
        if record.get("legacy_absent_old"):
            if live.get("present"):
                return None
        elif (not live.get("present")
                or not live.get("owned") or not live.get("exhausted") or live.get("remaining") != 0
                or not live.get("feeds_receiver") or not live.get("unit_number")):
            return None
        reserved_entities = factory._reserved()
        if record.get("legacy_absent_old"):
            fuel_key = record["fuel_retirement"]["plan_key"]
            arm_key = factory._entity_key(record["fuel_retirement"]["inserters"][0])
            # The dead intake's unfinished link will never be constructed.
            # Real belts still block the read-only footprint survey normally.
            reserved_entities = [e for category in ("blocks", "links", "power_links", "source_upgrades")
                for key, plan in factory.state.get(category, {}).items()
                if not (category == "links" and (key == fuel_key
                    or factory.state.get("blocks", {}).get(key, {}).get("retired_for_upgrade")))
                for e in plan.get("entities", [])
                if not (category == "blocks" and key == fuel_key and factory._entity_key(e) == arm_key)]
        reserved = factory.builder._occupied_by_plan(reserved_entities) | factory._port_clearances()
        candidates = [row for row in survey.get("candidates", [])
                      if row.get("remaining", 0) > 0 and not row.get("mixed") and row.get("terrain_clear")
                      and not row.get("blocked") and row.get("power") and not row.get("actual")
                      and not factory.builder._occupied_by_plan([row["drill"]]) & reserved]
        if not candidates:
            return None
        candidates.sort(key=lambda row: (-row["remaining"], row["drill"]["direction"],
                                          row["drill"]["position"]["x"], row["drill"]["position"]["y"]))
        chosen = candidates[0]
        record.update(old_unit_number=live.get("unit_number"), drill=chosen["drill"],
                      entities=[chosen["drill"]], ok=True, ports=[], state="reserved")
        factory.state.setdefault("source_upgrades", {})[item] = record
        factory._save()
    if record.get("legacy_absent_old"):
        if _legacy_absent_source(factory, observation, item, resource, record) is None:
            return _report("blocked", "legacy absent source construction provenance changed", item=item)
        if not association:
            association = {"receiver": record["receiver"], "extraction_block": record["extraction_block"],
                           "drill": record["old_drill"]}
    if (association.get("receiver") != record["receiver"]
            or association.get("extraction_block") != record["extraction_block"]
            or not association.get("drill", {}).get("position")
            or factory._entity_key(association["drill"])
            not in {factory._entity_key(record["old_drill"]), factory._entity_key(record["drill"])}
            or _protected(factory, record["old_drill"])):
        return _report("blocked", "pending drill upgrade no longer owns its raw source association", item=item)
    # Always reconcile the live unit and footprint, including after restart or
    # rollback. Persisted completion never substitutes for an observed machine.
    survey = _survey(factory, record)
    if not survey.get("ok") or survey.get("world_id") != observation["world_id"]:
        return _report("blocked", "cannot reconcile pending drill upgrade in this world", query_error=survey.get("reason"))
    rows = survey.get("candidates") or []
    if not rows:
        return _report("blocked", "replacement output no longer fits the established receiver")
    row, old = rows[0], survey.get("old", {})
    if record.get("legacy_absent_old") and old.get("present"):
        return _report("blocked", "legacy old drill reappeared without a preserved unit identity", item=item)
    if old.get("present") and (old.get("unit_number") != record["old_unit_number"] or not old.get("owned")
            or not old.get("exhausted") or old.get("remaining") != 0 or not old.get("feeds_receiver")):
        return _report("blocked", "old drill is no longer the proven exhausted raw source", item=item)
    actual = row.get("actual")
    if actual:
        if not actual.get("owned") or actual.get("direction") != record["drill"].get("direction", 0):
            return _report("blocked", "replacement drill identity or direction conflicts")
        if not actual.get("feeds_receiver") or row.get("mixed"):
            return _report("blocked", "replacement drill output or resource purity is unverified")
        if row.get("remaining", 0) <= 0:
            if record.get("state") != "retired":
                record["state"] = "retired"
                factory._save()
            return None
        if not actual.get("powered"):
            return _report("waiting", "waiting for observed replacement drill power", item=item)
        retirement = _retire_fuel_intake(factory, observation, item, record)
        if retirement is not None:
            return retirement
        if record.get("state") != "observed" or record.get("observed_unit_number") != actual["unit_number"]:
            record.update(state="observed", observed_tick=survey["tick"], observed_unit_number=actual["unit_number"])
            factory._save()
        return _report("succeeded", "electric drill power, resources and established receiver observed",
                       receiver=record["receiver"], drill=record["drill"], remaining=row["remaining"], flow_verified=False)
    if (row.get("remaining", 0) <= 0 or row.get("mixed") or not row.get("terrain_clear")
            or row.get("blocked") or not row.get("power")):
        return _report("blocked", "reserved drill upgrade footprint, pure resources or power is unavailable", item=item)
    replacement_item = factory.builder._placement_item(record["drill"]["name"])
    if observation.get("inventory", {}).get(replacement_item, 0) < 1:
        return factory.bootstrap.ensure_item(observation, replacement_item, 1)
    retirement = _retire_fuel_intake(factory, observation, item, record)
    if retirement is not None:
        return retirement
    if old.get("present"):
        return {"type": "mine", "name": record["old_drill"]["name"], "position": record["old_drill"]["position"],
                "count": 1, "expected_world_id": observation["world_id"], "expected_unit_number": record["old_unit_number"],
                "exhausted_source_receiver": record["receiver"], "required_replacement_item": replacement_item,
                "reason": "replace exhausted raw drill with a crafted electric drill feeding its existing receiver"}
    ground_items = sorted(row.get("ground_items") or [],
                          key=lambda entity: (entity["position"]["x"], entity["position"]["y"],
                                              entity["item"], entity["quality"]))
    if ground_items:
        dropped = ground_items[0]
        return {"type": "take", "name": dropped["name"], "position": dropped["position"],
                "item": dropped["item"], "quality": dropped["quality"], "count": min(50, dropped["count"]),
                "reason": "collect conserved ground items obstructing the replacement drill footprint"}
    if not row.get("can_place") and not (factory.game.backend == "character" and row.get("actor_only")):
        return _report("blocked", "normal placement rejects the reserved replacement drill", item=item)
    built = factory.builder.ensure_plan(observation, record)
    if built.get("status") == "succeeded" and "type" not in built:
        return _report("waiting", "waiting for live replacement drill output and power observation", item=item)
    return built
