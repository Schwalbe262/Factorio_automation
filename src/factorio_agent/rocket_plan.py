"""What a verified rocket launch still needs, derived from the live catalog (pure, no IO).

The roadmap's hand-written steps end at chemical science. Everything after it
comes from this module, read from the catalog's own data instead of a flow per
item:

* the technology ``TARGET_TECHNOLOGY`` and its unresearched prerequisite
  closure, with science packs per technology (``unit_count`` x ingredients);
* the rocket silo entity (the catalog entity of type ``rocket-silo``), its
  ``rocket_parts_required``, the item that places it, the recipe it crafts
  (the silo's own crafting category, e.g. ``rocket-part``) and the launch
  payload the launch action uses (``deterministic_launch.LAUNCH_PAYLOAD``);
* the material closure of those items down to raw sources (ores, crude oil,
  water), with continuous amounts, coproduct credit and one generic recipe
  rule: an item a multi-product recipe of the plan already yields is taken
  from that recipe (so petroleum gas comes from the refinery that makes the
  light oil, not from a second basic refinery);
* rates for a launch window (``minutes``), machines per recipe (slowest machine
  of the recipe's category: a conservative first build) and belts per item;
* for every recipe, who can make it: a declared block capability (smelting,
  steel, bricks, plastic/sulfur, refinery, advanced circuits, engines, packs),
  the generic ``produce`` row when the recipe is solid and structurally fits a
  row (``production_layout.plan_sources``), or the missing capability that
  would have to exist (``fluid_assembly``, ``chemical_product``, ...), each with
  status ``roadmap`` / ``wired`` / ``unwired`` / ``missing``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
import math
from typing import Any

from .core.world_catalog import CatalogError, _amount, _material, _yield

TARGET_TECHNOLOGY = "rocket-silo"
# Production window for the silo and one rocket (game minutes): rates and machines are sized for it.
DEFAULT_MINUTES = 60.0
# The roadmap stage the tail follows: production steps wait for it (the research steps
# wait for their packs anyway, ``progression._packs_unsupplied``).
TAIL_ANCHOR = "chemical_science"
# Items per minute of one yellow belt, both lanes (``mine_trunk.BELT_RATE_PER_MINUTE``).
BELT = "transport-belt"
PRODUCE = "produce"
PRODUCE_MIN_RATE = 1.0
PRODUCE_MAX_RATE = 600.0
STAGE_SILO, STAGE_PARTS, STAGE_LAUNCH = "rocket_silo", "rocket_parts", "rocket_launch"
MAX_STEP_ID = 59  # "auto_" + id within the queue id rule ^[A-Za-z0-9_-]{1,64}$

Key = tuple[str, str]


def _key_text(key: Key) -> str:
    return key[1] if key[0] == "item" else f"{key[0]}:{key[1]}"


def _names(value) -> set[str]:
    if isinstance(value, Mapping):
        return {name for name, flag in value.items() if flag}
    if isinstance(value, Iterable) and not isinstance(value, str):
        return {name for name in value if isinstance(name, str)}
    return set()


# ----- the rocket itself ------------------------------------------------------


def silo_entity(catalog) -> tuple[str, dict]:
    """The rocket silo prototype: the entity of type ``rocket-silo`` with a part count."""
    rows = sorted(
        (name, row)
        for name, row in catalog.entities.items()
        if row.get("type") == "rocket-silo" and row.get("rocket_parts_required")
    )
    if not rows:
        raise CatalogError("no rocket silo with rocket_parts_required in the catalog")
    return rows[0]


def silo_item(catalog, silo: Mapping) -> str:
    """The item that places the silo (``items_to_place_this``)."""
    for row in silo.get("items_to_place_this") or ():
        name = row.get("name") if isinstance(row, Mapping) else row
        if isinstance(name, str) and name in catalog.items:
            return name
    raise CatalogError("rocket silo has no placing item")


def part_recipe(catalog, silo: Mapping) -> dict:
    """The recipe the silo crafts (its own category; ``parameters`` is the blueprint category)."""
    categories = set(silo.get("crafting_categories") or ()) - {"parameters"}
    rows = sorted(
        (r for r in catalog.recipes.values() if categories & set(r.get("categories") or ()) and not r.get("hidden")),
        key=lambda r: r["name"],
    )
    if not rows:
        raise CatalogError("no recipe in the rocket silo's crafting category")
    return rows[0]


def launch_payload() -> str | None:
    """The cargo the launch action loads (``core.deterministic_launch``), or None for an empty rocket."""
    from .core.deterministic_launch import LAUNCH_PAYLOAD

    return LAUNCH_PAYLOAD


# ----- technologies -----------------------------------------------------------


def remaining_technologies(catalog, researched: Iterable[str] | None = None, *, target=TARGET_TECHNOLOGY) -> list[dict]:
    """Unresearched technologies of ``target``'s prerequisite closure, prerequisites first.

    ``researched`` (names) replaces the catalog snapshot's own flags when given.
    Each row: name, prerequisites, unit_count, unit_energy, packs {pack: count},
    trigger (a natural milestone instead of packs), unlocks.
    """
    done = set(researched) if researched is not None else None
    rows = []
    for name in catalog.technology_order(target, include_researched=True):
        tech = catalog.technologies[name]
        if (name in done) if done is not None else tech.get("researched"):
            continue
        units = tech.get("unit_count")
        packs: dict[str, float] = {}
        if not tech.get("research_trigger"):
            if not isinstance(units, (int, float)):
                raise CatalogError(f"research unit count unavailable: {name}")
            for ingredient in tech.get("ingredients") or ():
                packs[ingredient["name"]] = packs.get(ingredient["name"], 0.0) + _amount(ingredient["amount"]) * units
        rows.append(
            {
                "name": name,
                "prerequisites": sorted(tech.get("prerequisites") or ()),
                "unit_count": units,
                "unit_energy": tech.get("unit_energy"),
                "packs": packs,
                "trigger": tech.get("research_trigger") or None,
                "unlocks": list(tech.get("unlocks") or ()),
            }
        )
    return rows


def research_order(technologies: list[dict], recipe_depth: Mapping[str, int]) -> list[str]:
    """Prerequisites first; among researchable ones, the one unlocking the deepest bill recipe first.

    ``recipe_depth``: recipe -> position in the production order (raw-near first),
    so research that lets the earliest production start is done first and the
    factory grows while the rest is researched. Pure prerequisites come last
    among equals; ties by name.
    """
    names = {row["name"]: row for row in technologies}
    pending = {name: {p for p in row["prerequisites"] if p in names} for name, row in names.items()}
    big = len(recipe_depth) + 1

    def rank(name: str) -> tuple:
        depths = [recipe_depth[r] for r in names[name]["unlocks"] if r in recipe_depth]
        return (min(depths) if depths else big, name)

    order: list[str] = []
    while pending:
        ready = sorted((name for name, parents in pending.items() if not parents), key=rank)
        if not ready:
            raise CatalogError("technology prerequisite cycle in the rocket closure")
        name = ready[0]
        order.append(name)
        del pending[name]
        for parents in pending.values():
            parents.discard(name)
    return order


# ----- material flow ----------------------------------------------------------


def _choices(catalog, targets: Mapping[Key, float]) -> tuple[list[Key], dict[Key, dict]]:
    """Topological product order and recipe per product, with the coproduct rule (module doc)."""
    _, first = catalog._graph(targets)
    multi = sorted(
        {r["name"]: r for r in first.values() if len(r["products"]) > 1}.values(),
        key=lambda r: r["name"],
    )
    raw = catalog._raw

    def pick(key: Key) -> dict | None:
        for recipe in multi:
            if any(_material(p) == key for p in recipe["products"]) and not any(
                _material(i) == key for i in recipe["ingredients"]
            ):
                return recipe
        return catalog.recipe_for_product(key[1], product_type=key[0])

    order: list[Key] = []
    choices: dict[Key, dict] = {}
    visited: set[Key] = set()
    visiting: set[Key] = set()

    def visit(key: Key) -> None:
        if key in visiting:
            raise CatalogError(f"recipe dependency cycle: {key[0]}:{key[1]}")
        if key in visited:
            return
        visiting.add(key)
        recipe = None if key in raw else pick(key)
        if recipe is not None:
            choices[key] = recipe
            for ingredient in sorted(recipe["ingredients"], key=_material):
                visit(_material(ingredient))
        elif key not in raw:
            raise CatalogError(f"no local recipe or extraction source: {key[0]}:{key[1]}")
        visiting.remove(key)
        visited.add(key)
        order.append(key)

    for key in sorted(targets):
        visit(key)
    return order, choices


def material_flow(catalog, targets: Mapping[Key, float]) -> dict:
    """Continuous amounts of every product in the closure of ``targets`` (same recipe choices for every call).

    Returns ``order`` (ingredients first), ``choices`` (key -> recipe),
    ``amounts`` (key -> gross amount made or mined), ``crafts`` (recipe -> crafts)
    and ``surplus`` (coproducts above the need).
    """
    order, choices = _choices(catalog, targets)
    need: dict[Key, float] = defaultdict(float, targets)
    made: dict[Key, float] = defaultdict(float)
    crafts: dict[str, float] = defaultdict(float)
    for key in reversed(order):
        if key not in choices:
            continue
        missing = need[key] - made[key]
        if missing <= 1e-9:
            continue
        recipe = choices[key]
        output = sum(_yield(p) for p in recipe["products"] if _material(p) == key)
        if output <= 0:
            raise CatalogError(f"zero recipe yield: {recipe['name']}")
        count = missing / output
        crafts[recipe["name"]] += count
        for product in recipe["products"]:
            made[_material(product)] += _yield(product) * count
        for ingredient in recipe["ingredients"]:
            need[_material(ingredient)] += _amount(ingredient["amount"]) * count
    amounts = {key: (made[key] if key in choices else need[key]) for key in order}
    surplus = {key: made[key] - need[key] for key in list(made) if made[key] - need[key] > 1e-6}
    return {"order": order, "choices": choices, "amounts": amounts, "crafts": dict(crafts), "surplus": surplus}


def machine_for(catalog, recipe: Mapping) -> tuple[str, float] | None:
    """Slowest non-character crafting machine of the recipe's category (a first build, conservative)."""
    categories = set(recipe.get("categories") or ())
    rows = [
        (float(row.get("crafting_speed") or 0), name)
        for name, row in catalog.entities.items()
        if row.get("type") in {"assembling-machine", "furnace", "rocket-silo"}
        and categories & set(row.get("crafting_categories") or ())
        and (row.get("crafting_speed") or 0) > 0
    ]
    if not rows:
        return None
    speed, name = min(rows)
    return name, speed


