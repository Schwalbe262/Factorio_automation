from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .models import distance, entity_item_count, inventory_count, total_item_count


ASSEMBLER_ENTITY_NAMES = {"assembling-machine-1", "assembling-machine-2", "assembling-machine-3"}
FURNACE_ENTITY_NAMES = {"stone-furnace", "steel-furnace", "electric-furnace"}
GEAR_BELT_MALL_ASSEMBLER_SPACING = 4.0
NORTH = 0
EAST = 4
SOUTH = 8
WEST = 12


@dataclass(frozen=True)
class FactoryReadiness:
    automation_researched: bool
    gear_mall_exists: bool
    belt_mall_exists: bool
    gear_belt_logistics_connection_ready: bool
    belt_mall_can_output: bool
    iron_plate_source_ready: bool
    belt_line_buildable: bool
    boiler_feed_buildable: bool
    virtual_agent: bool
    bootstrap_seed_allowed: bool
    failure_root: str | None
    repair_skill: str | None
    blocked_by: tuple[str, ...] = ()
    seed_reason: str | None = None
    expected_followup: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["blocked_by"] = list(self.blocked_by)
        return data


def build_factory_readiness(observation: dict[str, Any]) -> FactoryReadiness:
    """Read-only bootstrap state shared by strategy, planner, recovery, and UI."""

    automation_researched = _technology_researched(observation, "automation")
    virtual_agent = _is_virtual_agent(observation)
    assemblers = _assemblers(observation)
    gear_assemblers = [
        entity
        for entity in assemblers
        if _recipe(entity) == "iron-gear-wheel" and _entity_powered(entity)
    ]
    belt_assemblers = [
        entity
        for entity in assemblers
        if _recipe(entity) == "transport-belt" and _entity_powered(entity)
    ]
    gear_belt_logistics_pair_exists = _gear_belt_logistics_pair_exists(assemblers, gear_assemblers)
    gear_belt_logistics_connection_ready = _gear_belt_logistics_connection_ready(
        observation,
        assemblers,
        gear_assemblers,
        belt_assemblers,
    )
    iron_sources = _iron_plate_sources(observation)
    transport_belt_stock = total_item_count(observation, "transport-belt")
    belt_mall_can_output = _belt_mall_can_output(observation, gear_assemblers, belt_assemblers)
    belt_mall_has_output = _belt_mall_has_output(belt_assemblers)
    belt_mall_has_output_source = belt_mall_has_output or _belt_mall_output_chest_exists(observation, belt_assemblers)
    belt_line_buildable = transport_belt_stock > 0
    coal_supply_ready = _coal_supply_ready(observation)
    starter_fuel_starved = _starter_fuel_supply_starved(observation) and not coal_supply_ready
    boiler_needs_fuel = _boiler_needs_fuel(observation)
    boiler_feed_buildable = boiler_needs_fuel and coal_supply_ready and belt_line_buildable
    bootstrap_seed_allowed = (
        automation_researched
        and virtual_agent
        and bool(gear_assemblers)
        and bool(belt_assemblers)
        and bool(iron_sources)
        and not belt_line_buildable
        and not belt_mall_can_output
    )

    blocked_by: list[str] = []
    failure_root: str | None = None
    repair_skill: str | None = None
    if automation_researched and starter_fuel_starved:
        failure_root = "starter_fuel_supply_starved"
        repair_skill = "setup_coal_supply"
        blocked_by.append("coal supply")
    elif automation_researched and not iron_sources:
        failure_root = "iron_plate_source_missing"
        repair_skill = "produce_iron_plate"
        blocked_by.append("iron-plate source")
    elif automation_researched and not gear_assemblers:
        failure_root = "gear_mall_missing"
        repair_skill = "bootstrap_build_item_mall"
        blocked_by.append("iron-gear mall")
    elif automation_researched and not belt_assemblers:
        failure_root = "belt_mall_missing"
        repair_skill = "bootstrap_build_item_mall"
        blocked_by.append("transport-belt mall")
    elif (
        automation_researched
        and gear_belt_logistics_pair_exists
        and belt_line_buildable
        and not belt_mall_has_output_source
        and not gear_belt_logistics_connection_ready
    ):
        failure_root = "gear_belt_logistics_incomplete"
        repair_skill = "build_gear_belt_mall_logistics"
        blocked_by.append("gear/belt mall logistics connection")
    elif automation_researched and not belt_line_buildable:
        failure_root = "belt_line_unbuildable"
        repair_skill = (
            "bootstrap_build_item_mall"
            if bootstrap_seed_allowed or not gear_belt_logistics_pair_exists
            else "build_gear_belt_mall_logistics"
        )
        blocked_by.append("construction transport belts")
        if not belt_mall_can_output:
            blocked_by.append("transport-belt mall output")
        if belt_assemblers and not gear_belt_logistics_pair_exists:
            blocked_by.append("gear/belt mall logistics pair")
    elif boiler_needs_fuel and not boiler_feed_buildable:
        failure_root = "boiler_feed_unbuildable"
        repair_skill = (
            "connect_coal_fuel_feed"
            if belt_line_buildable
            else ("build_gear_belt_mall_logistics" if gear_belt_logistics_pair_exists else "bootstrap_build_item_mall")
        )
        if not coal_supply_ready:
            blocked_by.append("coal supply")
        if not belt_line_buildable:
            blocked_by.append("construction transport belts")

    seed_reason = None
    expected_followup = None
    if bootstrap_seed_allowed:
        seed_reason = "virtual agent one-time iron/gear seed to start belt mall output"
        expected_followup = "transport-belt output increases or belt/inserter connection completes"

    details = {
        "transport_belt_stock": transport_belt_stock,
        "gear_mall_units": [_unit(entity) for entity in gear_assemblers],
        "belt_mall_units": [_unit(entity) for entity in belt_assemblers],
        "iron_plate_source_units": [_unit(entity) for entity in iron_sources],
        "gear_belt_logistics_pair_exists": gear_belt_logistics_pair_exists,
        "gear_belt_logistics_connection_ready": gear_belt_logistics_connection_ready,
        "belt_mall_output_source_ready": belt_mall_has_output_source,
        "coal_supply_ready": coal_supply_ready,
        "starter_fuel_starved": starter_fuel_starved,
        "boiler_needs_fuel": boiler_needs_fuel,
        "belt_mall_can_output_reason": _belt_mall_output_reason(observation, gear_assemblers, belt_assemblers),
    }
    return FactoryReadiness(
        automation_researched=automation_researched,
        gear_mall_exists=bool(gear_assemblers),
        belt_mall_exists=bool(belt_assemblers),
        gear_belt_logistics_connection_ready=gear_belt_logistics_connection_ready,
        belt_mall_can_output=belt_mall_can_output,
        iron_plate_source_ready=bool(iron_sources),
        belt_line_buildable=belt_line_buildable,
        boiler_feed_buildable=boiler_feed_buildable,
        virtual_agent=virtual_agent,
        bootstrap_seed_allowed=bootstrap_seed_allowed,
        failure_root=failure_root,
        repair_skill=repair_skill,
        blocked_by=tuple(blocked_by),
        seed_reason=seed_reason,
        expected_followup=expected_followup,
        details=details,
    )


