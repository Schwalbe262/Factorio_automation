from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .knowledge import dependency_tree_for_objective
from .monitor import recent_damage_events, summarize_factory
from .models import distance, entities_named, entity_item_count, inventory_count, total_item_count
from .planner import (
    factory_layout_issues,
    factory_layout_opportunities,
    factory_layout_simulation_candidates,
    factory_layout_structure,
)


KOREAN_ELECTRONIC_CIRCUIT = "\uc804\uc790\ud68c\ub85c"
KOREAN_ROCKET = "\ub85c\ucf13"
BUILD_ITEM_MALL_ITEMS = [
    "transport-belt",
    "inserter",
    "burner-inserter",
    "firearm-magazine",
    "gun-turret",
    "burner-mining-drill",
    "stone-furnace",
    "small-electric-pole",
    "assembling-machine-1",
]


@dataclass(frozen=True)
class SkillContract:
    name: str
    description: str
    executor: str
    preconditions: list[str]
    completion: list[str]
    llm_scope: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategicDecision:
    selected_skill: str
    priority: int
    reason: str
    evidence: list[str]
    blockers: list[str]
    expected_effect: str
    source: str = "heuristic"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SKILL_CATALOG: dict[str, SkillContract] = {
    "produce_iron_plate": SkillContract(
        name="produce_iron_plate",
        description="Bootstrap or replenish early iron plate production.",
        executor="IronPlateSkill",
        preconditions=["reachable iron ore", "reachable coal", "starter drill/furnace or craftable equivalents"],
        completion=["iron plates exist in player inventory or furnace output"],
        llm_scope="Choose this when iron plates are a bottleneck; do not place every miner tile by LLM.",
    ),
    "expand_iron_smelting": SkillContract(
        name="expand_iron_smelting",
        description="Build additional iron ore mining and smelting capacity with drills, furnaces, inserters, and belts.",
        executor="ExpandIronSmeltingSkill",
        preconditions=[
            "iron ore patch identified",
            "fuel or power available",
            "furnaces, drills, inserters, and belts craftable or available",
        ],
        completion=["sustained iron plate output on belts or machine outputs exceeds downstream demand"],
        llm_scope="Choose this when downstream goals are blocked by low iron throughput.",
    ),
    "expand_copper_smelting": SkillContract(
        name="expand_copper_smelting",
        description="Build additional copper ore mining and smelting capacity with drills, furnaces, inserters, and belts.",
        executor="ExpandCopperSmeltingSkill",
        preconditions=[
            "copper ore patch identified",
            "fuel or power available",
            "furnaces, drills, inserters, and belts craftable or available",
        ],
        completion=["sustained copper plate output on belts or machine outputs exceeds downstream demand"],
        llm_scope="Choose this when circuits, science, or cable are blocked by low copper throughput.",
    ),
    "build_belt_smelting_line": SkillContract(
        name="build_belt_smelting_line",
        description="Build a belt-fed smelting line using miners, belts, inserters, and furnaces.",
        executor="BeltSmeltingLineSkill",
        preconditions=[
            "ore patch and coal or power source identified",
            "transport belts, inserters, miners, and furnaces available or craftable",
            "site selected with room for input/output belts and future expansion",
        ],
        completion=["ore is mined, moved by belt or direct insertion, smelted, and output as plates"],
        llm_scope=(
            "Choose product, location, orientation, and throughput target. "
            "Executor places exact miners, belts, inserters, furnaces, and fuel/power connections."
        ),
    ),
    "setup_coal_supply": SkillContract(
        name="setup_coal_supply",
        description="Build a starter coal mining site with a burner mining drill and output belt for fuel logistics.",
        executor="CoalSupplySkill",
        preconditions=[
            "coal patch identified",
            "burner mining drill and transport belt available or craftable",
            "short-term hand fuel available to prime the first coal drill",
        ],
        completion=["coal is mined by a fueled burner mining drill and exposed on an output belt"],
        llm_scope=(
            "Choose this before scaling burner smelting or steam power when coal is still hand-mined. "
            "Executor validates exact coal patch tile, drill direction, output belt, and starter fuel."
        ),
    ),
    "produce_copper_plate": SkillContract(
        name="produce_copper_plate",
        description="Create or replenish copper plate supply.",
        executor="CopperPlateSkill",
        preconditions=["reachable copper ore", "fuel or power available", "furnace available or craftable"],
        completion=["copper plates exist in player inventory or furnace output"],
        llm_scope="Choose this when circuits, science, or cable are blocked by copper.",
    ),
    "produce_automation_science_pack": SkillContract(
        name="produce_automation_science_pack",
        description="Produce red science from copper plates and iron gear wheels.",
        executor="AutomationScienceSkill",
        preconditions=["iron plates", "copper plates", "gear crafting available"],
        completion=["automation science packs exist"],
        llm_scope="Choose this to unlock the first research tier after iron and copper are available.",
    ),
    "setup_power": SkillContract(
        name="setup_power",
        description="Build early steam power with offshore pump, boiler, steam engine, and poles.",
        executor="SetupPowerSkill",
        preconditions=["water located", "coal available", "stone/iron/copper supply available"],
        completion=["electric network has sustained production and powered poles"],
        llm_scope="Choose this before electric miners, assemblers, labs, and scalable factory blocks.",
    ),
    "research_automation": SkillContract(
        name="research_automation",
        description="Build and feed the first powered lab to unlock assembling-machine-1.",
        executor="ResearchAutomationSkill",
        preconditions=[
            "basic iron and copper supply available or producible",
            "steam power available or buildable",
            "automation science packs available or producible",
        ],
        completion=["automation technology is researched", "assembling-machine-1 recipe is unlocked"],
        llm_scope=(
            "Choose this when the next milestone requires assemblers. "
            "Executor handles lab placement, science insertion, and waiting for research."
        ),
    ),
    "produce_electronic_circuit": SkillContract(
        name="produce_electronic_circuit",
        description="Produce early green circuits from iron plates and copper cable.",
        executor="ElectronicCircuitSkill",
        preconditions=["iron plates available or producible", "copper plates available or producible", "copper cable recipe available"],
        completion=["electronic circuits exist in inventory"],
        llm_scope="Choose this after diagnosing whether iron or copper supply is the real bottleneck.",
    ),
    "automate_electronic_circuit_line": SkillContract(
        name="automate_electronic_circuit_line",
        description="Build an assembler-based green circuit line fed by iron plates and copper cable.",
        executor="CircuitAutomationSkill",
        preconditions=[
            "automation researched",
            "electric power available",
            "assembling machines, inserters, belts, and power poles available or craftable",
            "iron plate and copper plate supply lines exist or are planned",
        ],
        completion=["assemblers continuously output electronic circuits without hand crafting"],
        llm_scope=(
            "Choose ratios, site, input source, and expansion direction. "
            "Executor validates exact assembler, belt, inserter, and pole placement."
        ),
    ),
    "bootstrap_build_item_mall": SkillContract(
        name="bootstrap_build_item_mall",
        description="Automate recurring factory-expansion items such as belts, inserters, furnaces, drills, poles, and assemblers.",
        executor="BuildItemMallSkill",
        preconditions=[
            "automation researched",
            "electric power available",
            "iron plates, copper plates, gears, circuits, stone, and wood available or producible",
            "site selected near the main bus or starter factory with room for chests/belts",
        ],
        completion=[
            "core build items are produced by assemblers or dedicated furnace/drill supply loops",
            "expansion no longer depends on hand-crafting every belt, inserter, furnace, or assembler",
        ],
        llm_scope=(
            "Choose which build items need mall automation and where the mall belongs. "
            "Executor handles exact assembler recipes, inserter/chest placement, belts, power, and resource validation."
        ),
    ),
    "build_starter_defense": SkillContract(
        name="build_starter_defense",
        description="Build early gun-turret and firearm-magazine defenses around the starter factory.",
        executor="StarterDefenseSkill",
        preconditions=["enemy positions observed", "firearm magazines and gun turrets available or craftable"],
        completion=["factory sites have armed gun turrets on the threatened perimeter"],
        llm_scope=(
            "Choose when to pause expansion for static defense and ammo supply. "
            "Do not choose early nest clearing unless artillery, combat automation, or a validated turret-push skill exists. "
            "Executor handles exact turret placement, magazine crafting/insertion, and safe movement validation."
        ),
    ),
    "plan_factory_site": SkillContract(
        name="plan_factory_site",
        description="Diagnose inefficient site layout, site-to-site logistics gaps, and expansion parameters before building more.",
        executor="FactoryLayoutImprovementSkill",
        preconditions=["observed factory sites", "site-level logistics links", "known near-term production goal"],
        completion=["layout issues and recommended improvement parameters are logged for the next executor"],
        llm_scope=(
            "Choose when layout correction is more important than raw expansion. "
            "Use site graph, link status, distance, power, resource preservation, and corridor parameters; "
            "do not emit exact tile placements."
        ),
    ),
    "plan_rail_network": SkillContract(
        name="plan_rail_network",
        description="Plan main rail corridors, station districts, and resource outpost connections.",
        executor="future RailNetworkPlannerSkill",
        preconditions=["remote resource patches or long transport distance", "rail technology and materials available"],
        completion=["rail corridor and station intent are selected for executor validation"],
        llm_scope="Choose rail topology and station intent; executor validates rails, signals, and exact buildability.",
    ),
    "build_rail_supply_line": SkillContract(
        name="build_rail_supply_line",
        description="Connect a remote resource outpost to the factory by train when belt or walking logistics are too long.",
        executor="future RailSupplyLineSkill",
        preconditions=[
            "rail technology unlocked",
            "locomotive, cargo wagon, rails, train stops, signals, and fuel are craftable or available",
            "remote resource patch and factory unload district selected",
        ],
        completion=["train can move resource or intermediate items between load and unload stations"],
        llm_scope=(
            "Choose the supply route, served item, and station intent for remote resources. "
            "Do not place each rail segment, signal, train stop, or schedule entry directly."
        ),
    ),
    "plan_oil_outpost": SkillContract(
        name="plan_oil_outpost",
        description="Plan pumpjack, pipe, power, and future rail layout for crude-oil or fluid-resource outposts.",
        executor="future OilOutpostPlannerSkill",
        preconditions=[
            "oil processing or the relevant fluid extraction technology is near-term or unlocked",
            "fluid resource patches are observed",
            "pumpjacks, pipes, underground pipes, poles, and outpost logistics are available or planned",
        ],
        completion=[
            "pumpjack targets, pipe collection tree, power coverage, and connection point are selected",
            "the plan can be executed without installing an achievement-disabling mod",
        ],
        llm_scope=(
            "Choose when oil becomes strategically necessary and which outpost should be developed. "
            "Executor will adapt OilOutpostPlanner-style pumpjack/pipe/pole routing locally."
        ),
    ),
    "research_logistics": SkillContract(
        name="research_logistics",
        description="Feed labs with automation science to unlock belts/splitters and early logistics.",
        executor="ResearchTechnologySkill",
        preconditions=["labs available", "power available", "automation science supply"],
        completion=["target technology is researched"],
        llm_scope="Choose research goals and prerequisites, not lab insertion micro-steps.",
    ),
    "launch_rocket_program": SkillContract(
        name="launch_rocket_program",
        description="Long-horizon program that decomposes science, oil, modules, rocket parts, and launch.",
        executor="future RocketProgramPlanner",
        preconditions=["stable science progression", "scalable production blocks", "defense and logistics as needed"],
        completion=["rocket is launched in vanilla play"],
        llm_scope="Use LLM to decompose and reprioritize milestones; skills execute the build details.",
    ),
}


