"""Fresh, narrow permission to route directly to an unbuilt turret intake."""
from collections import Counter
import json
import math

from .factory_templates import DIRECTIONS
from .deterministic_intake_receiver import receiver_identity, verify_receiver


SURVEY = '''
local x=helpers.json_to_table(PAYLOAD);local r=x.receiver;local t=target(r.position,r.name)
local function normal(e) return e.quality and e.quality.name=="normal" end
local function healthy(e) return e.health and e.max_health and e.max_health>0 and e.health>=e.max_health end
if not d or d.world_id~=r.world_id or game.tick<x.tick or not t or t.force~=f or t.surface~=s
 or t.unit_number~=r.unit_number or t.position.x~=r.position.x or t.position.y~=r.position.y
 or not healthy(t) or not normal(t) then return {ok=false} end
local ammo=t.get_inventory(defines.inventory.turret_ammo)
if not ammo then return {ok=false} end
for _,stack in pairs(ammo.get_contents()) do
 if stack.count>0 and (stack.name~="firearm-magazine" or stack.quality~="normal") then return {ok=false} end
end
for _,b in ipairs(x.belts) do
 for _,e in pairs(s.find_entities_filtered{position=b.position,radius=0.2}) do
  if e.type~="resource" and e.type~="item-entity" and e.type~="character" then return {ok=false} end
 end
 if not s.can_place_entity{name=b.name,position=b.position,direction=b.direction,force=f} then return {ok=false} end
end
local vectors={[0]={0,-1},[4]={1,0},[8]={0,1},[12]={-1,0}}
local function touching(p)
 for _,b in ipairs(x.belts) do
  if math.abs(p.x-b.position.x)<0.51 and math.abs(p.y-b.position.y)<0.51 then return true end
 end
 return false
end
local function discharge(p,direction)
 local v=vectors[direction];return not v or touching{x=p.x+v[1],y=p.y+v[2]}
end
--[[ Scan actual contacts, including foreign forces and modified inserter reach. ]]
for _,e in pairs(s.find_entities_filtered{type={"transport-belt","underground-belt","inserter","splitter","mining-drill"}}) do
 if e.type=="inserter" then
  if touching(e.pickup_position) or touching(e.drop_position) then
   if not x.arm_unit or e.unit_number~=x.arm_unit or e.name~=x.arm.name or e.force~=f or e.surface~=s
    or e.position.x~=x.arm.position.x or e.position.y~=x.arm.position.y or e.direction~=x.arm.direction
    or not healthy(e) or not normal(e) then return {ok=false} end
   local box=t.bounding_box;local p=e.drop_position;local b=x.belts[1].position
   if math.abs(e.pickup_position.x-b.x)>=0.51 or math.abs(e.pickup_position.y-b.y)>=0.51
    or p.x<=box.left_top.x or p.x>=box.right_bottom.x or p.y<=box.left_top.y or p.y>=box.right_bottom.y
    or e.held_stack.valid_for_read then return {ok=false} end
  end
 elseif e.type=="mining-drill" then
  if touching(e.drop_position) then return {ok=false} end
 elseif e.type=="splitter" then
  for _,b in ipairs(x.belts) do
   if math.abs(e.position.x-b.position.x)<=2 and math.abs(e.position.y-b.position.y)<=2 then return {ok=false} end
  end
 elseif e.type=="transport-belt" or e.belt_to_ground_type=="output" then
  if discharge(e.position,e.direction) then return {ok=false} end
 end
end
local function rotate(p,d)
 local px,py=p.x or p[1],p.y or p[2]
 if d==4 then return -py,px elseif d==8 then return -px,-py elseif d==12 then return py,-px end
 return px,py
end
for _,e in ipairs(x.reserved) do
 local proto=prototypes.entity[e.name];if not proto or not vectors[e.direction] then return {ok=false} end
 if proto.type=="inserter" then
  for _,offset in ipairs({proto.inserter_pickup_position,proto.inserter_drop_position}) do
   local px,py=rotate(offset,e.direction)
   if touching{x=e.position.x+px,y=e.position.y+py} then return {ok=false} end
  end
 elseif proto.type=="transport-belt" or proto.type=="underground-belt" and e.belt_to_ground_type=="output" then
  if discharge(e.position,e.direction) then return {ok=false} end
 elseif proto.type=="splitter" then return {ok=false} end
end
return {ok=true,prospective_intake_verified=true,tick=game.tick}
'''


