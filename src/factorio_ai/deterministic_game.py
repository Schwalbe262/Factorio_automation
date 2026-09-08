"""Resource-conserving RCON adapter for the deterministic player.

This adapter deliberately has no dependency on strategy/model/foundry code.
Only initial freeplay inventory is supplied. Machines and the engine crafting
queue perform production; research and launch counters are observation-only.
"""
from __future__ import annotations

from dataclasses import replace
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import time
from typing import Any
import uuid

from .config import AppConfig, load_config
from .factorio import (build_create_no_mod_save_command,
    build_start_no_mod_server_command, no_mod_save_path, wait_for_rcon)
from .rcon import FactorioRconClient, RconError, parse_json_response


BUILD_BATCH_LIMIT = 32
BUILD_BATCH_NAMES = frozenset({"transport-belt", "small-electric-pole"})


RECOVER_EQUIPPED_AMMO_LUA = r'''
if not d or d.world_id~=x.expected_actor_world_id or a.unit_number~=x.expected_actor_unit_number
 or a.force~=f then return failure("equipped_ammo_actor_identity_changed") end
local proto=prototypes.item[x.item]
if x.item~="firearm-magazine" or not proto or proto.type~="ammo" or not proto.magazine_size or proto.magazine_size<=0
 or type(x.count)~="number" or x.count~=math.floor(x.count) or x.count<1 or x.count>math.min(100,proto.stack_size)
 then return failure("invalid_equipped_ammo_request") end
local ammo=a.get_inventory(defines.inventory.character_ammo)
if not ammo or not ammo.valid or type(x.slot)~="number" or x.slot~=math.floor(x.slot) or x.slot<1 or x.slot>#ammo
 then return failure("equipped_ammo_slot_missing") end
local source=ammo[x.slot]
if not source.valid_for_read or source.name~=x.item or source.quality.name~="normal" or source.ammo~=proto.magazine_size
 then return failure("equipped_ammo_stack_changed") end
if not inv or not inv.valid then return failure("character_main_inventory_missing") end
local amount=math.min(x.count,source.count,inv.get_insertable_count{name=x.item,quality="normal"})
if amount<1 then return success{status="waiting",reason="character_main_inventory_full",moved=0} end
local inserted=inv.insert{name=x.item,quality="normal",count=amount}
--[[ Only complete normal magazines are split. Preserve every other slot and
 subtract exactly the quantity the main inventory accepted in this command. ]]
source.count=source.count-inserted
return success{status=inserted>0 and "succeeded" or "waiting",moved=inserted,
 source_inventory="character_ammo",destination_inventory="character_main"}
'''


def validate_equipped_ammo_recovery(action: dict[str, Any]) -> None:
    if action.get("type") != "recover_equipped_ammo":
        return
    if (action.get("item") != "firearm-magazine"
            or not isinstance(action.get("expected_actor_world_id"), str) or not action["expected_actor_world_id"]
            or any(type(action.get(key)) is not int or action[key] < 1 for key in ("slot", "count", "expected_actor_unit_number"))
            or action["count"] > 100 or "position" in action):
        raise ValueError("equipped ammo recovery requires a bounded slot, item, world and actor identity")


