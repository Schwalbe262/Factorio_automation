"""Bounded, resumable model-selected jobs with a single world writer."""

from __future__ import annotations

from collections import Counter
import contextlib
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
import time
from typing import Any

from factorio_agent.core.deterministic_state import RunLock, _atomic_json
from factorio_agent import plan_needs, remedy
from factorio_agent.early_start import (
    EARLY_FREE_SLOTS,
    admission_requires,
    is_early,
    parked,
    runs_early,
    track_early_time,
)


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
        "effect": "steam power on a hand-fed boiler (feed route reserved); verify 30 game seconds",
    },
    # Phase 2 of starter power (power_coal_feed.py): proposed once belts are machine-made.
    "power_coal_feed": {
        "parameters": {},
        "requires": ["power"],
        "effect": "dedicated burner coal drill and belt to the boiler; verify 30 game seconds, then stop hand-fuelling",
    },
    "starter_capacity": {
        "parameters": {"resource": "iron-ore|copper-ore|coal|stone", "cells": "integer 1..12, absolute target"},
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
        "requires": ["power", "power_coal_feed"],
        "effect": "normally craft electric drill, prebuild grid, reserve-backed cutover and real coal output onto retained trunk",
    },
    # Mine-trunk rule (power_coal_tap.py): proposed once the coal patch has a proven bank with a trunk.
    "power_coal_tap": {
        "parameters": {},
        "requires": ["power", "power_coal_feed", "electric_coal"],
        "effect": "boiler coal from an output-priority splitter tap on the coal patch trunk; verify 30 game seconds, then mine the starter feed drill and its upstream belts",
    },
    "mining_outpost": {
        "parameters": {
            "resource": "iron-ore|copper-ore|coal|stone",
            "rate_per_minute": "finite number (0,960], absolute nominal target",
        },
        "native_recipes": ["electric-mining-drill"],
        "requires": ["power"],
        # Early start (early_start.py): planned from the site plan's mining zone once
        # power stands; collector belts, poles and the exit route are built, drills wait.
        "early_start": {"requires": ["power"], "inputs": "mining_zones", "locked_until": ["electric-mining-drill"]},
        "effect": "add aligned powered electric drills/collector banks with fixed output ports; never replace operating banks to expand",
    },
    # Mine-trunk rule 2 (trunk_lane.py): proposed when the trunk receipt says second_lane_required.
    "mine_trunk_lane": {
        "parameters": {"resource": "iron-ore|copper-ore|coal|stone"},
        "requires": ["mining_outpost"],
        "effect": "second belt beside the patch trunk with a 2-2 input-balanced splitter at its head; the trunk belt it replaces is released and mined by unit",
    },
    # Return feed (trunk_return_feed.py): proposed when carried surplus trips cost more than the feed.
    "trunk_return_feed": {
        "parameters": {"resource": "coal|iron-ore|copper-ore"},
        "requires": ["mining_outpost"],
        "native_recipes": ["splitter"],
        "effect": "chest, inserter and side belt with an input-priority splitter replacing one patch trunk belt just downstream of the trunk head and upstream of every tap; the chest is the item's carried surplus sink, so returned items reach every trunk consumer first",
    },
    # Remote smelting (remote_smelting.py): proposed when a plate ore cannot grow at home.
    "remote_smelting": {
        "parameters": {
            "resource": "iron-ore|copper-ore",
            "rate_per_minute": "finite number (0,960], ore draw the block is sized for",
        },
        "requires": ["mining_outpost", "planned_main_bus"],
        "native_recipes": ["electric-mining-drill", "splitter"],
        "effect": "a mining bank on the next lasting patch of the ore, a paired furnace block beside it fed by a coal trunk tap, and its plates onto the planned bus through a feed upstream of the lane's taps",
    },
    # Feed head rework (head_rework.py): proposed when a district bank's shared head limits growth.
    "feed_head_rework": {
        "parameters": {"block": "smelting_district", "resource": "iron-ore|copper-ore"},
        "requires": ["smelting_district"],
        "native_recipes": ["splitter", "underground-belt"],
        "effect": "replace a built district bank's shared feed head by the split head in place: plan republished first, new parts built beside the running head, stocked cut-over by guarded mines, old belts mined and reused; each row then gets a full ore lane",
    },
    "smelting_district": {
        "parameters": {},
        "requires": ["power", "bootstrap_logistics", "mining_outpost"],
        "native_recipes": ["underground-belt", "splitter"],
        # Early start (early_start.py): planned and partly built once every
        # source bank has a saved, published port; splitters/undergrounds wait for logistics.
        "early_start": {"requires": ["power"], "inputs": "mining_bank_ports", "locked_until": ["logistics"]},
        "effect": "reserve a PCB district, connect iron60/copper30 nominal banks and separate coal source with actual splitter/underground crossing, normally belt-feed furnaces",
    },
    "smelting_capacity": {
        "parameters": {
            "resource": "iron-ore|copper-ore",
            "rate_per_minute": "finite number (0,450], absolute nominal target",
        },
        "requires": ["smelting_district"],
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
        "requires": ["smelting_district"],
        "effect": "reserve normal machine science and labs, connect raw plate belts and power; no actor-fed science inputs",
    },
    "science_growth": {
        "parameters": {
            "labs": "integer [1,8], built labs of the staged red science block after growth",
            "machines": "integer [1,8] (red) or [1,12] (pack), built pack machines after growth",
            "pack": "optional instead of labs: another pack's block (pack_growth.PACK_STATES, today logistic-science-pack)",
        },
        "requires": ["automation_science"],
        "effect": "build reserved lab and science machine slots of the staged red science block (science_growth.py), or the reserved pack slots of another pack block ({pack, machines}: the green split row, pack_growth.py); proposed only on observed lab activity",
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
        # {} is the roadmap's first pair; {"stage": k} a power growth stage (power_growth.py).
        "parameters": {"stage": "optional integer 2..12 (power_growth stage)"},
        "requires": ["electric_coal"],
        "effect": "normally add an engine pair with dedicated paired coal and shared water; preserve old plant and verify thirty game seconds of native generation",
    },
    "defense_perimeter": {
        "parameters": {},
        "requires": ["starter"],
        "effect": "adapt normally paid gun targets to current native enemies, damage and tech; reuse paid guards, defer unneeded future posts and yield to production once current risk targets are armed; no whole-factory coverage claim",
    },
    "defense_growth": {
        "parameters": {"posts": "integer [1,16], current-risk post target the observe receipt asked for"},
        "requires": ["defense_perimeter"],
        "effect": "add the posts a risen native threat asks for on the threatened side, off planned land (defense_perimeter.growth_receipt); low priority, never on the roadmap's critical path",
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
        "requires": ["planned_main_bus", "defense_perimeter"],
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
        "requires": ["smelting_district", "logistic_science", "defense_perimeter"],
        "effect": "replace separate raw plate corridors with shared northbound iron/copper buses and native priority taps; commission compact green then normally retire obsolete western hardware; no throughput certification",
    },
    "planned_main_bus": {
        "parameters": {},
        "requires": ["smelting_district"],
        # Early start: trunks and feed routes while the district builds; the
        # splice into the district waits for its success (planned_bus.step).
        "early_start": {
            "requires": ["power"],
            "running": ["smelting_district"],
            "inputs": "district_feed_anchors",
            "locked_until": ["logistics"],
        },
        "effect": "normally build the site plan's iron/copper bus trunks fed by a splitter branch beside each district plate port; prove plates on every built lane tail; other lanes stay reserved",
    },
    "dedicated_materials": {
        "parameters": {"resource": "coal|stone"},
        "requires": ["mining_outpost"],
        "effect": "normally build an independent opposing drill pair and publish proven material ports; coal distribution stays outside resources",
    },
    "chemical_coal_supply": {
        "parameters": {},
        "requires": ["mining_outpost"],
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
        "requires": ["planned_main_bus"],
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
        "parameters": {"source_key": "optional: an independent namespaced crude source (advanced-crude)"},
        "requires": ["steel_production"],
        "effect": "normally build a defended powered pumpjack and real crude pipe outlet; naturally trigger native oil-processing research by extraction",
    },
    "water_gathering": {
        "parameters": {"source_key": "optional: an independent namespaced intake (advanced-water, acid-water)"},
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
        "requires": ["steel_production", "planned_main_bus"],
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
    "advanced_petroleum": {
        "parameters": {},
        "requires": ["basic_petroleum", "oil_gathering", "water_gathering"],
        "native_recipes": ["advanced-oil-processing", "heavy-oil-cracking", "light-oil-cracking"],
        "effect": "normally build a defended advanced refinery with circuit-controlled heavy and light cracking plants on the namespaced advanced-crude/advanced-water sources; prove all three products and publish heavy-oil, light-oil and advanced-petroleum-gas outlets",
    },
    "fluid_product": {
        "parameters": {"product": "sulfuric-acid|lubricant"},
        "requires": ["basic_petroleum", "water_gathering"],
        "effect": "normally build chemical cells for sulfuric acid (water, gas, sulfur, iron tap) or lubricant (heavy oil) on exact published fluid sources; prove native fluid output and publish it",
    },
    "fluid_distribution": {
        "parameters": {"source_key": "published fluid source", "aliases": "two new distinct outlet names"},
        "requires": ["basic_petroleum"],
        "effect": "normally build a same-fluid two-outlet header on an unclaimed published source and publish both outlets without multiplying source capacity",
    },
    "advanced_circuit_production": {
        "parameters": {},
        "requires": ["chemical_production", "planned_main_bus"],
        "effect": "normally build a compact advanced-circuit district with dedicated green/wire subcells, two fresh surplus bus taps and an exact proven plastic supply; preserve fixed ports and expansion space",
    },
    "chemical_science": {
        "parameters": {},
        "requires": ["engine_production", "advanced_circuit_production", "chemical_production", "automation_science"],
        "effect": "normally build compact blue science from exact automatic engine, advanced-circuit and sulfur sources; preserve original labs and red/green feeds while adding a guarded south-side blue supply",
    },
    "ammo_production": {
        "parameters": {},
        "requires": ["planned_main_bus"],
        "effect": "normally build a defended automatic magazine factory from a critical iron bus tap, bound its real output buffer, then supply full normal magazines through guarded inventory transfers",
    },
    "legacy_coal_cleanup": {
        "parameters": {},
        "requires": ["steam_expansion", "dedicated_materials"],
        "effect": "normally recover a proven isolated burner-to-coal-cache cell after automatic supply, loaded steam and inventory headroom checks; preserve all electric miners and power assets",
    },
    "produce": {
        "parameters": {
            "item": "product of an unlocked solid assembler recipe with at most two solid ingredients "
            "(three when all three are belt-fed through a lane merge, or two are in-line intermediates "
            "of one shared input)",
            "rate_per_minute": "finite number (0,600], absolute nominal target",
            "output": "optional: stock_chest (default, construction stock) or belt (a published belt "
            "output port other rows route from)",
        },
        "requires": ["smelting_district", "power"],
        "native_recipes": ["assembling-machine-1"],
        "effect": "normally build a recipe-driven assembler row (derated to its inserters, plus a one-level intermediate sub-row) on a master-plan block, route its inputs from free board ports or a bus tap, end it in a chest capped at two stacks, prove machine output in the chest and register the chest as construction stock; a higher rate fills reserved slots in place",
    },
    "supply_cell": {
        "parameters": {
            "products": "1..6 distinct items with assembler recipe trees from hand-fed plates (build)",
            "retire": "true instead of products: drain, demolish and release the cell",
            "retire_product": "one cell product with a producing produce row: stop it, mine what serves only it",
        },
        "requires": ["power"],
        "native_recipes": ["assembling-machine-1"],
        "effect": "temporary hand-fed assembler cell near the grid (not on the bus): plates fed in batches, products collected from bar-capped chests instead of hand crafting; retired by mining once demand ends or a produce row takes over",
    },
    "starter_retirement": {
        "parameters": {"resource": "iron-ore, copper-ore, stone or coal (a resource with a verified permanent source)"},
        "requires": ["starter"],
        "effect": "collect the remaining stock of a retired resource's burner starter cells in large trips, then mine the cold, empty, unlinked cells (paid, guarded) to free their tiles",
    },
    "wood_burner_feed": {
        "parameters": {},
        "requires": ["power_coal_feed"],
        "native_recipes": ["splitter"],
        "effect": "burn carried wood in the belt-fed boilers: a wooden chest, a burner arm and a splitter that replaces one coal feed belt with input priority on the wood side (wood first, coal fills the rest); the chest is the wood depot for carried surplus",
    },
    "startup_science_retirement": {
        "parameters": {},
        "requires": ["automation_science"],
        "effect": "dismantle the obsolete startup science cell, its slot labs and the starter lab once the permanent red science row researches from machine packs (paid, guarded mining; board block released; labs and assemblers carried for science_growth; leftover packs to the permanent labs)",
    },
    "research": {
        "parameters": {"technology": "catalog technology name"},
        "requires": ["automation_science"],
        "effect": "select normal native research; machine packs and belt-fed labs only; missing science tiers are explicit blockers",
    },
    # The rocket tail (rocket_stages.py, rocket_feed.py): generated roadmap rows after chemical science.
    "rocket_silo": {
        "parameters": {},
        "requires": ["chemical_science"],
        "native_recipes": ["rocket-silo"],
        "effect": "collect the silo's ingredients from published stock in batches (named waits per item and producer), hand-craft the silo, place it on a reserved 13x13 production block with a feed ring, bridge power after a measured headroom check; prove it standing, charged and on the part recipe",
    },
    "rocket_parts": {
        "parameters": {},
        "requires": ["rocket_silo"],
        "native_recipes": ["rocket-part"],
        "effect": "feed the silo's crafter input until a rocket stands ready: carried ingredients in stacks (guarded paid transfer), or a routed inserter feed from free belt ports when it costs less actor time; never writes the part counter; prove rocket_ready",
    },
    "rocket_launch": {
        "parameters": {},
        "requires": ["rocket_parts"],
        "native_recipes": ["space-platform-starter-pack"],
        "effect": "collect or hand-craft the starter pack, load it into the rocket, request a platform like the player's button and launch; prove the platform hub built from the delivered pack and rockets_launched above the baseline",
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
        # Structure checks live with each capability's handler (handlers/<domain>.py).
        from .capability_registry import handler_for

        handler = handler_for(self.capability)
        if handler.validate is not None:
            handler.validate(self.parameters)
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
        "research_queue": obs.get("research_queue"),
        "enemies_near_start": obs.get("enemies"),
        "research_progress": obs.get("research_progress"),
        "electricity": obs.get("electricity"),
        "adaptive_defense_risk": obs.get("adaptive_defense_risk"),
        "production_rate_measurement": obs.get("production_rate_measurement"),
        "known_project_construction_budget": obs.get("known_project_construction_budget"),
        "belt_lane_observation": obs.get("belt_lane_observation"),
        "smelting_throughput_bounds": obs.get("smelting_throughput_bounds"),
        "furnace_block_claims": obs.get("furnace_block_claims"),
        "belt_bandwidth": obs.get("belt_bandwidth"),
        "factory_constraints": obs.get("factory_constraints"),
        "available_research": obs.get("available_research"),
        "native_capability_gates": obs.get("native_capability_gates"),
        "resource_supply_options": obs.get("resource_supply_options"),
        "roadmap_material_demand": obs.get("roadmap_material_demand"),
        "roadmap_material_demand_error": obs.get("roadmap_material_demand_error"),
        "retirement_demand": obs.get("retirement_demand"),
        "retirement_demand_error": obs.get("retirement_demand_error"),
        "bulk_item_demand": obs.get("bulk_item_demand"),
        "supply_cell_candidates": obs.get("supply_cell_candidates"),
        "research_capacity": obs.get("research_capacity"),
        "research_capacity_error": obs.get("research_capacity_error"),
        "science_growth": obs.get("science_growth"),
        # Growth of the other pack blocks (pack_growth.py): science_growth {pack, machines} nodes.
        "pack_growth": obs.get("pack_growth"),
        "pack_growth_error": obs.get("pack_growth_error"),
        # Plate throughput growth (throughput_growth.py): the autopilot reads it from this summary.
        "throughput_growth": obs.get("throughput_growth"),
        "science_growth_error": obs.get("science_growth_error"),
        "defense_growth": obs.get("defense_growth"),
        "early_start_inputs": obs.get("early_start_inputs"),
        "coal_feed_belts": obs.get("coal_feed_belts"),
        "wood_burner_feed": obs.get("wood_burner_feed"),
        "trunk_return_feeds": obs.get("trunk_return_feeds"),
        "feed_head_rework": obs.get("feed_head_rework"),
        # The generated roadmap tail (progression.tail_steps) and its stage gates (rocket_stages.receipt).
        "rocket_plan": obs.get("rocket_plan"),
        "rocket_stages": obs.get("rocket_stages"),
        "launch": obs.get("launch"),
        # Rule inputs the autopilot reads (mine_trunk_nodes, power_coal_tap_nodes, produce_nodes); they
        # were computed in observe but dropped here, so those rules never fired in the supervisor.
        "mine_trunks": obs.get("mine_trunks"),
        "power_growth": obs.get("power_growth"),
        "produce_plate_sources": obs.get("produce_plate_sources"),
        # Bottleneck auditor (bottleneck_audit.py): diagnosis, proposals, idle_with_work for the supervisor.
        "bottlenecks": obs.get("bottlenecks"),
        "bottlenecks_error": obs.get("bottlenecks_error"),
        "capabilities": CAPABILITIES,
        "jobs": [
            {
                **{
                    k: v
                    for k, v in job.items()
                    if k in {"key", "capability", "status", "reason", "actions", "last_tick"}
                },
                # The remedy ladder (remedy.py): what failed, what was tried, what comes next.
                **{
                    k: job[k] for k in ("needs", "next_remedy", "remedy") if job.get(k) and job["status"] != "succeeded"
                },
                **(
                    {
                        "remedies_tried": [
                            {k: t.get(k) for k in ("rung", "fits", "reason")} for t in job["remedies_tried"][-6:]
                        ]
                    }
                    if job.get("remedies_tried") and job["status"] != "succeeded"
                    else {}
                ),
            }
            for job in ledger.data["jobs"].values()
        ],
        # From the live launch receipt (OBSERVE_LAUNCH_LUA), never from a job's saved claim.
        "rocket_launch_verified": _launch_verified(obs),
    }


def _launch_verified(obs: dict) -> bool:
    from .rocket_feed import launch_verified

    return launch_verified(obs)


MAX_ACTIVE_JOBS = 4
MIN_PRIORITY = -10000
MAX_PRIORITY = 10000


def is_active(job: dict) -> bool:
    return job["status"] not in TERMINAL | {"paused"}


# Capabilities that each drive the single research slot. Running two of them
# at once makes the second block with another_research_is_running. The single
# definition: progression counts the science packs of these steps too.
RESEARCH_CAPABILITIES = frozenset({"starter_research", "bootstrap_research", "bootstrap_logistics", "research"})


# Capabilities whose jobs for different resources work on different ore patches
# and may run side by side (copper capacity no longer queues behind iron).
# mining_outpost: the three banks share mining-outposts.json, but one
# MiningOutposts instance holds it in memory, jobs step one at a time under
# the writer lock, a step touches only its own resource's banks, and a new
# bank is planned against every saved bank's reserved area.
PER_RESOURCE_CAPABILITIES = frozenset({"starter_capacity", "mining_outpost"})


def holds_slot(job: dict) -> bool:
    """An active job counts toward MAX_ACTIVE_JOBS unless it is parked for research (early_start.parked)."""
    return is_active(job) and not parked(job)


# Capabilities whose per-resource jobs together hold one job slot: the outposts are one
# block of work for one actor (all-1113-2107: supply cell + drill research + iron + copper
# outposts filled the 4 slots at 12.9 min; the coal outpost, the district's last input,
# waited a median 5 minutes for the iron outpost to finish, and the district behind it).
SHARED_SLOT_CAPABILITIES = frozenset({"mining_outpost"})


def slot_group(capability: str, key: str) -> str:
    return capability if capability in SHARED_SLOT_CAPABILITIES else "job:" + str(key)


def slots_used(jobs) -> set:
    """Slot groups held by active, unparked jobs (``holds_slot``); a shared capability counts once."""
    return {slot_group(job["capability"], job.get("key")) for job in jobs if holds_slot(job)}


def slots_added(used: set, capability: str) -> int:
    """Slots a new job of ``capability`` adds: none when its shared group already holds one."""
    return 0 if capability in SHARED_SLOT_CAPABILITIES and capability in used else 1


def admission_lane(capability: str, parameters) -> str:
    """The exclusivity key of a job: its capability, plus the resource for per-resource capabilities."""
    if capability in PER_RESOURCE_CAPABILITIES and isinstance(parameters, dict) and parameters.get("resource"):
        return f"{capability}:{parameters['resource']}"
    return capability


def _admission_error(
    jobs: dict, capability: str, exclude_key: str | None = None, parameters: dict | None = None
) -> str | None:
    """Why another job in this admission lane may not become active now, if anything refuses it."""
    active = [job for key, job in jobs.items() if key != exclude_key and is_active(job)]
    lane = admission_lane(capability, parameters)
    if any(admission_lane(job["capability"], job.get("parameters")) == lane for job in active):
        return f"another job with capability {capability} is already active"
    if capability in RESEARCH_CAPABILITIES and any(job["capability"] in RESEARCH_CAPABILITIES for job in active):
        return "another job that owns research is already active"
    # A job parked for research (early start) keeps its lane but holds no slot; the outposts share one.
    used = slots_used([dict(job, key=key) for key, job in jobs.items() if key != exclude_key])
    slots = len(used) + slots_added(used, capability) - 1
    if slots >= MAX_ACTIVE_JOBS:
        return f"another job must finish or pause first: at most {MAX_ACTIVE_JOBS} jobs may be active"
    # Early start: the job runs ahead of unfinished prerequisites and never takes the last free slot
    # while one of them still has to be admitted. The free slot is for the work it runs ahead of: once
    # every missing strict requirement has an active job, nothing is left to admit for it.
    # aud-1114-0436: the district was refused from ~13.5 min (supply cell, logistics, outposts active)
    # until logistics ended (median 18.6), and its producer prefix (~100 entities) was built only then.
    succeeded = {job["capability"] for job in jobs.values() if job["status"] == "succeeded"}
    if is_early(CAPABILITIES, capability, succeeded) and slots >= MAX_ACTIVE_JOBS - EARLY_FREE_SLOTS:
        missing = set(CAPABILITIES[capability].get("requires", [])) - succeeded
        if not missing <= {job["capability"] for job in active}:
            limit = MAX_ACTIVE_JOBS - EARLY_FREE_SLOTS
            return f"an early start keeps a job slot free: at most {limit} jobs may be active"
    return None


def submit(root: Path, game, request: Request, *, priority: int = 0) -> dict:
    request.validate()
    if type(priority) is not int or not MIN_PRIORITY <= priority <= MAX_PRIORITY:
        raise ValueError(f"priority must be an integer in {MIN_PRIORITY}..{MAX_PRIORITY}")
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
        refusal = _admission_error(ledger.data["jobs"], request.capability, parameters=request.parameters)
        if refusal:
            raise ValueError(refusal)
        if "rollback_tick" in ledger.data:
            raise ValueError("world rollback requires explicit reconciliation")
        completed = {j["capability"] for j in ledger.data["jobs"].values() if j["status"] == "succeeded"}
        if not set(admission_requires(CAPABILITIES, request.capability)).issubset(completed):
            raise ValueError("capability prerequisites have not been verified")
        job = {
            **request.identity(),
            "request": request.identity(),
            "status": "pending",
            "reason": "model_requested",
            "priority": priority,
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


def control(root: Path, game, key: str, operation: str, *, remedy_rung: dict | None = None) -> dict:
    """Pause/resume/reconcile/retry one job. ``remedy_rung`` (retry only, the remedy ladder):
    ``{"rung", "kind", "options"}`` becomes the job's active ``remedy`` (its planner reads the options)."""
    if operation not in {"pause", "resume", "reconcile", "retry"}:
        raise ValueError("unsupported job control")
    if remedy_rung is not None and (operation != "retry" or not isinstance(remedy_rung.get("options"), dict)):
        raise ValueError("a remedy rung is applied by a retry with an options object")
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
            refusal = _admission_error(
                ledger.data["jobs"], job["capability"], exclude_key=key, parameters=job.get("parameters")
            )
            if refusal:
                raise ValueError(refusal)
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
                if job["reason"] == "job_deadline_exceeded":
                    # A retry is a fresh attempt: without this the job is over
                    # its deadline again on the very next pass (seed 2644599892:
                    # the main bus, waiting for plates, could not be retried).
                    job["paused_ticks"] = max(job["paused_ticks"], ledger.data["last_tick"] - job["created_tick"])
                    job["paused_seconds"] = max(job["paused_seconds"], now - job["created_at"])
                if job["reason"] == "objective_progress_stalled":
                    # The stall window was already used up when the job blocked.
                    # Carrying it over would block the retry on its first step.
                    job["progress_at_tick"] = ledger.data["last_tick"]
                    job["progress_at_time"] = now
                ledger.event(
                    key, "blocked_interval_excluded", tick=ledger.data["last_tick"], ticks=ticks, seconds=seconds
                )
                job.pop("blocked_at_tick", None)
                job.pop("blocked_at_time", None)
                ledger.event(key, "retry_requested", old_reason=job["reason"], tick=ledger.data["last_tick"])
                if remedy_rung is not None:
                    row = {
                        "rung": str(remedy_rung.get("rung"))[:60],
                        "kind": str(remedy_rung.get("kind"))[:20],
                        "options": dict(remedy_rung["options"]),
                        "needs_kind": (job.get("needs") or {}).get("kind"),
                        "tick": ledger.data["last_tick"],
                    }
                    job["remedy"] = row
                    job["remedies_applied"] = (job.get("remedies_applied") or [])[-11:] + [row]
                    ledger.event(
                        key,
                        "remedy_applied",
                        rung_kind=row["kind"],
                        **{k: v for k, v in row.items() if k != "kind"},
                    )
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


def note_remedy(
    root: Path, key: str, *, check: dict | None, next_remedy: dict | None, needs: dict | None = None
) -> dict:
    """Record one remedy-ladder check on a blocked job (``remedies_tried``, ``next_remedy``; a what-if's
    fresher ``needs``). Writes only these fields of a job that is still blocked; never acts."""
    with RunLock(root / "writer.lock"):
        ledger = Ledger(root)
        job = ledger.data["jobs"].get(key)
        if job is None:
            raise ValueError("unknown job key")
        if job["status"] != "blocked":
            return {"key": key, "status": job["status"], "noted": False}
        if check:
            job["remedies_tried"] = (job.get("remedies_tried") or [])[-23:] + [check]
        if next_remedy is not None:
            job["next_remedy"] = next_remedy
        if needs:
            job["needs"] = needs
        ledger.save()
        if check:
            ledger.event(
                key,
                "remedy_checked",
                tick=ledger.data.get("last_tick"),
                rung_kind=check.get("kind"),
                **{k: check.get(k) for k in ("rung", "fits", "needs_kind", "reason", "seconds")},
            )
        return {"key": key, "status": job["status"], "noted": True}


IDLE_RESEARCH_INTERVAL_TICKS = 600
IDLE_LANE = "lane:research"


def _unresolved_world_effect(jobs) -> bool:
    """Any durable intent, unknown action outcome or unreconciled rollback among these jobs."""
    return any(
        "pending_action" in job
        or "outcome_unknown" in str(job.get("reason", ""))
        or job.get("reason") == "world_rollback_requires_reconciliation"
        for job in jobs
    )


def _owns_research(capability) -> bool:
    """A capability that drives research owns the lab while it is active.

    The admission rule and the idle research lane must agree, so both use
    RESEARCH_CAPABILITIES; any capability taking a ``technology`` parameter
    counts too, in case one is added without updating that set.
    """
    return capability in RESEARCH_CAPABILITIES or "technology" in CAPABILITIES.get(capability, {}).get("parameters", {})


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


SAVE_INTERVAL_SECONDS = 60
# A job older than this (pauses and early-start time excluded) is blocked.
# Thirty game minutes / one hour killed jobs that wait legitimately in a long
# run (retirement waiting for cells to cool, research waiting for packs, a
# growth step waiting for materials): seed 2644599892 lost nine jobs that way.
JOB_DEADLINE_TICKS = 648000  # three game hours
JOB_DEADLINE_SECONDS = 10800  # three hours of wall time
# Exceptions that mean "this planner cannot read its inputs", never a game or connection failure.
PLANNER_FAULTS = (FileNotFoundError, LookupError, TypeError, AttributeError, ValueError, StopIteration)
UNKNOWN_BATCH_OUTCOMES = (
    "build_batch_outcome_unknown",
    "mine_batch_outcome_unknown",
    "storage_transfer_outcome_unknown",
    "mall_stock_transfer_outcome_unknown",
    "infrastructure_repair_outcome_unknown",
)


def _advance_job(
    root: Path, game, executors, ledger: Ledger, key: str, obs: dict, *, view: dict | None = None, guard=None
) -> tuple[str, dict]:
    """Advance one job by at most one executor step against an already synced observation.

    Returns (outcome, result). outcome is one of:
    "inactive" (terminal or paused before stepping), "stopped" (operator stop
    file), "terminal" (the job is now terminal), "action" (an action was
    performed and the job is still active), "status" (a non-action status was
    recorded and the job is still active). result is the executor's result, or
    {} when the executor was not called.

    ``view`` (default ``obs``) is the observation the executor plans against;
    a slack job gets ``critical_path.slack_view`` there. ``guard(result)`` may
    replace a planned action by a status before any intent is stored
    (``critical_path.slack_refusal``). Progress and proof use ``obs``.
    """
    job = ledger.data["jobs"][key]
    if job["status"] in TERMINAL | {"paused"}:
        # A terminal receipt is historical; callers must not assume it
        # proves present production after damage or world rollback.
        return "inactive", {}
    job["last_tick"] = obs["tick"]
    if job.get("pending_action"):
        # A prior process may have died after sending a mutation. Stop
        # rather than replaying an action whose outcome is unknown.
        job.update(status="blocked", reason="action_outcome_unknown_reconcile_required")
        ledger.save()
        return "terminal", {}
    succeeded = {j["capability"] for j in ledger.data["jobs"].values() if j["status"] == "succeeded"}
    early = runs_early(CAPABILITIES, job["capability"], succeeded, obs.get("technologies"))
    track_early_time(job, obs["tick"], time.time(), early)
    if (
        obs["tick"] - job["created_tick"] - job.get("paused_ticks", 0) - job.get("early_ticks", 0) > JOB_DEADLINE_TICKS
        or time.time() - job["created_at"] - job.get("paused_seconds", 0) - job.get("early_seconds", 0)
        > JOB_DEADLINE_SECONDS
    ):
        job.update(status="blocked", reason="job_deadline_exceeded")
        ledger.save()
        return "terminal", {}
    if (root / "stop.json").exists():
        job.update(status="waiting", reason="operator_stop_requested")
        ledger.save()
        return "stopped", {}
    progress = progress_key(obs, job["capability"])
    if progress != job.get("progress"):
        job.update(progress=progress, progress_at_tick=obs["tick"], progress_at_time=time.time())
    if obs["tick"] - job["progress_at_tick"] > 18000:
        job.update(status="blocked", reason="objective_progress_stalled")
        ledger.save()
        return "terminal", {}
    started = time.perf_counter()
    try:
        # A rung of the remedy ladder (job["remedy"], set by a retry) plans with its options.
        with remedy.applied((job.get("remedy") or {}).get("options")):
            result = executors.step(job["capability"], obs if view is None else view, job["parameters"])
        if guard is not None and result.get("type"):
            result = guard(result) or result
    except InterruptedError:
        # The operator stop arrived while planning (the builder's long searches poll stop.json):
        # nothing was sent, so this is the same clean stop as the check above, not a crashed
        # slice (fleet aud-1114-0703 seed 3743340251 logged it as an invalid CLI receipt).
        job.update(status="waiting", reason="operator_stop_requested")
        ledger.save()
        return "stopped", {}
    except PLANNER_FAULTS as exc:
        # Planning is read-only. A planner that trips over missing state or an
        # unexpected shape blocks its own job; it must not stop the other jobs
        # (game and world-guard errors are RuntimeError/OSError and still propagate).
        job.update(status="blocked", reason=f"planner_fault:{type(exc).__name__}:{exc}"[:200])
        job.update(blocked_at_tick=obs["tick"], blocked_at_time=time.time())
        ledger.save()
        ledger.event(key, "planner_fault", tick=obs["tick"], error=repr(exc)[:300])
        return "terminal", {}
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
            if receipt.get("reason") in UNKNOWN_BATCH_OUTCOMES:
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
        extra = {}
        if job["status"] == "blocked":
            job.update(blocked_at_tick=obs["tick"], blocked_at_time=time.time())
        if job["status"] in ("blocked", "failed"):
            # The failure contract (plan_needs): what failed and what would make it fit.
            failed = result
            if result and result.get("type"):
                # An action ended the job: only a repeated executor failure is a planning failure.
                failed = {"status": "blocked", "reason": job["reason"], "executor_action": result}
            needs = plan_needs.from_result(job["capability"], failed, job.get("parameters")) if failed else None
            if needs:
                job["needs"] = needs
                job.pop("next_remedy", None)
                extra = {"needs": {k: needs.get(k) for k in ("kind", "reason", "item", "change")}}
            else:
                job.pop("needs", None)
        else:
            job.pop("needs", None)
            job.pop("next_remedy", None)
        ledger.save()
        ledger.event(
            key,
            "terminal",
            status=job["status"],
            reason=job["reason"],
            evidence=job["evidence"],
            tick=obs["tick"],
            **extra,
        )
        return "terminal", result
    return ("action" if result.get("type") else "status"), result


def _yield_at_slice_end(job: dict, last_result: dict) -> None:
    # A slice boundary must not replace a real executor blocker
    # with a generic success-sounding scheduling reason.
    if job["status"] == "waiting" and not last_result.get("type"):
        job["slice_yield_reason"] = "execution_slice_complete"
    else:
        job.update(status="waiting", reason="execution_slice_complete")


def _save_if_due(game) -> None:
    # A one-shot CLI game has never saved, so it still saves every
    # slice. A long-lived session saves at most once per interval.
    last_save = getattr(game, "last_save_at", None)
    if type(last_save) is not float or time.monotonic() - last_save >= SAVE_INTERVAL_SECONDS:
        game.save()


def _survey_slice(executors):
    """The executors' survey reuse scope for one slice (``executors.survey_slice``), if any."""
    scope = getattr(executors, "survey_slice", None)
    return scope() if callable(scope) else contextlib.nullcontext()


def _begin_survey_pass(executors, obs: dict) -> None:
    # Every pass plans against a new observation: reused surveys are bound to
    # its world and tick (deterministic_bootstrap.SURVEY_SAME_PASS).
    begin = getattr(executors, "begin_survey_pass", None)
    if callable(begin):
        begin(obs)


def _validate_slice_seconds(seconds) -> None:
    if not isinstance(seconds, (int, float)) or not 0 < seconds <= 50:
        raise ValueError("slice duration must be greater than zero and at most 50 seconds")


def run_slice(root: Path, game, executors, key: str, *, seconds: float = 25) -> dict:
    _validate_slice_seconds(seconds)
    with RunLock(root / "writer.lock"), _survey_slice(executors):
        ledger = Ledger(root)
        if key not in ledger.data["jobs"]:
            raise ValueError("unknown job key")
        job = ledger.data["jobs"][key]
        deadline = time.monotonic() + seconds
        while True:
            obs = game.observe()
            ledger.sync(obs)
            _begin_survey_pass(executors, obs)
            outcome, result = _advance_job(root, game, executors, ledger, key, obs)
            if outcome in ("inactive", "stopped", "terminal"):
                return job
            if time.monotonic() >= deadline:
                _yield_at_slice_end(job, result)
                ledger.save()
                _save_if_due(game)
                return job
            if not result.get("type") or result.get("type") == "wait":
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))


