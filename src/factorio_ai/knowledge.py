from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
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
    "firearm-magazine": Recipe("firearm-magazine", 1.0, {"iron-plate": 4}, {"firearm-magazine": 1}),
    "gun-turret": Recipe(
        "gun-turret",
        8.0,
        {"iron-plate": 10, "copper-plate": 5, "iron-gear-wheel": 10},
        {"gun-turret": 1},
    ),
    "stone-furnace": Recipe("stone-furnace", 0.5, {"stone": 5}, {"stone-furnace": 1}),
    "burner-mining-drill": Recipe(
        "burner-mining-drill",
        2.0,
        {"iron-plate": 3, "stone": 5, "iron-gear-wheel": 3},
        {"burner-mining-drill": 1},
    ),
    "electric-mining-drill": Recipe(
        "electric-mining-drill",
        2.0,
        {"electronic-circuit": 3, "iron-gear-wheel": 5, "iron-plate": 10},
        {"electric-mining-drill": 1},
        technology="electric-mining-drill",
    ),
    "burner-inserter": Recipe(
        "burner-inserter",
        0.5,
        {"iron-plate": 1, "iron-gear-wheel": 1},
        {"burner-inserter": 1},
    ),
    "long-handed-inserter": Recipe(
        "long-handed-inserter",
        0.5,
        {"iron-plate": 1, "iron-gear-wheel": 1, "inserter": 1},
        {"long-handed-inserter": 1},
        technology="long-inserters",
    ),
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
    "underground-belt": Recipe(
        "underground-belt",
        1.0,
        {"iron-plate": 5, "transport-belt": 10},
        {"underground-belt": 2},
        technology="logistics",
    ),
    "splitter": Recipe(
        "splitter",
        1.0,
        {"electronic-circuit": 5, "iron-plate": 5, "transport-belt": 4},
        {"splitter": 1},
        technology="logistics",
    ),
    "inserter": Recipe(
        "inserter",
        0.5,
        {"electronic-circuit": 1, "iron-gear-wheel": 1, "iron-plate": 1},
        {"inserter": 1},
    ),
    "small-electric-pole": Recipe(
        "small-electric-pole",
        0.5,
        {"wood": 1, "copper-cable": 2},
        {"small-electric-pole": 2},
    ),
    "assembling-machine-1": Recipe(
        "assembling-machine-1",
        0.5,
        {"electronic-circuit": 3, "iron-gear-wheel": 5, "iron-plate": 9},
        {"assembling-machine-1": 1},
        technology="automation",
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
    "electric-mining-drill": Technology(
        "electric-mining-drill",
        ["automation-science-pack"],
        {"automation-science-pack": 25},
        ["electric-mining-drill"],
    ),
    "long-inserters": Technology("long-inserters", [], {"automation-science-pack": 50}, ["long-handed-inserter"]),
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


# ---------------------------------------------------------------------------
# Authoritative full game data (Factorio 2.0 / Space Age) dumped from the live
# server via tools/dump_game_data.py. The curated RECIPES/TECHNOLOGIES above stay
# the planner's stable, hand-tuned source of truth; the full set below powers the
# dependency tree, bottleneck analysis, and the skill-foundry codegen vocabulary.
# ---------------------------------------------------------------------------

_DATA_PATH = Path(__file__).resolve().parent / "data" / "game_data.json"


def _load_game_data() -> tuple[dict[str, Recipe], dict[str, Technology]]:
    try:
        raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return dict(RECIPES), dict(TECHNOLOGIES)

    techs_raw = raw.get("technologies") if isinstance(raw.get("technologies"), dict) else {}
    recipes_raw = raw.get("recipes") if isinstance(raw.get("recipes"), dict) else {}

    recipe_tech: dict[str, str] = {}
    for tname, tdata in techs_raw.items():
        if isinstance(tdata, dict):
            for rname in tdata.get("unlocks") or []:
                recipe_tech.setdefault(str(rname), str(tname))

    recipes: dict[str, Recipe] = {}
    for name, rdata in recipes_raw.items():
        if not isinstance(rdata, dict):
            continue
        enabled = bool(rdata.get("enabled"))
        recipes[name] = Recipe(
            name=name,
            time_seconds=float(rdata.get("energy") or 0.5),
            ingredients={str(k): float(v) for k, v in (rdata.get("ingredients") or {}).items()},
            products={str(k): float(v) for k, v in (rdata.get("products") or {}).items()},
            technology=None if enabled else recipe_tech.get(name),
        )

    technologies: dict[str, Technology] = {}
    for name, tdata in techs_raw.items():
        if not isinstance(tdata, dict):
            continue
        count = int(tdata.get("unit_count") or 0)
        packs_raw = tdata.get("science_packs") if isinstance(tdata.get("science_packs"), dict) else {}
        packs = {str(k): (int(v) * count if count else int(v)) for k, v in packs_raw.items()}
        technologies[name] = Technology(
            name=name,
            prerequisites=[str(p) for p in (tdata.get("prerequisites") or [])],
            science_packs=packs,
            unlocks=[str(u) for u in (tdata.get("unlocks") or [])],
        )

    # Hand-curated entries win where both define the same name: the planner is
    # tuned against those exact shapes, so keep the two views consistent there.
    recipes.update(RECIPES)
    technologies.update(TECHNOLOGIES)
    return recipes, technologies


ALL_RECIPES, ALL_TECHNOLOGIES = _load_game_data()


# Production buildings / infrastructure surfaced as their own dependency trees so
# the planner LLM can reason about producing them (not just the objective's
# critical path). Filtered to whatever the loaded data actually defines.
INFRASTRUCTURE_ROOTS: list[str] = [
    # smelting
    "stone-furnace", "steel-furnace", "electric-furnace",
    # assembling
    "assembling-machine-1", "assembling-machine-2", "assembling-machine-3",
    # mining / pumping
    "burner-mining-drill", "electric-mining-drill", "big-mining-drill", "pumpjack",
    # power
    "offshore-pump", "boiler", "steam-engine", "solar-panel", "accumulator",
    "nuclear-reactor", "heat-exchanger", "steam-turbine",
    # belts / inserters / pipes
    "transport-belt", "fast-transport-belt", "express-transport-belt",
    "underground-belt", "splitter", "inserter", "fast-inserter", "bulk-inserter",
    "long-handed-inserter", "pipe", "pipe-to-ground", "pump",
    # electric distribution
    "small-electric-pole", "medium-electric-pole", "big-electric-pole", "substation",
    # production / processing
    "oil-refinery", "chemical-plant", "lab", "biolab", "storage-tank", "radar",
    "electromagnetic-plant", "foundry", "biochamber", "cryogenic-plant", "recycler",
]


OBJECTIVE_ROOTS = {
    "launch_rocket_program": ["rocket-silo", "rocket-part"],
    "rocket": ["rocket-silo", "rocket-part"],
    "electronic_circuit": ["electronic-circuit"],
    "automation_science": ["automation-science-pack"],
    "red_science": ["automation-science-pack"],
}


RAW_RESOURCES = {
    # mined / pumped primaries — terminal leaves of the dependency tree
    "iron-ore", "copper-ore", "coal", "stone", "crude-oil", "water", "wood",
    "uranium-ore", "calcite", "tungsten-ore", "holmium-ore",
    # Space Age planet primaries with no standard crafting recipe (truly dangling)
    "scrap", "lava", "lithium-brine", "fluorine", "ammoniacal-solution",
}


def objective_roots(objective: str) -> list[str]:
    normalized = objective.strip().lower().replace(" ", "_").replace("-", "_")
    if "rocket" in normalized:
        return OBJECTIVE_ROOTS["rocket"]
    if "electronic" in normalized or "circuit" in normalized:
        return OBJECTIVE_ROOTS["electronic_circuit"]
    if "science" in normalized:
        return OBJECTIVE_ROOTS["automation_science"]
    return OBJECTIVE_ROOTS.get(normalized, [normalized])


def dependency_tree_for_objective(
    objective: str,
    max_depth: int = 4,
    *,
    include_infrastructure: bool = True,
    infrastructure_depth: int = 2,
) -> list[dict[str, Any]]:
    trees = [dependency_tree(item, max_depth=max_depth) for item in objective_roots(objective)]
    if include_infrastructure:
        seen = {tree.get("item") for tree in trees}
        for item in INFRASTRUCTURE_ROOTS:
            if item in seen or item not in ALL_RECIPES:
                continue
            tree = dependency_tree(item, max_depth=infrastructure_depth)
            tree["infrastructure"] = True
            trees.append(tree)
            seen.add(item)
    return trees


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
    recipe = ALL_RECIPES.get(recipe_name)
    if recipe is None or recipe.technology is None:
        return []
    return technology_chain(recipe.technology)


def technology_chain(technology_name: str, _seen: set[str] | None = None) -> list[dict[str, Any]]:
    seen = set(_seen or set())
    if technology_name in seen:
        return []
    seen.add(technology_name)
    tech = ALL_TECHNOLOGIES.get(technology_name)
    if tech is None:
        return [{"name": technology_name, "missing_static_data": True}]
    chain: list[dict[str, Any]] = []
    for prerequisite in tech.prerequisites:
        chain.extend(technology_chain(prerequisite, _seen=seen))
    chain.append(tech.to_dict())
    return chain


def _is_alt_recipe(name: str) -> bool:
    # recycling (and similar) recipes also "produce" base items; never canonical.
    return name.endswith("-recycling")


@lru_cache(maxsize=1)
def _canonical_product_map() -> dict[str, str]:
    """item -> canonical recipe name.

    With the full Space Age data many items are produced by several recipes
    (e.g. iron-plate via smelting *and* Fulgora recycling). Pick the standard one
    so the dependency tree follows the intended chain: prefer the recipe named
    after the item, then non-recycling, then base-enabled, then the simplest.
    """
    by_product: dict[str, list[Recipe]] = {}
    for recipe in ALL_RECIPES.values():
        for product in recipe.products:
            by_product.setdefault(product, []).append(recipe)
    chosen: dict[str, str] = {}
    for item, candidates in by_product.items():
        named = next((r for r in candidates if r.name == item), None)
        if named is not None:
            chosen[item] = named.name
            continue
        pool = [r for r in candidates if not _is_alt_recipe(r.name)] or candidates
        base = [r for r in pool if r.technology is None]
        pool = base or pool
        chosen[item] = min(pool, key=lambda r: (len(r.ingredients), r.name)).name
    return chosen


def recipe_for_product(item: str) -> Recipe | None:
    if item in RAW_RESOURCES:
        return None
    name = _canonical_product_map().get(item)
    return ALL_RECIPES.get(name) if name else None


def _collect_required(item: str, output: set[str], max_depth: int) -> None:
    if max_depth < 0 or item in output:
        return
    output.add(item)
    recipe = recipe_for_product(item)
    if recipe is None:
        return
    for ingredient in recipe.ingredients:
        _collect_required(ingredient, output, max_depth=max_depth - 1)