def skill_catalog_payload() -> list[dict[str, Any]]:
    return [skill.to_dict() for skill in SKILL_CATALOG.values()]


def make_strategy_payload(
    objective: str,
    observation: dict[str, Any],
    production_targets: dict[str, float] | None = None,
) -> dict[str, Any]:
    monitor = summarize_factory(observation, objective, production_targets=production_targets)
    return {
        "objective": objective,
        "observation": observation,
        "production_targets": dict(sorted((production_targets or {}).items())),
        "factory_monitor": monitor,
        "spatial_planning": make_spatial_planning_context(observation),
        "layout_improvement": make_layout_improvement_context(observation),
        "build_item_supply": make_build_item_supply_context(observation, monitor),
        "research_planning": make_research_planning_context(observation, monitor),
        "threats": make_threat_context(observation),
        "power_networks": monitor.get("power_networks", []),
        "goal_dependency_tree": dependency_tree_for_objective(objective, max_depth=5),
        "available_skills": skill_catalog_payload(),
        "decision_rule": (
            "Select exactly one high-level skill. Diagnose bottlenecks first. "
            "Evaluate electric supply per connected power network, not as a single global pool. "
            "For spatial work, choose districts, corridors, or rail topology only. "
            "When urgent production, defense, research, and power work are satisfied, use idle LLM cycles "
            "to improve factory site layout against reusable blueprint-style patterns. "
            "Do not emit tile-level movement, mining, building, rail, or signal actions."
        ),
    }


