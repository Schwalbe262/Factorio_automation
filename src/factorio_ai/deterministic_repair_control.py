"""Bounded native repair input with conserved cursor-stack cleanup.

Only the engine changes entity health and repair-pack durability. Issued inputs
are bounded; after interruption the single carried pack bounds native spending.
The saved cleanup record conserves the cursor, without a fabricated replacement.
"""
from __future__ import annotations

import json
import math
import time
import uuid

from .deterministic_state import stop_requested


MAX_REPAIR_TICKS = 120
MAX_REPAIR_PULSES = 20

REPAIR_COMMON_LUA = r'''
local function repair_owner(x)
 if not d or d.world_id~=x.expected_world_id or not a or not a.valid
  or a.unit_number~=x.expected_actor_unit or a.force~=f or a.surface~=s then return false end
 local p=a.player
 return p and p.connected and p.name=="FactoryAutomaton" and p.character==a
  and d.crafting_player_index==p.index and d.crafting_actor_unit_number==a.unit_number
end
local function repair_stock()
 local count,total=0,0
 local function add(stack)
  if stack and stack.valid_for_read and stack.name=="repair-pack" and stack.is_repair_tool then
   local maximum=stack.prototype.get_durability(stack.quality)
   if not maximum or maximum<=0 then error("repair_tool_durability_unavailable") end
   count=count+stack.count;total=total+(stack.count-1)*maximum+stack.durability
  end
 end
 local inventory=a.get_main_inventory()
 for i=1,#inventory do add(inventory[i]) end
 add(a.cursor_stack)
 return count,total
end
local function repair_target(job)
 local e=target(job.position,job.name)
 if e and e.valid and e.force==f and e.surface==s and e.unit_number==job.entity_unit
  and e.position.x==job.position.x and e.position.y==job.position.y then return e end
end
local function finish_repair(request)
 local job=d and d.native_repair
 if not job then return success{status="succeeded",reason="native_repair_already_stopped",repaired=0} end
 if request and job.request~=request then return failure("native_repair_request_changed") end
 if not repair_owner{expected_world_id=job.world_id,expected_actor_unit=job.actor_unit} then
  return failure("native_repair_owner_changed") end
 a.repair_state={repairing=false,position=job.position}
 if a.selected==job.last_selected then
  local previous=job.previous_selected
  if previous and previous.valid and previous.surface==s then a.selected=previous else a.clear_selected_entity() end
 end
 local cursor=a.cursor_stack
 if cursor and cursor.valid_for_read then
  if cursor.name~="repair-pack" or cursor.quality.name~=job.quality or not cursor.is_repair_tool then
   job.phase="cleanup";return failure("native_repair_cursor_changed") end
  a.get_main_inventory().transfer_from_stack(cursor)
  if cursor.valid_for_read then
   job.phase="cleanup";return success{status="waiting",reason="native_repair_cursor_return_waiting"} end
 end
 local e=repair_target(job);local count,total=repair_stock()
 local repaired=e and math.max(0,e.health-job.health) or 0
 local spent=job.durability-total
 local result={status=repaired>0 and "succeeded" or "waiting",reason=job.reason or "native_repair_burst_finished",
  repaired=repaired,health_before=job.health,health_after=e and e.health or nil,full=e and e.health>=e.max_health or false,
  packs_consumed=job.count-count,durability_used=spent,ticks=game.tick-job.started_tick,
  unit_number=job.entity_unit,world_id=job.world_id,request=job.request}
 d.native_repair=nil
 if spent<0 or result.packs_consumed<0 or game.tick<job.started_tick then
  return failure("native_repair_receipt_invalidated") end
 if repaired>0 and spent<=0 then return failure("native_repair_receipt_uncorrelated") end
 return success(result)
end
'''

