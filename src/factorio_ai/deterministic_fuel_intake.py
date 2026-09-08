"""Normal furnace fueling from an existing, explicitly owned coal belt tail."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any


def _survey(factory: Any, burner: dict, options: list[dict], pending: dict | None = None) -> dict:
    payload = json.dumps(json.dumps({"burner": burner, "options": options, "pending": pending}))
    return factory.game.query('''
--[[ adjacent_owned_coal_intake: all engine operations here are read-only. ]]
local args=helpers.json_to_table(''' + payload + ''')
local furnace=target(args.burner.position,args.burner.name)
if not furnace or furnace.force~=f or not furnace.burner then return {ok=false,reason="fuel receiver is missing or foreign"} end
if args.pending and (args.pending.world_id~=d.world_id or args.pending.burner_unit~=furnace.unit_number) then
 return {ok=false,reason="fuel receiver identity changed"}
end
local proto=prototypes.entity.inserter;local recipe=f.recipes.inserter
if not recipe or not recipe.enabled then return {ok=false,reason="inserter recipe is unavailable"} end
local function rotate(v,direction)
 local x,y=v.x or v[1],v.y or v[2]
 if direction==4 then return -y,x elseif direction==8 then return -x,-y elseif direction==12 then return y,-x end
 return x,y
end
local function inside(p,e)
 return math.abs(p.x-e.position.x)<e.prototype.tile_width/2 and math.abs(p.y-e.position.y)<e.prototype.tile_height/2
end
local networks={};for _,e in pairs(s.find_entities_filtered{force=f,type="generator"}) do
 if e.energy>0 and e.electric_network_id then networks[e.electric_network_id]=true end
end
local poles={};for _,e in pairs(s.find_entities_filtered{force=f,type="electric-pole"}) do
 if e.name=="small-electric-pole" and networks[e.electric_network_id] then poles[#poles+1]=e end
end
table.sort(poles,function(x,y) return x.unit_number<y.unit_number end)
local rows={}
for _,option in ipairs(args.options) do
 local expected=option.belt;local belt=target(expected.position,"transport-belt")
 if belt and belt.force==f and belt.unit_number==option.unit_number and belt.direction==expected.direction then
  local pure=true;local coal=0
  for lane=1,2 do for _,stack in pairs(belt.get_transport_line(lane).get_contents()) do
   if stack.name~="coal" and stack.count>0 then pure=false end
   if stack.name=="coal" then coal=coal+stack.count end
  end end
  if pure then for _,direction in ipairs{0,4,8,12} do
   local px,py=rotate(proto.inserter_pickup_position,direction)
   local dx,dy=rotate(proto.inserter_drop_position,direction)
   local position={x=belt.position.x-px,y=belt.position.y-py}
   local drop={x=position.x+dx,y=position.y+dy}
   if inside(drop,furnace) and (not args.pending or (position.x==args.pending.arm.position.x
    and position.y==args.pending.arm.position.y and direction==args.pending.arm.direction)) then
    local arm=target(position,"inserter");local allowed=false
    if args.pending and arm then
     allowed=arm.force==f and arm.direction==direction and inside(arm.pickup_position,belt) and inside(arm.drop_position,furnace)
      and (not args.pending.arm_unit or args.pending.arm_unit==arm.unit_number)
    elseif not arm and not (args.pending and args.pending.arm_unit) then
     allowed=s.can_place_entity{name="inserter",position=position,direction=direction,force=f}
     if args.pending and not allowed then
      local box=proto.collision_box;local others=false;local actor=false
      for _,e in pairs(s.find_entities_filtered{area={{position.x+box.left_top.x,position.y+box.left_top.y},
       {position.x+box.right_bottom.x,position.y+box.right_bottom.y}}}) do
       if e==a then actor=true elseif e.type~="resource" then others=true end
      end
      allowed=actor and not others and s.can_place_entity{name="inserter",position=position,direction=direction,
       force=f,build_check_type=defines.build_check_type.script,forced=false}
     end
    end
    if allowed then for _,pole in ipairs(poles) do
     local reach=math.min(2,pole.prototype.get_supply_area_distance(pole.quality))
     if math.abs(position.x-pole.position.x)<=reach and math.abs(position.y-pole.position.y)<=reach
      and (not args.pending or pole.unit_number==args.pending.pole_unit) then
      rows[#rows+1]={parent=option.parent,belt=expected,belt_unit=belt.unit_number,burner_unit=furnace.unit_number,
       arm={name="inserter",position=position,direction=direction},arm_unit=arm and arm.unit_number,
       pole={name=pole.name,position=pos(pole.position),direction=0},pole_unit=pole.unit_number,coal_items=coal}
      break
     end
    end end
   end
  end end
 end
end
return {ok=true,world_id=d.world_id,tick=game.tick,options=rows}
''')


def _owned_options(factory: Any, obs: dict, burner: dict, source: dict, *, exclude: str) -> list[dict]:
    observed = {factory._entity_key(e): e for e in obs.get("entities", []) if e["name"] == "transport-belt"}
    claims: dict[str, list[tuple[str, dict, dict]]] = {}
    for key, plan in factory.state.get("links", {}).items():
        if key == exclude or factory.state.get("blocks", {}).get(key, {}).get("retired_for_upgrade"):
            continue
        for belt in plan.get("entities", []):
            if belt["name"] == "transport-belt":
                claims.setdefault(factory._entity_key(belt), []).append((key, plan, belt))
    options = []
    for identity, owners in claims.items():
        if len(owners) != 1:
            continue
        key, plan, belt = owners[0]
        live = observed.get(identity, {})
        if (plan.get("source_port") != source or not live.get("unit_number")
                or live.get("direction") != belt.get("direction")
                or any(name != "coal" and count for name, count in live.get("belt_inventory", {}).items())):
            continue
        p, center = belt["position"], burner["position"]
        distance = abs(p["x"] - center["x"]) + abs(p["y"] - center["y"])
        if distance <= 4:
            options.append({"parent": key, "belt": deepcopy(belt), "unit_number": live["unit_number"]})
    options.sort(key=lambda row: (row["belt"]["position"]["x"], row["belt"]["position"]["y"], row["parent"]))
    return options[:32]


def reserve_adjacent_fuel_intake(factory: Any, obs: dict, burner: dict, source: dict, key: str) -> bool:
    if burner["name"] not in {"stone-furnace", "steel-furnace"} or source.get("item") != "coal":
        return False
    options = _owned_options(factory, obs, burner, source, exclude=key)
    if not options:
        return False
    survey = _survey(factory, burner, options)
    if not survey.get("ok") or survey.get("world_id") != obs["world_id"]:
        return False
    occupied = factory.builder._occupied_by_plan(factory._reserved()) | factory._port_clearances()
    for row in survey.get("options", []):
        if factory.builder._occupied_by_plan([row["arm"]]) & occupied:
            continue
        record = {**deepcopy(row), "world_id": obs["world_id"], "burner": deepcopy(burner), "source_port": deepcopy(source)}
        port = {"kind": "item", "item": "coal", "direction": "input", "position": deepcopy(row["belt"]["position"]),
                "facing": row["belt"]["direction"]}
        factory.state["blocks"][key] = {"ok": True, "entities": [row["arm"], row["pole"]], "ports": [port],
                                         "adjacent_fuel_intake": record}
        factory.state["links"][key] = {"ok": True, "entities": [deepcopy(row["belt"])], "ports": [],
            "source_port": deepcopy(source), "consumer_port": port,
            "upstream_tap": {"link_key": row["parent"], "belt": deepcopy(row["belt"]),
                "intake": {"block_key": key, "inserter": deepcopy(row["arm"]), "receiver": deepcopy(burner)}}}
        factory._save()
        return True
    return False


def validate_adjacent_fuel_intake(factory: Any, obs: dict, burner: dict, source: dict, key: str) -> dict | None:
    plan = factory.state["blocks"][key]
    record = plan.get("adjacent_fuel_intake")
    if not record:
        return None
    failure = {"status": "blocked", "reason": "owned adjacent coal intake identity, content or power changed", "evidence": {"burner": burner}}
    if record["burner"] != burner or record["source_port"] != source or record["world_id"] != obs["world_id"]:
        return failure
    if factory.builder._occupied_by_plan([record["arm"]]) & factory.builder._occupied_by_plan(factory._reserved(exclude=key)):
        return failure
    options = _owned_options(factory, obs, burner, source, exclude=key)
    options = [row for row in options if row["parent"] == record["parent"] and row["belt"] == record["belt"]
               and row["unit_number"] == record["belt_unit"]]
    if len(options) != 1:
        return failure
    survey = _survey(factory, burner, options, record)
    if not survey.get("ok") or survey.get("world_id") != obs["world_id"] or len(survey.get("options", [])) != 1:
        return failure
    row = survey["options"][0]
    if row.get("arm_unit") and record.get("arm_unit") != row["arm_unit"]:
        record["arm_unit"] = row["arm_unit"]
        factory._save()
    return None
