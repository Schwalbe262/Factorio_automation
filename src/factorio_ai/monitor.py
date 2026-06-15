from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from .blueprints import encode_blueprint_entities
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
    usable_per_minute: float | None = None

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


@dataclass(frozen=True)
class FactorySiteEstimate:
    site_id: str
    kind: str
    status: str
    position: dict[str, float]
    item: str | None
    machines: list[str]
    automation_level: str
    notes: list[str]
    blueprint: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LogisticsLinkEstimate:
    link_id: str
    kind: str
    item: str | None
    from_site: str
    to_site: str
    status: str
    length_tiles: float
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ThroughputConstraint:
    item: str
    required_per_minute: float
    available_per_minute: float
    bottleneck: str
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PowerNetworkEstimate:
    network_id: str
    generation_kw: float
    demand_kw: float
    satisfaction: float
    status: str
    producers: int
    consumers: int
    unconnected_consumers: int
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ThreatEstimate:
    danger_level: str
    enemy_count: int
    counts_by_type: dict[str, int]
    counts_by_name: dict[str, int]
    nearest_enemy: dict[str, Any] | None
    nearest_spawner: dict[str, Any] | None
    nearest_turret: dict[str, Any] | None
    armed_gun_turret_count: int
    unarmed_gun_turret_count: int
    recent_damage_count: int
    recent_destroyed_count: int
    max_spawner_pollution: float
    recommended_actions: list[str]

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
    "transport-belt",
    "inserter",
    "burner-inserter",
    "burner-mining-drill",
    "stone-furnace",
    "small-electric-pole",
    "assembling-machine-1",
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
BELT_ITEMS_PER_SECOND = {
    "transport-belt": 15.0,
    "fast-transport-belt": 30.0,
    "express-transport-belt": 45.0,
}
INSERTER_ITEMS_PER_SECOND = {
    "burner-inserter": 0.6,
    "inserter": 0.83,
    "fast-inserter": 2.31,
    "stack-inserter": 4.62,
}
ELECTRIC_GENERATION_KW = {
    "steam-engine": 900.0,
    "solar-panel": 60.0,
}
ELECTRIC_DEMAND_KW = {
    "assembling-machine-1": 75.0,
    "assembling-machine-2": 150.0,
    "assembling-machine-3": 375.0,
    "electric-mining-drill": 90.0,
    "electric-furnace": 180.0,
    "lab": 60.0,
    "inserter": 13.0,
    "fast-inserter": 46.0,
    "long-handed-inserter": 18.0,
    "pumpjack": 90.0,
    "oil-refinery": 420.0,
    "chemical-plant": 210.0,
    "radar": 300.0,
    "beacon": 480.0,
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
    factory_sites = estimate_factory_sites(observation)
    logistics_links = estimate_logistics_links(observation)
    factory_events = recent_factory_events(observation)
    damage_events = recent_damage_events(observation)
    threats = estimate_threats(observation)
    power_networks = estimate_power_networks(observation)
    throughput_constraints = estimate_throughput_constraints(observation)
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
        "factory_sites": [item.to_dict() for item in factory_sites],
        "logistics_links": [item.to_dict() for item in logistics_links],
        "factory_events": factory_events,
        "damage_events": damage_events,
        "threats": threats.to_dict(),
        "power_networks": [item.to_dict() for item in power_networks],
        "throughput_constraints": [item.to_dict() for item in throughput_constraints],
    }


def inventory_summary(observation: dict[str, Any], objective: str) -> dict[str, int]:
    required = required_items_for_objective(objective, max_depth=5)
    items = sorted(set(COMMON_ITEMS) | required)
    return {item: total_item_count(observation, item) for item in items if total_item_count(observation, item) > 0}


