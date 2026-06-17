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
        return dict(RECIPES), dict(TECHNOLOGIES), [], {}

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

    # Data-driven infrastructure roots: every buildable item in the logistics/production
    # groups (so ALL belt/underground/splitter/inserter/chest/machine TIERS + any future
    # items are auto-included -- a hand list always gaps). "name in products" keeps only the
    # canonical build recipe for that item; recycling/editor items are dropped.
    infra_groups = {"logistics", "production"}
    infra_roots = sorted(
        name
        for name, rdata in recipes_raw.items()
        if isinstance(rdata, dict)
        and rdata.get("group") in infra_groups
        and not name.endswith("-recycling")
        and name in (rdata.get("products") or {})
        and "infinity" not in name
    )

    # recipe name -> crafting category (kept in a side map so the curated merge below,
    # which lacks categories, doesn't clobber it). Drives the category->facility lookup.
    recipe_category = {
        name: str(rdata.get("category"))
        for name, rdata in recipes_raw.items()
        if isinstance(rdata, dict) and rdata.get("category")
    }

    # Hand-curated entries win where both define the same name: the planner is
    # tuned against those exact shapes, so keep the two views consistent there.
    recipes.update(RECIPES)
    technologies.update(TECHNOLOGIES)
    return recipes, technologies, infra_roots, recipe_category


ALL_RECIPES, ALL_TECHNOLOGIES, _DERIVED_INFRA_ROOTS, _RECIPE_CATEGORY = _load_game_data()


# --------------------------------------------------------------------------- #
# Crafting facilities + fluids (stable game knowledge; the live dump of these is
# RCON-contended, so encoded here -- category is authoritative from the data above).
# A compact category -> facilities legend so each item only carries a short category
# code instead of repeating the machine list.
# --------------------------------------------------------------------------- #
CRAFTING_FACILITIES: dict[str, list[str]] = {
    "crafting": ["assembling-machine-1", "assembling-machine-2", "assembling-machine-3"],
    "advanced-crafting": ["assembling-machine-2", "assembling-machine-3"],
    "crafting-with-fluid": ["assembling-machine-2", "assembling-machine-3"],
    "crafting-with-fluid-or-metallurgy": ["assembling-machine-2", "assembling-machine-3", "foundry"],
    "smelting": ["stone-furnace", "steel-furnace", "electric-furnace"],
    "metallurgy": ["foundry"],
    "metallurgy-or-assembling": ["foundry", "assembling-machine-2", "assembling-machine-3"],
    "electronics": ["electromagnetic-plant", "assembling-machine-1", "assembling-machine-2", "assembling-machine-3"],
    "electronics-or-assembling": ["electromagnetic-plant", "assembling-machine-2", "assembling-machine-3"],
    "electronics-with-fluid": ["electromagnetic-plant"],
    "electromagnetics": ["electromagnetic-plant"],
    "chemistry": ["chemical-plant"],
    "chemistry-or-cryogenics": ["chemical-plant", "cryogenic-plant"],
    "cryogenics": ["cryogenic-plant"],
    "cryogenics-or-assembling": ["cryogenic-plant", "assembling-machine-2", "assembling-machine-3"],
    "oil-processing": ["oil-refinery"],
    "organic": ["biochamber"],
    "organic-or-assembling": ["biochamber", "assembling-machine-2", "assembling-machine-3"],
    "organic-or-chemistry": ["biochamber", "chemical-plant"],
    "organic-or-hand-crafting": ["biochamber"],
    "centrifuging": ["centrifuge"],
    "rocket-building": ["rocket-silo"],
    "recycling": ["recycler"],
    "recycling-or-hand-crafting": ["recycler"],
    "crushing": ["crusher"],
    "pressing": ["assembling-machine-2", "assembling-machine-3"],
    "captive-spawner-process": ["biochamber"],
}

# Fluids (item names that are fluids, not solid items). High-confidence set; an item
# not listed is treated as a solid. Refine from prototypes.fluid via tools/dump_game_data.py.
FLUIDS: set[str] = {
    "water", "steam", "crude-oil", "heavy-oil", "light-oil", "petroleum-gas",
    "sulfuric-acid", "lubricant", "lava", "molten-iron", "molten-copper",
    "holmium-solution", "electrolyte", "fluorine", "fluoroketone-hot",
    "fluoroketone-cold", "lithium-brine", "ammonia", "ammoniacal-solution",
    "thruster-fuel", "thruster-oxidizer", "fusion-plasma",
}


