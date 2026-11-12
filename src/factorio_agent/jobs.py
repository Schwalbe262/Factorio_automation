"""Bounded, resumable model-selected jobs with a single world writer."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

from factorio_ai.deterministic_state import RunLock, _atomic_json


TERMINAL = {"succeeded", "blocked", "failed"}
CAPABILITIES = {
    "starter": {"parameters": {}, "requires": [],
                "effect": "drill-fed iron/copper, coal/stone starter cells; natural early unlocks"},
    "power": {"parameters": {}, "requires": ["starter"],
              "effect": "steam power with real self-fueling coal belt; verify 30 game seconds"},
    "starter_capacity": {"parameters": {"resource": "iron-ore|copper-ore|coal|stone", "cells": "integer 1..8, absolute target"},
                         "requires": ["starter"], "effect": "expand productive starter cells to fund construction faster"},
    "starter_research": {"parameters": {"technology": "automation|electric-mining-drill"}, "requires": ["power"],
                         "effect": "finite normally handcrafted startup science; natural research, maximum one budget per technology"},
    "regularize_power_layout": {"parameters": {}, "requires": ["starter"],
                               "effect": "recover obsolete unfinished coal feed and install validated straight trunks; no hot cutover"},
    "bootstrap_logistics": {"parameters": {}, "requires": ["power"],
                            "effect": "reserve a PCB cell, normally assemble finite red science, naturally unlock underground belts/splitters"},
    "electric_coal": {"parameters": {}, "requires": ["power"],
                      "effect": "normally craft electric drill, prebuild grid, reserve-backed cutover and real coal output onto retained trunk"},
    "mining_outpost": {"parameters": {"resource":"iron-ore|copper-ore|coal|stone","rate_per_minute":"finite number (0,960], absolute nominal target"},
                       "requires":["power"],"effect":"add aligned powered electric drills/collector banks with fixed output ports; never replace operating banks to expand"},
    "smelting_district": {"parameters":{},"requires":["power","bootstrap_logistics","mining_outpost"],
                         "effect":"reserve a PCB district, connect iron60/copper30 nominal banks and separate coal source with actual splitter/underground crossing, normally belt-feed furnaces"},
    "smelting_capacity": {"parameters":{"resource":"iron-ore|copper-ore","rate_per_minute":"finite number (0,450], absolute nominal target"},
                          "requires":["paired_smelting"],"effect":"add independent upper/lower furnaces inside reserved rows; consumer ports remain fixed"},
    "paired_smelting": {"parameters":{},"requires":["smelting_district"],
                        "effect":"replace cosmetic output splitting with independent upper/lower furnace rows directly feeding opposite lanes"},
    "paired_mining": {"parameters":{},"requires":["paired_smelting"],
                      "effect":"normally recover old ore starter cells; opposing electric drill pairs feed both collector lanes, fixed outputs and surveyed expansion columns"},
    "automation_science": {"parameters":{"rate_per_minute":"finite number (0,30], requested red-science rate with nominal headroom"},
                           "requires":["paired_smelting"],"effect":"reserve normal machine science and labs, connect raw plate belts and power; no actor-fed science inputs"},
    "logistic_science": {"parameters":{"rate_per_minute":"finite number (0,30], requested design rate; commissioning does not certify sustained throughput"},
                         "requires":["automation_science"],"effect":"normally belt-feed local gear/wire/circuit/belt/inserter cells and green science into a distinct lab belt lane; native research required"},
    "steam_expansion": {"parameters":{},"requires":["electric_coal"],
                        "effect":"normally add an engine pair with dedicated paired coal and shared water; preserve old plant and verify thirty game seconds of native generation"},
    "defense_perimeter": {"parameters":{},"requires":["starter"],
                          "effect":"normally build and arm strategic gun posts, native land/range/identity checks; refill known posts during later jobs; native gun research required"},
    "defense_watch": {"parameters":{},"requires":["defense_perimeter"],
                      "effect":"continuously inspect, normally restock and rebuild known defensive posts during a bounded worker session; yields waiting while armed so development pauses do not leave defenses unattended"},
    "defense_hardening": {"parameters":{},"requires":["defense_perimeter"],
                          "effect":"normally build mutually covering gun posts and open-rear wall bastions at an observed loss hotspot; preserve production reservations and service access"},
    "radar_network": {"parameters":{},"requires":["defense_perimeter"],
                      "effect":"normally build a defended radar behind the front with measured electrical headroom; prove native scanning"},
    "nest_sweep": {"parameters":{},"requires":["defense_hardening"],
                   "effect":"prestock one concentrated paid turret wave, retain armed rear cover and rapidly deploy multiple guns in actual spawner range despite reviewed worm fire; no incremental harassment or offensive rebuild loop"},
    "piercing_ammo": {"parameters":{},"requires":["ammo_production","defense_perimeter"],
                      "effect":"normally build a capped bulk ammunition factory with local yellow precursor assemblers and piercing assemblers; use real batch-fed ingredients and native machine production, never bulk character crafting"},
    "main_bus_factory": {"parameters":{},"requires":["paired_smelting","logistic_science","defense_perimeter"],
                         "effect":"replace separate raw plate corridors with shared northbound iron/copper buses and native priority taps; commission compact green then normally retire obsolete western hardware; no throughput certification"},
    "dedicated_materials": {"parameters":{"resource":"coal|stone"},"requires":["paired_mining"],
                           "effect":"normally build an independent opposing drill pair and publish proven material ports; coal distribution stays outside resources"},
    "chemical_coal_supply": {"parameters":{},"requires":["paired_mining"],
                             "effect":"normally build a separate defended opposing coal pair for chemistry, preserve existing coal consumers and publish one exact plastic-coal outlet without multiplying bank capacity"},
    "steam_coal_reroute": {"parameters":{},"requires":["steam_expansion"],
                          "effect":"prebuild an alternative off-resource coal corridor, normally cut over owned corners, verify loaded steam and recover obsolete transport"},
    "green_bus_reroute": {"parameters":{},"requires":["main_bus_factory"],
                         "effect":"move green bus taps and raw adapters around resources, prebuild the alternative and recover old owned transport only after fresh production proof"},
    "ore_transport_reroute": {"parameters":{},"requires":["paired_mining","paired_smelting"],
                             "effect":"move the iron delivery suffix off foreign resource patches after the final mining join; preserve mines and prove new raw flow and smelting before recovery"},
    "furnace_upgrade": {"parameters":{"resource":"iron-ore|copper-ore","rate_per_minute":"optional positive absolute nominal target"},
                        "requires":["paired_smelting"],"effect":"normally pay steel furnaces before exact owned replacements, preserve supply ports and prove real production; active mining must cover the target"},
    "steel_supply": {"parameters":{},"requires":["main_bus_factory"],
                     "effect":"normally install and prove a surplus-priority iron bus outlet at a bounded resource-free station"},
    "steel_production": {"parameters":{},"requires":["steel_supply","dedicated_materials"],
                         "effect":"normally build defended automatic iron/coal steel furnaces with fixed ports and prove native output"},
    "steel_coal_supply": {"parameters":{},"requires":["brick_production"],
                         "effect":"normally split the proven brick coal header into a shared steel outlet, preserve existing consumers and the total source capacity"},
    "brick_production": {"parameters":{},"requires":["dedicated_materials"],
                         "effect":"normally build defended automatic stone/coal brick furnaces with fixed ports and prove native output"},
    "oil_gathering": {"parameters":{},"requires":["steel_production"],
                      "effect":"normally build a defended powered pumpjack and real crude pipe outlet; naturally trigger native oil-processing research by extraction"},
    "water_gathering": {"parameters":{},"requires":["power"],
                        "effect":"normally build an independent defended shore intake with a typed water output while preserving existing steam-water pumps and their connections"},
    "basic_petroleum": {"parameters":{},"requires":["oil_gathering"],
                        "effect":"normally connect the exact proven crude source to a defended refinery using isolated bounded pipe segments; prove native petroleum production and publish a fixed gas outlet"},
    "engine_production": {"parameters":{},"requires":["steel_production","main_bus_factory"],
                          "effect":"normally build a defended compact engine district with a surplus iron tap, fixed steel supply and reserved machine expansion; prove real engines on its output"},
    "chemical_production": {"parameters":{"product":"plastic-bar|sulfur"},"requires":["basic_petroleum"],
                            "effect":"normally build compact defended chemical cells with exact declared fluid handoffs and paid typed ingredient connections; prove actual solid chemical output"},
    "gas_distribution": {"parameters":{},"requires":["basic_petroleum"],
                         "effect":"normally build a protected shared gas header before chemical consumers, preserve the original outlet and publish separate plastic/sulfur edges without multiplying source capacity"},
    "advanced_circuit_production": {"parameters":{},"requires":["chemical_production","main_bus_factory"],
                                    "effect":"normally build a compact advanced-circuit district with dedicated green/wire subcells, two fresh surplus bus taps and an exact proven plastic supply; preserve fixed ports and expansion space"},
    "chemical_science": {"parameters":{},"requires":["engine_production","advanced_circuit_production","chemical_production","automation_science"],
                         "effect":"normally build compact blue science from exact automatic engine, advanced-circuit and sulfur sources; preserve original labs and red/green feeds while adding a guarded south-side blue supply"},
    "ammo_production": {"parameters":{},"requires":["main_bus_factory"],
                       "effect":"normally build a defended automatic magazine factory from a critical iron bus tap, bound its real output buffer, then supply full normal magazines through guarded inventory transfers"},
    "legacy_coal_cleanup": {"parameters":{},"requires":["steam_expansion","dedicated_materials"],
                          "effect":"normally recover a proven isolated burner-to-coal-cache cell after automatic supply, loaded steam and inventory headroom checks; preserve all electric miners and power assets"},
    "research": {"parameters":{"technology":"catalog technology name"},"requires":["automation_science"],
                 "effect":"select normal native research; machine packs and belt-fed labs only; missing science tiers are explicit blockers"},
}


@dataclass(frozen=True)
class Request:
    key: str
    world_id: str
    observed_tick: int
    capability: str
    parameters: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not isinstance(self.key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", self.key):
            raise ValueError("key must contain 1..80 ASCII letters, digits, _ or -")
        if not isinstance(self.world_id, str) or not self.world_id:
            raise ValueError("world_id is required")
        if type(self.observed_tick) is not int or self.observed_tick < 0:
            raise ValueError("observed_tick must be a nonnegative integer")
        if self.capability not in CAPABILITIES:
            raise ValueError("capability_not_implemented: " + str(self.capability))
        if self.capability == "starter_capacity":
            from .mining import validate_parameters
            validate_parameters(self.parameters)
        elif self.capability == "starter_research":
            from .research import validate_parameters
            validate_parameters(self.parameters)
        elif self.capability == "mining_outpost":
            from .outposts import validate_parameters
            validate_parameters(self.parameters)
        elif self.capability == "smelting_capacity":
            from .smelting import validate_capacity
            validate_capacity(self.parameters)
        elif self.capability == "automation_science":
            from .science import validate_rate
            validate_rate(self.parameters)
        elif self.capability == "logistic_science":
            from .green_science import validate_rate
            validate_rate(self.parameters)
        elif self.capability == "dedicated_materials":
            from .material_supply import validate_material
            validate_material(self.parameters)
        elif self.capability == "furnace_upgrade":
            from .furnace_upgrade import validate_upgrade
            validate_upgrade(self.parameters)
        elif self.capability == "chemical_production":
            from .chemical_production import validate_chemical
            validate_chemical(self.parameters)
        elif self.capability == "research":
            from .normal_research import validate_parameters
            validate_parameters(self.parameters)
        elif not isinstance(self.parameters, dict) or self.parameters:
            raise ValueError("this capability accepts no parameters")

    def identity(self) -> dict:
        # Retrying a request after observing again retains the same effect ID.
        return {"key": self.key, "world_id": self.world_id,
                "capability": self.capability, "parameters": self.parameters}


class Ledger:
    def __init__(self, root: Path):
        self.root = Path(root)
        path = self.root / "jobs.json"
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "schema_version": 2, "jobs": {}, "world_id": None, "last_tick": 0}
        if self.data.get("schema_version") != 2:
            raise ValueError("unsupported jobs checkpoint")

    def save(self):
        _atomic_json(self.root / "jobs.json", self.data)

    def event(self, key: str, kind: str, **fields):
        with (self.root / "job-events.jsonl").open("a", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps({"key": key, "event": kind, "time": time.time(), **fields},
                                    ensure_ascii=False, allow_nan=False) + "\n")

    def sync(self, obs: dict):
        if not obs.get("ok"):
            raise RuntimeError(obs.get("reason", "observation_failed"))
        if self.data["world_id"] not in (None, obs["world_id"]):
            raise ValueError("world identity changed; use a new runtime")
        self.data["world_id"] = obs["world_id"]
        if obs["tick"] < self.data["last_tick"]:
            for job in self.data["jobs"].values():
                job.update(status="blocked", reason="world_rollback_requires_reconciliation", evidence={})
            self.data["rollback_tick"] = obs["tick"]
        self.data["last_tick"] = obs["tick"]
        self.save()


def summary(obs: dict, ledger: Ledger) -> dict:
    return {"world_id": obs["world_id"], "tick": obs["tick"],
            "inventory": obs.get("inventory", {}),
            "crafting_queue": obs.get("crafting_queue"),
            "entities": dict(Counter(e["name"] for e in obs.get("entities", []))),
            "production": obs.get("production", {}), "technologies": sorted(obs.get("technologies", {})),
            "research": obs.get("research"), "enemies_near_start": obs.get("enemies"),
            "research_progress": obs.get("research_progress"), "electricity": obs.get("electricity"),
            "production_rate_measurement":obs.get("production_rate_measurement"),
            "known_project_construction_budget":obs.get("known_project_construction_budget"),
            "belt_lane_observation":obs.get("belt_lane_observation"),
            "smelting_throughput_bounds":obs.get("smelting_throughput_bounds"),
            "factory_constraints":obs.get("factory_constraints"),
            "available_research":obs.get("available_research"),
            "capabilities": CAPABILITIES,
            "jobs": [{k: v for k, v in job.items() if k in
                      {"key", "capability", "status", "reason", "actions", "last_tick"}}
                     for job in ledger.data["jobs"].values()],
            "rocket_launch_verified": False}


def submit(root: Path, game, request: Request) -> dict:
    request.validate()
    with RunLock(root / "writer.lock"):
        obs = game.observe()
        ledger = Ledger(root)
        ledger.sync(obs)
        if request.world_id != obs["world_id"]:
            raise ValueError("request targets another world")
        old = ledger.data["jobs"].get(request.key)
        if old:
            if old["request"] != request.identity():
                raise ValueError("idempotency key already belongs to another request")
            return old
        if not 0 <= obs["tick"] - request.observed_tick <= 3600:
            raise ValueError("observation is stale or newer than the game; observe again")
        if any(j["status"] not in TERMINAL | {"paused"} for j in ledger.data["jobs"].values()):
            raise ValueError("another job is active; finish it before submitting another")
        if "rollback_tick" in ledger.data:
            raise ValueError("world rollback requires explicit reconciliation")
        completed = {j["capability"] for j in ledger.data["jobs"].values() if j["status"] == "succeeded"}
        if not set(CAPABILITIES[request.capability]["requires"]).issubset(completed):
            raise ValueError("capability prerequisites have not been verified")
        job = {**request.identity(), "request": request.identity(), "status": "pending",
               "reason": "model_requested", "created_tick": obs["tick"], "last_tick": obs["tick"],
               "created_at": time.time(), "actions": 0, "evidence": {}, "failures": 0,
               "progress_at_tick": obs["tick"], "progress_at_time": time.time()}
        ledger.data["jobs"][request.key] = job
        ledger.save()
        ledger.event(request.key, "submitted", capability=request.capability, tick=obs["tick"])
        return job


def control(root: Path, game, key: str, operation: str) -> dict:
    if operation not in {"pause", "resume", "reconcile", "retry"}:
        raise ValueError("unsupported job control")
    with RunLock(root / "writer.lock"):
        ledger = Ledger(root)
        ledger.sync(game.observe())
        job = ledger.data["jobs"].get(key)
        if job is None:
            raise ValueError("unknown job key")
        if operation == "pause":
            if job["status"] not in {"pending", "running", "waiting", "paused"}:
                raise ValueError("only an unfinished job can be paused")
            if job["status"] != "paused":
                job.update(paused_at_tick=ledger.data["last_tick"], paused_at_time=time.time())
            job.update(status="paused", reason="model_requested_pause")
        else:
            if any(j["key"] != key and j["status"] not in TERMINAL | {"paused"}
                   for j in ledger.data["jobs"].values()):
                raise ValueError("another job is active")
            if operation == "resume" and job["status"] != "paused":
                raise ValueError("only a paused job can be resumed")
            if operation == "retry":
                if (job['status']!='blocked' or job.get('pending_action')
                        or job['reason']=='action_outcome_unknown_reconcile_required' or 'rollback_tick' in ledger.data):
                    raise ValueError('only a known blocked outcome in the same world can be retried')
                now=time.time();blocked_time=job.get('blocked_at_time',now)
                if 'blocked_at_time' not in job:
                    path=root/'job-events.jsonl'
                    with path.open('rb') as events:
                        events.seek(max(0,path.stat().st_size-2*1024*1024));tail=events.read().splitlines()
                    for line in reversed(tail):
                        try:event=json.loads(line)
                        except (ValueError,UnicodeDecodeError):continue
                        if event.get('key')==key and event.get('event')=='terminal' and event.get('status')=='blocked':
                            blocked_time=event['time'];break
                ticks=max(0,ledger.data['last_tick']-job.get('blocked_at_tick',job['last_tick']))
                seconds=max(0,now-blocked_time)
                job['paused_ticks']=job.get('paused_ticks',0)+ticks
                job['paused_seconds']=job.get('paused_seconds',0)+seconds
                job['progress_at_tick']+=ticks;job['progress_at_time']+=seconds
                ledger.event(key,'blocked_interval_excluded',tick=ledger.data['last_tick'],ticks=ticks,seconds=seconds)
                job.pop('blocked_at_tick',None);job.pop('blocked_at_time',None)
                ledger.event(key,'retry_requested',old_reason=job['reason'],tick=ledger.data['last_tick'])
            if operation == "resume":
                if "paused_at_tick" not in job:
                    # Upgrade receipts created before pause-budget accounting.
                    # Search only the matching event; never replay any action.
                    with (root / "job-events.jsonl").open(encoding="utf-8") as events:
                        for line in events:
                            event = json.loads(line)
                            if event.get("key") == key and event.get("event") == "pause":
                                job.update(paused_at_tick=event["tick"], paused_at_time=event["time"])
                pause_ticks = max(0, ledger.data["last_tick"] - job.pop("paused_at_tick", ledger.data["last_tick"]))
                pause_seconds = max(0, time.time() - job.pop("paused_at_time", time.time()))
                job["paused_ticks"] = job.get("paused_ticks", 0) + pause_ticks
                job["paused_seconds"] = job.get("paused_seconds", 0) + pause_seconds
                job["progress_at_tick"] += pause_ticks
                job["progress_at_time"] += pause_seconds
            if operation == "reconcile":
                if (job["reason"] != "action_outcome_unknown_reconcile_required"
                        or "rollback_tick" in ledger.data):
                    raise ValueError("only an uncertain action in the same live world can be reconciled")
                # These registered executors replan from observed inventories,
                # entity identities and crafting queues; no old action is replayed.
                ledger.event(key, "unknown_action_reconciled", old_intent=job.pop("pending_action", None),
                             tick=ledger.data["last_tick"])
            job.update(status="waiting", reason="fresh_observation_before_replanning")
        ledger.save()
        ledger.event(key, operation, status=job["status"], tick=ledger.data["last_tick"])
        return job


class StarterExecutors:
    """Explicit temporary v1 executor reuse, with no v1 strategic supervisor."""
    def __init__(self, game, catalog):
        self.game,self.catalog=game,catalog
        from .construction_supply import V2Bootstrap
        from .power_layout import V2FactoryBuilder
        self.bootstrap = V2Bootstrap(game, catalog)
        self.builder = V2FactoryBuilder(game, self.bootstrap, catalog)
        from .mining import StarterCapacity
        from .research import StarterResearch
        self.capacity = StarterCapacity(game, self.bootstrap)
        self.research = StarterResearch(game, self.bootstrap, catalog)
        from .power_layout import RegularizePower
        self.power_layout = RegularizePower(game, self.builder)
        from .industry import BootstrapLogistics
        self.industry = BootstrapLogistics(game,self.bootstrap,self.builder,catalog)
        from .electric_mining import ElectricCoal
        self.electric_coal=ElectricCoal(game,self.bootstrap,self.builder)
        from .outposts import MiningOutposts
        self.outposts=MiningOutposts(game,self.bootstrap,self.builder)
        from .smelting import SmeltingDistrict
        self.smelting=SmeltingDistrict(game,self.bootstrap,self.builder,catalog)
        from .paired_smelting import PairedSmelting
        self.paired=PairedSmelting(game,self.builder,catalog,self.smelting)
        from .paired_mining import PairedMining
        self.paired_mining=PairedMining(game,self.bootstrap,self.builder,catalog,self.outposts,self.smelting)
        from .science import AutomaticScience
        self.science=AutomaticScience(game,self.builder,catalog)
        from .green_science import LogisticScience
        self.green=LogisticScience(game,self.builder,catalog,self.smelting,self.science)
        from .steam_expansion import SteamExpansion
        self.steam_expansion=SteamExpansion(game,self.builder,catalog,self.outposts)
        from .defense_perimeter import DefensePerimeter
        self.perimeter=DefensePerimeter(game,self.bootstrap,catalog)
        from .defense_repairs import DefenseRepairs
        self.defense_repairs=DefenseRepairs(game,self.bootstrap,self.perimeter)
        from .defense_hardening import DefenseHardening
        self.hardening=DefenseHardening(game,self.bootstrap,self.builder,catalog,self.perimeter)
        self.hardening.wall_repair=self.defense_repairs.wall_action
        from .bastion_maintenance import BastionMaintenance
        self.bastion_maintenance=BastionMaintenance(game,self.bootstrap,catalog,self.perimeter,self.hardening)
        from .nest_assault import NestAssault
        self.nest_assault=NestAssault(game,self.bootstrap,self.builder,catalog,self.perimeter)
        self.nest_sweep=None
        self.piercing_ammo=None
        self.piercing_path=Path(game.cfg.runtime_dir)/'piercing-ammo.json'
        from .radar_network import RadarNetwork
        self.radars=RadarNetwork(game,self.builder,catalog,self.perimeter)
        self.outposts.perimeter=self.perimeter
        from .bus_factory import BusFactory
        self.bus_factory=BusFactory(game,self.builder,catalog,self.smelting,self.science,self.green)
        from .inventory_storage import InventoryStorage
        self.storage=InventoryStorage(game,catalog)
        from .material_supply import DedicatedMaterials
        self.material_supply=DedicatedMaterials(game,self.builder,catalog,self.outposts,perimeter=self.perimeter)
        self.chemical_coal=DedicatedMaterials(game,self.builder,catalog,self.outposts,perimeter=self.perimeter,
                                             purpose='chemistry')
        from .resource_reroute import SteamCoalReroute
        self.steam_coal_reroute=SteamCoalReroute(game,self.builder,catalog,self.steam_expansion,self.outposts)
        from .green_bus_reroute import GreenBusReroute
        self.green_bus_reroute=GreenBusReroute(game,self.builder,catalog,self.bus_factory,self.science,self.green,self.smelting)
        from .legacy_coal_cleanup import LegacyCoalCleanup
        self.legacy_coal_cleanup=LegacyCoalCleanup(game,self.bootstrap,self.builder,catalog,self.storage,self.steam_expansion,self.outposts)
        from .ore_transport_reroute import OreTransportReroute
        self.ore_transport_reroute=OreTransportReroute(game,self.builder,catalog,self.smelting,self.outposts,
                                                     bus_factory=self.bus_factory,green=self.green)
        from .furnace_upgrade import FurnaceUpgrade
        self.furnace_upgrade=FurnaceUpgrade(game,self.builder,catalog,self.smelting,self.paired,outposts=self.outposts)
        from .industrial import IndustrialOperations
        self.industrial_operations=IndustrialOperations(game,self.builder,catalog,self.perimeter,
                                                        bus_factory=self.bus_factory,science=self.science,green=self.green)
        from .shared_coal_tap import SharedCoalTap
        self.steel_coal_supply=SharedCoalTap(game,self.builder,catalog,self.industrial_operations.brick,
                                           industrial=self.industrial_operations)
        from .oil_gathering import OilGathering
        self.oil_gathering=OilGathering(game,self.builder,catalog,self.perimeter)
        from .water_gathering import WaterGathering
        self.water=WaterGathering(game,self.builder,catalog,self.perimeter)
        from .petroleum import BasicPetroleum
        self.petroleum=BasicPetroleum(game,self.builder,catalog,self.perimeter)
        from .ammo_production import AmmoProduction
        self.ammo=AmmoProduction(game,self.builder,catalog,self.perimeter,
                                bus_factory=self.bus_factory,science=self.science,green=self.green)
        self.perimeter.ammo_source=self.ammo
        from .engine_production import EngineProduction
        self.engines=EngineProduction(game,self.builder,catalog,self.perimeter,
                                     bus_factory=self.bus_factory,science=self.science,green=self.green,
                                     steel=self.industrial_operations.steel)
        from .defense_relocation import DefenseRelocation
        self.engines.relocation=DefenseRelocation(game,self.bootstrap,self.builder,catalog,self.perimeter,self.engines)
        from .chemical_production import ChemicalProduction
        self.chemicals=ChemicalProduction(game,self.builder,catalog,self.perimeter)
        from .gas_distribution import GasDistribution
        self.gas_distribution=GasDistribution(game,self.builder,catalog,self.perimeter,
                                             petroleum=self.petroleum,chemical=self.chemicals)
        from .advanced_circuits import AdvancedCircuitProduction
        self.advanced_circuits=AdvancedCircuitProduction(game,self.builder,catalog,self.perimeter,
                                                        bus_factory=self.bus_factory,science=self.science,
                                                        green=self.green,chemical=self.chemicals)
        from .chemical_science import ChemicalScience
        self.blue=ChemicalScience(game,self.builder,catalog,self.perimeter,engine=self.engines,
                                  advanced=self.advanced_circuits,chemical=self.chemicals,
                                  science=self.science,green=self.green,bus_factory=self.bus_factory)
        from .normal_research import NormalResearch
        self.normal_research=NormalResearch(game,catalog,self.science,self.green,blue=self.blue)
        ledger=Ledger(Path(game.cfg.runtime_dir)).data
        self.industrial_mode=any(j['capability']=='smelting_district' and j['status']=='succeeded'
                                 for j in ledger['jobs'].values())

    def _piercing_factory(self):
        if self.piercing_ammo is None:
            from .piercing_ammo import PiercingAmmo
            self.piercing_ammo=PiercingAmmo(self.game,self.builder,self.catalog,self.perimeter)
        return self.piercing_ammo

    def _piercing_upkeep(self,obs):
        factory=getattr(self,'piercing_ammo',None)
        if factory is None:
            path=getattr(self,'piercing_path',None)
            if path is None or not path.exists():return None
            factory=self._piercing_factory()
        return factory.upkeep(obs) if getattr(factory,'commissioned',False) else None

    def starter_observation(self,obs):
        return {**obs,'entities':[e for e in obs.get('entities',[])
            if not self.builder.owns_automated_burner(e) and not self.smelting.owns_furnace(e,obs)
            and not (getattr(self,'paired',None) and self.paired.owns_furnace(e,obs))
            and not (getattr(self,'paired_mining',None) and self.paired_mining.retires_legacy(e,obs))
            and not (getattr(self,'legacy_coal_cleanup',None) and self.legacy_coal_cleanup.retires_legacy(e,obs))
            and not (getattr(self,'steam_expansion',None) and self.steam_expansion.owns_burner(e,obs))]}

    def starter_upkeep(self,obs):
        if getattr(self,'storage',None):
            storage=self.storage.step(obs)
            if storage:return storage
        if getattr(self,'bastion_maintenance',None):
            bastion=self.bastion_maintenance.step(obs)
            if bastion:return bastion
        if getattr(self,'perimeter',None):
            defense=self.perimeter.upkeep(obs)
            if defense:return defense
        if getattr(self,'defense_repairs',None):
            if getattr(self,'hardening',None):
                wall=self.hardening.upkeep(obs)
                if wall:return wall
            repair=self.defense_repairs.next_action(obs)
            if repair:return repair
        filtered=self.starter_observation(obs)
        if not getattr(self,'industrial_mode',False):return self.bootstrap.next_action(filtered)
        if self.smelting.state.get('world_id')!=obs['world_id']:
            return {'status':'blocked','reason':'industrial_supply_world_changed'}
        required={'steam-power','electronics','automation-science-pack'}
        if not required.issubset(obs.get('technologies',{})):
            return {'status':'blocked','reason':'industrial_natural_unlocks_missing'}
        upkeep=self.bootstrap.maintain_fuel(filtered)
        return upkeep or {'status':'succeeded','reason':'industrial supplies retain ordinary starter fuel upkeep'}

    def step(self, capability: str, obs: dict, parameters: dict | None = None) -> dict:
        from factorio_ai.deterministic_repair_control import pending_repair
        cleanup=pending_repair(obs)
        if cleanup:return cleanup
        if capability == 'nest_assault':
            return {'status':'blocked','reason':'staged_assault_retired_use_concentrated_nest_sweep'}
        if capability == 'radar_network':
            self.builder._sync(obs)
            result=self.radars.step(obs,parameters or {})
            if result.get('type')or result.get('status')!='waiting':return result
            restored=self.bastion_maintenance.step(obs)
            if restored and(restored.get('type')or restored.get('status')=='blocked'):return restored
            upkeep=self.perimeter.upkeep(obs)
            if upkeep and(upkeep.get('type')or upkeep.get('status')=='blocked'):return upkeep
            return result
        if capability == 'piercing_ammo':
            self.builder._sync(obs)
            result=self._piercing_factory().step(obs)
            if result.get('type')or result.get('status')!='waiting':return result
            restored=self.bastion_maintenance.step(obs)
            if restored and(restored.get('type')or restored.get('status')=='blocked'):return restored
            upkeep=self.perimeter.upkeep(obs)
            if upkeep and(upkeep.get('type')or upkeep.get('status')=='blocked'):return upkeep
            return result
        if capability == 'nest_sweep':
            self.builder._sync(obs)
            if self.nest_sweep is None:
                from .nest_sweep import NestSweep
                self.nest_sweep=NestSweep(self.game,self.bootstrap,self.builder,self.catalog,self.perimeter,
                    ammo_source=self._piercing_factory())
            restored=self.bastion_maintenance.step(obs)
            if restored and(restored.get('type')or restored.get('status')=='blocked'):return restored
            # Collect machine-produced raid stock before optional base topoffs.
            # Service factory inputs while stocking, then isolate committed combat.
            result=self.nest_sweep.step(obs)
            if result.get('type')or result.get('status')!='waiting':return result
            state=getattr(self.nest_sweep,'state',{})
            if not state.get('started_tick') and not state.get('funded_geometry_refresh_tick'):
                supplied=self._piercing_upkeep(obs)
                if supplied and(supplied.get('type')or supplied.get('status')=='blocked'):return supplied
            upkeep=self.perimeter.upkeep(obs)
            if upkeep and(upkeep.get('type')or upkeep.get('status')=='blocked'):return upkeep
            return result
        if capability == "defense_hardening":
            self.builder._sync(obs)
            return self.hardening.step(obs,parameters or {})
        if capability == "defense_watch":
            self.builder._sync(obs)
            from .upgrades import next_upgrade
            upgrade=next_upgrade(obs,self.normal_research)
            if upgrade:return upgrade
            bastion=self.bastion_maintenance.step(obs)
            if bastion:return bastion
            result=self.perimeter.step(obs)
            if result.get('status')=='succeeded':
                wall=self.hardening.upkeep(obs)
                if wall:return wall
                repair=self.defense_repairs.next_action(obs)
                if repair:return repair
                supplied=self._piercing_upkeep(obs)
                if supplied and supplied.get('type'):return supplied
                return {'status':'waiting','reason':'watching armed defenses and replenishing normal ammunition',
                        'evidence':{**result.get('evidence',{}),**({'piercing_factory':supplied} if supplied else {})}}
            return result
        if capability == "starter":
            self.builder._sync(obs)
            return self.starter_upkeep(obs)
        if capability == "regularize_power_layout":
            self.builder._sync(obs)
            fuel_obs = self.starter_observation(obs)
            fuel = self.bootstrap.maintain_fuel(fuel_obs)
            if fuel is not None:
                return fuel
            return self.power_layout.step(obs)
        # Retain starter supplies during power construction without handfeeding
        # burners now owned by the automatic power plan.
        self.builder._sync(obs)
        filtered = self.starter_observation(obs)
        result = self.starter_upkeep(obs)
        if result.get("status") != "succeeded":
            return result
        if capability not in ("research","defense_perimeter"):
            from .upgrades import next_upgrade
            upgrade=next_upgrade(obs,self.normal_research)
            if upgrade:return upgrade
        if capability == "starter_capacity":
            return self.capacity.step(filtered, parameters or {})
        if capability == "power":
            return self.builder.ensure_power(obs)
        if capability == "starter_research":
            result = self.builder.ensure_power(obs)
            if result.get("status") != "succeeded":
                return result
            return self.research.step(obs, parameters or {})
        if capability == "bootstrap_logistics":
            result=self.builder.ensure_power(obs)
            if result.get("status")!="succeeded":return result
            return self.industry.step(obs)
        if capability == "electric_coal":
            # During cutover the old source may be absent for one observed
            # action. Do not run ensure_power here and rebuild that old drill.
            return self.electric_coal.step(obs)
        if capability == "mining_outpost":
            result=self.builder.ensure_power(obs)
            if result.get("status")!="succeeded":return result
            return self.outposts.step(obs,parameters or {})
        if capability == "smelting_district":
            result=self.builder.ensure_power(obs)
            if result.get("status")!="succeeded":return result
            return self.smelting.step(obs)
        if capability == "smelting_capacity":
            result=self.builder.ensure_power(obs)
            if result.get("status")!="succeeded":return result
            return self.smelting.capacity_step(obs,parameters or {})
        if capability == "paired_smelting":
            result=self.builder.ensure_power(obs)
            if result.get("status")!="succeeded":return result
            return self.paired.step(obs)
        if capability == "paired_mining":
            return self.paired_mining.step(obs)
        if capability == "automation_science":
            result=self.builder.ensure_power(obs)
            if result.get("status")!="succeeded":return result
            return self.science.step(obs,parameters or {})
        if capability == "research":
            return self.normal_research.step(obs,parameters or {})
        if capability == "logistic_science":
            return self.green.step(obs,parameters or {})
        if capability == "steam_expansion":
            return self.steam_expansion.step(obs)
        if capability == "defense_perimeter":
            return self.perimeter.step(obs)
        if capability == "main_bus_factory":
            return self.bus_factory.step(obs,parameters or {})
        if capability == "dedicated_materials":
            return self.material_supply.step(obs,parameters or {})
        if capability == "chemical_coal_supply":
            return self.chemical_coal.step(obs,{'resource':'coal'})
        if capability == "steam_coal_reroute":
            return self.steam_coal_reroute.step(obs,parameters or {})
        if capability == "green_bus_reroute":
            return self.green_bus_reroute.step(obs,parameters or {})
        if capability == "legacy_coal_cleanup":
            return self.legacy_coal_cleanup.step(obs,parameters or {})
        if capability == "ore_transport_reroute":
            return self.ore_transport_reroute.step(obs,parameters or {})
        if capability == "furnace_upgrade":
            return self.furnace_upgrade.step(obs,parameters or {})
        if capability == "steel_supply":
            return self.industrial_operations.steel_supply(obs)
        if capability == "steel_production":
            return self.industrial_operations.steel_production(obs)
        if capability == "steel_coal_supply":
            return self.steel_coal_supply.step(obs)
        if capability == "brick_production":
            return self.industrial_operations.brick_production(obs)
        if capability == "oil_gathering":
            return self.oil_gathering.step(obs)
        if capability == "water_gathering":
            return self.water.step(obs)
        if capability == "basic_petroleum":
            return self.petroleum.step(obs)
        if capability == "ammo_production":
            return self.ammo.step(obs)
        if capability == "engine_production":
            return self.engines.step(obs)
        if capability == "chemical_production":
            return self.chemicals.step(obs,parameters or {})
        if capability == "gas_distribution":
            return self.gas_distribution.step(obs)
        if capability == "advanced_circuit_production":
            return self.advanced_circuits.step(obs)
        if capability == "chemical_science":
            return self.blue.step(obs)
        raise ValueError("no executor for capability: " + capability)

    def proof(self, capability: str, obs: dict, result: dict, parameters: dict | None = None) -> dict | None:
        if capability == "starter_capacity":
            return self.capacity.proof(obs, parameters or {})
        if capability == "starter_research":
            return self.research.proof(obs, parameters or {})
        if capability == "regularize_power_layout":
            return self.power_layout.proof(obs)
        if capability == "bootstrap_logistics":
            return self.industry.proof(obs)
        if capability == "electric_coal":
            return self.electric_coal.proof(obs)
        if capability == "mining_outpost":
            return self.outposts.proof(obs,parameters or {})
        if capability in {"smelting_district","smelting_capacity"}:
            return self.smelting.proof(obs)
        if capability == "paired_smelting":
            return self.paired.proof(obs)
        if capability == "paired_mining":
            return self.paired_mining.proof(obs)
        if capability == "automation_science":
            return self.science.proof(obs)
        if capability == "research":
            return self.normal_research.proof(obs,parameters or {})
        if capability == "logistic_science":
            return self.green.proof(obs)
        if capability == "steam_expansion":
            return self.steam_expansion.proof(obs)
        if capability == "defense_perimeter":
            return self.perimeter.proof(obs)
        if capability == "defense_hardening":
            return self.hardening.proof(obs)
        if capability == "radar_network":
            return self.radars.proof(obs)
        if capability == 'nest_assault':
            return self.nest_assault.proof(obs)
        if capability == 'nest_sweep':
            return self.nest_sweep.proof(obs) if self.nest_sweep is not None else None
        if capability == 'piercing_ammo':
            return self.piercing_ammo.proof(obs) if self.piercing_ammo is not None else None
        if capability == "main_bus_factory":
            return self.bus_factory.proof(obs)
        if capability == "dedicated_materials":
            return self.material_supply.proof(obs,parameters or {})
        if capability == "chemical_coal_supply":
            return self.chemical_coal.proof(obs,{'resource':'coal'})
        if capability == "steam_coal_reroute":
            return self.steam_coal_reroute.proof(obs)
        if capability == "green_bus_reroute":
            return self.green_bus_reroute.proof(obs)
        if capability == "legacy_coal_cleanup":
            return self.legacy_coal_cleanup.proof(obs)
        if capability == "ore_transport_reroute":
            return self.ore_transport_reroute.proof(obs)
        if capability == "furnace_upgrade":
            proof=self.furnace_upgrade.proof(obs)
            if proof:
                from .capacity_profiles import refresh_bus_capacity
                profile=refresh_bus_capacity(self.game,self.catalog,self.bus_factory)
                if profile is not None and not profile.get('ok'):
                    return None
                proof={**proof,'bus_capacity_profile':profile}
            return proof
        if capability == "steel_supply":
            return self.industrial_operations.steel_supply_proof(obs)
        if capability == "steel_production":
            return self.industrial_operations.steel_production_proof(obs)
        if capability == "steel_coal_supply":
            return self.steel_coal_supply.proof(obs)
        if capability == "brick_production":
            return self.industrial_operations.brick_production_proof(obs)
        if capability == "oil_gathering":
            return self.oil_gathering.proof(obs)
        if capability == "water_gathering":
            return self.water.proof(obs)
        if capability == "basic_petroleum":
            return self.petroleum.proof(obs)
        if capability == "ammo_production":
            return self.ammo.proof(obs)
        if capability == "engine_production":
            return self.engines.proof(obs)
        if capability == "chemical_production":
            return self.chemicals.proof(obs,parameters or {})
        if capability == "gas_distribution":
            return self.gas_distribution.proof(obs)
        if capability == "advanced_circuit_production":
            return self.advanced_circuits.proof(obs)
        if capability == "chemical_science":
            return self.blue.proof(obs)
        produced = obs.get("production", {})
        if not all(produced.get(name, {}).get("produced", 0) > 0 for name in ("iron-plate", "copper-plate")):
            return None
        if capability == "starter":
            techs = {"steam-power", "electronics", "automation-science-pack"}
            if not techs.issubset(obs.get("technologies", {})):
                return None
            return {"tick": obs["tick"], "natural_technologies": sorted(techs),
                    "machine_production": {n: produced[n] for n in ("iron-plate", "copper-plate")},
                    **result.get("evidence", {})}
        evidence = result.get("evidence", {})
        if evidence.get("flow_verified") and evidence.get("verification_ticks", 0) >= 1800:
            return {"tick": obs["tick"], **evidence}
        return None


def progress_key(obs: dict, capability: str) -> str:
    # Relevant effects only: unrelated inventory churn must not hide a stall.
    value = {"production": {n: obs.get("production", {}).get(n) for n in ("iron-plate", "copper-plate", "coal", "automation-science-pack", "logistic-science-pack", "chemical-science-pack")},
             "techs": [n for n in ("steam-power", "electronics", "automation-science-pack", "automation", "electric-mining-drill", "logistics")
                       if obs.get("technologies", {}).get(n)],
             "research":obs.get("research"),"research_progress":obs.get("research_progress"),
             "entities": [(e.get("unit_number"), e.get("name")) for e in obs.get("entities", [])]}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def run_slice(root: Path, game, executors, key: str, *, seconds: float = 25) -> dict:
    if not isinstance(seconds, (int, float)) or not 0 < seconds <= 50:
        raise ValueError("slice duration must be greater than zero and at most 50 seconds")
    with RunLock(root / "writer.lock"):
        ledger = Ledger(root)
        if key not in ledger.data["jobs"]:
            raise ValueError("unknown job key")
        job = ledger.data["jobs"][key]
        deadline = time.monotonic() + seconds
        while True:
            obs = game.observe()
            ledger.sync(obs)
            if job["status"] in TERMINAL | {"paused"}:
                # A terminal receipt is historical; callers must not assume it
                # proves present production after damage or world rollback.
                return job
            job["last_tick"] = obs["tick"]
            if job.get("pending_action"):
                # A prior process may have died after sending a mutation. Stop
                # rather than replaying an action whose outcome is unknown.
                job.update(status="blocked", reason="action_outcome_unknown_reconcile_required")
                ledger.save()
                return job
            if (obs["tick"] - job["created_tick"] - job.get("paused_ticks", 0) > 108000
                    or time.time() - job["created_at"] - job.get("paused_seconds", 0) > 3600):
                job.update(status="blocked", reason="job_deadline_exceeded")
                ledger.save()
                return job
            if (root / "stop.json").exists():
                job.update(status="waiting", reason="operator_stop_requested")
                ledger.save()
                return job
            progress = progress_key(obs, job["capability"])
            if progress != job.get("progress"):
                job.update(progress=progress, progress_at_tick=obs["tick"], progress_at_time=time.time())
            if obs["tick"] - job["progress_at_tick"] > 18000:
                job.update(status="blocked", reason="objective_progress_stalled")
                ledger.save()
                return job
            started = time.perf_counter()
            result = executors.step(job["capability"], obs, job["parameters"])
            job["planning_seconds"] = job.get("planning_seconds", 0) + time.perf_counter() - started
            if result.get("type"):
                job.update(status="running", reason=result.get("reason", result["type"]),
                           pending_action=result)
                ledger.save()
                ledger.event(key, "action_intent", tick=obs["tick"], action=result)
                started = time.perf_counter()
                try:
                    receipt = game.act(result)
                    if receipt.get('reason') in ('build_batch_outcome_unknown','mine_batch_outcome_unknown','storage_transfer_outcome_unknown'):
                        job['uncertain_batch_receipt']=receipt
                        raise RuntimeError('batch outcome unknown; observe and reconcile before continuing')
                except Exception:
                    # Durable intent survives connection errors; explicit
                    # reconciliation is necessary before further mutations.
                    job.update(status="blocked", reason="action_outcome_unknown_reconcile_required")
                    ledger.save()
                    raise
                job["execution_seconds"] = job.get("execution_seconds", 0) + time.perf_counter() - started
                job.pop("pending_action")
                job["actions"] += 1
                failure = None if receipt.get("ok") else receipt.get("reason", "action_failed")
                job["failures"] = job["failures"] + 1 if failure == job.get("last_failure") and failure else int(bool(failure))
                job["last_failure"] = failure
                if job["failures"] >= 3:
                    job.update(status="blocked", reason="repeated_executor_failure:" + str(failure))
                ledger.event(key, "action_result", tick=obs["tick"], result=receipt)
            else:
                status = result.get("status")
                if status not in {"running", "waiting", "blocked", "failed", "succeeded"}:
                    raise ValueError("executor returned no recognized action or status")
                if status == "succeeded":
                    # sync() persists this same mutable job. Never publish a
                    # terminal success before the independent proof returns.
                    job.update(status="waiting",reason="verifying_postcondition",evidence={})
                    fresh = game.observe()
                    ledger.sync(fresh)
                    try:
                        proof = executors.proof(job["capability"], fresh, result, job["parameters"])
                    except Exception:
                        job.update(status="blocked",reason="postcondition_verification_error",evidence={})
                        ledger.save()
                        raise
                    if proof is None:
                        job.update(status="blocked", reason="postcondition_not_verified", evidence={})
                    else:
                        job.update(status="succeeded",reason=result.get("reason",""),evidence=proof)
                    game.save()
                else:
                    evidence=result.get('evidence',{})
                    if not evidence and status in {'blocked','failed'}:
                        evidence={k:v for k,v in result.items()if k not in {'status','reason','ok','evidence'}
                                  and type(v)in(int,float,bool,str)and(not isinstance(v,str)or len(v)<=500)}
                    job.update(status=status, reason=result.get("reason", ""), evidence=evidence)
            ledger.save()
            if job["status"] in TERMINAL:
                if job['status']=='blocked':job.update(blocked_at_tick=obs['tick'],blocked_at_time=time.time());ledger.save()
                ledger.event(key, "terminal", status=job["status"], reason=job["reason"], evidence=job["evidence"])
                return job
            if time.monotonic() >= deadline:
                job.update(status="waiting", reason="execution_slice_complete")
                ledger.save()
                game.save()
                return job
            if not result.get("type") or result.get("type") == "wait":
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
