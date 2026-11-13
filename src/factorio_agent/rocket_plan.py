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
  row (``production_layout.plan_sources``, three belt-fed solids through the
  lane merge), or the missing capability that would have to exist
  (``fluid_assembly``, ``chemical_product``, ...), each with status ``roadmap`` /
  ``wired`` / ``unwired`` / ``missing``; a ``produce`` row is a belt-output row
  (``output: belt``) when a consumer that cannot make the item in-line takes it
  (``classify``), otherwise it ends in the stock chest the rocket stages collect from.

``tail(plan)`` turns the bill into roadmap step rows (research, production,
silo, parts, launch); ``progression.roadmap_for`` appends them to ``ROADMAP``
from the observe receipt ``rocket_plan`` (``receipt``). A step whose
capability is not registered is listed but never proposed, so the tail grows
by itself when a missing capability is added.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
import math
import re
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


def cracking_chain(catalog, refinery: Mapping) -> list[dict]:
    """Cracking recipes between the fluid coproducts of ``refinery``, heaviest first.

    A cracking recipe here: one coproduct in (plus raw water), one other coproduct out
    (``heavy-oil-cracking``, ``light-oil-cracking`` in vanilla). Ordered so each stage
    feeds the next (heavy -> light -> gas)."""
    products = {_material(p) for p in refinery["products"] if p.get("type") == "fluid"}
    raw = catalog._raw
    found = {}
    for recipe in catalog.recipes.values():
        if recipe.get("hidden") or len(recipe["products"]) != 1:
            continue
        ins = [_material(i) for i in recipe["ingredients"]]
        out = _material(recipe["products"][0])
        fed = [k for k in ins if k in products]
        if out in products and len(fed) == 1 and fed[0] != out and all(k in products or k in raw for k in ins):
            found.setdefault(fed[0], recipe)
    chain, seen = [], set()
    starts = [k for k in found if not any(_material(r["products"][0]) == k for r in found.values())]
    for key in sorted(starts):
        while key in found and key not in seen:
            seen.add(key)
            chain.append(found[key])
            key = _material(found[key]["products"][0])
    return chain


def crack_balance(catalog, flow: dict) -> dict:
    """Crack the multi-product refinery's surplus down the chain instead of reporting it (in place).

    The refinery runs only as many crafts as the binding product needs once every
    surplus of a heavier product is cracked into the next one; what is cracked is
    sized from the bill (``cracking`` recipe -> crafts). A coproduct that still has
    surplus after the chain (gas when heavy or light demand binds) is reported in
    ``surplus``: it would back the refinery up. Raw inputs (crude, water) follow."""
    multi = [r for r in {r["name"]: r for r in flow["choices"].values()}.values() if len(r["products"]) > 1]
    flow.setdefault("cracking", {})
    for refinery in multi:
        chain = cracking_chain(catalog, refinery)
        if not chain:
            continue
        yields = {_material(p): _yield(p) for p in refinery["products"]}
        made = {k: v * flow["crafts"].get(refinery["name"], 0.0) for k, v in yields.items()}
        need = {k: max(0.0, made[k] - flow["surplus"].get(k, 0.0)) for k in yields}

        def run(crafts: float):
            have = {k: v * crafts for k, v in yields.items()}
            cracked = {}
            for recipe in chain:
                fed = next(_material(i) for i in recipe["ingredients"] if _material(i) in yields)
                per = next(_amount(i["amount"]) for i in recipe["ingredients"] if _material(i) == fed)
                extra = max(0.0, have[fed] - need[fed])
                count = extra / per
                cracked[recipe["name"]] = count
                have[fed] -= extra
                out = recipe["products"][0]
                have[_material(out)] += _yield(out) * count
            return have, cracked

        old = flow["crafts"].get(refinery["name"], 0.0)
        if old <= 0:
            continue
        lo, hi = 0.0, old
        for _ in range(80):
            mid = (lo + hi) / 2
            have, _ = run(mid)
            if all(have[k] + 1e-9 >= need[k] for k in yields):
                hi = mid
            else:
                lo = mid
        have, cracked = run(hi)
        flow["crafts"][refinery["name"]] = hi
        for key, value in yields.items():
            if key in flow["amounts"] or have[key] > 1e-9:
                flow["amounts"][key] = need[key] + max(0.0, have[key] - need[key])
            surplus = have[key] - need[key]
            if surplus > 1e-6:
                flow["surplus"][key] = surplus
            else:
                flow["surplus"].pop(key, None)
        delta = {}
        for ingredient in refinery["ingredients"]:
            key = _material(ingredient)
            delta[key] = delta.get(key, 0.0) + _amount(ingredient["amount"]) * (hi - old)
        for recipe in chain:
            count = cracked[recipe["name"]]
            flow["crafts"][recipe["name"]] = count
            flow["cracking"][recipe["name"]] = count
            for ingredient in recipe["ingredients"]:
                key = _material(ingredient)
                if key not in yields:
                    delta[key] = delta.get(key, 0.0) + _amount(ingredient["amount"]) * count
        for key, change in delta.items():
            if key not in flow["choices"]:  # raw sources only (crude, water)
                flow["amounts"][key] = max(0.0, flow["amounts"].get(key, 0.0) + change)
    return flow


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


