"""Engine pathfinding and bounded character input, without teleportation.

The isolated scenario retains its existing event handlers. Inputs expire when
the controller stops renewing them, and all movement/mining uses engine state.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import socket
import tempfile
from typing import Any
import zipfile

from .deterministic_game import GUARDED_MINE_LUA, validate_mine_guard


SCENARIO_INPUT_LUA = r'''
if not _G.factorio_ai_character_input_version then
 local old_path=script.get_event_handler(defines.events.on_script_path_request_finished)
 script.on_event(defines.events.on_script_path_request_finished,function(event)
  if old_path then old_path(event) end
  local owner=storage.deterministic_player
  local motion=owner and owner.motion
  if not motion or motion.request_id~=event.id then return end
  motion.request_id=nil
  if event.try_again_later then motion.status="retry";motion.retry_after_tick=game.tick+60;return end
  if not event.path or #event.path==0 then motion.status="blocked";motion.reason="no_character_path";return end
  motion.path=event.path;motion.next_waypoint=1;motion.status="running"
 end)
 local old_tick=script.get_event_handler(defines.events.on_tick)
 script.on_event(defines.events.on_tick,function(event)
  if old_tick then old_tick(event) end
  local owner=storage.deterministic_player
  local actor=owner and owner.actor
  local motion=owner and owner.motion
  if not actor or not actor.valid or not motion then return end
  local good,err=pcall(function()
   if motion.status=="blocked" or motion.status=="succeeded" or motion.status=="expired" then
    actor.walking_state={walking=false};actor.mining_state={mining=false}
    return
   end
   if event.tick>(motion.expires_tick or 0) then
    actor.walking_state={walking=false};actor.mining_state={mining=false}
    motion.status="expired";motion.reason="character_input_lease_expired";return
   end
   if motion.kind=="mine" then
    actor.walking_state={walking=false}
    local target_entity=motion.entity
    if not target_entity or not target_entity.valid then
     actor.mining_state={mining=false};motion.status="succeeded";return
    end
    if not actor.can_reach_entity(target_entity) then
     actor.mining_state={mining=false};motion.status="blocked";motion.reason="mining_out_of_reach";return
    end
    if motion.item and actor.get_item_count(motion.item)-motion.baseline>=motion.count then
     actor.mining_state={mining=false};motion.status="succeeded";return
    end
    if motion.item and not actor.get_main_inventory().can_insert{name=motion.item,count=1} then
     actor.mining_state={mining=false};motion.status="blocked";motion.reason="mining_inventory_full";return
    end
    local progress=actor.character_mining_progress
    local stock=motion.item and actor.get_item_count(motion.item) or 0
    if progress~=motion.last_mining_progress or stock~=motion.last_mining_stock then
     motion.last_mining_progress=progress;motion.last_mining_stock=stock;motion.last_progress_tick=event.tick
    elseif event.tick-motion.last_progress_tick>300 then
     actor.mining_state={mining=false};motion.status="blocked";motion.reason="character_mining_stalled";return
    end
    actor.update_selected_entity(target_entity.position)
    if actor.selected~=target_entity then
     actor.mining_state={mining=false};motion.status="blocked";motion.reason="mining_target_obstructed";return
    end
    actor.mining_state={mining=true,position=target_entity.position}
    return
   end
   actor.mining_state={mining=false}
   if motion.status=="waiting" and event.tick-(motion.started_tick or motion.last_progress_tick)>1800 then
    actor.walking_state={walking=false};motion.status="blocked";motion.reason="character_path_request_timed_out";return
   end
   if motion.status~="running" or not motion.path then actor.walking_state={walking=false};return end
   local waypoint=motion.path[motion.next_waypoint]
   while waypoint and (waypoint.position.x-actor.position.x)^2+(waypoint.position.y-actor.position.y)^2<0.16 do
    motion.next_waypoint=motion.next_waypoint+1;waypoint=motion.path[motion.next_waypoint]
   end
   if not waypoint then actor.walking_state={walking=false};motion.status="succeeded";return end
   local dx=waypoint.position.x-actor.position.x;local dy=waypoint.position.y-actor.position.y
   local steering_dx,steering_dy=dx,dy
   local previous=motion.path[motion.next_waypoint-1]
   --[[ Belts can push an actor off a clear path into a neighbouring obstacle.
   Correct cross-track drift while continuing along a straight segment. ]]
   if previous then
    if math.abs(previous.position.x-waypoint.position.x)<0.001 and math.abs(dx)>0.05 and math.abs(dy)>0.05 then
     steering_dy=(dy<0 and -1 or 1)*math.abs(dx)
    elseif math.abs(previous.position.y-waypoint.position.y)<0.001 and math.abs(dy)>0.05 and math.abs(dx)>0.05 then
     steering_dx=(dx<0 and -1 or 1)*math.abs(dy)
    end
   end
   local angle=math.atan2(steering_dy,steering_dx)
   actor.walking_state={walking=true,direction=(math.floor(angle/(math.pi/4)+0.5)*2+4)%16}
   local distance=dx*dx+dy*dy
   if motion.progress_waypoint~=motion.next_waypoint then
    motion.progress_waypoint=motion.next_waypoint;motion.best_waypoint_distance=distance;motion.last_progress_tick=event.tick
   elseif distance<(motion.best_waypoint_distance or math.huge)-0.01 then
    motion.best_waypoint_distance=distance;motion.last_progress_tick=event.tick
   elseif event.tick-motion.last_progress_tick>300 then
    actor.walking_state={walking=false};motion.status="blocked";motion.reason="character_path_stalled"
   end
  end)
  if not good then
   actor.walking_state={walking=false};actor.mining_state={mining=false}
   motion.status="blocked";motion.reason=tostring(err)
  end
 end)
 _G.factorio_ai_character_input_version=1
end
'''


INSTALL_INPUT_LUA = r'''
if not d or not a or not a.valid then return failure("character_missing") end
if _G.factorio_ai_character_input_version~=1
 or not script.get_event_handler(defines.events.on_tick)
 or not script.get_event_handler(defines.events.on_script_path_request_finished) then
 return failure("character_scenario_not_installed")
end
d.motion=nil
a.walking_state={walking=false};a.mining_state={mining=false}
return success{status="ready",scenario_version=1}
'''


CHARACTER_INPUT_LUA = r'''
local key=helpers.table_to_json(x)
local motion=d.motion
local retry_count=0
local replan_count=0
if motion and motion.key==key then
 replan_count=motion.replan_count or 0
 if x.type=="move" and motion.status=="blocked" and motion.reason=="character_path_stalled" then
  if replan_count>=2 then motion.reason="character_path_replan_exhausted";return failure(motion.reason) end
  replan_count=replan_count+1
  motion=nil
 end
end
if motion and motion.key==key and motion.status=="retry" then
 if game.tick<(motion.retry_after_tick or 0) then
  motion.expires_tick=game.tick+600;return success{status="waiting",reason="character_pathfinder_busy"}
 end
 retry_count=(motion.retry_count or 0)+1
 if retry_count>3 then motion.status="blocked";motion.reason="character_path_retry_exhausted";return failure(motion.reason) end
end
if motion and motion.key==key and motion.status~="expired" and motion.status~="retry" then
 motion.expires_tick=game.tick+600
 if motion.status=="blocked" then return failure(motion.reason or "character_input_blocked") end
 if motion.status=="succeeded" then d.motion=nil end
 return success{status=motion.status=="succeeded" and "succeeded" or "running",
  reason=motion.reason or "character_input_active",position=pos(a.position)}
end
a.walking_state={walking=false};a.mining_state={mining=false}
motion={key=key,kind=x.type,status="running",expires_tick=game.tick+600,last_progress_tick=game.tick,
 started_tick=game.tick,retry_count=retry_count,replan_count=replan_count}
d.motion=motion
if x.type=="move" then
 local goal=s.find_non_colliding_position("character",x.position,12,0.5)
 if not goal then motion.status="blocked";motion.reason="no_character_standing_position";return failure(motion.reason) end
 motion.status="waiting"
 motion.request_id=s.request_path{bounding_box=a.prototype.collision_box,collision_mask=a.prototype.collision_mask,
  start=a.position,goal=goal,force=f,radius=0.3,can_open_gates=true,entity_to_ignore=a,
  pathfind_flags={allow_destroy_friendly_entities=false,allow_paths_through_own_entities=false,cache=false}}
else
 local entity=target(x.position,x.name)
 local e=entity;local inv=a.get_main_inventory()
''' + GUARDED_MINE_LUA + r'''
 if not entity then motion.status="succeeded";return success{status="succeeded"} end
 if string.find(entity.name,"crash%-site") or string.find(entity.name,"wreck") then motion.status="blocked";motion.reason="protected_artifact";return failure(motion.reason) end
 if not a.can_reach_entity(entity) then motion.status="blocked";motion.reason="out_of_reach";return failure(motion.reason) end
 if entity.type=="resource" and entity.prototype.resource_category~="basic-solid" then motion.status="blocked";motion.reason="resource_requires_machine";return failure(motion.reason) end
 motion.entity=entity;motion.count=x.count or 1
 if entity.type=="resource" then
  motion.item=entity.name;motion.baseline=a.get_item_count(entity.name)
 end
end
return success{status="running",reason="engine_character_input_started"}
'''


SCENARIO_MARKER = "-- factorio-ai persistent character input v1"
SCENARIO_MODULE = "factorio_ai_character_control.lua"


BUILD_FOOTPRINT_LUA = r'''
local function build_box(x)
 local box=prototypes.entity[x.name].collision_box
 local left,top,right,bottom=math.huge,math.huge,-math.huge,-math.huge
 for _,corner in ipairs({{box.left_top.x,box.left_top.y},{box.left_top.x,box.right_bottom.y},
                        {box.right_bottom.x,box.left_top.y},{box.right_bottom.x,box.right_bottom.y}}) do
  local px,py=corner[1],corner[2]
  for rotation=1,math.floor((x.direction or 0)/4) do px,py=-py,px end
  left=math.min(left,px);right=math.max(right,px);top=math.min(top,py);bottom=math.max(bottom,py)
 end
 return left+x.position.x,top+x.position.y,right+x.position.x,bottom+x.position.y
end
'''


def prepare_character_save(cfg: Any, *, newly_created: bool = False) -> dict:
    """Embed portable scenario handlers in an offline isolated character save.

    Unmarked existing saves are refused unless the caller explicitly identifies
    them as newly created. The original zip is preserved next to the save.
    Freeplay's original control code and other archive members remain intact.
    """
    from .factorio import no_mod_save_path

    root = Path(cfg.runtime_dir).resolve()
    save = no_mod_save_path(cfg).resolve()
    if not save.is_relative_to(root):
        raise ValueError("character save must be inside its isolated runtime directory")
    try:
        connection = socket.create_connection((cfg.rcon_host, cfg.rcon_port), timeout=.2)
    except OSError:
        pass
    else:
        connection.close()
        raise RuntimeError("character scenario must be prepared before the server starts")
    if not save.is_file():
        raise FileNotFoundError(save)
    with zipfile.ZipFile(save) as original:
        names = original.namelist()
        if any(".." in name.replace("\\", "/").split("/") for name in names):
            raise ValueError("invalid character save archive member")
        controls = [name for name in names if name.count("/") == 1 and name.endswith("/control.lua")]
        if len(controls) != 1:
            raise ValueError("character save must contain exactly one scenario control.lua")
        control_name = controls[0]
        control = original.read(control_name).decode("utf-8-sig")
        installed = SCENARIO_MARKER in control
        if not installed and not newly_created:
            raise ValueError("refusing to modify an unmarked existing save; create a new character world")
        module_name = control_name.rsplit("/", 1)[0] + "/" + SCENARIO_MODULE
        source = SCENARIO_INPUT_LUA.encode("utf-8")
        if installed and module_name in names and original.read(module_name) == source:
            return {"status": "ready", "changed": False, "save": str(save), "scenario_version": 1}
        if not installed:
            control = control.rstrip() + "\n\n" + SCENARIO_MARKER + '\nrequire("factorio_ai_character_control")\n'
        backup = save.with_name(save.stem + ".pre-character-control.zip")
        if not backup.exists():
            shutil.copyfile(save, backup)
        descriptor, temporary = tempfile.mkstemp(prefix=".character-scenario-", suffix=".zip", dir=save.parent)
        os.close(descriptor)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as output:
                for info in original.infolist():
                    if info.filename == control_name:
                        output.writestr(info, control.encode("utf-8"))
                    elif info.filename != module_name:
                        with original.open(info) as source_file, output.open(info, "w") as destination:
                            shutil.copyfileobj(source_file, destination)
                output.writestr(module_name, source)
            # Close the original archive before replacing it on Windows.
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, save)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"status": "ready", "changed": True, "save": str(save), "backup": str(backup),
            "scenario_version": 1, "controller_sha256": hashlib.sha256(source).hexdigest()}


class CharacterNavigator:
    def __init__(self, game: Any):
        if game.backend != "character":
            raise ValueError("character navigator requires the character backend")
        self.game = game

    def install(self) -> dict:
        return self.game.query(INSTALL_INPUT_LUA)

    def stop(self) -> dict:
        self.game.query('if d then d.motion=nil end;return success{}')
        return self.game.act({"type": "stop"})

    def pending_action(self) -> dict | None:
        """Preserve a finite mining batch across partial-inventory observations."""
        result = self.game.query('''
local motion=d and d.motion
if motion and motion.kind=="mine" and motion.status=="running" then
 return success{action=helpers.json_to_table(motion.key)}
end
return success{}
''')
        return result.get("action") if result.get("ok") else None

    def execute(self, action: dict, observation: dict) -> dict:
        """Move into actual reach before allowing the ordinary adapter action."""
        from .deterministic_underground import underground_fields
        if action.get("type") == "build":
            underground_fields(action)
        elif "belt_to_ground_type" in action:
            raise ValueError("underground role is only valid on build actions")
        if action.get("type") == "stop":
            return self.stop()
        pending = self.pending_action()
        if pending and pending != action:
            result = self._input(pending)
            result["continued_action"] = pending
            return result
        if action.get("position") and action["type"] != "move":
            encoded = json.dumps(json.dumps(action, separators=(",", ":")))
            reach = self.game.query(BUILD_FOOTPRINT_LUA + '''
local x=helpers.json_to_table(''' + encoded + ''')
local e=target(x.position,x.name)
local within
if e and x.type=="take" and e.type=="item-entity" then
 within=(e.position.x-a.position.x)^2+(e.position.y-a.position.y)^2<=a.item_pickup_distance^2
elseif e then within=a.can_reach_entity(e)
elseif x.type=="build" then within=(x.position.x-a.position.x)^2+(x.position.y-a.position.y)^2<=a.build_distance^2
else within=(x.position.x-a.position.x)^2+(x.position.y-a.position.y)^2<=math.min(4,a.build_distance)^2 end
if within and x.type=="build" then
 --[[ Freeze the previous walk before checking placement in this transaction;
 otherwise it can enter the footprint before the separate build command. ]]
 if d then d.motion=nil end;a.walking_state={walking=false};a.mining_state={mining=false}
end
if within and not e and x.type=="build" and prototypes.entity[x.name]
 and not s.can_place_entity{name=x.name,position=x.position,direction=x.direction or 0,force=f,type=x.belt_to_ground_type} then
 local left,top,right,bottom=build_box(x)
 local actor_box=a.bounding_box
 --[[ Engine collision includes touching quantized edges; move the owned actor
 before retrying the unchanged normal placement check. ]]
 if actor_box.right_bottom.x>=left and actor_box.left_top.x<=right
 and actor_box.right_bottom.y>=top and actor_box.left_top.y<=bottom then
  local best=nil;local best_distance=math.huge
  for _,position in ipairs({{right+.8,x.position.y},{left-.8,x.position.y},{x.position.x,bottom+.8},{x.position.x,top-.8}}) do
   local candidate={x=position[1],y=position[2]}
   local distance=(candidate.x-a.position.x)^2+(candidate.y-a.position.y)^2
   if distance<best_distance and s.can_place_entity{name="character",position=candidate,force=f} then best=candidate;best_distance=distance end
  end
  if not best then return failure("no_character_build_standing_position") end
  return success{within=false,approach=best}
 end
end
return success{within=within}
''')
            if not reach.get("ok"):
                return reach
            if not reach.get("within"):
                return self._input({"type": "move", "position": reach.get("approach") or action["position"]})
        if action["type"] in {"move", "mine"}:
            return self._input(action)
        self.game.query('if d then d.motion=nil end;a.walking_state={walking=false};a.mining_state={mining=false};return success{}')
        return self.game.act(action)

    def _input(self, action: dict) -> dict:
        if action.get("type") not in {"move", "mine"}:
            raise ValueError("character input must be a move or mine action")
        validate_mine_guard(action)
        count = action.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("mining count must be a positive integer")
        encoded = json.dumps(json.dumps(action, separators=(",", ":")))
        return self.game.query('local x=helpers.json_to_table(' + encoded + ');' + CHARACTER_INPUT_LUA)