BEGIN_REPAIR_LUA = REPAIR_COMMON_LUA + r'''
if not repair_owner(x) then return failure("native_repair_owner_changed") end
if d.native_repair then return failure("native_repair_cleanup_required") end
local e=target(x.position,x.name)
if not e or e.force~=f or e.surface~=s or e.unit_number~=x.expected_entity_unit
 or e.position.x~=x.position.x or e.position.y~=x.position.y then return failure("native_repair_target_changed") end
if e.type=="character" or not e.health or not e.max_health or e.max_health<=0 then return failure("native_repair_target_unsupported") end
if e.health>=e.max_health then return success{status="succeeded",reason="native_repair_not_needed",repaired=0} end
if not a.can_reach_entity(e) then return success{status="waiting",reason="native_repair_out_of_reach"} end
if a.crafting_queue_size>0 then return success{status="waiting",reason="native_repair_waiting_for_crafting"} end
local cursor=a.cursor_stack
if not cursor or cursor.valid_for_read or a.cursor_ghost or a.cursor_record then return failure("native_repair_cursor_busy") end
local stack=a.get_main_inventory().find_item_stack("repair-pack")
if not stack or not stack.is_repair_tool then return failure("native_repair_pack_missing") end
local count,durability=repair_stock()
--[[ Native repair automatically refills the cursor from the main inventory.
 Refuse spare packs so even an interrupted driver can spend only one real tool.
 The pulse deadline bounds issued inputs; it is not an autonomous engine timer. ]]
if count~=1 then return failure("native_repair_single_carried_pack_required") end
local job={request=x.request,world_id=d.world_id,actor_unit=a.unit_number,entity_unit=e.unit_number,
 name=e.name,position=pos(e.position),started_tick=game.tick,deadline=game.tick+120,
 previous_selected=a.selected,last_selected=a.selected,quality=stack.quality.name,health=e.health,
 count=count,durability=durability,phase="prepare"}
d.native_repair=job
if not cursor.transfer_stack(stack,1) or not cursor.valid_for_read or cursor.name~="repair-pack" then
 job.reason="native_repair_cursor_transfer_failed";return failure(job.reason) end
job.phase="repair"
d.motion=nil;a.walking_state={walking=false};a.mining_state={mining=false}
return success{status="running",reason="native_repair_prepared",request=job.request,tick=game.tick}
'''

PULSE_REPAIR_LUA = REPAIR_COMMON_LUA + r'''
if not repair_owner(x) then return failure("native_repair_owner_changed") end
local job=d.native_repair
if not job or job.request~=x.request then return failure("native_repair_request_changed") end
local e=repair_target(job);local cursor=a.cursor_stack
if game.tick<job.started_tick then job.reason="native_repair_tick_rollback"
elseif not e then job.reason="native_repair_target_changed"
elseif e.health>=e.max_health then job.reason="native_repair_target_restored"
elseif game.tick>=job.deadline then job.reason="native_repair_tick_budget_reached"
elseif not a.can_reach_entity(e) then job.reason="native_repair_out_of_reach"
elseif not cursor or not cursor.valid_for_read then job.reason="native_repair_pack_exhausted"
elseif cursor.name~="repair-pack" or cursor.quality.name~=job.quality or not cursor.is_repair_tool then job.reason="native_repair_cursor_changed"
elseif a.selected~=job.last_selected and a.selected~=job.previous_selected then job.reason="native_repair_selection_changed"
else
 a.walking_state={walking=false};a.mining_state={mining=false}
 a.update_selected_entity(e.position);job.last_selected=a.selected
 if a.selected~=e then job.reason="native_repair_target_obstructed"
 else
  a.repair_state={repairing=true,position=e.position}
  return success{status="running",reason="native_repair_input",tick=game.tick}
 end
end
a.repair_state={repairing=false,position=job.position}
return success{status="waiting",reason=job.reason}
'''

FINISH_REPAIR_LUA = REPAIR_COMMON_LUA + r'''
if not repair_owner(x) then return failure("native_repair_owner_changed") end
return finish_repair(x.request)
'''

# The normal stop action also recovers a pack left by an interrupted burst.
STOP_REPAIR_LUA = REPAIR_COMMON_LUA + r'''
if d and d.native_repair then
 local result=finish_repair(nil)
 if not result.ok or result.status=="waiting" and result.reason=="native_repair_cursor_return_waiting" then return result end
end
'''

