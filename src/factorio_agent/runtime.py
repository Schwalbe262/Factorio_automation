"""Isolated official-mod runtime. No v1 world startup or strategic supervisor."""
from __future__ import annotations

from dataclasses import replace
import json
import math
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time

from factorio_ai.config import load_config
from factorio_ai.deterministic_game import DeterministicGame
from factorio_ai.deterministic_state import RunLock, _atomic_json
from factorio_ai.factorio import (build_create_no_mod_save_command,
    build_start_no_mod_server_command, no_mod_save_path, wait_for_rcon)


class WorldBoundGame(DeterministicGame):
    """Check world identity inside the same Lua command as every effect."""
    def __init__(self, cfg, expected_world: str | None = None):
        super().__init__(cfg)
        self.expected_world = expected_world

    def query(self, body: str) -> dict:
        if self.expected_world:
            body = ('if not d or d.world_id~=' + json.dumps(self.expected_world)
                    + ' then return failure("bound_world_changed") end; ' + body)
        return super().query(body)

    def act(self,action:dict)->dict:
        if action.get('type')=='repair_registered_infrastructure':
            from .infrastructure_repair import execute_repair_registered_infrastructure
            return self._record_action(action,execute_repair_registered_infrastructure(self,action))
        if action.get('type')=='build_and_arm_bastion_post':
            from .defense_deployment import execute_bastion_deployment
            return self._record_action(action,execute_bastion_deployment(self,action))
        if action.get('type')=='build_and_arm_bastion_group':
            from .defense_deployment import execute_bastion_group
            return self._record_action(action,execute_bastion_group(self,action))
        if action.get('type')=='start_nest_assault':
            from .nest_assault import execute_start_assault
            return self._record_action(action,execute_start_assault(self,action))
        if action.get('type')=='start_nest_sweep':
            from .nest_sweep import execute_start_sweep
            return self._record_action(action,execute_start_sweep(self,action))
        if action.get('type')=='take_factory_magazines':
            from .factory_ammo_transfer import execute_take_factory_magazines
            return self._record_action(action,execute_take_factory_magazines(self,action))
        if action.get('type')=='mall_stock_limit':
            from .construction_mall import execute_mall_stock_limit
            return self._record_action(action,execute_mall_stock_limit(self,action))
        if action.get('type')=='take_mall_stock':
            from .construction_mall import execute_take_mall_stock
            return self._record_action(action,execute_take_mall_stock(self,action))
        if action.get('type')=='configure_piercing_factory':
            from .piercing_ammo import execute_configure_piercing
            return self._record_action(action,execute_configure_piercing(self,action))
        if action.get('type')=='supply_piercing_input':
            from .piercing_ammo import execute_supply_piercing_input
            return self._record_action(action,execute_supply_piercing_input(self,action))
        if action.get('type')=='build_defense_replacement':
            from .defense_relocation import execute_build_replacement
            return self._record_action(action,execute_build_replacement(self,action))
        if action.get('type')=='retire_defense_post':
            from .defense_relocation import execute_retire_post
            return self._record_action(action,execute_retire_post(self,action))
        if action.get('type')=='reconcile_defense_post_retirement':
            from .defense_relocation import execute_reconcile_retirement
            return self._record_action(action,execute_reconcile_retirement(self,action))
        if action.get('type')=='build_furnace_gap':
            from .furnace_gap import execute_gap_build
            return self._record_action(action,execute_gap_build(self,action))
        if action.get('type')=='take_full_magazines':
            from .ammo_transfer import execute_take_full_magazines
            return self._record_action(action,execute_take_full_magazines(self,action))
        if action.get('type')=='configure_ammo_buffer':
            from .ammo_transfer import execute_configure_ammo_buffer
            return self._record_action(action,execute_configure_ammo_buffer(self,action))
        if action.get('type')=='deposit':
            from .storage_transfer import execute_deposit
            return self._record_action(action,execute_deposit(self,action))
        if action.get('type')=='insert' and {'expected_entity_unit','expected_entity_world_id'}.intersection(action):
            p=action.get('position');unit=action.get('expected_entity_unit');world=action.get('expected_entity_world_id')
            allowed={'type','name','position','item','inventory','count','expected_entity_unit','expected_entity_world_id','reason'}
            if (set(action)-allowed or action.get('name')!='gun-turret' or action.get('item')!='firearm-magazine'
                    or action.get('inventory')!='turret_ammo' or type(action.get('count')) is not int or not 1<=action['count']<=100
                    or type(unit) is not int or unit<1 or not isinstance(world,str) or not world
                    or not isinstance(p,dict) or set(p)!={'x','y'}
                    or any(type(p[k]) not in (int,float) or not math.isfinite(p[k]) for k in ('x','y'))):
                raise ValueError('guarded turret loading requires bounded normal ammo and exact world/unit identity')
            payload=json.dumps(json.dumps(action,separators=(',',':')))
            body='''
local x=helpers.json_to_table('''+payload+''')
if not d or d.world_id~=x.expected_entity_world_id then return failure("turret_world_changed") end
if not a or not a.valid or a.force~=f then return failure("agent_dead_or_force_changed") end
local e=target(x.position,"gun-turret")
if not e or e.unit_number~=x.expected_entity_unit or e.force~=f then return failure("owned_turret_changed") end
'''
            if self.backend=='character':body+='if not a.can_reach_entity(e) then return failure("out_of_reach") end; '
            body+='''
local inv=a.get_main_inventory();local dest=e.get_inventory(defines.inventory.turret_ammo)
if not dest then return failure("turret_ammo_inventory_missing") end
local size=prototypes.item[x.item].magazine_size;local moved=0
for i=1,#inv do
 if moved>=x.count then break end
 local stack=inv[i]
 if stack.valid_for_read and stack.name==x.item and stack.quality.name=="normal" then
  local available=stack.count-(stack.ammo<size and 1 or 0)
  local n=math.min(x.count-moved,available)
  if n>0 then
   local inserted=dest.insert{name=x.item,quality="normal",count=n}
   if inserted>0 then stack.count=stack.count-inserted;moved=moved+inserted end
  end
 end
end
return success{status=moved>0 and "succeeded" or "waiting",moved=moved,unit_number=e.unit_number}
'''
            return self._record_action(action,self.query(body))
        if action.get('type')=='take_many':
            from .construction_supply import execute_take_many
            return self._record_action(action,execute_take_many(self,action))
        if action.get('type')=='mine_many':
            from .mining_batch import execute_mine_many
            return self._record_action(action,execute_mine_many(self,action))
        if action.get('type')!='configure_splitter':return super().act(action)
        allowed={'type','name','position','input_priority','output_priority','expected_splitter_unit','expected_splitter_world_id','reason'}
        p=action.get('position');unit=action.get('expected_splitter_unit');world=action.get('expected_splitter_world_id')
        if (set(action)-allowed or action.get('name')!='splitter' or type(unit) is not int or unit<1
                or not isinstance(world,str) or not world or not isinstance(p,dict) or set(p)!={'x','y'}
                or any(type(p[a]) not in (int,float) or not math.isfinite(p[a]) for a in ('x','y'))
                or any(action.get(k) not in ('none','left','right') for k in ('input_priority','output_priority'))):
            raise ValueError('splitter configuration requires finite position, priorities and exact world/unit identity')
        payload=json.dumps(json.dumps(action,separators=(',',':')))
        body='''
local x=helpers.json_to_table('''+payload+''')
if not d or d.world_id~=x.expected_splitter_world_id then return failure("splitter_world_changed") end
if not a or not a.valid or a.force~=f then return failure("agent_dead_or_force_changed") end
local e=target(x.position,"splitter")
if not e or e.unit_number~=x.expected_splitter_unit or e.force~=f then return failure("owned_splitter_changed") end
'''
        if self.backend=='character':body+='if not a.can_reach_entity(e) then return failure("out_of_reach") end; '
        body+='''
e.splitter_input_priority=x.input_priority;e.splitter_output_priority=x.output_priority
return success{status="succeeded",unit_number=e.unit_number,input_priority=e.splitter_input_priority,output_priority=e.splitter_output_priority}
'''
        return self._record_action(action,self.query(body))


