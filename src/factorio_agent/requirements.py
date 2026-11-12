"""Read-only planning information for model-selected research and production.

These functions expose requirements, not a scripted choice of the next goal.
They use the installed catalog and a fresh availability observation.
"""
from __future__ import annotations

from copy import deepcopy
import math

from factorio_agent.core.deterministic_production import ProductionGraph


def research_requirements(catalog, observation: dict, technology: str) -> dict:
    order = catalog.technology_order(technology, include_researched=True)
    done = observation.get("technologies", {})
    missing = [name for name in order if not done.get(name)]
    science = {}
    triggers = []
    for name in missing:
        row = catalog.technologies[name]
        if row.get("research_trigger"):
            triggers.append({"technology": name, "trigger": deepcopy(row["research_trigger"])})
        else:
            count = row.get("unit_count")
            if not isinstance(count, (int, float)):
                raise ValueError("research formula requires an explicit target level: " + name)
            for ingredient in row["ingredients"]:
                science[ingredient["name"]] = science.get(ingredient["name"], 0) + ingredient["amount"] * count
    return {"target": technology, "observed_tick": observation["tick"],
            "missing_technologies": missing, "natural_triggers": triggers,
            "science_pack_upper_bounds": science,
            "note": "Totals exclude completed technologies; current partial progress is conservatively included."}


def production_requirements(catalog, observation: dict, item: str, rate_per_minute: float) -> dict:
    if type(rate_per_minute) not in (int, float) or not math.isfinite(rate_per_minute) or not 0 < rate_per_minute <= 100000:
        raise ValueError("rate_per_minute must be finite and in (0, 100000]")
    recipe = catalog.recipe_for_product(item)
    if recipe is None:
        raise ValueError("item requires a resource extraction capability: " + item)
    products = recipe["products"]
    if len(products) != 1 or products[0].get("type", "item") != "item" or products[0].get("probability", 1) != 1:
        raise ValueError("coproduct or stochastic production requires a separate material-balance planner")
    amount = products[0].get("amount")
    if not isinstance(amount, (int, float)) or amount <= 0:
        raise ValueError("recipe has no deterministic product amount")
    cycles = rate_per_minute / amount
    graph = ProductionGraph(catalog)
    machines = graph.machines_for_recipe(recipe["name"], observation, unlocked_only=False)
    result = {"item": item, "rate_per_minute": rate_per_minute, "recipe": recipe["name"],
              "enabled": bool(observation.get("enabled_recipes", {}).get(recipe["name"])),
              "observed_tick": observation["tick"],
              "ingredient_rates_per_minute": [dict(row, amount=row["amount"] * cycles) for row in recipe["ingredients"]],
              "machines": []}
    for machine in machines:
        count = math.ceil(cycles * recipe["energy"] / machine["crafting_speed"] / 60 - 1e-12)
        result["machines"].append({"name": machine["name"], "available": machine["available"], "count": count,
                                  "capacity_per_minute": count * machine["crafting_speed"] * 60 / recipe["energy"] * amount})
    return result


def rocket_requirements(catalog) -> dict:
    plan = ProductionGraph(catalog).for_first_rocket()
    return {"catalog_fingerprint": catalog.fingerprint, "technology_order": plan["technology_order"],
            "bill_of_materials": plan["bom"], "unsupported_requirements": plan["gaps"],
            "production_nodes": [node for node in plan["nodes"] if node["kind"] == "production"],
            "note": "Requirement coverage is not execution capability or a verified rocket launch."}