def _has_fluid(recipe: Mapping) -> bool:
    return any(r.get("type") == "fluid" for r in list(recipe["ingredients"]) + list(recipe["products"]))


# ----- producers ---------------------------------------------------------------


def _capability_products(catalog) -> dict[str, dict]:
    """Recipe -> the block capability that makes it, as each capability declares it.

    ``belt``: the block publishes a belt (or pipe) output port another block can
    route from; a produce row and the engine row end in a stock chest.
    ``module``: where the controller lives (an unregistered one is ``unwired``).
    """
    table: dict[str, dict] = {}

    def add(recipe: str | None, capability: str, parameters=None, *, belt=True, module=None):
        if recipe and recipe in catalog.recipes and recipe not in table:
            table[recipe] = {
                "capability": capability,
                "parameters": dict(parameters or {}),
                "belt": belt,
                "module": module or capability,
            }

    def recipe_of(product: str, kind: str = "item") -> str | None:
        row = catalog.recipe_for_product(product, product_type=kind)
        return row["name"] if row else None

    for plate in ("iron-plate", "copper-plate"):  # site/planmodel.py feed_resources -> district plate ports
        add(recipe_of(plate), "smelting_district", module="smelting_site")
    add(recipe_of("steel-plate"), "steel_production", module="planned_furnaces")
    add(recipe_of("stone-brick"), "brick_production", module="planned_furnaces")
    add(recipe_of("engine-unit"), "engine_production", belt=False, module="planned_products")
    add(recipe_of("advanced-circuit"), "advanced_circuit_production", module="advanced_circuits")
    from .normal_research import PACK_PRODUCERS

    for pack, capability in sorted(PACK_PRODUCERS.items()):
        add(recipe_of(pack), capability, belt=False, module=None)
    from .chemical_production import PRODUCTS as CHEMICAL_PRODUCTS

    for product in sorted(CHEMICAL_PRODUCTS):
        add(recipe_of(product), "chemical_production", {"product": product}, module="chemical_production")
    from .petroleum import basic_petroleum_rates  # noqa: F401  (the module exists: basic refining)

    add("basic-oil-processing", "basic_petroleum", module="petroleum")
    from .advanced_petroleum import RECIPES as ADVANCED_RECIPES

    for recipe in ADVANCED_RECIPES:
        add(recipe, "advanced_petroleum", module="advanced_petroleum")
    from .fluid_products import PRODUCTS as FLUID_PRODUCTS

    for product in sorted(FLUID_PRODUCTS):
        add(recipe_of(product, "fluid"), "fluid_product", {"product": product}, module="fluid_products")
    return table


