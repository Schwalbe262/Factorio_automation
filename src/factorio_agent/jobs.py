"""Bounded, resumable model-selected jobs with a single world writer."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
import time
from typing import Any

from factorio_agent.core.deterministic_state import RunLock, _atomic_json


TERMINAL = {"succeeded", "blocked", "failed"}
CAPABILITIES = {
    "starter": {
        "parameters": {},
        "requires": [],
        "effect": "drill-fed iron/copper, coal/stone starter cells; natural early unlocks",
    },
    "power": {
        "parameters": {},
        "requires": ["starter"],
        "effect": "steam power with real self-fueling coal belt; verify 30 game seconds",
    },
    "starter_capacity": {
        "parameters": {"resource": "iron-ore|copper-ore|coal|stone", "cells": "integer 1..8, absolute target"},
        "requires": ["starter"],
        "effect": "expand productive starter cells to fund construction faster",
    },
    "prepare_components": {
        "parameters": {"items": "object of 1..8 unlocked building items, absolute actor targets1..50"},
        "requires": ["starter"],
        "opportunistic": True,
        "effect": "collect or normally craft bounded building stock for approved next work, preserving the main raw-material budget",
    },
    "starter_research": {
        "parameters": {"technology": "automation|electric-mining-drill"},
        "requires": ["power"],
        "effect": "finite natural startup research; reuse an existing funded machine batch before paying a new handcraft allowance",
    },
    "regularize_power_layout": {
        "parameters": {},
        "requires": ["starter"],
        "effect": "recover obsolete unfinished coal feed and install validated straight trunks; no hot cutover",
    },
    "bootstrap_logistics": {
        "parameters": {},
        "requires": ["power"],
        "effect": "normally assemble finite science to unlock native logistics; repair a known replaced legacy input cell through one receipt-audited paid repurchase, preserving its old budget",
    },
    "bootstrap_research": {
        "parameters": {
            "technology": "any available technology costing only automation-science-pack, fixed 1..100 units"
        },
        "requires": ["power"],
        "effect": "machine-build one finite red-science batch at normal cost and research the chosen technology; resumes or repairs its funded batch, including audited gear diversion",
    },
    "electric_coal": {
        "parameters": {},
        "requires": ["power"],
        "effect": "normally craft electric drill, prebuild grid, reserve-backed cutover and real coal output onto retained trunk",
    },
    "mining_outpost": {
        "parameters": {
            "resource": "iron-ore|copper-ore|coal|stone",
            "rate_per_minute": "finite number (0,960], absolute nominal target",
        },
        "native_recipes": ["electric-mining-drill"],
        "requires": ["power"],
        "effect": "add aligned powered electric drills/collector banks with fixed output ports; never replace operating banks to expand",
    },
    "smelting_district": {
        "parameters": {},
        "requires": ["power", "bootstrap_logistics", "mining_outpost"],
        "native_recipes": ["underground-belt", "splitter"],
        "effect": "reserve a PCB district, connect iron60/copper30 nominal banks and separate coal source with actual splitter/underground crossing, normally belt-feed furnaces",
    },
    "smelting_capacity": {
        "parameters": {
            "resource": "iron-ore|copper-ore",
            "rate_per_minute": "finite number (0,450], absolute nominal target",
        },
        "requires": ["paired_smelting"],
        "effect": "add independent upper/lower furnaces inside reserved rows; consumer ports remain fixed",
    },
    "paired_smelting": {
        "parameters": {},
        "requires": ["smelting_district"],
        "effect": "replace cosmetic output splitting with independent upper/lower furnace rows directly feeding opposite lanes",
    },
    "paired_mining": {
        "parameters": {},
        "requires": ["paired_smelting"],
        "effect": "normally recover old ore starter cells; opposing electric drill pairs feed both collector lanes, fixed outputs and surveyed expansion columns",
    },
    "automation_science": {
        "parameters": {"rate_per_minute": "finite number (0,30], requested red-science rate with nominal headroom"},
        "native_recipes": ["assembling-machine-1", "automation-science-pack", "lab"],
        "requires": ["paired_smelting"],
        "effect": "reserve normal machine science and labs, connect raw plate belts and power; no actor-fed science inputs",
    },
    "logistic_science": {
        "parameters": {
            "rate_per_minute": "finite number (0,30], requested design rate; commissioning does not certify sustained throughput"
        },
        "native_recipes": ["logistic-science-pack", "fast-inserter"],
        "requires": ["automation_science"],
        "effect": "normally belt-feed local gear/wire/circuit/belt/inserter cells and green science into a distinct lab belt lane; native research required",
    },
    "steam_expansion": {
        "parameters": {},
        "requires": ["electric_coal"],
        "effect": "normally add an engine pair with dedicated paired coal and shared water; preserve old plant and verify thirty game seconds of native generation",
    },
    "defense_perimeter": {
        "parameters": {},
        "requires": ["starter"],
        "effect": "adapt normally paid gun targets to current native enemies, damage and tech; reuse paid guards, defer unneeded future posts and yield to production once current risk targets are armed; no whole-factory coverage claim",
    },
    "defense_watch": {
        "parameters": {},
        "requires": ["defense_perimeter"],
        "effect": "continuously inspect, normally restock and rebuild known defensive posts during a bounded worker session; yields waiting while armed so development pauses do not leave defenses unattended",
    },
    "defense_hardening": {
        "parameters": {},
        "requires": ["defense_perimeter"],
        "effect": "normally build mutually covering gun posts and open-rear wall bastions at an observed loss hotspot; preserve production reservations and service access",
    },
    "radar_network": {
        "parameters": {},
        "requires": ["defense_perimeter"],
        "effect": "normally build a defended radar behind the front with measured electrical headroom; prove native scanning",
    },
    "radar_coverage": {
        "parameters": {},
        "requires": ["radar_network", "defense_perimeter"],
        "effect": "normally extend powered defended radar visibility across every registered factory/mining/utility/transport footprint; chart history alone is insufficient",
    },
    "nest_sweep": {
        "parameters": {},
        "requires": ["defense_hardening"],
        "effect": "prestock one concentrated paid turret wave, retain armed rear cover and rapidly deploy multiple guns in actual spawner range despite reviewed worm fire; no incremental harassment or offensive rebuild loop",
    },
    "piercing_ammo": {
        "parameters": {},
        "requires": ["ammo_production", "defense_perimeter"],
        "effect": "normally build a capped bulk ammunition factory with local yellow precursor assemblers and piercing assemblers; use real batch-fed ingredients and native machine production, never bulk character crafting",
    },
    "construction_mall": {
        "parameters": {"product": "transport-belt|gun-turret|underground-belt|splitter|assembling-machine-1|inserter"},
        "requires": ["main_bus_factory", "defense_perimeter"],
        "effect": "normally belt-feed expandable construction cells from surplus bus taps, cap native finished stocks, prove actual machine output and collect it for recurring construction",
    },
    "base_coal_recovery": {
        "parameters": {},
        "requires": ["steam_expansion", "defense_perimeter"],
        "effect": "detect actual exhaustion of the original coal drill and normally branch a verified shared coal bank into both existing boiler feeds; preserve ports and prove four loaded engines",
    },
    "water_distribution": {
        "parameters": {},
        "requires": ["water_gathering", "gas_distribution", "defense_perimeter"],
        "effect": "normally extend the existing water intake through a protected fixed outlet; preflight a complete isolated sulfur input pair, preserve existing pipes and prove actual water before publishing the new source",
    },
    "main_bus_factory": {
        "parameters": {},
        "requires": ["paired_smelting", "logistic_science", "defense_perimeter"],
        "effect": "replace separate raw plate corridors with shared northbound iron/copper buses and native priority taps; commission compact green then normally retire obsolete western hardware; no throughput certification",
    },
    "dedicated_materials": {
        "parameters": {"resource": "coal|stone"},
        "requires": ["paired_mining"],
        "effect": "normally build an independent opposing drill pair and publish proven material ports; coal distribution stays outside resources",
    },
    "chemical_coal_supply": {
        "parameters": {},
        "requires": ["paired_mining"],
        "effect": "normally build a separate defended opposing coal pair for chemistry, preserve existing coal consumers and publish one exact plastic-coal outlet without multiplying bank capacity",
    },
    "steam_coal_reroute": {
        "parameters": {},
        "requires": ["steam_expansion"],
        "effect": "prebuild an alternative off-resource coal corridor, normally cut over owned corners, verify loaded steam and recover obsolete transport",
    },
    "green_bus_reroute": {
        "parameters": {},
        "requires": ["main_bus_factory"],
        "effect": "move green bus taps and raw adapters around resources, prebuild the alternative and recover old owned transport only after fresh production proof",
    },
    "ore_transport_reroute": {
        "parameters": {},
        "requires": ["paired_mining", "paired_smelting"],
        "effect": "move the iron delivery suffix off foreign resource patches after the final mining join; preserve mines and prove new raw flow and smelting before recovery",
    },
    "furnace_upgrade": {
        "parameters": {
            "resource": "iron-ore|copper-ore",
            "rate_per_minute": "optional positive absolute nominal target",
        },
        "requires": ["paired_smelting"],
        "effect": "normally pay steel furnaces before exact owned replacements, preserve supply ports and prove real production; active mining must cover the target",
    },
    "steel_supply": {
        "parameters": {},
        "requires": ["main_bus_factory"],
        "effect": "normally install and prove a surplus-priority iron bus outlet at a bounded resource-free station",
    },
    "steel_production": {
        "parameters": {},
        "requires": ["steel_supply", "dedicated_materials"],
        "effect": "normally build defended automatic iron/coal steel furnaces with fixed ports and prove native output",
    },
    "steel_capacity": {
        "parameters": {"rate_per_minute": "positive absolute steel target within reserved furnace pairs"},
        "requires": ["steel_production"],
        "effect": "normally add paired steel furnaces while preserving every old unit, output port and consumer pin; prove new production and account for additional shared iron demand",
    },
    "steel_coal_supply": {
        "parameters": {},
        "requires": ["brick_production"],
        "effect": "normally split the proven brick coal header into a shared steel outlet, preserve existing consumers and the total source capacity",
    },
    "brick_production": {
        "parameters": {},
        "requires": ["dedicated_materials"],
        "effect": "normally build defended automatic stone/coal brick furnaces with fixed ports and prove native output",
    },
    "oil_gathering": {
        "parameters": {},
        "requires": ["steel_production"],
        "effect": "normally build a defended powered pumpjack and real crude pipe outlet; naturally trigger native oil-processing research by extraction",
    },
    "water_gathering": {
        "parameters": {},
        "requires": ["power"],
        "effect": "normally build an independent defended shore intake with a typed water output while preserving existing steam-water pumps and their connections",
    },
    "basic_petroleum": {
        "parameters": {},
        "requires": ["oil_gathering"],
        "effect": "normally connect the exact proven crude source to a defended refinery using isolated bounded pipe segments; prove native petroleum production and publish a fixed gas outlet",
    },
    "engine_production": {
        "parameters": {},
        "requires": ["steel_production", "main_bus_factory"],
        "effect": "normally build a defended compact engine district with a surplus iron tap, fixed steel supply and reserved machine expansion; prove real engines on its output",
    },
    "engine_capacity": {
        "parameters": {"rate_per_minute": "positive absolute engine target within reserved machine slots"},
        "requires": ["engine_production", "steel_capacity"],
        "effect": "normally add engine machines in reserved space, preserve old units and source pins, verify new output and publish additive iron demand",
    },
    "chemical_production": {
        "parameters": {"product": "plastic-bar|sulfur"},
        "requires": ["basic_petroleum"],
        "effect": "normally build compact defended chemical cells with exact declared fluid handoffs and paid typed ingredient connections; prove actual solid chemical output",
    },
    "gas_distribution": {
        "parameters": {},
        "requires": ["basic_petroleum"],
        "effect": "normally build a protected shared gas header before chemical consumers, preserve the original outlet and publish separate plastic/sulfur edges without multiplying source capacity",
    },
    "advanced_circuit_production": {
        "parameters": {},
        "requires": ["chemical_production", "main_bus_factory"],
        "effect": "normally build a compact advanced-circuit district with dedicated green/wire subcells, two fresh surplus bus taps and an exact proven plastic supply; preserve fixed ports and expansion space",
    },
    "chemical_science": {
        "parameters": {},
        "requires": ["engine_production", "advanced_circuit_production", "chemical_production", "automation_science"],
        "effect": "normally build compact blue science from exact automatic engine, advanced-circuit and sulfur sources; preserve original labs and red/green feeds while adding a guarded south-side blue supply",
    },
    "ammo_production": {
        "parameters": {},
        "requires": ["main_bus_factory"],
        "effect": "normally build a defended automatic magazine factory from a critical iron bus tap, bound its real output buffer, then supply full normal magazines through guarded inventory transfers",
    },
    "legacy_coal_cleanup": {
        "parameters": {},
        "requires": ["steam_expansion", "dedicated_materials"],
        "effect": "normally recover a proven isolated burner-to-coal-cache cell after automatic supply, loaded steam and inventory headroom checks; preserve all electric miners and power assets",
    },
    "research": {
        "parameters": {"technology": "catalog technology name"},
        "requires": ["automation_science"],
        "effect": "select normal native research; machine packs and belt-fed labs only; missing science tiers are explicit blockers",
    },
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
        elif self.capability == "prepare_components":
            from .component_reserve import validate_parameters

            validate_parameters(self.parameters)
        elif self.capability == "starter_research":
            from .research import validate_parameters

            validate_parameters(self.parameters)
        elif self.capability == "bootstrap_research":
            from .industry import validate_bootstrap_research

            validate_bootstrap_research(self.parameters)
        elif self.capability == "mining_outpost":
            from .outposts import validate_parameters

            validate_parameters(self.parameters)
        elif self.capability == "smelting_capacity":
            from .smelting import validate_capacity

            validate_capacity(self.parameters)
        elif self.capability == "steel_capacity":
            from .steel_capacity import validate_parameters

            validate_parameters(self.parameters)
        elif self.capability == "engine_capacity":
            from .engine_capacity import validate_parameters

            validate_parameters(self.parameters)
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
        elif self.capability == "construction_mall":
            from .construction_mall import _product

            _product(self.parameters)
        elif self.capability == "research":
            from .normal_research import validate_parameters

            validate_parameters(self.parameters)
        elif not isinstance(self.parameters, dict) or self.parameters:
            raise ValueError("this capability accepts no parameters")

    def identity(self) -> dict:
        # Retrying a request after observing again retains the same effect ID.
        return {
            "key": self.key,
            "world_id": self.world_id,
            "capability": self.capability,
            "parameters": self.parameters,
        }


class Ledger:
    def __init__(self, root: Path):
        self.root = Path(root)
        path = self.root / "jobs.json"
        self.data = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {"schema_version": 2, "jobs": {}, "world_id": None, "last_tick": 0}
        )
        if self.data.get("schema_version") != 2:
            raise ValueError("unsupported jobs checkpoint")

    def save(self):
        _atomic_json(self.root / "jobs.json", self.data)

    def event(self, key: str, kind: str, **fields):
        with (self.root / "job-events.jsonl").open("a", encoding="utf-8", newline="\n") as output:
            output.write(
                json.dumps(
                    {"key": key, "event": kind, "time": time.time(), **fields}, ensure_ascii=False, allow_nan=False
                )
                + "\n"
            )

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
    return {
        "world_id": obs["world_id"],
        "tick": obs["tick"],
        "inventory": obs.get("inventory", {}),
        "crafting_queue": obs.get("crafting_queue"),
        "entities": dict(Counter(e["name"] for e in obs.get("entities", []))),
        "production": obs.get("production", {}),
        "technologies": sorted(obs.get("technologies", {})),
        "enabled_recipes": sorted(k for k, v in obs.get("enabled_recipes", {}).items() if v),
        "research": obs.get("research"),
        "enemies_near_start": obs.get("enemies"),
        "research_progress": obs.get("research_progress"),
        "electricity": obs.get("electricity"),
        "adaptive_defense_risk": obs.get("adaptive_defense_risk"),
        "production_rate_measurement": obs.get("production_rate_measurement"),
        "known_project_construction_budget": obs.get("known_project_construction_budget"),
        "belt_lane_observation": obs.get("belt_lane_observation"),
        "smelting_throughput_bounds": obs.get("smelting_throughput_bounds"),
        "factory_constraints": obs.get("factory_constraints"),
        "available_research": obs.get("available_research"),
        "native_capability_gates": obs.get("native_capability_gates"),
        "resource_supply_options": obs.get("resource_supply_options"),
        "capabilities": CAPABILITIES,
        "jobs": [
            {k: v for k, v in job.items() if k in {"key", "capability", "status", "reason", "actions", "last_tick"}}
            for job in ledger.data["jobs"].values()
        ],
        "rocket_launch_verified": False,
    }


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
        job = {
            **request.identity(),
            "request": request.identity(),
            "status": "pending",
            "reason": "model_requested",
            "created_tick": obs["tick"],
            "last_tick": obs["tick"],
            "created_at": time.time(),
            "actions": 0,
            "evidence": {},
            "failures": 0,
            "progress_at_tick": obs["tick"],
            "progress_at_time": time.time(),
        }
        ledger.data["jobs"][request.key] = job
        ledger.save()
        ledger.event(request.key, "submitted", capability=request.capability, tick=obs["tick"])
        return job


def control(root: Path, game, key: str, operation: str) -> dict:
    if operation not in {"pause", "resume", "reconcile", "retry"}:
        raise ValueError("unsupported job control")
    with RunLock(root / "writer.lock"):
        ledger = Ledger(root)
        observation = game.observe()
        if operation == "pause" and observation.get("ok") is False and observation.get("reason") == "agent_dead":
            observation = game.query("return {ok=true,world_id=d.world_id,tick=game.tick}")
        ledger.sync(observation)
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
            if any(j["key"] != key and j["status"] not in TERMINAL | {"paused"} for j in ledger.data["jobs"].values()):
                raise ValueError("another job is active")
            if operation == "resume" and job["status"] != "paused":
                raise ValueError("only a paused job can be resumed")
            if operation == "retry":
                if (
                    job["status"] != "blocked"
                    or job.get("pending_action")
                    or job["reason"] == "action_outcome_unknown_reconcile_required"
                    or "rollback_tick" in ledger.data
                ):
                    raise ValueError("only a known blocked outcome in the same world can be retried")
                now = time.time()
                blocked_time = job.get("blocked_at_time", now)
                if "blocked_at_time" not in job:
                    path = root / "job-events.jsonl"
                    with path.open("rb") as events:
                        events.seek(max(0, path.stat().st_size - 2 * 1024 * 1024))
                        tail = events.read().splitlines()
                    for line in reversed(tail):
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if (
                            event.get("key") == key
                            and event.get("event") == "terminal"
                            and event.get("status") == "blocked"
                        ):
                            blocked_time = event["time"]
                            break
                ticks = max(0, ledger.data["last_tick"] - job.get("blocked_at_tick", job["last_tick"]))
                seconds = max(0, now - blocked_time)
                job["paused_ticks"] = job.get("paused_ticks", 0) + ticks
                job["paused_seconds"] = job.get("paused_seconds", 0) + seconds
                job["progress_at_tick"] += ticks
                job["progress_at_time"] += seconds
                ledger.event(
                    key, "blocked_interval_excluded", tick=ledger.data["last_tick"], ticks=ticks, seconds=seconds
                )
                job.pop("blocked_at_tick", None)
                job.pop("blocked_at_time", None)
                ledger.event(key, "retry_requested", old_reason=job["reason"], tick=ledger.data["last_tick"])
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
                if job["reason"] != "action_outcome_unknown_reconcile_required" or "rollback_tick" in ledger.data:
                    raise ValueError("only an uncertain action in the same live world can be reconciled")
                if job.get("uncertain_batch_receipt", {}).get("reason") == "infrastructure_repair_outcome_unknown":
                    pending = job.get("pending_action", {})
                    from .registered_belt_repair import approved_reconciliation

                    if pending.get("type") != "repair_registered_belts" or not approved_reconciliation(
                        root, key, pending.get("transaction_id")
                    ):
                        raise ValueError(
                            "registered infrastructure requires an exact reviewed native reconciliation epoch"
                        )
                # These registered executors replan from observed inventories,
                # entity identities and crafting queues; no old action is replayed.
                ledger.event(
                    key,
                    "unknown_action_reconciled",
                    old_intent=job.pop("pending_action", None),
                    tick=ledger.data["last_tick"],
                )
            job.update(status="waiting", reason="fresh_observation_before_replanning")
        ledger.save()
        ledger.event(key, operation, status=job["status"], tick=ledger.data["last_tick"])
        return job


class StarterExecutors:
    """Explicit temporary v1 executor reuse, with no v1 strategic supervisor."""

    def __init__(self, game, catalog):
        self.game, self.catalog = game, catalog
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

        self.industry = BootstrapLogistics(game, self.bootstrap, self.builder, catalog)
        self.bootstrap_research_targets = {}
        from .electric_mining import ElectricCoal

        self.electric_coal = ElectricCoal(game, self.bootstrap, self.builder)
        from .outposts import MiningOutposts

        self.outposts = MiningOutposts(game, self.bootstrap, self.builder)
        from .smelting import SmeltingDistrict

        self.smelting = SmeltingDistrict(game, self.bootstrap, self.builder, catalog)
        from .paired_smelting import PairedSmelting

        self.paired = PairedSmelting(game, self.builder, catalog, self.smelting)
        from .paired_mining import PairedMining

        self.paired_mining = PairedMining(game, self.bootstrap, self.builder, catalog, self.outposts, self.smelting)
        from .science import AutomaticScience

        self.science = AutomaticScience(game, self.builder, catalog)
        from .green_science import LogisticScience

        self.green = LogisticScience(game, self.builder, catalog, self.smelting, self.science)
        from .steam_expansion import SteamExpansion

        self.steam_expansion = SteamExpansion(game, self.builder, catalog, self.outposts)
        from .defense_perimeter import DefensePerimeter

        self.perimeter = DefensePerimeter(game, self.bootstrap, catalog)
        from .defense_repairs import DefenseRepairs

        self.defense_repairs = DefenseRepairs(game, self.bootstrap, self.perimeter)
        from .defense_hardening import DefenseHardening

        self.hardening = DefenseHardening(game, self.bootstrap, self.builder, catalog, self.perimeter)
        self.hardening.wall_repair = self.defense_repairs.wall_action
        from .bastion_maintenance import BastionMaintenance

        self.bastion_maintenance = BastionMaintenance(game, self.bootstrap, catalog, self.perimeter, self.hardening)
        from .nest_assault import NestAssault

        self.nest_assault = NestAssault(game, self.bootstrap, self.builder, catalog, self.perimeter)
        self.nest_sweep = None
        self.piercing_ammo = None
        self.piercing_path = Path(game.cfg.runtime_dir) / "piercing-ammo.json"
        self.construction_mall = None
        self.radar_coverage = None
        self.construction_mall_path = Path(game.cfg.runtime_dir) / "construction-mall.json"
        self.bootstrap.construction_stock_source = self._construction_stock
        self.base_coal_recovery = None
        self.water_distribution = None
        from .radar_network import RadarNetwork

        self.radars = RadarNetwork(game, self.builder, catalog, self.perimeter)
        self.outposts.perimeter = self.perimeter
        from .bus_factory import BusFactory

        self.bus_factory = BusFactory(game, self.builder, catalog, self.smelting, self.science, self.green)
        from .inventory_storage import InventoryStorage

        self.storage = InventoryStorage(game, catalog)
        from .material_supply import DedicatedMaterials

        self.material_supply = DedicatedMaterials(game, self.builder, catalog, self.outposts, perimeter=self.perimeter)
        self.chemical_coal = DedicatedMaterials(
            game, self.builder, catalog, self.outposts, perimeter=self.perimeter, purpose="chemistry"
        )
        from .resource_reroute import SteamCoalReroute

        self.steam_coal_reroute = SteamCoalReroute(game, self.builder, catalog, self.steam_expansion, self.outposts)
        from .green_bus_reroute import GreenBusReroute

        self.green_bus_reroute = GreenBusReroute(
            game, self.builder, catalog, self.bus_factory, self.science, self.green, self.smelting
        )
        from .legacy_coal_cleanup import LegacyCoalCleanup

        self.legacy_coal_cleanup = LegacyCoalCleanup(
            game, self.bootstrap, self.builder, catalog, self.storage, self.steam_expansion, self.outposts
        )
        from .ore_transport_reroute import OreTransportReroute

        self.ore_transport_reroute = OreTransportReroute(
            game, self.builder, catalog, self.smelting, self.outposts, bus_factory=self.bus_factory, green=self.green
        )
        from .furnace_upgrade import FurnaceUpgrade

        self.furnace_upgrade = FurnaceUpgrade(
            game, self.builder, catalog, self.smelting, self.paired, outposts=self.outposts
        )
        from .industrial import IndustrialOperations

        self.industrial_operations = IndustrialOperations(
            game,
            self.builder,
            catalog,
            self.perimeter,
            bus_factory=self.bus_factory,
            science=self.science,
            green=self.green,
        )
        from .shared_coal_tap import SharedCoalTap

        self.steel_coal_supply = SharedCoalTap(
            game, self.builder, catalog, self.industrial_operations.brick, industrial=self.industrial_operations
        )
        from .oil_gathering import OilGathering

        self.oil_gathering = OilGathering(game, self.builder, catalog, self.perimeter)
        from .water_gathering import WaterGathering

        self.water = WaterGathering(game, self.builder, catalog, self.perimeter)
        from .petroleum import BasicPetroleum

        self.petroleum = BasicPetroleum(game, self.builder, catalog, self.perimeter)
        from .ammo_production import AmmoProduction

        self.ammo = AmmoProduction(
            game,
            self.builder,
            catalog,
            self.perimeter,
            bus_factory=self.bus_factory,
            science=self.science,
            green=self.green,
        )
        self.perimeter.ammo_source = self.ammo
        from .engine_production import EngineProduction

        self.engines = EngineProduction(
            game,
            self.builder,
            catalog,
            self.perimeter,
            bus_factory=self.bus_factory,
            science=self.science,
            green=self.green,
            steel=self.industrial_operations.steel,
        )
        from .defense_relocation import DefenseRelocation

        self.engines.relocation = DefenseRelocation(
            game, self.bootstrap, self.builder, catalog, self.perimeter, self.engines
        )
        from .chemical_production import ChemicalProduction

        self.chemicals = ChemicalProduction(game, self.builder, catalog, self.perimeter)
        from .gas_distribution import GasDistribution

        self.gas_distribution = GasDistribution(
            game, self.builder, catalog, self.perimeter, petroleum=self.petroleum, chemical=self.chemicals
        )
        self.infrastructure_repair = None
        self.registered_belt_repair = None
        self.steel_capacity = None
        self.engine_capacity = None
        from .advanced_circuits import AdvancedCircuitProduction

        self.advanced_circuits = AdvancedCircuitProduction(
            game,
            self.builder,
            catalog,
            self.perimeter,
            bus_factory=self.bus_factory,
            science=self.science,
            green=self.green,
            chemical=self.chemicals,
        )
        from .chemical_science import ChemicalScience

        self.blue = ChemicalScience(
            game,
            self.builder,
            catalog,
            self.perimeter,
            engine=self.engines,
            advanced=self.advanced_circuits,
            chemical=self.chemicals,
            science=self.science,
            green=self.green,
            bus_factory=self.bus_factory,
        )
        from .normal_research import NormalResearch

        self.normal_research = NormalResearch(game, catalog, self.science, self.green, blue=self.blue)
        ledger = Ledger(Path(game.cfg.runtime_dir)).data
        self.radar_auto_enabled = any(
            j["capability"] == "radar_network" and j["status"] == "succeeded" for j in ledger["jobs"].values()
        )
        self.mall_auto_enabled = any(
            j["capability"] == "construction_mall" and j["status"] == "succeeded" for j in ledger["jobs"].values()
        )
        self.registered_belt_autodetect = (
            any(
                j["capability"] == "advanced_circuit_production" and j["status"] == "succeeded"
                for j in ledger["jobs"].values()
            )
            and (Path(game.cfg.runtime_dir) / "advanced-circuits.json").exists()
        )
        self.infrastructure_autodetect = any(
            j["capability"] == "basic_petroleum" and j["status"] == "succeeded" for j in ledger["jobs"].values()
        ) and all(
            (Path(game.cfg.runtime_dir) / name).exists()
            for name in ("basic-petroleum.json", "pcb-board.json", "fluid-sources.json")
        )
        self.base_coal_autodetect = any(
            j["capability"] == "steam_expansion" and j["status"] == "succeeded" for j in ledger["jobs"].values()
        ) and all(
            (Path(game.cfg.runtime_dir) / name).exists()
            for name in ("factory-builder.json", "steam-coal-reroute.json", "mining-outposts.json")
        )
        self.industrial_mode = any(
            j["capability"] == "smelting_district" and j["status"] == "succeeded" for j in ledger["jobs"].values()
        )

    def _piercing_factory(self):
        if self.piercing_ammo is None:
            from .piercing_ammo import PiercingAmmo

            self.piercing_ammo = PiercingAmmo(self.game, self.builder, self.catalog, self.perimeter)
        return self.piercing_ammo

    def _bootstrap_research(self, parameters):
        from .industry import BootstrapLogistics, validate_bootstrap_research

        validate_bootstrap_research(parameters)
        name = parameters["technology"]
        if not hasattr(self, "bootstrap_research_targets"):
            self.bootstrap_research_targets = {}
        if name not in self.bootstrap_research_targets:
            self.bootstrap_research_targets[name] = BootstrapLogistics(
                self.game, self.bootstrap, self.builder, self.catalog, technology=name
            )
        return self.bootstrap_research_targets[name]

    def record_action(self, capability, obs, action, receipt, parameters=None):
        pipeline = getattr(self, "science_pipeline", None)
        if pipeline is not None:
            pipeline.record_action(action, receipt)
            if pipeline.target_controller is not None:
                self.industry = pipeline.target_controller
        if capability == "bootstrap_research":
            self._bootstrap_research(parameters or {}).record_action(action, receipt)
        elif capability == "starter_research":
            batch = self._existing_startup_batch(parameters or {})
            if batch is not None:
                batch.record_action(action, receipt)
        elif capability == "bootstrap_logistics":
            batch = self._funded_startup_owner(obs)
            if batch is not None:
                batch.record_action(action, receipt)
            else:
                self.industry.record_action(action, receipt)

    def _component_reserve(self):
        if getattr(self, "components", None) is None:
            from .component_reserve import ComponentReserve

            self.components = ComponentReserve(self.game, self.bootstrap, self.catalog)
        return self.components

    def _science_wait_action(self, obs, batch, result):
        if (
            result.get("status") != "waiting"
            or result.get("type")
            or not result.get("evidence", {}).get("autonomous_wait")
        ):
            return result
        from .science_pipeline import SciencePipeline

        if getattr(self, "science_pipeline", None) is None:
            self.science_pipeline = SciencePipeline(self.game, self.bootstrap, self.builder, self.catalog)
        action = self.science_pipeline.next_action(obs, batch)
        if action and self.science_pipeline.target_controller is not None:
            self.industry = self.science_pipeline.target_controller
        return action or result

    def _funded_startup_owner(self, obs):
        # Any finite red-only batch owns its running research; the state file
        # only exists after that batch passed native admission and was funded.
        from .industry import validate_bootstrap_research

        technology = obs.get("research")
        try:
            validate_bootstrap_research({"technology": technology})
        except ValueError:
            return None
        if technology == "logistics" or obs.get("technologies", {}).get(technology):
            return None
        root = getattr(getattr(getattr(self, "game", None), "cfg", None), "runtime_dir", None)
        if (
            not isinstance(root, (str, Path))
            or not (Path(root) / ("bootstrap-research-" + technology + ".json")).exists()
        ):
            return None
        batch = self._bootstrap_research({"technology": technology})
        allocated = batch.state.get("allocated_inputs", {})
        return batch if any(type(n) is int and n > 0 for n in allocated.values()) else None

    def _existing_startup_batch(self, parameters):
        if parameters.get("technology") != "electric-mining-drill":
            return None
        root = getattr(getattr(getattr(self, "game", None), "cfg", None), "runtime_dir", None)
        if not isinstance(root, (str, Path)):
            return None
        if not (Path(root) / "bootstrap-research-electric-mining-drill.json").exists():
            return None
        batch = self._bootstrap_research(parameters)
        # A real reserved batch, including an invalid binding, must be handled
        # by its owner rather than paying for a second research implementation.
        return batch if batch.state else None

    def _construction_factory(self):
        if self.construction_mall is None:
            from .construction_mall import ConstructionMall

            self.construction_mall = ConstructionMall(
                self.game,
                self.builder,
                self.catalog,
                self.perimeter,
                bus_factory=self.bus_factory,
                science=self.science,
                green=self.green,
                smelting=self.smelting,
            )
        return self.construction_mall

    def _construction_stock(self, obs, item, count):
        factory = getattr(self, "construction_mall", None)
        if factory is None:
            path = getattr(self, "construction_mall_path", None)
            if path is None or not path.exists():
                return None
            factory = self._construction_factory()
        return factory.ensure_stock(obs, item, count) if factory.has_stock_cell(item) else None

    def _coverage_factory(self):
        if getattr(self, "radar_coverage", None) is None:
            from .radar_coverage import RadarCoverage

            self.radar_coverage = RadarCoverage(
                self.game, self.builder, self.catalog, self.perimeter, radars=self.radars
            )
        return self.radar_coverage

    def _mall_pending(self):
        factory = getattr(self, "construction_mall", None)
        path = getattr(self, "construction_mall_path", None)
        if factory is None and path is not None and path.exists():
            factory = self._construction_factory()
        return tuple(getattr(factory, "pending_items", ())) if factory is not None else ()

    def _mall_queue_upkeep(self, obs, *, pending_only=False):
        pending = self._mall_pending()
        if not pending and (pending_only or not getattr(self, "mall_auto_enabled", False)):
            return None
        if obs.get("tick", 0) < getattr(self, "mall_retry_tick", 0):
            return getattr(self, "mall_diagnostic", None)
        factory = self._construction_factory()
        product = (
            pending[0]
            if pending
            else next(
                (
                    q
                    for q in ("underground-belt", "splitter", "assembling-machine-1", "inserter", "gun-turret")
                    if not factory.has_stock_cell(q)
                ),
                None,
            )
        )
        if product is None:
            return None
        result = factory.step(obs, {"product": product})
        self.mall_diagnostic = result if not result.get("type") else None
        if result.get("status") == "blocked":
            self.mall_retry_tick = obs.get("tick", 0) + 1800
        return None if result.get("status") == "succeeded" else result

    def _radar_upkeep(self, obs, *, active_only=False):
        factory = getattr(self, "radar_coverage", None)
        active = (
            factory is not None and bool(getattr(factory, "state", {})) and factory.state.get("phase") != "verified"
        )
        if active_only and not active:
            return None
        if not active and not getattr(self, "radar_auto_enabled", False):
            return None
        if obs.get("tick", 0) < getattr(self, "radar_retry_tick", 0):
            return getattr(self, "radar_diagnostic", None) if active else None
        factory = self._coverage_factory()
        result = factory.upkeep(obs)
        self.radar_diagnostic = result if not result.get("type") else None
        if not result.get("type"):
            self.radar_retry_tick = obs.get("tick", 0) + (
                1800 if result.get("status") in ("succeeded", "blocked") else 180
            )
        return None if result.get("status") == "succeeded" else result

    def _continue_background_construction(self, obs, capability):
        # A controller's board snapshot stays valid until that build completes.
        if capability in ("power", "starter", "research", "defense_perimeter", "steam_expansion", "electric_coal"):
            return None
        if self._mall_pending():
            return self._mall_queue_upkeep(obs, pending_only=True)
        return self._radar_upkeep(obs, active_only=True)

    def _coal_recovery(self):
        if self.base_coal_recovery is None:
            from .base_coal_recovery import BaseCoalRecovery

            self.base_coal_recovery = BaseCoalRecovery(
                self.game,
                self.builder,
                self.catalog,
                self.perimeter,
                steam=self.steam_expansion,
                outposts=self.outposts,
                reroute=self.steam_coal_reroute,
            )
        return self.base_coal_recovery

    def _water_distribution(self):
        if self.water_distribution is None:
            from .water_distribution import WaterDistribution

            self.water_distribution = WaterDistribution(
                self.game, self.builder, self.catalog, self.perimeter, water=self.water
            )
        return self.water_distribution

    def _critical_coal_upkeep(self, obs):
        if obs.get("tick", 0) < getattr(self, "coal_retry_tick", 0):
            return getattr(self, "coal_retry_diagnostic", None)
        factory = getattr(self, "base_coal_recovery", None)
        if factory is None:
            if not getattr(self, "base_coal_autodetect", False):
                return None
            factory = self._coal_recovery()
        if not factory.state:
            diagnostic = factory.depletion_diagnostic(obs)
            if not diagnostic.get("ok") or not diagnostic.get("depleted"):
                return None
        result = factory.step(obs)
        if result.get("status") == "blocked":
            self.coal_retry_tick = obs["tick"] + 1800
            self.coal_retry_diagnostic = result
        return None if result.get("status") == "succeeded" else result

    def _steel_capacity(self):
        if self.steel_capacity is None:
            from .steel_capacity import SteelCapacity

            self.steel_capacity = SteelCapacity(
                self.game,
                self.builder,
                self.catalog,
                self.perimeter,
                steel=self.industrial_operations.steel,
                industrial=self.industrial_operations,
            )
        return self.steel_capacity

    def _engine_capacity(self):
        if self.engine_capacity is None:
            from .engine_capacity import EngineCapacity

            self.engine_capacity = EngineCapacity(
                self.game, self.builder, self.catalog, self.perimeter, engine=self.engines
            )
        return self.engine_capacity

    def _piercing_upkeep(self, obs):
        factory = getattr(self, "piercing_ammo", None)
        if factory is None:
            path = getattr(self, "piercing_path", None)
            if path is None or not path.exists():
                return None
            factory = self._piercing_factory()
        return factory.upkeep(obs) if getattr(factory, "commissioned", False) else None

    def _infrastructure_upkeep(self, obs):
        """Survey registered pipes without making a failed survey stop defenses."""
        if obs.get("tick", 0) < getattr(self, "infrastructure_retry_tick", 0):
            return None
        factory = getattr(self, "infrastructure_repair", None)
        if factory is None:
            if not getattr(self, "infrastructure_autodetect", False):
                return None
            from .infrastructure_repair import InfrastructureRepair

            factory = self.infrastructure_repair = InfrastructureRepair(
                self.game, self.builder, self.catalog, petroleum=self.petroleum, gas=self.gas_distribution
            )
        result = factory.step(obs)
        self.infrastructure_diagnostic = factory.last_diagnostic
        if result and result.get("type"):
            return result
        # Material waits retry promptly; quiet or rejected surveys stay bounded.
        delay = (
            180
            if (factory.last_diagnostic or {}).get("reason") == "normally prepay registered pipe replacements"
            else 1800
        )
        self.infrastructure_retry_tick = obs.get("tick", 0) + delay
        return None

    def _registered_belt_upkeep(self, obs):
        if obs.get("tick", 0) < getattr(self, "belt_repair_retry_tick", 0):
            return None
        factory = getattr(self, "registered_belt_repair", None)
        if factory is None:
            if not getattr(self, "registered_belt_autodetect", False):
                return None
            from .registered_belt_repair import RegisteredBeltRepair

            factory = self.registered_belt_repair = RegisteredBeltRepair(
                self.game, self.builder, self.catalog, advanced=self.advanced_circuits
            )
        result = factory.step(obs)
        self.belt_repair_diagnostic = factory.last_diagnostic
        if result and result.get("type"):
            return result
        delay = (
            180
            if (factory.last_diagnostic or {}).get("reason") == "normally fund registered belt replacements"
            else 1800
        )
        self.belt_repair_retry_tick = obs.get("tick", 0) + delay
        return None

    def starter_observation(self, obs):
        return {
            **obs,
            "entities": [
                e
                for e in obs.get("entities", [])
                if not self.builder.owns_automated_burner(e)
                and not self.smelting.owns_furnace(e, obs)
                and not (getattr(self, "paired", None) and self.paired.owns_furnace(e, obs))
                and not (getattr(self, "paired_mining", None) and self.paired_mining.retires_legacy(e, obs))
                and not (getattr(self, "legacy_coal_cleanup", None) and self.legacy_coal_cleanup.retires_legacy(e, obs))
                and not (getattr(self, "steam_expansion", None) and self.steam_expansion.owns_burner(e, obs))
            ],
        }

    def _defense_with_fuel(self, obs, result):
        if result and not result.get("type") and result.get("status") == "waiting":
            fuel = self.bootstrap.maintain_fuel(self.starter_observation(obs))
            if fuel and fuel.get("type"):
                return fuel
        return result

    def _approved_initial_defense(self, obs, *, maintain_armed=False):
        """Finish an already requested perimeter; never invent a defense job."""
        perimeter = getattr(self, "perimeter", None)
        if perimeter is None:
            return None
        adaptive = bool(getattr(perimeter, "adaptive_enabled", False))
        armed = getattr(perimeter, "state", {}).get("phase") == "armed"
        if armed and not maintain_armed and not adaptive:
            return None
        if not all(obs.get("enabled_recipes", {}).get(name) for name in ("gun-turret", "firearm-magazine")):
            return None
        game = getattr(self, "game", None)
        if game is None:
            return None
        path = Path(game.cfg.runtime_dir) / "jobs.json"
        if not path.exists():
            return None
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger.get("schema_version") != 2 or ledger.get("world_id") != obs.get("world_id"):
            return None
        same_world = [job for job in ledger.get("jobs", {}).values() if job.get("world_id") == obs["world_id"]]
        # No background defense mutation during any unresolved world effect,
        # even an empty durable intent or a clean older defense approval.
        if any(
            "pending_action" in job
            or "outcome_unknown" in str(job.get("reason", ""))
            or job.get("reason") == "world_rollback_requires_reconciliation"
            for job in same_world
        ):
            return None
        rows = [job for job in same_world if job.get("capability") == "defense_perimeter"]
        known = {"pending", "running", "waiting", "paused", "blocked", "failed", "succeeded"}
        if not any(job.get("status") in known and job.get("parameters") == {} for job in rows):
            return None
        result = perimeter.step(obs) if adaptive or not armed else perimeter.upkeep(obs)
        if (
            adaptive
            and result.get("status") == "blocked"
            and str(result.get("reason", "")).startswith("adaptive_defense_")
        ):
            self.defense_policy_diagnostic = result
            result = perimeter.upkeep(obs)
        result = self._defense_with_fuel(obs, result)
        # Commissioning defense cannot complete the unrelated mining/build job.
        return result if result and (result.get("type") or result.get("status") != "succeeded") else None

    def starter_upkeep(self, obs):
        if getattr(self, "storage", None):
            storage = self.storage.step(obs)
            if storage:
                return storage
        if getattr(self, "bastion_maintenance", None):
            bastion = self.bastion_maintenance.step(obs)
            if bastion:
                return bastion
        if getattr(self, "perimeter", None):
            defense = self.perimeter.upkeep(obs)
            if defense:
                return defense
        if getattr(self, "defense_repairs", None):
            if getattr(self, "hardening", None):
                wall = self.hardening.upkeep(obs)
                if wall:
                    return wall
            repair = self.defense_repairs.next_action(obs)
            if repair:
                return repair
        filtered = self.starter_observation(obs)
        if not getattr(self, "industrial_mode", False):
            return self.bootstrap.next_action(filtered)
        if self.smelting.state.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": "industrial_supply_world_changed"}
        required = {"steam-power", "electronics", "automation-science-pack"}
        if not required.issubset(obs.get("technologies", {})):
            return {"status": "blocked", "reason": "industrial_natural_unlocks_missing"}
        upkeep = self.bootstrap.maintain_fuel(filtered)
        return upkeep or {"status": "succeeded", "reason": "industrial supplies retain ordinary starter fuel upkeep"}

    def step(self, capability: str, obs: dict, parameters: dict | None = None) -> dict:
        from factorio_agent.core.deterministic_repair_control import pending_repair

        cleanup = pending_repair(obs)
        if cleanup:
            return cleanup
        if capability == "nest_assault":
            return {"status": "blocked", "reason": "staged_assault_retired_use_concentrated_nest_sweep"}
        if capability == "starter_research" and obs.get("technologies", {}).get((parameters or {}).get("technology")):
            return self.research.step(obs, parameters or {})
        gun_bridge = capability == "bootstrap_research" and (parameters or {}).get("technology") == "gun-turret"
        if capability != "defense_perimeter" and not gun_bridge:
            initial_defense = self._approved_initial_defense(obs, maintain_armed=capability == "bootstrap_research")
            if initial_defense:
                return initial_defense
        if capability == "prepare_components":
            return self._component_reserve().step(obs, parameters or {})
        if capability == "bootstrap_logistics":
            batch = self._funded_startup_owner(obs)
            if batch is not None:
                self.builder._sync(obs)
                result = self._science_wait_action(obs, batch, batch.step(obs))
                # Shared machines must finish their paid current owner before
                # a second science target can use them. Its proof does not
                # complete this logistics job.
                if result.get("status") == "succeeded":
                    return {"status": "waiting", "reason": "funded_science_prerequisite_finished", "evidence": result}
                return result
            if obs.get("technologies", {}).get("logistics") or self.industry.local_power_ready(obs):
                self.builder._sync(obs)
                return self.industry.step(obs)
        if capability == "starter_research":
            batch = self._existing_startup_batch(parameters or {})
            if batch is not None:
                self.builder._sync(obs)
                return self._science_wait_action(obs, batch, batch.step(obs))
        if capability == "bootstrap_research":
            # Early defense research must not first rebuild remote damaged
            # starter/coal assets. The controller checks its own live power.
            self.builder._sync(obs)
            batch = self._bootstrap_research(parameters or {})
            return self._science_wait_action(obs, batch, batch.step(obs))
        if capability == "defense_perimeter":
            self.builder._sync(obs)
            return self._defense_with_fuel(obs, self.perimeter.step(obs))
        if capability == "radar_network":
            self.builder._sync(obs)
            result = self.radars.step(obs, parameters or {})
            if result.get("type") or result.get("status") != "waiting":
                return result
            restored = self.bastion_maintenance.step(obs)
            if restored and (restored.get("type") or restored.get("status") == "blocked"):
                return restored
            upkeep = self.perimeter.upkeep(obs)
            if upkeep and (upkeep.get("type") or upkeep.get("status") == "blocked"):
                return upkeep
            return result
        if capability == "piercing_ammo":
            self.builder._sync(obs)
            result = self._piercing_factory().step(obs)
            if result.get("type") or result.get("status") != "waiting":
                return result
            restored = self.bastion_maintenance.step(obs)
            if restored and (restored.get("type") or restored.get("status") == "blocked"):
                return restored
            upkeep = self.perimeter.upkeep(obs)
            if upkeep and (upkeep.get("type") or upkeep.get("status") == "blocked"):
                return upkeep
            return result
        if capability == "construction_mall":
            self.builder._sync(obs)
            background = self._radar_upkeep(obs, active_only=True)
            if background:
                return background
            recovery = getattr(self, "base_coal_recovery", None)
            if recovery is not None and recovery.state and not recovery.verified:
                coal = self._critical_coal_upkeep(obs)
                if coal:
                    if coal.get("type") or coal.get("status") == "blocked":
                        return coal
                    return {
                        "status": "waiting",
                        "reason": "mall_waiting_for_coal_recovery_publication",
                        "evidence": {"coal_recovery": coal},
                    }
            result = self._construction_factory().step(obs, parameters or {})
            if result.get("reason") == "mall_power_margin_changed":
                coal = self._critical_coal_upkeep(obs)
                if coal and coal.get("type"):
                    return coal
                if coal and coal.get("status") == "waiting":
                    return {
                        "status": "waiting",
                        "reason": "mall_waiting_for_automatic_coal_power_recovery",
                        "evidence": {"power": result.get("evidence", {}), "coal": coal},
                    }
                if coal:
                    result = {**result, "evidence": {**result.get("evidence", {}), "coal_recovery": coal}}
            if result.get("type") or result.get("status") != "waiting":
                return result
            restored = self.bastion_maintenance.step(obs)
            if restored and (restored.get("type") or restored.get("status") == "blocked"):
                return restored
            upkeep = self.perimeter.upkeep(obs)
            if upkeep and (upkeep.get("type") or upkeep.get("status") == "blocked"):
                return upkeep
            return result
        if capability == "base_coal_recovery":
            self.builder._sync(obs)
            result = self._coal_recovery().step(obs)
            if result.get("type") or result.get("status") != "waiting":
                return result
            upkeep = self.perimeter.upkeep(obs)
            if upkeep and (upkeep.get("type") or upkeep.get("status") == "blocked"):
                return upkeep
            return result
        if capability == "water_distribution":
            self.builder._sync(obs)
            result = self._water_distribution().step(obs)
            if result.get("type") or result.get("status") != "waiting":
                return result
            upkeep = self.perimeter.upkeep(obs)
            if upkeep and (upkeep.get("type") or upkeep.get("status") == "blocked"):
                return upkeep
            return result
        if capability == "radar_coverage":
            self.builder._sync(obs)
            pending = self._mall_queue_upkeep(obs, pending_only=True)
            if pending:
                return pending
            coverage = self._coverage_factory()
            if not coverage.state or coverage.state.get("phase") == "verified":
                upkeep = self.perimeter.upkeep(obs)
                if upkeep:
                    return upkeep
            result = coverage.step(obs)
            if result.get("type") or result.get("status") != "waiting":
                return result
            upkeep = self.perimeter.upkeep(obs)
            if upkeep and upkeep.get("type"):
                return upkeep
            return result
        if capability == "nest_sweep":
            self.builder._sync(obs)
            if self.nest_sweep is None:
                from .nest_sweep import NestSweep

                self.nest_sweep = NestSweep(
                    self.game,
                    self.bootstrap,
                    self.builder,
                    self.catalog,
                    self.perimeter,
                    ammo_source=self._piercing_factory(),
                )
            restored = self.bastion_maintenance.step(obs)
            if restored and (restored.get("type") or restored.get("status") == "blocked"):
                return restored
            # Collect machine-produced raid stock before optional base topoffs.
            # Service factory inputs while stocking, then isolate committed combat.
            result = self.nest_sweep.step(obs)
            if result.get("type") or result.get("status") != "waiting":
                return result
            state = getattr(self.nest_sweep, "state", {})
            if not state.get("started_tick") and not state.get("funded_geometry_refresh_tick"):
                supplied = self._piercing_upkeep(obs)
                if supplied and (supplied.get("type") or supplied.get("status") == "blocked"):
                    return supplied
            upkeep = self.perimeter.upkeep(obs)
            if upkeep and (upkeep.get("type") or upkeep.get("status") == "blocked"):
                return upkeep
            return result
        if capability == "defense_hardening":
            self.builder._sync(obs)
            return self.hardening.step(obs, parameters or {})
        if capability == "defense_watch":
            self.builder._sync(obs)
            coal = self._critical_coal_upkeep(obs)
            if coal and coal.get("type"):
                return coal
            from .upgrades import next_upgrade

            upgrade = next_upgrade(obs, self.normal_research)
            if upgrade:
                return upgrade
            bastion = self.bastion_maintenance.step(obs)
            if bastion:
                return bastion
            result = self.perimeter.step(obs)
            if result.get("status") == "succeeded":
                infrastructure = self._infrastructure_upkeep(obs)
                if infrastructure:
                    return infrastructure
                belts = self._registered_belt_upkeep(obs)
                if belts:
                    return belts
                wall = self.hardening.upkeep(obs)
                if wall:
                    return wall
                repair = self.defense_repairs.next_action(obs)
                if repair:
                    return repair
                pending = self._mall_queue_upkeep(obs, pending_only=True)
                if pending and pending.get("type"):
                    return pending
                radar = None
                if not self._mall_pending():
                    radar = self._radar_upkeep(obs)
                    if radar and radar.get("type"):
                        return radar
                active_radar = getattr(self, "radar_coverage", None)
                if not pending and not (
                    active_radar and getattr(active_radar, "state", {}).get("phase") not in (None, "verified")
                ):
                    pending = self._mall_queue_upkeep(obs)
                    if pending and pending.get("type"):
                        return pending
                supplied = self._piercing_upkeep(obs)
                if supplied and supplied.get("type"):
                    return supplied
                return {
                    "status": "waiting",
                    "reason": "watching armed defenses and replenishing normal ammunition",
                    "evidence": {
                        **result.get("evidence", {}),
                        **({"piercing_factory": supplied} if supplied else {}),
                        **(
                            {"infrastructure": self.infrastructure_diagnostic}
                            if getattr(self, "infrastructure_diagnostic", None)
                            else {}
                        ),
                        **(
                            {"registered_belts": self.belt_repair_diagnostic}
                            if getattr(self, "belt_repair_diagnostic", None)
                            else {}
                        ),
                        **({"radar_coverage": radar} if radar else {}),
                        **({"construction_mall": pending} if pending else {}),
                        **({"coal_recovery": coal} if coal else {}),
                    },
                }
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
        background = self._continue_background_construction(obs, capability)
        if background:
            return background
        if capability in (
            "basic_petroleum",
            "gas_distribution",
            "chemical_production",
            "advanced_circuit_production",
            "chemical_science",
        ):
            infrastructure = self._infrastructure_upkeep(obs)
            if infrastructure:
                return infrastructure
            belts = self._registered_belt_upkeep(obs)
            if belts:
                return belts
        if capability not in ("research", "bootstrap_research", "defense_perimeter"):
            from .upgrades import next_upgrade

            upgrade = next_upgrade(obs, self.normal_research)
            if upgrade:
                return upgrade
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
            result = self.builder.ensure_power(obs)
            if result.get("status") != "succeeded":
                return result
            return self.industry.step(obs)
        if capability == "electric_coal":
            # During cutover the old source may be absent for one observed
            # action. Do not run ensure_power here and rebuild that old drill.
            return self.electric_coal.step(obs)
        if capability == "mining_outpost":
            result = self.builder.ensure_power(obs)
            if result.get("status") != "succeeded":
                return result
            return self.outposts.step(obs, parameters or {})
        if capability == "smelting_district":
            result = self.builder.ensure_power(obs)
            if result.get("status") != "succeeded":
                return result
            return self.smelting.step(obs)
        if capability == "smelting_capacity":
            result = self.builder.ensure_power(obs)
            if result.get("status") != "succeeded":
                return result
            return self.smelting.capacity_step(obs, parameters or {})
        if capability == "paired_smelting":
            result = self.builder.ensure_power(obs)
            if result.get("status") != "succeeded":
                return result
            return self.paired.step(obs)
        if capability == "paired_mining":
            return self.paired_mining.step(obs)
        if capability == "automation_science":
            result = self.builder.ensure_power(obs)
            if result.get("status") != "succeeded":
                return result
            return self.science.step(obs, parameters or {})
        if capability == "research":
            return self.normal_research.step(obs, parameters or {})
        if capability == "logistic_science":
            return self.green.step(obs, parameters or {})
        if capability == "steam_expansion":
            return self.steam_expansion.step(obs)
        if capability == "main_bus_factory":
            return self.bus_factory.step(obs, parameters or {})
        if capability == "dedicated_materials":
            return self.material_supply.step(obs, parameters or {})
        if capability == "chemical_coal_supply":
            return self.chemical_coal.step(obs, {"resource": "coal"})
        if capability == "steam_coal_reroute":
            return self.steam_coal_reroute.step(obs, parameters or {})
        if capability == "green_bus_reroute":
            return self.green_bus_reroute.step(obs, parameters or {})
        if capability == "legacy_coal_cleanup":
            return self.legacy_coal_cleanup.step(obs, parameters or {})
        if capability == "ore_transport_reroute":
            return self.ore_transport_reroute.step(obs, parameters or {})
        if capability == "furnace_upgrade":
            return self.furnace_upgrade.step(obs, parameters or {})
        if capability == "steel_supply":
            return self.industrial_operations.steel_supply(obs)
        if capability == "steel_production":
            return self.industrial_operations.steel_production(obs)
        if capability == "steel_capacity":
            return self._steel_capacity().step(obs, parameters or {})
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
        if capability == "engine_capacity":
            return self._engine_capacity().step(obs, parameters or {})
        if capability == "chemical_production":
            return self.chemicals.step(obs, parameters or {})
        if capability == "gas_distribution":
            return self.gas_distribution.step(obs)
        if capability == "advanced_circuit_production":
            return self.advanced_circuits.step(obs)
        if capability == "chemical_science":
            return self.blue.step(obs)
        raise ValueError("no executor for capability: " + capability)

    def proof(self, capability: str, obs: dict, result: dict, parameters: dict | None = None) -> dict | None:
        if capability == "prepare_components":
            return self._component_reserve().proof(obs, parameters or {})
        if capability == "radar_coverage":
            return self.radar_coverage.proof(obs) if getattr(self, "radar_coverage", None) is not None else None
        if capability == "starter_capacity":
            return self.capacity.proof(obs, parameters or {})
        if capability == "starter_research":
            if obs.get("technologies", {}).get((parameters or {}).get("technology")):
                return self.research.proof(obs, parameters or {})
            batch = self._existing_startup_batch(parameters or {})
            if batch is not None:
                return batch.proof(obs)
            return self.research.proof(obs, parameters or {})
        if capability == "regularize_power_layout":
            return self.power_layout.proof(obs)
        if capability == "bootstrap_logistics":
            return self.industry.proof(obs)
        if capability == "bootstrap_research":
            return self._bootstrap_research(parameters or {}).proof(obs)
        if capability == "electric_coal":
            return self.electric_coal.proof(obs)
        if capability == "mining_outpost":
            return self.outposts.proof(obs, parameters or {})
        if capability in {"smelting_district", "smelting_capacity"}:
            return self.smelting.proof(obs)
        if capability == "paired_smelting":
            return self.paired.proof(obs)
        if capability == "paired_mining":
            return self.paired_mining.proof(obs)
        if capability == "automation_science":
            return self.science.proof(obs)
        if capability == "research":
            return self.normal_research.proof(obs, parameters or {})
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
        if capability == "nest_assault":
            return self.nest_assault.proof(obs)
        if capability == "nest_sweep":
            return self.nest_sweep.proof(obs) if self.nest_sweep is not None else None
        if capability == "piercing_ammo":
            return self.piercing_ammo.proof(obs) if self.piercing_ammo is not None else None
        if capability == "construction_mall":
            return self.construction_mall.proof(obs, parameters or {}) if self.construction_mall is not None else None
        if capability == "base_coal_recovery":
            return self.base_coal_recovery.proof(obs) if self.base_coal_recovery is not None else None
        if capability == "water_distribution":
            return self.water_distribution.proof(obs) if self.water_distribution is not None else None
        if capability == "main_bus_factory":
            return self.bus_factory.proof(obs)
        if capability == "dedicated_materials":
            return self.material_supply.proof(obs, parameters or {})
        if capability == "chemical_coal_supply":
            return self.chemical_coal.proof(obs, {"resource": "coal"})
        if capability == "steam_coal_reroute":
            return self.steam_coal_reroute.proof(obs)
        if capability == "green_bus_reroute":
            return self.green_bus_reroute.proof(obs)
        if capability == "legacy_coal_cleanup":
            return self.legacy_coal_cleanup.proof(obs)
        if capability == "ore_transport_reroute":
            return self.ore_transport_reroute.proof(obs)
        if capability == "furnace_upgrade":
            proof = self.furnace_upgrade.proof(obs)
            if proof:
                from .capacity_profiles import refresh_bus_capacity

                profile = refresh_bus_capacity(self.game, self.catalog, self.bus_factory)
                if profile is not None and not profile.get("ok"):
                    return None
                proof = {**proof, "bus_capacity_profile": profile}
            return proof
        if capability == "steel_supply":
            return self.industrial_operations.steel_supply_proof(obs)
        if capability == "steel_production":
            return self.industrial_operations.steel_production_proof(obs)
        if capability == "steel_capacity":
            return self.steel_capacity.proof(obs) if self.steel_capacity is not None else None
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
        if capability == "engine_capacity":
            return self.engine_capacity.proof(obs) if self.engine_capacity is not None else None
        if capability == "chemical_production":
            return self.chemicals.proof(obs, parameters or {})
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
            return {
                "tick": obs["tick"],
                "natural_technologies": sorted(techs),
                "machine_production": {n: produced[n] for n in ("iron-plate", "copper-plate")},
                **result.get("evidence", {}),
            }
        evidence = result.get("evidence", {})
        if evidence.get("flow_verified") and evidence.get("verification_ticks", 0) >= 1800:
            return {"tick": obs["tick"], **evidence}
        return None


def progress_key(obs: dict, capability: str) -> str:
    # Relevant effects only: unrelated inventory churn must not hide a stall.
    if capability in ("bootstrap_logistics", "bootstrap_research", "starter_research"):
        required_structures = {
            "assembling-machine-1",
            "lab",
            "inserter",
            "wooden-chest",
            "small-electric-pole",
            "burner-mining-drill",
            "offshore-pump",
            "boiler",
            "steam-engine",
            "pipe",
            "pipe-to-ground",
            "transport-belt",
            "underground-belt",
            "splitter",
        }
        value = {
            "science": obs.get("production", {}).get("automation-science-pack"),
            "research": obs.get("research"),
            "research_progress": obs.get("research_progress"),
            "technologies": {
                k: obs.get("technologies", {}).get(k)
                for k in ("automation", "electric-mining-drill", "logistics", "gun-turret", "stone-wall")
            },
            "structures": sorted(
                (e.get("unit_number"), e.get("name"))
                for e in obs.get("entities", [])
                if e.get("name") in required_structures and type(e.get("unit_number")) is int
            ),
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    value = {
        "production": {
            n: obs.get("production", {}).get(n)
            for n in (
                "iron-plate",
                "copper-plate",
                "coal",
                "automation-science-pack",
                "logistic-science-pack",
                "chemical-science-pack",
            )
        },
        "techs": [
            n
            for n in (
                "steam-power",
                "electronics",
                "automation-science-pack",
                "automation",
                "electric-mining-drill",
                "logistics",
            )
            if obs.get("technologies", {}).get(n)
        ],
        "research": obs.get("research"),
        "research_progress": obs.get("research_progress"),
        "entities": [(e.get("unit_number"), e.get("name")) for e in obs.get("entities", [])],
    }
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
            if (
                obs["tick"] - job["created_tick"] - job.get("paused_ticks", 0) > 108000
                or time.time() - job["created_at"] - job.get("paused_seconds", 0) > 3600
            ):
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
                job.update(
                    status="running",
                    reason=result.get("reason", result["type"]),
                    pending_action=result,
                    evidence_status="retained_from_prior_observation",
                )
                ledger.save()
                ledger.event(key, "action_intent", tick=obs["tick"], action=result)
                started = time.perf_counter()
                try:
                    receipt = game.act(result)
                    if receipt.get("reason") in (
                        "build_batch_outcome_unknown",
                        "mine_batch_outcome_unknown",
                        "storage_transfer_outcome_unknown",
                        "mall_stock_transfer_outcome_unknown",
                        "infrastructure_repair_outcome_unknown",
                    ):
                        job["uncertain_batch_receipt"] = receipt
                        raise RuntimeError("batch outcome unknown; observe and reconcile before continuing")
                    record = getattr(executors, "record_action", None)
                    if callable(record):
                        record(job["capability"], obs, result, receipt, job["parameters"])
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
                job["failures"] = (
                    job["failures"] + 1 if failure == job.get("last_failure") and failure else int(bool(failure))
                )
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
                    job.update(
                        status="waiting",
                        reason="verifying_postcondition",
                        evidence={},
                        evidence_status="unavailable",
                        evidence_observed_tick=None,
                    )
                    fresh = game.observe()
                    ledger.sync(fresh)
                    try:
                        proof = executors.proof(job["capability"], fresh, result, job["parameters"])
                    except Exception:
                        job.update(status="blocked", reason="postcondition_verification_error", evidence={})
                        ledger.save()
                        raise
                    if proof is None:
                        job.update(status="blocked", reason="postcondition_not_verified", evidence={})
                    else:
                        job.update(
                            status="succeeded",
                            reason=result.get("reason", ""),
                            evidence=proof,
                            evidence_status="verified_postcondition",
                            evidence_observed_tick=fresh["tick"],
                        )
                    game.save()
                else:
                    evidence = result.get("evidence", {})
                    if not evidence and status in {"waiting", "blocked", "failed"}:
                        evidence = {
                            k: v
                            for k, v in result.items()
                            if k not in {"status", "reason", "ok", "evidence"}
                            and type(v) in (int, float, bool, str)
                            and (not isinstance(v, str) or len(v) <= 500)
                        }
                        position = result.get("position")
                        if (
                            isinstance(position, dict)
                            and set(position) == {"x", "y"}
                            and all(type(v) in (int, float) and math.isfinite(v) for v in position.values())
                        ):
                            evidence["position"] = dict(position)
                    job.update(
                        status=status,
                        reason=result.get("reason", ""),
                        evidence=evidence,
                        evidence_status="observation_result",
                        evidence_observed_tick=obs["tick"],
                    )
            ledger.save()
            if job["status"] in TERMINAL:
                if job["status"] == "blocked":
                    job.update(blocked_at_tick=obs["tick"], blocked_at_time=time.time())
                    ledger.save()
                ledger.event(key, "terminal", status=job["status"], reason=job["reason"], evidence=job["evidence"])
                return job
            if time.monotonic() >= deadline:
                # A slice boundary must not replace a real executor blocker
                # with a generic success-sounding scheduling reason.
                if job["status"] == "waiting" and not result.get("type"):
                    job["slice_yield_reason"] = "execution_slice_complete"
                else:
                    job.update(status="waiting", reason="execution_slice_complete")
                ledger.save()
                game.save()
                return job
            if not result.get("type") or result.get("type") == "wait":
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