def validate_mine_guard(action: dict[str, Any]) -> None:
    identity = {"expected_entity_unit", "expected_entity_world_id"}
    if identity.intersection(action):
        unit = action.get("expected_entity_unit")
        if (not identity.issubset(action) or action.get("type") != "mine"
                or type(action.get("count", 1)) is not int or action.get("count", 1) != 1
                or not isinstance(unit, int) or isinstance(unit, bool) or unit < 1
                or not isinstance(action.get("expected_entity_world_id"), str) or not action["expected_entity_world_id"]):
            raise ValueError("invalid owned entity mining guard")
    if "lab_replacement" in action:
        replacement = action["lab_replacement"]
        position = replacement.get("position") if isinstance(replacement, dict) else None
        unit = replacement.get("unit_number") if isinstance(replacement, dict) else None
        if (not identity.issubset(action) or action.get("name") != "lab"
                or not isinstance(unit, int) or isinstance(unit, bool) or unit < 1
                or unit == action.get("expected_entity_unit")
                or not isinstance(position, dict) or not all(isinstance(position.get(axis), (int, float))
                    and not isinstance(position[axis], bool) and math.isfinite(position[axis]) for axis in ("x", "y"))):
            raise ValueError("invalid laboratory retirement guard")
    keys = {"expected_world_id", "expected_unit_number", "exhausted_source_receiver", "required_replacement_item"}
    if not keys.intersection(action):
        return
    receiver = action.get("exhausted_source_receiver")
    position = receiver.get("position") if isinstance(receiver, dict) else None
    unit = action.get("expected_unit_number")
    valid = (keys.issubset(action) and action.get("type") == "mine"
             and action.get("name") == "burner-mining-drill" and type(action.get("count", 1)) is int and action.get("count", 1) == 1
             and isinstance(action.get("expected_world_id"), str) and bool(action["expected_world_id"])
             and isinstance(unit, int) and not isinstance(unit, bool) and unit > 0
             and action.get("required_replacement_item") == "electric-mining-drill"
             and isinstance(receiver, dict) and isinstance(receiver.get("name"), str) and bool(receiver["name"])
             and isinstance(position, dict) and all(isinstance(position.get(k), (int, float))
                 and not isinstance(position[k], bool) and math.isfinite(position[k]) for k in ("x", "y")))
    if not valid:
        raise ValueError("invalid exhausted source mining guard")


GUARDED_MINE_LUA = r'''
if x.expected_entity_unit then
 if not d or d.world_id~=x.expected_entity_world_id then return failure("owned_mine_world_changed") end
 if not e or not e.valid or e.unit_number~=x.expected_entity_unit or e.force~=f then return failure("owned_mine_target_changed") end
end
if x.lab_replacement then
 if e.name~="lab" or not e.minable then return failure("lab_retirement_target_changed") end
 local input=e.get_inventory(defines.inventory.lab_input);local modules=e.get_module_inventory()
 if f.current_research or not input or not input.is_empty() or (modules and not modules.is_empty())
  then return failure("lab_retirement_not_idle_empty") end
 local replacement=target(x.lab_replacement.position,"lab")
 if not replacement or replacement.unit_number~=x.lab_replacement.unit_number or replacement==e
  or replacement.force~=f or replacement.energy<=0 then return failure("lab_retirement_replacement_changed") end
 if inv.get_insertable_count("lab")<1 then return failure("lab_retirement_inventory_full") end
end
if x.expected_world_id then
 if not d or d.world_id~=x.expected_world_id then return failure("source_upgrade_world_changed") end
 if not e or not e.valid or e.unit_number~=x.expected_unit_number or e.name~="burner-mining-drill" or e.force~=f
  then return failure("source_upgrade_target_changed") end
 local receiver=target(x.exhausted_source_receiver.position,x.exhausted_source_receiver.name)
 if not receiver or receiver.force~=f or (receiver.type~="container" and receiver.type~="furnace")
  then return failure("source_upgrade_receiver_changed") end
 local p=e.drop_position;local b=receiver.bounding_box
 if p.x<b.left_top.x or p.x>b.right_bottom.x or p.y<b.left_top.y or p.y>b.right_bottom.y
  then return failure("source_upgrade_receiver_changed") end
 local radius=e.prototype.mining_drill_radius
 for _,ore in pairs(s.find_entities_filtered{area={{e.position.x-radius,e.position.y-radius},
   {e.position.x+radius,e.position.y+radius}},type="resource"}) do
  if ore.amount>0 then return failure("source_upgrade_drill_not_exhausted") end
 end
 if inv.get_item_count(x.required_replacement_item)<1 then return failure("source_upgrade_replacement_missing") end
end
'''