# Research a block needs beyond its recipes' unlocks (advanced_petroleum: the crackers' circuit control).
EXTRA_RESEARCH = {"advanced_petroleum": ("advanced-oil-processing", "circuit-network")}


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
    """Producer per product of ``flow`` (module doc), in production order.

    A ``produce`` row ends in a stock chest unless a consumer that cannot make the
    item in-line needs it on a belt (``_belt_needs``): then it is a belt-output row
    (``output: belt``) and a belt source for the rows after it. Repeated until stable.
    """
    belt_items: set[str] = set()
    out: dict[Key, dict] = {}
    for _ in range(8):
        out = _classify(catalog, flow, silo=silo, silo_name=silo_name, payload=payload, belt_items=belt_items)
        more = _belt_needs(flow, out) - belt_items
        if not more:
            break
        belt_items |= more
    return out


def _belt_needs(flow: Mapping, producers: Mapping[Key, dict]) -> set[str]:
    """Stock-row items a non-``produce`` generic consumer (a row that cannot sub-row them) takes."""
    needs = set()
    for key, row in producers.items():
        if row["kind"] != "generic" or row["capability"] == PRODUCE:
            continue
        for ingredient in flow["choices"][key]["ingredients"]:
            source = producers.get(_material(ingredient))
            if source is None or ingredient.get("type", "item") != "item" or source.get("belt"):
                continue
            if source["capability"] == PRODUCE or source["kind"] == "block":
                # A stock-only producer (a stock row, the engine row's chest): a belt row is needed.
                needs.add(ingredient["name"])
    return needs


def _classify(catalog, flow: Mapping, *, silo, silo_name, payload, belt_items: set[str]) -> dict[Key, dict]:
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
            if key[1] in belt_items and not row["belt"] and key[0] == "item":
                # The block keeps its stock chest; a belt-output produce row of the same item feeds
                # the consumers that need a belt (production_rows.row_key: a separate instance).
                row.update(
                    belt=True, belt_row={"capability": PRODUCE, "parameters": {"item": key[1], "output": "belt"}}
                )
        elif recipe["name"] == parts:
            row = {"capability": STAGE_PARTS, "parameters": {}, "kind": "stage", "belt": False}
            row["note"] = f"crafted inside {silo_name} from its input inventory"
        elif key == ("item", placing) or key == ("item", payload):
            stage = STAGE_SILO if key == ("item", placing) else STAGE_LAUNCH
            crafted = "hand-crafted from stock" if hand & set(recipe["categories"]) else "needs an assembler"
            row = {"capability": stage, "parameters": {}, "kind": "stage", "belt": False, "note": crafted}
        else:
            row = _generic_producer(catalog, recipe, key, belt_sources, belt_output=key[1] in belt_items)
        if row.get("belt") and key[0] == "item":
            belt_sources.add(key[1])
        row.setdefault("module", row["capability"])
        out[key] = row
    return out


