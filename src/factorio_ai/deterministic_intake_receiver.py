"""Observe an existing turret independently from newly reserved intake hardware."""
from copy import deepcopy
import json

from .factory_templates import DIRECTIONS


def receiver_identity(driver, turret):
    return {"name": "gun-turret", "position": deepcopy(turret["position"]),
            "unit_number": turret.get("unit_number"), "world_id": driver.state.get("world_id"),
            "catalog_fingerprint": driver.catalog.fingerprint}


def support_entities(plan, turret):
    """Compare legacy orphan support without changing the original saved plan."""
    turrets = [e for e in plan.get("entities", []) if e["name"] == "gun-turret"]
    if turrets and (len(turrets) != 1 or turrets[0] != {
            "name": "gun-turret", "position": turret["position"], "direction": turrets[0].get("direction", 0),
            "_width": 2, "_height": 2}):
        return None
    return [e for e in plan.get("entities", []) if e["name"] != "gun-turret"]


def verify_receiver(driver, obs, turret, plan=None):
    expected = receiver_identity(driver, turret)
    if (expected["world_id"] != obs.get("world_id") or type(expected["unit_number"]) is not int
            or expected["unit_number"] <= 0 or type(obs.get("tick")) is not int
            or obs["tick"] < 0 or (plan is not None and plan.get("existing_receiver") != expected)):
        return False
    arms = []
    for direction, (dx, dy) in DIRECTIONS.items():
        for tangent in (-.5, .5):
            arms.append({"name": "inserter", "direction": direction, "position": {
                "x": turret["position"]["x"] + dx * 1.5 - dy * tangent,
                "y": turret["position"]["y"] + dy * 1.5 + dx * tangent}})
    if plan is not None:
        planned = [e for e in plan.get("entities", []) if e["name"] == "inserter"]
        if len(planned) != 1 or planned[0] not in arms:
            return False
        arm = planned[0]
        dx, dy = DIRECTIONS[arm["direction"]]
        pickup = {"name": "transport-belt", "position": {
            "x": arm["position"]["x"] + dx, "y": arm["position"]["y"] + dy},
            "direction": (arm["direction"] + 8) % 16}
        if pickup not in plan["entities"]:
            return False
    payload = json.dumps(json.dumps({"receiver": expected, "tick": obs["tick"], "arms": arms}))
    result = driver.game.query('''
--[[ existing_turret_receiver: no receiver construction or reservation. ]]
local x=helpers.json_to_table(''' + payload + ''');local r=x.receiver;local t=target(r.position,r.name)
if not d or d.world_id~=r.world_id or game.tick<x.tick or not t or t.name~=r.name or t.force~=f or t.surface~=s
 or t.unit_number~=r.unit_number or t.position.x~=r.position.x or t.position.y~=r.position.y
 or t.prototype.tile_width~=2 or t.prototype.tile_height~=2 then return {ok=false} end
local proto=prototypes.entity.inserter;local belt=prototypes.entity["transport-belt"].collision_box
local function rotate(v,direction)
 local px,py=v.x or v[1],v.y or v[2]
 if direction==4 then return -py,px elseif direction==8 then return -px,-py elseif direction==12 then return py,-px end
 return px,py
end
for _,arm in ipairs(x.arms) do
 local px,py=rotate(proto.inserter_pickup_position,arm.direction)
 local dx,dy=rotate(proto.inserter_drop_position,arm.direction)
 local vx,vy=rotate({0,-1},arm.direction)
 local drop={x=arm.position.x+dx,y=arm.position.y+dy};local b=t.bounding_box
 if not (drop.x>b.left_top.x and drop.x<b.right_bottom.x and drop.y>b.left_top.y and drop.y<b.right_bottom.y
  and px-vx>belt.left_top.x and px-vx<belt.right_bottom.x
  and py-vy>belt.left_top.y and py-vy<belt.right_bottom.y) then return {ok=false} end
end
return {ok=true,receiver_verified=true,tick=game.tick}
''')
    return (isinstance(result, dict) and result.get("ok") is True and result.get("receiver_verified") is True
            and type(result.get("tick")) is int and result["tick"] >= obs["tick"])
