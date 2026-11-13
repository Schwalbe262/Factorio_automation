"""Default roadmap toward a rocket launch, and the autopilot that proposes it.

``ROADMAP`` is data: the capability order proven on earlier live runs. The
functions below are pure (no IO) and only read job dicts, queue node dicts
and an observe receipt, so the factory keeps progressing while the planner
is quiet and finished stages are skipped quickly on a replayed map.

Rules:
- A step is *satisfied* when a job with the same capability and parameters
  succeeded, or, for a step naming a ``technology``, when that technology
  is already researched.
- A step is *represented* when a queue node or a non-terminal job already
  carries the same capability and parameters (or the queue already holds
  the step's node id).
- A step is *ready* when every capability its capability ``requires`` has a
  succeeded job, every ``after`` step is satisfied, its native recipes are
  enabled, and (for ``research`` steps, when the receipt lists available
  research) its technology is researchable now.
- A step whose matching job is ``blocked`` or ``failed`` is left to the
  planner; later independent steps are still proposed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .capacity_planning import plan_capacity
from .jobs import RESEARCH_CAPABILITIES

TERMINAL_STATUSES = frozenset({"succeeded", "blocked", "failed"})
STALLED_STATUSES = frozenset({"blocked", "failed"})
OPEN_NODE_STATUSES = frozenset({"pending", "ready", "running", "waiting"})
RESEARCH_CAPABILITY = "research"
CAPACITY_CAPABILITY = "starter_capacity"
NODE_PREFIX = "auto_"
# Autopilot nodes rank below the planner's usual non-negative priorities, so
# an explicit planner decision always runs first; earlier steps rank higher.
TOP_PRIORITY = -100
# Need-driven capacity outranks every roadmap step.
CAPACITY_PRIORITY = TOP_PRIORITY + 50
# Raw terminals of the lookahead bill of materials (plates count as raw).
RAW_ITEMS = frozenset({"iron-plate", "copper-plate", "coal", "stone", "wood"})
# Machine-made construction stock (``produce``), see ``produce_nodes``.
PRODUCE_CAPABILITY = "produce"
# Just below capacity: plates first, then the rows that turn them into stock.
PRODUCE_PRIORITY = CAPACITY_PRIORITY - 1
# Finished items the next steps place before a row pays for itself.
BULK_THRESHOLD = 20
# A proposed row covers the upcoming need in about this many minutes.
COVER_MINUTES = 5.0
PRODUCE_MAX_RATE = 600.0
PRODUCE_MIN_RATE = 1.0
# Growth of the staged red science block (``science_growth_nodes``).
SCIENCE_GROWTH_CAPABILITY = "science_growth"
# Below every roadmap step: growth fills a free job slot, it never delays the roadmap.
SCIENCE_GROWTH_PRIORITY = TOP_PRIORITY - 500


@dataclass(frozen=True)
class Step:
    """One roadmap entry: run ``capability`` with ``parameters`` once ``after`` steps are satisfied."""

    id: str
    capability: str
    parameters: dict[str, Any] = field(default_factory=dict)
    after: tuple[str, ...] = ()
    # Main hardware the step places: item name -> count. Raw items (plates,
    # coal, stone) may appear directly, e.g. a fuel seed. Only early steps are
    # filled; a step without ``builds`` contributes no lookahead demand, so
    # later stages are under-estimated.
    builds: Mapping[str, float] | None = None


def _research(technology: str, after: tuple[str, ...] = ()) -> Step:
    """A normal lab research step; its id is derived from the technology name."""
    return Step("research_" + technology.replace("-", "_"), RESEARCH_CAPABILITY, {"technology": technology}, after)


# ``builds`` of the early steps, counted from the executors. "estimate" marks
# a routed part whose length depends on the map.
# power: core/deterministic_builder.py:777-790 (pump, steam bank, pipe route),
# core/factory_templates.py:259-280 (1 boiler, 2 engines, 3 pipes, 5 belts,
# 1 burner inserter, 3 poles), deterministic_builder.py:811-834 (coal drill,
# 13 belts, 1 burner inserter) and :988-991 (8 coal seeded into drill and boiler).
POWER_BUILDS = {
    "boiler": 1,
    "steam-engine": 2,
    "offshore-pump": 1,
    "pipe": 8,  # 3 fixed + about 5 routed (estimate)
    "transport-belt": 30,  # 5 + 13 fixed + about 12 routed (estimate)
    "burner-inserter": 2,
    "small-electric-pole": 3,
    "burner-mining-drill": 1,
    "coal": 16,
}
# starter_research: one lab next to power (research.py:56-63); packs come from unit_count.
STARTER_RESEARCH_BUILDS = {"lab": 1}
# bootstrap_logistics: startup science cell, board.py:27-38, plus a pole chain to the grid (board.py:48-62).
BOOTSTRAP_LOGISTICS_BUILDS = {
    "assembling-machine-1": 2,
    "wooden-chest": 3,
    "inserter": 4,
    "small-electric-pole": 5,  # 2 in the cell + about 3 links (estimate)
}
# electric_coal: one electric drill replaces the burner coal drill (electric_mining.py:38, 146-150),
# grid link poles (electric_mining.py:77-78, 115); the output belt is reused.
ELECTRIC_COAL_BUILDS = {"electric-mining-drill": 1, "small-electric-pole": 3}  # poles: estimate
# mining_outpost at 30 or 60/min: ceil(rate / 30) drills rounded up to one opposing
# pair (outposts.py:593-600, mining_layout_policy.py:680, paired_mining.py:23-29).
MINING_OUTPOST_BUILDS = {
    "electric-mining-drill": 2,
    "small-electric-pole": 8,  # 2 fixed + about 6 bridge poles (estimate)
    "transport-belt": 10,  # 4 collector + about 6 exit belts (estimate)
}
# smelting_district: 4 iron + 2 copper furnaces (smelting.py:341-349), 2 inserters,
# 1 pole per furnace and 84 fixed belts (smelting.py:64-84), 1 splitter (smelting.py:366-372).
SMELTING_DISTRICT_BUILDS = {
    "stone-furnace": 6,
    "inserter": 12,
    "small-electric-pole": 10,  # 6 fixed + about 4 grid links (estimate)
    "transport-belt": 234,  # 84 fixed + about 150 on five routes (estimate)
    "splitter": 1,
    "underground-belt": 4,  # crossings only (estimate)
}

# planned_main_bus: two trunks of planned_bus.TRUNK_TILES (64) belts, two feed
# routes from the district (about 30 belts each, estimate) and 5 branch belts
# per feed, one splitter per feed, underground pairs where belts cross (estimate).
PLANNED_BUS_BUILDS = {"transport-belt": 200, "splitter": 2, "underground-belt": 8}

ROADMAP: tuple[Step, ...] = (
    Step("starter", "starter", {}),
    # Starter capacity is not a fixed step: ``autopilot_nodes`` sizes it from
    # measured supply, the current bill and ``lookahead_demand``.
    Step("power", "power", {}, builds=POWER_BUILDS),
    Step(
        "starter_research_automation",
        "starter_research",
        {"technology": "automation"},
        builds=STARTER_RESEARCH_BUILDS,
    ),
    # Machine-built science from here on: once automation is researched the
    # 25 packs come from the startup science cell, not from hand crafting.
    # (The id keeps its old name so queues written earlier still match.)
    Step(
        "starter_research_electric_mining_drill",
        "bootstrap_research",
        {"technology": "electric-mining-drill"},
        after=("starter_research_automation",),
        builds=BOOTSTRAP_LOGISTICS_BUILDS,
    ),
    Step(
        "electric_coal",
        "electric_coal",
        {},
        after=("starter_research_electric_mining_drill",),
        builds=ELECTRIC_COAL_BUILDS,
    ),
    Step(
        "bootstrap_logistics",
        "bootstrap_logistics",
        {},
        after=("starter_research_automation",),
    ),
    Step(
        "mining_outpost_iron",
        "mining_outpost",
        {"resource": "iron-ore", "rate_per_minute": 60},
        builds=MINING_OUTPOST_BUILDS,
    ),
    Step(
        "mining_outpost_copper",
        "mining_outpost",
        {"resource": "copper-ore", "rate_per_minute": 60},
        builds=MINING_OUTPOST_BUILDS,
    ),
    Step(
        "mining_outpost_coal",
        "mining_outpost",
        {"resource": "coal", "rate_per_minute": 30},
        builds=MINING_OUTPOST_BUILDS,
    ),
    Step(
        "smelting_district",
        "smelting_district",
        {},
        after=("mining_outpost_iron", "mining_outpost_copper", "mining_outpost_coal"),
        builds=SMELTING_DISTRICT_BUILDS,
    ),
    # paired_smelting / paired_mining are migrations of the old development
    # map and are not roadmap steps: fresh districts publish plate ports and
    # fresh mining banks are already paired (outposts.py, pcb-paired-mining-v1).
    # The fresh-map bus comes right after the district: trunks from the site
    # plan, fed beside the district ports (planned_bus.py); every consumer
    # beside it then takes plates from bus taps (planned_tap.py), never from
    # its own line out of the district. bus_factory's main_bus_factory is the
    # old development-map migration and is only run on explicit request.
    Step("main_bus", "planned_main_bus", {}, builds=PLANNED_BUS_BUILDS),
    Step("automation_science", "automation_science", {"rate_per_minute": 30}, after=("main_bus",)),
    _research("logistic-science-pack"),
    _research("steel-processing"),
    _research("fast-inserter"),
    Step("logistic_science", "logistic_science", {"rate_per_minute": 30}),
    Step("steam_expansion", "steam_expansion", {}, after=("logistic_science",)),
    _research("gun-turret"),
    Step("defense_perimeter", "defense_perimeter", {}, after=("research_gun_turret", "steam_expansion")),
    _research("advanced-material-processing"),
    _research("automation-2"),
    _research("engine"),
    _research("fluid-handling"),
    _research("oil-gathering"),
    Step("dedicated_materials_coal", "dedicated_materials", {"resource": "coal"}, after=("main_bus",)),
    Step("dedicated_materials_stone", "dedicated_materials", {"resource": "stone"}, after=("main_bus",)),
    Step("steel_supply", "steel_supply", {}),
    Step(
        "steel_production",
        "steel_production",
        {},
        after=("research_steel_processing", "dedicated_materials_coal"),
    ),
    Step("brick_production", "brick_production", {}, after=("dedicated_materials_stone",)),
    _research("electric-energy-distribution-1"),
    Step("steel_coal_supply", "steel_coal_supply", {}),
    Step("oil_gathering", "oil_gathering", {}, after=("research_oil_gathering",)),
    Step("ammo_production", "ammo_production", {}),
    _research("plastics", after=("oil_gathering",)),
    Step("basic_petroleum", "basic_petroleum", {}),
    Step("engine_production", "engine_production", {}, after=("research_engine",)),
    Step("chemical_coal_supply", "chemical_coal_supply", {}, after=("basic_petroleum",)),
    Step(
        "chemical_production_plastic",
        "chemical_production",
        {"product": "plastic-bar"},
        after=("research_plastics", "chemical_coal_supply"),
    ),
    Step("water_gathering", "water_gathering", {}, after=("basic_petroleum",)),
    _research("sulfur-processing"),
    Step("gas_distribution", "gas_distribution", {}, after=("chemical_production_plastic",)),
    _research("advanced-circuit"),
    _research("chemical-science-pack"),
    Step("advanced_circuit_production", "advanced_circuit_production", {}, after=("research_advanced_circuit",)),
    Step("water_distribution", "water_distribution", {}),
    Step(
        "chemical_production_sulfur",
        "chemical_production",
        {"product": "sulfur"},
        after=("research_sulfur_processing", "gas_distribution", "water_distribution"),
    ),
    Step("chemical_science", "chemical_science", {}, after=("research_chemical_science_pack",)),
)


def _names(value) -> set[str]:
    """Names from a ``name -> bool`` mapping (truthy only) or from a list of names."""
    if isinstance(value, Mapping):
        return {name for name, flag in value.items() if flag}
    if isinstance(value, Iterable) and not isinstance(value, str):
        return {name for name in value if isinstance(name, str)}
    return set()


# Science stages whose ``rate_per_minute`` the autopilot takes from the observe
# receipt's ``research_capacity.recommended_rates`` (capability -> pack). A job
# or node matches such a step whatever rate it was given.
RATE_SIZED_CAPABILITIES = {
    "automation_science": "automation-science-pack",
    "logistic_science": "logistic-science-pack",
}


def _same(row: Mapping, step: Step) -> bool:
    if row.get("capability") != step.capability:
        return False
    parameters = row.get("parameters", {})
    if step.capability in RATE_SIZED_CAPABILITIES and isinstance(parameters, Mapping):
        return _without_rate(parameters) == _without_rate(step.parameters)
    return parameters == step.parameters


def _without_rate(parameters: Mapping) -> dict:
    return {key: value for key, value in parameters.items() if key != "rate_per_minute"}


def _sized_parameters(step: Step, observation: Mapping) -> tuple[dict, str]:
    """The step's parameters, with a science rate from the receipt when it recommends one.

    The recommendation (``research_capacity.recommended_science_rate``) is used
    only when it is a number in ``(0, step default]``; otherwise the default
    stays. Returns the parameters and a note for the node reason.
    """
    parameters = dict(step.parameters)
    pack = RATE_SIZED_CAPABILITIES.get(step.capability)
    default = parameters.get("rate_per_minute")
    receipt = observation.get("research_capacity")
    if pack is None or not isinstance(default, (int, float)) or not isinstance(receipt, Mapping):
        return parameters, ""
    rates = receipt.get("recommended_rates")
    row = rates.get(pack) if isinstance(rates, Mapping) else None
    rate = row.get("rate_per_minute") if isinstance(row, Mapping) else None
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 < rate <= default:
        return parameters, ""
    parameters["rate_per_minute"] = rate
    return parameters, f"; rate {rate}/min from research_capacity ({row.get('reason', '')})"[:300]


@dataclass(frozen=True)
class _Progress:
    """What the ledger and the receipt say is already done."""

    jobs: tuple[Mapping, ...]
    researched: frozenset[str]

    @property
    def succeeded_capabilities(self) -> set[str]:
        return {job.get("capability") for job in self.jobs if job.get("status") == "succeeded"}

    def satisfied(self, step: Step) -> bool:
        """A matching job succeeded, or the step's technology is already researched."""
        if step.parameters.get("technology") in self.researched:
            return True
        return any(_same(job, step) and job.get("status") == "succeeded" for job in self.jobs)

    def stalled(self, step: Step) -> bool:
        """A matching job is blocked or failed; the planner owns that blocker."""
        return any(_same(job, step) and job.get("status") in STALLED_STATUSES for job in self.jobs)

    def in_flight(self, step: Step) -> bool:
        """A matching job exists that has not reached a terminal status."""
        return any(_same(job, step) and job.get("status") not in TERMINAL_STATUSES for job in self.jobs)