def _generic_producer(catalog, recipe: Mapping, key: Key, belt_sources: set[str], *, belt_output=False) -> dict:
    """A recipe no block capability declares: the generic row, or the capability that is missing.

    A ``produce`` row carries its ``layout`` (input mode from ``plan_sources``, output mode)."""
    machine = machine_for(catalog, recipe)
    machine_row = catalog.entities.get(machine[0], {}) if machine else {}
    from .blocks.sizing import fluid_ingredient

    fluid = fluid_ingredient(recipe)
    if fluid is not None and _fluid_row_machine(catalog, recipe):
        # A fluid row: the generic produce row with a typed header port (blocks.row fluid rows).
        return _produce_row(catalog, recipe, key, belt_sources, belt_output, fluid=fluid)
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
    return _produce_row(catalog, recipe, key, belt_sources, belt_output)


def _produce_row(catalog, recipe, key, belt_sources, belt_output, *, fluid=None) -> dict:
    from .production_layout import plan_sources

    try:
        plan = plan_sources(catalog, recipe["name"], lambda item: item in belt_sources)
    except ValueError as exc:
        return {
            "capability": "produce_unfit",
            "parameters": {"item": key[1]},
            "kind": "generic",
            "belt": False,
            "note": f"no produce row fits: {str(exc)[:160]}",
        }
    parameters = {"item": key[1], **({"output": "belt"} if belt_output else {})}
    layout = {"inputs": _input_mode(plan), "output": "belt" if belt_output else "stock_chest"}
    row = {"capability": PRODUCE, "parameters": parameters, "kind": "generic", "belt": belt_output, "layout": layout}
    if fluid:
        layout["fluid"] = fluid
        parameters["fluid_source"] = fluid_source_key(key[1], fluid)
        row["note"] = "fluid row: typed pipe from fluid_source to the header port (production_fluids)"
    return row


def _fluid_row_machine(catalog, recipe) -> bool:
    """An electric assembling machine of the recipe's category with an input fluid box
    (a catalog without fluid box data counts: the row generator checks it again)."""
    from .blocks.sizing import input_fluid_boxes

    categories = set(recipe.get("categories") or ())
    return any(
        row.get("type") == "assembling-machine"
        and categories & set(row.get("crafting_categories") or ())
        and (input_fluid_boxes(row) or "fluidbox_prototypes" not in row)
        for row in catalog.entities.values()
    )


def fluid_source_key(item: str, fluid: str) -> str:
    """The registry key a fluid row reads its fluid from (published by ``FluidSupply``)."""
    return re.sub(r"[^a-z0-9-]", "-", f"{item}-{fluid}".lower())[:48]


def _input_mode(plan: Mapping) -> str:
    """How a produce row gets its solid inputs (``production_layout.plan_sources``)."""
    if plan.get("merge"):
        return plan["merge"]
    if plan.get("twin_item"):
        return f"twin_sub_rows:{plan['sub_item']}+{plan['twin_item']}"
    if plan.get("sub_item"):
        return f"sub_row:{plan['sub_item']}"
    return "belts"


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
    total = crack_balance(catalog, material_flow(catalog, total_targets))
    silo_flow = crack_balance(catalog, material_flow(catalog, silo_targets))
    rocket_flow = crack_balance(catalog, material_flow(catalog, rocket_targets))

    producers = classify(catalog, total, silo=silo, silo_name=silo_name, payload=payload)
    technologies = remaining_technologies(catalog, researched)
    known = {row["name"] for row in technologies}
    for capability in sorted({row["capability"] for row in producers.values()}):
        for target in EXTRA_RESEARCH.get(capability, ()):
            if target not in catalog.technologies:
                continue
            for row in remaining_technologies(catalog, researched, target=target):
                if row["name"] not in known:
                    known.add(row["name"])
                    technologies.append(row)
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
                **({"layout": producer["layout"]} if producer.get("layout") else {}),
                **({"belt_row": producer["belt_row"]} if producer.get("belt_row") else {}),
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
            # Bus lane declaration by demand: one site-plan lane per belt of the item's rate,
            # one tap per consuming recipe (the planned bus taps each consumer, planned_tap).
            "lanes_needed": r["belts"],
            "lanes_short": max(0, r["belts"] - r["bus_lanes_reserved"]),
            "taps": len(r["consumers"]),
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
        "cracking": _cracking_rows(catalog, total, minutes),
        "bus": bus,
        "gaps": [{"capability": c, "status": s, "items": _gap_items(items, c)} for c, s in gaps],
        "assumptions": [
            "normal quality, no productivity bonus",
            "continuous amounts (no integer batches)",
            "factory construction and operating fuel excluded",
            "machine counts at the slowest machine of each category",
            "an item a multi-product recipe of the plan yields comes from that recipe",
            "refinery coproducts above the need are cracked down the chain (crack_balance); what is left is surplus",
        ],
        "catalog_fingerprint": catalog.fingerprint,
    }