# Fallback used only if the game-data dump is missing (loader returns no roots).
_FALLBACK_INFRA_ROOTS: list[str] = [
    "stone-furnace", "steel-furnace", "electric-furnace",
    "assembling-machine-1", "assembling-machine-2", "assembling-machine-3",
    "burner-mining-drill", "electric-mining-drill", "pumpjack",
    "offshore-pump", "boiler", "steam-engine", "solar-panel",
    "transport-belt", "underground-belt", "splitter", "inserter",
    "small-electric-pole", "lab", "oil-refinery", "chemical-plant",
]

# Production buildings / logistics infrastructure surfaced as their own dependency
# trees so the planner LLM can reason about producing them, not just the objective's
# critical path.
INFRASTRUCTURE_ROOTS: list[str] = _DERIVED_INFRA_ROOTS or _FALLBACK_INFRA_ROOTS


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


def flat_dependency_map(roots: list[str] | None = None) -> dict[str, list[str]]:
    """Flat ``{item: [direct ingredient names]}`` -- one hop, not a recursed tree.

    Each item appears ONCE with its canonical recipe's direct ingredients, so a consumer
    (the strategy LLM) gets the whole recipe graph compactly -- A->[B1,B2,B3], B1->[C1,C2]
    -- without the deep tree's duplicated sub-trees or truncation. Raw resources are
    omitted (no ingredients). With ``roots``, restrict to items reachable from them;
    otherwise include every craftable item.
    """
    out: dict[str, list[str]] = {}
    if roots:
        frontier = [r for r in roots]
        while frontier:
            item = frontier.pop()
            if item in out or item in RAW_RESOURCES:
                continue
            recipe = recipe_for_product(item)
            if recipe is None:
                continue
            ingredients = sorted(recipe.ingredients.keys())
            out[item] = ingredients
            frontier.extend(ingredients)
        return out
    for item in sorted(_canonical_product_map().keys()):
        if item in RAW_RESOURCES:
            continue
        recipe = recipe_for_product(item)
        if recipe is not None:
            out[item] = sorted(recipe.ingredients.keys())
    return out


def _num(value: Any) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return int(number) if number == int(number) else round(number, 3)


def is_fluid(item: str) -> bool:
    return item in FLUIDS


def recipe_category_for(item: str) -> str | None:
    recipe = recipe_for_product(item)
    return _RECIPE_CATEGORY.get(recipe.name) if recipe is not None else None


def recipe_details(items: Any) -> dict[str, dict[str, Any]]:
    """Rich, compact recipe info for the given items:

        {item: {"in": {ingredient: amount}, "out": n (omitted if 1),
                "cat": crafting-category (-> CRAFTING_FACILITIES), "fluid": true (omitted if false)}}

    "cat" is a short code into the facility legend (facility_legend) so the machine list
    isn't repeated per item; "out"/"fluid" are omitted at their defaults to stay compact.
    """
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        recipe = recipe_for_product(item)
        if recipe is None:
            continue
        product_amount = recipe.products.get(item)
        if product_amount is None and recipe.products:
            product_amount = next(iter(recipe.products.values()))
        entry: dict[str, Any] = {"in": {k: _num(v) for k, v in recipe.ingredients.items()}}
        if product_amount not in (None, 1, 1.0):
            entry["out"] = _num(product_amount)
        category = _RECIPE_CATEGORY.get(recipe.name)
        if category:
            entry["cat"] = category
        if item in FLUIDS:
            entry["fluid"] = True
        out[item] = entry
    return out


def facility_legend(categories: Any = None) -> dict[str, list[str]]:
    """category -> [facilities] table. With ``categories`` given, only those (so a scoped
    payload ships just the legend rows it actually references)."""
    if categories is None:
        return {cat: list(machines) for cat, machines in CRAFTING_FACILITIES.items()}
    wanted = {str(c) for c in categories}
    return {cat: list(CRAFTING_FACILITIES[cat]) for cat in wanted if cat in CRAFTING_FACILITIES}