def estimate_production(observation: dict[str, Any]) -> list[ProductionEstimate]:
    rates: dict[str, ProductionEstimate] = {}
    busy_plate_furnaces = {"iron-plate": 0, "copper-plate": 0}
    usable_busy_plate_furnaces = {"iron-plate": 0, "copper-plate": 0}
    for entity in _entities(observation):
        name = str(entity.get("name") or "")
        if name in FURNACE_SPEEDS:
            estimate = _estimate_furnace(observation, entity, FURNACE_SPEEDS[name])
            if estimate and estimate.item in busy_plate_furnaces:
                busy_plate_furnaces[estimate.item] += 1
                if _usable_rate(estimate) > 0:
                    usable_busy_plate_furnaces[estimate.item] += 1
            _add_estimate(rates, estimate)
        elif name in MINER_RATES_PER_MINUTE:
            _add_estimate(rates, _estimate_miner(entity, observation, MINER_RATES_PER_MINUTE[name]))
        elif name in ASSEMBLER_SPEEDS:
            _add_estimate(rates, _estimate_assembler(observation, entity, ASSEMBLER_SPEEDS[name]))
    for product, resource in [("iron-plate", "iron-ore"), ("copper-plate", "copper-ore")]:
        complete_lines = _complete_belt_line_count(observation, resource)
        usable_complete_lines = _complete_belt_line_count(observation, resource, starter_usable_only=True)
        extra_lines = max(0, complete_lines - busy_plate_furnaces[product])
        usable_extra_lines = max(0, usable_complete_lines - usable_busy_plate_furnaces[product])
        if extra_lines:
            _add_estimate(
                rates,
                ProductionEstimate(
                    item=product,
                    per_minute=round(extra_lines * 18.75, 3),
                    producers=extra_lines,
                    confidence=0.5,
                    notes=_starter_usability_notes(
                        complete_lines,
                        usable_complete_lines,
                        f"inferred from complete burner {resource} belt smelting lines",
                    ),
                    usable_per_minute=round(usable_extra_lines * 18.75, 3),
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


def machine_output_per_minute(recipe_name: str, machine_name: str, product: str | None = None) -> float:
    recipe = RECIPES.get(recipe_name)
    speed = ASSEMBLER_SPEEDS.get(machine_name) or FURNACE_SPEEDS.get(machine_name)
    if recipe is None or speed is None:
        return 0.0
    product_name = product or recipe_name
    count = float(recipe.products.get(product_name) or 0.0)
    if count <= 0:
        return 0.0
    return round(60.0 * speed * count / max(recipe.time_seconds, 0.001), 3)


def recipe_machine_ratio(
    producer_recipe: str,
    consumer_recipe: str,
    item: str,
    machine_name: str = "assembling-machine-1",
) -> dict[str, float]:
    producer_rate = machine_output_per_minute(producer_recipe, machine_name, item)
    consumer_recipe_data = RECIPES.get(consumer_recipe)
    consumer_speed = ASSEMBLER_SPEEDS.get(machine_name)
    if producer_rate <= 0 or consumer_recipe_data is None or consumer_speed is None:
        return {"producer": 0.0, "consumer": 0.0}
    ingredient_count = float(consumer_recipe_data.ingredients.get(item) or 0.0)
    crafts_per_minute = 60.0 * consumer_speed / max(consumer_recipe_data.time_seconds, 0.001)
    consumer_rate = crafts_per_minute * ingredient_count
    # Minimal whole-machine ratio after scaling by each side's per-machine flow.
    producer_units = consumer_rate
    consumer_units = producer_rate
    if producer_units <= 0 or consumer_units <= 0:
        return {"producer": 0.0, "consumer": 0.0}
    scale = _gcd_float(producer_units, consumer_units)
    return {
        "producer": round(producer_units / scale, 3),
        "consumer": round(consumer_units / scale, 3),
        "producer_per_minute": round(producer_rate, 3),
        "consumer_demand_per_minute": round(consumer_rate, 3),
    }


def estimate_throughput_constraints(observation: dict[str, Any]) -> list[ThroughputConstraint]:
    constraints: list[ThroughputConstraint] = []
    cable_assemblers = [
        item
        for item in _entities(observation)
        if item.get("name") in ASSEMBLER_SPEEDS
        and item.get("recipe") == "copper-cable"
        and item.get("electric_network_connected") is not False
    ]
    circuit_assemblers = [
        item
        for item in _entities(observation)
        if item.get("name") in ASSEMBLER_SPEEDS
        and item.get("recipe") == "electronic-circuit"
        and item.get("electric_network_connected") is not False
    ]
    if cable_assemblers or circuit_assemblers:
        cable_output = sum(machine_output_per_minute("copper-cable", str(item.get("name") or ""), "copper-cable") for item in cable_assemblers)
        cable_required = 0.0
        for item in circuit_assemblers:
            speed = ASSEMBLER_SPEEDS.get(str(item.get("name") or "")) or 0.0
            recipe = RECIPES["electronic-circuit"]
            cable_required += 60.0 * speed * float(recipe.ingredients["copper-cable"]) / recipe.time_seconds
        ratio = recipe_machine_ratio("copper-cable", "electronic-circuit", "copper-cable")
        bottleneck = "ok" if cable_output >= cable_required else "copper-cable assembler ratio"
        constraints.append(
            ThroughputConstraint(
                item="copper-cable",
                required_per_minute=round(cable_required, 3),
                available_per_minute=round(cable_output, 3),
                bottleneck=bottleneck,
                notes=[
                    "electronic-circuit balance from recipe time and assembler speed",
                    f"assembling-machine-1 copper-cable:electronic-circuit ratio is {ratio['producer']:.0f}:{ratio['consumer']:.0f}",
                ],
            )
        )

    belt_counts = Counter(str(entity.get("name") or "") for entity in _entities(observation) if entity.get("name") in BELT_ITEMS_PER_SECOND)
    for belt_name, count in sorted(belt_counts.items()):
        constraints.append(
            ThroughputConstraint(
                item=belt_name,
                required_per_minute=0.0,
                available_per_minute=round(BELT_ITEMS_PER_SECOND[belt_name] * 60.0 * count, 3),
                bottleneck="belt capacity tracked",
                notes=[f"{belt_name} carries {BELT_ITEMS_PER_SECOND[belt_name]} items/s per lane pair before split/side limits"],
            )
        )

    inserter_counts = Counter(str(entity.get("name") or "") for entity in _entities(observation) if entity.get("name") in INSERTER_ITEMS_PER_SECOND)
    for inserter_name, count in sorted(inserter_counts.items()):
        constraints.append(
            ThroughputConstraint(
                item=inserter_name,
                required_per_minute=0.0,
                available_per_minute=round(INSERTER_ITEMS_PER_SECOND[inserter_name] * 60.0 * count, 3),
                bottleneck="inserter transfer tracked",
                notes=[f"{inserter_name} rough transfer estimate is {INSERTER_ITEMS_PER_SECOND[inserter_name]} items/s before stack bonuses"],
            )
        )
    for network in estimate_power_networks(observation):
        if network.status in {"ok", "unknown_generation"}:
            continue
        constraints.append(
            ThroughputConstraint(
                item="electricity",
                required_per_minute=network.demand_kw,
                available_per_minute=network.generation_kw,
                bottleneck=f"power network {network.network_id}: {network.status}",
                notes=network.notes[:3],
            )
        )
    return constraints


def estimate_power_networks(observation: dict[str, Any]) -> list[PowerNetworkEstimate]:
    networks: dict[str, dict[str, Any]] = {}
    unconnected: list[str] = []
    for entity in _entities(observation):
        name = str(entity.get("name") or "")
        generation = ELECTRIC_GENERATION_KW.get(name, 0.0)
        demand = ELECTRIC_DEMAND_KW.get(name, 0.0)
        if generation <= 0 and demand <= 0:
            continue
        connected = entity.get("electric_network_connected")
        network_id = _electric_network_key(entity)
        if demand > 0 and connected is False:
            unconnected.append(name)
            continue
        if generation > 0 and connected is False:
            continue
        row = networks.setdefault(
            network_id,
            {
                "generation_kw": 0.0,
                "demand_kw": 0.0,
                "producers": 0,
                "consumers": 0,
                "notes": [],
            },
        )
        if generation > 0:
            row["generation_kw"] += generation
            row["producers"] += 1
        if demand > 0:
            row["demand_kw"] += demand
            row["consumers"] += 1
    estimates: list[PowerNetworkEstimate] = []
    for network_id, row in sorted(networks.items()):
        generation = round(float(row["generation_kw"]), 3)
        demand = round(float(row["demand_kw"]), 3)
        satisfaction = 1.0 if demand <= 0 else min(1.0, generation / max(demand, 0.001))
        status = "ok"
        notes = [
            "power is shared only inside one connected electric network",
            "if demand exceeds generation, electric machines throttle down together on that network",
        ]
        if demand > 0 and generation <= 0:
            status = "unknown_generation"
            notes.append("no generator was observed in the scan; this does not prove the network has no power source")
        elif demand > generation:
            status = "insufficient_generation"
        estimates.append(
            PowerNetworkEstimate(
                network_id=network_id,
                generation_kw=generation,
                demand_kw=demand,
                satisfaction=round(satisfaction, 3),
                status=status,
                producers=int(row["producers"]),
                consumers=int(row["consumers"]),
                unconnected_consumers=0,
                notes=notes,
            )
        )
    if unconnected:
        counts = Counter(unconnected)
        estimates.append(
            PowerNetworkEstimate(
                network_id="unconnected",
                generation_kw=0.0,
                demand_kw=round(sum(ELECTRIC_DEMAND_KW.get(name, 0.0) * count for name, count in counts.items()), 3),
                satisfaction=0.0,
                status="unconnected_consumers",
                producers=0,
                consumers=sum(counts.values()),
                unconnected_consumers=sum(counts.values()),
                notes=[
                    "electric consumers without a pole connection do not share power with the main grid",
                    "; ".join(f"{name} x{count}" for name, count in sorted(counts.items())),
                ],
            )
        )
    return estimates


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
    actual_rate_by_item = {item.item: item.per_minute for item in production}
    usable_rate_by_item = {item.item: _usable_rate(item) for item in production}
    rows = []
    all_satisfied = bool(production_targets)
    for item, target in sorted(production_targets.items()):
        actual = float(actual_rate_by_item.get(item) or 0.0)
        estimated = float(usable_rate_by_item.get(item) or 0.0)
        satisfied = estimated >= target
        all_satisfied = all_satisfied and satisfied
        rows.append(
            {
                "item": item,
                "target_per_minute": target,
                "estimated_per_minute": round(estimated, 3),
                "observed_per_minute": round(actual, 3),
                "isolated_per_minute": round(max(0.0, actual - estimated), 3),
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
    actual_rate_by_item = {item.item: item.per_minute for item in production}
    rate_by_item = {item.item: _usable_rate(item) for item in production}
    required = required_items_for_objective(objective, max_depth=5)
    dependents = _dependents(required)
    bottlenecks: list[BottleneckEstimate] = []
    for item, target in sorted(production_targets.items()):
        rate = float(rate_by_item.get(item) or 0.0)
        actual_rate = float(actual_rate_by_item.get(item) or 0.0)
        if target > 0 and rate < target:
            stock = total_item_count(observation, item)
            deficit = round(target - rate, 3)
            reason = f"target deficit: needs {target}/min, starter-usable estimated {rate}/min"
            if actual_rate > rate:
                reason += f" ({actual_rate}/min observed but isolated or remote)"
            bottlenecks.append(
                BottleneckEstimate(
                    item=item,
                    reason=reason,
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


def estimate_factory_sites(observation: dict[str, Any]) -> list[FactorySiteEstimate]:
    sites: list[FactorySiteEstimate] = []
    seen_site_ids: set[str] = set()

    for boiler in [item for item in _entities(observation) if item.get("name") == "boiler"]:
        position = _position(boiler)
        fuel_link = _fuel_feed_type(observation, position)
        site = FactorySiteEstimate(
            site_id=_entity_site_id("power", boiler),
            kind="steam_power",
            status="automated_fuel" if fuel_link == "belt" else "manual_fuel",
            position=position,
            item="electricity",
            machines=_nearby_machine_names(observation, position, {"offshore-pump", "boiler", "steam-engine", "small-electric-pole"}, 8.0),
            automation_level="bootstrap" if fuel_link == "manual" else "belt-fed",
            notes=[
                "boiler fuel should be upgraded from manual coal inserts to belt/inserter feed"
                if fuel_link == "manual"
                else "coal feed belt/inserter observed near boiler"
            ]
            + _entity_modification_notes(boiler),
        )
        sites.append(site)
        seen_site_ids.add(site.site_id)

    for layout in _deduped_belt_line_layouts(observation):
        furnace = layout.get("furnace")
        position = _position(furnace) if isinstance(furnace, dict) else _layout_center(layout)
        resource_name = str(layout.get("resource_name") or "iron-ore")
        product = _product_for_resource(resource_name)
        complete = all(layout.get(key) is not None for key in ("belt1", "belt2", "inserter", "furnace", "drill"))
        fueled = _belt_line_fueled(layout) if complete else False
        site = FactorySiteEstimate(
            site_id=_position_site_id("smelting", position),
            kind="plate_smelting_line",
            status="running" if fueled else ("built_unfueled" if complete else "incomplete"),
            position=position,
            item=product,
            machines=[name for key in ("drill", "belt1", "belt2", "inserter", "furnace") if (name := _machine_name(layout.get(key)))],
            automation_level="burner-bootstrap",
            notes=[
                "burner mining drill is an early bootstrap layout; replace with electric mining drills after power",
                f"{resource_name} is moved by a short belt/inserter chain into a furnace",
            ]
            + _layout_modification_notes(layout),
        )
        if site.site_id not in seen_site_ids:
            sites.append(site)
            seen_site_ids.add(site.site_id)

    for assembler in [
        item
        for item in _entities(observation)
        if str(item.get("name") or "") in ASSEMBLER_SPEEDS
    ]:
        recipe = assembler.get("recipe")
        position = _position(assembler)
        kind = "assembler_cell"
        automation_level = "powered" if assembler.get("electric_network_connected") else "unpowered"
        if recipe in {
            "transport-belt",
            "inserter",
            "burner-inserter",
            "burner-mining-drill",
            "electric-mining-drill",
            "stone-furnace",
            "assembling-machine-1",
            "small-electric-pole",
        }:
            kind = "build_item_mall"
        elif recipe in {"copper-cable", "electronic-circuit"}:
            kind = "circuit_automation"
        site = FactorySiteEstimate(
            site_id=_entity_site_id(kind, assembler),
            kind=kind,
            status="running" if recipe and assembler.get("electric_network_connected") else ("unconfigured" if not recipe else "unpowered"),
            position=position,
            item=str(recipe) if isinstance(recipe, str) and recipe else None,
            machines=[str(assembler.get("name") or "assembling-machine")],
            automation_level=automation_level,
            notes=[f"assembler recipe: {recipe or 'unset'}"] + _entity_modification_notes(assembler),
        )
        sites.append(site)

    labs = [item for item in _entities(observation) if item.get("name") == "lab"]
    lab_chain_links = _lab_chain_link_count(observation)
    for lab in labs:
        powered = lab.get("electric_network_connected") is not False
        science_packs = sum(entity_item_count(lab, item) for item in COMMON_ITEMS if item.endswith("-science-pack"))
        site = FactorySiteEstimate(
            site_id=_entity_site_id("research_lab", lab),
            kind="research_lab_block",
            status="researching" if powered and science_packs > 0 else ("needs_science" if powered else "unpowered"),
            position=_position(lab),
            item="research",
            machines=["lab"],
            automation_level="daisy-chain" if lab_chain_links > 0 else "manual-feed",
            notes=[
                "labs can pass science packs to other labs through inserter daisy chains",
                "keep early daisy chains short or split them into multiple feed points to avoid starving tail labs",
            ]
            + _entity_modification_notes(lab),
        )
        sites.append(site)

    for drill in [
        item
        for item in _entities(observation)
        if str(item.get("name") or "") in MINER_RATES_PER_MINUTE
    ]:
        position = _position(drill)
        resource = _nearest_resource(observation, position)
        resource_name = _entity_mining_target_name(drill) or (
            str(resource.get("name")) if resource and resource.get("name") else None
        )
        name = str(drill.get("name") or "")
        electric = name == "electric-mining-drill"
        fueled = electric or entity_item_count(drill, "coal") > 0 or not _entity_status_is(drill, "no_fuel", 53)
        site = FactorySiteEstimate(
            site_id=_entity_site_id("mining_patch", drill),
            kind="mining_patch",
            status="powered" if electric and drill.get("electric_network_connected") else ("fueled" if fueled else "needs_fuel"),
            position=position,
            item=resource_name,
            machines=[name],
            automation_level="electric" if electric else "burner-bootstrap",
            notes=[
                "electric mining drill is the preferred post-power mining layer"
                if electric
                else "burner mining drill should be replaced by electric mining drill after power and green circuits stabilize"
            ]
            + _entity_modification_notes(drill),
        )
        sites.append(site)

    return _attach_site_blueprints(observation, _group_factory_sites(sites))


def recent_factory_events(observation: dict[str, Any], limit: int = 40) -> list[dict[str, Any]]:
    raw = observation.get("factory_events")
    if not isinstance(raw, list):
        return []
    output: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        actor = item.get("actor") if isinstance(item.get("actor"), dict) else {}
        output.append(
            {
                "tick": item.get("tick"),
                "action": item.get("action"),
                "actor": actor.get("name") or actor.get("kind"),
                "actor_kind": actor.get("kind"),
                "entity": item.get("entity"),
                "unit_number": item.get("unit_number"),
                "cause": item.get("cause"),
                "cause_force": item.get("cause_force"),
                "damage": item.get("damage"),
                "damage_type": item.get("damage_type"),
                "health": item.get("health"),
                "position": item.get("position") if isinstance(item.get("position"), dict) else {},
                "distance": item.get("distance"),
            }
        )
        if len(output) >= limit:
            break
    return output


def recent_damage_events(observation: dict[str, Any], limit: int = 40) -> list[dict[str, Any]]:
    damage_actions = {"damaged", "destroyed"}
    return [item for item in recent_factory_events(observation, limit=limit * 2) if item.get("action") in damage_actions][:limit]


def estimate_threats(observation: dict[str, Any]) -> ThreatEstimate:
    enemies = _enemies(observation)
    nearest_enemy = _nearest_enemy(enemies)
    spawners = [item for item in enemies if item.get("type") == "unit-spawner"]
    turrets = [item for item in enemies if item.get("type") == "turret"]
    nearest_spawner = _nearest_enemy(spawners)
    nearest_turret = _nearest_enemy(turrets)
    damage_events = recent_damage_events(observation, limit=20)
    recent_destroyed_count = sum(1 for item in damage_events if item.get("action") == "destroyed")
    counts_by_type = Counter(str(item.get("type") or "unknown") for item in enemies)
    counts_by_name = Counter(str(item.get("name") or "unknown") for item in enemies)
    gun_turrets = [item for item in _entities(observation) if item.get("name") == "gun-turret"]
    armed_gun_turrets = [item for item in gun_turrets if entity_item_count(item, "firearm-magazine") > 0]
    max_spawner_pollution = max([_safe_float(item.get("pollution")) for item in spawners] or [0.0])
    danger_level = _threat_danger_level(nearest_enemy, damage_events, max_spawner_pollution)
    recommended_actions = _threat_recommendations(
        danger_level=danger_level,
        recent_destroyed_count=recent_destroyed_count,
        armed_gun_turret_count=len(armed_gun_turrets),
        spawner_count=len(spawners),
        max_spawner_pollution=max_spawner_pollution,
    )
    return ThreatEstimate(
        danger_level=danger_level,
        enemy_count=len(enemies),
        counts_by_type=dict(sorted(counts_by_type.items())),
        counts_by_name=dict(sorted(counts_by_name.items())),
        nearest_enemy=nearest_enemy,
        nearest_spawner=nearest_spawner,
        nearest_turret=nearest_turret,
        armed_gun_turret_count=len(armed_gun_turrets),
        unarmed_gun_turret_count=max(0, len(gun_turrets) - len(armed_gun_turrets)),
        recent_damage_count=len(damage_events),
        recent_destroyed_count=recent_destroyed_count,
        max_spawner_pollution=round(max_spawner_pollution, 3),
        recommended_actions=recommended_actions,
    )


def _threat_danger_level(
    nearest_enemy: dict[str, Any] | None,
    damage_events: list[dict[str, Any]],
    max_spawner_pollution: float,
) -> str:
    if any(item.get("action") == "destroyed" for item in damage_events):
        return "critical"
    if damage_events:
        return "high"
    if max_spawner_pollution > 0:
        return "high"
    nearest_distance = _enemy_distance(nearest_enemy)
    nearest_type = str((nearest_enemy or {}).get("type") or "")
    if nearest_distance is not None and nearest_distance <= 32:
        return "critical"
    if nearest_type == "unit" and nearest_distance is not None and nearest_distance <= 64:
        return "high"
    if nearest_distance is not None and nearest_distance <= 128:
        return "medium"
    if nearest_enemy:
        return "low"
    return "none"


def _threat_recommendations(
    *,
    danger_level: str,
    recent_destroyed_count: int,
    armed_gun_turret_count: int,
    spawner_count: int,
    max_spawner_pollution: float,
) -> list[str]:
    recommendations: list[str] = []
    if danger_level in {"critical", "high"} and armed_gun_turret_count <= 0:
        recommendations.append("run build_starter_defense to place armed gun turrets around factory sites before expanding")
    if recent_destroyed_count > 0:
        recommendations.append("queue factory repair/rebuild for destroyed entities")
    if spawner_count > 0 and max_spawner_pollution > 0:
        recommendations.append("pollution is reaching enemy spawners; plan turret/wall coverage and reduce undefended expansion")
    if danger_level in {"critical", "high", "medium"}:
        recommendations.append("route walking and future rails/belts around enemy threat radii")
    return recommendations


def _layout_modification_notes(layout: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    for key in ("drill", "belt1", "belt2", "inserter", "furnace"):
        entity = layout.get(key)
        if isinstance(entity, dict):
            notes.extend(_entity_modification_notes(entity))
    return sorted(set(notes))


def _lab_chain_link_count(observation: dict[str, Any]) -> int:
    labs = [item for item in _entities(observation) if item.get("name") == "lab"]
    if len(labs) < 2:
        return 0
    inserters = [
        item
        for item in _entities(observation)
        if str(item.get("name") or "") in {"inserter", "fast-inserter", "long-handed-inserter", "stack-inserter"}
    ]
    links = 0
    for inserter in inserters:
        pos = _position(inserter)
        nearby_labs = [lab for lab in labs if distance(pos, _position(lab)) <= 4.5]
        if len(nearby_labs) >= 2:
            links += 1
    return links


def _entity_modification_notes(*entities: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    for entity in entities:
        modified = entity.get("last_modified") if isinstance(entity.get("last_modified"), dict) else None
        if not modified:
            continue
        actor = modified.get("actor") if isinstance(modified.get("actor"), dict) else {}
        actor_name = actor.get("name") or actor.get("kind") or "unknown"
        action = modified.get("action") or "modified"
        tick = modified.get("tick")
        notes.append(f"last {action} by {actor_name} at tick {tick}")
    return notes


def _group_factory_sites(sites: list[FactorySiteEstimate]) -> list[FactorySiteEstimate]:
    grouped_inputs: dict[tuple[str, str | None, str], list[FactorySiteEstimate]] = {}
    for site in sites:
        grouped_inputs.setdefault(_factory_site_group_key(site), []).append(site)

    output: list[FactorySiteEstimate] = []
    for candidates in grouped_inputs.values():
        visited: set[int] = set()
        for start_index in range(len(candidates)):
            if start_index in visited:
                continue
            stack = [start_index]
            cluster: list[FactorySiteEstimate] = []
            visited.add(start_index)
            while stack:
                index = stack.pop()
                site = candidates[index]
                cluster.append(site)
                for other_index, other in enumerate(candidates):
                    if other_index in visited:
                        continue
                    if _factory_sites_are_adjacent(site, other):
                        visited.add(other_index)
                        stack.append(other_index)
            output.append(_merge_factory_site_cluster(cluster))

    return sorted(output, key=lambda item: (item.kind, item.item or "", item.position["x"], item.position["y"]))


def _factory_site_group_key(site: FactorySiteEstimate) -> tuple[str, str | None, str]:
    item = site.item
    if site.kind in {"assembler_cell", "build_item_mall", "circuit_automation", "research_lab_block"}:
        item = None
    return (site.kind, item, site.automation_level)


def _factory_sites_are_adjacent(left: FactorySiteEstimate, right: FactorySiteEstimate) -> bool:
    threshold = max(_factory_site_group_radius(left), _factory_site_group_radius(right))
    return distance(left.position, right.position) <= threshold


def _factory_site_group_radius(site: FactorySiteEstimate) -> float:
    if site.kind in {"mining_patch", "plate_smelting_line"}:
        return 36.0
    if site.kind in {"assembler_cell", "build_item_mall", "circuit_automation"}:
        return 14.0
    if site.kind == "steam_power":
        return 18.0
    if site.kind == "research_lab_block":
        return 18.0
    return 16.0


def _merge_factory_site_cluster(cluster: list[FactorySiteEstimate]) -> FactorySiteEstimate:
    if not cluster:
        raise ValueError("cannot merge an empty factory site cluster")
    if len(cluster) == 1:
        return cluster[0]

    position = _centroid([site.position for site in cluster])
    items = sorted({item for site in cluster if (item := site.item)})
    automation_levels = Counter(site.automation_level for site in cluster)
    notes = sorted({note for site in cluster for note in site.notes})
    notes.insert(0, f"grouped {len(cluster)} adjacent site records")
    return FactorySiteEstimate(
        site_id=_position_site_id(f"{cluster[0].kind}:group:{items[0] if len(items) == 1 else 'mixed'}", position),
        kind=cluster[0].kind,
        status=_summarize_site_values(site.status for site in cluster),
        position=position,
        item=items[0] if len(items) == 1 else None,
        machines=_summarize_machine_counts(cluster),
        automation_level=automation_levels.most_common(1)[0][0] if len(automation_levels) == 1 else _summarize_site_values(automation_levels.elements()),
        notes=notes,
    )


def _attach_site_blueprints(
    observation: dict[str, Any],
    sites: list[FactorySiteEstimate],
) -> list[FactorySiteEstimate]:
    return [
        FactorySiteEstimate(
            site_id=site.site_id,
            kind=site.kind,
            status=site.status,
            position=site.position,
            item=site.item,
            machines=site.machines,
            automation_level=site.automation_level,
            notes=site.notes,
            blueprint=_site_blueprint_export(observation, site),
        )
        for site in sites
    ]


_SITE_BLUEPRINT_ENTITY_NAMES = {
    "assembling-machine-1",
    "assembling-machine-2",
    "assembling-machine-3",
    "beacon",
    "big-electric-pole",
    "boiler",
    "burner-inserter",
    "burner-mining-drill",
    "chemical-plant",
    "electric-furnace",
    "electric-mining-drill",
    "express-splitter",
    "express-transport-belt",
    "express-underground-belt",
    "fast-inserter",
    "fast-splitter",
    "fast-transport-belt",
    "fast-underground-belt",
    "gun-turret",
    "inserter",
    "iron-chest",
    "lab",
    "long-handed-inserter",
    "medium-electric-pole",
    "offshore-pump",
    "oil-refinery",
    "pipe",
    "pipe-to-ground",
    "pump",
    "pumpjack",
    "radar",
    "rocket-silo",
    "small-electric-pole",
    "solar-panel",
    "splitter",
    "steam-engine",
    "steel-chest",
    "steel-furnace",
    "stone-furnace",
    "substation",
    "transport-belt",
    "underground-belt",
    "wooden-chest",
}


def _site_blueprint_export(observation: dict[str, Any], site: FactorySiteEstimate) -> dict[str, Any] | None:
    entities = _site_blueprint_entities(observation, site)
    if not entities:
        return None
    label_item = site.item or "mixed"
    label = f"{site.kind}:{label_item}@{site.position['x']},{site.position['y']}"
    description = (
        "Exported from the Factorio AI factory monitor. "
        "This reconstructs the observed machine/site footprint for review or manual reuse."
    )
    exchange_string = encode_blueprint_entities(label, entities, description=description)
    return {
        "label": label,
        "format": "factorio-blueprint-string",
        "entity_count": len(entities),
        "exchange_string": exchange_string,
    }


def _site_blueprint_entities(observation: dict[str, Any], site: FactorySiteEstimate) -> list[dict[str, Any]]:
    center = site.position
    radius = _site_blueprint_radius(site)
    rows: list[dict[str, Any]] = []
    for entity in _entities(observation):
        name = str(entity.get("name") or "")
        if name not in _SITE_BLUEPRINT_ENTITY_NAMES:
            continue
        if not _site_blueprint_entity_allowed(site, entity):
            continue
        position = _position(entity)
        if distance(position, center) > radius:
            continue
        rows.append(_entity_to_relative_blueprint_row(entity, center))
    return sorted(rows, key=lambda item: (item["position"]["y"], item["position"]["x"], item["name"]))


def _site_blueprint_entity_allowed(site: FactorySiteEstimate, entity: dict[str, Any]) -> bool:
    name = str(entity.get("name") or "")
    recipe = str(entity.get("recipe") or "")
    logistics = {
        "transport-belt",
        "fast-transport-belt",
        "express-transport-belt",
        "underground-belt",
        "fast-underground-belt",
        "express-underground-belt",
        "splitter",
        "fast-splitter",
        "express-splitter",
        "inserter",
        "burner-inserter",
        "fast-inserter",
        "long-handed-inserter",
        "wooden-chest",
        "iron-chest",
        "steel-chest",
        "small-electric-pole",
        "medium-electric-pole",
        "big-electric-pole",
        "substation",
    }
    if site.kind == "steam_power":
        return name in {
            "offshore-pump",
            "boiler",
            "steam-engine",
            "pipe",
            "pipe-to-ground",
            "pump",
            "transport-belt",
            "fast-transport-belt",
            "express-transport-belt",
            "inserter",
            "burner-inserter",
            "fast-inserter",
            "small-electric-pole",
            "medium-electric-pole",
        }
    if site.kind == "plate_smelting_line":
        return name in logistics | {
            "burner-mining-drill",
            "electric-mining-drill",
            "big-mining-drill",
            "stone-furnace",
            "steel-furnace",
            "electric-furnace",
        }
    if site.kind == "mining_patch":
        return name in logistics | {"burner-mining-drill", "electric-mining-drill", "big-mining-drill"}
    if site.kind == "research_lab_block":
        return name in logistics | {"lab"}
    if site.kind == "circuit_automation":
        if name in logistics:
            return True
        return name in ASSEMBLER_SPEEDS and recipe in {"copper-cable", "electronic-circuit"}
    if site.kind == "build_item_mall":
        if name in logistics:
            return True
        return name in ASSEMBLER_SPEEDS and recipe in {
            "transport-belt",
            "inserter",
            "burner-inserter",
            "burner-mining-drill",
            "stone-furnace",
            "assembling-machine-1",
            "small-electric-pole",
        }
    if site.kind == "assembler_cell":
        return name in logistics or name in ASSEMBLER_SPEEDS
    return True


def _site_blueprint_radius(site: FactorySiteEstimate) -> float:
    if site.kind in {"mining_patch", "plate_smelting_line"}:
        return 42.0
    if site.kind == "steam_power":
        return 24.0
    if site.kind in {"build_item_mall", "assembler_cell", "circuit_automation", "research_lab_block"}:
        return 18.0
    return 18.0


def _entity_to_relative_blueprint_row(
    entity: dict[str, Any],
    center: dict[str, float],
) -> dict[str, Any]:
    position = _position(entity)
    row: dict[str, Any] = {
        "name": str(entity.get("name") or ""),
        "position": {
            "x": round(float(position.get("x") or 0.0) - float(center.get("x") or 0.0), 3),
            "y": round(float(position.get("y") or 0.0) - float(center.get("y") or 0.0), 3),
        },
    }
    if entity.get("direction") is not None:
        row["direction"] = int(entity.get("direction") or 0)
    if isinstance(entity.get("recipe"), str) and entity.get("recipe"):
        row["recipe"] = str(entity["recipe"])
    return row


def _centroid(positions: list[dict[str, float]]) -> dict[str, float]:
    if not positions:
        return {"x": 0.0, "y": 0.0}
    return {
        "x": round(sum(float(item.get("x") or 0.0) for item in positions) / len(positions), 2),
        "y": round(sum(float(item.get("y") or 0.0) for item in positions) / len(positions), 2),
    }


def _summarize_machine_counts(cluster: list[FactorySiteEstimate]) -> list[str]:
    counts = Counter(machine for site in cluster for machine in site.machines if machine)
    return [
        name if count == 1 else f"{name} x{count}"
        for name, count in sorted(counts.items())
    ]


def _summarize_site_values(values: Any) -> str:
    counts = Counter(str(value) for value in values if str(value))
    if not counts:
        return ""
    if len(counts) == 1:
        return next(iter(counts))
    return ", ".join(
        value if count == 1 else f"{value} x{count}"
        for value, count in sorted(counts.items())
    )


def _gcd_float(left: float, right: float) -> float:
    a = max(1, int(round(left * 1000)))
    b = max(1, int(round(right * 1000)))
    while b:
        a, b = b, a % b
    return max(a / 1000.0, 0.001)


def estimate_logistics_links(observation: dict[str, Any]) -> list[LogisticsLinkEstimate]:
    sites = estimate_factory_sites(observation)
    links: list[LogisticsLinkEstimate] = []
    seen: set[str] = set()
    sources_by_item: dict[str, list[FactorySiteEstimate]] = {}
    for site in sites:
        for item in _site_output_items(site):
            sources_by_item.setdefault(item, []).append(site)

    for consumer in sites:
        for required_item in _site_required_input_items(consumer):
            source = _nearest_source_site(required_item, consumer, sources_by_item)
            link = _site_logistics_link(observation, required_item, source, consumer)
            if link.link_id not in seen:
                links.append(link)
                seen.add(link.link_id)

    rails = [item for item in _entities(observation) if item.get("name") in {"straight-rail", "curved-rail-a", "curved-rail-b", "half-diagonal-rail"}]
    train_stops = [item for item in _entities(observation) if item.get("name") == "train-stop"]
    if rails or train_stops:
        links.append(
            LogisticsLinkEstimate(
                link_id="rail-network:observed",
                kind="rail",
                item=None,
                from_site="rail_network",
                to_site="rail_network",
                status="observed",
                length_tiles=float(len(rails)),
                notes=[f"{len(rails)} rail entities and {len(train_stops)} train stops observed"],
            )
        )

    return links


def _site_output_items(site: FactorySiteEstimate) -> list[str]:
    if not site.item:
        return []
    if site.kind in {"mining_patch", "plate_smelting_line", "build_item_mall", "assembler_cell", "circuit_automation"}:
        return [site.item]
    return []


def _site_required_input_items(site: FactorySiteEstimate) -> list[str]:
    required: list[str] = []
    if site.kind == "plate_smelting_line":
        if site.item == "iron-plate":
            required.append("iron-ore")
        elif site.item == "copper-plate":
            required.append("copper-ore")
        if _site_uses_burner_or_fuel(site):
            required.append("coal")
    elif site.kind == "steam_power":
        required.append("coal")
    elif site.kind in {"build_item_mall", "assembler_cell", "circuit_automation"} and site.item:
        recipe = RECIPES.get(site.item)
        if recipe:
            required.extend(recipe.ingredients.keys())
    return sorted(set(required))


def _site_uses_burner_or_fuel(site: FactorySiteEstimate) -> bool:
    text = " ".join(site.machines + [site.automation_level] + site.notes)
    return any(token in text for token in ["burner", "stone-furnace", "boiler", "coal"])


def _nearest_source_site(
    item: str,
    consumer: FactorySiteEstimate,
    sources_by_item: dict[str, list[FactorySiteEstimate]],
) -> FactorySiteEstimate | None:
    candidates = [site for site in sources_by_item.get(item, []) if site.site_id != consumer.site_id]
    if not candidates:
        return None
    return min(candidates, key=lambda site: distance(site.position, consumer.position))


def _site_logistics_link(
    observation: dict[str, Any],
    item: str,
    source: FactorySiteEstimate | None,
    consumer: FactorySiteEstimate,
) -> LogisticsLinkEstimate:
    source_id = source.site_id if source is not None else f"missing_source:{item}"
    source_position = source.position if source is not None else consumer.position
    route = _site_route_observed(observation, item, source, consumer)
    kind = "belt" if route else "site_flow"
    status = "route_observed" if route else ("missing_source" if source is None else "route_needed")
    notes = [
        "site-level logistics link inferred from producer and consumer sites",
        "exact belt, train, inserter, and chest routing is executor-level detail",
    ]
    if consumer.kind == "plate_smelting_line" and item == "coal":
        notes.append("stone furnace bootstrap smelting needs coal until electric furnaces or fuel automation replace it")
    return LogisticsLinkEstimate(
        link_id=f"site-link:{item}:{source_id}->{consumer.site_id}",
        kind=kind,
        item=item,
        from_site=source_id,
        to_site=consumer.site_id,
        status=status,
        length_tiles=round(distance(source_position, consumer.position), 2),
        notes=notes,
    )


def _site_route_observed(
    observation: dict[str, Any],
    item: str,
    source: FactorySiteEstimate | None,
    consumer: FactorySiteEstimate,
) -> bool:
    if source is None:
        return False
    if item in {"iron-ore", "copper-ore"} and consumer.kind == "plate_smelting_line":
        for layout in _deduped_belt_line_layouts(observation):
            if str(layout.get("resource_name") or "") != item:
                continue
            start = _position(layout["drill"]) if isinstance(layout.get("drill"), dict) else layout.get("drill_position")
            end = _position(layout["furnace"]) if isinstance(layout.get("furnace"), dict) else _layout_center(layout)
            if not isinstance(start, dict) or not isinstance(end, dict):
                continue
            if distance(start, source.position) <= 48 and distance(end, consumer.position) <= 48:
                return True
    if item == "coal" and consumer.kind == "plate_smelting_line":
        if distance(source.position, consumer.position) > 48:
            return False
        return _fuel_feed_type(observation, consumer.position) == "belt"
    if item == "coal" and consumer.kind == "steam_power":
        if distance(source.position, consumer.position) > 48:
            return False
        return any(_fuel_feed_type(observation, _position(entity)) == "belt" for entity in _entities(observation) if entity.get("name") == "boiler")
    return False


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


def _deduped_belt_line_layouts(observation: dict[str, Any]) -> list[dict[str, Any]]:
    layouts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for belt in [item for item in _entities(observation) if item.get("name") == "transport-belt"]:
        for layout in _belt_line_layouts_from_anchor(observation, belt):
            key_position = (
                _position(layout["furnace"])
                if isinstance(layout.get("furnace"), dict)
                else _layout_center(layout)
            )
            key = _position_site_id("belt_line", key_position)
            if key in seen:
                continue
            seen.add(key)
            layouts.append(layout)
    return layouts


def _fuel_feed_type(observation: dict[str, Any], target_position: dict[str, float]) -> str:
    inserters = [
        item
        for item in _entities(observation)
        if item.get("name") in {"burner-inserter", "inserter", "fast-inserter"}
        and distance(_position(item), target_position) <= 3.0
    ]
    for inserter in inserters:
        inserter_position = _position(inserter)
        if _entity_near(observation, "transport-belt", inserter_position, 2.5) is not None:
            return "belt"
    return "manual"


def _nearby_machine_names(
    observation: dict[str, Any],
    position: dict[str, float],
    names: set[str],
    radius: float,
) -> list[str]:
    output = sorted(
        {
            str(entity.get("name") or "")
            for entity in _entities(observation)
            if entity.get("name") in names and distance(position, _position(entity)) <= radius
        }
    )
    return [item for item in output if item]


def _layout_center(layout: dict[str, Any]) -> dict[str, float]:
    positions = []
    for key in ("drill_position", "belt1_position", "belt2_position", "inserter_position", "furnace_position"):
        value = layout.get(key)
        if isinstance(value, dict):
            positions.append(_position({"position": value}))
    for key in ("drill", "belt1", "belt2", "inserter", "furnace"):
        value = layout.get(key)
        if isinstance(value, dict):
            positions.append(_position(value))
    if not positions:
        return {"x": 0.0, "y": 0.0}
    return {
        "x": round(sum(item["x"] for item in positions) / len(positions), 2),
        "y": round(sum(item["y"] for item in positions) / len(positions), 2),
    }


def _machine_name(entity: Any) -> str | None:
    if not isinstance(entity, dict):
        return None
    name = entity.get("name")
    return str(name) if name else None


def _entity_site_id(prefix: str, entity: dict[str, Any]) -> str:
    unit_number = entity.get("unit_number")
    if unit_number:
        return f"{prefix}:{unit_number}"
    return _position_site_id(prefix, _position(entity))


def _position_site_id(prefix: str, position: dict[str, float]) -> str:
    return f"{prefix}:{round(float(position.get('x') or 0.0), 1)},{round(float(position.get('y') or 0.0), 1)}"


def _product_for_resource(resource_name: str) -> str | None:
    return {
        "iron-ore": "iron-plate",
        "copper-ore": "copper-plate",
        "stone": "stone-brick",
    }.get(resource_name)


def _link_id(kind: str, start: dict[str, float], end: dict[str, float], item: str | None) -> str:
    return (
        f"{kind}:{item or 'mixed'}:"
        f"{round(float(start.get('x') or 0.0), 1)},{round(float(start.get('y') or 0.0), 1)}->"
        f"{round(float(end.get('x') or 0.0), 1)},{round(float(end.get('y') or 0.0), 1)}"
    )


def _electric_network_key(entity: dict[str, Any]) -> str:
    raw = entity.get("electric_network_id")
    if raw is None or raw == "":
        return "unknown"
    return str(raw)


STARTER_USABLE_RADIUS = 224.0


def _estimate_furnace(observation: dict[str, Any], entity: dict[str, Any], speed: float) -> ProductionEstimate | None:
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
    usable = _starter_usable_rate(observation, entity, per_minute)
    return ProductionEstimate(
        item=product,
        per_minute=round(per_minute, 3),
        producers=1,
        confidence=0.65,
        notes=_starter_usability_notes_for_entity(
            observation,
            entity,
            f"inferred from {entity.get('name')} inventories",
        ),
        usable_per_minute=round(usable, 3),
    )


def _estimate_miner(
    entity: dict[str, Any],
    observation: dict[str, Any],
    per_minute: float,
) -> ProductionEstimate | None:
    position = entity.get("position") if isinstance(entity.get("position"), dict) else {}
    if not position:
        return None
    if str(entity.get("name") or "") == "burner-mining-drill" and _entity_status_is(entity, "no_fuel", 53):
        return None
    resource_name = _entity_mining_target_name(entity)
    if resource_name is None:
        resource = _nearest_resource(observation, {"x": float(position.get("x") or 0), "y": float(position.get("y") or 0)})
        resource_name = str(resource.get("name")) if resource is not None and resource.get("name") else None
    if resource_name is None:
        return None
    return ProductionEstimate(
        item=resource_name,
        per_minute=round(per_minute, 3),
        producers=1,
        confidence=0.55,
        notes=_starter_usability_notes_for_entity(
            observation,
            entity,
            f"inferred from {entity.get('name')} near resource patch",
        ),
        usable_per_minute=round(_starter_usable_rate(observation, entity, per_minute), 3),
    )


def _entity_status_is(entity: dict[str, Any], status_name: str, status_code: int) -> bool:
    if str(entity.get("status_name") or "") == status_name:
        return True
    try:
        return int(entity.get("status")) == status_code
    except (TypeError, ValueError):
        return False


def _estimate_assembler(observation: dict[str, Any], entity: dict[str, Any], crafting_speed: float) -> ProductionEstimate | None:
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
                notes=_starter_usability_notes_for_entity(
                    observation,
                    entity,
                    f"inferred from {entity.get('name')} recipe {recipe_name}",
                ),
                usable_per_minute=round(_starter_usable_rate(observation, entity, per_minute), 3),
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
        usable_per_minute=round(_usable_rate(current) + _usable_rate(estimate), 3),
    )


def _usable_rate(estimate: ProductionEstimate) -> float:
    if estimate.usable_per_minute is None:
        return estimate.per_minute
    return float(estimate.usable_per_minute)


def _starter_usable_rate(observation: dict[str, Any], entity: dict[str, Any], per_minute: float) -> float:
    return per_minute if _is_starter_usable_position(observation, _position(entity)) else 0.0


def _starter_usability_notes_for_entity(
    observation: dict[str, Any],
    entity: dict[str, Any],
    note: str,
) -> list[str]:
    if _is_starter_usable_position(observation, _position(entity)):
        return [note]
    return [note, "not counted toward starter production targets until rail or a validated logistics link connects it"]


def _starter_usability_notes(
    total_count: int,
    usable_count: int,
    note: str,
) -> list[str]:
    if usable_count >= total_count:
        return [note]
    return [
        note,
        f"{max(0, total_count - usable_count)} remote or isolated line(s) are not counted toward starter production targets",
    ]


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


def _enemies(observation: dict[str, Any]) -> list[dict[str, Any]]:
    enemies = observation.get("enemies")
    if not isinstance(enemies, list):
        return []
    return [item for item in enemies if isinstance(item, dict)]


def _nearest_enemy(enemies: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not enemies:
        return None
    return min(enemies, key=lambda item: _enemy_distance(item) or 999999.0)


def _enemy_distance(enemy: dict[str, Any] | None) -> float | None:
    if not enemy:
        return None
    value = enemy.get("distance")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


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


def _complete_belt_line_count(
    observation: dict[str, Any],
    resource_name: str,
    *,
    starter_usable_only: bool = False,
) -> int:
    furnace_positions: set[tuple[float, float]] = set()
    for belt in [item for item in _entities(observation) if item.get("name") == "transport-belt"]:
        for layout in _belt_line_layouts_from_anchor(observation, belt):
            if layout["resource_name"] != resource_name:
                continue
            if starter_usable_only and not _is_starter_usable_position(observation, _layout_center(layout)):
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
        drill = _entity_near(observation, "burner-mining-drill", drill_position, 2.0)
        resource = _nearest_resource(observation, drill_position)
        resource_name = (
            _entity_mining_target_name(drill)
            or (str(resource.get("name")) if resource is not None and resource.get("name") else None)
            or "iron-ore"
        )
        output.append(
            {
                "resource_name": resource_name,
                "drill_position": drill_position,
                "belt1_position": belt_position,
                "belt2_position": {"x": belt_position["x"] + dx, "y": belt_position["y"] + dy},
                "inserter_position": {"x": belt_position["x"] + 2 * dx, "y": belt_position["y"] + 2 * dy},
                "furnace_position": {"x": belt_position["x"] + 3 * dx, "y": belt_position["y"] + 3 * dy},
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
                "drill": drill,
            }
        )
    return output


def _entity_mining_target_name(entity: dict[str, Any] | None) -> str | None:
    if not isinstance(entity, dict):
        return None
    direct = str(entity.get("mining_target") or entity.get("resource_name") or "")
    return direct or None


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


def _is_starter_usable_position(observation: dict[str, Any], position: dict[str, float]) -> bool:
    anchor = _base_anchor_position(observation)
    if anchor is None:
        return True
    if _technology_researched(observation, "railway"):
        return True
    return distance(anchor, position) <= STARTER_USABLE_RADIUS


def _base_anchor_position(observation: dict[str, Any]) -> dict[str, float] | None:
    base = observation.get("base") if isinstance(observation.get("base"), dict) else {}
    for key in ("anchor_position", "spawn_position"):
        value = base.get(key)
        if isinstance(value, dict) and isinstance(value.get("x"), (int, float)) and isinstance(value.get("y"), (int, float)):
            return {"x": float(value["x"]), "y": float(value["y"])}
    return None


def _technology_researched(observation: dict[str, Any], technology: str) -> bool:
    research = observation.get("research") if isinstance(observation.get("research"), dict) else {}
    technologies = research.get("technologies") if isinstance(research.get("technologies"), dict) else {}
    state = technologies.get(technology)
    return bool(isinstance(state, dict) and state.get("researched"))


def _dependents(required: set[str]) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {}
    for recipe in RECIPES.values():
        for ingredient in recipe.ingredients:
            if ingredient in required:
                output.setdefault(ingredient, []).extend(recipe.products.keys())
    return {key: sorted(set(value)) for key, value in output.items()}
