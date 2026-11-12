"""Conserved native transfer of one pinned player's exact death-corpse stacks."""
from __future__ import annotations

import json


def _payload(value):return json.dumps(json.dumps(value, separators=(',', ':')))


def _arguments(game, world, actor, death_tick):
    if (not isinstance(world, str) or not world or any(type(v) is not int or v < 1 for v in (actor, death_tick))
            or (getattr(game, 'expected_world', None) is not None and game.expected_world != world)):
        raise ValueError('exact bound world, positive actor unit and death tick required')
    return {'world_id':world, 'actor_unit':actor, 'death_tick':death_tick}


def inspect_corpse_recovery(game, expected_world, expected_actor_unit, death_tick):
    args = _arguments(game, expected_world, expected_actor_unit, death_tick)
    return game.query(CORPSE_RECOVERY_INSPECT_LUA.replace('PAYLOAD', _payload(args)))


def execute_corpse_recovery(game, expected_world, expected_actor_unit, death_tick, *, keep_single_repair_tool=False):
    """Caller holds the writer lock; no movement or item-specification recreation."""
    args = _arguments(game, expected_world, expected_actor_unit, death_tick)
    if type(keep_single_repair_tool)is not bool:raise ValueError('repair-tool recovery policy must be boolean')
    args['keep_single_repair_tool']=keep_single_repair_tool
    return game.query(CORPSE_RECOVERY_EXECUTE_LUA.replace('PAYLOAD', _payload(args)))