def technology_chain_for_recipe(recipe_name: str) -> list[dict[str, Any]]:
    recipe = ALL_RECIPES.get(recipe_name)
    if recipe is None or recipe.technology is None:
        return []
    return technology_chain(recipe.technology)


def objective_research_reference(objective: str = "launch_rocket_program", limit: int = 40) -> dict[str, dict[str, Any]]:
    """{technology: {science_packs, prerequisites, unlocks}} for the objective's tech chain.

    Tells the LLM which RESEARCHABLE TECHNOLOGIES exist (distinct from science-pack ITEMS),
    what each consumes, and its prerequisites -- so a research skill targets a real technology
    (e.g. 'automation'), not a science pack name ('automation-science-pack').
    """
    reference: dict[str, dict[str, Any]] = {}
    for root in objective_roots(objective):
        for tech in technology_chain_for_recipe(root):
            name = tech.get("name")
            if not name or name in reference:
                continue
            reference[name] = {
                "science_packs": tech.get("science_packs") or {},
                "prerequisites": tech.get("prerequisites") or [],
                "unlocks": (tech.get("unlocks") or [])[:6],
            }
            if len(reference) >= limit:
                return reference
    return reference


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


# --------------------------------------------------------------------------- #
# Machine / module / belt PROFILES — the physical specs (crafting speed, power
# draw, footprint, module slots, module effects, belt throughput) the deterministic
# factory-cell compiler needs. Read from the live dump (game_data.json "machines" /
# "modules" / "belts") when present, else from the curated fallbacks below so the
# compiler works before a re-dump. (knowledge.py cannot import monitor.py — circular —
# so the canonical static tables live here; monitor.py keeps its own production-estimate
# copies.) Values are Factorio 2.0 / Space Age vanilla.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MachineProfile:
    name: str
    crafting_speed: float
    energy_kw: float  # active electric draw (0 for burner machines)
    drain_kw: float
    module_slots: int
    tile_width: float
    tile_height: float
    categories: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModuleProfile:
    name: str
    speed: float  # additive fraction, e.g. 0.2 == +20%
    productivity: float
    consumption: float  # additive fraction; negative reduces power
    pollution: float
    tier: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BeltProfile:
    name: str
    items_per_second: float
    tier_rank: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PoleProfile:
    """Electric pole coverage spec used by the placer's power-coverage solver.

    ``supply_radius`` is the half-width of the (square) supply area measured from the pole's
    centre: a machine is powered if its footprint overlaps that square (partial overlap counts).
    ``wire_reach`` is the max centre-to-centre distance to wire-connect two poles into one network.
    """

    name: str
    supply_radius: float  # half supply-area side (tiles)
    wire_reach: float     # max pole-to-pole wire distance (tiles)
    tile_width: float
    tile_height: float
    tier_rank: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Factorio 2.0 vanilla pole coverage. supply_radius = supply_area_distance (half-side of the
# square supply area); wire_reach = maximum_wire_distance.
_FALLBACK_POLE_PROFILES: dict[str, PoleProfile] = {
    "small-electric-pole": PoleProfile("small-electric-pole", 2.5, 7.5, 1, 1, 1),
    "medium-electric-pole": PoleProfile("medium-electric-pole", 3.5, 9.0, 1, 1, 2),
    "big-electric-pole": PoleProfile("big-electric-pole", 2.0, 30.0, 2, 2, 3),
    "substation": PoleProfile("substation", 9.0, 18.0, 2, 2, 4),
}


_FALLBACK_MACHINE_PROFILES: dict[str, MachineProfile] = {
    "assembling-machine-1": MachineProfile("assembling-machine-1", 0.5, 75.0, 2.5, 0, 3, 3),
    "assembling-machine-2": MachineProfile("assembling-machine-2", 0.75, 150.0, 5.0, 2, 3, 3),
    "assembling-machine-3": MachineProfile("assembling-machine-3", 1.25, 375.0, 12.5, 4, 3, 3),
    "stone-furnace": MachineProfile("stone-furnace", 1.0, 0.0, 0.0, 0, 2, 2),
    "steel-furnace": MachineProfile("steel-furnace", 2.0, 0.0, 0.0, 0, 2, 2),
    "electric-furnace": MachineProfile("electric-furnace", 2.0, 180.0, 6.0, 2, 3, 3),
    "electric-mining-drill": MachineProfile("electric-mining-drill", 0.5, 90.0, 0.0, 3, 3, 3),
    "chemical-plant": MachineProfile("chemical-plant", 1.0, 210.0, 7.0, 3, 3, 3),
    "oil-refinery": MachineProfile("oil-refinery", 1.0, 420.0, 14.0, 3, 5, 5),
    "foundry": MachineProfile("foundry", 4.0, 2500.0, 80.0, 4, 5, 5),
    "electromagnetic-plant": MachineProfile("electromagnetic-plant", 2.0, 2000.0, 60.0, 5, 4, 4),
    "centrifuge": MachineProfile("centrifuge", 1.0, 350.0, 12.0, 2, 3, 3),
    "lab": MachineProfile("lab", 1.0, 60.0, 2.0, 2, 3, 3),
}

# Non-crafting entity footprints (tiles) used by the placer for spacing/reserve.
_FALLBACK_FOOTPRINTS: dict[str, tuple[float, float]] = {
    "transport-belt": (1, 1), "fast-transport-belt": (1, 1), "express-transport-belt": (1, 1),
    "underground-belt": (1, 1), "splitter": (2, 1), "inserter": (1, 1),
    "fast-inserter": (1, 1), "long-handed-inserter": (1, 1), "stack-inserter": (1, 1),
    "small-electric-pole": (1, 1), "medium-electric-pole": (1, 1), "big-electric-pole": (2, 2),
    "substation": (2, 2), "pipe": (1, 1), "pipe-to-ground": (1, 1), "beacon": (3, 3),
    "solar-panel": (3, 3), "steam-engine": (3, 5), "boiler": (3, 2),
}

_FALLBACK_MODULE_PROFILES: dict[str, ModuleProfile] = {
    "speed-module": ModuleProfile("speed-module", 0.2, 0.0, 0.5, 0.0, 1),
    "speed-module-2": ModuleProfile("speed-module-2", 0.3, 0.0, 0.6, 0.0, 2),
    "speed-module-3": ModuleProfile("speed-module-3", 0.5, 0.0, 0.7, 0.0, 3),
    "productivity-module": ModuleProfile("productivity-module", -0.05, 0.04, 0.4, 0.05, 1),
    "productivity-module-2": ModuleProfile("productivity-module-2", -0.1, 0.06, 0.6, 0.07, 2),
    "productivity-module-3": ModuleProfile("productivity-module-3", -0.15, 0.10, 0.8, 0.10, 3),
    "efficiency-module": ModuleProfile("efficiency-module", 0.0, 0.0, -0.3, 0.0, 1),
    "efficiency-module-2": ModuleProfile("efficiency-module-2", 0.0, 0.0, -0.4, 0.0, 2),
    "efficiency-module-3": ModuleProfile("efficiency-module-3", 0.0, 0.0, -0.5, 0.0, 3),
}

_FALLBACK_BELT_PROFILES: dict[str, BeltProfile] = {
    "transport-belt": BeltProfile("transport-belt", 15.0, 1),
    "fast-transport-belt": BeltProfile("fast-transport-belt", 30.0, 2),
    "express-transport-belt": BeltProfile("express-transport-belt", 45.0, 3),
    "turbo-transport-belt": BeltProfile("turbo-transport-belt", 60.0, 4),
}


def _w_to_kw(value: Any) -> float | None:
    try:
        return round(float(value) / 1000.0, 4)
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=1)
def _load_profiles() -> tuple[dict[str, MachineProfile], dict[str, ModuleProfile], dict[str, BeltProfile], dict[str, tuple[float, float]], dict[str, PoleProfile]]:
    """Merge dumped profiles (authoritative for our version) over the curated fallbacks."""
    machines = dict(_FALLBACK_MACHINE_PROFILES)
    modules = dict(_FALLBACK_MODULE_PROFILES)
    belts = dict(_FALLBACK_BELT_PROFILES)
    footprints = dict(_FALLBACK_FOOTPRINTS)
    poles = dict(_FALLBACK_POLE_PROFILES)
    try:
        raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return machines, modules, belts, footprints, poles

    for name, md in (raw.get("machines") or {}).items():
        if not isinstance(md, dict):
            continue
        speed = md.get("crafting_speed")
        if speed is None:
            continue  # category-only entry (older dump) — keep the fallback profile
        energy_kw = _w_to_kw(md.get("energy_usage"))
        drain_kw = _w_to_kw(md.get("drain"))
        machines[name] = MachineProfile(
            name=name,
            crafting_speed=float(speed),
            energy_kw=energy_kw if energy_kw is not None else machines.get(name, _FALLBACK_MACHINE_PROFILES.get(name, MachineProfile(name, float(speed), 0.0, 0.0, 0, 3, 3))).energy_kw,
            drain_kw=drain_kw if drain_kw is not None else 0.0,
            module_slots=int(md.get("module_inventory_size") or 0),
            tile_width=float(md.get("tile_width") or 3),
            tile_height=float(md.get("tile_height") or 3),
            categories=tuple(str(c) for c in (md.get("crafting_categories") or ())),
        )
        footprints[name] = (machines[name].tile_width, machines[name].tile_height)

    for name, dd in (raw.get("modules") or {}).items():
        if not isinstance(dd, dict):
            continue
        eff = dd.get("effect") if isinstance(dd.get("effect"), dict) else dd
        modules[name] = ModuleProfile(
            name=name,
            speed=float(eff.get("speed") or 0.0),
            productivity=float(eff.get("productivity") or 0.0),
            consumption=float(eff.get("consumption") or 0.0),
            pollution=float(eff.get("pollution") or 0.0),
            tier=int(dd.get("tier") or 0),
        )

    for name, bd in (raw.get("belts") or {}).items():
        if not isinstance(bd, dict):
            continue
        speed = bd.get("belt_speed")
        ips = float(speed) * 480.0 if speed is not None else bd.get("items_per_second")
        if ips is None:
            continue
        belts[name] = BeltProfile(name=name, items_per_second=float(ips), tier_rank=int(bd.get("tier") or 0))

    for name, fp in (raw.get("footprints") or {}).items():
        if isinstance(fp, dict) and fp.get("tile_width"):
            footprints[name] = (float(fp["tile_width"]), float(fp.get("tile_height") or fp["tile_width"]))

    for name, pd in (raw.get("poles") or {}).items():
        if not isinstance(pd, dict):
            continue
        supply = pd.get("supply_area_distance")
        if supply is None:
            continue
        poles[name] = PoleProfile(
            name=name,
            supply_radius=float(supply),
            wire_reach=float(pd.get("maximum_wire_distance") or pd.get("wire_reach") or 7.5),
            tile_width=float(pd.get("tile_width") or 1),
            tile_height=float(pd.get("tile_height") or 1),
            tier_rank=int(pd.get("tier") or 0),
        )
    return machines, modules, belts, footprints, poles