def make_layout_improvement_context(observation: dict[str, Any]) -> dict[str, Any]:
    issues = factory_layout_issues(observation)
    opportunities = factory_layout_opportunities(observation)
    candidates = factory_layout_simulation_candidates(observation)
    top_severity = max(
        [int(item.get("severity") or 0) for item in issues + opportunities]
        + [
            int((item.get("simulation") if isinstance(item.get("simulation"), dict) else {}).get("score") or 0)
            for item in candidates
        ],
        default=0,
    )
    return {
        "llm_responsibility": (
            "Use idle strategy cycles to compare current site graph against reusable blueprint-style patterns, "
            "then choose whether layout cleanup, compaction, ratio correction, or corridor reservation should happen next."
        ),
        "executor_responsibility": (
            "Translate approved improvement plans into safe, validated small build/move/rebuild operations; "
            "do not demolish working production without a replacement path."
        ),
        "recommended_skill": "plan_factory_site" if top_severity >= 75 else None,
        "site_structure": factory_layout_structure(observation),
        "issues": issues,
        "opportunities": opportunities,
        "simulation_candidates": candidates,
        "patterns": [
            "parallel smelting columns with shared ore/fuel/input and plate output lanes",
            "green circuit cells near iron/copper supply, roughly 3 cable assemblers per 2 circuit assemblers",
            "starter mall row near iron, gear, and circuit supply with shared inputs and chest outputs",
            "short lab daisy chain or science belt feed with room for later science colors",
            "main bus or trunk corridors before dense consumer blocks",
            "remote outpost plus rail corridor when belts or walking logistics become too long",
        ],
    }


def make_build_item_supply_context(observation: dict[str, Any], monitor: dict[str, Any] | None = None) -> dict[str, Any]:
    production_rows = monitor.get("production") if isinstance(monitor, dict) and isinstance(monitor.get("production"), list) else []
    production_by_item = {
        str(row.get("item")): float(row.get("per_minute") or 0.0)
        for row in production_rows
        if isinstance(row, dict)
    }
    items = []
    for item in BUILD_ITEM_MALL_ITEMS:
        stock = total_item_count(observation, item)
        estimated = production_by_item.get(item, 0.0)
        items.append(
            {
                "item": item,
                "stock": stock,
                "estimated_per_minute": estimated,
                "automated": estimated > 0.0,
                "needs_mall": stock < _build_item_stock_floor(item) and estimated <= 0.0,
            }
        )
    return {
        "llm_responsibility": (
            "Decide when expansion is limited by build-item supply instead of raw plates, "
            "and choose which mall items should be automated first."
        ),
        "executor_responsibility": "Build exact assembler/chest/belt/power layouts only through validated skills.",
        "items": items,
        "recommended_skill": "bootstrap_build_item_mall" if any(item["needs_mall"] for item in items) else None,
        "constraints": [
            "do not keep hand-crafting common expansion items once automation and power are available",
            "place build-item production close to iron/circuit supply and future logistics corridors",
            "reserve room for belts, inserters, chests, assemblers, and later provider/requester logistics",
        ],
    }