def _roadmap_capabilities() -> set[tuple[str, str]]:
    from .progression import ROADMAP

    return {(step.capability, repr(sorted(step.parameters.items()))) for step in ROADMAP}


def _status(capability: str, parameters: Mapping, capabilities: Mapping, module: str | None) -> str:
    if capability in capabilities:
        if (capability, repr(sorted(parameters.items()))) in _roadmap_capabilities():
            return "roadmap"
        return "wired"
    if module:
        import importlib.util

        if importlib.util.find_spec(f"{__package__}.{module}") is not None:
            return "unwired"
    return "missing"


def _raw_producer(catalog, key: Key) -> dict:
    if key[0] == "item":
        return {"capability": "mining_outpost", "parameters": {"resource": key[1]}, "kind": "raw", "belt": True}
    resource = any(
        row.get("type") == "resource" and any(_material(p) == key for p in row.get("mining_products") or ())
        for row in catalog.entities.values()
    )
    capability = "oil_gathering" if resource else "water_gathering"
    return {"capability": capability, "parameters": {}, "kind": "raw", "belt": True}


def classify(catalog, flow: Mapping, *, silo: Mapping, silo_name: str, payload: str | None) -> dict[Key, dict]:
    """Producer per product of ``flow`` (module doc), in production order."""
    declared = _capability_products(catalog)
    hand = set((catalog.entities.get("character") or {}).get("crafting_categories") or ())
    parts = part_recipe(catalog, silo)["name"]
    placing = silo_item(catalog, silo)
    out: dict[Key, dict] = {}
    belt_sources: set[str] = set()
    for key in flow["order"]:
        recipe = flow["choices"].get(key)
        if recipe is None:
            row = _raw_producer(catalog, key)
        elif recipe["name"] in declared:
            row = dict(declared[recipe["name"]], kind="block")
        elif recipe["name"] == parts:
            row = {"capability": STAGE_PARTS, "parameters": {}, "kind": "stage", "belt": False}
            row["note"] = f"crafted inside {silo_name} from its input inventory"
        elif key == ("item", placing) or key == ("item", payload):
            stage = STAGE_SILO if key == ("item", placing) else STAGE_LAUNCH
            crafted = "hand-crafted from stock" if hand & set(recipe["categories"]) else "needs an assembler"
            row = {"capability": stage, "parameters": {}, "kind": "stage", "belt": False, "note": crafted}
        else:
            row = _generic_producer(catalog, recipe, key, belt_sources)
        if row.get("belt") and key[0] == "item":
            belt_sources.add(key[1])
        row.setdefault("module", row["capability"])
        out[key] = row
    return out