CORPSE_RECOVERY_COMMON_LUA=r'''
local args=helpers.json_to_table(PAYLOAD)
local function reject(reason)return {ok=false,status="blocked",reason=reason,tick=game.tick}end
local function same_position(p,q)return p.x==q.x and p.y==q.y end
local function context()
 if not d or d.world_id~=args.world_id then return nil,"corpse_recovery_world_changed"end
 if type(d.crafting_player_index)~="number"or d.crafting_player_index<1 then return nil,"corpse_recovery_pinned_owner_changed"end
 local owner=game.get_player(d.crafting_player_index)
 local expected_name=d.crafting_player_name or"FactoryAutomaton"
 if not owner or not owner.valid or owner.name~=expected_name or not owner.connected or owner.force~=f
  or game.get_player(expected_name)~=owner then return nil,"corpse_recovery_pinned_owner_changed"end
 local actor=d.actor
 if not actor or not actor.valid or actor.name~="character"or actor.unit_number~=args.actor_unit
  or d.crafting_actor_unit_number~=args.actor_unit or owner.character~=actor or actor.player~=owner
  or actor.force~=f or actor.surface~=s then return nil,"corpse_recovery_exact_owned_actor_required"end
 return owner,nil
end
local function select_corpse(owner)
 local matches={};local spawn=f.get_spawn_position(s)
 local rows=s.find_entities_filtered{position=spawn,radius=256,type="character-corpse",limit=65}
 if #rows>=65 then return nil,"corpse_recovery_corpse_survey_truncated"end
 for _,e in pairs(rows)do if e.valid and e.character_corpse_player_index==owner.index
  and e.character_corpse_tick_of_death==args.death_tick then matches[#matches+1]=e end end
 if #matches>1 then return nil,"corpse_recovery_ambiguous_owner_death"end
 return matches[1],nil
end
local function empty_goods()return {counts={},normal_counts={},rounds={},durability={},item_count=0,occupied_stacks=0}end
local function add(map,key,value)map[key]=(map[key]or 0)+value end
local function add_stack(value,stack)
 if not stack or not stack.valid_for_read then return end
 local count=stack.count;local quality=stack.quality.name;local key=stack.name.."@"..quality
 if type(count)~="number"or count<1 or count%1~=0 then error("corpse_recovery_invalid_native_stack")end
 add(value.counts,key,count);if quality=="normal"then add(value.normal_counts,stack.name,count)end
 value.item_count=value.item_count+count;value.occupied_stacks=value.occupied_stacks+1
 if stack.prototype.type=="ammo"then
  local size=stack.prototype.magazine_size;local rounds=stack.ammo
  if not size or size<=0 or not rounds or rounds<0 or rounds>size then error("corpse_recovery_ammo_metadata_unavailable")end
  add(value.rounds,key,(count-1)*size+rounds)
 end
 if stack.prototype.type=="repair-tool"or stack.prototype.type=="tool"then
  local maximum=stack.prototype.get_durability(stack.quality);local durability=stack.durability
  if not maximum or maximum<=0 or not durability or durability<0 or durability>maximum then error("corpse_recovery_tool_metadata_unavailable")end
  add(value.durability,key,(count-1)*maximum+durability)
 end
end
local function inventory_goods(inv,limit)
 local value=empty_goods()
 if not inv or not inv.valid then return value end
 if #inv>limit then error("corpse_recovery_inventory_stack_budget_exceeded")end
 for i=1,#inv do add_stack(value,inv[i])end
 return value
end
local function combined(a,b)
 local value=empty_goods()
 for _,name in ipairs{"counts","normal_counts","rounds","durability"}do
  for key,count in pairs(a[name])do add(value[name],key,count)end
  for key,count in pairs(b[name])do add(value[name],key,count)end
 end
 value.item_count=a.item_count+b.item_count;value.occupied_stacks=a.occupied_stacks+b.occupied_stacks
 return value
end
local function actor_goods(actor)
 local value=empty_goods();local seen={}
 local indices={defines.inventory.character_main,defines.inventory.character_guns,defines.inventory.character_ammo,
  defines.inventory.character_armor,defines.inventory.character_trash}
 for _,index in ipairs(indices)do if not seen[index]then seen[index]=true
  value=combined(value,inventory_goods(actor.get_inventory(index),1024))
 end end
 add_stack(value,actor.cursor_stack)
 return value
end
local function conserved(a,b)
 for _,name in ipairs{"counts","normal_counts","rounds","durability"}do
  local tolerance=name=="durability"and .000001 or 0
  for key,value in pairs(a[name])do if math.abs(value-(b[name][key]or 0))>tolerance then return false end end
  for key,value in pairs(b[name])do if math.abs(value-(a[name][key]or 0))>tolerance then return false end end
 end
 return a.item_count==b.item_count
end
local function not_increased(before,after)
 for _,name in ipairs{"counts","rounds","durability"}do
  for key,value in pairs(after[name])do if value>(before[name][key]or 0)+.000001 then return false end end
 end
 return after.item_count<=before.item_count
end
local function corpse_goods(corpse)
 if not corpse or not corpse.valid then return empty_goods(),nil end
 local inv=corpse.get_inventory(defines.inventory.character_corpse)
 if not inv or not inv.valid then error("corpse_recovery_native_corpse_inventory_missing")end
 return inventory_goods(inv,512),inv
end
local function marker_key(owner)return args.world_id..":"..owner.index..":"..args.death_tick end
local function valid_marker(marker,owner,corpse)
 return type(marker)=="table"and marker.kind=="native-corpse-stack-transfer-v1"and marker.world_id==args.world_id
  and marker.owner_index==owner.index and marker.owner_name==owner.name and marker.actor_unit==args.actor_unit
  and marker.death_tick==args.death_tick and marker.surface_index==s.index and marker.force_index==f.index
  and type(marker.receipts)=="table"and type(marker.actor_position)=="table"
  and type(marker.remaining_goods)=="table"and type(marker.transferred_items)=="number"and marker.transferred_items>=0
  and(marker.phase=="waiting"or marker.phase=="completed"or marker.phase=="blocked")
  and type(marker.position)=="table"and(not corpse or same_position(corpse.position,marker.position))
end
local function read_marker(owner,corpse)
 if d.corpse_recoveries~=nil and type(d.corpse_recoveries)~="table"then return nil,"corpse_recovery_audit_state_invalid"end
 local marker=d.corpse_recoveries and d.corpse_recoveries[marker_key(owner)]
 if marker and not valid_marker(marker,owner,corpse)then return nil,"corpse_recovery_marker_identity_changed"end
 return marker,nil
end
'''