def configuration(root: Path):
    root = root.resolve()
    saved = json.loads((root / "run.json").read_text(encoding="utf-8"))
    if saved.get("schema_version") != 2 or saved.get("root") != str(root):
        raise ValueError("invalid or relocated v2 runtime")
    return replace(load_config(), runtime_dir=root, log_dir=root / "logs",
                   rcon_host="127.0.0.1", server_port=saved["server_port"],
                   rcon_port=saved["rcon_port"], rcon_password=saved["rcon_password"],
                   slurm_enabled=False)


def create(root: Path, *, seed: int, server_port: int, rcon_port: int) -> dict:
    root = root.resolve()
    if any(type(p) is not int or not 1024 <= p <= 65535 for p in (server_port, rcon_port)):
        raise ValueError("ports must be integers between 1024 and 65535")
    if server_port == rcon_port or type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("distinct ports and an unsigned 32-bit seed are required")
    root.mkdir(parents=True, exist_ok=True)
    with RunLock(root / "writer.lock"):
        if (root / "run.json").exists() or (root / "vanilla").exists():
            raise FileExistsError("runtime already exists; use start to resume")
        _atomic_json(root / "run.json", {"schema_version": 2, "root": str(root),
                     "seed": seed, "server_port": server_port, "rcon_port": rcon_port,
                     "rcon_password": secrets.token_urlsafe(24)})
        cfg = configuration(root)
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        target = no_mod_save_path(cfg)
        target.parent.mkdir(parents=True, exist_ok=True)
        command = build_create_no_mod_save_command(cfg, target)
        settings = Path(command[command.index("--map-gen-settings") + 1])
        # V1's helper writes non-default cliffs/enemies. Replace all overrides.
        _atomic_json(settings, {"seed": seed})
        with (cfg.log_dir / "create-world.log").open("wb") as output:
            subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, check=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"status": "created", "runtime": str(root), "seed": seed}