def _generic_producer(catalog, recipe: Mapping, key: Key, belt_sources: set[str]) -> dict:
    """A recipe no block capability declares: the generic row, or the capability that is missing."""
    machine = machine_for(catalog, recipe)
    machine_row = catalog.entities.get(machine[0], {}) if machine else {}
    if _has_fluid(recipe):
        assembler = "crafting" in set(machine_row.get("crafting_categories") or ())
        if assembler:
            return {
                "capability": "fluid_assembly",
                "parameters": {"item": key[1]},
                "kind": "generic",
                "belt": False,
                "note": "assembler row with a typed pipe input (no such row exists)",
            }
        return {
            "capability": "chemical_product",
            "parameters": {"product": key[1]},
            "kind": "generic",
            "belt": key[0] == "fluid",
            "note": "chemical plant cell for a product chemical_production / fluid_products do not declare",
        }
    if machine_row.get("type") == "furnace":
        return {"capability": "furnace_block", "parameters": {"item": key[1]}, "kind": "generic", "belt": True}
    from .production_layout import plan_sources

    try:
        plan_sources(catalog, recipe["name"], lambda item: item in belt_sources)
    except ValueError as exc:
        return {
            "capability": "produce_merged",
            "parameters": {"item": key[1]},
            "kind": "generic",
            "belt": False,
            "note": f"no produce row fits: {str(exc)[:160]}",
        }
    return {"capability": PRODUCE, "parameters": {"item": key[1]}, "kind": "generic", "belt": False}


