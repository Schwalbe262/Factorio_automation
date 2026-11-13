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

from collections import Counter
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
# Threat-sized defense posts (``defense_growth_nodes``): a free slot, never the critical path.
DEFENSE_GROWTH_CAPABILITY = "defense_growth"
DEFENSE_GROWTH_PRIORITY = SCIENCE_GROWTH_PRIORITY - 1
# A target is re-proposed at most once per this many ticks (10 game minutes).
DEFENSE_GROWTH_PERIOD_TICKS = 36000


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
    # Optional extra readiness rule (``gate_ready``) and a fixed priority that
    # replaces ``TOP_PRIORITY - index`` (a step kept off the critical path).
    gate: str | None = None
    priority: int | None = None


def _research(technology: str, after: tuple[str, ...] = ()) -> Step:
    """A normal lab research step; its id is derived from the technology name."""
    return Step("research_" + technology.replace("-", "_"), RESEARCH_CAPABILITY, {"technology": technology}, after)


# ``builds`` of the early steps, counted from the executors. "estimate" marks
# a routed part whose length depends on the map.
# power (phase 1, hand-fed boiler): FactoryBuilder._choose_power_plan (pump,
# steam bank, pipe route), core/factory_templates.py steam bank (1 boiler,
# 2 engines, pipes, the 5-belt coal stub under the boiler inserter, 1 burner
# inserter, 3 poles); coal = the first boiler fill (power_coal_feed.boiler_sizing
# "target": one stack).
POWER_BUILDS = {
    "boiler": 1,
    "steam-engine": 2,
    "offshore-pump": 1,
    "pipe": 8,  # 3 fixed + about 5 routed (estimate; saved plans 6 and 11)
    "transport-belt": 5,
    "burner-inserter": 1,
    "small-electric-pole": 3,
    "coal": 50,
}
# power_coal_feed (phase 2): the dedicated feed of the saved coal_plan
# (V2FactoryBuilder._choose_coal_plan: burner drill, fuel arm, routed belt to the
# boiler; saved plans on two maps had 95 and 141 belts) plus its 8-coal drill seed.
POWER_COAL_FEED_BUILDS = {
    "burner-mining-drill": 1,
    "burner-inserter": 1,
    "transport-belt": 100,  # estimate; saved plans 95 and 141
    "coal": 8,
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
    "small-electric-pole": 13,  # 2 fixed + bridge poles; saved plans on two maps: 4..20, mean 13
    "transport-belt": 10,  # 4 collector + about 6 exit belts (estimate; plans: 6..13)
}
# smelting_district: two paired banks (smelting.paired_furnace_bank), 2+2 iron and
# 1+1 copper furnaces, 2 inserters and 1 pole per furnace, 175 fixed belts (full
# output belts, split feed heads, coal branches), 5 splitters and 4 head undergrounds
# (smelting_site.district_candidate with the split feed head, feed_head.split_feed_head).
SMELTING_DISTRICT_BUILDS = {
    "stone-furnace": 6,
    "inserter": 12,
    "small-electric-pole": 22,  # 6 fixed + grid links; saved plans on two maps: 19 and 24
    "transport-belt": 355,  # 175 fixed + routes; saved plans (shared head): 368 and 319
    "splitter": 5,
    "underground-belt": 12,  # 4 in the heads + crossings; saved plans: 8 and 6 crossings
}
# automation_science: science machines plus the lab block and their routes
# (saved plans automation-science.json on two maps: 6 machines, 21 inserters,
# 8 labs, 51/50 poles, 249/234 belts). Without it the step counted nothing.
AUTOMATION_SCIENCE_BUILDS = {
    "assembling-machine-1": 6,
    "inserter": 21,
    "lab": 8,
    "small-electric-pole": 50,
    "transport-belt": 240,
    "underground-belt": 2,
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
    # Phase 2 of starter power: the dedicated coal drill and belt replace hand
    # fuelling of the boiler once belts are machine-made (power_coal_feed.py).
    # Listed here so its belts count in the lookahead and supply cell demand;
    # its low fixed priority keeps it off the research and outpost path.
    Step(
        "power_coal_feed",
        "power_coal_feed",
        {},
        builds=POWER_COAL_FEED_BUILDS,
        gate="machine_made_belts",
        priority=TOP_PRIORITY - 400,
    ),
    Step(
        "electric_coal",
        "electric_coal",
        {},
        after=("starter_research_electric_mining_drill", "power_coal_feed"),
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
    Step(
        "automation_science",
        "automation_science",
        {"rate_per_minute": 30},
        after=("main_bus",),
        builds=AUTOMATION_SCIENCE_BUILDS,
    ),
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
    # The hand-served starter coal cells stand on the best coal sites; the
    # dedicated bank found its only site obstructed by them (seed 4176033748),
    # so it waits until they are retired.
    Step(
        "dedicated_materials_coal",
        "dedicated_materials",
        {"resource": "coal"},
        after=("main_bus",),
        gate="starter_coal_retired",
    ),
    Step("dedicated_materials_stone", "dedicated_materials", {"resource": "stone"}, after=("main_bus",)),
    # On a planned world steel and bricks are site-planned furnace blocks
    # (planned_furnaces.py): the steel block routes the dedicated "coal"
    # outlet and taps bus iron, the brick block routes "stone" and "brick-coal".
    Step("steel_supply", "steel_supply", {}, after=("dedicated_materials_coal",)),
    Step(
        "steel_production",
        "steel_production",
        {},
        after=("research_steel_processing", "dedicated_materials_coal"),
    ),
    Step(
        "brick_production",
        "brick_production",
        {},
        after=("dedicated_materials_stone", "dedicated_materials_coal"),
    ),
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
    if step.capability == PRODUCE_CAPABILITY and isinstance(parameters, Mapping):
        # One row per item: any produce job or node for the item is this step's row
        # (a higher rate fills the row's reserved slots in place, production_rows).
        return parameters.get("item") == step.parameters.get("item")
    return parameters == step.parameters


def _rate_covers(row: Mapping, step: Step) -> bool:
    """A produce row's rate reaches the step's; other capabilities have no rate rule here."""
    if step.capability != PRODUCE_CAPABILITY:
        return True
    rate, wanted = (row.get("parameters") or {}).get("rate_per_minute"), step.parameters.get("rate_per_minute")
    if not isinstance(wanted, (int, float)):
        return True
    return isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate >= wanted


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
        return any(
            _same(job, step) and job.get("status") == "succeeded" and _rate_covers(job, step) for job in self.jobs
        )

    def stalled(self, step: Step) -> bool:
        """A matching job is blocked or failed; the planner owns that blocker."""
        return any(_same(job, step) and job.get("status") in STALLED_STATUSES for job in self.jobs)

    def in_flight(self, step: Step) -> bool:
        """A matching job exists that has not reached a terminal status."""
        return any(_same(job, step) and job.get("status") not in TERMINAL_STATUSES for job in self.jobs)


def _by_id(steps: Iterable[Step] = ROADMAP) -> dict[str, Step]:
    return {step.id: step for step in steps}


# Generated tail rows a receipt may carry (``rocket_plan.tail``): plain ids and JSON parameters.
_TAIL_ID = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def tail_steps(observation: Mapping) -> tuple[Step, ...]:
    """Steps after ``ROADMAP`` from the receipt's ``rocket_plan["tail"]`` (``rocket_plan.receipt``).

    The tail is generated from the catalog (the rocket silo technology closure,
    the silo, part and payload bills, the producer of every item), so the roadmap
    reaches a launch without a hand-written step per item. Malformed rows and ids
    that clash with ``ROADMAP`` are dropped; ``after`` keeps only ids the
    combined roadmap has (an unknown id would never be satisfied).
    """
    plan = observation.get("rocket_plan") if isinstance(observation, Mapping) else None
    rows = plan.get("tail") if isinstance(plan, Mapping) else None
    if not isinstance(rows, list):
        return ()
    known = {step.id for step in ROADMAP}
    steps: list[Step] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        sid, capability, parameters = row.get("id"), row.get("capability"), row.get("parameters")
        after = row.get("after")
        if (
            not isinstance(sid, str)
            or not sid
            or len(NODE_PREFIX + sid) > 64
            or not set(sid) <= _TAIL_ID
            or sid in known
            or not isinstance(capability, str)
            or not isinstance(parameters, Mapping)
            or not isinstance(after, list)
        ):
            continue
        steps.append(Step(sid, capability, dict(parameters), tuple(a for a in after if a in known)))
        known.add(sid)
    return tuple(steps)


def roadmap_for(observation: Mapping) -> tuple[Step, ...]:
    """``ROADMAP`` plus the generated rocket tail (``tail_steps``)."""
    return ROADMAP + tail_steps(observation or {})


def _recipes_ready(step: Step, capabilities: Mapping, observation: Mapping) -> bool:
    """Native recipe locks from the receipt's gates, else from its enabled recipes; unknown means ready."""
    gates = observation.get("native_capability_gates")
    if isinstance(gates, Mapping) and isinstance(gates.get(step.capability), Mapping):
        return gates[step.capability].get("native_recipes_ready") is not False
    required = capabilities[step.capability].get("native_recipes") or ()
    if not required or "enabled_recipes" not in observation:
        return True
    return set(required) <= _names(observation["enabled_recipes"])


def gate_ready(step: Step, jobs, observation: Mapping) -> bool:
    """A step's ``gate`` rule; a step without one is ready.

    ``machine_made_belts``: ``power_coal_feed.feed_step_ready`` (belts come
    from a supply cell or produce row, the fallback time passed, or the power
    job of an old map already built the feed).
    ``starter_coal_retired``: see ``_starter_coal_retired``.
    """
    if step.gate == "machine_made_belts":
        from .power_coal_feed import feed_step_ready

        return feed_step_ready(jobs, observation)
    if step.gate == "starter_coal_retired":
        return _starter_coal_retired(jobs, observation)
    return step.gate is None


# Game ticks after the main bus before the dedicated coal bank stops waiting
# for the starter coal cells (30 min): a map where they are never retired
# must still get its coal bank attempt.
STARTER_COAL_WAIT_TICKS = 108000


def _starter_coal_retired(jobs, observation: Mapping) -> bool:
    """The starter coal cells are gone (their retirement job succeeded), or the wait ran out."""
    bus_tick = None
    for job in jobs:
        if job.get("status") != "succeeded":
            continue
        if job.get("capability") == "starter_retirement" and job.get("parameters", {}).get("resource") == "coal":
            return True
        if job.get("capability") == "planned_main_bus":
            bus_tick = job.get("last_tick")
    tick = observation.get("tick")
    if isinstance(bus_tick, (int, float)) and isinstance(tick, (int, float)):
        return tick - bus_tick >= STARTER_COAL_WAIT_TICKS
    return bus_tick is None


def _research_ready(step: Step, observation: Mapping) -> bool:
    """A research step waits until the receipt lists its technology as researchable now."""
    if step.capability != RESEARCH_CAPABILITY:
        return True
    available = observation.get("available_research")
    if not isinstance(available, Mapping) or not isinstance(available.get("technologies"), list):
        return True
    listed = {row.get("technology") for row in available["technologies"] if isinstance(row, Mapping)}
    return step.parameters["technology"] in listed


def _packs_unsupplied(step: Step, observation: Mapping, succeeded: set[str]) -> list[str]:
    """Science packs of a research step whose producer block has no succeeded job (sorted).

    Packs come from the receipt's ``available_research`` row of the technology;
    a pack's producer is ``normal_research.PACK_PRODUCERS``. Such a step is not
    proposed: its job could only wait (``waiting_for_science_pack_supply``) and
    hold the research lane, while the producer is its own roadmap step.
    """
    if step.capability != RESEARCH_CAPABILITY:
        return []
    from .normal_research import PACK_PRODUCERS

    available = observation.get("available_research")
    rows = available.get("technologies") if isinstance(available, Mapping) else None
    row = next(
        (r for r in rows or () if isinstance(r, Mapping) and r.get("technology") == step.parameters["technology"]),
        None,
    )
    packs = {i.get("name") for i in (row or {}).get("science_ingredients") or () if isinstance(i, Mapping)}
    # Red science has its own bootstrap path (startup science, finite batches): only
    # later packs hold a research step back.
    packs.discard("automation-science-pack")
    return sorted(p for p in packs if p in PACK_PRODUCERS and PACK_PRODUCERS[p] not in succeeded)


def _rocket_inputs_ready(step: Step, observation: Mapping) -> bool:
    """A rocket stage waits until the ``rocket_stages`` receipt finds every input produced somewhere.

    ``rocket_stages.receipt``: each hand-craft leaf of the silo, of one rocket's parts or of the
    payload is carried or published by a block's output port. Without the receipt (or a row for
    the stage) the stage is proposed and its job names what it waits for.
    """
    stages = observation.get("rocket_stages")
    row = stages.get(step.capability) if isinstance(stages, Mapping) else None
    return not (isinstance(row, Mapping) and row.get("ready") is False)


def _natively_queued(step: Step, observation: Mapping) -> bool:
    """A research step whose technology already sits in the native research queue needs no job.

    The queue researches it; the step is satisfied once it is researched, so
    roadmap research steps no longer wait behind each other as separate jobs.
    """
    if step.capability != RESEARCH_CAPABILITY:
        return False
    queue = observation.get("research_queue")
    names = set(queue) if isinstance(queue, list) else set()
    if isinstance(observation.get("research"), str):
        names.add(observation["research"])
    return step.parameters.get("technology") in names


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
    item is skipped (outposts then grow by ``throughput_growth``). Requires
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
    from .starter_retirement import retired_by_jobs  # a permanent source replaced the starter cells

    outposts |= retired_by_jobs(jobs)
    from .mining import UNLOCK_TECHNOLOGY, _researched

    if _researched(observation.get("technologies"), UNLOCK_TECHNOLOGY):
        # The permanent replacement is planned and buildable: its job is active and the drill is
        # researched. A new starter cell would compete with it for the same plates and actor
        # time and be retired soon after (owner rule: the transitional layer does not grow;
        # eval-4177156614 added 5 coal cells at 23.6 min, 0.2 min before the coal outpost).
        outposts |= {
            (job.get("parameters") or {}).get("resource")
            for job in jobs
            if job.get("capability") == "mining_outpost" and job.get("status") not in TERMINAL_STATUSES | {"paused"}
        }
    from .power_coal_feed import hand_fed_coal_per_minute  # the hand-fed boiler burns starter coal too

    try:
        decisions = plan_capacity(
            observation.get("resource_supply_options") or {},
            observation.get("roadmap_material_demand") or {},
            hand_fed_coal_per_minute=hand_fed_coal_per_minute(jobs, observation),
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return []
    from .mining import BULK_RESOURCES, CELLS_AFTER_UNLOCK, UNLOCK_TECHNOLOGY, closed_by_unlock

    technologies = observation.get("technologies")
    # The drill research running now: a new plate cell would finish around the unlock and be
    # closed by it; the outposts already wait for that research (early_start), so none is proposed.
    unlocking = observation.get("research") == UNLOCK_TECHNOLOGY
    queued_ids = {node.get("id") for node in queue_nodes}
    nodes = []
    for decision in decisions:
        target = decision.target_cells
        if closed_by_unlock(decision.resource, target, technologies):
            target = CELLS_AFTER_UNLOCK  # the capability refuses more (StarterCapacity.step)
        elif unlocking and decision.resource in BULK_RESOURCES:
            continue
        node_id = f"{NODE_PREFIX}capacity_{decision.resource}_{target}"
        if decision.resource in outposts or node_id in queued_ids:
            continue
        if target <= decision.current_cells:
            continue
        if _capacity_taken(decision.resource, target, jobs, queue_nodes):
            continue
        nodes.append(
            {
                "id": node_id,
                "capability": CAPACITY_CAPABILITY,
                "parameters": {"resource": decision.resource, "cells": target},
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
    roadmap = roadmap_for(observation)
    steps = _by_id(roadmap)
    queued_ids = {node.get("id") for node in queue_nodes}
    proposed: list[dict] = capacity_nodes(jobs, queue_nodes, observation, capabilities)[: max(0, limit)]
    proposed += produce_nodes(jobs, queue_nodes, observation, capabilities)[: max(0, limit - len(proposed))]
    proposed += smelting_capacity_nodes(jobs, queue_nodes, observation, capabilities)[: max(0, limit - len(proposed))]
    proposed += steel_capacity_nodes(jobs, queue_nodes, observation, capabilities)[: max(0, limit - len(proposed))]
    from .early_start import early_ready
    from .supply_cell import supply_cell_nodes  # temporary hand-fed cell: build once, retire once

    proposed += supply_cell_nodes(jobs, queue_nodes, observation, capabilities)[: max(0, limit - len(proposed))]
    from .throughput_growth import throughput_growth_nodes  # plate growth: critical class before the roadmap

    seen = list(queue_nodes) + proposed
    proposed += throughput_growth_nodes(jobs, seen, observation, capabilities, critical=True)[
        : max(0, limit - len(proposed))
    ]
    from .mine_trunk import mine_trunk_nodes  # patch bank grows in place when its trunk's taps outgrow it

    proposed += mine_trunk_nodes(jobs, list(queue_nodes) + proposed, observation, capabilities)[
        : max(0, limit - len(proposed))
    ]
    from .power_coal_tap import power_coal_tap_nodes  # boiler coal moves onto the coal trunk once

    proposed += power_coal_tap_nodes(jobs, list(queue_nodes) + proposed, observation, capabilities)[
        : max(0, limit - len(proposed))
    ]
    for index, step in enumerate(roadmap):
        if len(proposed) >= limit:
            break
        node_id = NODE_PREFIX + step.id
        if step.capability not in capabilities or progress.satisfied(step):
            continue
        if node_id in queued_ids or any(_same(node, step) for node in queue_nodes):
            continue
        if progress.in_flight(step) or progress.stalled(step):
            continue
        early = ""
        if (
            not set(capabilities[step.capability].get("requires", [])) <= succeeded
            or not all(progress.satisfied(steps[name]) for name in step.after)
            or not _recipes_ready(step, capabilities, observation)
        ):
            # Early start (early_start.py): inputs planned, prerequisites in flight.
            early = early_ready(step, progress, steps, capabilities, observation, jobs)
            if early is None:
                continue
        if not _research_ready(step, observation) or _packs_unsupplied(step, observation, succeeded):
            continue
        if _natively_queued(step, observation) or not gate_ready(step, jobs, observation):
            continue
        if not _rocket_inputs_ready(step, observation):
            continue
        parameters, note = _sized_parameters(step, observation)
        note += early
        proposed.append(
            {
                "id": node_id,
                "capability": step.capability,
                "parameters": parameters,
                "priority": step.priority if step.priority is not None else TOP_PRIORITY - index,
                "depends_on": [],
                "reason": f"autopilot roadmap step {index + 1}/{len(roadmap)}: {step.id}{note}"[:300],
            }
        )
    proposed += throughput_growth_nodes(jobs, list(queue_nodes) + proposed, observation, capabilities, critical=False)[
        : max(0, limit - len(proposed))
    ]
    proposed += science_growth_nodes(jobs, queue_nodes, observation, capabilities)[: max(0, limit - len(proposed))]
    proposed += defense_growth_nodes(jobs, queue_nodes, observation, capabilities)[: max(0, limit - len(proposed))]
    from .starter_retirement import starter_retirement_nodes  # mine starter cells a permanent source replaced

    proposed += starter_retirement_nodes(jobs, queue_nodes, observation, capabilities)[: max(0, limit - len(proposed))]
    from .wood_burner_feed import wood_burner_feed_nodes  # wood into the belt-fed boilers, once

    proposed += wood_burner_feed_nodes(jobs, queue_nodes, observation, capabilities)[: max(0, limit - len(proposed))]
    from .trunk_return_feed import trunk_return_feed_nodes  # surplus back onto its patch trunk, once per trunk

    proposed += trunk_return_feed_nodes(jobs, queue_nodes, observation, capabilities)[: max(0, limit - len(proposed))]
    from .head_rework import feed_head_rework_nodes  # a district bank head that limits growth, once per bank

    proposed += feed_head_rework_nodes(jobs, queue_nodes, observation, capabilities)[: max(0, limit - len(proposed))]
    from .science_retirement import science_retirement_nodes  # obsolete startup science cell

    proposed += science_retirement_nodes(jobs, queue_nodes, observation, capabilities)[: max(0, limit - len(proposed))]
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


def defense_growth_nodes(jobs, queue_nodes, observation, capabilities) -> list[dict]:
    """One ``defense_growth`` node when the observe receipt ``defense_growth`` wants more posts.

    The receipt (``defense_perimeter.growth_receipt``) compares the current
    risk's post target with the posts the policy holds. Here only: the
    capability exists, ``defense_perimeter`` succeeded, no growth node or job
    is open, the same target has not stalled. The node id carries a
    ``DEFENSE_GROWTH_PERIOD_TICKS`` bucket, so a target is re-evaluated
    periodically instead of once per map.
    """
    if DEFENSE_GROWTH_CAPABILITY not in capabilities:
        return []
    row = observation.get("defense_growth")
    if not isinstance(row, Mapping) or not row.get("grow"):
        return []
    posts = row.get("target_posts")
    tick = observation.get("tick")
    if type(posts) is not int or not 1 <= posts <= 16 or type(tick) is not int:
        return []
    succeeded = {job.get("capability") for job in jobs if job.get("status") == "succeeded"}
    if not set(capabilities[DEFENSE_GROWTH_CAPABILITY].get("requires", [])) <= succeeded:
        return []
    parameters = {"posts": posts}
    for node in queue_nodes:
        if node.get("capability") == DEFENSE_GROWTH_CAPABILITY and node.get("status", "pending") in OPEN_NODE_STATUSES:
            return []
    for job in jobs:
        if job.get("capability") != DEFENSE_GROWTH_CAPABILITY:
            continue
        if job.get("status") not in TERMINAL_STATUSES:
            return []
        if job.get("status") in STALLED_STATUSES and job.get("parameters") == parameters:
            return []
    node_id = f"{NODE_PREFIX}defense_growth_{posts}_{tick // DEFENSE_GROWTH_PERIOD_TICKS}"
    if node_id in {node.get("id") for node in queue_nodes}:
        return []
    return [
        {
            "id": node_id,
            "capability": DEFENSE_GROWTH_CAPABILITY,
            "parameters": parameters,
            "priority": DEFENSE_GROWTH_PRIORITY,
            "depends_on": [],
            "reason": f"defense growth to {posts} posts: {row.get('reason', '')}"[:300],
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


# Roadmap steps whose material bill counts as upcoming demand for capacity
# decisions. With 4, the first steps (power, two researches, electric coal)
# hid the outposts and the smelting district (hundreds of belts) until they
# were next, so plate capacity grew too late (user rule: think 3-4 steps
# beyond the current one; the heavy steps are 6-9 at the start).
LOOKAHEAD_STEPS = 8


def lookahead_demand(catalog, jobs, observation, *, horizon=LOOKAHEAD_STEPS) -> dict[str, float]:
    """Raw items the next ``horizon`` unsatisfied roadmap steps will need.

    Research steps add ``unit_count`` x pack raw cost; steps with ``builds``
    add the raw cost of that hardware, steps without them the bill their
    capability's plan fixes (``step_bill``, ``PLAN_BILLS``). Stock is not
    subtracted (the supply ``shortfall`` covers the current bill). Other steps
    add nothing, so stages past the early game are under-estimated. Steps already
    in flight are included and may overlap their saved construction bill.
    """
    researched = _names((observation or {}).get("technologies"))
    items: dict[str, float] = {}
    for step in _upcoming(jobs, researched, horizon):
        for name, count in _research_items(catalog, step).items():
            items[name] = items.get(name, 0.0) + count
        for name, count in step_bill(catalog, step).items():
            items[name] = items.get(name, 0.0) + count
    return {name: round(amount, 3) for name, amount in raw_cost(catalog, items).items()}


def _green_cluster_bill(catalog) -> dict[str, float]:
    """The logistic science unit ``green_science.green_cluster`` places (its taps and lab route are map-dependent)."""
    from .green_science import green_cluster

    return dict(Counter(e["name"] for e in green_cluster(catalog, (0.5, 0.5))["entities"]))


def _steam_pair_bill(catalog) -> dict[str, float]:
    """The next ``steam_bank`` pair (boiler, two engines, stub, poles) plus its own paired coal bank."""
    from .core.factory_templates import build_template

    bill = Counter(e["name"] for e in build_template("steam_bank")["entities"])
    bill.update(MINING_OUTPOST_BUILDS)
    return dict(bill)


# Steps without ``builds`` whose main hardware is fixed by the capability's own plan
# template (computed from the catalog, so it follows the template). Not derived (the
# plan depends on the map or the threat): defense_perimeter, dedicated_materials,
# steel/brick blocks, oil, chemical and later steps.
PLAN_BILLS = {"logistic_science": _green_cluster_bill, "steam_expansion": _steam_pair_bill}


def step_bill(catalog, step: Step) -> Mapping[str, float]:
    """``step.builds``, else the bill ``PLAN_BILLS`` derives from the capability's plan, else nothing."""
    if step.builds:
        return step.builds
    derive = PLAN_BILLS.get(step.capability)
    if derive is None:
        return {}
    try:
        return derive(catalog)
    except Exception:  # a demand estimate never breaks the lookahead (a test catalog lacks the recipe tree)
        return {}


def roadmap_wanted_recipes(jobs, capabilities, *, horizon=6) -> list[str]:
    """``native_recipes`` of the next ``horizon`` unsatisfied steps, in roadmap order, without repeats."""
    recipes: list[str] = []
    for step in _upcoming(jobs, (), horizon):
        recipes.extend(capabilities.get(step.capability, {}).get("native_recipes", []))
    return list(dict.fromkeys(recipes))


# Research a step performs without naming it in its parameters. Without this
# the research-capacity decision saw only the current technology and judged a
# second lab or science machine "not worth it" although logistics came next.
STEP_TECHNOLOGY = {"bootstrap_logistics": "logistics"}


def roadmap_wanted_technologies(jobs, technologies_researched, *, horizon=6) -> list[str]:
    """Technologies named by the next ``horizon`` unsatisfied steps, in roadmap order."""
    steps = _upcoming(jobs, _names(technologies_researched), horizon)
    names = [step.parameters.get("technology") or STEP_TECHNOLOGY.get(step.capability) for step in steps]
    return [name for name in names if name]


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


SMELTING_CAPACITY = "smelting_capacity"
SMELTING_CAPACITY_PRIORITY = PRODUCE_PRIORITY - 1
# Grow a district bank once bus taps claim this share of its nominal plates,
# to the claims times SMELTING_HEADROOM (capped by the bank's reserved slots
# and the connected ore).
SMELTING_GROW_AT = 0.8
SMELTING_HEADROOM = 1.25


def smelting_capacity_nodes(jobs, queue_nodes, observation, capabilities) -> list[dict]:
    """``smelting_capacity`` nodes when bus taps claim most of a district bank's plates.

    Reads the receipt's ``smelting_throughput_bounds`` rows (``diagnostics``):
    ``nominal_capacity_per_minute``, ``full_bank_capacity_per_minute`` (all
    reserved slots built), ``claimed_bus_demand_per_minute`` (declared tap
    demand) and ``connected_nominal_ore_supply``. One open or stalled
    ``smelting_capacity`` row per resource at a time; a full bank proposes
    nothing (the next step is another bank, see ``smelting_site``).
    """
    if SMELTING_CAPACITY not in capabilities:
        return []
    succeeded = {job.get("capability") for job in jobs if job.get("status") == "succeeded"}
    if not set(capabilities[SMELTING_CAPACITY].get("requires", [])) <= succeeded:
        return []
    bounds = observation.get("smelting_throughput_bounds")
    rows = bounds.get("banks") if isinstance(bounds, Mapping) else None
    nodes = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        resource = row.get("resource")
        values = [
            row.get(k)
            for k in (
                "nominal_capacity_per_minute",
                "full_bank_capacity_per_minute",
                "claimed_bus_demand_per_minute",
                "connected_nominal_ore_supply",
            )
        ]
        if resource not in ("iron-ore", "copper-ore") or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) for v in values
        ):
            continue
        nominal, full, claimed, ore = (float(v) for v in values)
        if nominal <= 0 or claimed < nominal * SMELTING_GROW_AT or nominal >= full:
            continue
        target = round(min(full, ore, claimed * SMELTING_HEADROOM, 450.0), 1)
        if target <= nominal:
            continue

        def covers(r):
            parameters = r.get("parameters")
            if r.get("capability") != SMELTING_CAPACITY or not isinstance(parameters, Mapping):
                return False
            if parameters.get("resource") != resource:
                return False
            rate = parameters.get("rate_per_minute")
            done = isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate >= target
            return r.get("status") != "succeeded" or done

        busy = any(covers(r) for r in list(jobs) + list(queue_nodes))
        if busy:
            continue
        nodes.append(
            {
                "id": f"{NODE_PREFIX}smelting_capacity_{resource}_{int(target)}",
                "capability": SMELTING_CAPACITY,
                "parameters": {"resource": resource, "rate_per_minute": target},
                "priority": SMELTING_CAPACITY_PRIORITY,
                "depends_on": [],
                "reason": (
                    f"bus taps claim {claimed:g}/min of {row.get('product')}, bank nominal {nominal:g}/min; "
                    f"grow in reserved slots to {target:g}/min (bank full at {full:g})"
                )[:300],
            }
        )
    return nodes


STEEL_CAPACITY = "steel_capacity"


def steel_capacity_nodes(jobs, queue_nodes, observation, capabilities) -> list[dict]:
    """``steel_capacity`` nodes when consumers claim most of the planned steel block's output.

    Same trigger as ``smelting_capacity_nodes``: reads the receipt's
    ``furnace_block_claims`` (``planned_furnaces.furnace_block_claims``: built
    and full nameplate, declared demand on the product port); grows when the
    claims reach ``SMELTING_GROW_AT`` of the built rate, to the claims times
    ``SMELTING_HEADROOM`` capped by the reserved slots. One open or stalled
    row at a time; a full block proposes nothing.
    """
    if STEEL_CAPACITY not in capabilities:
        return []
    succeeded = {job.get("capability") for job in jobs if job.get("status") == "succeeded"}
    if not set(capabilities[STEEL_CAPACITY].get("requires", [])) <= succeeded:
        return []
    claims = observation.get("furnace_block_claims")
    rows = claims.get("blocks") if isinstance(claims, Mapping) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping) or row.get("kind") != "steel":
            continue
        values = [
            row.get(k)
            for k in ("nominal_capacity_per_minute", "full_block_capacity_per_minute", "claimed_port_demand_per_minute")
        ]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values):
            continue
        nominal, full, claimed = (float(v) for v in values)
        if nominal <= 0 or claimed < nominal * SMELTING_GROW_AT or nominal >= full:
            continue
        target = round(min(full, claimed * SMELTING_HEADROOM), 1)
        if target <= nominal:
            continue

        def covers(r):
            parameters = r.get("parameters")
            if r.get("capability") != STEEL_CAPACITY or not isinstance(parameters, Mapping):
                return False
            rate = parameters.get("rate_per_minute")
            done = isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate >= target
            return r.get("status") != "succeeded" or done

        if any(covers(r) for r in list(jobs) + list(queue_nodes)):
            continue
        return [
            {
                "id": f"{NODE_PREFIX}steel_capacity_{int(target)}",
                "capability": STEEL_CAPACITY,
                "parameters": {"rate_per_minute": target},
                "priority": SMELTING_CAPACITY_PRIORITY,
                "depends_on": [],
                "reason": (
                    f"consumers claim {claimed:g}/min of steel, block nominal {nominal:g}/min; "
                    f"grow in reserved slots to {target:g}/min (block full at {full:g})"
                )[:300],
            }
        ]
    return []


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
        # A row whose plate has no belt source yet (no published bus lane, no free board
        # port) would only block production_row_input_source_missing; it is proposed once a
        # source exists (all-1113-2107: 6 rows in 3 runs blocked for the rest of the run).
        sources = observation.get("produce_plate_sources")
        if isinstance(sources, Mapping) and any(sources.get(plate) is False for plate in raw):
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