def run_config(seed: int = 20260908, *, runtime: Path | None = None,
               server_port: int = 34200, rcon_port: int = 27015) -> AppConfig:
    cfg = load_config()
    root = Path(runtime) if runtime else cfg.runtime_dir / "deterministic" / str(seed)
    return replace(cfg, runtime_dir=root.resolve(), log_dir=(root / "logs").resolve(),
                   slurm_enabled=False, server_port=server_port, rcon_port=rcon_port)


def start_world(cfg: AppConfig, *, seed: int, new_world: bool = False, backend: str = "assisted") -> subprocess.Popen:
    """Create only a new isolated save, or resume its explicitly saved state."""
    save = no_mod_save_path(cfg)
    if backend not in {"assisted", "character"}:
        raise ValueError("unknown deterministic backend")
    newly_created = not save.exists()
    if new_world and save.exists():
        raise FileExistsError(f"World already exists; use --resume: {save}")
    try:
        connection = socket.create_connection((cfg.rcon_host, cfg.rcon_port), timeout=0.3)
    except OSError:
        pass
    else:
        connection.close()
        raise RuntimeError("RCON port is already in use; select --connect-only for the existing run")
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    if not save.exists():
        if not new_world:
            raise FileNotFoundError(f"No saved world; use --new-world: {save}")
        save.parent.mkdir(parents=True, exist_ok=True)
        command = build_create_no_mod_save_command(cfg, save)
        settings_path = Path(command[command.index("--map-gen-settings") + 1])
        # All controls use the game's default preset, with the agreed cliff exception.
        settings_path.write_text(json.dumps({"seed": seed, "cliff_settings": {
            "name": "cliff", "richness": 0, "cliff_elevation_interval": 0}}), encoding="utf-8")
        with (cfg.log_dir / "create-world.log").open("wb") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if backend == "character":
        from .deterministic_navigation import prepare_character_save
        prepare_character_save(cfg, newly_created=newly_created)
    command = build_start_no_mod_server_command(cfg, save_path=save)
    with (cfg.log_dir / "server-process.log").open("ab") as log:
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (cfg.runtime_dir / "server.pid").write_text(str(proc.pid), encoding="ascii")
    try:
        wait_for_rcon(cfg, timeout_seconds=60)
    except Exception:
        proc.terminate()
        raise
    return proc


_HELPERS = r'''
local d=storage.deterministic_player
local a=d and d.actor
if d and d.crafting_player_index and (not a or not a.valid) then
 local owner=game.get_player(d.crafting_player_index)
 if owner and owner.name=="FactoryAutomaton" and owner.connected and owner.character and owner.character.valid
    and d.crafting_actor_unit_number and owner.character.unit_number==d.crafting_actor_unit_number then
  a=owner.character;d.actor=a
 end
end
local s=game.surfaces.nauvis
local f=game.forces.player
local function contents(inv)
  local out={}
  if inv and inv.valid then
    for _,row in pairs(inv.get_contents()) do out[row.name]=(out[row.name] or 0)+row.count end
  end
  return out
end
local function pos(p) return {x=p.x,y=p.y} end
local function target(p,name)
  local rows=s.find_entities_filtered{position=p,radius=0.2,name=name}
  for _,e in pairs(rows) do if e.valid then return e end end
end
local function success(t) t=t or {};t.ok=true;return t end
local function failure(reason) return {ok=false,reason=reason} end
'''