def _cracking_rows(catalog, flow: Mapping, minutes: float) -> list[dict]:
    """Cracking stages sized from the bill, with machines and whether the advanced block's plants carry them."""
    rows = []
    try:
        from .advanced_petroleum import advanced_petroleum_rates

        block = advanced_petroleum_rates(catalog)
    except (KeyError, ValueError, TypeError):
        block = None
    capacity = {}
    if block is not None:
        capacity = {
            "heavy-oil-cracking": block["heavy_cracking_crafts_required"],
            "light-oil-cracking": block["light_cracking_crafts_required"],
        }
    for name, crafts in sorted(flow.get("cracking", {}).items()):
        recipe = catalog.recipes[name]
        machine = machine_for(catalog, recipe)
        per_minute = crafts / minutes
        row = {"recipe": name, "crafts": round(crafts, 3), "crafts_per_minute": round(per_minute, 3)}
        if machine:
            row["machine"] = machine[0]
            row["machines"] = round(per_minute * float(recipe["energy"]) / 60.0 / machine[1], 2)
        if name in capacity:
            # The block's single cracker of this stage runs at most the refinery's full byproduct rate.
            row["block_crafts_per_minute"] = round(capacity[name], 3)
            row["fits_block"] = per_minute <= capacity[name] + 1e-9
        rows.append(row)
    return rows


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


# ----- roadmap tail ------------------------------------------------------------------


def step_id(*parts: str) -> str:
    """A node-id-safe step id (``auto_`` + id stays within the 64-character queue id rule)."""
    text = "_".join(parts)
    return re.sub(r"[^a-z0-9_]", "_", text.lower())[:MAX_STEP_ID]


def research_id(technology: str) -> str:
    return "research_" + technology.replace("-", "_")


def roadmap_research(steps=None) -> dict[str, str]:
    """Technology -> id of the roadmap step that researches it (``technology`` parameter or ``STEP_TECHNOLOGY``)."""
    from .progression import ROADMAP, STEP_TECHNOLOGY

    found: dict[str, str] = {}
    for step in ROADMAP if steps is None else steps:
        name = step.parameters.get("technology") or STEP_TECHNOLOGY.get(step.capability)
        if isinstance(name, str):
            found.setdefault(name, step.id)
    return found


