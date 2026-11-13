"""Read-only whole-block surveys with explicit ordinary natural clearance."""

from __future__ import annotations

import json
from .resource_policy import policy_specs, mining_reservations, RESOURCE_CHECK_LUA


OWNED_CONFIGURATION_GUARD = r"""
local same_direction=old.direction==(spec.direction or 0)
if spec.name=="gun-turret"then same_direction=true end
if spec.name=="steam-engine"or spec.name=="steam-turbine"then same_direction=old.direction%8==(spec.direction or 0)%8 end
if old.force~=f or(not same_direction and old.type~="electric-pole"and not spec.recipe)then
 return {ok=false,reason="owned_block_entity_configuration_changed"}end
"""


def overlap(a, b):
    return a[0][0] < b[1][0] and a[1][0] > b[0][0] and a[0][1] < b[1][1] and a[1][1] > b[0][1]


def survey_slots(game, slots):
    payload = json.dumps(json.dumps(slots, separators=(",", ":")))
    return game.query(
        """
local slots=helpers.json_to_table("""
        + payload
        + """);local legal={}
for index,areas in ipairs(slots) do
 local clear=true
 for _,area in ipairs(areas) do
  if s.count_entities_filtered{area=area,type="resource"}>0 then clear=false;break end
  for _,e in pairs(s.find_entities_filtered{area=area}) do
   local rock=e.type=="simple-entity" and (e.name=="big-rock" or e.name=="huge-rock" or e.name=="big-sand-rock")
   local nature=e.force.name=="neutral" and e.minable and (e.type=="tree" or rock)
   if e~=a and e.type~="electric-pole" and not nature and not e.prototype.collision_mask.colliding_with_tiles_only then clear=false;break end
  end
  if not clear then break end
  for _,tile in pairs(s.find_tiles_filtered{area=area}) do
   for layer in pairs(prototypes.entity["assembling-machine-1"].collision_mask.layers) do
    if tile.collides_with(layer) then clear=false;break end
   end
   if not clear then break end
  end
  if not clear then break end
 end
 if clear then legal[#legal+1]=index end
end
return {ok=true,legal=legal}
"""
    )


def preflight(game, builder, obs, entities, *, ignored_units=()):
    boxes = [builder._occupied_by_plan([e]) for e in entities]
    if sum(map(len, boxes)) != len(set().union(*boxes)):
        return {"ok": False, "reason": "whole_block_prospective_overlap"}
    try:
        specs = policy_specs(builder, obs, entities, ignored_units=ignored_units)
        zones = mining_reservations(game, obs["world_id"])
    except (ValueError, KeyError, TypeError) as exc:
        return {"ok": False, "reason": "invalid_mining_resource_scope", "detail": str(exc)}
    payload = json.dumps(
        json.dumps({"specs": specs, "ignored": list(ignored_units), "zones": zones}, separators=(",", ":"))
    )
    result = game.query(
        RESOURCE_CHECK_LUA
        + """
local args=helpers.json_to_table("""
        + payload
        + """);local specs=args.specs;local obstacles={};local seen={};local ignored={}
for _,unit in ipairs(args.ignored) do ignored[unit]=true end
for _,spec in ipairs(specs) do
 local resource_reason=resource_violation(spec,args.zones,ignored,args.specs)
 if resource_reason then return {ok=false,reason=resource_reason,entity=spec.name,position=spec.position} end
 if not spec.resource_virtual_tunnel then
 local old=target(spec.position,spec.name)
 if old and ignored[old.unit_number] then old=nil end
 if old then
"""
        + OWNED_CONFIGURATION_GUARD
        + """
 else
  local legal=s.can_place_entity{name=spec.name,position=spec.position,direction=spec.direction or 0,force=f,
    type=spec.belt_to_ground_type,build_check_type=defines.build_check_type.manual_ghost,forced=true}
  local colliders=s.find_entities_filtered{area=spec.survey_area}
  if not legal then
   for _,e in pairs(colliders) do if ignored[e.unit_number] then legal=true;break end end
   if legal then for _,tile in pairs(s.find_tiles_filtered{area=spec.survey_area}) do
    for layer in pairs(prototypes.entity[spec.name].collision_mask.layers) do if tile.collides_with(layer) then legal=false;break end end
    if not legal then break end
   end end
  end
  if not legal then
   return {ok=false,reason="block_terrain_or_immovable_obstruction",entity=spec.name,position=spec.position}
  end
  for _,e in pairs(colliders) do
   if e~=a and e.type~="resource" and e.type~="corpse" and not ignored[e.unit_number] and not e.prototype.collision_mask.colliding_with_tiles_only then
    local rock=e.type=="simple-entity" and (e.name=="big-rock" or e.name=="huge-rock" or e.name=="big-sand-rock")
    if e.force.name~="neutral" or not e.minable or not (e.type=="tree" or rock) then
     return {ok=false,reason="block_protected_entity_obstruction",entity=e.name,position=pos(e.position)}
    end
    local key=e.name..":"..e.position.x..":"..e.position.y
    if not seen[key] then seen[key]=true;obstacles[#obstacles+1]={name=e.name,position=pos(e.position),unit_number=e.unit_number} end
   end
  end
 end
 end
end
return {ok=true,clear_obstacles=obstacles}
"""
    )
    if not result.get("ok"):
        return result
    # No cap: trees and rocks never reject a planned site (site.nature); the
    # builder clears every listed obstacle in batches before building.
    obstacles = result.get("clear_obstacles", [])
    if obstacles == {}:
        obstacles = []
    if not isinstance(obstacles, list):
        return {"ok": False, "reason": "invalid_native_clearance_shape"}
    return {**result, "clear_obstacles": obstacles, "clearance_world_id": obs["world_id"]}