def _active_in_priority_order(ledger: Ledger) -> list[str]:
    active = [job for job in ledger.data["jobs"].values() if is_active(job)]
    active.sort(key=lambda job: (-job.get("priority", 0), job["created_at"], job["key"]))
    return [job["key"] for job in active]


def _without_early(critical: set, jobs: dict, obs: dict) -> set:
    """Critical jobs minus those running ahead of an unfinished prerequisite (early start).

    A pre-building job cannot finish before the research or the block it runs
    ahead of, so it must not take the items those need: early outposts and the
    early district took the plates of the research batches and research got
    slower by 2-5 minutes on a 12-seed fleet (auto-1113-1618). Kept as they are
    when nothing else is critical.
    """
    succeeded = {job["capability"] for job in jobs.values() if job.get("status") == "succeeded"}
    technologies = obs.get("technologies")
    ahead = {key for key in critical if runs_early(CAPABILITIES, jobs[key]["capability"], succeeded, technologies)}
    return critical - ahead if critical - ahead else critical


def _critical_split(ledger: Ledger, obs: dict, executors, order: list[str]):
    """Critical job keys of this pass and, per slack job, its (view, guard) (critical_path.py).

    Claims are read when the first slack job advances, after the critical jobs
    of the pass reported, so they reflect what those jobs wait for now. A
    planner fault here never stops the jobs: the pass runs in plain priority order.
    """
    from . import critical_path as cp

    jobs = ledger.data["jobs"]
    catalog = getattr(executors, "catalog", None)
    try:
        path = cp.critical_path(jobs.values(), obs, CAPABILITIES, catalog=catalog)
        known = cp.known_items(catalog, obs)
        critical = set(cp.critical_jobs({k: jobs[k] for k in order}, path, known_items=known))
        critical = _without_early(critical, jobs, obs)
    except PLANNER_FAULTS:
        return set(), lambda key: (None, None)
    cache: dict = {}

    def slack(key):
        if key in critical or not critical:
            return None, None
        if "view" not in cache:
            try:
                claimed = cp.critical_claims(jobs, sorted(critical), known)
                cache["view"] = cp.slack_view(obs, claimed) if claimed else None
            except PLANNER_FAULTS:
                claimed, cache["view"] = {}, None
            cache["claimed"] = claimed
        claimed = cache["claimed"]
        if not claimed:
            return None, None
        guard = None
        if jobs[key]["capability"] not in cp.GUARD_EXEMPT:
            guard = lambda result: cp.slack_refusal(result, obs, claimed, catalog)  # noqa: E731
        return cache["view"], guard

    return critical, slack