def tail(bill: Mapping, *, existing: Iterable[str] = (), researched_by: Mapping[str, str] | None = None) -> list[dict]:
    """Roadmap step rows after ``TAIL_ANCHOR``: research, production, silo, parts, launch.

    Rows: ``{"id", "capability", "parameters", "after"}``. ``existing``: step ids
    already in the roadmap (``after`` may name them); ``researched_by``:
    technology -> the roadmap step researching it (``roadmap_research``), never
    repeated. Trigger technologies are milestones, not research rows. Research
    rows follow ``bill["technologies"]`` (``research_order``), each after the
    anchor and its prerequisites' steps. Production rows follow the
    production order (ingredients first) for every item whose producer is the
    generic ``produce`` row or a capability that is not a block of the roadmap
    (``fluid_assembly``, ``chemical_product``, ``advanced_petroleum``, ...),
    after the anchor and the research that unlocks the recipe. ``produce`` rates
    come from the bill (total over ``minutes``) in ``[PRODUCE_MIN_RATE,
    PRODUCE_MAX_RATE]``. Raw sources and roadmap blocks get no row (their growth
    rules size them from claims).
    """
    known = set(existing)
    by_tech = dict(researched_by or {})
    rows: list[dict] = []

    def add(row: dict) -> None:
        if row["id"] in known:
            return
        known.add(row["id"])
        rows.append(row)

    for tech in bill["technologies"]:
        if tech["trigger"] or tech["name"] in by_tech:
            continue
        after = sorted({by_tech[p] for p in tech["prerequisites"] if by_tech.get(p) in known})
        if TAIL_ANCHOR in known and TAIL_ANCHOR not in after:
            # The lab lane belongs to the hand-written roadmap until its last stage; idle labs
            # may still research these earlier (research_policy.choose_research).
            after.insert(0, TAIL_ANCHOR)
        node = research_id(tech["name"])
        add({"id": node, "capability": "research", "parameters": {"technology": tech["name"]}, "after": after})
        by_tech[tech["name"]] = node
    produced: dict[str, str] = {}
    makers = {r["recipe"]: r["producer"] for r in bill["items"] if r.get("recipe")}
    supply = FluidSupply()
    for row in bill["items"]:
        producer = row["producer"]
        if producer.get("belt_row"):
            # A stock-only block's item a consumer needs on a belt: its belt-output produce row.
            belt = producer["belt_row"]
            after = [TAIL_ANCHOR] + (
                [by_tech[row["technology"]]] if by_tech.get(row.get("technology")) in known else []
            )
            node = step_id("rocket", "produce", row["item"], "belt")
            parameters = dict(belt["parameters"], rate_per_minute=_produce_rate(row))
            add({"id": node, "capability": belt["capability"], "parameters": parameters, "after": after})
            produced[row["item"]] = node
        if producer["status"] == "roadmap" or producer["kind"] not in ("generic", "block"):
            continue
        if producer["kind"] == "block":
            # A registered block the hand-written roadmap does not build (advanced_petroleum, fluid_product).
            node = step_id("rocket", producer["capability"], *map(str, producer["parameters"].values()))
            produced[row["item"]] = node
            if node in known:
                continue
            after = [TAIL_ANCHOR]
            tech = row.get("technology")
            if tech and by_tech.get(tech) in known:
                after.append(by_tech[tech])
            for extra in EXTRA_RESEARCH.get(producer["capability"], ()):
                if by_tech.get(extra) in known:
                    after.append(by_tech[extra])
            for fluid, key in fluid_inputs(producer["capability"], producer["parameters"]).items():
                for supply_row in supply.rows(fluid, key, produced, known):
                    add(supply_row)
                after.append(supply.node_of(key))
            after += sorted({produced[c] for c in _inputs(bill, row) if c in produced and produced[c] != node})
            add(
                {
                    "id": node,
                    "capability": producer["capability"],
                    "parameters": dict(producer["parameters"]),
                    "after": sorted(set(after), key=after.index),
                }
            )
            continue
        if (
            producer["capability"] == PRODUCE
            and producer["parameters"].get("output") != "belt"
            and all(_makes_own_inputs(makers.get(c)) for c in row["consumers"])
        ):
            continue  # every consumer is a row (one-level sub-row) or a block that makes it itself
        parameters = dict(producer["parameters"])
        if producer["capability"] in (PRODUCE, "fluid_assembly"):
            parameters["rate_per_minute"] = round(
                min(PRODUCE_MAX_RATE, max(PRODUCE_MIN_RATE, row["rate_per_minute"])), 1
            )
        after = [TAIL_ANCHOR]
        tech = row.get("technology")
        if tech and by_tech.get(tech) in known:
            after.append(by_tech[tech])
        after += sorted({produced[c] for c in _inputs(bill, row) if c in produced})
        fluid = (producer.get("layout") or {}).get("fluid")
        if fluid and parameters.get("fluid_source"):
            # A fluid row reads its own source key: a supply row publishes it first.
            for supply_row in supply.rows(fluid, parameters["fluid_source"], produced, known):
                add(supply_row)
            after.append(supply.node_of(parameters["fluid_source"]))
        node = step_id("rocket", producer["capability"], row["item"])
        produced[row["item"]] = node
        add({"id": node, "capability": producer["capability"], "parameters": parameters, "after": after})
    silo_after = [TAIL_ANCHOR]
    if by_tech.get(bill["target_technology"]) in known:
        silo_after.append(by_tech[bill["target_technology"]])
    add({"id": STAGE_SILO, "capability": STAGE_SILO, "parameters": {}, "after": silo_after})
    add({"id": STAGE_PARTS, "capability": STAGE_PARTS, "parameters": {}, "after": [STAGE_SILO]})
    add({"id": STAGE_LAUNCH, "capability": STAGE_LAUNCH, "parameters": {}, "after": [STAGE_PARTS]})
    return rows