CORPSE_RECOVERY_INSPECT_LUA=CORPSE_RECOVERY_COMMON_LUA+r'''
local owner,reason=context();if not owner then return reject(reason)end
local corpse,why=select_corpse(owner);if why then return reject(why)end
local marker,invalid=read_marker(owner,corpse);if invalid then return reject(invalid)end
if not corpse and not(marker and marker.phase=="completed")then return reject("corpse_recovery_exact_owner_death_missing")end
local goods,inv=corpse_goods(corpse);local actor=actor_goods(d.actor);local main=d.actor.get_main_inventory();local free=0
for i=1,#main do if not main[i].valid_for_read then free=free+1 end end
return {ok=true,tick=game.tick,world_id=d.world_id,owner_index=owner.index,actor_unit=d.actor.unit_number,
 actor_position=pos(d.actor.position),actor_at_spawn=same_position(d.actor.position,f.get_spawn_position(s)),
 corpse=corpse and {position=pos(corpse.position),player_index=owner.index,death_tick=args.death_tick,surface_index=s.index,
  inventory_slots=#inv,goods=goods}or nil,actor_goods=actor,empty_main_slots=free,native_repair_pending=d.native_repair and true or false,
 existing_phase=marker and marker.phase or nil,already_recovered=marker and marker.phase=="completed"or false,
 scope="exact owner/death corpse selected without force filter; read-only stack and conservation baseline"}
'''


