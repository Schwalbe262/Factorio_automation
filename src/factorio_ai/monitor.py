from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .knowledge import (
    RAW_RESOURCES,
    RECIPES,
    dependency_tree_for_objective,
    recipe_for_product,
    required_items_for_objective,
    technology_chain_for_recipe,
)
from .models import distance, entity_item_count, total_item_count


@dataclass(frozen=True)
class ProductionEstimate:
    item: str
    per_minute: float
    producers: int
    confidence: float
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BottleneckEstimate:
    item: str
    reason: str
    stock: int
    estimated_per_minute: float
    required_by: list[str]
    severity: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ConsumptionEstimate:
    item: str
    per_minute: float
    consumers: int
    confidence: float
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


COMMON_ITEMS = [
    "iron-ore",
    "copper-ore",
    "coal",
    "stone",
    "iron-plate",
    "copper-plate",
    "stone-brick",
    "steel-plate",
    "iron-gear-wheel",
    "copper-cable",
    "electronic-circuit",
    "automation-science-pack",
    "logistic-science-pack",
    "advanced-circuit",
    "processing-unit",
    "plastic-bar",
    "sulfur",
    "sulfuric-acid",
    "solid-fuel",
    "rocket-fuel",
    "low-density-structure",
    "rocket-part",
]

FURNACE_SPEEDS = {
    "stone-furnace": 1.0,
    "steel-furnace": 2.0,
    "electric-furnace": 2.0,
}
MINER_RATES_PER_MINUTE = {
    "burner-mining-drill": 15.0,
    "electric-mining-drill": 30.0,
}
ASSEMBLER_SPEEDS = {
    "assembling-machine-1": 0.5,
    "assembling-machine-2": 0.75,
    "assembling-machine-3": 1.25,
}
NORTH = 0
EAST = 4
SOUTH = 8
WEST = 12


def summarize_factory(
    observation: dict[str, Any],
    objective: str = "launch_rocket_program",
    production_targets: dict[str, float] | None = None,
) -> dict[str, Any]:
    inventory = inventory_summary(observation, objective)
    production = estimate_production(observation)
    consumption = estimate_consumption(observation)
    net_rates = estimate_net_rates(production, consumption)
    bottlenecks = estimate_bottlenecks(objective, observation, production, production_targets or {})
    target_status = production_target_status(production_targets or {}, production)
    dependency = dependency_tree_for_objective(objective, max_depth=5)
    technologies = technology_requirements_for_objective(objective)
    return {
        "objective": objective,
        "inventory": inventory,
        "targets_per_minute": dict(sorted((production_targets or {}).items())),
        "production": [item.to_dict() for item in production],
        "consumption": [item.to_dict() for item in consumption],
        "net_rates": net_rates,
        "target_status": target_status,
        "bottlenecks": [item.to_dict() for item in bottlenecks],
        "dependency_tree": dependency,
        "technology_chain": technologies,
    }


def inventory_summary(observation: dict[str, Any], objective: str) -> dict[str, int]:
    required = required_items_for_objective(objective, max_depth=5)
    items = sorted(set(COMMON_ITEMS) | required)
    return {item: total_item_count(observation, item) for item in items if total_item_count(observation, item) > 0}


def estimate_production(observation: dict[str, Any]) -> list[ProductionEstimate]:
    rates: dict[str, ProductionEstimate] = {}
    busy_plate_furnaces = {"iron-plate": 0, "copper-plate": 0}
    for entity in _entities(observation):
        name = str(entity.get("name") or "")
        if name in FURNACE_SPEEDS:
            estimate = _estimate_furnace(entity, FURNACE_SPEEDS[name])
            if estimate and estimate.item in busy_plate_furnaces:
                busy_plate_furnaces[estimate.item] += 1
            _add_estimate(rates, estimate)
        elif name in MINER_RATES_PER_MINUTE:
            _add_estimate(rates, _estimate_miner(entity, observation, MINER_RATES_PER_MINUTE[name]))
        elif name in ASSEMBLER_SPEEDS:
            _add_estimate(rates, _estimate_assembler(entity, ASSEMBLER_SPEEDS[name]))
    for product, resource in [("iron-plate", "iron-ore"), ("copper-plate", "copper-ore")]:
        complete_lines = _complete_belt_line_count(observation, resource)
        extra_lines = max(0, complete_lines - busy_plate_furnaces[product])
        if extra_lines:
            _add_estimate(
                rates,
                ProductionEstimate(
                    item=product,
                    per_minute=round(extra_lines * 18.75, 3),
                    producers=extra_lines,
                    confidence=0.5,
                    notes=[f"inferred from complete burner {resource} belt smelting lines"],
                ),
            )
    return sorted(rates.values(), key=lambda item: (-item.per_minute, item.item))


