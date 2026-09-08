"""Dedicated paid mining cells for unsmelted ore and a persistent output bus."""
from copy import deepcopy
import json
import math


def _inspect(factory, obs: dict, item: str, plan: dict) -> dict:
    drills = [e for e in plan.get("entities", []) if e["name"] == "electric-mining-drill"]
    receivers = [e for e in plan.get("entities", []) if e["name"] == "wooden-chest"]
    arms = [e for e in plan.get("entities", []) if e["name"] == "inserter"]
    belts = [e for e in plan.get("entities", []) if e["name"] == "transport-belt"]
    ports = plan.get("ports", [])
    if (plan.get("resource_cell") is not True or len(drills) != 1 or len(receivers) != 1 or len(arms) != 1
            or len(belts) != 2 or len(ports) != 1 or ports[0].get("item") != item or ports[0].get("kind") != "item"
            or ports[0].get("direction") != "output"
            or any(e["name"] not in {"electric-mining-drill", "wooden-chest", "inserter", "transport-belt", "small-electric-pole"}
                   for e in plan["entities"])):
        return {"ok": False, "reason": "dedicated raw ore cell ownership is incompatible"}
    actual = {(e["name"], e["position"]["x"], e["position"]["y"]): e for e in obs.get("entities", [])}
    rows = [{**e, "unit_number": actual.get((e["name"], e["position"]["x"], e["position"]["y"]), {}).get("unit_number")}
            for e in [drills[0], receivers[0], arms[0], *belts]]
    payload = json.dumps(json.dumps({"world": obs["world_id"], "item": item, "rows": rows,
                                     "output": ports[0]}, separators=(",", ":")))
    measured = factory.game.query('''
--[[ dedicated_raw_ore: inert pure resource, owned geometry and live power proof. ]]
local x=helpers.json_to_table(''' + payload + ''')
if not d or d.world_id~=x.world then return {ok=false,reason="raw ore world identity changed"} end
local row=x.rows[1];local radius=prototypes.entity[row.name].mining_drill_radius
local remaining=0;local mixed=false
for _,ore in pairs(s.find_entities_filtered{area={{row.position.x-radius,row.position.y-radius},{row.position.x+radius,row.position.y+radius}},type="resource"}) do
 if ore.name==x.item then remaining=remaining+ore.amount elseif ore.amount>0 then mixed=true end
end
if mixed then return {ok=false,reason="dedicated raw ore mining area contains another resource"} end
local live={};local complete=true
for i,wanted in ipairs(x.rows) do
 local e=target(wanted.position,wanted.name)
 if not e or e.force~=f or not wanted.unit_number or wanted.unit_number~=e.unit_number then complete=false
 elseif wanted.name~="wooden-chest" and e.direction~=wanted.direction then return {ok=false,reason="raw ore cell facing changed"}
 else live[i]=e end
end
if not complete then return {ok=true,world_id=d.world_id,remaining=remaining,complete=false} end
local drill,chest,arm,first,last=live[1],live[2],live[3],live[4],live[5]
local function contains(e,p) local b=e.bounding_box;return p.x>=b.left_top.x and p.x<b.right_bottom.x and p.y>=b.left_top.y and p.y<b.right_bottom.y end
if not contains(chest,drill.drop_position) or not contains(chest,arm.pickup_position) or not contains(first,arm.drop_position)
 or last.position.x~=x.output.position.x or last.position.y~=x.output.position.y or last.direction~=x.output.facing then
 return {ok=false,reason="raw ore drill, receiver and output geometry is disconnected"}
end
local connected=false;for _,e in pairs(first.belt_neighbours.outputs) do if e==last then connected=true end end
if not connected then return {ok=false,reason="raw ore output belts are disconnected"} end
for _,stack in pairs(chest.get_inventory(defines.inventory.chest).get_contents()) do
 if stack.name~=x.item and stack.count>0 then return {ok=false,reason="raw ore receiver contains another material"} end
end
local output=0
for _,belt in ipairs({first,last}) do for lane=1,2 do for _,stack in pairs(belt.get_transport_line(lane).get_contents()) do
 if stack.name~=x.item and stack.count>0 then return {ok=false,reason="raw ore output contains another material"} end
 output=output+stack.count
end end end
if arm.held_stack.valid_for_read and arm.held_stack.name~=x.item then return {ok=false,reason="raw ore extraction arm carries another material"} end
local network=drill.electric_network_id;local powered=network~=nil and network==arm.electric_network_id and drill.energy>0 and arm.energy>0
local generator=false;for _,e in pairs(s.find_entities_filtered{force=f,type="generator"}) do if e.electric_network_id==network then generator=true;break end end
local rotation=arm.prototype.get_inserter_rotation_speed("normal");local extension=arm.prototype.get_inserter_extension_speed("normal")
if not rotation or rotation<=0 or not extension or extension<=0 then return {ok=false,reason="raw ore extraction rate is unavailable"} end
local function radius_at(p) return math.sqrt((p.x-arm.position.x)^2+(p.y-arm.position.y)^2) end
local arm_rate=3600/(1/rotation+2*math.abs(radius_at(arm.drop_position)-radius_at(arm.pickup_position))/extension)
local mining_time=prototypes.entity[x.item].mineable_properties.mining_time
local rate=math.min(drill.prototype.mining_speed*60/mining_time,arm_rate,first.prototype.belt_speed*4*3600,last.prototype.belt_speed*4*3600)
return {ok=true,world_id=d.world_id,tick=game.tick,remaining=remaining,complete=true,powered=powered and generator,
 drill_unit=drill.unit_number,nominal_rate_per_minute=rate,speed_bonus=drill.speed_bonus,productivity_bonus=drill.productivity_bonus,
 output_items=output,chest_items=chest.get_inventory(defines.inventory.chest).get_item_count(x.item)}
''')
    if measured.get("ok") and measured.get("complete"):
        # Ignore beneficial modules/research. Apply every negative live effect
        # even when the extraction arm already limits the baseline rate.
        for effect in ("speed_bonus", "productivity_bonus"):
            bonus = measured.get(effect)
            if not isinstance(bonus, (int, float)) or not math.isfinite(bonus):
                return {"ok": False, "reason": "raw ore mining effects are unavailable"}
            measured["nominal_rate_per_minute"] *= max(0, 1 + min(0, bonus))
    return measured