def make_research_planning_context(observation: dict[str, Any], monitor: dict[str, Any] | None = None) -> dict[str, Any]:
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    labs = [item for item in entities if isinstance(item, dict) and item.get("name") == "lab"]
    powered_labs = [item for item in labs if item.get("electric_network_connected") is not False]
    sites = monitor.get("factory_sites") if isinstance(monitor, dict) and isinstance(monitor.get("factory_sites"), list) else []
    lab_sites = [site for site in sites if isinstance(site, dict) and site.get("kind") == "research_lab_block"]
    return {
        "llm_responsibility": (
            "Choose when research throughput needs more labs and science-pack logistics; "
            "do not decide individual inserter ticks."
        ),
        "executor_responsibility": (
            "Build labs, inserters, belts, chests, power poles, and science-pack insertion through validated skills."
        ),
        "lab_count": len(labs),
        "powered_lab_count": len(powered_labs),
        "lab_sites": lab_sites,
        "layout_patterns": [
            "early research can use short lab daisy chains because inserters can move science packs from one lab to another",
            "split long daisy chains into multiple feed points or belt-fed rows so tail labs do not starve",
            "leave belt lanes for multiple science-pack colors before placing dense lab blocks",
            "keep lab blocks near science production and the main power network",
        ],
    }


def make_spatial_planning_context(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "site_selection": {
            "llm_responsibility": (
                "Pick production districts near the right inputs, reserve room for expansion, "
                "and avoid forcing high-volume items to cross the whole base."
            ),
            "executor_responsibility": "Validate exact tiles, walking, buildability, collisions, and entity placement.",
            "current_inputs": {
                "player_position": observation.get("player", {}).get("position")
                if isinstance(observation.get("player"), dict)
                else None,
                "factory_centroid": _entity_centroid(observation),
                "resource_patches": _resource_summary(observation),
            },
            "constraints": [
                "place smelting close to ore when early logistics are weak",
                "reserve straight transport corridors before dense builds",
                "keep high-throughput intermediates near their consumers",
                "leave room for belts, power poles, pipes, train stops, and expansion",
            ],
        },
        "rail_network": {
            "llm_responsibility": (
                "Choose when rails are justified, where trunk lines should run, and which resources need outposts."
            ),
            "executor_responsibility": "Place rails, signals, stations, trains, and schedules only after local validation.",
            "planning_inputs": {
                "known_remote_resources": _resource_summary(observation, minimum_distance=80.0),
                "existing_factory_centroid": _entity_centroid(observation),
                "rail_candidate_distance_tiles": 160,
            },
            "constraints": [
                "avoid rail plans before the required technology and material supply exist",
                "if a needed resource or factory district is far away, prefer train outposts over long belt or walking loops",
                "separate station blocks from dense starter factories",
                "prefer expandable trunk corridors over point-to-point spaghetti",
                "treat signaling and station placement as executor-level validated details",
            ],
        },
    }


def make_threat_context(observation: dict[str, Any]) -> dict[str, Any]:
    enemies = observation.get("enemies")
    if not isinstance(enemies, list):
        enemies = []
    enemy_rows = [item for item in enemies if isinstance(item, dict)]
    nearest = _nearest_enemy(enemy_rows)
    nearest_spawner = _nearest_enemy([item for item in enemy_rows if item.get("type") == "unit-spawner"])
    nearest_turret = _nearest_enemy([item for item in enemy_rows if item.get("type") == "turret"])
    counts_by_type: dict[str, int] = {}
    counts_by_name: dict[str, int] = {}
    for enemy in enemy_rows:
        enemy_type = str(enemy.get("type") or "unknown")
        enemy_name = str(enemy.get("name") or "unknown")
        counts_by_type[enemy_type] = counts_by_type.get(enemy_type, 0) + 1
        counts_by_name[enemy_name] = counts_by_name.get(enemy_name, 0) + 1
    nearest_distance = _enemy_distance(nearest)
    armed_gun_turret_count = _armed_gun_turret_count(observation)
    damage_events = recent_damage_events(observation, limit=10)
    recent_destroyed_count = sum(1 for item in damage_events if item.get("action") == "destroyed")
    max_spawner_pollution = max([_safe_float(item.get("pollution")) for item in enemy_rows if item.get("type") == "unit-spawner"] or [0.0])
    return {
        "enemy_count": len(enemy_rows),
        "counts_by_type": dict(sorted(counts_by_type.items())),
        "counts_by_name": dict(sorted(counts_by_name.items())),
        "nearest_enemy": nearest,
        "nearest_spawner": nearest_spawner,
        "nearest_turret": nearest_turret,
        "armed_gun_turret_count": armed_gun_turret_count,
        "recent_enemy_damage_count": len(damage_events),
        "recent_destroyed_count": recent_destroyed_count,
        "max_spawner_pollution": round(max_spawner_pollution, 3),
        "danger_level": _danger_level(nearest, nearest_distance, damage_events, max_spawner_pollution),
        "constraints": [
            "avoid expanding toward enemy nests without a defense plan",
            "prioritize defense when hostile units are close to the factory or player",
            "early defense means gun turrets and firearm-magazine supply around factory sites, not clearing nests",
            "repair or rebuild damaged factory sites before treating old throughput estimates as reliable",
            "reserve space for turret lines, walls, ammo belts, and radar coverage",
        ],
    }


