from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil
from typing import Any

from .knowledge import dependency_tree_for_objective
from .monitor import recent_damage_events, summarize_factory
from .models import distance, entities_named, entity_item_count, inventory_count, player_position, total_item_count
from .planner import (
    _find_gear_belt_mall_relocation_layout,
    _gear_belt_mall_relocation_power_corridor_positions,
    _missing_power_corridor_positions,
    factory_layout_issues,
    factory_layout_opportunities,
    factory_layout_simulation_candidates,
    factory_layout_structure,
)
from .site_selection import sanitize_selected_improvement_site


KOREAN_ELECTRONIC_CIRCUIT = "\uc804\uc790\ud68c\ub85c"
KOREAN_ROCKET = "\ub85c\ucf13"
BUILD_ITEM_MALL_ITEMS = [
    "transport-belt",
    "inserter",
    "long-handed-inserter",
    "burner-inserter",
    "firearm-magazine",
    "gun-turret",
    "burner-mining-drill",
    "electric-mining-drill",
    "stone-furnace",
    "small-electric-pole",
    "assembling-machine-1",
]
PRE_RAIL_GEAR_MALL_PLATE_DISTANCE_LIMIT = 128.0
SMALL_POWER_POLE_REACH = 7.5
GEAR_MALL_RELOCATION_FIXED_COST = 18.0
GEAR_MALL_RELOCATION_POWER_POLE_COST = 2.0
GEAR_MALL_PLATE_BELT_TILE_COST = 1.0
GEAR_MALL_RELOCATION_ADVANTAGE_RATIO = 0.75
POWER_ANCHOR_ENTITY_NAMES = {
    "small-electric-pole",
    "medium-electric-pole",
    "big-electric-pole",
    "substation",
    "steam-engine",
}
ASSEMBLER_ENTITY_NAMES = {"assembling-machine-1", "assembling-machine-2", "assembling-machine-3"}
CRITICAL_FACTORY_POWER_RECIPES = set(BUILD_ITEM_MALL_ITEMS) | {
    "automation-science-pack",
    "logistic-science-pack",
    "chemical-science-pack",
    "copper-cable",
    "electronic-circuit",
    "iron-gear-wheel",
    "long-handed-inserter",
}


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
        description="Build additional belt-fed iron ore mining and smelting capacity after belt production is automated.",
        executor="ExpandIronSmeltingSkill",
        preconditions=[
            "Automation researched and transport-belt production automated",
            "iron ore patch identified",
            "fuel or power available",
            "furnaces, drills, inserters, and belts craftable or available",
        ],
        completion=["sustained iron plate output on belts or machine outputs exceeds downstream demand"],
        llm_scope="Choose this when downstream goals are blocked by low iron throughput.",
    ),
    "expand_copper_smelting": SkillContract(
        name="expand_copper_smelting",
        description="Build additional belt-fed copper ore mining and smelting capacity after belt production is automated.",
        executor="ExpandCopperSmeltingSkill",
        preconditions=[
            "Automation researched and transport-belt production automated",
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
            "transport belts are automated or explicitly available for this line",
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
    "setup_stone_supply": SkillContract(
        name="setup_stone_supply",
        description="Build a starter stone mining site with a burner mining drill outputting into a chest.",
        executor="StoneSupplySkill",
        preconditions=[
            "stone patch identified",
            "burner mining drill and wooden or iron chest available or craftable",
            "short-term hand fuel available to prime the first stone drill",
        ],
        completion=["stone is mined by a fueled burner mining drill and buffered in an output chest"],
        llm_scope=(
            "Choose this when furnaces or burner drills are blocked by repeated stone collection. "
            "Executor places the exact drill/chest pair and starter fuel."
        ),
    ),
    "connect_coal_fuel_feed": SkillContract(
        name="connect_coal_fuel_feed",
        description="Connect a starter coal belt to nearby burner fuel consumers, including boilers, with belts and inserters.",
        executor="CoalFuelFeedSkill",
        preconditions=[
            "fueled coal mining patch with output belt",
            "nearby burner furnace or boiler fuel consumer, or room for a starter furnace fuel receiver",
            "transport-belt production automated or existing belt stock available for this route",
            "burner inserter available for boiler feed, or an inserter appropriate for a powered local consumer",
        ],
        completion=["coal route to the local fuel consumer is observed as belt-fed", "boiler fuel is delivered by belt/inserter rather than repeated manual insertion"],
        llm_scope=(
            "Choose this after coal mining exists but site-level coal links are still route_needed. "
            "Executor places exact belt extension, burner inserter, and fuel consumer connection."
        ),
    ),
    "produce_copper_plate": SkillContract(
        name="produce_copper_plate",
        description="Create or replenish copper plate supply with a direct burner-drill smelting cell before belt automation.",
        executor="CopperPlateSkill",
        preconditions=["reachable copper ore", "fuel or power available", "burner drill and furnace available or craftable"],
        completion=["copper plates exist in player inventory or automated copper smelting output"],
        llm_scope="Choose this when circuits, science, or cable are blocked by copper; the executor must not hand-mine copper ore.",
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
    "bootstrap_power_pole_mall": SkillContract(
        name="bootstrap_power_pole_mall",
        description="Automate small electric pole production when site relocation or power corridors are blocked by pole count.",
        executor="BuildItemMallSkill",
        preconditions=[
            "automation researched",
            "electric power available",
            "wood and copper cable are available or producible",
            "a powered mall cell can be placed near existing factory logistics",
        ],
        completion=[
            "small electric poles are produced by a powered assembler",
            "power corridor construction no longer depends on hand-crafting every pole",
        ],
        llm_scope=(
            "Choose this when relocation, power expansion, or new factory sites are blocked by insufficient small electric poles. "
            "Executor uses the generic build-item mall with target item small-electric-pole."
        ),
    ),
    "research_electric_mining_drill": SkillContract(
        name="research_electric_mining_drill",
        description="Research electric mining drills so burner miners can be replaced soon after power is stable.",
        executor="ResearchTechnologySkill",
        preconditions=[
            "powered lab exists or can be fed",
            "automation science packs are available or producible",
            "burner mining drills are still active in factory sites",
        ],
        completion=["electric-mining-drill recipe is unlocked"],
        llm_scope=(
            "Choose this after Automation and stable power when burner mining drills are still supplying production. "
            "Do not postpone indefinitely behind nonblocking layout diagnostics."
        ),
    ),
    "bootstrap_electric_mining_drill_mall": SkillContract(
        name="bootstrap_electric_mining_drill_mall",
        description="Automate electric mining drill production to replace burner mining drills and reduce coal-fuel maintenance.",
        executor="BuildItemMallSkill",
        preconditions=[
            "electric-mining-drill technology researched",
            "electric power available",
            "iron plates, iron gears, and electronic circuits available through nearby logistics or producer cells",
            "burner mining drills remain in active factory sites",
        ],
        completion=[
            "electric mining drills are produced by a powered assembler",
            "burner mining drill replacement can proceed without hand-crafting each drill",
        ],
        llm_scope=(
            "Choose this once the recipe is researched and burner miners remain. "
            "Executor reuses the generic build-item mall with target item electric-mining-drill."
        ),
    ),
    "build_gear_belt_mall_logistics": SkillContract(
        name="build_gear_belt_mall_logistics",
        description="Connect an iron-gear assembler to a transport-belt assembler without player gear crafting or gear hand-carry.",
        executor="GearBeltMallLogisticsSkill",
        preconditions=[
            "automation researched",
            "electric power available",
            "powered iron-gear assembler and reusable adjacent assembler are available",
            "short bootstrap belts and inserters are available or recoverable without hand-crafting gears",
            "existing inventory iron plates may be used only as a one-time assembler seed; distant plate shuttle loops are not allowed",
        ],
        completion=[
            "iron gears move toward the transport-belt assembler through inserters and belts",
            "transport-belt production no longer depends on player gear collection",
        ],
        llm_scope=(
            "Choose this when belt automation is blocked by gear mall output logistics or when the belt mall needs a one-time iron seed before it can replenish construction belts. "
            "Executor handles exact belt lane, inserter direction, burner fuel, and recipe changes."
        ),
    ),
    "relocate_gear_belt_mall_to_iron_source": SkillContract(
        name="relocate_gear_belt_mall_to_iron_source",
        description="Move a pre-rail gear/belt mall close to its iron-plate source when the route cost model rejects a long belt recovery.",
        executor="GearBeltMallRelocationSkill",
        preconditions=[
            "automation researched",
            "existing gear and belt assemblers are known",
            "nearby iron-plate source furnace is known",
            "site cost model prefers relocation over extending the exhausted pre-rail belt route",
            "power corridor materials are available before existing assemblers are mined",
        ],
        completion=[
            "gear and belt assemblers are rebuilt near the iron-plate source",
            "follow-up local gear-to-belt logistics can be built without long player shuttles",
        ],
        llm_scope=(
            "Choose this when related starter sites are too far apart and construction belts are exhausted. "
            "Executor validates cost evidence, protects against premature teardown, and moves the exact assemblers."
        ),
    ),
    "build_iron_plate_logistic_line_to_gear_mall": SkillContract(
        name="build_iron_plate_logistic_line_to_gear_mall",
        description="Build a transport-belt logistics route from iron-plate furnace output toward the gear/belt mall.",
        executor="IronPlateLogisticLineToGearMallSkill",
        preconditions=[
            "automation researched",
            "gear/belt mall exists",
            "transport belts are available from the belt mall output or inventory",
            "iron-plate source furnace is known",
        ],
        completion=[
            "iron plates can move toward the iron-gear assembler by belt and inserter endpoints",
            "the gear/belt mall no longer depends on player iron-plate shuttling",
        ],
        llm_scope=(
            "Choose this when a gear or belt mall is blocked by missing iron-plate input logistics. "
            "Executor extends the belt route and endpoint inserters without crafting gears or carrying iron plates."
        ),
    ),
    "build_site_input_logistic_line": SkillContract(
        name="build_site_input_logistic_line",
        description="Build a transport-belt route for a repeated producer-to-consumer factory input.",
        executor="SiteInputLogisticLineSkill",
        preconditions=[
            "automation researched",
            "transport-belt assembler exists and belts are available from inventory or mall output",
            "source entity produces or buffers the repeated input item",
            "consumer assembler needs that item as a recipe ingredient",
        ],
        completion=[
            "the input item can move by belt and endpoint inserters from source to consumer",
            "the consumer no longer depends on player inventory shuttle loops for that repeated input",
        ],
        llm_scope=(
            "Choose this for route_needed or missing input links such as copper-plate, copper-cable, gears, or circuits "
            "after belt production exists. Executor chooses the exact route, blockers, and endpoint inserters."
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
    selected_improvement_site: dict[str, Any] | None = None,
) -> dict[str, Any]:
    monitor = summarize_factory(observation, objective, production_targets=production_targets)
    return {
        "objective": objective,
        "observation": observation,
        "production_targets": dict(sorted((production_targets or {}).items())),
        "selected_improvement_site": sanitize_selected_improvement_site(selected_improvement_site),
        "factory_monitor": monitor,
        "spatial_planning": make_spatial_planning_context(observation),
        "layout_improvement": make_layout_improvement_context(
            observation,
            selected_improvement_site=selected_improvement_site,
        ),
        "automation_policy": make_automation_policy_context(monitor),
        "build_item_supply": make_build_item_supply_context(observation, monitor),
        "research_planning": make_research_planning_context(observation, monitor),
        "threats": make_threat_context(observation),
        "power_networks": monitor.get("power_networks", []),
        "goal_dependency_tree": dependency_tree_for_objective(objective, max_depth=5),
        "available_skills": skill_catalog_payload(),
        "decision_rule": (
            "Select exactly one high-level skill. Diagnose bottlenecks first. "
            "Evaluate electric supply per connected power network, not as a single global pool. "
            "After the first bootstrap phase, prefer site-to-site logistic lines over hand crafting or hand-carrying items. "
            "Treat factory placement as site-graph optimization: compare belt, pipe, pole, and later rail distance costs "
            "against future input/output traffic, and co-locate tightly coupled producer/consumer sites unless a trunk or rail corridor is justified. "
            "Do not hard-ban factories near power blocks, but account for lost boiler, engine, pole, fuel, water, and future power expansion clearance. "
            "When new buildings, inserters, modules, beacons, rails, quality tiers, or logistics tools unlock, re-evaluate site layout candidates because optimal footprint and bottlenecks can change. "
            "For spatial work, choose districts, corridors, or rail topology only. "
            "When urgent production, defense, research, and power work are satisfied, use idle LLM cycles "
            "to improve factory site layout against reusable blueprint-style patterns. "
            "Do not emit tile-level movement, mining, building, rail, or signal actions."
        ),
    }


def make_layout_improvement_context(
    observation: dict[str, Any],
    *,
    selected_improvement_site: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issues = factory_layout_issues(observation)
    opportunities = factory_layout_opportunities(observation)
    selected_site = sanitize_selected_improvement_site(selected_improvement_site)
    if selected_site:
        opportunities = [_selected_site_layout_focus(selected_site)] + opportunities
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
        "selected_improvement_site": selected_site,
        "site_structure": factory_layout_structure(observation),
        "layout_capabilities": make_layout_capability_context(observation),
        "issues": issues,
        "opportunities": opportunities,
        "simulation_candidates": candidates,
        "patterns": [
            "parallel smelting columns with shared ore/fuel/input and plate output lanes",
            "green circuit cells near iron/copper supply, roughly 3 cable assemblers per 2 circuit assemblers",
            "starter mall row near iron, gear, and circuit supply with shared inputs and chest outputs",
            "short lab daisy chain or science belt feed with room for later science colors",
            "site-to-site logistic lines for repeated inputs instead of player inventory shuttle loops",
            "main bus or trunk corridors before dense consumer blocks",
            "remote outpost plus rail corridor when belts or walking logistics become too long",
        ],
    }


def make_layout_capability_context(observation: dict[str, Any]) -> dict[str, Any]:
    inserters = {
        "burner-inserter": {
            "available": total_item_count(observation, "burner-inserter") > 0,
            "stock": total_item_count(observation, "burner-inserter"),
            "layout_impact": "fuel-dependent inserter; use only for bootstrap or burner fuel loops",
        },
        "inserter": {
            "available": total_item_count(observation, "inserter") > 0 or _technology_researched(observation, "automation"),
            "stock": total_item_count(observation, "inserter"),
            "layout_impact": "standard one-tile pickup/drop for most early assembler and belt layouts",
        },
        "long-handed-inserter": {
            "available": _technology_researched(observation, "long-inserters")
            or _recipe_unlocked(observation, "long-handed-inserter")
            or total_item_count(observation, "long-handed-inserter") > 0
            or _recipe_assembler_exists(observation, "long-handed-inserter"),
            "stock": total_item_count(observation, "long-handed-inserter"),
            "researched": _technology_researched(observation, "long-inserters"),
            "recipe_unlocked": _recipe_unlocked(observation, "long-handed-inserter"),
            "automated": _recipe_assembler_exists(observation, "long-handed-inserter"),
            "layout_impact": (
                "can reach across one intervening tile or belt lane, enabling denser 2-belt input layouts, "
                "cleaner assembler rows, and fewer awkward belt doglegs"
            ),
        },
    }
    modules = {
        name: {
            "available": _technology_researched(observation, name)
            or _recipe_unlocked(observation, name)
            or total_item_count(observation, name) > 0,
            "researched": _technology_researched(observation, name),
            "recipe_unlocked": _recipe_unlocked(observation, name),
            "stock": total_item_count(observation, name),
            "layout_impact": "module availability can change assembler count, power demand, pollution, and beacon-ready spacing",
        }
        for name in ("speed-module", "productivity-module", "efficiency-module")
    }
    machine_techs = {"assembling-machine-2": "automation-2", "assembling-machine-3": "automation-3"}
    machines = {
        name: {
            "available": _technology_researched(observation, technology)
            or _recipe_unlocked(observation, name)
            or total_item_count(observation, name) > 0
            or _entity_count_by_name(observation, name) > 0,
            "researched": _technology_researched(observation, technology),
            "recipe_unlocked": _recipe_unlocked(observation, name),
            "stock": total_item_count(observation, name),
            "built": _entity_count_by_name(observation, name),
            "layout_impact": "higher tier machines change throughput, module slots, power demand, and site footprint",
        }
        for name, technology in machine_techs.items()
    }
    furnace_techs = {"steel-furnace": "advanced-material-processing", "electric-furnace": "advanced-material-processing-2"}
    furnaces = {
        name: {
            "available": _technology_researched(observation, technology)
            or _recipe_unlocked(observation, name)
            or total_item_count(observation, name) > 0
            or _entity_count_by_name(observation, name) > 0,
            "researched": _technology_researched(observation, technology),
            "recipe_unlocked": _recipe_unlocked(observation, name),
            "stock": total_item_count(observation, name),
            "built": _entity_count_by_name(observation, name),
            "layout_impact": "higher tier furnaces change column size, fuel or power routing, pollution, and output density",
        }
        for name, technology in furnace_techs.items()
    }
    beacons = {
        "beacon": {
            "available": _technology_researched(observation, "effect-transmission")
            or _recipe_unlocked(observation, "beacon")
            or total_item_count(observation, "beacon") > 0
            or _entity_count_by_name(observation, "beacon") > 0,
            "researched": _technology_researched(observation, "effect-transmission"),
            "recipe_unlocked": _recipe_unlocked(observation, "beacon"),
            "stock": total_item_count(observation, "beacon"),
            "built": _entity_count_by_name(observation, "beacon"),
            "layout_impact": "beacons require reserved spacing and can make earlier compact rows obsolete",
        }
    }
    return {
        "llm_responsibility": (
            "When proposing or ranking site layouts, account for currently researched, stocked, or automated buildings, "
            "inserters, modules, quality, and logistics options. A layout optimal before a new item may become obsolete after unlock."
        ),
        "inserters": inserters,
        "modules": modules,
        "machines": machines,
        "furnaces": furnaces,
        "beacons": beacons,
        "rerank_trigger": bool(
            inserters["long-handed-inserter"]["available"]
            or any(row["available"] for row in modules.values())
            or any(row["available"] for row in machines.values())
            or any(row["available"] for row in furnaces.values())
            or any(row["available"] for row in beacons.values())
        ),
        "constraints": [
            "prefer long-handed inserters when they reduce belt crossings, input bottlenecks, or site footprint without harming expansion access",
            "re-evaluate existing sites after new machines, inserters, modules, beacons, rails, or quality tiers unlock",
            "record before/after layout metrics when a newly unlocked item improves footprint, throughput, power, pollution, or bottlenecks",
        ],
    }


def _recipe_assembler_exists(observation: dict[str, Any], recipe: str) -> bool:
    entities = observation.get("entities")
    if not isinstance(entities, list):
        return False
    return any(
        isinstance(assembler, dict)
        and str(assembler.get("name") or "") in ASSEMBLER_ENTITY_NAMES
        and assembler.get("recipe") == recipe
        and assembler.get("electric_network_connected") is not False
        for assembler in entities
    )


def make_automation_policy_context(monitor: dict[str, Any]) -> dict[str, Any]:
    links = monitor.get("logistics_links") if isinstance(monitor.get("logistics_links"), list) else []
    sites = monitor.get("factory_sites") if isinstance(monitor.get("factory_sites"), list) else []
    automated_items = {
        "iron-plate",
        "copper-plate",
        "iron-gear-wheel",
        "copper-cable",
        "electronic-circuit",
        "automation-science-pack",
        "logistic-science-pack",
    }
    route_needed = [
        link
        for link in links
        if isinstance(link, dict)
        and link.get("status") == "route_needed"
        and link.get("item") in automated_items
    ]
    manual_sites = [
        site
        for site in sites
        if isinstance(site, dict)
        and ("manual" in str(site.get("automation_level") or "") or "manual" in str(site.get("status") or ""))
    ]
    return {
        "principle": (
            "Factorio progress should become factory automation, not repeated player inventory transport. "
            "Manual crafting, taking, and inserting are acceptable only for short bootstrap or one-time priming."
        ),
        "route_needed_links": route_needed[:8],
        "manual_sites": manual_sites[:8],
        "recommended_skill": "plan_factory_site" if route_needed or manual_sites else None,
        "constraints": [
            "prefer belt, inserter, chest, pipe, or train links between producer and consumer sites",
            "place related starter sites close enough for short local belts until a bus or rail system exists",
            "do not scale a consumer site while its repeated inputs are supplied by hand-carry",
        ],
    }


def _selected_site_layout_focus(site: dict[str, Any]) -> dict[str, Any]:
    kind = str(site.get("kind") or "factory_site")
    item = str(site.get("item") or "")
    item_suffix = f" for {item}" if item else ""
    return {
        "kind": "operator_selected_site",
        "severity": 86,
        "item": item or None,
        "site_id": site.get("site_id"),
        "detail": f"operator selected {kind}{item_suffix} as the next layout improvement focus",
        "recommendation": (
            "prioritize layout diagnosis, prerequisite tasks, and candidate anchors for this selected site "
            "before proposing unrelated factory expansion"
        ),
        "selected_site": site,
        "manual_selection": True,
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
        available_for_mall = _build_item_mall_item_available(observation, item)
        items.append(
            {
                "item": item,
                "stock": stock,
                "estimated_per_minute": estimated,
                "automated": estimated > 0.0,
                "available_for_mall": available_for_mall,
                "needs_mall": available_for_mall and stock < _build_item_stock_floor(item) and estimated <= 0.0,
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


def _build_item_mall_item_available(observation: dict[str, Any], item: str) -> bool:
    if item == "long-handed-inserter":
        return (
            _technology_researched(observation, "long-inserters")
            or _recipe_unlocked(observation, item)
            or total_item_count(observation, item) > 0
            or _recipe_assembler_exists(observation, item)
        )
    return True


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
                "world_map_memory": observation.get("world_map_memory")
                if isinstance(observation.get("world_map_memory"), dict)
                else {},
            },
            "constraints": [
                "use remembered resource clusters, water anchors, factory zones, and sparse feature cells before requesting another full map/site scan",
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
    result = StrategicDecision(
        selected_skill=selected,
        priority=priority,
        reason=str(raw.get("reason") or raw.get("justification") or raw.get("recommendation") or ""),
        evidence=evidence,
        blockers=blockers,
        expected_effect=str(raw.get("expected_effect") or ""),
        source=str(raw.get("source") or "llm"),
    ).to_dict()
    if selected == "bootstrap_build_item_mall":
        target_item = _sanitize_build_item_target(raw.get("target_item") or raw.get("item"))
        if target_item:
            result["target_item"] = target_item
        target_count = _positive_int_or_none(raw.get("target_count") or raw.get("count"))
        if target_count is not None:
            result["target_count"] = target_count
    if selected == "build_site_input_logistic_line":
        input_item = _sanitize_site_input_target(raw.get("input_item") or raw.get("item") or raw.get("target_item"))
        if input_item:
            result["input_item"] = input_item
    return result


def reconcile_strategy_decision(
    decision: dict[str, Any],
    objective: str,
    observation: dict[str, Any],
    production_targets: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Apply deterministic safety/feasibility guardrails to an LLM strategy choice."""

    selected = str(decision.get("selected_skill") or decision.get("selected_goal") or "")
    rocket_objective = _is_rocket_objective(objective)
    remote_guardrail = decision.get("guardrail_adjusted") if isinstance(decision.get("guardrail_adjusted"), dict) else {}
    if remote_guardrail.get("from") == "plan_factory_site" and selected == remote_guardrail.get("to"):
        # Remote Slurm workers may run slightly older source. Recompute plan-site guardrails
        # locally so target deficits use the current monitor semantics.
        decision = dict(decision)
        decision["selected_skill"] = "plan_factory_site"
        decision["reason"] = ""
        decision["blockers"] = []
        decision["evidence"] = []
        decision["expected_effect"] = ""
        decision.pop("guardrail_adjusted", None)
        selected = "plan_factory_site"
    if remote_guardrail.get("from") == "bootstrap_build_item_mall" and selected == remote_guardrail.get("to"):
        # Remote Slurm workers may have already applied older pre-automation mall guardrails.
        # Recompute locally so fresh starter worlds bootstrap plates before research/mall planning.
        decision = dict(decision)
        decision["selected_skill"] = "bootstrap_build_item_mall"
        decision["reason"] = ""
        decision["blockers"] = []
        decision["evidence"] = []
        decision["expected_effect"] = ""
        decision.pop("guardrail_adjusted", None)
        selected = "bootstrap_build_item_mall"
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
    if selected == "bootstrap_build_item_mall" and not _technology_researched(observation, "automation"):
        iron_total = total_item_count(observation, "iron-plate")
        if rocket_objective and iron_total < 10:
            adjusted = dict(decision)
            adjusted["selected_skill"] = "produce_iron_plate"
            adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 95)
            original_reason = str(decision.get("reason") or "").strip()
            guardrail_reason = (
                "LLM selected the build-item mall before Automation, but the starter base has not "
                "bootstrapped basic iron plates yet."
            )
            adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
            adjusted["blockers"] = sorted(set(_string_list(decision.get("blockers")) + ["basic iron supply"]))
            adjusted["evidence"] = _string_list(decision.get("evidence")) + [
                "guardrail_adjusted_from=bootstrap_build_item_mall",
                "automation_researched=false",
                f"iron_plate_total={iron_total}",
            ]
            adjusted["expected_effect"] = (
                "Bootstrap direct burner drill to stone furnace iron smelting before research or mall planning."
            )
            adjusted["guardrail_adjusted"] = {
                "from": "bootstrap_build_item_mall",
                "to": "produce_iron_plate",
                "reason": guardrail_reason,
            }
            return adjusted
        adjusted = dict(decision)
        adjusted["selected_skill"] = "research_automation"
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 90)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            "LLM selected the build-item mall, but assembling-machine automation is not researched yet."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(set(_string_list(decision.get("blockers")) + ["automation research"]))
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            "guardrail_adjusted_from=bootstrap_build_item_mall",
            "automation_researched=false",
        ]
        adjusted["expected_effect"] = "Research Automation before attempting assembler-based item mall production."
        adjusted["guardrail_adjusted"] = {
            "from": "bootstrap_build_item_mall",
            "to": "research_automation",
            "reason": guardrail_reason,
        }
        return adjusted
    if selected == "connect_coal_fuel_feed" and not _transport_belt_automation_ready(observation):
        adjusted = dict(decision)
        automation_researched = _technology_researched(observation, "automation")
        target_skill = "build_gear_belt_mall_logistics" if automation_researched else "research_automation"
        adjusted["selected_skill"] = target_skill
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 87)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            "LLM selected a site-to-site coal belt feed before transport-belt production is automated."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(
            set(_string_list(decision.get("blockers")) + ["transport-belt automation before site links"])
        )
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            "guardrail_adjusted_from=connect_coal_fuel_feed",
            "transport_belt_automation_ready=false",
        ]
        adjusted["expected_effect"] = (
            "Automate gear-fed belt production before spending scarce bootstrap items on site-to-site fuel paths."
        )
        adjusted["guardrail_adjusted"] = {
            "from": "connect_coal_fuel_feed",
            "to": target_skill,
            "reason": guardrail_reason,
        }
        return adjusted
    if selected in {"build_belt_smelting_line", "expand_iron_smelting", "expand_copper_smelting"} and not _transport_belt_automation_ready(observation):
        adjusted = dict(decision)
        automation_researched = _technology_researched(observation, "automation")
        if automation_researched:
            target_skill = "build_gear_belt_mall_logistics"
        elif selected == "expand_copper_smelting":
            target_skill = "produce_copper_plate"
        else:
            target_skill = "produce_iron_plate"
        adjusted["selected_skill"] = target_skill
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 86)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            "LLM selected belt-fed smelting before transport-belt production is automated; "
            "use direct burner-drill bootstrap cells first, then expand with belts after the belt mall exists."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(
            set(_string_list(decision.get("blockers")) + ["transport-belt automation before belt smelting expansion"])
        )
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            f"guardrail_adjusted_from={selected}",
            "transport_belt_automation_ready=false",
            f"automation_researched={str(automation_researched).lower()}",
        ]
        adjusted["expected_effect"] = (
            "Avoid spending early hand-crafted belts on smelting; establish direct plate supply or automate gear-fed belts first."
        )
        adjusted["guardrail_adjusted"] = {
            "from": selected,
            "to": target_skill,
            "reason": guardrail_reason,
        }
        return adjusted
    if (
        selected in _COAL_DEPENDENT_SKILLS
        and _coal_fuel_feed_needed(observation, objective, production_targets)
        and _transport_belt_automation_ready(observation)
    ):
        adjusted = dict(decision)
        adjusted["selected_skill"] = "connect_coal_fuel_feed"
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 86)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            f"LLM selected {selected}, but a nearby coal logistics link is still route_needed."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(set(_string_list(decision.get("blockers")) + ["coal fuel feed route"]))
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            f"guardrail_adjusted_from={selected}",
            "coal_fuel_feed_route_needed=true",
        ]
        adjusted["expected_effect"] = "Connect the starter coal belt to local burner fuel consumers before scaling demand."
        adjusted["guardrail_adjusted"] = {
            "from": selected,
            "to": "connect_coal_fuel_feed",
            "reason": guardrail_reason,
        }
        return adjusted
    if selected in {
        "setup_power",
        "plan_factory_site",
        "produce_electronic_circuit",
        "automate_electronic_circuit_line",
        "bootstrap_build_item_mall",
        "bootstrap_power_pole_mall",
        "build_iron_plate_logistic_line_to_gear_mall",
        "build_site_input_logistic_line",
        "research_electric_mining_drill",
        "produce_automation_science_pack",
    } and _boiler_coal_feed_should_preempt_power(observation):
        adjusted = dict(decision)
        adjusted["selected_skill"] = "connect_coal_fuel_feed"
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 91)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            "LLM selected powered work or setup_power, but the starter boiler is fuel-starved and "
            "belt-fed coal supply can be connected instead of repeating manual boiler insertion."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(set(_string_list(decision.get("blockers")) + ["boiler coal fuel feed"]))
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            f"guardrail_adjusted_from={selected}",
            "boiler_no_fuel=true",
            "coal_supply_ready=true",
            "transport_belt_automation_ready=true",
        ]
        adjusted["expected_effect"] = "Build a coal belt/inserter feed into the boiler instead of hand-feeding boiler fuel."
        adjusted["guardrail_adjusted"] = {
            "from": selected,
            "to": "connect_coal_fuel_feed",
            "reason": guardrail_reason,
        }
        return adjusted
    critical_factory_power_issue = _critical_factory_power_issue(observation)
    gear_belt_mall_power_issue = _gear_belt_mall_power_issue(observation)
    gear_belt_mall_bootstrap_issue = _gear_belt_mall_bootstrap_issue(observation)
    transport_belt_mall_gear_retool_issue = _transport_belt_mall_gear_retool_issue(observation)
    transport_belt_mall_retool_issue = _transport_belt_mall_retool_issue(observation)
    gear_mall_iron_plate_issue = _gear_mall_iron_plate_logistics_issue(observation)
    relocation_power_pole_deficit = _gear_mall_relocation_power_pole_deficit(gear_mall_iron_plate_issue, observation)
    power_recovery_waits_on_belt_mall = _power_recovery_waits_on_belt_mall(observation)
    if transport_belt_mall_gear_retool_issue is not None and selected in {
        "setup_power",
        "plan_factory_site",
        "bootstrap_build_item_mall",
        "bootstrap_power_pole_mall",
        "build_iron_plate_logistic_line_to_gear_mall",
        "build_site_input_logistic_line",
        "research_electric_mining_drill",
        "produce_automation_science_pack",
    }:
        adjusted = dict(decision)
        adjusted["selected_skill"] = "bootstrap_build_item_mall"
        adjusted["target_item"] = "transport-belt"
        adjusted["target_count"] = max(_positive_int_or_none(decision.get("target_count")) or 0, 20)
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 94)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            f"LLM selected {selected}, but the transport-belt assembler should be preserved and a nearby "
            "non-belt assembler can be retooled to iron-gear-wheel before repeating emergency boiler fueling."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(
            set(_string_list(decision.get("blockers")) + ["iron-gear assembler retooling before repeated power bootstrap"])
        )
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            f"guardrail_adjusted_from={selected}",
            f"belt_assembler_unit={transport_belt_mall_gear_retool_issue.get('belt_unit')}",
            f"gear_retool_assembler_unit={transport_belt_mall_gear_retool_issue.get('gear_unit')}",
            f"gear_retool_assembler_recipe={transport_belt_mall_gear_retool_issue.get('gear_recipe')}",
            f"mall_distance_tiles={transport_belt_mall_gear_retool_issue.get('mall_distance_tiles')}",
            "transport_belt_automation_ready=false",
            "preserve_transport_belt_assembler=true",
        ]
        adjusted["expected_effect"] = (
            "Set a nearby reusable assembler to iron gears while preserving the transport-belt assembler, "
            "then use the next power window to produce belts."
        )
        adjusted["guardrail_adjusted"] = {
            "from": selected,
            "to": "bootstrap_build_item_mall",
            "reason": guardrail_reason,
        }
        return adjusted
    if transport_belt_mall_retool_issue is not None and selected in {
        "setup_power",
        "plan_factory_site",
        "bootstrap_build_item_mall",
        "bootstrap_power_pole_mall",
        "build_iron_plate_logistic_line_to_gear_mall",
        "build_site_input_logistic_line",
        "research_electric_mining_drill",
        "produce_automation_science_pack",
    }:
        adjusted = dict(decision)
        adjusted["selected_skill"] = "bootstrap_build_item_mall"
        adjusted["target_item"] = "transport-belt"
        adjusted["target_count"] = max(_positive_int_or_none(decision.get("target_count")) or 0, 20)
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 93)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            f"LLM selected {selected}, but the boiler fuel route is blocked on transport-belt automation and "
            "an existing stocked small-electric-pole mall assembler can be safely retooled to transport-belt."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(
            set(_string_list(decision.get("blockers")) + ["transport-belt mall retooling before boiler fuel route"])
        )
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            f"guardrail_adjusted_from={selected}",
            f"retool_assembler_unit={transport_belt_mall_retool_issue.get('unit')}",
            f"retool_assembler_recipe={transport_belt_mall_retool_issue.get('recipe')}",
            f"small_electric_pole_stock={transport_belt_mall_retool_issue.get('small_electric_pole_stock')}",
            f"clear_item={transport_belt_mall_retool_issue.get('clear_item')}",
            "transport_belt_automation_ready=false",
        ]
        adjusted["expected_effect"] = (
            "Retool the stocked starter mall assembler to transport belts so belts can feed the boiler and later site links."
        )
        adjusted["guardrail_adjusted"] = {
            "from": selected,
            "to": "bootstrap_build_item_mall",
            "reason": guardrail_reason,
        }
        return adjusted
    if (
        _gear_mall_plate_route_needs_compaction(gear_mall_iron_plate_issue)
        and relocation_power_pole_deficit <= 0
        and (
            _power_issue_allows_pre_power_relocation(critical_factory_power_issue)
            or power_recovery_waits_on_belt_mall
            or bool(gear_mall_iron_plate_issue.get("relocation_in_progress"))
        )
        and selected in {
            "setup_power",
            "plan_factory_site",
            "relocate_gear_belt_mall_to_iron_source",
            "produce_electronic_circuit",
            "automate_electronic_circuit_line",
            "bootstrap_build_item_mall",
            "bootstrap_power_pole_mall",
            "research_electric_mining_drill",
            "bootstrap_electric_mining_drill_mall",
            "research_logistics",
            "build_iron_plate_logistic_line_to_gear_mall",
            "build_site_input_logistic_line",
        }
    ):
        adjusted = dict(decision)
        adjusted["selected_skill"] = "relocate_gear_belt_mall_to_iron_source"
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 94 if selected == "setup_power" else 93)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            f"LLM selected {selected}, but emergency power bootstrapping is only buying short windows while "
            "the gear/belt mall is 100+ tiles from iron plates; build the relocation power corridor and move "
            "the mall instead of repeating hand-fuel recovery."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(
            set(
                _string_list(decision.get("blockers"))
                + [
                    "costed gear/belt mall relocation",
                    "costed gear/belt mall relocation before repeated emergency power",
                ]
            )
        )
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            f"guardrail_adjusted_from={selected}",
            f"gear_assembler_unit={gear_mall_iron_plate_issue.get('gear_unit')}",
            f"iron_source_unit={gear_mall_iron_plate_issue.get('source_unit')}",
            f"source_distance_tiles={gear_mall_iron_plate_issue.get('source_distance_tiles')}",
            f"belt_route_cost={gear_mall_iron_plate_issue.get('belt_route_cost')}",
            f"relocation_power_poles_estimate={gear_mall_iron_plate_issue.get('relocation_power_poles_estimate')}",
            f"relocation_cost={gear_mall_iron_plate_issue.get('relocation_cost')}",
            f"route_cost_preference={gear_mall_iron_plate_issue.get('route_cost_preference')}",
            f"small_electric_pole_deficit={relocation_power_pole_deficit}",
            "transport_belts_available_for_mall_logistics=false",
            "gear_handcraft_blocked=true",
        ]
        if power_recovery_waits_on_belt_mall:
            adjusted["evidence"].append("power_recovery_waits_on_belt_mall=true")
        if gear_mall_iron_plate_issue.get("relocation_in_progress"):
            adjusted["evidence"].append("relocation_in_progress=true")
        adjusted["expected_effect"] = (
            "Build the relocation power corridor first, then move the gear/belt mall beside the iron-plate source "
            "so belt automation no longer depends on 149-tile hand-carry or emergency boiler windows."
        )
        adjusted["guardrail_adjusted"] = {
            "from": selected,
            "to": "relocate_gear_belt_mall_to_iron_source",
            "reason": guardrail_reason,
        }
        return adjusted
    if critical_factory_power_issue is not None and selected in {
        "plan_factory_site",
        "produce_electronic_circuit",
        "automate_electronic_circuit_line",
        "bootstrap_build_item_mall",
        "bootstrap_power_pole_mall",
        "build_iron_plate_logistic_line_to_gear_mall",
        "relocate_gear_belt_mall_to_iron_source",
        "research_logistics",
        "research_electric_mining_drill",
        "bootstrap_electric_mining_drill_mall",
        "produce_automation_science_pack",
        "build_site_input_logistic_line",
    }:
        adjusted = dict(decision)
        adjusted["selected_skill"] = "setup_power"
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 94)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            f"LLM selected {selected}, but a critical powered factory block is starved of electricity; "
            "restore steam/electric power before research, mall, or layout expansion."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(
            set(_string_list(decision.get("blockers")) + _critical_factory_power_blockers(critical_factory_power_issue))
        )
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            f"guardrail_adjusted_from={selected}",
            f"factory_power_unit={critical_factory_power_issue.get('unit')}",
            f"factory_power_entity={critical_factory_power_issue.get('entity')}",
            f"factory_power_recipe={critical_factory_power_issue.get('recipe')}",
            f"factory_power_status={critical_factory_power_issue.get('status')}",
        ]
        adjusted["expected_effect"] = "Restore factory electricity before attempting electric research, science, mall, or layout work."
        adjusted["guardrail_adjusted"] = {
            "from": selected,
            "to": "setup_power",
            "reason": guardrail_reason,
        }
        return adjusted
    if gear_belt_mall_power_issue is not None and selected in {
        "plan_factory_site",
        "produce_electronic_circuit",
        "automate_electronic_circuit_line",
        "bootstrap_build_item_mall",
        "build_iron_plate_logistic_line_to_gear_mall",
        "build_site_input_logistic_line",
    }:
        adjusted = dict(decision)
        adjusted["selected_skill"] = "setup_power"
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 93)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            f"LLM selected {selected}, but the gear/belt mall is unpowered and cannot replenish belts "
            "or feed the no-handcraft logistics route."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(set(_string_list(decision.get("blockers")) + ["gear/belt mall power"]))
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            f"guardrail_adjusted_from={selected}",
            f"gear_belt_mall_unit={gear_belt_mall_power_issue.get('unit')}",
            f"gear_belt_mall_recipe={gear_belt_mall_power_issue.get('recipe')}",
            f"gear_belt_mall_status={gear_belt_mall_power_issue.get('status')}",
        ]
        adjusted["expected_effect"] = (
            "Restore electric power before extending the iron-plate logistics route or running circuit automation."
        )
        adjusted["guardrail_adjusted"] = {
            "from": selected,
            "to": "setup_power",
            "reason": guardrail_reason,
        }
        return adjusted
    if gear_belt_mall_bootstrap_issue is not None and selected in {
        "plan_factory_site",
        "produce_electronic_circuit",
        "automate_electronic_circuit_line",
        "bootstrap_build_item_mall",
        "build_iron_plate_logistic_line_to_gear_mall",
        "build_site_input_logistic_line",
        "research_logistics",
    }:
        adjusted = dict(decision)
        adjusted["selected_skill"] = "build_gear_belt_mall_logistics"
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 92)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            f"LLM selected {selected}, but transport belts are exhausted while the gear/belt mall can be "
            "restarted from existing assembler/inventory materials; replenish belts before extending long "
            "iron-plate logistics or running downstream automation."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(
            set(_string_list(decision.get("blockers")) + ["transport-belt mall bootstrap before iron-plate logistics"])
        )
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            f"guardrail_adjusted_from={selected}",
            f"gear_assembler_unit={gear_belt_mall_bootstrap_issue.get('gear_unit')}",
            f"belt_assembler_unit={gear_belt_mall_bootstrap_issue.get('belt_unit')}",
            f"inventory_iron_plate={gear_belt_mall_bootstrap_issue.get('inventory_iron_plate')}",
            f"belt_assembler_iron_gear={gear_belt_mall_bootstrap_issue.get('belt_assembler_iron_gear')}",
            f"local_iron_plate_seed_source_unit={gear_belt_mall_bootstrap_issue.get('local_iron_plate_seed_source_unit')}",
            f"local_iron_plate_seed_distance={gear_belt_mall_bootstrap_issue.get('local_iron_plate_seed_distance')}",
            "transport_belts_available_for_mall_logistics=false",
            "gear_handcraft_blocked=true",
        ]
        adjusted["expected_effect"] = (
            "Seed or finish the gear-fed belt mall so it outputs construction belts without hand-crafting gears, "
            "then resume the sustained iron-plate logistics route."
        )
        adjusted["guardrail_adjusted"] = {
            "from": selected,
            "to": "build_gear_belt_mall_logistics",
            "reason": guardrail_reason,
        }
        return adjusted
    if relocation_power_pole_deficit > 0 and selected in {
        "plan_factory_site",
        "relocate_gear_belt_mall_to_iron_source",
        "produce_electronic_circuit",
        "automate_electronic_circuit_line",
        "bootstrap_build_item_mall",
        "bootstrap_power_pole_mall",
        "research_electric_mining_drill",
        "bootstrap_electric_mining_drill_mall",
        "research_logistics",
        "build_iron_plate_logistic_line_to_gear_mall",
        "build_site_input_logistic_line",
    }:
        adjusted = dict(decision)
        adjusted["selected_skill"] = "bootstrap_power_pole_mall"
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 94)
        original_reason = str(decision.get("reason") or "").strip()
        required_poles = gear_mall_iron_plate_issue.get("relocation_power_poles_estimate") if isinstance(gear_mall_iron_plate_issue, dict) else None
        available_poles = total_item_count(observation, "small-electric-pole")
        guardrail_reason = (
            "The gear/belt mall relocation is cost-preferred, but the power corridor lacks enough small electric poles; "
            "automate pole supply before mining the existing mall."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(set(_string_list(decision.get("blockers")) + ["small-electric-pole supply for mall relocation"]))
        evidence = _string_list(decision.get("evidence"))
        if selected != "bootstrap_power_pole_mall":
            evidence.append(f"guardrail_adjusted_from={selected}")
        for item in [
            f"gear_assembler_unit={gear_mall_iron_plate_issue.get('gear_unit')}",
            f"iron_source_unit={gear_mall_iron_plate_issue.get('source_unit')}",
            f"source_distance_tiles={gear_mall_iron_plate_issue.get('source_distance_tiles')}",
            f"relocation_power_poles_estimate={required_poles}",
            f"small_electric_poles_available={available_poles}",
            f"small_electric_pole_deficit={relocation_power_pole_deficit}",
            f"route_cost_preference={gear_mall_iron_plate_issue.get('route_cost_preference')}",
            "gear_handcraft_blocked=true",
        ]:
            if item not in evidence:
                evidence.append(item)
        adjusted["evidence"] = evidence
        adjusted["expected_effect"] = (
            "Automate small electric poles so the later gear/belt mall relocation can build a power corridor "
            "without hand-crafting poles or prematurely tearing down the old mall."
        )
        if selected != "bootstrap_power_pole_mall":
            adjusted["guardrail_adjusted"] = {
                "from": selected,
                "to": "bootstrap_power_pole_mall",
                "reason": guardrail_reason,
            }
        else:
            adjusted.pop("guardrail_adjusted", None)
        return adjusted
    if _gear_mall_plate_route_needs_compaction(gear_mall_iron_plate_issue) and selected in {
        "plan_factory_site",
        "relocate_gear_belt_mall_to_iron_source",
        "produce_electronic_circuit",
        "automate_electronic_circuit_line",
        "bootstrap_build_item_mall",
        "bootstrap_power_pole_mall",
        "research_electric_mining_drill",
        "bootstrap_electric_mining_drill_mall",
        "research_logistics",
        "build_iron_plate_logistic_line_to_gear_mall",
        "build_site_input_logistic_line",
    }:
        adjusted = dict(decision)
        adjusted["selected_skill"] = "relocate_gear_belt_mall_to_iron_source"
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 93)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            "The gear/belt mall iron-plate site cost model prefers compact relocation or a trunk corridor "
            "over this pre-rail belt recovery while construction belts are exhausted."
        )
        current_blockers = _string_list(decision.get("blockers"))
        already_explains_issue = (
            selected == "relocate_gear_belt_mall_to_iron_source"
            and "costed gear/belt mall relocation" in current_blockers
        )
        adjusted["reason"] = original_reason if already_explains_issue else f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(set(current_blockers + ["costed gear/belt mall relocation"]))
        evidence = _string_list(decision.get("evidence"))
        if selected != "relocate_gear_belt_mall_to_iron_source":
            evidence.append(f"guardrail_adjusted_from={selected}")
        for item in [
            f"gear_assembler_unit={gear_mall_iron_plate_issue.get('gear_unit')}",
            f"iron_source_unit={gear_mall_iron_plate_issue.get('source_unit')}",
            f"source_distance_tiles={gear_mall_iron_plate_issue.get('source_distance_tiles')}",
            f"belt_route_cost={gear_mall_iron_plate_issue.get('belt_route_cost')}",
            f"relocation_power_poles_estimate={gear_mall_iron_plate_issue.get('relocation_power_poles_estimate')}",
            f"relocation_cost={gear_mall_iron_plate_issue.get('relocation_cost')}",
            f"route_cost_preference={gear_mall_iron_plate_issue.get('route_cost_preference')}",
            "transport_belts_available_for_mall_logistics=false",
            "gear_handcraft_blocked=true",
        ]:
            if item not in evidence:
                evidence.append(item)
        adjusted["evidence"] = evidence
        adjusted["expected_effect"] = (
            "Execute the costed relocation precheck so the gear/belt mall can be rebuilt near iron-plate production "
            "without extending another exhausted pre-rail belt route."
        )
        if selected != "relocate_gear_belt_mall_to_iron_source":
            adjusted["guardrail_adjusted"] = {
                "from": selected,
                "to": "relocate_gear_belt_mall_to_iron_source",
                "reason": guardrail_reason,
            }
        else:
            adjusted.pop("guardrail_adjusted", None)
        return adjusted
    if gear_mall_iron_plate_issue is not None and selected in {
        "plan_factory_site",
        "produce_electronic_circuit",
        "automate_electronic_circuit_line",
        "bootstrap_build_item_mall",
        "build_site_input_logistic_line",
        "research_logistics",
    }:
        adjusted = dict(decision)
        adjusted["selected_skill"] = "build_iron_plate_logistic_line_to_gear_mall"
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 92)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            f"LLM selected {selected}, but the gear/belt mall lacks a sustained iron-plate logistics route; "
            "direct iron-gear handcraft must not be used to cover that input gap."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(
            set(_string_list(decision.get("blockers")) + ["iron-plate logistic line to gear mall"])
        )
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            f"guardrail_adjusted_from={selected}",
            f"gear_assembler_unit={gear_mall_iron_plate_issue.get('gear_unit')}",
            f"iron_source_unit={gear_mall_iron_plate_issue.get('source_unit')}",
            f"source_distance_tiles={gear_mall_iron_plate_issue.get('source_distance_tiles')}",
            f"gear_assembler_status={gear_mall_iron_plate_issue.get('gear_assembler_status')}",
            f"transport_belts_available_for_mall_logistics={str(bool(gear_mall_iron_plate_issue.get('transport_belts_available'))).lower()}",
            "gear_handcraft_blocked=true",
        ]
        adjusted["expected_effect"] = (
            "Build the iron-plate belt route into the gear assembler before continuing downstream circuit, "
            "research, or diagnostic layout work."
        )
        adjusted["guardrail_adjusted"] = {
            "from": selected,
            "to": "build_iron_plate_logistic_line_to_gear_mall",
            "reason": guardrail_reason,
        }
        return adjusted
    site_input_line_issue = _site_input_line_issue(observation)
    if (
        _technology_researched(observation, "automation")
        and site_input_line_issue is not None
        and selected
        in {
            "plan_factory_site",
            "produce_electronic_circuit",
            "automate_electronic_circuit_line",
            "bootstrap_build_item_mall",
            "research_logistics",
            "build_site_input_logistic_line",
        }
    ):
        target_skill = (
            "build_site_input_logistic_line"
            if _transport_belt_automation_ready(observation)
            else "build_gear_belt_mall_logistics"
        )
        if selected != target_skill:
            adjusted = dict(decision)
            adjusted["selected_skill"] = target_skill
            adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 90)
            original_reason = str(decision.get("reason") or "").strip()
            guardrail_reason = (
                f"LLM selected {selected}, but an existing factory consumer has a repeated input logistics gap; "
                "do not cover it by player inventory shuttles."
            )
            adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
            blocker = (
                "site input logistic line"
                if target_skill == "build_site_input_logistic_line"
                else "transport-belt automation before site input line"
            )
            adjusted["blockers"] = sorted(set(_string_list(decision.get("blockers")) + [blocker]))
            adjusted["evidence"] = _string_list(decision.get("evidence")) + [
                f"guardrail_adjusted_from={selected}",
                f"layout_kind={site_input_line_issue.get('kind')}",
                f"item={site_input_line_issue.get('item')}",
                f"site_id={site_input_line_issue.get('site_id')}",
                f"transport_belt_automation_ready={str(_transport_belt_automation_ready(observation)).lower()}",
                "hand_carry_seed_risk=true",
            ]
            adjusted["expected_effect"] = (
                "Build the missing repeated input route by belt and endpoint inserters."
                if target_skill == "build_site_input_logistic_line"
                else "Automate transport-belt supply before spending belts on repeated site-to-site input routes."
            )
            adjusted["guardrail_adjusted"] = {
                "from": selected,
                "to": target_skill,
                "reason": guardrail_reason,
            }
            return (
                _with_site_input_target(adjusted, site_input_line_issue)
                if target_skill == "build_site_input_logistic_line"
                else adjusted
            )
    bootstrap_site_logistics_issue = _bootstrap_mall_site_logistics_risk(observation)
    if bootstrap_site_logistics_issue is not None and selected == "bootstrap_build_item_mall":
        adjusted = dict(decision)
        adjusted["selected_skill"] = "plan_factory_site"
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 90)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            "LLM selected build-item mall expansion while existing factory sites still have missing input links "
            "and transport-belt production is not automated; avoid starting another mall cycle that would be "
            "seeded by player inventory shuttles."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(set(_string_list(decision.get("blockers")) + ["site-to-site logistic line"]))
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            "guardrail_adjusted_from=bootstrap_build_item_mall",
            f"layout_kind={bootstrap_site_logistics_issue.get('kind')}",
            f"item={bootstrap_site_logistics_issue.get('item')}",
            f"site_id={bootstrap_site_logistics_issue.get('site_id')}",
            "transport_belt_automation_ready=false",
            f"assembling_machine_1_inventory={inventory_count(observation, 'assembling-machine-1')}",
            "hand_carry_seed_risk=true",
        ]
        adjusted["expected_effect"] = (
            "Plan the missing producer-to-consumer logistics correction before expanding another build-item mall."
        )
        adjusted["guardrail_adjusted"] = {
            "from": "bootstrap_build_item_mall",
            "to": "plan_factory_site",
            "reason": guardrail_reason,
        }
        return adjusted
    burner_drill_replacement_issue = _burner_drill_replacement_issue(observation)
    if burner_drill_replacement_issue is not None and selected in {
        "plan_factory_site",
        "research_logistics",
        "produce_electronic_circuit",
        "automate_electronic_circuit_line",
        "bootstrap_build_item_mall",
        "build_belt_smelting_line",
        "expand_iron_smelting",
        "expand_copper_smelting",
    }:
        target_skill = (
            "research_electric_mining_drill"
            if not bool(burner_drill_replacement_issue.get("electric_mining_drill_researched"))
            else "bootstrap_electric_mining_drill_mall"
        )
        if selected != target_skill:
            adjusted = dict(decision)
            adjusted["selected_skill"] = target_skill
            adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 90)
            original_reason = str(decision.get("reason") or "").strip()
            guardrail_reason = (
                "Burner mining drills remain after Automation and stable power; introduce electric mining drills "
                "before letting layout diagnostics or downstream automation consume the next strategy cycle."
            )
            adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
            blockers = ["electric mining drill research"] if target_skill == "research_electric_mining_drill" else ["electric mining drill mall"]
            adjusted["blockers"] = sorted(set(_string_list(decision.get("blockers")) + blockers))
            adjusted["evidence"] = _string_list(decision.get("evidence")) + [
                f"guardrail_adjusted_from={selected}",
                f"burner_mining_drill_count={burner_drill_replacement_issue.get('burner_drill_count')}",
                f"electric_mining_drill_count={burner_drill_replacement_issue.get('electric_drill_count')}",
                f"electric_mining_drill_researched={str(bool(burner_drill_replacement_issue.get('electric_mining_drill_researched'))).lower()}",
                f"electric_mining_drill_automated={str(bool(burner_drill_replacement_issue.get('electric_mining_drill_automated'))).lower()}",
            ]
            adjusted["expected_effect"] = (
                "Move the factory from coal-fueled burner mining toward powered electric mining before scaling more raw throughput."
            )
            adjusted["guardrail_adjusted"] = {
                "from": selected,
                "to": target_skill,
                "reason": guardrail_reason,
            }
            return adjusted
    if (
        rocket_objective
        and selected != "research_automation"
        and not _technology_researched(observation, "automation")
        and (
            selected in {"produce_electronic_circuit", "automate_electronic_circuit_line"}
            or _target_deficit_exists(objective, observation, production_targets, "electronic-circuit")
        )
    ):
        adjusted = dict(decision)
        adjusted["selected_skill"] = "research_automation"
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 92)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            "Electronic-circuit production is not the next rocket-program step until automation science "
            "has opened Automation research."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(set(_string_list(decision.get("blockers")) + ["automation research"]))
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            f"guardrail_adjusted_from={selected}",
            "automation_researched=false",
            f"automation_science_pack_total={total_item_count(observation, 'automation-science-pack')}",
        ]
        adjusted["expected_effect"] = "Produce automation science and feed a powered lab before committing strategy cycles to circuits."
        adjusted["guardrail_adjusted"] = {
            "from": selected,
            "to": "research_automation",
            "reason": guardrail_reason,
        }
        return adjusted
    if (
        rocket_objective
        and selected != "research_logistics"
        and _technology_researched(observation, "automation")
        and not _technology_researched(observation, "logistics")
        and (
            selected in {"produce_electronic_circuit", "automate_electronic_circuit_line"}
            or _target_deficit_exists(objective, observation, production_targets, "electronic-circuit")
        )
    ):
        adjusted = dict(decision)
        adjusted["selected_skill"] = "research_logistics"
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 91)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            "Red-science Logistics research should come before committing the rocket-program loop "
            "to a green-circuit production line."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(set(_string_list(decision.get("blockers")) + ["logistics research"]))
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            f"guardrail_adjusted_from={selected}",
            "automation_researched=true",
            "logistics_researched=false",
            f"automation_science_pack_total={total_item_count(observation, 'automation-science-pack')}",
        ]
        adjusted["expected_effect"] = "Feed the powered lab with automation science to unlock early logistics before circuit-line expansion."
        adjusted["guardrail_adjusted"] = {
            "from": selected,
            "to": "research_logistics",
            "reason": guardrail_reason,
        }
        return adjusted
    if (
        rocket_objective
        and selected == "plan_factory_site"
        and _technology_researched(observation, "automation")
        and not _technology_researched(observation, "logistics")
        and not production_targets
        and not _plan_site_should_preempt_logistics(observation)
    ):
        adjusted = dict(decision)
        adjusted["selected_skill"] = "research_logistics"
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 89)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            "LLM selected simulation-only layout planning before Logistics, but there is no confirmed "
            "manual site-logistics issue that should preempt the next red-science research step."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(set(_string_list(decision.get("blockers")) + ["logistics research"]))
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            "guardrail_adjusted_from=plan_factory_site",
            "automation_researched=true",
            "logistics_researched=false",
            "manual_site_logistics_preemption=false",
        ]
        adjusted["expected_effect"] = "Feed the powered lab with automation science before spending main cycles on diagnostic-only layout plans."
        adjusted["guardrail_adjusted"] = {
            "from": "plan_factory_site",
            "to": "research_logistics",
            "reason": guardrail_reason,
        }
        return adjusted
    if (
        rocket_objective
        and selected == "plan_factory_site"
        and _technology_researched(observation, "logistics")
    ):
        fallback_decision = heuristic_strategy(objective, observation, production_targets)
        fallback_skill = str(fallback_decision.get("selected_skill") or "")
        if fallback_skill == "plan_factory_site":
            layout_fallback = _executable_layout_plan_fallback(objective, observation, production_targets)
            if layout_fallback is not None:
                fallback_decision = layout_fallback
                fallback_skill = str(fallback_decision.get("selected_skill") or "")
        if fallback_skill != "plan_factory_site":
            adjusted = dict(decision)
            adjusted["selected_skill"] = fallback_skill
            adjusted["priority"] = max(
                _bounded_int(decision.get("priority"), 50, 0, 100),
                _bounded_int(fallback_decision.get("priority"), 50, 0, 100),
            )
            original_reason = str(decision.get("reason") or "").strip()
            guardrail_reason = (
                "LLM selected simulation-only layout planning after Logistics, but the deterministic strategy "
                f"has an executable next automation step: {fallback_skill}."
            )
            adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
            adjusted["blockers"] = sorted(set(_string_list(decision.get("blockers")) + _string_list(fallback_decision.get("blockers"))))
            adjusted["evidence"] = _string_list(decision.get("evidence")) + [
                "guardrail_adjusted_from=plan_factory_site",
                "logistics_researched=true",
                f"heuristic_selected_skill={fallback_skill}",
                *_string_list(fallback_decision.get("evidence")),
            ]
            for key in ("target_item", "target_count"):
                if fallback_decision.get(key) is not None:
                    adjusted[key] = fallback_decision[key]
            adjusted["expected_effect"] = str(fallback_decision.get("expected_effect") or f"Run {fallback_skill} before more diagnostic-only layout work.")
            adjusted["guardrail_adjusted"] = {
                "from": "plan_factory_site",
                "to": fallback_skill,
                "reason": guardrail_reason,
            }
            return adjusted
    if selected == "plan_factory_site":
        deficit = _first_actionable_target_deficit(objective, observation, production_targets)
        if deficit is not None:
            item, skill, estimated, deficit_per_minute = deficit
            adjusted = dict(decision)
            adjusted["selected_skill"] = skill
            adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 90)
            original_reason = str(decision.get("reason") or "").strip()
            guardrail_reason = (
                f"LLM selected simulation-only layout planning, but {item} still has a target production deficit "
                f"({deficit_per_minute}/min missing, starter-usable estimated {estimated}/min)."
            )
            adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
            adjusted["blockers"] = sorted(set(_string_list(decision.get("blockers")) + [item]))
            adjusted["evidence"] = _string_list(decision.get("evidence")) + [
                "guardrail_adjusted_from=plan_factory_site",
                f"{item}_target_deficit={deficit_per_minute}",
                f"{item}_starter_usable_per_minute={estimated}",
            ]
            adjusted["expected_effect"] = f"Run {skill} before spending the main strategy cycle on simulation-only layout work."
            adjusted["guardrail_adjusted"] = {
                "from": "plan_factory_site",
                "to": skill,
                "reason": guardrail_reason,
            }
            return adjusted
    if (
        selected == "produce_electronic_circuit"
        and _technology_researched(observation, "automation")
        and _target_deficit_exists(objective, observation, production_targets, "electronic-circuit")
    ):
        adjusted = dict(decision)
        adjusted["selected_skill"] = "automate_electronic_circuit_line"
        adjusted["priority"] = max(_bounded_int(decision.get("priority"), 50, 0, 100), 85)
        original_reason = str(decision.get("reason") or "").strip()
        guardrail_reason = (
            "LLM selected hand circuit production for a per-minute electronic-circuit deficit, "
            "but Automation is researched and hand crafting cannot satisfy a sustained rate target."
        )
        adjusted["reason"] = f"{guardrail_reason} {original_reason}".strip()
        adjusted["blockers"] = sorted(set(_string_list(decision.get("blockers")) + ["assembler-based electronic circuit production"]))
        adjusted["evidence"] = _string_list(decision.get("evidence")) + [
            "guardrail_adjusted_from=produce_electronic_circuit",
            "automation_researched=true",
            "electronic_circuit_target_deficit=true",
            "hand_crafting_not_rate_solution=true",
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
    selected_improvement_site: dict[str, Any] | None = None,
) -> dict[str, Any]:
    objective_lower = objective.lower()
    rocket_objective = _is_rocket_objective(objective)
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
    layout = make_layout_improvement_context(observation, selected_improvement_site=selected_improvement_site)
    layout_issues = layout.get("issues") if isinstance(layout.get("issues"), list) else []
    layout_opportunities = layout.get("opportunities") if isinstance(layout.get("opportunities"), list) else []
    top_layout_item = _top_layout_item(layout_issues, layout_opportunities)
    automation_logistics_issue = _first_automation_logistics_issue(layout_issues)
    site_input_line_issue = _site_input_line_issue(observation)
    layout_build_item_shortage = _layout_unlocked_build_item_shortage(layout)
    gear_mall_iron_plate_issue = _gear_mall_iron_plate_logistics_issue(observation)
    critical_factory_power_issue = _critical_factory_power_issue(observation)
    gear_belt_mall_power_issue = _gear_belt_mall_power_issue(observation)
    gear_belt_mall_bootstrap_issue = _gear_belt_mall_bootstrap_issue(observation)
    burner_drill_replacement_issue = _burner_drill_replacement_issue(observation)
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

    if _boiler_coal_feed_should_preempt_power(observation):
        return StrategicDecision(
            selected_skill="connect_coal_fuel_feed",
            priority=91,
            reason=(
                "The starter boiler is fuel-starved, but coal supply and belt production are ready enough to build "
                "a boiler coal feed instead of inserting more fuel by hand."
            ),
            evidence=[
                "boiler_no_fuel=true",
                "coal_supply_ready=true",
                "transport_belt_automation_ready=true",
            ],
            blockers=["boiler coal fuel feed"],
            expected_effect="Build a coal belt and burner inserter feed for the boiler before resuming powered work.",
        ).to_dict()

    if _coal_fuel_feed_needed(observation, objective, production_targets, monitor=monitor):
        if not _transport_belt_automation_ready(observation):
            if automation_researched:
                return StrategicDecision(
                    selected_skill="build_gear_belt_mall_logistics",
                    priority=87,
                    reason=(
                        "A coal consumer link is route_needed, but transport-belt production is not automated; "
                        "build gear-fed belt mall logistics before adding site-to-site paths."
                    ),
                    evidence=[
                        "coal_fuel_feed_route_needed=true",
                        "transport_belt_automation_ready=false",
                        "automation_researched=true",
                    ],
                    blockers=["transport-belt automation before site links"],
                    expected_effect="Automate gear-fed transport belts so future site logistics can be built without hand-crafting each path.",
                ).to_dict()
        else:
            return StrategicDecision(
                selected_skill="connect_coal_fuel_feed",
                priority=89,
                reason="A fueled coal mining site exists, but a nearby coal consumer link is still route_needed.",
                evidence=["coal_fuel_feed_route_needed=true", "transport_belt_automation_ready=true"],
                blockers=["coal fuel feed route"],
                expected_effect="Build belt/inserter fuel feed from the coal supply belt to the nearby burner consumer.",
            ).to_dict()

    relocation_power_pole_deficit = _gear_mall_relocation_power_pole_deficit(gear_mall_iron_plate_issue, observation)
    power_recovery_waits_on_belt_mall = _power_recovery_waits_on_belt_mall(observation)
    if (
        _gear_mall_plate_route_needs_compaction(gear_mall_iron_plate_issue)
        and relocation_power_pole_deficit <= 0
        and (
            _power_issue_allows_pre_power_relocation(critical_factory_power_issue)
            or power_recovery_waits_on_belt_mall
            or bool(gear_mall_iron_plate_issue.get("relocation_in_progress"))
        )
    ):
        evidence = [
            f"gear_assembler_unit={gear_mall_iron_plate_issue.get('gear_unit')}",
            f"iron_source_unit={gear_mall_iron_plate_issue.get('source_unit')}",
            f"source_distance_tiles={gear_mall_iron_plate_issue.get('source_distance_tiles')}",
            f"belt_route_cost={gear_mall_iron_plate_issue.get('belt_route_cost')}",
            f"relocation_power_poles_estimate={gear_mall_iron_plate_issue.get('relocation_power_poles_estimate')}",
            f"relocation_cost={gear_mall_iron_plate_issue.get('relocation_cost')}",
            f"route_cost_preference={gear_mall_iron_plate_issue.get('route_cost_preference')}",
            f"small_electric_pole_deficit={relocation_power_pole_deficit}",
            "transport_belts_available_for_mall_logistics=false",
            "gear_handcraft_blocked=true",
        ]
        if power_recovery_waits_on_belt_mall:
            evidence.append("power_recovery_waits_on_belt_mall=true")
        if gear_mall_iron_plate_issue.get("relocation_in_progress"):
            evidence.append("relocation_in_progress=true")
        return StrategicDecision(
            selected_skill="relocate_gear_belt_mall_to_iron_source",
            priority=94 if critical_factory_power_issue is not None else 93,
            reason=(
                "The powered gear/belt mall has no sustained iron-plate input, and repeated emergency boiler "
                "fueling only creates short windows. Build the relocation power corridor and move the mall "
                "toward iron-plate production before trying more long-route recovery."
            ),
            evidence=evidence,
            blockers=["costed gear/belt mall relocation"],
            expected_effect=(
                "Build the relocation power corridor before teardown, then rebuild the gear/belt mall beside "
                "iron-plate production."
            ),
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

    if critical_factory_power_issue is not None:
        return StrategicDecision(
            selected_skill="setup_power",
            priority=94,
            reason=(
                "A critical powered factory block has no electricity, so research, science, mall, and layout "
                "expansion would stall until steam/electric power is restored."
            ),
            evidence=[
                f"factory_power_unit={critical_factory_power_issue.get('unit')}",
                f"factory_power_entity={critical_factory_power_issue.get('entity')}",
                f"factory_power_recipe={critical_factory_power_issue.get('recipe')}",
                f"factory_power_status={critical_factory_power_issue.get('status')}",
            ],
            blockers=_critical_factory_power_blockers(critical_factory_power_issue),
            expected_effect="Restore electricity before attempting electric research, science, mall, or layout work.",
        ).to_dict()

    if gear_belt_mall_power_issue is not None:
        return StrategicDecision(
            selected_skill="setup_power",
            priority=93,
            reason=(
                "The gear/belt mall is unpowered, so it cannot replenish transport belts for the iron-plate "
                "logistics route or downstream automation."
            ),
            evidence=[
                f"gear_belt_mall_unit={gear_belt_mall_power_issue.get('unit')}",
                f"gear_belt_mall_recipe={gear_belt_mall_power_issue.get('recipe')}",
                f"gear_belt_mall_status={gear_belt_mall_power_issue.get('status')}",
            ],
            blockers=["gear/belt mall power"],
            expected_effect="Restore electric power before trying to extend the belt route or expand circuit automation.",
        ).to_dict()

    if gear_belt_mall_bootstrap_issue is not None:
        return StrategicDecision(
            selected_skill="build_gear_belt_mall_logistics",
            priority=92,
            reason=(
                "Transport belts are exhausted, but the powered gear/belt mall can be restarted from existing "
                "assembler or inventory materials. Replenish belt output before extending long iron-plate "
                "logistics or downstream automation."
            ),
            evidence=[
                f"gear_assembler_unit={gear_belt_mall_bootstrap_issue.get('gear_unit')}",
                f"belt_assembler_unit={gear_belt_mall_bootstrap_issue.get('belt_unit')}",
                f"inventory_iron_plate={gear_belt_mall_bootstrap_issue.get('inventory_iron_plate')}",
                f"gear_assembler_iron_plate={gear_belt_mall_bootstrap_issue.get('gear_assembler_iron_plate')}",
                f"gear_assembler_iron_gear={gear_belt_mall_bootstrap_issue.get('gear_assembler_iron_gear')}",
                f"belt_assembler_iron_gear={gear_belt_mall_bootstrap_issue.get('belt_assembler_iron_gear')}",
                f"local_iron_plate_seed_source_unit={gear_belt_mall_bootstrap_issue.get('local_iron_plate_seed_source_unit')}",
                f"local_iron_plate_seed_distance={gear_belt_mall_bootstrap_issue.get('local_iron_plate_seed_distance')}",
                "transport_belts_available_for_mall_logistics=false",
                "gear_handcraft_blocked=true",
            ],
            blockers=["transport-belt mall bootstrap before iron-plate logistics"],
            expected_effect=(
                "Use the gear-fed belt mall executor to seed or finish belt production without hand-crafted gears, "
                "then resume the sustained iron-plate input route."
            ),
        ).to_dict()

    relocation_power_pole_deficit = _gear_mall_relocation_power_pole_deficit(gear_mall_iron_plate_issue, observation)
    if relocation_power_pole_deficit > 0:
        required_poles = gear_mall_iron_plate_issue.get("relocation_power_poles_estimate") if isinstance(gear_mall_iron_plate_issue, dict) else None
        available_poles = total_item_count(observation, "small-electric-pole")
        return StrategicDecision(
            selected_skill="bootstrap_power_pole_mall",
            priority=94,
            reason=(
                "The powered gear/belt mall should be relocated near iron plates, but the relocation power corridor "
                "does not have enough small electric poles. Automate pole supply before mining the existing mall."
            ),
            evidence=[
                f"gear_assembler_unit={gear_mall_iron_plate_issue.get('gear_unit')}",
                f"iron_source_unit={gear_mall_iron_plate_issue.get('source_unit')}",
                f"source_distance_tiles={gear_mall_iron_plate_issue.get('source_distance_tiles')}",
                f"relocation_power_poles_estimate={required_poles}",
                f"small_electric_poles_available={available_poles}",
                f"small_electric_pole_deficit={relocation_power_pole_deficit}",
                f"route_cost_preference={gear_mall_iron_plate_issue.get('route_cost_preference')}",
                "gear_handcraft_blocked=true",
            ],
            blockers=["small-electric-pole supply for mall relocation"],
            expected_effect=(
                "Automate small electric poles so the mall relocation can reserve and connect a power corridor "
                "without hand-crafting poles."
            ),
        ).to_dict()

    if _gear_mall_plate_route_needs_compaction(gear_mall_iron_plate_issue):
        return StrategicDecision(
            selected_skill="relocate_gear_belt_mall_to_iron_source",
            priority=93,
            reason=(
                "The powered gear/belt mall has no sustained iron-plate input, and the site placement cost "
                "model prefers compact relocation or a trunk corridor over another starter-era ad-hoc belt "
                "extension while construction belts are exhausted."
            ),
            evidence=[
                f"gear_assembler_unit={gear_mall_iron_plate_issue.get('gear_unit')}",
                f"iron_source_unit={gear_mall_iron_plate_issue.get('source_unit')}",
                f"source_distance_tiles={gear_mall_iron_plate_issue.get('source_distance_tiles')}",
                f"belt_route_cost={gear_mall_iron_plate_issue.get('belt_route_cost')}",
                f"relocation_power_poles_estimate={gear_mall_iron_plate_issue.get('relocation_power_poles_estimate')}",
                f"relocation_cost={gear_mall_iron_plate_issue.get('relocation_cost')}",
                f"route_cost_preference={gear_mall_iron_plate_issue.get('route_cost_preference')}",
                "transport_belts_available_for_mall_logistics=false",
                "gear_handcraft_blocked=true",
            ],
            blockers=["costed gear/belt mall relocation"],
            expected_effect=(
                "Run the relocation precheck and move the gear/belt mall toward iron-plate production only when "
                "the power corridor and assembler materials make the rebuild safe."
            ),
        ).to_dict()

    if gear_mall_iron_plate_issue is not None:
        return StrategicDecision(
            selected_skill="build_iron_plate_logistic_line_to_gear_mall",
            priority=92,
            reason=(
                "The powered gear/belt mall can make belts, but the iron-gear assembler has no sustained "
                "iron-plate input from the distant furnace. Build the plate logistics route before any more "
                "gear handcraft pressure appears."
            ),
            evidence=[
                f"gear_assembler_unit={gear_mall_iron_plate_issue.get('gear_unit')}",
                f"iron_source_unit={gear_mall_iron_plate_issue.get('source_unit')}",
                f"source_distance_tiles={gear_mall_iron_plate_issue.get('source_distance_tiles')}",
                f"gear_assembler_iron_plate={gear_mall_iron_plate_issue.get('gear_assembler_iron_plate')}",
                f"gear_assembler_status={gear_mall_iron_plate_issue.get('gear_assembler_status')}",
                f"transport_belts_available_for_mall_logistics={str(bool(gear_mall_iron_plate_issue.get('transport_belts_available'))).lower()}",
            ],
            blockers=["iron-plate logistic line to gear mall"],
            expected_effect=(
                "Extend transport belts and endpoint inserters so the gear assembler consumes furnace output "
                "without player gear crafting or iron-plate shuttle runs."
            ),
        ).to_dict()

    if burner_drill_replacement_issue is not None and not bool(
        burner_drill_replacement_issue.get("electric_mining_drill_researched")
    ):
        return StrategicDecision(
            selected_skill="research_electric_mining_drill",
            priority=90,
            reason=(
                "Burner mining drills are still active after Automation and stable power; research electric mining drills "
                "so coal-fueled mining can be phased out early."
            ),
            evidence=[
                f"burner_mining_drill_count={burner_drill_replacement_issue.get('burner_drill_count')}",
                f"electric_mining_drill_count={burner_drill_replacement_issue.get('electric_drill_count')}",
                f"burner_drill_resources={burner_drill_replacement_issue.get('resource_counts')}",
                "electric_mining_drill_researched=false",
            ],
            blockers=["electric mining drill research"],
            expected_effect="Unlock electric mining drills before adding more burner-drill mining capacity.",
        ).to_dict()

    if (
        burner_drill_replacement_issue is not None
        and bool(burner_drill_replacement_issue.get("electric_mining_drill_researched"))
        and not bool(burner_drill_replacement_issue.get("electric_mining_drill_automated"))
    ):
        return StrategicDecision(
            selected_skill="bootstrap_electric_mining_drill_mall",
            priority=89,
            reason=(
                "Electric mining drill technology is available while burner mining drills remain; automate electric drill "
                "production before scaling more coal-fueled mining."
            ),
            evidence=[
                f"burner_mining_drill_count={burner_drill_replacement_issue.get('burner_drill_count')}",
                f"electric_mining_drill_stock={burner_drill_replacement_issue.get('electric_mining_drill_stock')}",
                "electric_mining_drill_researched=true",
                "electric_mining_drill_automated=false",
            ],
            blockers=["electric mining drill mall"],
            expected_effect="Produce electric mining drills by assembler so burner miners can be replaced without hand-crafting.",
        ).to_dict()

    if automation_researched and site_input_line_issue is not None:
        belt_ready = _transport_belt_automation_ready(observation)
        target_skill = "build_site_input_logistic_line" if belt_ready else "build_gear_belt_mall_logistics"
        decision = StrategicDecision(
            selected_skill=target_skill,
            priority=90,
            reason=(
                "A repeated producer-to-consumer input flow is missing; build the logistics route instead of "
                "letting the player carry items between factory sites."
                if belt_ready
                else "A repeated producer-to-consumer input flow is missing, but transport-belt production is not ready; "
                "automate belts before building site-to-site routes."
            ),
            evidence=[
                f"layout_kind={site_input_line_issue.get('kind')}",
                f"item={site_input_line_issue.get('item')}",
                f"site_id={site_input_line_issue.get('site_id')}",
                f"transport_belt_automation_ready={str(belt_ready).lower()}",
            ],
            blockers=[
                "site input logistic line"
                if belt_ready
                else "transport-belt automation before site input line"
            ],
            expected_effect=(
                "Build a belt and endpoint-inserter route for the repeated input."
                if belt_ready
                else "Replenish automated construction belts before spending them on repeated site input lines."
            ),
        ).to_dict()
        return _with_site_input_target(decision, site_input_line_issue) if belt_ready else decision

    if automation_researched and automation_logistics_issue is not None:
        issue_text = " ".join(
            str(automation_logistics_issue.get(key) or "")
            for key in ("item", "site_id", "detail", "recommendation")
        ).lower()
        if "iron-plate" in issue_text and ("gear" in issue_text or "iron-gear-wheel" in issue_text):
            return StrategicDecision(
                selected_skill="build_iron_plate_logistic_line_to_gear_mall",
                priority=91,
                reason=(
                    "The gear/belt mall needs iron-plate input logistics; build a belt route instead of "
                    "letting the player shuttle plates from a distant furnace."
                ),
                evidence=[
                    f"layout_kind={automation_logistics_issue.get('kind')}",
                    f"item={automation_logistics_issue.get('item')}",
                    f"site_id={automation_logistics_issue.get('site_id')}",
                    "gear_mall_iron_plate_logistics=true",
                ],
                blockers=["iron-plate logistic line to gear mall"],
                expected_effect="Extend transport belts and endpoint inserters so iron plates reach the gear mall without player carrying.",
            ).to_dict()
        return StrategicDecision(
            selected_skill="plan_factory_site",
            priority=90,
            reason=(
                "Automation is researched, but a repeated producer-to-consumer input flow still has no logistic route; "
                "avoid continuing by hand-carrying items between sites."
            ),
            evidence=[
                f"layout_kind={automation_logistics_issue.get('kind')}",
                f"item={automation_logistics_issue.get('item')}",
                f"site_id={automation_logistics_issue.get('site_id')}",
            ],
            blockers=["site-to-site logistic line"],
            expected_effect="Plan the closest belt/chest/logistic-line correction before scaling or repeating that consumer loop.",
        ).to_dict()

    if rocket_objective and total_iron < 10:
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
        if rocket_objective and not automation_researched and item == "electronic-circuit":
            return StrategicDecision(
                selected_skill="research_automation",
                priority=93,
                reason=(
                    "Automation science and Automation research unlock the first practical tech step; "
                    "delay green-circuit work until the lab path is moving."
                ),
                evidence=[f"automation_science_pack_total={science}", "automation_researched=false"],
                blockers=["automation research"],
                expected_effect="Produce automation science and feed a powered lab before committing strategy cycles to circuits.",
            ).to_dict()
        if (
            rocket_objective
            and automation_researched
            and item == "electronic-circuit"
            and not _technology_researched(observation, "logistics")
        ):
            return StrategicDecision(
                selected_skill="research_logistics",
                priority=92,
                reason=(
                    "Electronic circuits are bottlenecked, but early red-science research should unlock Logistics "
                    "before the rocket-program loop commits to a circuit line."
                ),
                evidence=[
                    f"automation_science_pack_total={science}",
                    "automation_researched=true",
                    "logistics_researched=false",
                ],
                blockers=["logistics research"],
                expected_effect="Feed the powered lab with automation science to unlock early logistics before circuit-line expansion.",
            ).to_dict()
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
            skill = _plate_smelting_skill("iron", observation)
            return StrategicDecision(
                selected_skill=skill,
                priority=90,
                reason="Electronic circuits require steady iron plates; current iron supply is below the circuit threshold.",
                evidence=[f"iron_plate_total={total_iron}", f"iron_plate_inventory={inventory_iron}"],
                blockers=["iron plate throughput"],
                expected_effect=f"Run {skill} before circuit assembly.",
            ).to_dict()
        if total_copper < 20:
            skill = _plate_smelting_skill("copper", observation)
            return StrategicDecision(
                selected_skill=skill,
                priority=85,
                reason="Electronic circuits also need copper cable; copper plates are below the circuit threshold.",
                evidence=[f"copper_plate_total={total_copper}"],
                blockers=["copper plate throughput"],
                expected_effect=f"Run {skill} for cable production.",
            ).to_dict()
        return StrategicDecision(
            selected_skill="produce_electronic_circuit",
            priority=80,
            reason="Iron and copper prerequisites look available; proceed to circuit production.",
            evidence=[f"iron_plate_total={total_iron}", f"copper_plate_total={total_copper}", f"circuits={circuits}"],
            blockers=[],
            expected_effect="Begin green circuit production.",
        ).to_dict()

    if rocket_objective:
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
            if not _technology_researched(observation, "logistics"):
                return StrategicDecision(
                    selected_skill="research_logistics",
                    priority=88,
                    reason="Automation is researched; use red science to unlock Logistics before building the first green circuit line.",
                    evidence=[
                        f"automation_science_pack_total={science}",
                        f"electronic_circuit_total={circuits}",
                        "automation_researched=true",
                        "logistics_researched=false",
                    ],
                    blockers=["logistics research"],
                    expected_effect="Feed the powered lab with automation science to unlock early logistics before circuit-line expansion.",
                ).to_dict()
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
            if layout_build_item_shortage is not None and automation_researched:
                return _layout_build_item_shortage_decision(
                    layout_build_item_shortage,
                    priority=max(86, min(90, int(top_layout_item.get("severity") or 75))),
                )
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
            if layout_build_item_shortage is not None and automation_researched:
                return _layout_build_item_shortage_decision(
                    layout_build_item_shortage,
                    priority=max(84, min(88, int(top_layout_item.get("severity") or 75))),
                )
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
    return max(
        candidates,
        key=lambda item: (
            int(item.get("severity") or 0),
            _site_input_issue_item_priority(str(item.get("item") or "")),
        ),
    )


def _layout_unlocked_build_item_shortage(layout: dict[str, Any]) -> dict[str, Any] | None:
    candidates = layout.get("simulation_candidates") if isinstance(layout.get("simulation_candidates"), list) else []
    rows: list[tuple[float, int, str, dict[str, Any], dict[str, Any]]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        supply = candidate.get("build_item_supply") if isinstance(candidate.get("build_item_supply"), dict) else {}
        used_supply = (
            supply.get("used_unlocked_item_supply")
            if isinstance(supply.get("used_unlocked_item_supply"), dict)
            else {}
        )
        simulation = candidate.get("simulation") if isinstance(candidate.get("simulation"), dict) else {}
        try:
            score = float(simulation.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        for item, state in used_supply.items():
            target_item = _sanitize_build_item_target(item)
            if not target_item or not isinstance(state, dict):
                continue
            missing = _positive_int_or_none(state.get("missing")) or 0
            if missing <= 0:
                continue
            rows.append((score, missing, target_item, state, candidate))
    if not rows:
        return None
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    score, missing, target_item, state, candidate = rows[0]
    return {
        "item": target_item,
        "missing": missing,
        "candidate_id": candidate.get("candidate_id"),
        "score": round(score, 1),
        "state": state,
    }


def _layout_build_item_shortage_decision(
    shortage: dict[str, Any],
    *,
    priority: int = 88,
) -> dict[str, Any]:
    item = str(shortage.get("item") or "")
    missing = _positive_int_or_none(shortage.get("missing")) or _build_item_stock_floor(item)
    return _with_build_item_target(
        StrategicDecision(
            selected_skill="bootstrap_build_item_mall",
            priority=priority,
            reason=(
                f"Unlock-aware layout candidates can use {item}, but the required build items are missing; "
                "automate the unlocked tool before applying the improved site layout."
            ),
            evidence=[
                f"unlock_aware_candidate={shortage.get('candidate_id')}",
                f"build_item_shortage={item}",
                f"missing={missing}",
                f"candidate_score={shortage.get('score')}",
                "layout_unlocks_considered=true",
            ],
            blockers=[f"{item} build-item mall"],
            expected_effect=(
                f"Build a {item} mall cell so later site layout optimization can actually place the improved blueprint."
            ),
        ).to_dict(),
        item,
        max(missing, _build_item_stock_floor(item)),
    )


def _executable_layout_plan_fallback(
    objective: str,
    observation: dict[str, Any],
    production_targets: dict[str, float] | None,
) -> dict[str, Any] | None:
    layout = make_layout_improvement_context(observation)
    issues = layout.get("issues") if isinstance(layout.get("issues"), list) else []
    opportunities = layout.get("opportunities") if isinstance(layout.get("opportunities"), list) else []
    top_layout_item = _top_layout_item(issues, opportunities)
    shortage = _layout_unlocked_build_item_shortage(layout)
    if top_layout_item is None or int(top_layout_item.get("severity") or 0) < 75:
        if (
            shortage is not None
            and _technology_researched(observation, "automation")
            and float(shortage.get("score") or 0.0) >= 75.0
        ):
            return _layout_build_item_shortage_decision(shortage, priority=86)
        return None

    if shortage is not None and _technology_researched(observation, "automation"):
        return _layout_build_item_shortage_decision(
            shortage,
            priority=max(86, min(92, int(top_layout_item.get("severity") or 75))),
        )

    kind = str(top_layout_item.get("kind") or "")
    skill = ""
    expected_effect = ""
    if kind in {"incomplete_logistics_link", "manual_site_logistics_gap", "distant_related_sites", "manual_feed_factory_block"}:
        issue = _site_input_line_issue(observation)
        if issue is not None and _technology_researched(observation, "automation"):
            if _transport_belt_automation_ready(observation):
                skill = "build_site_input_logistic_line"
                expected_effect = "Build the missing repeated input belt route through the implemented site logistics executor."
            else:
                skill = "build_gear_belt_mall_logistics"
                expected_effect = "Automate gear-fed transport belts before building repeated site-to-site input routes."
    if kind in {"rebalance_green_circuit_ratio", "complete_green_circuit_block_pattern"}:
        if not _technology_researched(observation, "automation"):
            skill = "research_automation"
            expected_effect = "Unlock assemblers before applying the green-circuit layout correction."
        elif not _technology_researched(observation, "logistics"):
            skill = "research_logistics"
            expected_effect = "Unlock early logistics before rebuilding or extending the green-circuit cell."
        else:
            skill = "automate_electronic_circuit_line"
            expected_effect = "Apply the green-circuit ratio correction through the implemented circuit automation executor."
    elif kind == "manual_power_fuel":
        if _transport_belt_automation_ready(observation):
            skill = "connect_coal_fuel_feed"
            expected_effect = "Replace repeated boiler or burner hand-fueling with an implemented coal fuel feed."
        elif _technology_researched(observation, "automation"):
            skill = "build_gear_belt_mall_logistics"
            expected_effect = "Automate gear-fed belt production before building site-to-site fuel feed paths."

    if not skill:
        return None
    decision = StrategicDecision(
        selected_skill=skill,
        priority=max(84, min(92, int(top_layout_item.get("severity") or 75))),
        reason=(
            f"Layout planning selected {kind}, but main autopilot cycles need an executable correction; "
            f"run {skill} instead of another diagnostic-only plan."
        ),
        evidence=[
            f"layout_executable_fallback={kind}",
            f"layout_kind={kind}",
            f"severity={top_layout_item.get('severity')}",
            f"site_id={top_layout_item.get('site_id')}",
        ],
        blockers=["diagnostic-only layout plan"],
        expected_effect=expected_effect,
        source="heuristic",
    ).to_dict()
    return _with_site_input_target(decision, _site_input_line_issue(observation)) if skill == "build_site_input_logistic_line" else decision


def _first_automation_logistics_issue(issues: list[Any]) -> dict[str, Any] | None:
    target_kinds = {"manual_site_logistics_gap", "distant_related_sites", "manual_feed_factory_block"}
    candidates = [
        item
        for item in issues
        if isinstance(item, dict)
        and item.get("kind") in target_kinds
        and int(item.get("severity") or 0) >= 84
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: int(item.get("severity") or 0))


def _bootstrap_mall_site_logistics_risk(observation: dict[str, Any]) -> dict[str, Any] | None:
    if _transport_belt_automation_ready(observation):
        return None
    issue = _first_unserved_factory_input_issue(observation)
    if issue is None:
        return None
    if inventory_count(observation, "assembling-machine-1") <= 0:
        return issue
    item = str(issue.get("item") or "")
    if item and inventory_count(observation, item) <= 0:
        return issue
    return None


def _first_unserved_factory_input_issue(observation: dict[str, Any]) -> dict[str, Any] | None:
    layout = make_layout_improvement_context(observation)
    issues = layout.get("issues") if isinstance(layout.get("issues"), list) else []
    candidate_items = {
        "iron-plate",
        "copper-plate",
        "iron-gear-wheel",
        "copper-cable",
        "electronic-circuit",
        "automation-science-pack",
        "logistic-science-pack",
    }
    candidates: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        kind = str(issue.get("kind") or "")
        if kind not in {"incomplete_logistics_link", "manual_site_logistics_gap", "distant_related_sites", "manual_feed_factory_block"}:
            continue
        if int(issue.get("severity") or 0) < 75:
            continue
        item = str(issue.get("item") or "")
        if item and item not in candidate_items:
            continue
        text = " ".join([kind, *(str(issue.get(key) or "") for key in ("site_id", "detail", "recommendation"))]).lower()
        if not any(token in text for token in ("missing_source", "incomplete", "missing", "route_needed", "manual")):
            continue
        candidates.append(issue)
    if not candidates:
        return None
    return max(candidates, key=lambda item: int(item.get("severity") or 0))


def _site_input_line_issue(observation: dict[str, Any]) -> dict[str, Any] | None:
    issue = _first_unserved_factory_input_issue(observation)
    if issue is None:
        return None
    item = str(issue.get("item") or "")
    if item not in {
        "iron-plate",
        "copper-plate",
        "iron-gear-wheel",
        "copper-cable",
        "electronic-circuit",
        "automation-science-pack",
        "logistic-science-pack",
    }:
        return None
    text = " ".join(str(issue.get(key) or "") for key in ("site_id", "detail", "recommendation")).lower()
    if "build_item_mall" in text and item in {"iron-plate", "iron-gear-wheel"}:
        return None
    if item == "iron-plate" and ("gear" in text or "iron-gear-wheel" in text):
        return None
    return issue


def _site_input_issue_item_priority(item: str) -> int:
    return {
        "copper-plate": 98,
        "iron-plate": 95,
        "iron-gear-wheel": 92,
        "copper-cable": 90,
        "electronic-circuit": 86,
        "automation-science-pack": 82,
        "logistic-science-pack": 78,
    }.get(item, 60)


def _plan_site_should_preempt_logistics(observation: dict[str, Any]) -> bool:
    layout = make_layout_improvement_context(observation)
    issues = layout.get("issues") if isinstance(layout.get("issues"), list) else []
    return _first_automation_logistics_issue(issues) is not None


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


def _sanitize_build_item_target(value: Any) -> str | None:
    item = str(value or "").strip()
    if not item:
        return None
    return item if item in BUILD_ITEM_MALL_ITEMS else None


def _positive_int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _with_build_item_target(
    decision: dict[str, Any],
    target_item: str,
    target_count: int | None = None,
) -> dict[str, Any]:
    result = dict(decision)
    result["target_item"] = target_item
    if target_count is not None:
        result["target_count"] = target_count
    return result


def _sanitize_site_input_target(value: Any) -> str | None:
    item = str(value or "").strip()
    if item in {
        "iron-plate",
        "copper-plate",
        "iron-gear-wheel",
        "copper-cable",
        "electronic-circuit",
        "automation-science-pack",
        "logistic-science-pack",
    }:
        return item
    return None


def _with_site_input_target(decision: dict[str, Any], issue: dict[str, Any] | None) -> dict[str, Any]:
    input_item = _sanitize_site_input_target((issue or {}).get("item"))
    if not input_item:
        return decision
    result = dict(decision)
    result["input_item"] = input_item
    return result


def _is_rocket_objective(objective: str) -> bool:
    return "rocket" in objective.lower() or KOREAN_ROCKET in objective


def _first_power_issue(monitor: dict[str, Any]) -> dict[str, Any] | None:
    networks = monitor.get("power_networks") if isinstance(monitor.get("power_networks"), list) else []
    for row in networks:
        if not isinstance(row, dict):
            continue
        if row.get("status") in {"insufficient_generation", "no_generation", "unconnected_consumers"}:
            return row
    return None


def _critical_factory_power_issue(observation: dict[str, Any]) -> dict[str, Any] | None:
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "")
        recipe = str(entity.get("recipe") or entity.get("recipe_name") or "")
        if name in ASSEMBLER_ENTITY_NAMES and recipe in CRITICAL_FACTORY_POWER_RECIPES and _entity_no_power(entity):
            return {
                "unit": entity.get("unit_number"),
                "entity": name,
                "recipe": recipe,
                "status": str(entity.get("status_name") or entity.get("status") or "no_power"),
            }
        if name == "lab" and _entity_no_power(entity):
            return {
                "unit": entity.get("unit_number"),
                "entity": "lab",
                "recipe": "research",
                "status": str(entity.get("status_name") or entity.get("status") or "no_power"),
            }
    steam_issue = _starter_steam_power_issue(observation)
    if steam_issue is not None and _critical_electric_factory_present(observation):
        return steam_issue
    return None


def _critical_factory_power_blockers(issue: dict[str, Any]) -> list[str]:
    blockers = ["factory power"]
    recipe = str(issue.get("recipe") or "")
    if recipe in {"iron-gear-wheel", "transport-belt"}:
        blockers.append("gear/belt mall power")
    return blockers


def _power_issue_allows_pre_power_relocation(issue: dict[str, Any] | None) -> bool:
    if issue is None:
        return True
    recipe = str(issue.get("recipe") or "")
    entity = str(issue.get("entity") or "")
    if recipe == "steam-power" and entity in {"boiler", "steam-engine"}:
        return True
    return entity in ASSEMBLER_ENTITY_NAMES and recipe in {"iron-gear-wheel", "transport-belt"}


def _power_recovery_waits_on_belt_mall(observation: dict[str, Any]) -> bool:
    if _starter_steam_power_issue(observation) is None:
        return False
    if _transport_belts_available_for_mall_logistics(observation):
        return False
    assemblers = (
        entities_named(observation, "assembling-machine-1")
        + entities_named(observation, "assembling-machine-2")
        + entities_named(observation, "assembling-machine-3")
    )
    return any(
        str(assembler.get("recipe") or assembler.get("recipe_name") or "") == "transport-belt"
        for assembler in assemblers
    )


def _critical_electric_factory_present(observation: dict[str, Any]) -> bool:
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "")
        recipe = str(entity.get("recipe") or entity.get("recipe_name") or "")
        if name == "lab":
            return True
        if name in ASSEMBLER_ENTITY_NAMES and recipe in CRITICAL_FACTORY_POWER_RECIPES:
            return True
    return False


def _starter_steam_power_issue(observation: dict[str, Any]) -> dict[str, Any] | None:
    for boiler in entities_named(observation, "boiler"):
        if _entity_status_name_in(boiler, {"no_fuel", "no_input_fluid"}):
            return {
                "unit": boiler.get("unit_number"),
                "entity": "boiler",
                "recipe": "steam-power",
                "status": str(boiler.get("status_name") or boiler.get("status") or "starved"),
            }
    for engine in entities_named(observation, "steam-engine"):
        if _entity_status_name_in(engine, {"no_input_fluid", "no_fuel", "no_power"}):
            return {
                "unit": engine.get("unit_number"),
                "entity": "steam-engine",
                "recipe": "steam-power",
                "status": str(engine.get("status_name") or engine.get("status") or "starved"),
            }
    return None


def _skill_for_bottleneck_item(item: str, observation: dict[str, Any]) -> str | None:
    if item == "coal":
        return "setup_coal_supply"
    if item == "stone":
        return "setup_stone_supply"
    if item in {"iron-plate", "iron-ore", "steel-plate"}:
        return _plate_smelting_skill("iron", observation)
    if item in {"copper-plate", "copper-ore", "copper-cable"}:
        return _plate_smelting_skill("copper", observation)
    if item == "automation-science-pack":
        return "produce_automation_science_pack"
    if item == "electronic-circuit":
        if _technology_researched(observation, "automation"):
            return "automate_electronic_circuit_line"
        return "produce_electronic_circuit"
    return None


def _plate_smelting_skill(kind: str, observation: dict[str, Any]) -> str:
    if _transport_belt_automation_ready(observation):
        return "expand_copper_smelting" if kind == "copper" else "expand_iron_smelting"
    if _technology_researched(observation, "automation"):
        return "build_gear_belt_mall_logistics"
    return "produce_copper_plate" if kind == "copper" else "produce_iron_plate"


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
        if entity_item_count(drill, "coal") > 0 or not _entity_status_is(drill, "no_fuel", 53):
            return True
    return False


def _entity_status_is(entity: dict[str, Any], status_name: str, status_code: int) -> bool:
    if str(entity.get("status_name") or "") == status_name:
        return True
    try:
        return int(entity.get("status")) == status_code
    except (TypeError, ValueError):
        return False


def _entity_status_name_in(entity: dict[str, Any], names: set[str]) -> bool:
    return str(entity.get("status_name") or "") in names


def _entity_no_power(entity: dict[str, Any]) -> bool:
    return _entity_status_is(entity, "no_power", 3) or _entity_status_is(entity, "no_power", 54)


def _entity_fluid_amount(entity: dict[str, Any], fluid_name: str) -> float:
    fluids = entity.get("fluids") if isinstance(entity.get("fluids"), dict) else {}
    total = 0.0
    for row in fluids.values():
        if not isinstance(row, dict) or row.get("name") != fluid_name:
            continue
        try:
            total += float(row.get("amount") or 0.0)
        except (TypeError, ValueError):
            pass
    return total


def _entity_on_resource(observation: dict[str, Any], entity: dict[str, Any], resource_name: str) -> bool:
    if str(entity.get("mining_target") or entity.get("resource_name") or "") == resource_name:
        return True
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


def _burner_drill_replacement_issue(observation: dict[str, Any]) -> dict[str, Any] | None:
    if not _technology_researched(observation, "automation"):
        return None
    if not _electric_network_available(observation):
        return None
    electric_researched = _technology_researched(observation, "electric-mining-drill")
    if not electric_researched and not _electric_drill_research_supply_ready(observation):
        return None
    burners = [
        drill
        for drill in entities_named(observation, "burner-mining-drill")
        if isinstance(drill, dict)
    ]
    if not burners:
        return None
    electric_drills = [
        drill
        for drill in entities_named(observation, "electric-mining-drill")
        if isinstance(drill, dict) and drill.get("electric_network_connected") is not False
    ]
    resource_counts: dict[str, int] = {}
    for drill in burners:
        resource = str(drill.get("mining_target") or drill.get("resource_name") or "")
        if not resource:
            nearest = _nearest_resource_name(observation, drill)
            resource = nearest or "unknown"
        resource_counts[resource] = resource_counts.get(resource, 0) + 1
    automated = any(
        assembler.get("recipe") == "electric-mining-drill" and assembler.get("electric_network_connected") is not False
        for assembler in entities_named(observation, "assembling-machine-1")
    )
    return {
        "burner_drill_count": len(burners),
        "electric_drill_count": len(electric_drills),
        "resource_counts": resource_counts,
        "electric_mining_drill_researched": electric_researched,
        "electric_mining_drill_stock": total_item_count(observation, "electric-mining-drill"),
        "electric_mining_drill_automated": automated,
    }


def _electric_drill_research_supply_ready(observation: dict[str, Any]) -> bool:
    if total_item_count(observation, "automation-science-pack") >= 25:
        return True
    for assembler in entities_named(observation, "assembling-machine-1"):
        if str(assembler.get("recipe") or assembler.get("recipe_name") or "") != "automation-science-pack":
            continue
        if assembler.get("electric_network_connected") is False or _entity_no_power(assembler):
            continue
        if _entity_status_name_in(assembler, {"working", "waiting_for_space_in_destination"}):
            return True
        if entity_item_count(assembler, "automation-science-pack") > 0:
            return True
    return False


def _electric_network_available(observation: dict[str, Any]) -> bool:
    for engine in entities_named(observation, "steam-engine"):
        if engine.get("electric_network_connected") is False:
            continue
        if _entity_no_power(engine) or _entity_status_name_in(engine, {"no_input_fluid", "no_fuel"}):
            continue
        if _entity_fluid_amount(engine, "steam") > 0 or str(engine.get("status_name") or "") == "working":
            return True
    for name in ("solar-panel", "accumulator", "steam-turbine"):
        for producer in entities_named(observation, name):
            if producer.get("electric_network_connected") is not False and not _entity_no_power(producer):
                return True
    return False


def _nearest_resource_name(observation: dict[str, Any], entity: dict[str, Any]) -> str | None:
    position = entity.get("position") if isinstance(entity.get("position"), dict) else None
    if position is None:
        return None
    resources = observation.get("resources") if isinstance(observation.get("resources"), list) else []
    candidates = [
        resource
        for resource in resources
        if isinstance(resource, dict)
        and isinstance(resource.get("position"), dict)
        and isinstance(resource.get("name"), str)
        and distance(position, resource["position"]) <= 4.5
    ]
    if not candidates:
        return None
    nearest = min(candidates, key=lambda resource: distance(position, resource["position"]))
    return str(nearest.get("name") or "")


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


def _coal_fuel_feed_needed(
    observation: dict[str, Any],
    objective: str,
    production_targets: dict[str, float] | None = None,
    *,
    monitor: dict[str, Any] | None = None,
) -> bool:
    monitor = monitor or summarize_factory(observation, objective, production_targets=production_targets)
    links = monitor.get("logistics_links") if isinstance(monitor.get("logistics_links"), list) else []
    for link in links:
        if not isinstance(link, dict) or link.get("item") != "coal":
            continue
        if link.get("status") != "route_needed":
            continue
        if "mining_patch" not in str(link.get("from_site") or ""):
            continue
        try:
            length = float(link.get("length_tiles") or 999999.0)
        except (TypeError, ValueError):
            length = 999999.0
        if length <= 32.0:
            return True
    return False


def _boiler_coal_feed_should_preempt_power(observation: dict[str, Any]) -> bool:
    issue = _starter_steam_power_issue(observation)
    if issue is None:
        return False
    if str(issue.get("entity") or "") != "boiler":
        return False
    if str(issue.get("status") or "") != "no_fuel":
        return False
    if not _critical_electric_factory_present(observation):
        return False
    return _coal_supply_ready(observation) and _transport_belt_automation_ready(observation)


def _transport_belt_automation_ready(observation: dict[str, Any]) -> bool:
    assemblers = (
        entities_named(observation, "assembling-machine-1")
        + entities_named(observation, "assembling-machine-2")
        + entities_named(observation, "assembling-machine-3")
    )
    for assembler in assemblers:
        recipe = str(assembler.get("recipe") or assembler.get("recipe_name") or "")
        if recipe != "transport-belt":
            continue
        if assembler.get("electric_network_connected") is False:
            continue
        if _entity_status_is(assembler, "no_power", 3):
            continue
        if entity_item_count(assembler, "transport-belt") > 0:
            return True
        if entity_item_count(assembler, "iron-gear-wheel") > 0 and entity_item_count(assembler, "iron-plate") > 0:
            return True
    return False


def _gear_mall_iron_plate_logistics_issue(observation: dict[str, Any]) -> dict[str, Any] | None:
    if not _technology_researched(observation, "automation"):
        return None
    belts_available = _transport_belts_available_for_mall_logistics(observation)
    assemblers = (
        entities_named(observation, "assembling-machine-1")
        + entities_named(observation, "assembling-machine-2")
        + entities_named(observation, "assembling-machine-3")
    )
    gear_assemblers = [
        entity
        for entity in assemblers
        if str(entity.get("recipe") or entity.get("recipe_name") or "") == "iron-gear-wheel"
        and entity.get("electric_network_connected") is not False
    ]
    source_furnaces = _iron_plate_source_furnaces(observation)
    if not gear_assemblers or not source_furnaces:
        return _relocated_gear_belt_mall_target_issue(
            observation,
            source_furnaces,
            belts_available,
        ) or _partial_gear_mall_relocation_issue(observation, source_furnaces, belts_available)
    best_issue: dict[str, Any] | None = None
    best_distance = 999999.0
    for gear in gear_assemblers:
        gear_position = _position(gear)
        if gear_position is None:
            continue
        if entity_item_count(gear, "iron-plate") >= 2:
            continue
        if _gear_mall_has_local_plate_input(observation, gear_position):
            continue
        for source in source_furnaces:
            source_position = _position(source)
            source_distance = _distance(gear_position, source_position)
            if source_distance is None or source_distance < 32.0:
                continue
            if source_distance >= best_distance:
                continue
            route_cost = _gear_mall_plate_route_cost_estimate(observation, source_position, gear_position)
            best_distance = source_distance
            best_issue = {
                "gear_unit": gear.get("unit_number"),
                "source_unit": source.get("unit_number"),
                "source_distance_tiles": round(source_distance, 1),
                "gear_assembler_iron_plate": entity_item_count(gear, "iron-plate"),
                "gear_assembler_status": gear.get("status_name") or gear.get("status"),
                "transport_belts_available": belts_available,
                **route_cost,
            }
    return best_issue or _relocated_gear_belt_mall_target_issue(observation, source_furnaces, belts_available)


def _relocated_gear_belt_mall_target_issue(
    observation: dict[str, Any],
    source_furnaces: list[dict[str, Any]],
    belts_available: bool,
) -> dict[str, Any] | None:
    if not source_furnaces:
        return None
    for source in source_furnaces:
        source_position = _position(source)
        target_gear_position = {"x": round(source_position["x"] + 5.5, 1), "y": round(source_position["y"] - 5.0, 1)}
        target_belt_position = {"x": round(target_gear_position["x"] + 3.0, 1), "y": target_gear_position["y"]}
        target_gear = _assembler_near_position(observation, target_gear_position)
        target_belt = _assembler_near_position(observation, target_belt_position)
        if target_gear is None and target_belt is None:
            continue
        route_cost = _gear_mall_plate_route_cost_estimate(observation, source_position, player_position(observation))
        route_cost = {**route_cost, "route_cost_preference": "relocate_mall_to_iron_source"}
        return {
            "gear_unit": target_gear.get("unit_number") if isinstance(target_gear, dict) else "inventory",
            "source_unit": source.get("unit_number"),
            "source_distance_tiles": round(distance(source_position, player_position(observation)), 1),
            "gear_assembler_iron_plate": entity_item_count(target_gear, "iron-plate") if isinstance(target_gear, dict) else 0,
            "gear_assembler_status": (
                target_gear.get("status_name") or target_gear.get("status") if isinstance(target_gear, dict) else "relocation_in_progress"
            ),
            "transport_belts_available": belts_available,
            "relocation_in_progress": True,
            "target_gear_assembler_unit": target_gear.get("unit_number") if isinstance(target_gear, dict) else None,
            "target_belt_assembler_unit": target_belt.get("unit_number") if isinstance(target_belt, dict) else None,
            **route_cost,
        }
    return None


def _assembler_near_position(observation: dict[str, Any], position: dict[str, float]) -> dict[str, Any] | None:
    candidates = [
        entity
        for entity in (
            entities_named(observation, "assembling-machine-1")
            + entities_named(observation, "assembling-machine-2")
            + entities_named(observation, "assembling-machine-3")
        )
        if _distance(_position(entity), position) is not None and (_distance(_position(entity), position) or 999999.0) <= 0.75
    ]
    return min(candidates, key=lambda entity: _distance(_position(entity), position) or 999999.0) if candidates else None


def _partial_gear_mall_relocation_issue(
    observation: dict[str, Any],
    source_furnaces: list[dict[str, Any]],
    belts_available: bool,
) -> dict[str, Any] | None:
    if inventory_count(observation, "assembling-machine-1") <= 0 or not source_furnaces:
        return None
    recoverable = _recoverable_relocation_assembler(observation)
    inventory_rebuild = not isinstance(recoverable, dict)
    recoverable_position = _position(recoverable) if isinstance(recoverable, dict) else player_position(observation)
    source = min(
        source_furnaces,
        key=lambda item: _distance(_position(item), recoverable_position) or 999999.0,
    )
    source_position = _position(source)
    source_distance = _distance(source_position, recoverable_position)
    if source_position is None or source_distance is None:
        return None
    if not inventory_rebuild and source_distance < 32.0:
        return None
    route_cost = _gear_mall_plate_route_cost_estimate(observation, source_position, recoverable_position)
    if inventory_rebuild:
        route_cost = {**route_cost, "route_cost_preference": "relocate_mall_to_iron_source"}
    if (
        not inventory_rebuild
        and route_cost.get("route_cost_preference") != "relocate_mall_to_iron_source"
        and source_distance <= PRE_RAIL_GEAR_MALL_PLATE_DISTANCE_LIMIT
    ):
        return None
    return {
        "gear_unit": "inventory",
        "source_unit": source.get("unit_number"),
        "source_distance_tiles": round(source_distance, 1),
        "gear_assembler_iron_plate": 0,
        "gear_assembler_status": "relocation_in_progress",
        "transport_belts_available": belts_available,
        "relocation_in_progress": True,
        "recoverable_assembler_unit": recoverable.get("unit_number") if isinstance(recoverable, dict) else None,
        **route_cost,
    }


def _recoverable_relocation_assembler(observation: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [
        entity
        for entity in entities_named(observation, "assembling-machine-1")
        if entity.get("electric_network_connected") is not False
        and str(entity.get("recipe") or entity.get("recipe_name") or "") not in {"copper-cable", "electronic-circuit"}
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda entity: (
            0
            if str(entity.get("recipe") or entity.get("recipe_name") or "") in {"small-electric-pole", "transport-belt", "iron-gear-wheel"}
            else 1,
            float(entity.get("distance") or 999999.0),
        )
    )
    return candidates[0]


def _gear_mall_plate_route_needs_compaction(issue: dict[str, Any] | None) -> bool:
    if not isinstance(issue, dict):
        return False
    if issue.get("route_cost_preference") == "relocate_mall_to_iron_source":
        return not bool(issue.get("transport_belts_available"))
    try:
        source_distance = float(issue.get("source_distance_tiles") or 0.0)
    except (TypeError, ValueError):
        source_distance = 0.0
    return (
        source_distance > PRE_RAIL_GEAR_MALL_PLATE_DISTANCE_LIMIT
        and not bool(issue.get("transport_belts_available"))
    )


def _gear_mall_relocation_power_pole_deficit(issue: dict[str, Any] | None, observation: dict[str, Any]) -> int:
    if not _gear_mall_plate_route_needs_compaction(issue):
        return 0
    required = issue.get("relocation_power_poles_estimate") if isinstance(issue, dict) else None
    if not isinstance(required, (int, float)):
        return 0
    layout = _find_gear_belt_mall_relocation_layout(observation)
    if layout is not None:
        corridor = _gear_belt_mall_relocation_power_corridor_positions(observation, layout)
        if corridor:
            required = float(len(_missing_power_corridor_positions(observation, corridor)))
    available = total_item_count(observation, "small-electric-pole")
    return max(0, int(ceil(float(required))) - int(available))


def _gear_mall_plate_route_cost_estimate(
    observation: dict[str, Any],
    source_position: dict[str, float],
    gear_position: dict[str, float],
) -> dict[str, Any]:
    source_distance = distance(source_position, gear_position)
    belt_tiles = int(ceil(max(0.0, source_distance)))
    belt_cost = round(belt_tiles * GEAR_MALL_PLATE_BELT_TILE_COST, 1)
    power_distance = _nearest_power_anchor_distance(observation, source_position)
    if power_distance is None:
        return {
            "belt_route_tiles_estimate": belt_tiles,
            "belt_route_cost": belt_cost,
            "power_anchor_distance_tiles": None,
            "relocation_power_poles_estimate": None,
            "relocation_cost": None,
            "route_cost_preference": (
                "relocate_mall_to_iron_source"
                if source_distance > PRE_RAIL_GEAR_MALL_PLATE_DISTANCE_LIMIT
                else "build_belt_route"
            ),
        }

    power_poles = int(ceil(max(0.0, power_distance) / SMALL_POWER_POLE_REACH))
    relocation_cost = round(
        GEAR_MALL_RELOCATION_FIXED_COST + power_poles * GEAR_MALL_RELOCATION_POWER_POLE_COST,
        1,
    )
    preference = (
        "relocate_mall_to_iron_source"
        if relocation_cost < belt_cost * GEAR_MALL_RELOCATION_ADVANTAGE_RATIO
        else "build_belt_route"
    )
    return {
        "belt_route_tiles_estimate": belt_tiles,
        "belt_route_cost": belt_cost,
        "power_anchor_distance_tiles": round(power_distance, 1),
        "relocation_power_poles_estimate": power_poles,
        "relocation_cost": relocation_cost,
        "route_cost_preference": preference,
    }


def _nearest_power_anchor_distance(observation: dict[str, Any], target_position: dict[str, float]) -> float | None:
    distances: list[float] = []
    for entity in observation.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "")
        if name not in POWER_ANCHOR_ENTITY_NAMES and entity.get("electric_network_connected") is not True:
            continue
        position = entity.get("position")
        if not isinstance(position, dict):
            continue
        distances.append(distance(position, target_position))
    return min(distances) if distances else None


def _gear_belt_mall_power_issue(observation: dict[str, Any]) -> dict[str, Any] | None:
    if not _technology_researched(observation, "automation"):
        return None
    assemblers = (
        entities_named(observation, "assembling-machine-1")
        + entities_named(observation, "assembling-machine-2")
        + entities_named(observation, "assembling-machine-3")
    )
    for assembler in assemblers:
        recipe = str(assembler.get("recipe") or assembler.get("recipe_name") or "")
        if recipe not in {"iron-gear-wheel", "transport-belt"}:
            continue
        if assembler.get("electric_network_connected") is False:
            continue
        if _entity_status_is(assembler, "no_power", 3):
            return {
                "unit": assembler.get("unit_number"),
                "recipe": recipe,
                "status": assembler.get("status_name") or assembler.get("status"),
            }
    return None


def _transport_belts_available_for_mall_logistics(observation: dict[str, Any]) -> bool:
    if inventory_count(observation, "transport-belt") > 0:
        return True
    assemblers = (
        entities_named(observation, "assembling-machine-1")
        + entities_named(observation, "assembling-machine-2")
        + entities_named(observation, "assembling-machine-3")
    )
    for assembler in assemblers:
        if str(assembler.get("recipe") or assembler.get("recipe_name") or "") != "transport-belt":
            continue
        if entity_item_count(assembler, "transport-belt") > 0:
            return True
        if assembler.get("electric_network_connected") is not False and not _entity_status_is(assembler, "no_power", 3):
            if entity_item_count(assembler, "iron-gear-wheel") > 0 and entity_item_count(assembler, "iron-plate") > 0:
                return True
    return False


def _gear_belt_mall_bootstrap_issue(observation: dict[str, Any]) -> dict[str, Any] | None:
    if not _technology_researched(observation, "automation"):
        return None
    if _transport_belts_available_for_mall_logistics(observation):
        return None
    assemblers = (
        entities_named(observation, "assembling-machine-1")
        + entities_named(observation, "assembling-machine-2")
        + entities_named(observation, "assembling-machine-3")
    )
    gear_assemblers = [
        entity
        for entity in assemblers
        if str(entity.get("recipe") or entity.get("recipe_name") or "") == "iron-gear-wheel"
        and entity.get("electric_network_connected") is not False
        and not _entity_status_is(entity, "no_power", 54)
    ]
    belt_assemblers = [
        entity
        for entity in assemblers
        if str(entity.get("recipe") or entity.get("recipe_name") or "") == "transport-belt"
        and entity.get("electric_network_connected") is not False
        and not _entity_status_is(entity, "no_power", 54)
    ]
    if not gear_assemblers or not belt_assemblers:
        return None

    best_issue: dict[str, Any] | None = None
    best_distance = 999999.0
    for gear in gear_assemblers:
        gear_position = _position(gear)
        if gear_position is None:
            continue
        gear_iron_plate = entity_item_count(gear, "iron-plate")
        gear_iron_gear = entity_item_count(gear, "iron-gear-wheel")
        local_gear_plate_seed = _local_iron_plate_seed_source(
            observation,
            gear_position,
            exclude_units={gear.get("unit_number")},
        )
        gear_can_be_seeded = (
            inventory_count(observation, "iron-plate") > 0
            or gear_iron_plate >= 2
            or gear_iron_gear > 0
            or local_gear_plate_seed is not None
        )
        if not gear_can_be_seeded:
            continue
        for belt in belt_assemblers:
            belt_position = _position(belt)
            mall_distance = _distance(gear_position, belt_position)
            if mall_distance is None or mall_distance > 16.0 or mall_distance >= best_distance:
                continue
            belt_output = entity_item_count(belt, "transport-belt")
            if belt_output > 0:
                continue
            belt_iron_plate = entity_item_count(belt, "iron-plate")
            belt_iron_gear = entity_item_count(belt, "iron-gear-wheel")
            local_belt_plate_seed = _local_iron_plate_seed_source(
                observation,
                belt_position,
                exclude_units={gear.get("unit_number"), belt.get("unit_number")},
            )
            if (
                belt_iron_plate <= 0
                and belt_iron_gear <= 0
                and inventory_count(observation, "iron-plate") <= 0
                and local_gear_plate_seed is None
                and local_belt_plate_seed is None
            ):
                continue
            local_seed = local_gear_plate_seed or local_belt_plate_seed
            best_distance = mall_distance
            best_issue = {
                "gear_unit": gear.get("unit_number"),
                "belt_unit": belt.get("unit_number"),
                "mall_distance_tiles": round(mall_distance, 1),
                "inventory_iron_plate": inventory_count(observation, "iron-plate"),
                "gear_assembler_iron_plate": gear_iron_plate,
                "gear_assembler_iron_gear": gear_iron_gear,
                "belt_assembler_iron_plate": belt_iron_plate,
                "belt_assembler_iron_gear": belt_iron_gear,
                "local_iron_plate_seed_source_unit": local_seed.get("unit_number") if isinstance(local_seed, dict) else None,
                "local_iron_plate_seed_distance": (
                    round(distance(_position(local_seed), gear_position), 1) if isinstance(local_seed, dict) else None
                ),
            }
    return best_issue


def _transport_belt_mall_retool_issue(observation: dict[str, Any]) -> dict[str, Any] | None:
    if not _technology_researched(observation, "automation"):
        return None
    if _transport_belt_automation_ready(observation):
        return None
    if inventory_count(observation, "assembling-machine-1") > 0:
        return None
    pole_stock = total_item_count(observation, "small-electric-pole")
    if pole_stock < 8:
        return None
    candidates: list[tuple[float, dict[str, Any]]] = []
    assemblers = (
        entities_named(observation, "assembling-machine-1")
        + entities_named(observation, "assembling-machine-2")
        + entities_named(observation, "assembling-machine-3")
    )
    for assembler in assemblers:
        if str(assembler.get("recipe") or assembler.get("recipe_name") or "") != "small-electric-pole":
            continue
        if assembler.get("electric_network_connected") is False:
            continue
        position = _position(assembler)
        if position is None:
            continue
        if _near_recipe_assembler_for_strategy(observation, position, {"copper-cable", "electronic-circuit"}, radius=3.0):
            continue
        candidates.append((float(assembler.get("distance") or 999999.0), assembler))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    assembler = candidates[0][1]
    return {
        "unit": assembler.get("unit_number"),
        "recipe": assembler.get("recipe") or assembler.get("recipe_name"),
        "position": assembler.get("position"),
        "small_electric_pole_stock": pole_stock,
        "clear_item": _first_incompatible_retool_item_for_strategy(assembler, "transport-belt"),
    }


def _transport_belt_mall_gear_retool_issue(observation: dict[str, Any]) -> dict[str, Any] | None:
    if not _technology_researched(observation, "automation"):
        return None
    if _transport_belt_automation_ready(observation):
        return None
    if inventory_count(observation, "assembling-machine-1") > 0:
        return None
    assemblers = (
        entities_named(observation, "assembling-machine-1")
        + entities_named(observation, "assembling-machine-2")
        + entities_named(observation, "assembling-machine-3")
    )
    belt_assemblers = [
        assembler
        for assembler in assemblers
        if str(assembler.get("recipe") or assembler.get("recipe_name") or "") == "transport-belt"
        and assembler.get("electric_network_connected") is not False
    ]
    if not belt_assemblers:
        return None
    if any(
        str(assembler.get("recipe") or assembler.get("recipe_name") or "") == "iron-gear-wheel"
        and assembler.get("electric_network_connected") is not False
        for assembler in assemblers
    ):
        return None
    best_issue: dict[str, Any] | None = None
    best_distance = 999999.0
    for belt in belt_assemblers:
        belt_position = _position(belt)
        if belt_position is None:
            continue
        for assembler in assemblers:
            recipe = str(assembler.get("recipe") or assembler.get("recipe_name") or "")
            if recipe in {"transport-belt", "iron-gear-wheel", "copper-cable", "electronic-circuit", "small-electric-pole"}:
                continue
            if assembler.get("electric_network_connected") is False:
                continue
            position = _position(assembler)
            if position is None:
                continue
            mall_distance = _distance(position, belt_position)
            if mall_distance is None or mall_distance > 16.0 or mall_distance >= best_distance:
                continue
            best_distance = mall_distance
            best_issue = {
                "belt_unit": belt.get("unit_number"),
                "gear_unit": assembler.get("unit_number"),
                "gear_recipe": recipe,
                "mall_distance_tiles": round(mall_distance, 1),
            }
    return best_issue


def _near_recipe_assembler_for_strategy(
    observation: dict[str, Any],
    position: dict[str, float],
    recipes: set[str],
    *,
    radius: float,
) -> bool:
    for assembler in (
        entities_named(observation, "assembling-machine-1")
        + entities_named(observation, "assembling-machine-2")
        + entities_named(observation, "assembling-machine-3")
    ):
        if str(assembler.get("recipe") or assembler.get("recipe_name") or "") not in recipes:
            continue
        other_position = _position(assembler)
        if other_position is not None and distance(position, other_position) <= radius:
            return True
    return False


def _first_incompatible_retool_item_for_strategy(assembler: dict[str, Any], target_recipe: str) -> str | None:
    allowed_by_recipe = {
        "transport-belt": {"iron-plate", "iron-gear-wheel", "transport-belt"},
    }
    allowed = allowed_by_recipe.get(target_recipe, set())
    inventories = assembler.get("inventories") if isinstance(assembler.get("inventories"), dict) else {}
    for inventory in inventories.values():
        if not isinstance(inventory, dict):
            continue
        for item, raw_count in inventory.items():
            try:
                count = int(raw_count or 0)
            except (TypeError, ValueError):
                count = 0
            if count > 0 and item not in allowed:
                return str(item)
    return None


def _local_iron_plate_seed_source(
    observation: dict[str, Any],
    target_position: dict[str, float],
    *,
    exclude_units: set[Any] | None = None,
    max_distance: float = 16.0,
) -> dict[str, Any] | None:
    excluded = set(exclude_units or set())
    allowed_names = {
        "assembling-machine-1",
        "assembling-machine-2",
        "assembling-machine-3",
        "stone-furnace",
        "steel-furnace",
        "electric-furnace",
        "wooden-chest",
        "iron-chest",
        "steel-chest",
    }
    candidates: list[dict[str, Any]] = []
    for entity in observation.get("entities") or []:
        if not isinstance(entity, dict) or entity.get("unit_number") in excluded:
            continue
        if str(entity.get("name") or "") not in allowed_names:
            continue
        if entity_item_count(entity, "iron-plate") <= 0:
            continue
        position = _position(entity)
        if distance(position, target_position) > max_distance:
            continue
        candidates.append(entity)
    if not candidates:
        return None
    return min(candidates, key=lambda entity: distance(_position(entity), target_position))


def _iron_plate_source_furnaces(observation: dict[str, Any]) -> list[dict[str, Any]]:
    entities = observation.get("entities")
    if not isinstance(entities, list):
        return []
    furnaces: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict) or entity.get("name") not in {"stone-furnace", "steel-furnace", "electric-furnace"}:
            continue
        if str(entity.get("recipe") or entity.get("recipe_name") or "") != "iron-plate" and entity_item_count(entity, "iron-plate") <= 0:
            continue
        furnaces.append(entity)
    return furnaces


def _gear_mall_has_local_plate_input(observation: dict[str, Any], gear_position: dict[str, float]) -> bool:
    target_belt = {"x": gear_position["x"] + 1.0, "y": gear_position["y"] - 3.0}
    target_inserter = {"x": gear_position["x"] + 1.0, "y": gear_position["y"] - 2.0}
    return _entity_near_name(observation, "transport-belt", target_belt, radius=0.8) and any(
        _entity_near_name(observation, name, target_inserter, radius=0.8)
        for name in ("inserter", "burner-inserter", "fast-inserter", "stack-inserter")
    )


def _entity_near_name(observation: dict[str, Any], name: str, position: dict[str, float], radius: float) -> bool:
    for entity in entities_named(observation, name):
        entity_position = _position(entity)
        if entity_position is None:
            continue
        entity_distance = _distance(entity_position, position)
        if entity_distance is not None and entity_distance <= radius:
            return True
    return False


def _first_actionable_target_deficit(
    objective: str,
    observation: dict[str, Any],
    production_targets: dict[str, float] | None,
) -> tuple[str, str, float, float] | None:
    if not production_targets:
        return None
    monitor = summarize_factory(observation, objective, production_targets=production_targets)
    target_status = monitor.get("target_status")
    if isinstance(target_status, dict):
        target_rows = target_status.get("items") if isinstance(target_status.get("items"), list) else []
    else:
        target_rows = target_status if isinstance(target_status, list) else []
    candidates: list[tuple[int, float, float, str, str, float]] = []
    for row in target_rows:
        if not isinstance(row, dict):
            continue
        deficit = float(row.get("deficit_per_minute") or 0.0)
        if deficit <= 0.0:
            continue
        item = str(row.get("item") or "")
        skill = _skill_for_bottleneck_item(item, observation)
        if not skill:
            continue
        if skill in {"bootstrap_build_item_mall", "build_gear_belt_mall_logistics"}:
            if item in {"iron-plate", "iron-ore", "steel-plate"} and not _starter_resource_available(observation, "iron-ore"):
                continue
            if item in {"copper-plate", "copper-ore", "copper-cable"} and not _starter_resource_available(observation, "copper-ore"):
                continue
        if not _skill_has_starter_inputs(skill, observation):
            continue
        target = float(row.get("target_per_minute") or 0.0)
        deficit_ratio = deficit / max(target, 0.001)
        candidates.append(
            (
                _target_deficit_priority(item),
                deficit_ratio,
                deficit,
                item,
                skill,
                float(row.get("estimated_per_minute") or 0.0),
            )
        )
    if candidates:
        candidates.sort(reverse=True)
        _, _, deficit, item, skill, estimated = candidates[0]
        return item, skill, estimated, deficit
    return None


def _target_deficit_priority(item: str) -> int:
    return {
        "iron-plate": 100,
        "copper-plate": 90,
        "automation-science-pack": 85,
        "electronic-circuit": 80,
    }.get(item, 50)


def _skill_has_starter_inputs(skill: str, observation: dict[str, Any]) -> bool:
    if skill == "expand_iron_smelting":
        return _starter_resource_available(observation, "iron-ore")
    if skill == "expand_copper_smelting":
        return _starter_resource_available(observation, "copper-ore")
    if skill == "produce_iron_plate":
        return _starter_resource_available(observation, "iron-ore")
    if skill == "produce_copper_plate":
        return _starter_resource_available(observation, "copper-ore")
    if skill == "setup_coal_supply":
        return _starter_resource_available(observation, "coal")
    if skill == "setup_stone_supply":
        return _starter_resource_available(observation, "stone")
    return True


def _starter_resource_available(observation: dict[str, Any], resource_name: str, max_distance: float = 192.0) -> bool:
    base = observation.get("base") if isinstance(observation.get("base"), dict) else {}
    anchor = base.get("anchor_position") or base.get("spawn_position")
    if not isinstance(anchor, dict):
        return True
    resources = observation.get("resources") if isinstance(observation.get("resources"), list) else []
    for resource in resources:
        if not isinstance(resource, dict) or resource.get("name") != resource_name:
            continue
        try:
            distance_from_base = float(resource.get("distance_from_base"))
        except (TypeError, ValueError):
            position = resource.get("position") if isinstance(resource.get("position"), dict) else None
            distance_from_base = _distance(anchor, position) if position else None
        if distance_from_base is not None and distance_from_base <= max_distance:
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


def _recipe_unlocked(observation: dict[str, Any], recipe: str) -> bool:
    recipes = observation.get("recipe_unlocks")
    if not isinstance(recipes, dict):
        return False
    state = recipes.get(recipe)
    return bool(isinstance(state, dict) and state.get("enabled"))


def _entity_count_by_name(observation: dict[str, Any], name: str) -> int:
    entities = observation.get("entities")
    if not isinstance(entities, list):
        return 0
    return sum(1 for entity in entities if isinstance(entity, dict) and str(entity.get("name") or "") == name)


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
        "long-handed-inserter": 20,
        "burner-inserter": 10,
        "burner-mining-drill": 5,
        "electric-mining-drill": 6,
        "stone-furnace": 10,
        "small-electric-pole": 20,
        "assembling-machine-1": 10,
    }.get(item, 5)