def _perform_idle_action(game, executors, ledger: Ledger, obs: dict) -> bool:
    """Perform the executors' optional idle action once; True when an action was sent.

    The action has no job record and no durable intent. Ordinary failures
    (ValueError, RuntimeError) are logged and swallowed; connection and
    timeout errors, including RconError, propagate.
    """
    from .core.rcon import RconError

    idle = getattr(executors, "idle_action", None)
    if not callable(idle):
        return False
    try:
        action = idle(obs)
        if not isinstance(action, dict) or not action.get("type"):
            return False
        ledger.event(IDLE_LANE, "action_intent", tick=obs["tick"], action=action)
        receipt = game.act(action)
    except (ValueError, RuntimeError) as exc:
        if isinstance(exc, RconError):
            raise
        ledger.event(IDLE_LANE, "action_error", tick=obs["tick"], error=type(exc).__name__ + ": " + str(exc)[:500])
        return False
    ledger.event(IDLE_LANE, "action_result", tick=obs["tick"], result=receipt)
    return True


def run_jobs(root: Path, game, executors, *, seconds: float = 25) -> dict:
    """Interleave every active job at action granularity for one bounded slice.

    Each pass shares one observation. A job that only waits or reports a
    status hands the same observation to the next job; the first job that
    performs a world-changing action ends the pass so that the next pass
    plans against a fresh observation. A pass in which no job acted offers
    the actor to ``executors.idle_action`` (if defined); a performed idle
    action is counted in ``idle_actions`` and also starts a new pass.
    """
    _validate_slice_seconds(seconds)
    with RunLock(root / "writer.lock"), _survey_slice(executors):
        ledger = Ledger(root)
        deadline = time.monotonic() + seconds
        last_results: dict[str, dict] = {}
        touched: list[str] = []
        actions = 0
        idle_actions = 0
        any_active = False

        def receipt(status: str) -> dict:
            jobs = ledger.data["jobs"]
            return {
                "status": status,
                "jobs": {
                    key: {
                        "status": jobs[key]["status"],
                        "reason": jobs[key]["reason"],
                        "actions": jobs[key]["actions"],
                        "capability": jobs[key]["capability"],
                    }
                    for key in touched
                },
                "actions": actions,
                "idle_actions": idle_actions,
                "last_tick": ledger.data["last_tick"],
            }

        def finished() -> dict:
            if not any_active:
                return receipt("idle")
            return receipt("running" if actions else "waiting")

        while True:
            obs = game.observe()
            ledger.sync(obs)
            _begin_survey_pass(executors, obs)
            order = _active_in_priority_order(ledger)
            if not order:
                return finished()
            any_active = True
            for key in order:
                if key not in touched:
                    touched.append(key)
            if (root / "stop.json").exists():
                # Every active job gets the same stop treatment as run_slice.
                for key in order:
                    _advance_job(root, game, executors, ledger, key, obs)
                return finished()
            acted = False
            stopped = False
            critical, slack = _critical_split(ledger, obs, executors, order)
            order = [key for key in order if key in critical] + [key for key in order if key not in critical]
            for key in order:
                job = ledger.data["jobs"][key]
                before = job["actions"]
                view, guard = slack(key)
                outcome, result = _advance_job(root, game, executors, ledger, key, obs, view=view, guard=guard)
                last_results[key] = result
                performed = job["actions"] - before
                actions += performed
                if outcome == "stopped":
                    stopped = True
                    break
                # A "wait" action does not change the world in a way that
                # invalidates the shared observation, so the pass continues.
                if performed and result.get("type") != "wait":
                    acted = True
                    break
                if time.monotonic() >= deadline:
                    break
            if stopped:
                # The stop file appeared mid-pass: the next pass stops every job.
                continue
            if time.monotonic() >= deadline:
                remaining = [key for key in touched if is_active(ledger.data["jobs"][key])]
                for key in remaining:
                    _yield_at_slice_end(ledger.data["jobs"][key], last_results.get(key, {}))
                ledger.save()
                if remaining:
                    _save_if_due(game)
                return finished()
            if not acted:
                # An action-free pass leaves the actor free for the research lane.
                if _perform_idle_action(game, executors, ledger, obs):
                    idle_actions += 1
                    continue
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))


def __getattr__(name):
    # Lazy re-export: executors.py imports this module, so it cannot be imported at the top.
    if name == "StarterExecutors":
        from .executors import StarterExecutors

        return StarterExecutors
    raise AttributeError("module " + repr(__name__) + " has no attribute " + repr(name))
