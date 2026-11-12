"""Production/research graph built exclusively from a live WorldCatalog."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import math
from typing import Any, Mapping

from .world_catalog import CatalogError, WorldCatalog, _amount, _material, _typed_rows, _yield


def _researched(observation: Mapping[str, Any], catalog: WorldCatalog) -> set[str]:
    observed = observation.get("technologies")
    if isinstance(observed, dict):
        return {name for name, value in observed.items()
                if (value.get("researched", False) if isinstance(value, dict) else value is True)}
    if isinstance(observed, list):
        return {value if isinstance(value, str) else value["name"] for value in observed
                if isinstance(value, str) or value.get("researched")}
    return {name for name, value in catalog.technologies.items() if value.get("researched")}


class ProductionGraph:
    """Recipe quantities, sustained science capacity and natural unlock gates.

    Methods return declarative requirements. They never execute a game action.
    Rate requirements are normal-quality, zero-productivity capacities; one-off
    rocket/silo/starter-pack batch requirements remain separate from those rates.
    """

    def __init__(self, catalog: WorldCatalog, *, science_rate_per_minute: float = 30,
                 power_reserve_fraction: float = 0.2, fuel_reserve_seconds: float = 120):
        if not math.isfinite(science_rate_per_minute) or science_rate_per_minute <= 0:
            raise ValueError("science_rate_per_minute must be positive")
        if not 0 <= power_reserve_fraction < 1:
            raise ValueError("power_reserve_fraction must be in [0, 1)")
        if not math.isfinite(fuel_reserve_seconds) or fuel_reserve_seconds <= 0:
            raise ValueError("fuel_reserve_seconds must be positive")
        self.catalog = catalog
        self.science_rate_per_minute = float(science_rate_per_minute)
        self.power_reserve_fraction = power_reserve_fraction
        self.fuel_reserve_seconds = fuel_reserve_seconds
        self._plan: dict[str, Any] | None = None

    def _enabled(self, observation: Mapping[str, Any] | None) -> set[str]:
        if observation is not None and "enabled_recipes" in observation:
            values = observation["enabled_recipes"]
            return {name for name, value in values.items() if value} if isinstance(values, dict) else set(values)
        return {name for name, value in self.catalog.recipes.items() if value.get("enabled")}

    def machines_for_recipe(self, recipe_name: str, observation: Mapping[str, Any] | None = None,
                            *, unlocked_only: bool = True) -> list[dict[str, Any]]:
        recipe = self.catalog.recipes[recipe_name]
        categories = set(recipe["categories"])
        enabled = self._enabled(observation)
        existing = {entity["name"] for entity in (observation or {}).get("entities", [])}
        machines = []
        for name, entity in self.catalog.entities.items():
            if entity.get("type") not in {"assembling-machine", "furnace", "rocket-silo"}:
                continue
            if not categories.intersection(entity["crafting_categories"]):
                continue
            placement_items = [item["name"] for item in entity["items_to_place_this"]]
            build_recipes = [candidate for item in placement_items
                             if (candidate := self.catalog.recipe_for_product(item)) is not None]
            available = name in existing or any(candidate["name"] in enabled for candidate in build_recipes)
            if unlocked_only and not available:
                continue
            speed = entity.get("crafting_speed")
            if speed is None or not math.isfinite(float(speed)) or speed <= 0:
                continue
            machines.append({"name": name, "crafting_speed": speed, "available": available,
                             "placement_items": placement_items,
                             "energy_usage_per_tick": entity.get("energy_usage", 0) or 0,
                             "electric": entity.get("electric", False), "burner": entity.get("burner", False),
                             "fluidbox_prototypes": deepcopy(entity["fluidbox_prototypes"])})
        # Favor established machines, then the cheapest construction ingredient
        # count. This does not prescribe a curated machine tier by recipe name.
        def rank(machine):
            costs = []
            for item in machine["placement_items"]:
                row = self.catalog.recipe_for_product(item)
                if row:
                    costs.append(sum(_amount(i["amount"]) for i in row["ingredients"]))
            return (machine["name"] not in existing, min(costs, default=math.inf), machine["name"])
        return sorted(machines, key=rank)

    def _continuous_rates(self, science_names: list[str]) -> tuple[dict[str, float], dict[tuple[str, str], float]]:
        targets = {("item", name): self.science_rate_per_minute for name in science_names}
        order, choices = self.catalog._graph(targets)
        material_rates: dict[tuple[str, str], float] = defaultdict(float, targets)
        cycles: dict[str, float] = defaultdict(float)
        for key in reversed(order):
            if key not in choices or material_rates[key] <= 1e-9:
                continue
            recipe = choices[key]
            produced = sum(_yield(row) for row in recipe["products"] if _material(row) == key)
            if produced <= 0:
                raise CatalogError(f"zero production rate: {recipe['name']}")
            rate = material_rates[key] / produced
            cycles[recipe["name"]] += rate
            for product in recipe["products"]:
                material_rates[_material(product)] -= _yield(product) * rate
            for ingredient in recipe["ingredients"]:
                material_rates[_material(ingredient)] += _amount(ingredient["amount"]) * rate
        return dict(cycles), dict(material_rates)

    def for_first_rocket(self) -> dict[str, Any]:
        if self._plan is not None:
            return deepcopy(self._plan)
        bom = self.catalog.first_rocket_bom()
        targets = {row["name"]: row["amount"] for row in bom["targets"]}
        # A lab must be engine-crafted before red science can unlock. Include
        # natural craft milestones as minimum aggregate production, crediting
        # items already required by the rocket/science chain.
        trigger_targets: dict[str, float] = {}
        produced: dict[str, float] = defaultdict(float)
        for name, batches in bom["recipe_batches"].items():
            for product in self.catalog.recipes[name]["products"]:
                if product.get("type", "item") == "item":
                    produced[product["name"]] += _yield(product) * batches
        for row in bom["trigger_requirements"]:
            trigger = row["trigger"]
            if trigger.get("type") == "craft-item":
                item = trigger["item"]
                name = item["name"] if isinstance(item, dict) else item
                count = float(trigger.get("count", 1))
                if produced[name] < count:
                    trigger_targets[name] = max(trigger_targets.get(name, 0), count - produced[name])
        for name, count in trigger_targets.items():
            targets[name] = targets.get(name, 0) + count
        full_bom = self.catalog.bill_of_materials(targets)
        cycles, raw_rates = self._continuous_rates(sorted(bom["science_packs"]))
        closure = self.catalog.dependency_closure(targets)
        nodes = []
        gaps = []
        for name in closure["recipes"]:
            recipe = self.catalog.recipes[name]
            machines = self.machines_for_recipe(name, unlocked_only=False)
            character = self.catalog.entities.get("character", {})
            handcraft = bool(set(recipe["categories"]).intersection(character.get("crafting_categories", [])))
            if not machines and not handcraft:
                gaps.append({"kind": "machine_category", "recipe": name, "categories": recipe["categories"]})
            unlocks = [technology for technology, row in self.catalog.technologies.items() if name in row["unlocks"]]
            nodes.append({"id": f"recipe:{name}", "kind": "production", "recipe": name,
                          "ingredients": deepcopy(recipe["ingredients"]), "products": deepcopy(recipe["products"]),
                          "categories": list(recipe["categories"]), "energy_seconds": recipe["energy"],
                          "batches_required": full_bom["recipe_batches"].get(name, 0),
                          "cycles_per_minute": cycles.get(name, 0),
                          "unlock_technologies": sorted(unlocks), "handcraft_allowed": handcraft,
                          "machine_candidates": [machine["name"] for machine in machines]})
        technology_order = self.catalog.technology_order(set(bom["technology_order"]) | set(full_bom["technologies"]))
        for name in technology_order:
            technology = self.catalog.technologies[name]
            trigger = technology.get("research_trigger")
            if trigger and trigger.get("type") not in {"craft-item", "craft-fluid", "mine-entity", "build-entity", "create-space-platform", "send-item-to-orbit"}:
                gaps.append({"kind": "research_trigger", "technology": name, "trigger": deepcopy(trigger)})
            nodes.append({"id": f"technology:{name}", "kind": "trigger" if trigger else "research",
                          "technology": name, "prerequisites": list(technology["prerequisites"]),
                          "trigger": deepcopy(trigger), "science": deepcopy(technology["ingredients"]),
                          "unit_count": technology.get("unit_count")})
        self._plan = {"catalog_fingerprint": self.catalog.fingerprint, "objective": "first_space_age_rocket",
                      "nodes": nodes, "technology_order": technology_order,
                      "bom": {**full_bom, "science_packs": bom["science_packs"]},
                      "science_rate_per_minute": self.science_rate_per_minute,
                      "raw_rates_per_minute": _typed_rows({key: rate for key, rate in raw_rates.items() if key in self.catalog._raw}),
                      "reserves": {"power_spare_fraction": self.power_reserve_fraction,
                                   "fuel_seconds": self.fuel_reserve_seconds}, "gaps": gaps}
        return deepcopy(self._plan)

    def next_research(self, observation: Mapping[str, Any]) -> dict[str, Any] | None:
        plan = self.for_first_rocket()
        done = _researched(observation, self.catalog)
        current = observation.get("research")
        if isinstance(current, dict):
            current = current.get("name")
        if current and current in plan["technology_order"] and current not in done:
            return {"kind": "wait", "technology": current, "reason": "research_running", "action": None}
        for name in plan["technology_order"]:
            technology = self.catalog.technologies[name]
            if name in done or any(parent not in done for parent in technology["prerequisites"]):
                continue
            trigger = technology.get("research_trigger")
            if trigger:
                return {"kind": "trigger", "technology": name, "trigger": deepcopy(trigger),
                        "reason": "natural_trigger_required", "action": None}
            return {"kind": "research", "technology": name, "science": deepcopy(technology["ingredients"]),
                    "action": {"type": "research", "technology": name}}
        return None

    def recipe_demand(self, observation: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        """Map recipes to capacity/stock needs and current unlock/machine gates."""
        plan = self.for_first_rocket()
        enabled = self._enabled(observation)
        result = {}
        for node in plan["nodes"]:
            if node["kind"] != "production":
                continue
            row = deepcopy(node)
            name = row["recipe"]
            machines = self.machines_for_recipe(name, observation)
            row["enabled"] = name in enabled
            row["available_machines"] = [machine["name"] for machine in machines]
            row["status"] = "locked" if not row["enabled"] else "ready" if machines else "handcraft_only" if row["handcraft_allowed"] else "missing_machine"
            row["ingredient_rates_per_minute"] = [dict(ingredient, amount=ingredient["amount"] * row["cycles_per_minute"]) for ingredient in row["ingredients"]]
            if machines:
                selected = machines[0]
                row["selected_machine"] = selected["name"]
                count = math.ceil(row["cycles_per_minute"] * row["energy_seconds"] / selected["crafting_speed"] / 60 - 1e-12)
                row["machines_for_rate"] = max(0, count)
                demand_watts = selected["energy_usage_per_tick"] * max(0, count) * 60
                row["reserved_power_watts"] = demand_watts / (1 - self.power_reserve_fraction) if selected["electric"] else 0
                row["burner_fuel_energy_reserve_joules"] = demand_watts * self.fuel_reserve_seconds if selected["burner"] else 0
            result[name] = row
        return result