def _technology_researched(observation: dict[str, Any], technology: str) -> bool:
    research = observation.get("research") if isinstance(observation.get("research"), dict) else {}
    technologies = research.get("technologies") if isinstance(research.get("technologies"), dict) else {}
    state = technologies.get(technology)
    return bool(isinstance(state, dict) and state.get("researched"))


def _is_virtual_agent(observation: dict[str, Any]) -> bool:
    player = observation.get("player") if isinstance(observation.get("player"), dict) else {}
    execution = observation.get("execution") if isinstance(observation.get("execution"), dict) else {}
    return (
        player.get("character_valid") is False
        or execution.get("virtual") is True
        or str(player.get("kind") or "") == "server"
    )


def _entities(observation: dict[str, Any]) -> list[dict[str, Any]]:
    entities = observation.get("entities")
    return [entity for entity in entities if isinstance(entity, dict)] if isinstance(entities, list) else []


def _assemblers(observation: dict[str, Any]) -> list[dict[str, Any]]:
    return [entity for entity in _entities(observation) if str(entity.get("name") or "") in ASSEMBLER_ENTITY_NAMES]


def _recipe(entity: dict[str, Any]) -> str:
    return str(entity.get("recipe") or entity.get("recipe_name") or "")


def _entity_powered(entity: dict[str, Any]) -> bool:
    if entity.get("electric_network_connected") is False:
        return False
    status = str(entity.get("status_name") or "")
    if status == "no_power":
        return False
    try:
        return int(entity.get("status")) not in {3, 54}
    except (TypeError, ValueError):
        return True