def machine_profile(name: str) -> MachineProfile | None:
    return _load_profiles()[0].get(name)


def module_profile(name: str) -> ModuleProfile | None:
    return _load_profiles()[1].get(name)


def belt_profile(name: str) -> BeltProfile | None:
    return _load_profiles()[2].get(name)


def all_belt_profiles() -> list[BeltProfile]:
    return sorted(_load_profiles()[2].values(), key=lambda b: b.tier_rank)


def pole_profile(name: str) -> PoleProfile | None:
    return _load_profiles()[4].get(name)


def all_pole_profiles() -> list[PoleProfile]:
    return sorted(_load_profiles()[4].values(), key=lambda p: p.tier_rank)


def entity_footprint(name: str) -> tuple[float, float]:
    """(tile_width, tile_height); machines use their MachineProfile, else the footprint table,
    else a 1x1 default."""
    machines, _modules, _belts, footprints, _poles = _load_profiles()
    if name in machines:
        return (machines[name].tile_width, machines[name].tile_height)
    return footprints.get(name, (1.0, 1.0))


def machines_for_category(category: str | None) -> list[str]:
    """Machines that can craft a category, ordered slowest→fastest (so callers can pick the
    fastest available tier). Uses the dumped machine categories when present, else
    CRAFTING_FACILITIES."""
    if not category:
        return []
    machines, _m, _b, _f, _p = _load_profiles()
    dumped = [name for name, p in machines.items() if category in p.categories]
    names = dumped or list(CRAFTING_FACILITIES.get(category, []))
    return sorted(names, key=lambda n: (machines[n].crafting_speed if n in machines else 0.0, n))