def fluid_inputs(capability: str, parameters: Mapping) -> dict[str, str]:
    """Fluid -> the source key a block reads (its module's own declaration)."""
    if capability == "advanced_petroleum":
        from .advanced_petroleum import INPUT_KEYS

        return dict(INPUT_KEYS)
    if capability == "fluid_product":
        from .fluid_products import SOURCE_KEYS

        return dict(SOURCE_KEYS.get(parameters.get("product"), {}))
    return {}


# Where a fluid is published before any distribution (fluid-sources.json registry keys).
PUBLISHED_FLUID = {
    "petroleum-gas": "advanced-petroleum-gas",
    "light-oil": "light-oil",
    "heavy-oil": "heavy-oil",
    "sulfuric-acid": "sulfuric-acid",
    "lubricant": "lubricant",
}
FLUID_PRODUCER = {
    "petroleum-gas": "advanced-petroleum-gas",
    "light-oil": "light-oil",
    "heavy-oil": "heavy-oil",
}


class FluidSupply:
    """Supply rows that publish one consumer's source key (``fluid_supply_rows`` rule).

    Water and crude: an independent namespaced intake / pumpjack (``water_gathering`` /
    ``oil_gathering`` with ``source_key``). A produced fluid: a ``fluid_distribution``
    header splitting the fluid's current free outlet into the consumer's key and the next
    free outlet (``<port>-2``, ``<port>-3``, ...), so N consumers form a chain and
    no outlet is claimed twice (fluid_handoffs)."""

    def __init__(self):
        self.free: dict[str, str] = {}
        self.count: dict[str, int] = {}
        self.nodes: dict[str, str] = {}

    def node_of(self, key: str) -> str:
        return self.nodes[key]

    def rows(self, fluid: str, key: str, produced: Mapping[str, str], known: set) -> list[dict]:
        if key in self.nodes:
            return []
        if fluid == "water":
            node = step_id("rocket", "water", key)
            self.nodes[key] = node
            return [
                {"id": node, "capability": "water_gathering", "parameters": {"source_key": key}, "after": [TAIL_ANCHOR]}
            ]
        if fluid == "crude-oil":
            node = step_id("rocket", "crude", key)
            self.nodes[key] = node
            return [
                {"id": node, "capability": "oil_gathering", "parameters": {"source_key": key}, "after": [TAIL_ANCHOR]}
            ]
        port = PUBLISHED_FLUID.get(fluid, fluid)
        current = self.free.get(fluid, port)
        number = self.count.get(fluid, 1) + 1
        self.count[fluid] = number
        spare = f"{port}-{number}"
        self.free[fluid] = spare
        node = step_id("rocket", "split", key)
        self.nodes[key] = node
        after = [TAIL_ANCHOR] + [produced[fluid]] * (fluid in produced)
        previous = next((n for k, n in self.nodes.items() if k == current), None)
        if previous:
            after.append(previous)
        self.nodes[spare] = node
        return [
            {
                "id": node,
                "capability": "fluid_distribution",
                "parameters": {"source_key": current, "aliases": [key, spare]},
                "after": after,
            }
        ]


def _produce_rate(row: Mapping) -> float:
    return round(min(PRODUCE_MAX_RATE, max(PRODUCE_MIN_RATE, row["rate_per_minute"])), 1)


