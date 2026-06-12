from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Recipe:
    name: str
    time_seconds: float
    ingredients: dict[str, float]
    products: dict[str, float]
    technology: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Technology:
    name: str
    prerequisites: list[str]
    science_packs: dict[str, int]
    unlocks: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


RECIPES: dict[str, Recipe] = {
    "iron-plate": Recipe("iron-plate", 3.2, {"iron-ore": 1}, {"iron-plate": 1}),
    "copper-plate": Recipe("copper-plate", 3.2, {"copper-ore": 1}, {"copper-plate": 1}),
    "stone-brick": Recipe("stone-brick", 3.2, {"stone": 2}, {"stone-brick": 1}),
    "iron-gear-wheel": Recipe("iron-gear-wheel", 0.5, {"iron-plate": 2}, {"iron-gear-wheel": 1}),
    "copper-cable": Recipe("copper-cable", 0.5, {"copper-plate": 1}, {"copper-cable": 2}),
    "electronic-circuit": Recipe(
        "electronic-circuit",
        0.5,
        {"iron-plate": 1, "copper-cable": 3},
        {"electronic-circuit": 1},
    ),
    "automation-science-pack": Recipe(
        "automation-science-pack",
        5.0,
        {"copper-plate": 1, "iron-gear-wheel": 1},
        {"automation-science-pack": 1},
    ),
    "transport-belt": Recipe("transport-belt", 0.5, {"iron-plate": 1, "iron-gear-wheel": 1}, {"transport-belt": 2}),
    "inserter": Recipe(
        "inserter",
        0.5,
        {"electronic-circuit": 1, "iron-gear-wheel": 1, "iron-plate": 1},
        {"inserter": 1},
    ),
    "logistic-science-pack": Recipe(
        "logistic-science-pack",
        6.0,
        {"inserter": 1, "transport-belt": 1},
        {"logistic-science-pack": 1},
        technology="logistic-science-pack",
    ),
    "steel-plate": Recipe("steel-plate", 16.0, {"iron-plate": 5}, {"steel-plate": 1}, technology="steel-processing"),
    "pipe": Recipe("pipe", 0.5, {"iron-plate": 1}, {"pipe": 1}),
    "engine-unit": Recipe(
        "engine-unit",
        10.0,
        {"steel-plate": 1, "iron-gear-wheel": 1, "pipe": 2},
        {"engine-unit": 1},
        technology="engine",
    ),
    "advanced-circuit": Recipe(
        "advanced-circuit",
        6.0,
        {"electronic-circuit": 2, "copper-cable": 4, "plastic-bar": 2},
        {"advanced-circuit": 1},
        technology="advanced-circuit",
    ),
    "processing-unit": Recipe(
        "processing-unit",
        10.0,
        {"electronic-circuit": 20, "advanced-circuit": 2, "sulfuric-acid": 5},
        {"processing-unit": 1},
        technology="processing-unit",
    ),
    "low-density-structure": Recipe(
        "low-density-structure",
        20.0,
        {"copper-plate": 20, "steel-plate": 2, "plastic-bar": 5},
        {"low-density-structure": 1},
        technology="low-density-structure",
    ),
    "rocket-fuel": Recipe("rocket-fuel", 30.0, {"solid-fuel": 10}, {"rocket-fuel": 1}, technology="rocket-fuel"),
    "rocket-part": Recipe(
        "rocket-part",
        3.0,
        {"processing-unit": 10, "low-density-structure": 10, "rocket-fuel": 10},
        {"rocket-part": 1},
        technology="rocket-silo",
    ),
    "rocket-silo": Recipe(
        "rocket-silo",
        30.0,
        {"steel-plate": 1000, "concrete": 1000, "pipe": 100, "processing-unit": 200, "electric-engine-unit": 200},
        {"rocket-silo": 1},
        technology="rocket-silo",
    ),
}