class DeterministicGame:
    def __init__(self, cfg: AppConfig, *, backend: str = "assisted"):
        if backend not in {"assisted", "character"}:
            raise ValueError("backend must be assisted or character")
        self.cfg = cfg
        self.backend = backend
        self._confirmed = False

    def query(self, body: str) -> dict[str, Any]:
        if not self._confirmed:
            # Confirm with an inert command. Never retry a timed-out mutation.
            for attempt in range(2):
                try:
                    with FactorioRconClient(self.cfg.rcon_host, self.cfg.rcon_port,
                                            self.cfg.rcon_password, timeout=2) as c:
                        c.execute('/silent-command rcon.print("ready")', drain_seconds=0.02)
                    self._confirmed = True
                    break
                except TimeoutError:
                    if attempt:
                        raise
        lua = "local good,result=pcall(function() " + _HELPERS + body + " end); "
        lua += 'if not good then result={ok=false,reason=tostring(result)} end; rcon.print(helpers.table_to_json(result or {}))'
        command = "/silent-command " + " ".join(lua.splitlines())
        with FactorioRconClient(self.cfg.rcon_host, self.cfg.rcon_port,
                                self.cfg.rcon_password, timeout=30) as client:
            response = client.execute(command, drain_seconds=0.02)
        return parse_json_response(response)

    def initialize(self) -> dict[str, Any]:
        wid = str(uuid.uuid4())
        return self.query('''
if d then
  if not a or not a.valid then return failure("agent_dead") end
  return success{world_id=d.world_id,position=pos(a.position),inventory=contents(a.get_main_inventory())}
end
local spawn=f.get_spawn_position(s)
local where=s.find_non_colliding_position("character",spawn,32,0.5)
if not where then return failure("no_character_spawn") end
local actor=s.create_entity{name="character",position=where,force=f}
if not actor then return failure("character_creation_failed") end
storage.deterministic_player={world_id=''' + json.dumps(wid) + ''',actor=actor,created_tick=game.tick}
local items={['iron-plate']=8,wood=1,pistol=1,['firearm-magazine']=10,['burner-mining-drill']=1,['stone-furnace']=1}
for name,count in pairs(items) do actor.insert{name=name,count=count} end
return success{world_id=storage.deterministic_player.world_id,position=pos(actor.position),inventory=contents(actor.get_main_inventory())}
''')

    def observe(self, radius: float = 384) -> dict[str, Any]:
        observation = self.query('''
if not a or not a.valid then return failure("agent_dead") end
local equipped=a.get_inventory(defines.inventory.character_ammo);local recoverable={}
if equipped and equipped.valid then for i=1,#equipped do local stack=equipped[i]
 if stack.valid_for_read and stack.name=="firearm-magazine" and stack.prototype.type=="ammo" and stack.quality.name=="normal"
  and stack.prototype.magazine_size>0 and stack.ammo==stack.prototype.magazine_size then
  recoverable[#recoverable+1]={slot=i,item=stack.name,count=stack.count} end
end end
local rows={}
for _,e in pairs(s.find_entities_filtered{force=f}) do
 if e.valid and e.type~="character" then
  local r={name=e.name,type=e.type,unit_number=e.unit_number,position=pos(e.position),direction=e.direction,
    health=e.health,energy=e.energy,status=e.status,inventory={},fluids={}}
  for name,id in pairs(defines.entity_status) do if id==e.status then r.status_name=name;break end end
  local good,recipe=pcall(function() return e.get_recipe() end)
  if good and recipe then r.recipe=recipe.name end
  local has_burner,burner=pcall(function() return e.burner end)
  if has_burner and burner then r.remaining_burning_fuel=burner.remaining_burning_fuel end
  local has_network,network=pcall(function() return e.electric_network_id end)
  if has_network then r.electric_network_id=network end
  local has_connection,connected=pcall(function() return e.is_connected_to_electric_network() end)
  if has_connection then r.electric_network_connected=connected end
  for i=1,e.get_max_inventory_index() do
   local inv=e.get_inventory(i)
   if inv then for name,n in pairs(contents(inv)) do r.inventory[name]=(r.inventory[name] or 0)+n end end
  end
  local has_output,output=pcall(function() return e.get_output_inventory() end)
  if has_output and output then r.output_inventory=contents(output) end
  local has_products,products=pcall(function() return e.products_finished end)
  if has_products then r.products_finished=products end
  if e.type=="transport-belt" then
   r.belt_inventory={}
   for lane=1,2 do
    for _,row in pairs(e.get_transport_line(lane).get_contents()) do
     r.belt_inventory[row.name]=(r.belt_inventory[row.name] or 0)+row.count
    end
   end
  end
  local has_fluid,fluids=pcall(function() return e.get_fluid_contents() end)
  if has_fluid and fluids then r.fluids=fluids end
  if e.type=="rocket-silo" then
   r.rocket_parts=e.rocket_parts
   for name,id in pairs(defines.rocket_silo_status) do if e.rocket_silo_status==id then r.rocket_silo_status=name;break end end
  end
  rows[#rows+1]=r
 end
end
local techs={}
for name,t in pairs(f.technologies) do if t.researched then techs[name]=true end end
local enabled={}
for name,r in pairs(f.recipes) do if r.enabled then enabled[name]=true end end
local resources={}
for _,name in ipairs({'iron-ore','copper-ore','coal','stone','crude-oil'}) do
 local nearest=s.find_entities_filtered{position={0,0},radius=''' + str(float(radius)) + ''',name=name,limit=2000}
 local best=nil;local dist=math.huge
 for _,e in pairs(nearest) do local ds=e.position.x^2+e.position.y^2;if ds<dist then best=e;dist=ds end end
 if best then resources[name]={position=pos(best.position),amount=best.amount} end
end
local stats=f.get_item_production_statistics(s)
local production={}
for _,name in ipairs({'iron-plate','copper-plate','coal','automation-science-pack','logistic-science-pack','chemical-science-pack','electronic-circuit','rocket-part'}) do
 production[name]={produced=stats.get_input_count(name),consumed=stats.get_output_count(name)}
end
local current=f.current_research
return success{world_id=d.world_id,tick=game.tick,surface=s.name,position=pos(a.position),inventory=contents(a.get_main_inventory()),
 actor_unit_number=a.unit_number,equipped_ammo=contents(equipped),recoverable_equipped_ammo=recoverable,
 crafting_queue=a.crafting_queue,entities=rows,resources=resources,technologies=techs,enabled_recipes=enabled,
 research=current and current.name or nil,research_progress=f.research_progress,production=production,rockets_launched=f.rockets_launched,
 enemies=s.count_entities_filtered{position={0,0},radius=128,force="enemy",type={"unit","unit-spawner"}}}
''')
        if observation.get("ok"):
            from .deterministic_launch import OBSERVE_LAUNCH_LUA
            observation["launch"] = self.query(OBSERVE_LAUNCH_LUA)
        return observation

    def act(self, action: dict[str, Any]) -> dict[str, Any]:
        validate_mine_guard(action)
        validate_equipped_ammo_recovery(action)
        if "count" in action and (isinstance(action["count"], bool)
                or not isinstance(action["count"], int) or action["count"] < 1):
            raise ValueError("action count must be a positive integer")
        if action.get("type") == "bar" and (isinstance(action.get("slots"), bool)
                or not isinstance(action.get("slots"), int) or action["slots"] < 0):
            raise ValueError("bar slots must be a non-negative integer")
        if action.get("type") == "build_many":
            return self._record_action(action, self._build_many(action))
        encoded = json.dumps(json.dumps(action, separators=(",", ":")))
        body = 'local x=helpers.json_to_table(' + encoded + '); '
        body += 'if not a or not a.valid then return failure("agent_dead") end; '
        body += 'local inv=a.get_main_inventory(); '
        kind = action.get("type")
        if kind == "recover_equipped_ammo":
            body += RECOVER_EQUIPPED_AMMO_LUA
        elif kind == "craft":
            body += '''
local recipe=f.recipes[x.recipe]
if not recipe or not recipe.enabled then return failure("recipe_locked") end
if a.crafting_queue_size>0 then return success{status="waiting",reason="crafting_queue_busy"} end
local n=a.begin_crafting{count=x.count or 1,recipe=x.recipe}
if n<=0 then return failure("not_craftable") end
return success{status="running",started=n}
'''
        elif kind == "move":
            if self.backend == "character":
                body += '''
local dx=x.position.x-a.position.x;local dy=x.position.y-a.position.y
if dx*dx+dy*dy<1 then a.walking_state={walking=false};return success{status="succeeded"} end
local angle=math.atan2(dy,dx);local direction=(math.floor(angle/(math.pi/4)+0.5)*2+4)%16
a.walking_state={walking=true,direction=direction}
return success{status="running",position=pos(a.position)}
'''
            else:
                body += '''
local p=s.find_non_colliding_position("character",x.position,8,0.5)
if not p then return failure("position_blocked") end
if not a.teleport(p) then return failure("move_failed") end
return success{status="succeeded",position=pos(a.position)}
'''
        elif kind == "mine":
            body += '''
local e=target(x.position,x.name)
if not e then return failure("target_missing") end
if string.find(e.name,"crash%-site") or string.find(e.name,"wreck") then return failure("protected_artifact") end
'''
            body += GUARDED_MINE_LUA
            if self.backend == "character":
                body += '''
if not a.can_reach_entity(e) then return failure("out_of_reach") end
a.mining_state={mining=true,position=e.position}
return success{status="running"}
'''
            else:
                body += '''
if e.type=="resource" and e.prototype.resource_category~="basic-solid" then return failure("resource_requires_machine") end
if e.type=="resource" then
 local amount=math.min(x.count or 1,50,e.amount,inv.get_insertable_count(e.name))
 if amount<1 then return failure("inventory_full") end
 local inserted=inv.insert{name=e.name,count=amount}
 if inserted>=e.amount then e.destroy() else e.amount=e.amount-inserted end
 return success{status="succeeded",mined=inserted,assisted=true}
end
if not a.mine_entity(e) then return failure("mining_failed") end
return success{status="succeeded"}
'''
        elif kind == "build":
            body += '''
local existing=target(x.position,x.name)
if existing then
 local desired=x.direction or 0
 local axis_only=x.name=="steam-engine" or x.name=="steam-turbine"
 if existing.direction~=desired and not (axis_only and existing.direction%8==desired%8) then return failure("existing_direction_mismatch") end
 return success{status="succeeded",reused=true,unit_number=existing.unit_number}
end
local proto=prototypes.entity[x.name]
if not proto then return failure("unknown_entity") end
local item=x.item or x.name
local valid_item=false
for _,cost in pairs(proto.items_to_place_this or {}) do if cost.name==item then valid_item=true end end
if not valid_item then return failure("invalid_placement_item") end
if inv.get_item_count(item)<1 then return failure("missing_item:"..item) end
'''
            if self.backend == "character":
                body += 'if ((x.position.x-a.position.x)^2+(x.position.y-a.position.y)^2)^0.5>a.build_distance then return failure("out_of_reach") end; '
            body += '''
if not s.can_place_entity{name=x.name,position=x.position,direction=x.direction or 0,force=f} then return failure("placement_blocked") end
if inv.remove{name=item,count=1}~=1 then return failure("missing_item:"..item) end
local e=s.create_entity{name=x.name,position=x.position,direction=x.direction or 0,force=f,raise_built=true,player=a.player}
if not e then inv.insert{name=item,count=1};return failure("build_failed") end
return success{status="succeeded",unit_number=e.unit_number}
'''
        elif kind in {"insert", "take"}:
            body += '''
local e=target(x.position,x.name)
if not e then return failure("target_missing") end
'''
            if self.backend == "character":
                ground_exception = 'e.type~="item-entity" and ' if kind == "take" else ''
                body += 'if ' + ground_exception + 'not a.can_reach_entity(e) then return failure("out_of_reach") end; '
            if kind == "insert":
                body += '''
local n=math.min(x.count or 1,inv.get_item_count(x.item))
if n<=0 then return success{status="waiting",moved=0} end
local dest=x.inventory and e.get_inventory(defines.inventory[x.inventory]) or nil
local inserted=dest and dest.insert{name=x.item,count=n} or e.insert{name=x.item,count=n}
if inserted>0 then inv.remove{name=x.item,count=inserted} end
return success{status=inserted>0 and "succeeded" or "waiting",moved=inserted}
'''
            else:
                body += '''
if e.type=="item-entity" then
 local stack=e.stack
 if not stack or not stack.valid_for_read or stack.name~=x.item then return failure("ground_item_changed") end
 if stack.prototype.type~="item" then return failure("ground_stack_metadata_unsupported") end
 local quality=stack.quality.name
 if x.quality and x.quality~=quality then return failure("ground_item_quality_changed") end
'''
                if self.backend == "character":
                    body += 'if (e.position.x-a.position.x)^2+(e.position.y-a.position.y)^2>a.item_pickup_distance^2 then return failure("out_of_pickup_reach") end; '
                body += '''
 local amount=math.min(x.count or 1,stack.count)
 local moved=inv.insert{name=stack.name,quality=quality,count=amount}
 if moved>0 then
  if moved==stack.count then e.destroy() else stack.count=stack.count-moved end
 end
 return success{status=moved>0 and "succeeded" or "waiting",moved=moved,quality=quality}
end
local n=math.min(x.count or 1,inv.get_insertable_count(x.item))
if n<=0 then return success{status="waiting",moved=0} end
if e.type=="transport-belt" then
 local first=e.get_transport_line(1);local second=e.get_transport_line(2)
 local available=first.get_item_count(x.item)+second.get_item_count(x.item)
 if available<=0 then return success{status="waiting",moved=0} end
 local inserted=inv.insert{name=x.item,count=math.min(n,available)}
 if inserted<=0 then return success{status="waiting",moved=0} end
 local removed=first.remove_item{name=x.item,count=inserted}
 if removed<inserted then removed=removed+second.remove_item{name=x.item,count=inserted-removed} end
 if removed<inserted then inv.remove{name=x.item,count=inserted-removed} end
 return success{status=removed>0 and "succeeded" or "waiting",moved=removed}
end
local source=e.get_output_inventory()
if x.inventory then source=e.get_inventory(defines.inventory[x.inventory]) end
if not source then source=e.get_inventory(defines.inventory.chest) end
if not source then return failure("no_output_inventory") end
local removed=source.remove{name=x.item,count=n}
if removed<=0 then return success{status="waiting",moved=0} end
local inserted=inv.insert{name=x.item,count=removed}
if inserted<removed then source.insert{name=x.item,count=removed-inserted} end
return success{status=inserted>0 and "succeeded" or "waiting",moved=inserted}
'''
        elif kind == "bar":
            body += '''
local e=target(x.position,x.name)
if not e or e.type~="container" then return failure("container_missing") end
'''
            if self.backend == "character":
                body += 'if not a.can_reach_entity(e) then return failure("out_of_reach") end; '
            body += '''
local chest=e.get_inventory(defines.inventory.chest)
if not chest or not chest.supports_bar() then return failure("inventory_bar_unsupported") end
if x.slots>#chest then return failure("bar_exceeds_inventory_size") end
chest.set_bar(x.slots+1)
local bar=chest.get_bar()
return bar==x.slots+1 and success{status="succeeded",slots=bar-1} or failure("inventory_bar_rejected")
'''
        elif kind == "recipe":
            body += '''
local e=target(x.position,x.name)
if not e then return failure("target_missing") end
'''
            if self.backend == "character":
                body += 'if not a.can_reach_entity(e) then return failure("out_of_reach") end; '
            body += '''
local r=f.recipes[x.recipe]
if not r or not r.enabled then return failure("recipe_locked") end
local current=e.get_recipe()
if not current or current.name~=x.recipe then e.set_recipe(x.recipe) end
if x.direction~=nil then e.direction=x.direction end
current=e.get_recipe()
if not current or current.name~=x.recipe then return failure("recipe_rejected") end
if x.direction~=nil and e.direction~=x.direction then return failure("recipe_direction_rejected") end
return success{status="succeeded",direction=e.direction}
'''
        elif kind == "research":
            body += '''
local t=f.technologies[x.technology]
if not t then return failure("unknown_technology") end
if t.researched then return success{status="succeeded"} end
if t.prototype.research_trigger then return failure("natural_trigger_required") end
for _,prereq in pairs(t.prerequisites) do if not prereq.researched then return failure("prerequisite:"..prereq.name) end end
if f.current_research and f.current_research.name==t.name then return success{status="running"} end
if not f.add_research(t) then return failure("research_rejected") end
return success{status="running"}
'''
        elif kind == "stop":
            body += 'a.walking_state={walking=false};a.mining_state={mining=false};return success{status="succeeded"}'
        elif kind == "launch":
            from .deterministic_launch import LAUNCH_LUA
            body += LAUNCH_LUA
        else:
            raise ValueError(f"Unsupported deterministic action: {kind}")
        return self._record_action(action, self.query(body))

    def _record_action(self, action: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        with (self.cfg.log_dir / "actions.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"action": action, "result": result}, ensure_ascii=False) + "\n")
        return result

    def _build_many(self, action: dict[str, Any]) -> dict[str, Any]:
        """Amortize planning while retaining the ordinary build for every item."""
        if self.backend != "assisted":
            raise ValueError("build_many requires the assisted backend")
        rows = action.get("actions")
        if (set(action) - {"type", "actions", "reason"} or not isinstance(rows, list)
                or not 1 <= len(rows) <= BUILD_BATCH_LIMIT):
            raise ValueError("build_many requires 1 to 32 infrastructure build actions")
        if "reason" in action and not isinstance(action["reason"], str):
            raise ValueError("build_many reasons must be strings")
        # Validate the entire envelope before the first mutation, including later
        # children that would otherwise fail only after earlier items were spent.
        for row in rows:
            if (not isinstance(row, dict) or set(row) - {"type", "name", "item", "position", "direction", "reason"}
                    or row.get("type") != "build" or not isinstance(row.get("name"), str)
                    or row["name"] not in BUILD_BATCH_NAMES):
                raise ValueError("build_many accepts only ordinary belt and pole builds")
            position = row.get("position")
            if (not isinstance(position, dict) or set(position) != {"x", "y"}
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(value) for value in position.values())):
                raise ValueError("build_many requires finite x/y positions")
            direction = row.get("direction", 0)
            if isinstance(direction, bool) or not isinstance(direction, int) or direction not in (0, 4, 8, 12):
                raise ValueError("build_many requires cardinal build directions")
            if "item" in row and (not isinstance(row["item"], str) or not row["item"]):
                raise ValueError("build_many placement items must be nonempty names")
            if "reason" in row and not isinstance(row["reason"], str):
                raise ValueError("build_many reasons must be strings")
        results = []
        built = reused = 0
        for index, child in enumerate(rows):
            try:
                result = self.act(child)
            except (TimeoutError, RconError, OSError) as exc:
                # The last command may have executed. Keep the confirmed prefix
                # separate and stop the worker; a resume must observe the world.
                return {"ok": False, "status": "blocked", "reason": "build_batch_outcome_unknown",
                        "completed": index, "built": built, "reused": reused, "results": results,
                        "uncertain_index": index, "exception": str(exc), "exception_type": type(exc).__name__}
            results.append(result)
            if not result.get("ok") or result.get("status") != "succeeded":
                return {**result, "ok": False, "reason": result.get("reason", "build_batch_child_not_complete"),
                        "completed": index, "built": built, "reused": reused, "results": results,
                        "failed_index": index}
            if result.get("reused"):
                reused += 1
            else:
                built += 1
        return {"ok": True, "status": "succeeded", "completed": len(rows),
                "built": built, "reused": reused, "results": results}

    def save(self) -> None:
        with FactorioRconClient(self.cfg.rcon_host, self.cfg.rcon_port, self.cfg.rcon_password) as c:
            c.execute("/server-save", drain_seconds=0.4)