def _by_id() -> dict[str, Step]:
    return {step.id: step for step in ROADMAP}


def _recipes_ready(step: Step, capabilities: Mapping, observation: Mapping) -> bool:
    """Native recipe locks from the receipt's gates, else from its enabled recipes; unknown means ready."""
    gates = observation.get("native_capability_gates")
    if isinstance(gates, Mapping) and isinstance(gates.get(step.capability), Mapping):
        return gates[step.capability].get("native_recipes_ready") is not False
    required = capabilities[step.capability].get("native_recipes") or ()
    if not required or "enabled_recipes" not in observation:
        return True
    return set(required) <= _names(observation["enabled_recipes"])


def _research_ready(step: Step, observation: Mapping) -> bool:
    """A research step waits until the receipt lists its technology as researchable now."""
    if step.capability != RESEARCH_CAPABILITY:
        return True
    available = observation.get("available_research")
    if not isinstance(available, Mapping) or not isinstance(available.get("technologies"), list):
        return True
    listed = {row.get("technology") for row in available["technologies"] if isinstance(row, Mapping)}
    return step.parameters["technology"] in listed


def _capacity_taken(resource: str, cells: int, jobs, queue_nodes) -> bool:
    """A starter capacity node or job for ``resource`` is open, or this exact target already stalled.

    Open means a queue node that is pending, ready, running or waiting (or has
    no status yet), or a job that has not reached a terminal status. A blocked
    or failed job with the same target belongs to the planner.
    """
    for node in queue_nodes:
        if node.get("capability") != CAPACITY_CAPABILITY:
            continue
        if (
            node.get("parameters", {}).get("resource") == resource
            and node.get("status", "pending") in OPEN_NODE_STATUSES
        ):
            return True
    for job in jobs:
        if job.get("capability") != CAPACITY_CAPABILITY:
            continue
        parameters = job.get("parameters", {})
        if parameters.get("resource") != resource:
            continue
        if job.get("status") not in TERMINAL_STATUSES:
            return True
        if job.get("status") in STALLED_STATUSES and parameters.get("cells") == cells:
            return True
    return False


