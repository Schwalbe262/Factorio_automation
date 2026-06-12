from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .knowledge import dependency_tree_for_objective
from .monitor import summarize_factory
from .models import inventory_count, total_item_count


KOREAN_ELECTRONIC_CIRCUIT = "\uc804\uc790\ud68c\ub85c"
KOREAN_ROCKET = "\ub85c\ucf13"


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
        description="Build additional iron ore mining and smelting capacity.",
        executor="future ExpandSmeltingSkill",
        preconditions=["iron ore patch identified", "fuel or power available", "furnaces/drills craftable or available"],
        completion=["sustained iron plate output exceeds downstream demand"],
        llm_scope="Choose this when downstream goals are blocked by low iron throughput.",
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
        executor="future SetupPowerSkill",
        preconditions=["water located", "coal available", "stone/iron/copper supply available"],
        completion=["electric network has sustained production and powered poles"],
        llm_scope="Choose this before electric miners, assemblers, labs, and scalable factory blocks.",
    ),
    "produce_electronic_circuit": SkillContract(
        name="produce_electronic_circuit",
        description="Produce early green circuits from iron plates and copper cable.",
        executor="ElectronicCircuitSkill",
        preconditions=["iron plates available or producible", "copper plates available or producible", "copper cable recipe available"],
        completion=["electronic circuits exist in inventory"],
        llm_scope="Choose this after diagnosing whether iron or copper supply is the real bottleneck.",
    ),
    "plan_factory_site": SkillContract(
        name="plan_factory_site",
        description="Choose a factory district location and expansion direction before building a larger block.",
        executor="future SpatialPlannerSkill",
        preconditions=["observed resource patches", "existing factory footprint", "known near-term production goal"],
        completion=["candidate site and reserved logistics corridors are selected"],
        llm_scope=(
            "Choose relative factory placement, expansion direction, and logistics corridors. "
            "Do not emit exact tile placements."
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
    "research_logistics": SkillContract(
        name="research_logistics",
        description="Feed labs with automation science to unlock belts/splitters and early logistics.",
        executor="future ResearchSkill",
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
        "goal_dependency_tree": dependency_tree_for_objective(objective, max_depth=5),
        "available_skills": skill_catalog_payload(),
        "decision_rule": (
            "Select exactly one high-level skill. Diagnose bottlenecks first. "
            "For spatial work, choose districts, corridors, or rail topology only. "
            "Do not emit tile-level movement, mining, building, rail, or signal actions."
        ),
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
    monitor = summarize_factory(observation, objective, production_targets=production_targets)
    bottlenecks = monitor.get("bottlenecks") if isinstance(monitor.get("bottlenecks"), list) else []

    if bottlenecks:
        first = bottlenecks[0] if isinstance(bottlenecks[0], dict) else {}
        item = str(first.get("item") or "")
        skill = _skill_for_bottleneck_item(item)
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
                selected_skill="produce_copper_plate",
                priority=85,
                reason="Electronic circuits also need copper cable; copper plates are below the circuit threshold.",
                evidence=[f"copper_plate_total={total_copper}"],
                blockers=["copper plate throughput"],
                expected_effect="Create copper plate supply for cable production.",
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
        if science < 5:
            return StrategicDecision(
                selected_skill="produce_automation_science_pack",
                priority=90,
                reason="The first research tier is not established yet.",
                evidence=[f"automation_science_pack_total={science}"],
                blockers=["automation science"],
                expected_effect="Unlock early research progression.",
            ).to_dict()
        return StrategicDecision(
            selected_skill="setup_power",
            priority=80,
            reason="After red science, scalable automation needs electricity.",
            evidence=[f"automation_science_pack_total={science}", f"iron_plate_total={total_iron}"],
            blockers=["electric power"],
            expected_effect="Enable electric miners, labs, and assemblers.",
        ).to_dict()

    if total_iron < 10:
        selected = "produce_iron_plate"
        reason = "Default strategy found low iron plates."
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


def _skill_for_bottleneck_item(item: str) -> str | None:
    if item in {"iron-plate", "iron-ore", "steel-plate"}:
        return "expand_iron_smelting"
    if item in {"copper-plate", "copper-ore", "copper-cable"}:
        return "produce_copper_plate"
    if item == "automation-science-pack":
        return "produce_automation_science_pack"
    if item == "electronic-circuit":
        return "produce_electronic_circuit"
    return None


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
