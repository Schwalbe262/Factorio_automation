"""Explicit owner-character succession; never respawn, transfer items or erase receipts."""

from __future__ import annotations

import json

from .actor_client import DETACHED_ACTOR_LUA


def _payload(value):
    return json.dumps(json.dumps(value, separators=(",", ":")))


def inspect_actor_recovery(game):
    """Works through raw query even when normal observation reports agent_dead."""
    return game.query(
        ACTOR_RECOVERY_INSPECT_LUA.replace(
            "PAYLOAD", _payload({"expected_world": getattr(game, "expected_world", None)})
        )
    )


def execute_actor_recovery(game, expected_world, expected_old_unit, expected_new_unit):
    """Caller holds the world writer lock; this changes framework storage only."""
    if (
        not isinstance(expected_world, str)
        or not expected_world
        or any(type(v) is not int or v < 1 for v in (expected_old_unit, expected_new_unit))
        or expected_old_unit == expected_new_unit
    ):
        raise ValueError("exact world and distinct positive old/new character units required")
    bound = getattr(game, "expected_world", None)
    if bound is not None and bound != expected_world:
        raise ValueError("actor recovery world disagrees with bound game")
    return game.query(
        ACTOR_RECOVERY_EXECUTE_LUA.replace(
            "PAYLOAD",
            _payload({"expected_world": expected_world, "old_unit": expected_old_unit, "new_unit": expected_new_unit}),
        )
    )


ACTOR_RECOVERY_COMMON_LUA = r"""
local function character_row(e)
 if not e or not e.valid then return {valid=false}end
 return {valid=true,name=e.name,unit_number=e.unit_number,position=pos(e.position),surface_index=e.surface.index,
  force_index=e.force.index,player_index=e.player and e.player.index or nil,health=e.health,max_health=e.max_health}
end
local function live_old_character(unit)
 local surfaces={};local count=0;local characters=0
 for _,surface in pairs(game.surfaces)do if not surfaces[surface.index]then
  surfaces[surface.index]=true;count=count+1
  if count>16 then return nil,"actor_recovery_character_survey_budget_exceeded"end
  local rows=surface.find_entities_filtered{type="character",limit=129}
  characters=characters+#rows
  if characters>128 then return nil,"actor_recovery_character_survey_budget_exceeded"end
  for _,e in pairs(rows)do if e.valid and e.unit_number==unit then return e end end
 end end
 return nil,nil
end
local function pinned_owner()
 local index=d and d.crafting_player_index
 if type(index)~="number"or index<1 then return nil,"actor_recovery_owner_not_pinned"end
 local owner=game.get_player(index)
 local expected_name=d.crafting_player_name or"FactoryAutomaton"
 if not owner or not owner.valid or owner.name~=expected_name or game.get_player(expected_name)~=owner then
  return nil,"actor_recovery_pinned_owner_changed"
 end
 return owner,nil
end
local function exact_successor(owner,unit)
 local e=owner and owner.character
 return owner and owner.connected and owner.force==f and e and e.valid and e.name=="character"
  and e.unit_number==unit and e.player==owner and e.force==f and e.surface==s
end
"""


ACTOR_RECOVERY_INSPECT_LUA = (
    r"""
local args=helpers.json_to_table(PAYLOAD)
if not d or not d.world_id then return {ok=false,reason="actor_recovery_framework_world_missing"}end
if args.expected_world and d.world_id~=args.expected_world then return {ok=false,reason="actor_recovery_world_changed"}end
"""
    + ACTOR_RECOVERY_COMMON_LUA
    + DETACHED_ACTOR_LUA
    + r"""
local owner,owner_error=pinned_owner();local old=d.crafting_actor_unit_number
local old_pointer=character_row(d.actor);local successor=character_row(owner and owner.character)
local live,scan_error=nil,nil
if type(old)=="number"and old>0 then live,scan_error=live_old_character(old)end
local inventory={};local truncated=false
if owner and owner.character and owner.character.valid then
 local e=owner.character
 local groups={{"main",defines.inventory.character_main},{"guns",defines.inventory.character_guns},
  {"ammo",defines.inventory.character_ammo},{"armor",defines.inventory.character_armor},{"trash",defines.inventory.character_trash}}
 local seen={}
 for _,group in ipairs(groups)do if group[2]and not seen[group[2]]then
  seen[group[2]]=true;local inv=e.get_inventory(group[2]);local items={};local total={}
  if inv then
   if #inv>1024 then truncated=true end
   for i=1,math.min(#inv,1024)do local stack=inv[i]
    if stack.valid_for_read then
     local row={slot=i,name=stack.name,quality=stack.quality.name,count=stack.count}
     if stack.prototype.type=="ammo"then row.ammo=stack.ammo;row.magazine_size=stack.prototype.magazine_size end
     if stack.prototype.type=="repair-tool"then row.durability=stack.durability end
     items[#items+1]=row;total[stack.name]=(total[stack.name]or 0)+stack.count
    end
   end
  end
  inventory[group[1]]={items=items,totals=total}
 end end
end
local center=f.get_spawn_position(s);local center_source="force_spawn";local last=d.actor_last_known_position
if type(last)=="table"and type(last.x)=="number"and type(last.y)=="number"then center=last;center_source="framework_last_known_position"end
local radius=center_source=="force_spawn"and 256 or 64
local corpses=s.find_entities_filtered{position=center,radius=radius,type="character-corpse",limit=33}
local matches={}
for _,corpse in pairs(corpses)do
 if corpse.valid and owner and corpse.character_corpse_player_index==owner.index then
  matches[#matches+1]={unit_number=corpse.unit_number,position=pos(corpse.position),surface_index=corpse.surface.index,
   player_index=corpse.character_corpse_player_index,tick_of_death=corpse.character_corpse_tick_of_death}
 end
end
local classification="owner_or_character_unavailable"
if old_pointer.valid or live then classification="old_character_still_live"
elseif successor.valid then
 if #matches>0 then classification="owner_death_evidence_with_successor"
 else classification="replacement_or_death_without_retained_corpse"end
end
local repair=d.native_repair;local pending=repair and true or false
local ready=not old_pointer.valid and not live and not scan_error and not pending and type(old)=="number"
 and old>0 and successor.unit_number~=old and exact_successor(owner,successor.unit_number)or false
return {ok=true,tick=game.tick,world_id=d.world_id,surface_index=s.index,pinned_owner_index=d.crafting_player_index,
 owner=owner and {name=owner.name,index=owner.index,connected=owner.connected,controller_type=owner.controller_type}or nil,
 owner_error=owner_error,old_unit=old,old_pointer=old_pointer,live_old_character=live and character_row(live)or nil,
 character_survey_error=scan_error,successor=successor,successor_inventory=inventory,inventory_truncated=truncated,
 matching_owner_corpses=matches,corpse_survey={center=pos(center),center_source=center_source,radius=radius,
  force_filter=false,matched_player_index=owner and owner.index or nil,truncated=#corpses>=33},
 classification=classification,recovery_ready=ready,native_repair_pending=pending,
 detached=detached_actor(),detached_record=d.detached_actor,
 pending_native_repair=type(repair)=="table"and {actor_unit=repair.actor_unit,world_id=repair.world_id,request=repair.request,phase=repair.phase}or nil,
 scope="read-only owner succession evidence; a corpse identifies its player, not the old character unit; absence does not prove no death"}
"""
)