def capacity_nodes(jobs, queue_nodes, observation, capabilities) -> list[dict]:
    """Queue nodes for need-driven starter capacity (see ``capacity_planning``).

    Only in the burner starter phase: once a ``mining_outpost`` job for a
    resource succeeded, electric outposts replace its starter cells and the
    item is skipped (sizing outposts from demand is a later step). Requires
    the capability's ``requires`` (``starter``) to have succeeded jobs.
    """
    if CAPACITY_CAPABILITY not in capabilities:
        return []
    succeeded = {job.get("capability") for job in jobs if job.get("status") == "succeeded"}
    if not set(capabilities[CAPACITY_CAPABILITY].get("requires", [])) <= succeeded:
        return []
    outposts = {
        job.get("parameters", {}).get("resource")
        for job in jobs
        if job.get("capability") == "mining_outpost" and job.get("status") == "succeeded"
    }
    try:
        decisions = plan_capacity(
            observation.get("resource_supply_options") or {},
            observation.get("roadmap_material_demand") or {},
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return []
    queued_ids = {node.get("id") for node in queue_nodes}
    nodes = []
    for decision in decisions:
        node_id = f"{NODE_PREFIX}capacity_{decision.resource}_{decision.target_cells}"
        if decision.resource in outposts or node_id in queued_ids:
            continue
        if decision.target_cells <= decision.current_cells:
            continue
        if _capacity_taken(decision.resource, decision.target_cells, jobs, queue_nodes):
            continue
        nodes.append(
            {
                "id": node_id,
                "capability": CAPACITY_CAPABILITY,
                "parameters": {"resource": decision.resource, "cells": decision.target_cells},
                "priority": CAPACITY_PRIORITY,
                "depends_on": [],
                "reason": decision.reason,
            }
        )
    return nodes


def autopilot_nodes(jobs, queue_nodes, observation, capabilities, *, limit=3) -> list[dict]:
    """Up to ``limit`` new queue nodes: need-driven capacity first, then roadmap steps.

    Capacity nodes (``capacity_nodes``) rank at ``CAPACITY_PRIORITY``, above
    every roadmap step, because jobs advance in priority order and the
    capacity job must get scarce plates first; they count toward ``limit``.
    ``produce`` nodes (``produce_nodes``) follow at ``PRODUCE_PRIORITY``.
    Roadmap node ids are ``"auto_" + step.id``. ``depends_on`` is always
    empty: a step is only proposed once every ``after`` step is satisfied,
    and satisfied dependencies are dropped. Priorities descend along the
    roadmap from ``TOP_PRIORITY``.
    """
    jobs = tuple(jobs)
    progress = _Progress(jobs, frozenset(_names(observation.get("technologies"))))
    succeeded = progress.succeeded_capabilities
    steps = _by_id()
    queued_ids = {node.get("id") for node in queue_nodes}
    proposed: list[dict] = capacity_nodes(jobs, queue_nodes, observation, capabilities)[: max(0, limit)]
    proposed += produce_nodes(jobs, queue_nodes, observation, capabilities)[: max(0, limit - len(proposed))]
    for index, step in enumerate(ROADMAP):
        if len(proposed) >= limit:
            break
        node_id = NODE_PREFIX + step.id
        if step.capability not in capabilities or progress.satisfied(step):
            continue
        if node_id in queued_ids or any(_same(node, step) for node in queue_nodes):
            continue
        if progress.in_flight(step) or progress.stalled(step):
            continue
        if not set(capabilities[step.capability].get("requires", [])) <= succeeded:
            continue
        if not all(progress.satisfied(steps[name]) for name in step.after):
            continue
        if not _recipes_ready(step, capabilities, observation) or not _research_ready(step, observation):
            continue
        parameters, note = _sized_parameters(step, observation)
        proposed.append(
            {
                "id": node_id,
                "capability": step.capability,
                "parameters": parameters,
                "priority": TOP_PRIORITY - index,
                "depends_on": [],
                "reason": f"autopilot roadmap step {index + 1}/{len(ROADMAP)}: {step.id}{note}",
            }
        )
    proposed += science_growth_nodes(jobs, queue_nodes, observation, capabilities)[: max(0, limit - len(proposed))]
    return proposed


def science_growth_nodes(jobs, queue_nodes, observation, capabilities) -> list[dict]:
    """One ``science_growth`` node when the receipt's ``science_growth`` decision grows the staged block.

    The decision (``science_growth.growth_decision``) already holds the
    evidence rule; here only: the capability exists, its ``requires``
    succeeded, no growth node or job is open, and the same target has not
    stalled (blocked/failed) before.
    """
    if SCIENCE_GROWTH_CAPABILITY not in capabilities:
        return []
    row = observation.get("science_growth")
    if not isinstance(row, Mapping) or not row.get("grow"):
        return []
    succeeded = {job.get("capability") for job in jobs if job.get("status") == "succeeded"}
    if not set(capabilities[SCIENCE_GROWTH_CAPABILITY].get("requires", [])) <= succeeded:
        return []
    labs, machines = row.get("target_labs"), row.get("target_machines")
    if type(labs) is not int or type(machines) is not int:
        return []
    parameters = {"labs": labs, "machines": machines}
    for node in queue_nodes:
        if node.get("capability") == SCIENCE_GROWTH_CAPABILITY and node.get("status", "pending") in OPEN_NODE_STATUSES:
            return []
    for job in jobs:
        if job.get("capability") != SCIENCE_GROWTH_CAPABILITY:
            continue
        if job.get("status") not in TERMINAL_STATUSES:
            return []
        if job.get("status") in STALLED_STATUSES and job.get("parameters") == parameters:
            return []
    node_id = f"{NODE_PREFIX}science_growth_{labs}_{machines}"
    if node_id in {node.get("id") for node in queue_nodes}:
        return []
    return [
        {
            "id": node_id,
            "capability": SCIENCE_GROWTH_CAPABILITY,
            "parameters": parameters,
            "priority": SCIENCE_GROWTH_PRIORITY,
            "depends_on": [],
            "reason": f"science growth to {labs} labs, {machines} machines: {row.get('reason', '')}"[:300],
        }
    ]


def _upcoming(jobs, researched: Iterable[str], horizon: int) -> list[Step]:
    """The next ``horizon`` roadmap steps that are not satisfied yet."""
    progress = _Progress(tuple(jobs), frozenset(researched))
    return [step for step in ROADMAP if not progress.satisfied(step)][:horizon]


def _product_amount(recipe: Mapping, item: str) -> float:
    """Items of ``item`` one craft of ``recipe`` yields (expected amount, default 1)."""
    for product in recipe.get("products") or ():
        if not isinstance(product, Mapping) or product.get("name") != item:
            continue
        amount = product.get("amount")
        if not isinstance(amount, (int, float)):
            low, high = product.get("amount_min"), product.get("amount_max")
            amount = (low + high) / 2 if isinstance(low, (int, float)) and isinstance(high, (int, float)) else 1
        probability = product.get("probability", 1)
        probability = probability if isinstance(probability, (int, float)) else 1
        if amount * probability > 0:
            return float(amount * probability)
    return 1.0


def raw_cost(catalog, items: Mapping[str, float]) -> dict[str, float]:
    """Raw plates/coal/stone/wood for ``items`` by recursive expansion of catalog recipes.

    Fractional crafts are kept (an estimate, not a crafting plan); fluids and
    items with no recipe are dropped; a dependency cycle stops at depth 24.
    """
    cost: dict[str, float] = {}

    def expand(item: str, count: float, depth: int) -> None:
        if count <= 0:
            return
        if item in RAW_ITEMS:
            cost[item] = cost.get(item, 0.0) + count
            return
        if depth > 24:
            return
        recipe = catalog.recipe_for_product(item)
        if not recipe:
            return
        crafts = count / _product_amount(recipe, item)
        for ingredient in recipe.get("ingredients") or ():
            if not isinstance(ingredient, Mapping) or ingredient.get("type", "item") != "item":
                continue
            amount = ingredient.get("amount")
            if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                expand(ingredient["name"], amount * crafts, depth + 1)

    for name, count in items.items():
        if isinstance(count, (int, float)) and not isinstance(count, bool):
            expand(name, float(count), 0)
    return cost


def _research_items(catalog, step: Step) -> dict[str, float]:
    """Science packs of a research step: ``unit_count`` x each ingredient amount."""
    if step.capability not in RESEARCH_CAPABILITIES or "technology" not in step.parameters:
        return {}
    technology = (getattr(catalog, "technologies", None) or {}).get(step.parameters["technology"])
    if not isinstance(technology, Mapping):
        return {}
    units = technology.get("unit_count")
    if not isinstance(units, (int, float)) or isinstance(units, bool):
        return {}
    packs: dict[str, float] = {}
    for ingredient in technology.get("ingredients") or ():
        if isinstance(ingredient, Mapping) and isinstance(ingredient.get("amount"), (int, float)):
            name = ingredient.get("name")
            packs[name] = packs.get(name, 0.0) + units * ingredient["amount"]
    return packs


def lookahead_demand(catalog, jobs, observation, *, horizon=4) -> dict[str, float]:
    """Raw items the next ``horizon`` unsatisfied roadmap steps will need.

    Research steps add ``unit_count`` x pack raw cost; steps with ``builds``
    add the raw cost of that hardware. Stock is not subtracted (the supply
    ``shortfall`` covers the current bill). Steps without ``builds`` add
    nothing, so stages past the early game are under-estimated. Steps already
    in flight are included and may overlap their saved construction bill.
    """
    researched = _names((observation or {}).get("technologies"))
    items: dict[str, float] = {}
    for step in _upcoming(jobs, researched, horizon):
        for name, count in _research_items(catalog, step).items():
            items[name] = items.get(name, 0.0) + count
        for name, count in (step.builds or {}).items():
            items[name] = items.get(name, 0.0) + count
    return {name: round(amount, 3) for name, amount in raw_cost(catalog, items).items()}


def roadmap_wanted_recipes(jobs, capabilities, *, horizon=6) -> list[str]:
    """``native_recipes`` of the next ``horizon`` unsatisfied steps, in roadmap order, without repeats."""
    recipes: list[str] = []
    for step in _upcoming(jobs, (), horizon):
        recipes.extend(capabilities.get(step.capability, {}).get("native_recipes", []))
    return list(dict.fromkeys(recipes))


def roadmap_wanted_technologies(jobs, technologies_researched, *, horizon=6) -> list[str]:
    """Technologies named by the next ``horizon`` unsatisfied steps, in roadmap order."""
    steps = _upcoming(jobs, _names(technologies_researched), horizon)
    return [step.parameters["technology"] for step in steps if "technology" in step.parameters]


def bulk_item_demand(catalog, jobs, observation, *, horizon=4) -> dict[str, dict]:
    """Finished construction items the next ``horizon`` unsatisfied steps place.

    ``{item: {"count": n, "raw_per_item": {plate: amount}}}`` from
    ``Step.builds``; raw items (plates, coal, stone, wood) are left out, and
    so is an item without a catalog recipe. ``raw_per_item`` is the
    recursive raw cost of one item (``raw_cost``). Steps without ``builds``
    add nothing, so later stages are under-estimated.
    """
    researched = _names((observation or {}).get("technologies"))
    counts: dict[str, float] = {}
    for step in _upcoming(jobs, researched, horizon):
        for name, count in (step.builds or {}).items():
            if name in RAW_ITEMS or isinstance(count, bool) or not isinstance(count, (int, float)):
                continue
            counts[name] = counts.get(name, 0.0) + float(count)
    demand = {}
    for name, count in counts.items():
        if not catalog.recipe_for_product(name):
            continue
        raw = raw_cost(catalog, {name: 1})
        demand[name] = {"count": round(count, 3), "raw_per_item": {k: round(v, 4) for k, v in raw.items()}}
    return demand


def _net_rate(observation: Mapping, item: str) -> float | None:
    supply = observation.get("resource_supply_options")
    row = supply.get(item) if isinstance(supply, Mapping) else None
    evidence = row.get("evidence") if isinstance(row, Mapping) else None
    value = evidence.get("net_per_minute") if isinstance(evidence, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _produce_taken(item: str, jobs, queue_nodes) -> bool:
    """Any produce node or job for ``item`` exists (open, done, or stalled for the planner)."""
    for row in list(jobs) + list(queue_nodes):
        if row.get("capability") != PRODUCE_CAPABILITY:
            continue
        parameters = row.get("parameters")
        if isinstance(parameters, Mapping) and parameters.get("item") == item:
            return True
    return False


def produce_nodes(jobs, queue_nodes, observation, capabilities) -> list[dict]:
    """Queue nodes for machine-made construction stock (``produce``).

    Reads the receipt's ``bulk_item_demand`` (``bulk_item_demand``) and the
    plate ``net_per_minute`` of ``resource_supply_options`` (the evidence
    ``capacity_planning`` uses). An item is proposed when the next steps place
    more than ``BULK_THRESHOLD`` of it, ``produce`` prerequisites succeeded and
    no produce job or node for it exists. Rate: the need over
    ``COVER_MINUTES``, capped so each plate's per-item cost stays within that
    plate's measured net production (unmeasured or non-positive net: no node,
    a row would only starve), and within ``(PRODUCE_MIN_RATE, PRODUCE_MAX_RATE]``.
    Largest need first.
    """
    if PRODUCE_CAPABILITY not in capabilities:
        return []
    succeeded = {job.get("capability") for job in jobs if job.get("status") == "succeeded"}
    if not set(capabilities[PRODUCE_CAPABILITY].get("requires", [])) <= succeeded:
        return []
    demand = observation.get("bulk_item_demand")
    if not isinstance(demand, Mapping):
        return []
    nodes = []
    ranked = sorted(demand.items(), key=lambda row: (-float((row[1] or {}).get("count", 0) or 0), row[0]))
    for item, row in ranked:
        count = row.get("count") if isinstance(row, Mapping) else None
        raw = row.get("raw_per_item") if isinstance(row, Mapping) else None
        if isinstance(count, bool) or not isinstance(count, (int, float)) or count <= BULK_THRESHOLD:
            continue
        if not isinstance(raw, Mapping) or not raw or _produce_taken(item, jobs, queue_nodes):
            continue
        rate = count / COVER_MINUTES
        caps = []
        for plate, per_item in raw.items():
            net = _net_rate(observation, plate)
            if net is None or net <= 0 or not isinstance(per_item, (int, float)) or per_item <= 0:
                caps = None
                break
            caps.append(net / per_item)
        if caps is None:
            continue
        rate = round(min([rate, PRODUCE_MAX_RATE] + caps), 1)
        if rate < PRODUCE_MIN_RATE:
            continue
        nodes.append(
            {
                "id": f"{NODE_PREFIX}produce_{item}",
                "capability": PRODUCE_CAPABILITY,
                "parameters": {"item": item, "rate_per_minute": rate},
                "priority": PRODUCE_PRIORITY,
                "depends_on": [],
                "reason": (
                    f"next steps place {count:g} {item}; machine stock at {rate}/min covers it in "
                    f"{count / rate:.1f} min within measured plate net production"
                )[:300],
            }
        )
    return nodes