def prospective_intake(factory, obs, consumer):
    """Return only a current canonical four-support intake, never a flow proof."""
    try:
        state, fingerprint = factory.state, factory.catalog.fingerprint
        if (not obs.get("world_id") or state.get("world_id") != obs["world_id"]
                or state.get("catalog_fingerprint") != fingerprint or not fingerprint
                or type(obs.get("tick")) is not int or obs["tick"] < max(0, state.get("last_tick", 0))
                or consumer.get("item") != "firearm-magazine" or consumer.get("kind") != "item"
                or consumer.get("direction") != "input"):
            return None
        owners = [(key, plan) for key, plan in state.get("blocks", {}).items() if consumer in plan.get("ports", [])]
        if len(owners) != 1:
            return None
        key, plan = owners[0]
        receiver = plan.get("existing_receiver", {})
        turret = next((e for e in obs.get("entities", []) if e.get("name") == "gun-turret"
                       and e.get("unit_number") == receiver.get("unit_number") and e.get("position") == receiver.get("position")), None)
        if (not turret or key != f'armaments:gun-turret:{turret["position"]["x"]:g},{turret["position"]["y"]:g}'
                or plan.get("key") != key or key in state.get("links", {})
                or receiver != receiver_identity(factory, turret) or plan.get("ports") != [consumer]
                or Counter(e["name"] for e in plan["entities"]) != {"inserter": 1, "transport-belt": 2, "small-electric-pole": 1}):
            return None
        arm = next(e for e in plan["entities"] if e["name"] == "inserter")
        if type(arm.get("direction")) is not int or arm["direction"] not in DIRECTIONS:
            return None
        dx, dy = DIRECTIONS[arm["direction"]]
        belts = [{"name": "transport-belt", "direction": (arm["direction"]+8) % 16, "position": {
            "x": arm["position"]["x"]+step*dx, "y": arm["position"]["y"]+step*dy}} for step in (1, 2)]
        if (any(b not in plan["entities"] for b in belts) or consumer.get("position") != belts[1]["position"]
                or type(consumer.get("facing")) is not int or consumer["facing"] != belts[1]["direction"]
                or any(type(value) not in (int, float) or not math.isfinite(value)
                       for b in belts for value in b["position"].values())):
            return None
        other = factory._reserved(exclude=key)
        if factory.builder._occupied_by_plan(belts) & factory.builder._occupied_by_plan(other):
            return None
        contacts = []
        for e in other:
            kind = factory.catalog.entities[e["name"]]["type"]
            near = any(max(abs(e["position"][a]-b["position"][a]) for a in ("x", "y")) <= 3 for b in belts)
            if kind == "inserter" or near and kind in {"transport-belt", "underground-belt", "splitter"}:
                contacts.append({k: e[k] for k in ("name", "position", "direction", "belt_to_ground_type") if k in e})
                contacts[-1].setdefault("direction", 0)
        if not verify_receiver(factory, obs, turret, plan):
            return None
        actual_arm = next((e for e in obs.get("entities", []) if e.get("name") == arm["name"]
                           and e.get("position") == arm["position"]), {})
        payload = {"receiver": receiver, "tick": obs["tick"], "belts": belts, "arm": arm,
                   "arm_unit": actual_arm.get("unit_number"), "reserved": contacts}
        result = factory.game.query(SURVEY.replace("PAYLOAD", json.dumps(json.dumps(payload))))
        if (not isinstance(result, dict) or result.get("ok") is not True or result.get("prospective_intake_verified") is not True
                or type(result.get("tick")) is not int or result["tick"] < obs["tick"]):
            return None
        return plan if factory.builder.can_place(plan["entities"]).get("ok") is True else None
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None