def _makes_own_inputs(producer: Mapping | None) -> bool:
    """A consumer that needs no separate stock row: a produce row (sub-row) or a roadmap block."""
    return producer is not None and (producer["capability"] == PRODUCE or producer["kind"] == "block")


def _inputs(bill: Mapping, row: Mapping) -> list[str]:
    """Items consumed by ``row``'s recipe (from the bill's consumer lists)."""
    recipe = row.get("recipe")
    return [r["item"] for r in bill["items"] if recipe and recipe in r["consumers"]]


# ----- bus lanes from the bill -------------------------------------------------------------
SPARE_LANES = 2  # lanes the planned bus keeps free beyond the bill's demand


def bus_lane_order(bill: Mapping, base: Iterable[str], capacity: int) -> tuple[str, ...]:
    """``base`` plus one lane per missing belt of every shared bus item the bill makes (not raw ore:
    ores stay on their mine trunks), keeping ``SPARE_LANES`` of ``capacity`` free."""
    order = list(base)
    raw = {r["item"] for r in bill["items"] if r["producer"]["kind"] == "raw"}
    for row in sorted(bill["bus"], key=lambda r: (-r["rate_per_minute"], r["item"])):
        if row["item"] in raw:
            continue
        have = order.count(row["item"])
        for _ in range(max(0, row["lanes_needed"] - have)):
            if len(order) >= capacity - SPARE_LANES:
                return tuple(order)
            order.append(row["item"])
    return tuple(order)


_SITE_CONFIGS: dict[str, Any] = {}


def site_config(catalog):
    """``SiteConfig`` whose bus ``lane_order`` reserves the rocket bill's shared items at plan time
    (``bus_lane_order``); the default config when the catalog has no rocket bill. Only a new site
    plan uses it: a saved ``site-plan.json`` keeps its lanes (``tools/rocket_plan.py`` reports the
    shortfall of an existing map)."""
    from dataclasses import replace

    from .site.busframe import lane_table
    from .site.planmodel import SiteConfig

    base = SiteConfig()
    key = getattr(catalog, "fingerprint", None)
    if key in _SITE_CONFIGS:
        return _SITE_CONFIGS[key]
    try:
        bill = rocket_bill(catalog, researched=(), bus_lanes=base.lane_order)
    except Exception:  # noqa: BLE001 - a catalog without a rocket bill plans the default bus
        return base
    capacity = len(lane_table(base.bus_width, base.lane_order, base.lane_group, base.lane_group_gap))
    config = replace(base, lane_order=bus_lane_order(bill, base.lane_order, capacity))
    _SITE_CONFIGS[key] = config
    return config


# ----- observe receipt ------------------------------------------------------------------

_CACHE: dict[tuple, dict] = {}


def receipt(
    catalog, observation: Mapping, *, capabilities: Mapping | None = None, minutes: float = DEFAULT_MINUTES
) -> dict:
    """Compact receipt for the autopilot: the tail rows, the gaps, the remaining research count.

    Cached per catalog fingerprint, researched set and capability names (the
    bill does not depend on anything else in the observation).
    """
    if capabilities is None:
        from .jobs import CAPABILITIES as capabilities  # noqa: N811
    researched = frozenset(_names(observation.get("technologies")))
    key = (catalog.fingerprint, researched, frozenset(capabilities), float(minutes))
    if key in _CACHE:
        return _CACHE[key]
    from .progression import ROADMAP

    bill = rocket_bill(catalog, researched=researched, minutes=minutes, capabilities=capabilities)
    rows = tail(bill, existing=[step.id for step in ROADMAP], researched_by=roadmap_research())
    result = {
        "minutes": bill["minutes"],
        "parts_per_rocket": bill["parts_per_rocket"],
        "remaining_technologies": len(bill["technologies"]),
        "science_packs": bill["science_packs"],
        "tail": rows,
        "gaps": [{"capability": g["capability"], "status": g["status"]} for g in bill["gaps"]],
    }
    if len(_CACHE) > 8:
        _CACHE.clear()
    _CACHE[key] = result
    return result