def estimate_consumption(observation: dict[str, Any]) -> list[ConsumptionEstimate]:
    rates: dict[str, ConsumptionEstimate] = {}
    for estimate in estimate_production(observation):
        recipe = recipe_for_product(estimate.item)
        if recipe is None:
            continue
        product_count = float(recipe.products.get(estimate.item) or 1.0)
        crafts_per_minute = estimate.per_minute / max(product_count, 0.001)
        for ingredient, amount in recipe.ingredients.items():
            _add_consumption(
                rates,
                ConsumptionEstimate(
                    item=ingredient,
                    per_minute=round(crafts_per_minute * amount, 3),
                    consumers=estimate.producers,
                    confidence=estimate.confidence,
                    notes=[f"inferred from {estimate.item} production"],
                ),
            )
    return sorted(rates.values(), key=lambda item: (-item.per_minute, item.item))


def estimate_net_rates(
    production: list[ProductionEstimate],
    consumption: list[ConsumptionEstimate],
) -> dict[str, float]:
    output: dict[str, float] = {}
    for item in production:
        output[item.item] = output.get(item.item, 0.0) + item.per_minute
    for item in consumption:
        output[item.item] = output.get(item.item, 0.0) - item.per_minute
    return {key: round(value, 3) for key, value in sorted(output.items())}


def production_target_status(
    production_targets: dict[str, float],
    production: list[ProductionEstimate],
) -> dict[str, Any]:
    rate_by_item = {item.item: item.per_minute for item in production}
    rows = []
    all_satisfied = bool(production_targets)
    for item, target in sorted(production_targets.items()):
        estimated = float(rate_by_item.get(item) or 0.0)
        satisfied = estimated >= target
        all_satisfied = all_satisfied and satisfied
        rows.append(
            {
                "item": item,
                "target_per_minute": target,
                "estimated_per_minute": round(estimated, 3),
                "deficit_per_minute": round(max(0.0, target - estimated), 3),
                "satisfied": satisfied,
            }
        )
    return {
        "all_satisfied": all_satisfied,
        "items": rows,
    }


def estimate_bottlenecks(
    objective: str,
    observation: dict[str, Any],
    production: list[ProductionEstimate] | None = None,
    production_targets: dict[str, float] | None = None,
) -> list[BottleneckEstimate]:
    production = production or estimate_production(observation)
    production_targets = production_targets or {}
    rate_by_item = {item.item: item.per_minute for item in production}
    required = required_items_for_objective(objective, max_depth=5)
    dependents = _dependents(required)
    bottlenecks: list[BottleneckEstimate] = []
    for item, target in sorted(production_targets.items()):
        rate = float(rate_by_item.get(item) or 0.0)
        if target > 0 and rate < target:
            stock = total_item_count(observation, item)
            deficit = round(target - rate, 3)
            bottlenecks.append(
                BottleneckEstimate(
                    item=item,
                    reason=f"target deficit: needs {target}/min, estimated {rate}/min",
                    stock=stock,
                    estimated_per_minute=rate,
                    required_by=dependents.get(item, []),
                    severity=100 + min(50, int(50 * deficit / max(target, 0.001))),
                )
            )
    for item in sorted(required):
        stock = total_item_count(observation, item)
        rate = float(rate_by_item.get(item) or 0.0)
        if item in RAW_RESOURCES and stock == 0 and rate == 0:
            continue
        if stock <= 0 and rate <= 0:
            bottlenecks.append(
                BottleneckEstimate(
                    item=item,
                    reason="no stock and no observed producer",
                    stock=stock,
                    estimated_per_minute=rate,
                    required_by=dependents.get(item, []),
                    severity=100,
                )
            )
        elif stock < 10 and item not in RAW_RESOURCES and rate <= 0:
            bottlenecks.append(
                BottleneckEstimate(
                    item=item,
                    reason="low stock and no observed producer",
                    stock=stock,
                    estimated_per_minute=rate,
                    required_by=dependents.get(item, []),
                    severity=80,
                )
            )
    return sorted(bottlenecks, key=lambda item: (-item.severity, item.item))