def ensure_raw_ore(factory, obs: dict, item: str, rate_per_minute: float | None = None) -> dict:
    from .deterministic_factory import _ready, _report

    requested = 1.0 if rate_per_minute is None else float(rate_per_minute)
    if not math.isfinite(requested) or requested < 0:
        return _report("blocked", "raw ore demand must be a finite nonnegative rate", item=item)
    if not obs.get("enabled_recipes", {}).get("electric-mining-drill"):
        return factory.request_recipe_unlock(obs, "electric-mining-drill")
    primary_key = "source:" + item
    primary = factory._raw_capacity_site(obs, item, primary_key)
    if not primary.get("ok"):
        return _report("blocked", primary.get("reason", "dedicated raw ore site unavailable"), item=item)
    if "raw_ore_cells" not in primary:
        primary["raw_ore_cells"] = [primary_key]
        factory._save()
    keys = primary["raw_ore_cells"]
    if (not isinstance(keys, list) or not keys or any(not isinstance(key, str) for key in keys)
            or keys[0] != primary_key or len(keys) != len(set(keys))
            or any(key != primary_key and not key.startswith(primary_key + ":ore:") for key in keys)):
        return _report("blocked", "dedicated raw ore cell history is incompatible", item=item)
    capacity, evidence, drill_units = 0.0, [], set()
    for key in list(keys):
        plan = factory.state["blocks"].get(key)
        if plan is None:
            return _report("blocked", "dedicated raw ore cell reservation is missing", item=item, cell=key)
        proof = _inspect(factory, obs, item, plan)
        if not proof.get("ok") or proof.get("world_id") != obs.get("world_id"):
            return _report("blocked", proof.get("reason", "raw ore observation unavailable"), item=item, cell=key)
        if "remaining" not in proof:
            return _report("blocked", "raw ore resource survey is incomplete", item=item, cell=key)
        retired = proof["remaining"] <= 0
        if plan.get("raw_ore_retired", False) != retired:
            plan["raw_ore_retired"] = retired
            factory._save()
        # Exhausted drills are left in place. Preserve and repair the original
        # buffer/extraction/bus so downstream consumers never move their ports.
        maintained = ({**plan, "entities": [e for e in plan["entities"] if e["name"] != "electric-mining-drill"]}
                      if retired else plan)
        result = factory.builder.ensure_plan(obs, maintained)
        if not _ready(result):
            return result
        result = factory.ensure_power_connection(obs, key, maintained)
        if not _ready(result):
            return result
        if key != primary_key:
            result = factory._merge_output(obs, plan["ports"][0], primary["ports"][0], key + ":output")
            if not _ready(result):
                return result
        if retired:
            continue
        if not proof.get("complete") or not proof.get("powered"):
            return _report("waiting", "waiting for dedicated ore drill and extraction power observation", item=item, cell=key)
        rate = float(proof.get("nominal_rate_per_minute", 0))
        if not math.isfinite(rate) or rate <= 0:
            return _report("blocked", "raw ore nominal mining rate is unavailable", item=item, cell=key)
        unit = proof.get("drill_unit")
        if type(unit) is not int or unit <= 0 or unit in drill_units:
            return _report("blocked", "raw ore drill identity is unavailable or counted twice", item=item, cell=key)
        drill_units.add(unit)
        capacity += rate
        evidence.append({"cell": key, **proof})
    if not evidence or capacity + .01 < requested:
        if len(keys) >= 32:
            return _report("blocked", "dedicated raw ore cell reservation budget exhausted", item=item)
        key = primary_key + ":ore:" + str(len(keys))
        plan = factory._raw_capacity_site(obs, item, key)
        if not plan.get("ok"):
            return _report("blocked", plan.get("reason", "replacement raw ore site unavailable"), item=item)
        keys.append(key)
        factory._save()
        return _report("waiting", "reserved an additional dedicated ore mining cell", item=item, cell=key)
    return _report("succeeded", "dedicated ore mining supplies its stable output bus", ports=deepcopy(primary["ports"]),
                   cells=evidence, nominal_capacity_per_minute=capacity, requested_rate_per_minute=requested,
                   flow_verified=False, transport_route_capacity_verified=False, input_handcarry=False)