ACTOR_RECOVERY_EXECUTE_LUA = (
    r"""
local args=helpers.json_to_table(PAYLOAD)
if not d or d.world_id~=args.expected_world then return {ok=false,status="blocked",tick=game.tick,reason="actor_recovery_world_changed"}end
if d.native_repair then return {ok=false,status="blocked",tick=game.tick,reason="actor_recovery_pending_native_repair_requires_separate_review"}end
if d.detached_actor~=nil then return {ok=false,status="blocked",tick=game.tick,reason="actor_recovery_detached_actor_has_no_player_successor"}end
"""
    + ACTOR_RECOVERY_COMMON_LUA
    + r"""
local owner,owner_error=pinned_owner()
if not owner then return {ok=false,status="blocked",tick=game.tick,reason=owner_error}end
if not exact_successor(owner,args.new_unit)then return {ok=false,status="blocked",tick=game.tick,reason="actor_recovery_exact_owner_successor_required"}end
local live,scan_error=live_old_character(args.old_unit)
if scan_error then return {ok=false,status="blocked",tick=game.tick,reason=scan_error}end
if live then return {ok=false,status="blocked",tick=game.tick,reason="actor_recovery_expected_old_character_still_live"}end
local records=d.actor_successions
if records~=nil and type(records)~="table"then return {ok=false,status="blocked",tick=game.tick,reason="actor_recovery_audit_state_invalid"}end
local key=args.expected_world..":"..owner.index..":"..args.old_unit..":"..args.new_unit
local old=records and records[key]
if old then
 if type(old)~="table"or old.kind~="owner-character-succession-v1"or old.world_id~=args.expected_world
  or old.owner_index~=owner.index or old.owner_name~=owner.name or old.old_unit~=args.old_unit or old.new_unit~=args.new_unit
  or old.force_index~=f.index or old.surface_index~=s.index or old.old_pointer_invalid~=true or old.old_character_absent~=true
  or type(old.tick)~="number"or old.tick<0 or not d.actor or not d.actor.valid
  or d.actor~=owner.character or d.crafting_actor_unit_number~=args.new_unit then
  return {ok=false,status="blocked",tick=game.tick,reason="actor_recovery_retry_marker_or_current_binding_changed"}
 end
 return {ok=true,status="succeeded",tick=game.tick,reason="actor_recovery_already_bound",already_recovered=true,succession=old}
end
if d.actor and d.actor.valid then return {ok=false,status="blocked",tick=game.tick,reason="actor_recovery_live_framework_actor_must_not_be_replaced"}end
if d.crafting_actor_unit_number~=args.old_unit then return {ok=false,status="blocked",tick=game.tick,reason="actor_recovery_expected_old_unit_changed"}end
for _,record in pairs(records or{})do
 if type(record)~="table"then return {ok=false,status="blocked",tick=game.tick,reason="actor_recovery_audit_state_invalid"}end
 if record.world_id==args.expected_world and record.old_unit==args.old_unit then
  return {ok=false,status="blocked",tick=game.tick,reason="actor_recovery_conflicting_succession"}
 end
end
local marker={kind="owner-character-succession-v1",world_id=d.world_id,owner_index=owner.index,owner_name=owner.name,
 old_unit=args.old_unit,new_unit=args.new_unit,force_index=f.index,surface_index=s.index,tick=game.tick,
 old_pointer_invalid=true,old_character_absent=true,scope="framework storage rebind only; prior actor receipts remain unchanged"}
if not records then records={};d.actor_successions=records end
records[key]=marker
d.actor=owner.character
d.crafting_actor_unit_number=args.new_unit
return {ok=true,status="succeeded",tick=game.tick,reason="framework_actor_rebound_to_pinned_owner_successor",already_recovered=false,
 succession=marker,scope="no respawn, inventory, health, controller, research, production, settings or prior-receipt changes"}
"""
)