# ----- the bill ------------------------------------------------------------------


def _rows(amounts: Mapping[Key, float]) -> dict[str, float]:
    return {_key_text(k): round(v, 3) for k, v in sorted(amounts.items()) if v > 1e-9}


def rocket_bill(
    catalog,
    *,
    researched: Iterable[str] | None = None,
    minutes: float = DEFAULT_MINUTES,
    rockets: int = 1,
    capabilities: Mapping | None = None,
    bus_lanes: Iterable[str] | None = None,
) -> dict:
    """The machine-readable bill of a verified rocket launch (module doc).

    ``minutes``: production window for the silo and ``rockets`` rockets (rates,
    machines, belts); research packs are counted separately (``science_packs``)
    and their rate is given for the same window. ``capabilities``: the registered
    capability contracts (default ``jobs.CAPABILITIES``). ``bus_lanes``: items
    the site plan reserves bus lanes for (default ``site.planmodel.SiteConfig``).
    """
    if not (isinstance(minutes, (int, float)) and minutes > 0) or type(rockets) is not int or rockets < 1:
        raise ValueError("minutes must be positive and rockets a positive integer")
    if capabilities is None:
        from .jobs import CAPABILITIES as capabilities  # noqa: N811
    if bus_lanes is None:
        from .site.planmodel import SiteConfig

        bus_lanes = SiteConfig().lane_order
    lanes: dict[str, int] = defaultdict(int)
    for item in bus_lanes:
        lanes[item] += 1
    silo_name, silo = silo_entity(catalog)
    parts_required = int(silo["rocket_parts_required"])
    placing = silo_item(catalog, silo)
    part = part_recipe(catalog, silo)
    part_item = next(_material(p) for p in part["products"])
    payload = launch_payload()
    silo_targets = {("item", placing): 1.0}
    rocket_targets: dict[Key, float] = {part_item: float(parts_required * rockets)}
    if payload:
        rocket_targets[("item", payload)] = float(rockets)
    total_targets = dict(silo_targets)
    for key, value in rocket_targets.items():
        total_targets[key] = total_targets.get(key, 0.0) + value
    total = material_flow(catalog, total_targets)
    silo_flow = material_flow(catalog, silo_targets)
    rocket_flow = material_flow(catalog, rocket_targets)

    technologies = remaining_technologies(catalog, researched)
    depth = {}
    for index, key in enumerate(total["order"]):
        if key in total["choices"]:
            depth.setdefault(total["choices"][key]["name"], index)
    order = research_order(technologies, depth)
    by_name = {row["name"]: row for row in technologies}
    packs: dict[str, float] = defaultdict(float)
    for row in technologies:
        for pack, count in row["packs"].items():
            packs[pack] += count
    unlocked_by = {}
    for row in technologies:
        for recipe in row["unlocks"]:
            unlocked_by.setdefault(recipe, row["name"])

    producers = classify(catalog, total, silo=silo, silo_name=silo_name, payload=payload)
    belt_rate = _belt_rate()
    consumers: dict[Key, set[str]] = defaultdict(set)
    for recipe_key, recipe in total["choices"].items():
        for ingredient in recipe["ingredients"]:
            consumers[_material(ingredient)].add(recipe["name"])
    items = []
    for key in total["order"]:
        recipe = total["choices"].get(key)
        amount = total["amounts"].get(key, 0.0)
        rate = amount / minutes
        producer = producers[key]
        row: dict[str, Any] = {
            "item": key[1],
            "type": key[0],
            "silo": round(silo_flow["amounts"].get(key, 0.0), 3),
            "per_rocket": round(rocket_flow["amounts"].get(key, 0.0) / rockets, 3),
            "total": round(amount, 3),
            "rate_per_minute": round(rate, 3),
            "consumers": sorted(consumers.get(key, ())),
            "producer": {
                "capability": producer["capability"],
                "parameters": producer["parameters"],
                "kind": producer["kind"],
                "status": _status(producer["capability"], producer["parameters"], capabilities, producer["module"]),
                **({"note": producer["note"]} if producer.get("note") else {}),
            },
        }
        if recipe is not None:
            crafts = total["crafts"].get(recipe["name"], 0.0)
            machine = machine_for(catalog, recipe)
            row["recipe"] = recipe["name"]
            row["category"] = recipe["category"]
            row["fluid"] = _has_fluid(recipe)
            row["technology"] = unlocked_by.get(recipe["name"])
            if machine:
                row["machine"] = machine[0]
                row["machines"] = round(crafts / minutes * float(recipe["energy"]) / 60.0 / machine[1], 2)
        if key[0] == "item":
            row["belts"] = max(1, math.ceil(rate / belt_rate - 1e-9)) if rate > 0 else 0
            row["bus_lanes_reserved"] = lanes.get(key[1], 0)
        items.append(row)
    bus = [
        {
            "item": r["item"],
            "rate_per_minute": r["rate_per_minute"],
            "belts": r["belts"],
            "reserved": r["bus_lanes_reserved"],
        }
        for r in items
        if r["type"] == "item" and len(r["consumers"]) >= 2 and producers[("item", r["item"])].get("belt")
    ]
    gaps = sorted(
        {
            (r["producer"]["capability"], r["producer"]["status"])
            for r in items
            if r["producer"]["status"] in ("unwired", "missing")
        }
    )
    return {
        "target_technology": TARGET_TECHNOLOGY,
        "minutes": float(minutes),
        "rockets": rockets,
        "silo_entity": silo_name,
        "silo_item": placing,
        "parts_per_rocket": parts_required,
        "part_recipe": {
            "name": part["name"],
            "energy": part["energy"],
            "ingredients": {_key_text(_material(i)): _amount(i["amount"]) for i in part["ingredients"]},
        },
        "silo_recipe": _recipe_row(catalog, placing),
        "payload": payload,
        "payload_recipe": _recipe_row(catalog, payload) if payload else None,
        "technologies": [by_name[name] for name in order],
        "science_packs": dict(sorted(packs.items())),
        "pack_types": sorted(packs),
        "items": items,
        "raw": _rows({k: v for k, v in total["amounts"].items() if k not in total["choices"]}),
        "surplus": _rows(total["surplus"]),
        "bus": bus,
        "gaps": [{"capability": c, "status": s, "items": _gap_items(items, c)} for c, s in gaps],
        "assumptions": [
            "normal quality, no productivity bonus",
            "continuous amounts (no integer batches)",
            "factory construction and operating fuel excluded",
            "machine counts at the slowest machine of each category",
            "an item a multi-product recipe of the plan yields comes from that recipe",
            "no cracking: coproducts above the need are reported as surplus",
        ],
        "catalog_fingerprint": catalog.fingerprint,
    }


def _gap_items(items: list[dict], capability: str) -> list[str]:
    return [r["item"] for r in items if r["producer"]["capability"] == capability]


def _recipe_row(catalog, item: str) -> dict | None:
    recipe = catalog.recipe_for_product(item)
    if recipe is None:
        return None
    return {
        "name": recipe["name"],
        "category": recipe["category"],
        "energy": recipe["energy"],
        "ingredients": {_key_text(_material(i)): _amount(i["amount"]) for i in recipe["ingredients"]},
    }


def _belt_rate() -> float:
    from .mine_trunk import BELT_RATE_PER_MINUTE

    return BELT_RATE_PER_MINUTE[BELT]