# Generic name/count insert/remove recreates tool metadata. Use real stack
# transfers for the repair pack when ordinary production picks up or deposits it.
TRANSFER_REPAIR_PACK_LUA = r'''
if x.item=="repair-pack" then
 if e.force~=f or e.surface~=s then return failure("repair_pack_storage_changed") end
 local other=x.inventory and e.get_inventory(defines.inventory[x.inventory]) or nil
 if not other and x.type=="take" then other=e.get_output_inventory() end
 if not other then other=e.get_inventory(defines.inventory.chest) end
 if not other then return failure("repair_pack_inventory_required") end
 local source=x.type=="take" and other or inv
 local destination=x.type=="take" and inv or other
 local moved=0
 for i=1,#source do
  local stack=source[i]
  if stack.valid_for_read and stack.name=="repair-pack" and stack.is_repair_tool then
   for j=1,#destination do
    if not stack.valid_for_read or moved>=(x.count or 1) then break end
    local before=stack.count
    destination[j].transfer_stack(stack,(x.count or 1)-moved)
    moved=moved+before-(stack.valid_for_read and stack.count or 0)
   end
  end
  if moved>=(x.count or 1) then break end
 end
 return success{status=moved>0 and "succeeded" or "waiting",moved=moved}
end
'''


def validate_repair(action: dict) -> None:
    if action.get("type") not in {"repair", "finish_repair"}:
        raise ValueError("unsupported native repair action")
    if not isinstance(action.get("expected_world_id"), str) or not action["expected_world_id"]:
        raise ValueError("native repair requires the observed world")
    if type(action.get("expected_actor_unit")) is not int or action["expected_actor_unit"] <= 0:
        raise ValueError("native repair requires the observed actor")
    if "request" in action and (not isinstance(action["request"], str) or not action["request"]):
        raise ValueError("native repair request must be a nonempty string")
    if action.get("type") == "finish_repair":
        if not isinstance(action.get("request"), str) or not action["request"]:
            raise ValueError("native repair cleanup requires its request")
        return
    if type(action.get("expected_entity_unit")) is not int or action["expected_entity_unit"] <= 0:
        raise ValueError("native repair requires the observed entity")
    if not isinstance(action.get("name"), str) or not action["name"]:
        raise ValueError("native repair requires an entity name")
    position = action.get("position")
    if not isinstance(position, dict) or any(type(position.get(k)) not in (int, float)
            or not math.isfinite(position[k]) for k in ("x", "y")):
        raise ValueError("native repair requires a finite position")


def pending_repair(observation: dict) -> dict | None:
    pending = observation.get("repair_pending")
    if pending is None:
        return None
    if not isinstance(pending, dict):
        raise ValueError("invalid native repair cleanup observation")
    action = {"type": "finish_repair", "request": pending.get("request"),
              "expected_world_id": observation.get("world_id"),
              "expected_actor_unit": observation.get("actor_unit_number"),
              "reason": "return the conserved repair pack from an interrupted burst"}
    validate_repair(action)
    return action


def run_repair(game, action: dict) -> dict:
    validate_repair(action)
    request = dict(action, request=action.get("request") or str(uuid.uuid4()))
    encoded = json.dumps(json.dumps(request, separators=(",", ":")))
    prefix = "local x=helpers.json_to_table(" + encoded + ");"
    if action["type"] == "finish_repair":
        return game.query(prefix + FINISH_REPAIR_LUA)
    result = None
    cleanup = None
    error = None
    try:
        result = game.query(prefix + BEGIN_REPAIR_LUA)
        if result.get("ok") and result.get("status") == "running":
            started = time.monotonic()
            for _ in range(MAX_REPAIR_PULSES):
                if stop_requested(game.cfg.runtime_dir / "stop.json") or time.monotonic() - started >= 2:
                    break
                result = game.query(prefix + PULSE_REPAIR_LUA)
                if not result.get("ok") or result.get("status") != "running":
                    break
                time.sleep(.04)
    except Exception as exc:
        error = exc
    finally:
        # This exact request can only release our own cursor claim. It is safe
        # even when the preceding RCON reply was lost after native input began.
        try:
            cleanup = game.query(prefix + FINISH_REPAIR_LUA)
        except Exception as exc:
            if error is None:
                error = exc
    if error is not None:
        raise error
    if cleanup and not cleanup.get("ok"):
        return cleanup
    if result and not result.get("ok"):
        return result
    if cleanup and cleanup.get("reason") != "native_repair_already_stopped":
        return cleanup
    return result or cleanup