def _iron_plate_sources(observation: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for entity in _entities(observation):
        name = str(entity.get("name") or "")
        if name not in FURNACE_ENTITY_NAMES and name not in {"wooden-chest", "iron-chest", "steel-chest"}:
            continue
        if entity_item_count(entity, "iron-plate") <= 0 and _recipe(entity) != "iron-plate":
            continue
        sources.append(entity)
    if inventory_count(observation, "iron-plate") > 0:
        sources.append({"name": "player-inventory", "unit_number": "inventory"})
    return sources


def _belt_mall_can_output(
    observation: dict[str, Any],
    gear_assemblers: list[dict[str, Any]],
    belt_assemblers: list[dict[str, Any]],
) -> bool:
    return _belt_mall_output_reason(observation, gear_assemblers, belt_assemblers) is not None


def _belt_mall_output_reason(
    observation: dict[str, Any],
    gear_assemblers: list[dict[str, Any]],
    belt_assemblers: list[dict[str, Any]],
) -> str | None:
    if total_item_count(observation, "transport-belt") > 0:
        return "transport_belt_stock"
    for belt in belt_assemblers:
        if entity_item_count(belt, "transport-belt") > 0:
            return "belt_assembler_output"
        if entity_item_count(belt, "iron-gear-wheel") > 0 and entity_item_count(belt, "iron-plate") > 0:
            return "belt_assembler_buffered_inputs"
        belt_position = _position(belt)
        if belt_position is None or entity_item_count(belt, "iron-plate") <= 0:
            continue
        for gear in gear_assemblers:
            gear_position = _position(gear)
            if gear_position is None:
                continue
            if distance(gear_position, belt_position) <= 16.0 and entity_item_count(gear, "iron-gear-wheel") > 0:
                return "nearby_gear_output_and_belt_plate"
    return None


def _belt_mall_has_output(belt_assemblers: list[dict[str, Any]]) -> bool:
    return any(entity_item_count(belt, "transport-belt") > 0 for belt in belt_assemblers)


def _belt_mall_output_chest_exists(
    observation: dict[str, Any],
    belt_assemblers: list[dict[str, Any]],
) -> bool:
    belt_positions = [_position(belt) for belt in belt_assemblers]
    belt_positions = [position for position in belt_positions if position is not None]
    if not belt_positions:
        return False
    for entity in _entities(observation):
        if str(entity.get("name") or "") not in {"wooden-chest", "iron-chest", "steel-chest"}:
            continue
        if entity_item_count(entity, "transport-belt") <= 0:
            continue
        chest_position = _position(entity)
        if chest_position is None:
            continue
        if any(distance(chest_position, belt_position) <= 4.0 for belt_position in belt_positions):
            return True
    return False


def _gear_belt_logistics_pair_exists(
    assemblers: list[dict[str, Any]],
    gear_assemblers: list[dict[str, Any]],
) -> bool:
    for gear in gear_assemblers:
        gear_position = _position(gear)
        if gear_position is None:
            continue
        for candidate in assemblers:
            if candidate is gear or not _entity_powered(candidate):
                continue
            if _recipe(candidate) in {"copper-cable", "electronic-circuit"}:
                continue
            candidate_position = _position(candidate)
            if candidate_position is None:
                continue
            if abs(candidate_position["y"] - gear_position["y"]) > 0.1:
                continue
            horizontal = abs(candidate_position["x"] - gear_position["x"])
            if GEAR_BELT_MALL_ASSEMBLER_SPACING <= horizontal <= 8.0:
                return True
    return False


def _gear_belt_logistics_connection_ready(
    observation: dict[str, Any],
    assemblers: list[dict[str, Any]],
    gear_assemblers: list[dict[str, Any]],
    belt_assemblers: list[dict[str, Any]],
) -> bool:
    for gear in gear_assemblers:
        gear_position = _position(gear)
        if gear_position is None:
            continue
        for belt in belt_assemblers:
            belt_position = _position(belt)
            if belt_position is None:
                continue
            if abs(belt_position["y"] - gear_position["y"]) > 0.1:
                continue
            horizontal = abs(belt_position["x"] - gear_position["x"])
            if horizontal < GEAR_BELT_MALL_ASSEMBLER_SPACING or horizontal > 8.0:
                continue
            if _direct_gear_transfer_ready(observation, gear_position, belt_position):
                return True
            if _belt_lane_transfer_ready(observation, gear_position, belt_position):
                return True
    return False


def _direct_gear_transfer_ready(
    observation: dict[str, Any],
    gear_position: dict[str, float],
    belt_position: dict[str, float],
) -> bool:
    direction_sign = 1 if belt_position["x"] >= gear_position["x"] else -1
    if abs(abs(belt_position["x"] - gear_position["x"]) - GEAR_BELT_MALL_ASSEMBLER_SPACING) > 0.25:
        return False
    direct_position = {"x": gear_position["x"] + direction_sign * 2.0, "y": gear_position["y"]}
    return _inserter_at(observation, direct_position, EAST if direction_sign > 0 else WEST) is not None


def _belt_lane_transfer_ready(
    observation: dict[str, Any],
    gear_position: dict[str, float],
    belt_position: dict[str, float],
) -> bool:
    direction_sign = 1 if belt_position["x"] >= gear_position["x"] else -1
    belt_direction = EAST if direction_sign > 0 else WEST
    horizontal_distance = abs(belt_position["x"] - gear_position["x"])
    steps = max(1, int(round(horizontal_distance)) - 1)
    for vertical_sign, output_direction, input_direction in [(-1, SOUTH, NORTH), (1, NORTH, SOUTH)]:
        lane_y = gear_position["y"] + (3.0 * vertical_sign)
        inserter_y = gear_position["y"] + (2.0 * vertical_sign)
        output_position = {"x": gear_position["x"] + direction_sign, "y": inserter_y}
        input_position = {"x": belt_position["x"] - direction_sign, "y": inserter_y}
        if _inserter_at(observation, output_position, output_direction) is None:
            continue
        if _inserter_at(observation, input_position, input_direction) is None:
            continue
        if all(
            _entity_at(observation, "transport-belt", {"x": gear_position["x"] + direction_sign * step, "y": lane_y}, belt_direction)
            is not None
            for step in range(1, steps + 1)
        ):
            return True
    return False


def _inserter_at(observation: dict[str, Any], position: dict[str, float], direction: int) -> dict[str, Any] | None:
    for name in ("inserter", "burner-inserter", "fast-inserter"):
        entity = _entity_at(observation, name, position, direction)
        if entity is not None and _entity_powered(entity) and not _entity_status_name_is(entity, "no_fuel"):
            return entity
    return None


def _entity_at(
    observation: dict[str, Any],
    name: str,
    position: dict[str, float],
    direction: int | None = None,
) -> dict[str, Any] | None:
    for entity in _entities(observation):
        if str(entity.get("name") or "") != name:
            continue
        entity_position = _position(entity)
        if entity_position is None or distance(entity_position, position) > 0.35:
            continue
        if direction is not None and _direction_or_default(entity.get("direction"), direction) != direction:
            continue
        return entity
    return None


def _direction_or_default(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _coal_supply_ready(observation: dict[str, Any]) -> bool:
    if inventory_count(observation, "coal") >= 8:
        return True
    for entity in _entities(observation):
        name = str(entity.get("name") or "")
        if name in {"wooden-chest", "iron-chest", "steel-chest", "transport-belt"} and entity_item_count(entity, "coal") > 0:
            return True
        if name not in {"burner-mining-drill", "electric-mining-drill"}:
            continue
        if str(entity.get("mining_target") or entity.get("resource_name") or "") != "coal":
            continue
        if entity_item_count(entity, "coal") > 0:
            return True
        if name == "electric-mining-drill" and _entity_powered(entity):
            return True
        if name == "burner-mining-drill" and not _entity_status_name_is(entity, "no_fuel"):
            return True
    return False


def _starter_fuel_supply_starved(observation: dict[str, Any]) -> bool:
    for entity in _entities(observation):
        if str(entity.get("name") or "") not in {"burner-mining-drill", "stone-furnace"}:
            continue
        if _entity_status_name_is(entity, "no_fuel"):
            return True
    return False


def _boiler_needs_fuel(observation: dict[str, Any]) -> bool:
    for entity in _entities(observation):
        if str(entity.get("name") or "") == "boiler" and _entity_status_name_is(entity, "no_fuel"):
            return True
    return False


def _entity_status_name_is(entity: dict[str, Any], status_name: str) -> bool:
    return str(entity.get("status_name") or "") == status_name


def _position(entity: dict[str, Any]) -> dict[str, float] | None:
    value = entity.get("position")
    if not isinstance(value, dict):
        return None
    try:
        return {"x": float(value["x"]), "y": float(value["y"])}
    except (KeyError, TypeError, ValueError):
        return None


def _unit(entity: dict[str, Any]) -> Any:
    return entity.get("unit_number")