TECHNOLOGIES: dict[str, Technology] = {
    "automation": Technology("automation", [], {"automation-science-pack": 10}, ["assembling-machine-1"]),
    "logistics": Technology("logistics", [], {"automation-science-pack": 20}, ["splitter", "underground-belt"]),
    "steel-processing": Technology("steel-processing", [], {"automation-science-pack": 50}, ["steel-plate"]),
    "logistic-science-pack": Technology(
        "logistic-science-pack",
        ["automation", "logistics"],
        {"automation-science-pack": 75},
        ["logistic-science-pack"],
    ),
    "electronics": Technology("electronics", ["automation"], {"automation-science-pack": 30}, ["electronic-circuit"]),
    "engine": Technology(
        "engine",
        ["steel-processing", "logistic-science-pack"],
        {"automation-science-pack": 100, "logistic-science-pack": 100},
        ["engine-unit"],
    ),
    "oil-processing": Technology(
        "oil-processing",
        ["steel-processing", "logistic-science-pack"],
        {"automation-science-pack": 100, "logistic-science-pack": 100},
        ["plastic-bar", "solid-fuel", "sulfur"],
    ),
    "advanced-circuit": Technology(
        "advanced-circuit",
        ["electronics", "oil-processing"],
        {"automation-science-pack": 200, "logistic-science-pack": 200},
        ["advanced-circuit"],
    ),
    "chemical-science-pack": Technology(
        "chemical-science-pack",
        ["advanced-circuit", "engine"],
        {"automation-science-pack": 200, "logistic-science-pack": 200},
        ["chemical-science-pack"],
    ),
    "processing-unit": Technology(
        "processing-unit",
        ["advanced-circuit", "chemical-science-pack"],
        {"automation-science-pack": 200, "logistic-science-pack": 200, "chemical-science-pack": 200},
        ["processing-unit"],
    ),
    "low-density-structure": Technology(
        "low-density-structure",
        ["advanced-circuit", "chemical-science-pack"],
        {"automation-science-pack": 200, "logistic-science-pack": 200, "chemical-science-pack": 200},
        ["low-density-structure"],
    ),
    "rocket-fuel": Technology(
        "rocket-fuel",
        ["oil-processing"],
        {"automation-science-pack": 200, "logistic-science-pack": 200, "chemical-science-pack": 200},
        ["rocket-fuel"],
    ),
    "rocket-silo": Technology(
        "rocket-silo",
        ["processing-unit", "low-density-structure", "rocket-fuel"],
        {
            "automation-science-pack": 1000,
            "logistic-science-pack": 1000,
            "chemical-science-pack": 1000,
            "production-science-pack": 1000,
            "utility-science-pack": 1000,
        },
        ["rocket-silo", "rocket-part"],
    ),
}


OBJECTIVE_ROOTS = {
    "launch_rocket_program": ["rocket-silo", "rocket-part"],
    "rocket": ["rocket-silo", "rocket-part"],
    "electronic_circuit": ["electronic-circuit"],
    "automation_science": ["automation-science-pack"],
    "red_science": ["automation-science-pack"],
}


RAW_RESOURCES = {"iron-ore", "copper-ore", "coal", "stone", "crude-oil", "water"}


def objective_roots(objective: str) -> list[str]:
    normalized = objective.strip().lower().replace(" ", "_").replace("-", "_")
    if "rocket" in normalized:
        return OBJECTIVE_ROOTS["rocket"]
    if "electronic" in normalized or "circuit" in normalized:
        return OBJECTIVE_ROOTS["electronic_circuit"]
    if "science" in normalized:
        return OBJECTIVE_ROOTS["automation_science"]
    return OBJECTIVE_ROOTS.get(normalized, [normalized])


def dependency_tree_for_objective(objective: str, max_depth: int = 4) -> list[dict[str, Any]]:
    return [dependency_tree(item, max_depth=max_depth) for item in objective_roots(objective)]


def dependency_tree(item: str, max_depth: int = 4, _seen: set[str] | None = None) -> dict[str, Any]:
    seen = set(_seen or set())
    if item in seen or max_depth < 0:
        return {"item": item, "cycle_or_depth_limit": True}
    seen.add(item)

    recipe = recipe_for_product(item)
    if recipe is None:
        return {"item": item, "raw_resource": item in RAW_RESOURCES, "recipe": None, "technology": None, "ingredients": []}

    return {
        "item": item,
        "recipe": recipe.name,
        "technology": recipe.technology,
        "ingredients": [
            {
                "item": ingredient,
                "amount": amount,
                "dependency": dependency_tree(ingredient, max_depth=max_depth - 1, _seen=seen),
            }
            for ingredient, amount in recipe.ingredients.items()
        ],
    }


def required_items_for_objective(objective: str, max_depth: int = 4) -> set[str]:
    required: set[str] = set()
    for root in objective_roots(objective):
        _collect_required(root, required, max_depth=max_depth)
    return required


def technology_chain_for_recipe(recipe_name: str) -> list[dict[str, Any]]:
    recipe = RECIPES.get(recipe_name)
    if recipe is None or recipe.technology is None:
        return []
    return technology_chain(recipe.technology)


def technology_chain(technology_name: str, _seen: set[str] | None = None) -> list[dict[str, Any]]:
    seen = set(_seen or set())
    if technology_name in seen:
        return []
    seen.add(technology_name)
    tech = TECHNOLOGIES.get(technology_name)
    if tech is None:
        return [{"name": technology_name, "missing_static_data": True}]
    chain: list[dict[str, Any]] = []
    for prerequisite in tech.prerequisites:
        chain.extend(technology_chain(prerequisite, _seen=seen))
    chain.append(tech.to_dict())
    return chain


def recipe_for_product(item: str) -> Recipe | None:
    for recipe in RECIPES.values():
        if item in recipe.products:
            return recipe
    return None


def _collect_required(item: str, output: set[str], max_depth: int) -> None:
    if max_depth < 0 or item in output:
        return
    output.add(item)
    recipe = recipe_for_product(item)
    if recipe is None:
        return
    for ingredient in recipe.ingredients:
        _collect_required(ingredient, output, max_depth=max_depth - 1)