CORPSE_RECOVERY_EXECUTE_LUA=CORPSE_RECOVERY_COMMON_LUA+r'''
local owner,reason=context();if not owner then return reject(reason)end
if d.native_repair or d.actor.crafting_queue_size>0 then return reject("corpse_recovery_actor_inventory_operation_pending")end
local corpse,why=select_corpse(owner);if why then return reject(why)end
local marker,invalid=read_marker(owner,corpse);if invalid then return reject(invalid)end
local remaining,source=corpse_goods(corpse)
if marker and marker.phase=="completed"then
 if remaining.item_count~=0 then return reject("corpse_recovery_completed_corpse_refilled")end
 return {ok=true,status="succeeded",reason="corpse_recovery_already_completed",tick=game.tick,already_recovered=true,
  transferred_items=0,total_transferred_items=marker.transferred_items,remaining_items=0,receipt_count=#marker.receipts,completion_tick=marker.completed_tick,
  actor_position=pos(d.actor.position),scope="idempotent replay; no inventory transfer"}
end
if marker and marker.phase=="blocked"then return reject("corpse_recovery_prior_conservation_failure_requires_review")end
local last=marker and marker.receipts[#marker.receipts]
if not corpse and not(last and last.phase=="pending")then return reject("corpse_recovery_exact_owner_death_missing")end
local actor=d.actor;local position=pos(actor.position);local main=actor.get_main_inventory()
if not main or not main.valid then return reject("corpse_recovery_actor_main_inventory_missing")end
if not marker then
 marker={kind="native-corpse-stack-transfer-v1",world_id=d.world_id,owner_index=owner.index,owner_name=owner.name,
  actor_unit=actor.unit_number,death_tick=args.death_tick,surface_index=s.index,force_index=f.index,position=pos(corpse.position),
  actor_position=position,started_tick=game.tick,phase="waiting",receipts={},transferred_items=0,initial_goods=remaining,remaining_goods=remaining}
 if not d.corpse_recoveries then d.corpse_recoveries={}end
 d.corpse_recoveries[marker_key(owner)]=marker
end
local function snapshot()
 local current,err=select_corpse(owner);if err then error(err)end
 if current and not same_position(current.position,marker.position)then error("corpse_recovery_position_changed")end
 local stock=corpse_goods(current);local held=actor_goods(actor)
 return {corpse=stock,actor=held,total=combined(stock,held),actor_position=pos(actor.position)}
end
local function finish(receipt,after,returned)
 if not same_position(receipt.before.actor_position,after.actor_position)or not conserved(receipt.before.total,after.total)
  or not not_increased(receipt.before.corpse,after.corpse)then
  receipt.after=after;receipt.phase="blocked";marker.phase="blocked";return false,"corpse_recovery_goods_not_conserved"
 end
 local moved=receipt.before.corpse.item_count-after.corpse.item_count
 if returned and returned~=moved then
  receipt.after=after;receipt.phase="blocked";marker.phase="blocked";return false,"corpse_recovery_native_transfer_count_changed"
 end
 receipt.after=after;receipt.transferred_items=moved;receipt.phase="verified";receipt.finished_tick=game.tick
 marker.transferred_items=marker.transferred_items+moved;marker.remaining_goods=after.corpse
 if after.corpse.item_count==0 then marker.phase="completed";marker.completed_tick=game.tick else marker.phase="waiting"end
 return true,nil
end
local pending=marker.receipts[#marker.receipts]
if pending and pending.phase=="pending"then
 local good,after=pcall(snapshot)
 if not good then return reject("corpse_recovery_pending_receipt_snapshot_failed")end
 local ok,err=finish(pending,after,nil);if not ok then return reject(err)end
 remaining,source=corpse_goods(corpse)
 if marker.phase=="completed"then
  return {ok=true,status="succeeded",reason="corpse_recovery_pending_prefix_verified",tick=game.tick,
   transferred_items=0,total_transferred_items=marker.transferred_items,remaining_items=0,receipt_count=#marker.receipts,actor_position=position}
 end
elseif not conserved(marker.remaining_goods,remaining)then return reject("corpse_recovery_remaining_corpse_changed")end
if #marker.receipts>=512 then return reject("corpse_recovery_receipt_budget_exceeded")end
local good,before=pcall(snapshot);if not good then return reject("corpse_recovery_before_snapshot_failed")end
local receipt={index=#marker.receipts+1,phase="pending",started_tick=game.tick,before=before,native_returned_items=0}
marker.receipts[#marker.receipts+1]=receipt
local size=#source
local deferred_tools=0
for i=1,size do
 if not corpse.valid or not source.valid then break end
 local stack=source[i]
 if stack.valid_for_read then
  if args.keep_single_repair_tool and stack.name=="repair-pack"
   and(main.get_item_count("repair-pack")>0 or stack.count>1)then deferred_tools=deferred_tools+stack.count
  else
  local ok,moved=pcall(function()return main.transfer_from_stack(stack)end)
  if not ok then receipt.native_error=tostring(moved);return reject("corpse_recovery_native_transfer_interrupted")end
  receipt.native_returned_items=receipt.native_returned_items+moved
  end
 end
end
local ok,after=pcall(snapshot)
if not ok then return reject("corpse_recovery_after_snapshot_failed")end
local verified,err=finish(receipt,after,receipt.native_returned_items);if not verified then return reject(err)end
return {ok=true,status=marker.phase=="completed"and"succeeded"or"waiting",tick=game.tick,
 reason=marker.phase=="completed"and"owned_belongings_transferred_conserved"or(deferred_tools>0 and"spare_repair_tools_retained_in_owned_corpse"or"corpse_recovery_main_inventory_full_remaining"),
 transferred_items=receipt.transferred_items,total_transferred_items=marker.transferred_items,remaining_items=after.corpse.item_count,
 remaining_stacks=after.corpse.occupied_stacks,receipt_index=receipt.index,actor_position=pos(actor.position),
 deferred_repair_tools=deferred_tools,
 conservation={before=before.total,after=after.total},scope="actual native stack transfer only; count, rounds and tool durability conserved; actor never moved"}
'''