def technology_requirements_for_objective(objective: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for item in sorted(required_items_for_objective(objective, max_depth=5)):
        recipe = recipe_for_product(item)
        if recipe is None:
            continue
        for tech in technology_chain_for_recipe(recipe.name):
            name = str(tech.get("name") or "")
            if name and name not in seen:
                seen.add(name)
                output.append(tech)
    return output


def _estimate_furnace(entity: dict[str, Any], speed: float) -> ProductionEstimate | None:
    name = str(entity.get("name") or "")
    if name in {"stone-furnace", "steel-furnace"} and entity_item_count(entity, "coal") <= 0:
        return None
    if name == "electric-furnace" and entity.get("electric_network_connected") is False:
        return None
    product = None
    if entity_item_count(entity, "iron-ore") > 0 or entity_item_count(entity, "iron-plate") > 0:
        product = "iron-plate"
    elif entity_item_count(entity, "copper-ore") > 0 or entity_item_count(entity, "copper-plate") > 0:
        product = "copper-plate"
    elif entity_item_count(entity, "stone") > 0 or entity_item_count(entity, "stone-brick") > 0:
        product = "stone-brick"
    if product is None:
        return None
    recipe = RECIPES.get(product)
    if recipe is None:
        return None
    count = float(recipe.products.get(product) or 1.0)
    per_minute = 60.0 * speed * count / recipe.time_seconds
    return ProductionEstimate(
        item=product,
        per_minute=round(per_minute, 3),
        producers=1,
        confidence=0.65,
        notes=[f"inferred from {entity.get('name')} inventories"],
    )


def _estimate_miner(
    entity: dict[str, Any],
    observation: dict[str, Any],
    per_minute: float,
) -> ProductionEstimate | None:
    position = entity.get("position") if isinstance(entity.get("position"), dict) else {}
    if not position:
        return None
    resource = _nearest_resource(observation, {"x": float(position.get("x") or 0), "y": float(position.get("y") or 0)})
    if resource is None:
        return None
    return ProductionEstimate(
        item=str(resource.get("name")),
        per_minute=round(per_minute, 3),
        producers=1,
        confidence=0.55,
        notes=[f"inferred from {entity.get('name')} near resource patch"],
    )


def _estimate_assembler(entity: dict[str, Any], crafting_speed: float) -> ProductionEstimate | None:
    recipe_name = entity.get("recipe")
    if not isinstance(recipe_name, str) or not recipe_name:
        return None
    recipe = RECIPES.get(recipe_name)
    if recipe is None:
        return None
    if entity.get("electric_network_connected") is False:
        return None
    estimates = []
    for product, count in recipe.products.items():
        per_minute = 60.0 * crafting_speed * float(count) / max(recipe.time_seconds, 0.001)
        estimates.append(
            ProductionEstimate(
                item=product,
                per_minute=round(per_minute, 3),
                producers=1,
                confidence=0.6,
                notes=[f"inferred from {entity.get('name')} recipe {recipe_name}"],
            )
        )
    return estimates[0] if estimates else None


def _add_estimate(rates: dict[str, ProductionEstimate], estimate: ProductionEstimate | None) -> None:
    if estimate is None:
        return
    current = rates.get(estimate.item)
    if current is None:
        rates[estimate.item] = estimate
        return
    rates[estimate.item] = ProductionEstimate(
        item=estimate.item,
        per_minute=round(current.per_minute + estimate.per_minute, 3),
        producers=current.producers + estimate.producers,
        confidence=round(min(current.confidence, estimate.confidence), 3),
        notes=sorted(set(current.notes + estimate.notes)),
    )


def _add_consumption(rates: dict[str, ConsumptionEstimate], estimate: ConsumptionEstimate) -> None:
    current = rates.get(estimate.item)
    if current is None:
        rates[estimate.item] = estimate
        return
    rates[estimate.item] = ConsumptionEstimate(
        item=estimate.item,
        per_minute=round(current.per_minute + estimate.per_minute, 3),
        consumers=current.consumers + estimate.consumers,
        confidence=round(min(current.confidence, estimate.confidence), 3),
        notes=sorted(set(current.notes + estimate.notes)),
    )


def _entities(observation: dict[str, Any]) -> list[dict[str, Any]]:
    entities = observation.get("entities")
    if not isinstance(entities, list):
        return []
    return [item for item in entities if isinstance(item, dict)]


def _nearest_resource(observation: dict[str, Any], position: dict[str, float]) -> dict[str, Any] | None:
    resources = observation.get("resources")
    if not isinstance(resources, list):
        return None
    candidates = [item for item in resources if isinstance(item, dict) and isinstance(item.get("position"), dict)]
    if not candidates:
        return None
    nearest = min(candidates, key=lambda item: distance(position, item["position"]))
    if distance(position, nearest["position"]) > 3.0:
        return None
    return nearest


def _complete_belt_line_count(observation: dict[str, Any], resource_name: str) -> int:
    furnace_positions: set[tuple[float, float]] = set()
    for belt in [item for item in _entities(observation) if item.get("name") == "transport-belt"]:
        for layout in _belt_line_layouts_from_anchor(observation, belt):
            if layout["resource_name"] != resource_name:
                continue
            if all(layout.get(key) is not None for key in ("belt1", "belt2", "inserter", "furnace", "drill")) and _belt_line_fueled(layout):
                furnace_position = _position(layout["furnace"])
                furnace_positions.add((round(furnace_position["x"], 2), round(furnace_position["y"], 2)))
    return len(furnace_positions)


def _belt_line_layouts_from_anchor(observation: dict[str, Any], belt: dict[str, Any]) -> list[dict[str, Any]]:
    belt_position = _position(belt)
    output = []
    for orientation in ("east", "west", "south", "north"):
        dx, dy, belt_direction = _smelting_orientation(orientation)
        if not _entity_direction_matches(belt, belt_direction):
            continue
        drill_position = {"x": belt_position["x"] - 2 * dx, "y": belt_position["y"] - 2 * dy}
        resource = _nearest_resource(observation, drill_position)
        output.append(
            {
                "resource_name": str(resource.get("name")) if resource is not None and resource.get("name") else "iron-ore",
                "belt1": belt,
                "belt2": _entity_near(
                    observation,
                    "transport-belt",
                    {"x": belt_position["x"] + dx, "y": belt_position["y"] + dy},
                    0.75,
                ),
                "inserter": _entity_near(
                    observation,
                    "burner-inserter",
                    {"x": belt_position["x"] + 2 * dx, "y": belt_position["y"] + 2 * dy},
                    1.0,
                ),
                "furnace": _entity_near(
                    observation,
                    "stone-furnace",
                    {"x": belt_position["x"] + 3 * dx, "y": belt_position["y"] + 3 * dy},
                    1.75,
                ),
                "drill": _entity_near(observation, "burner-mining-drill", drill_position, 2.0),
            }
        )
    return output


def _smelting_orientation(orientation: str) -> tuple[int, int, int]:
    if orientation == "west":
        return -1, 0, WEST
    if orientation == "south":
        return 0, 1, SOUTH
    if orientation == "north":
        return 0, -1, NORTH
    return 1, 0, EAST


def _entity_direction_matches(entity: dict[str, Any], expected: int) -> bool:
    if "direction" not in entity:
        return True
    try:
        return int(entity.get("direction")) == expected
    except (TypeError, ValueError):
        return True


def _belt_line_fueled(layout: dict[str, Any]) -> bool:
    for key in ("drill", "inserter", "furnace"):
        entity = layout.get(key)
        if not isinstance(entity, dict) or entity_item_count(entity, "coal") < 1:
            return False
    return True


def _entity_near(observation: dict[str, Any], name: str, position: dict[str, float], radius: float) -> dict[str, Any] | None:
    candidates = [
        item
        for item in _entities(observation)
        if item.get("name") == name and distance(position, _position(item)) <= radius
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda item: distance(position, _position(item)))


def _position(entity: dict[str, Any]) -> dict[str, float]:
    raw = entity.get("position") if isinstance(entity.get("position"), dict) else {}
    return {"x": float(raw.get("x") or 0.0), "y": float(raw.get("y") or 0.0)}


def _dependents(required: set[str]) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {}
    for recipe in RECIPES.values():
        for ingredient in recipe.ingredients:
            if ingredient in required:
                output.setdefault(ingredient, []).extend(recipe.products.keys())
    return {key: sorted(set(value)) for key, value in output.items()}