def normalize_strategy_response(raw: dict[str, Any], fallback_objective: str = "launch_rocket_program") -> dict[str, Any]:
    selected = str(raw.get("selected_skill") or raw.get("selected_goal") or "")
    if selected not in SKILL_CATALOG:
        selected = fallback_objective if fallback_objective in SKILL_CATALOG else "produce_iron_plate"
    priority = _bounded_int(raw.get("priority"), fallback=50, minimum=0, maximum=100)
    evidence = _string_list(raw.get("evidence"))
    blockers = _string_list(raw.get("blockers"))
    return StrategicDecision(
        selected_skill=selected,
        priority=priority,
        reason=str(raw.get("reason") or ""),
        evidence=evidence,
        blockers=blockers,
        expected_effect=str(raw.get("expected_effect") or ""),
        source=str(raw.get("source") or "llm"),
    ).to_dict()


def reconcile_strategy_decision(
    decision: dict[str, Any],
    objective: str,
    observation: dict[str, Any],
    production_targets: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Apply deterministic safety/feasibility guardrails to an LLM strategy choice."""

    selected = str(decision.get("selected_skill") or decision.get("selected_goal") or "")
    if selected in _COAL_DEPENDENT_SKILLS and _coal_supply_needed(observation):
        adjusted = dict(decision)
        adjusted["selected_skill"] = "setup_coal_supply"
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 88)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            f"LLM selected {selected}, but burner smelting or steam power still lacks an automated coal supply site."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(set(_string_list(decision.get("blockers")) + ["automated coal fuel supply"]))
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            f"guardrail_adjusted_from={selected}",
            "coal_supply_ready=false",
        ]
        adjusted["expected_effect"] = "Prime a coal mining site before expanding fuel-dependent production."
        adjusted["guardrail_adjusted"] = {
            "from": selected,
            "to": "setup_coal_supply",
            "reason": guardrail_reason,
        }
        return adjusted
    if (
        selected == "produce_electronic_circuit"
        and _technology_researched(observation, "automation")
        and _target_deficit_exists(objective, observation, production_targets, "electronic-circuit")
        and not _circuit_automation_ready(observation)
    ):
        adjusted = dict(decision)
        adjusted["selected_skill"] = "automate_electronic_circuit_line"
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 85)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            "LLM selected hand circuit production for a per-minute electronic-circuit deficit, "
            "but Automation is researched and no powered circuit cell is ready."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(set(_string_list(decision.get("blockers")) + ["assembler-based electronic circuit production"]))
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            "guardrail_adjusted_from=produce_electronic_circuit",
            "automation_researched=true",
            "electronic_circuit_target_deficit=true",
            "circuit_automation_ready=false",
        ]
        adjusted["expected_effect"] = (
            "Build the first powered assembler cell for electronic circuits instead of repeatedly hand-crafting circuits."
        )
        adjusted["guardrail_adjusted"] = {
            "from": "produce_electronic_circuit",
            "to": "automate_electronic_circuit_line",
            "reason": guardrail_reason,
        }
        return adjusted
    return decision


def heuristic_strategy(
    objective: str,
    observation: dict[str, Any],
    production_targets: dict[str, float] | None = None,
) -> dict[str, Any]:
    objective_lower = objective.lower()
    inventory_iron = inventory_count(observation, "iron-plate")
    total_iron = total_item_count(observation, "iron-plate")
    total_copper = total_item_count(observation, "copper-plate")
    science = total_item_count(observation, "automation-science-pack")
    circuits = total_item_count(observation, "electronic-circuit")
    automation_researched = _technology_researched(observation, "automation")
    monitor = summarize_factory(observation, objective, production_targets=production_targets)
    bottlenecks = monitor.get("bottlenecks") if isinstance(monitor.get("bottlenecks"), list) else []
    power_issue = _first_power_issue(monitor)
    threats = make_threat_context(observation)
    layout = make_layout_improvement_context(observation)
    layout_issues = layout.get("issues") if isinstance(layout.get("issues"), list) else []
    layout_opportunities = layout.get("opportunities") if isinstance(layout.get("opportunities"), list) else []
    top_layout_item = _top_layout_item(layout_issues, layout_opportunities)
    if threats["danger_level"] in {"critical", "high"} and int(threats.get("armed_gun_turret_count") or 0) <= 0:
        nearest = threats.get("nearest_enemy") if isinstance(threats.get("nearest_enemy"), dict) else {}
        return StrategicDecision(
            selected_skill="build_starter_defense",
            priority=98,
            reason="Hostile entities are close enough to threaten factory expansion; early response is factory-site defense, not nest clearing.",
            evidence=[
                f"danger_level={threats['danger_level']}",
                f"nearest_enemy={nearest.get('name')}",
                f"nearest_enemy_distance={nearest.get('distance')}",
                f"recent_enemy_damage_count={threats.get('recent_enemy_damage_count')}",
            ],
            blockers=["enemy threat"],
            expected_effect="Pause expansion and build armed gun turrets plus firearm-magazine supply around threatened factory sites.",
        ).to_dict()

    if _coal_supply_needed(observation):
        return StrategicDecision(
            selected_skill="setup_coal_supply",
            priority=91,
            reason="Fuel-dependent starter production is still relying on hand-mined coal; build a coal supply site before more burner expansion.",
            evidence=[
                f"coal_inventory={inventory_count(observation, 'coal')}",
                "coal_supply_ready=false",
            ],
            blockers=["automated coal fuel supply"],
            expected_effect="Build and fuel a burner coal drill with an output belt so smelting and power can be refueled locally.",
        ).to_dict()

    if power_issue:
        return StrategicDecision(
            selected_skill="setup_power",
            priority=94,
            reason=f"Electric power issue on network {power_issue.get('network_id')}: {power_issue.get('status')}.",
            evidence=[
                f"generation_kw={power_issue.get('generation_kw')}",
                f"demand_kw={power_issue.get('demand_kw')}",
                f"satisfaction={power_issue.get('satisfaction')}",
                f"unconnected_consumers={power_issue.get('unconnected_consumers')}",
            ],
            blockers=["electric power network"],
            expected_effect="Expand or connect the electric network before adding more electric machines.",
        ).to_dict()

    if ("rocket" in objective_lower or KOREAN_ROCKET in objective) and total_iron < 10:
        return StrategicDecision(
            selected_skill="produce_iron_plate",
            priority=96,
            reason="Rocket program starts with basic iron production; production target deficits wait until iron is established.",
            evidence=[f"iron_plate_total={total_iron}", f"iron_plate_inventory={inventory_iron}"],
            blockers=["basic iron supply"],
            expected_effect="Bootstrap the first iron plates before expanding target-driven production.",
        ).to_dict()

    if bottlenecks:
        first = bottlenecks[0] if isinstance(bottlenecks[0], dict) else {}
        item = str(first.get("item") or "")
        skill = _skill_for_bottleneck_item(item, observation)
        if skill:
            return StrategicDecision(
                selected_skill=skill,
                priority=95,
                reason=f"Current factory monitor reports bottleneck for {item}: {first.get('reason')}",
                evidence=[f"{item}_stock={first.get('stock')}", f"{item}_per_minute={first.get('estimated_per_minute')}"],
                blockers=[item],
                expected_effect=f"Run {skill} to address the monitored production bottleneck.",
            ).to_dict()

    if "electronic" in objective_lower or "circuit" in objective_lower or KOREAN_ELECTRONIC_CIRCUIT in objective:
        if total_iron < 20:
            return StrategicDecision(
                selected_skill="expand_iron_smelting",
                priority=90,
                reason="Electronic circuits require steady iron plates; current iron supply is below the circuit threshold.",
                evidence=[f"iron_plate_total={total_iron}", f"iron_plate_inventory={inventory_iron}"],
                blockers=["iron plate throughput"],
                expected_effect="Increase iron plate supply before circuit assembly.",
            ).to_dict()
        if total_copper < 20:
            return StrategicDecision(
                selected_skill="expand_copper_smelting",
                priority=85,
                reason="Electronic circuits also need copper cable; copper plates are below the circuit threshold.",
                evidence=[f"copper_plate_total={total_copper}"],
                blockers=["copper plate throughput"],
                expected_effect="Increase copper plate supply for cable production.",
            ).to_dict()
        return StrategicDecision(
            selected_skill="produce_electronic_circuit",
            priority=80,
            reason="Iron and copper prerequisites look available; proceed to circuit production.",
            evidence=[f"iron_plate_total={total_iron}", f"copper_plate_total={total_copper}", f"circuits={circuits}"],
            blockers=[],
            expected_effect="Begin green circuit production.",
        ).to_dict()

    if "rocket" in objective_lower or KOREAN_ROCKET in objective:
        if total_iron < 10:
            return StrategicDecision(
                selected_skill="produce_iron_plate",
                priority=95,
                reason="Rocket program starts with basic iron production; iron is not established.",
                evidence=[f"iron_plate_total={total_iron}"],
                blockers=["basic iron supply"],
                expected_effect="Bootstrap the first iron plates.",
            ).to_dict()
        if not automation_researched:
            return StrategicDecision(
                selected_skill="research_automation",
                priority=90,
                reason="The rocket program needs assembler-based automation; Automation has not been researched yet.",
                evidence=[f"automation_science_pack_total={science}", "automation_researched=false"],
                blockers=["automation research"],
                expected_effect="Build and feed a powered lab to unlock assembling-machine-1.",
            ).to_dict()
        if not _circuit_automation_ready(observation):
            return StrategicDecision(
                selected_skill="automate_electronic_circuit_line",
                priority=85,
                reason="Automation is researched, but the first powered green circuit assembler cell is not ready.",
                evidence=[
                    f"automation_science_pack_total={science}",
                    f"electronic_circuit_total={circuits}",
                    "automation_researched=true",
                ],
                blockers=["assembler-based electronic circuit production"],
                expected_effect="Build the first powered assembler cell for electronic circuit production.",
            ).to_dict()
        if not _technology_researched(observation, "logistics"):
            return StrategicDecision(
                selected_skill="research_logistics",
                priority=82,
                reason="The rocket program needs the next science tier; Logistics is the next red-science research step.",
                evidence=[
                    f"automation_science_pack_total={science}",
                    f"electronic_circuit_total={circuits}",
                    "logistics_researched=false",
                ],
                blockers=["logistics research"],
                expected_effect="Feed the powered lab with automation science to unlock Logistics.",
            ).to_dict()
        if top_layout_item is not None and int(top_layout_item.get("severity") or 0) >= 75:
            return StrategicDecision(
                selected_skill="plan_factory_site",
                priority=min(83, int(top_layout_item.get("severity") or 75)),
                reason=f"Urgent rocket prerequisites are not currently blocking progress, so use idle strategy time to improve factory layout: {top_layout_item.get('detail')}",
                evidence=[
                    f"layout_kind={top_layout_item.get('kind')}",
                    f"severity={top_layout_item.get('severity')}",
                    f"site_id={top_layout_item.get('site_id')}",
                ],
                blockers=[],
                expected_effect="Generate a site-level improvement plan before placing more inefficient factory blocks.",
            ).to_dict()
        return StrategicDecision(
            selected_skill="produce_automation_science_pack",
            priority=80,
            reason="Automation and Logistics are researched; keep building science capacity for the next tier.",
            evidence=[
                f"automation_science_pack_total={science}",
                f"iron_plate_total={total_iron}",
                "logistics_researched=true",
            ],
            blockers=["next science tier"],
            expected_effect="Produce more automation science while the next science-pack executor is implemented.",
        ).to_dict()

    if total_iron < 10:
        selected = "produce_iron_plate"
        reason = "Default strategy found low iron plates."
    else:
        if top_layout_item is not None and int(top_layout_item.get("severity") or 0) >= 75:
            selected = "plan_factory_site"
            reason = f"Default strategy uses idle cycles to improve factory layout: {top_layout_item.get('kind')}"
        else:
            selected = "produce_automation_science_pack"
            reason = "Default strategy advances to first science after basic iron."
    return StrategicDecision(
        selected_skill=selected,
        priority=50,
        reason=reason,
        evidence=[f"iron_plate_total={total_iron}", f"automation_science_pack_total={science}"],
        blockers=[],
        expected_effect=f"Run {selected} skill.",
        source="heuristic",
    ).to_dict()


def _top_layout_item(issues: list[Any], opportunities: list[Any]) -> dict[str, Any] | None:
    candidates = [item for item in issues + opportunities if isinstance(item, dict)]
    if not candidates:
        return None
    return max(candidates, key=lambda item: int(item.get("severity") or 0))


def _bounded_int(value: Any, fallback: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(maximum, parsed))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return [str(value)] if value else []
    return [str(item) for item in value]


def _first_power_issue(monitor: dict[str, Any]) -> dict[str, Any] | None:
    networks = monitor.get("power_networks") if isinstance(monitor.get("power_networks"), list) else []
    for row in networks:
        if not isinstance(row, dict):
            continue
        if row.get("status") in {"insufficient_generation", "no_generation", "unconnected_consumers"}:
            return row
    return None


def _skill_for_bottleneck_item(item: str, observation: dict[str, Any]) -> str | None:
    if item == "coal":
        return "setup_coal_supply"
    if item in {"iron-plate", "iron-ore", "steel-plate"}:
        return "expand_iron_smelting"
    if item in {"copper-plate", "copper-ore", "copper-cable"}:
        return "expand_copper_smelting"
    if item == "automation-science-pack":
        return "produce_automation_science_pack"
    if item == "electronic-circuit":
        if _technology_researched(observation, "automation"):
            return "automate_electronic_circuit_line"
        return "produce_electronic_circuit"
    return None


_COAL_DEPENDENT_SKILLS = {
    "build_belt_smelting_line",
    "expand_iron_smelting",
    "expand_copper_smelting",
    "setup_power",
}


def _coal_supply_needed(observation: dict[str, Any]) -> bool:
    resources = observation.get("resources") if isinstance(observation.get("resources"), list) else []
    if not any(isinstance(item, dict) and item.get("name") == "coal" for item in resources):
        return False
    if _coal_supply_ready(observation):
        return False
    if inventory_count(observation, "coal") >= 24:
        return False
    return _fuel_dependent_factory_exists(observation) or _coal_patch_is_near_player(observation)


def _coal_supply_ready(observation: dict[str, Any]) -> bool:
    coal_drills = [
        entity
        for entity in entities_named(observation, "burner-mining-drill") + entities_named(observation, "electric-mining-drill")
        if _entity_on_resource(observation, entity, "coal")
    ]
    for drill in coal_drills:
        name = str(drill.get("name") or "")
        if name == "electric-mining-drill" and drill.get("electric_network_connected") is not False:
            return True
        if entity_item_count(drill, "coal") > 0:
            return True
    return False


def _entity_on_resource(observation: dict[str, Any], entity: dict[str, Any], resource_name: str) -> bool:
    position = entity.get("position") if isinstance(entity.get("position"), dict) else None
    if position is None:
        return False
    resources = observation.get("resources") if isinstance(observation.get("resources"), list) else []
    for resource in resources:
        if not isinstance(resource, dict) or resource.get("name") != resource_name:
            continue
        resource_position = resource.get("position") if isinstance(resource.get("position"), dict) else None
        if resource_position is not None and distance(position, resource_position) <= 4.5:
            return True
    return False


def _fuel_dependent_factory_exists(observation: dict[str, Any]) -> bool:
    for name in ("stone-furnace", "boiler", "burner-inserter"):
        if entities_named(observation, name):
            return True
    for drill in entities_named(observation, "burner-mining-drill"):
        if not _entity_on_resource(observation, drill, "coal"):
            return True
    return False


def _coal_patch_is_near_player(observation: dict[str, Any]) -> bool:
    player = observation.get("player") if isinstance(observation.get("player"), dict) else {}
    position = player.get("position") if isinstance(player.get("position"), dict) else {"x": 0, "y": 0}
    resources = observation.get("resources") if isinstance(observation.get("resources"), list) else []
    for resource in resources:
        if not isinstance(resource, dict) or resource.get("name") != "coal":
            continue
        resource_position = resource.get("position") if isinstance(resource.get("position"), dict) else None
        if resource_position is not None and distance(position, resource_position) <= 32.0:
            return True
    return False


def _target_deficit_exists(
    objective: str,
    observation: dict[str, Any],
    production_targets: dict[str, float] | None,
    item: str,
) -> bool:
    if not production_targets or float(production_targets.get(item) or 0.0) <= 0.0:
        return False
    monitor = summarize_factory(observation, objective, production_targets=production_targets)
    target_status = monitor.get("target_status")
    if isinstance(target_status, dict):
        target_rows = target_status.get("items") if isinstance(target_status.get("items"), list) else []
    else:
        target_rows = target_status if isinstance(target_status, list) else []
    for row in target_rows:
        if not isinstance(row, dict) or row.get("item") != item:
            continue
        return float(row.get("deficit_per_minute") or 0.0) > 0.0
    return False


def _technology_researched(observation: dict[str, Any], technology: str) -> bool:
    research = observation.get("research")
    if not isinstance(research, dict):
        return False
    technologies = research.get("technologies")
    if not isinstance(technologies, dict):
        return False
    state = technologies.get(technology)
    return bool(isinstance(state, dict) and state.get("researched"))


def _circuit_automation_ready(observation: dict[str, Any]) -> bool:
    entities = observation.get("entities")
    if not isinstance(entities, list):
        return False
    has_cable = False
    has_circuit = False
    for entity in entities:
        if not isinstance(entity, dict) or entity.get("name") not in {"assembling-machine-1", "assembling-machine-2", "assembling-machine-3"}:
            continue
        if entity.get("electric_network_connected") is False:
            continue
        if entity.get("recipe") == "copper-cable":
            has_cable = True
        if entity.get("recipe") == "electronic-circuit":
            has_circuit = True
    return has_cable and has_circuit


def _entity_centroid(observation: dict[str, Any]) -> dict[str, float] | None:
    entities = observation.get("entities")
    if not isinstance(entities, list):
        return None
    positions = [_position(item) for item in entities if isinstance(item, dict)]
    positions = [item for item in positions if item is not None]
    if not positions:
        return None
    return {
        "x": round(sum(item["x"] for item in positions) / len(positions), 2),
        "y": round(sum(item["y"] for item in positions) / len(positions), 2),
    }


def _resource_summary(observation: dict[str, Any], minimum_distance: float = 0.0) -> list[dict[str, Any]]:
    resources = observation.get("resources")
    if not isinstance(resources, list):
        return []
    player = observation.get("player") if isinstance(observation.get("player"), dict) else {}
    player_position = player.get("position") if isinstance(player.get("position"), dict) else None
    rows: list[dict[str, Any]] = []
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        position = _position(resource)
        distance_value = _distance(player_position, position)
        if distance_value is not None and distance_value < minimum_distance:
            continue
        rows.append(
            {
                "name": resource.get("name"),
                "position": position,
                "amount": resource.get("amount"),
                "distance_from_player": round(distance_value, 1) if distance_value is not None else None,
            }
        )
    return rows[:30]


def _position(value: dict[str, Any]) -> dict[str, float] | None:
    raw = value.get("position")
    if not isinstance(raw, dict):
        return None
    try:
        return {"x": float(raw["x"]), "y": float(raw["y"])}
    except (KeyError, TypeError, ValueError):
        return None


def _distance(a: dict[str, Any] | None, b: dict[str, Any] | None) -> float | None:
    if not a or not b:
        return None
    try:
        dx = float(a["x"]) - float(b["x"])
        dy = float(a["y"]) - float(b["y"])
    except (KeyError, TypeError, ValueError):
        return None
    return (dx * dx + dy * dy) ** 0.5


def _nearest_enemy(enemies: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not enemies:
        return None
    return min(enemies, key=lambda item: _enemy_distance(item) or 999999.0)


def _armed_gun_turret_count(observation: dict[str, Any]) -> int:
    return sum(1 for turret in entities_named(observation, "gun-turret") if entity_item_count(turret, "firearm-magazine") > 0)


def _enemy_distance(enemy: dict[str, Any] | None) -> float | None:
    if not enemy:
        return None
    try:
        return float(enemy.get("distance"))
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _danger_level(
    nearest_enemy: dict[str, Any] | None,
    nearest_distance: float | None,
    damage_events: list[dict[str, Any]] | None = None,
    max_spawner_pollution: float = 0.0,
) -> str:
    damage_events = damage_events or []
    if any(item.get("action") == "destroyed" for item in damage_events):
        return "critical"
    if damage_events:
        return "high"
    if max_spawner_pollution > 0:
        return "high"
    if nearest_distance is None:
        return "none"
    nearest_type = str((nearest_enemy or {}).get("type") or "")
    if nearest_distance <= 32:
        return "critical"
    if nearest_type == "unit" and nearest_distance <= 64:
        return "high"
    if nearest_distance <= 128:
        return "medium"
    return "low"


def _build_item_stock_floor(item: str) -> int:
    return {
        "transport-belt": 50,
        "inserter": 20,
        "burner-inserter": 10,
        "burner-mining-drill": 5,
        "stone-furnace": 10,
        "small-electric-pole": 20,
        "assembling-machine-1": 10,
    }.get(item, 5)