def start(root: Path) -> dict:
    cfg = configuration(root)
    with RunLock(root / "writer.lock"):
        if not no_mod_save_path(cfg).exists():
            raise FileNotFoundError("save creation did not complete; inspect create-world.log")
        # An unanswered connect is not proof that a process is dead. An exclusive
        # bind proves local availability without interpreting network timeouts.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if os.name == "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind((cfg.rcon_host, cfg.rcon_port))
        # Check UDP availability before starting a second world on the same port.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            if os.name == "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", cfg.server_port))
        command = build_start_no_mod_server_command(cfg)
        settings_path = Path(command[command.index("--server-settings") + 1])
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        settings.update(visibility={"public": False, "lan": False}, auto_pause=False,
                        name="Factorio Luna v2", max_players=2)
        _atomic_json(settings_path, settings)
        with (cfg.log_dir / "server-process.log").open("ab") as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        from factorio_ai.deterministic_character import _identity
        _atomic_json(root / "server.json", {"pid": process.pid,
                     "creation_time": _identity(process.pid), "started_at": time.time()})
        # On timeout leave the handle/world in place for inspection, never restart.
        wait_for_rcon(cfg, timeout_seconds=60)
        old_settings = root / "world-settings.json"
        expected = json.loads(old_settings.read_text(encoding="utf-8"))["world_id"] if old_settings.exists() else None
        game = WorldBoundGame(cfg, expected)
        initialized = game.initialize()
        if not initialized.get("ok"):
            raise RuntimeError(initialized.get("reason", "initialization_failed"))
        game.expected_world = initialized["world_id"]
        actual = game.query('''local m=game.map_settings;return {version=helpers.game_version,mods=script.active_mods,
            map_gen=s.map_gen_settings,enemy_expansion={enabled=m.enemy_expansion.enabled},
            pollution={enabled=m.pollution.enabled},evolution={time_factor=m.enemy_evolution.time_factor,
            destroy_factor=m.enemy_evolution.destroy_factor,pollution_factor=m.enemy_evolution.pollution_factor},
            speed=game.speed,world_id=d.world_id}''')
        _atomic_json(root / "world-settings.json", actual)
        if actual.get("version") != "2.1.20" or "space-age" not in actual.get("mods", {}):
            raise RuntimeError("acceptance requires installed Factorio/Space Age 2.1.20")
        game.save()
        return {"status": "running", "pid": process.pid, "world_id": initialized["world_id"],
                "version": actual["version"], "server_port": cfg.server_port}


def game_for(root: Path) -> DeterministicGame:
    saved = json.loads((root / "world-settings.json").read_text(encoding="utf-8"))
    return WorldBoundGame(configuration(root), saved["world_id"])


def connect_player(root: Path) -> dict:
    from factorio_ai.deterministic_character import ensure_crafting_player
    game = game_for(root)
    with RunLock(root / "writer.lock"):
        deadline = time.monotonic() + 125
        while True:
            result = ensure_crafting_player(game)
            if result.get("status") != "running" or time.monotonic() >= deadline:
                return result
            time.sleep(0.5)


def catalog_for(root: Path, game: DeterministicGame):
    from factorio_ai.world_catalog import WorldCatalog
    path = root / "catalog.json"
    # Recipe geometry is stable in a pinned run; per-job recipe availability is
    # always checked against a fresh observation by the executor.
    if path.exists():
        return WorldCatalog.from_dict(json.loads(path.read_text(encoding="utf-8")))
    value = WorldCatalog.from_game(game.query)
    _atomic_json(path, value.to_dict())
    return value


def electricity_snapshot(game) -> dict:
    return game.query('''
local networks={};local rows={}
for _,pole in pairs(s.find_entities_filtered{force=f,type="electric-pole"}) do
 local id=pole.electric_network_id
 if id and not networks[id] then
  networks[id]=true;local stats=pole.electric_network_statistics
  rows[#rows+1]={network_id=id,generation_kw=stats.get_flow_count{name="steam-engine",category="output",
   precision_index=defines.flow_precision_index.five_seconds}*60/1000,
   total_steam_energy_generated=stats.get_output_count("steam-engine")}
 end
end
return {ok=true,tick=game.tick,networks=rows,scope="steam engine networks, measured engine statistics"}
''')
