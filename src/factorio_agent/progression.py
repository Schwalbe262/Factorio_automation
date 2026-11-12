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

TERMINAL_STATUSES = frozenset({"succeeded", "blocked", "failed"})
STALLED_STATUSES = frozenset({"blocked", "failed"})
RESEARCH_CAPABILITY = "research"
NODE_PREFIX = "auto_"
# Autopilot nodes rank below the planner's usual non-negative priorities, so
# an explicit planner decision always runs first; earlier steps rank higher.
TOP_PRIORITY = -100


@dataclass(frozen=True)
class Step:
    """One roadmap entry: run ``capability`` with ``parameters`` once ``after`` steps are satisfied."""

    id: str
    capability: str
    parameters: dict[str, Any] = field(default_factory=dict)
    after: tuple[str, ...] = ()


def _research(technology: str, after: tuple[str, ...] = ()) -> Step:
    """A normal lab research step; its id is derived from the technology name."""
    return Step("research_" + technology.replace("-", "_"), RESEARCH_CAPABILITY, {"technology": technology}, after)


ROADMAP: tuple[Step, ...] = (
    Step("starter", "starter", {}),
    # Burner cells and the boiler all burn coal. Two starter coal drills could
    # not keep up on a live run, so widen coal before steam power is built.
    Step("starter_capacity_coal", "starter_capacity", {"resource": "coal", "cells": 4}),
    Step("power", "power", {}),
    Step("starter_capacity_iron", "starter_capacity", {"resource": "iron-ore", "cells": 2}),
    Step("starter_research_automation", "starter_research", {"technology": "automation"}),
    Step(
        "starter_research_electric_mining_drill",
        "starter_research",
        {"technology": "electric-mining-drill"},
        after=("starter_research_automation",),
    ),
    Step("electric_coal", "electric_coal", {}, after=("starter_research_electric_mining_drill",)),
    Step("bootstrap_logistics", "bootstrap_logistics", {}, after=("starter_research_automation",)),
    Step("mining_outpost_iron", "mining_outpost", {"resource": "iron-ore", "rate_per_minute": 60}),
    Step("mining_outpost_copper", "mining_outpost", {"resource": "copper-ore", "rate_per_minute": 60}),
    Step("mining_outpost_coal", "mining_outpost", {"resource": "coal", "rate_per_minute": 30}),
    Step(
        "smelting_district",
        "smelting_district",
        {},
        after=("mining_outpost_iron", "mining_outpost_copper", "mining_outpost_coal"),
    ),
    Step("paired_smelting", "paired_smelting", {}),
    Step("paired_mining", "paired_mining", {}),
    Step("automation_science", "automation_science", {"rate_per_minute": 30}),
    _research("logistic-science-pack"),
    _research("steel-processing"),
    _research("fast-inserter"),
    Step("logistic_science", "logistic_science", {"rate_per_minute": 30}),
    Step("steam_expansion", "steam_expansion", {}, after=("logistic_science",)),
    _research("gun-turret"),
    Step("defense_perimeter", "defense_perimeter", {}, after=("research_gun_turret", "steam_expansion")),
    Step("main_bus_factory", "main_bus_factory", {}),
    _research("advanced-material-processing"),
    _research("automation-2"),
    _research("engine"),
    _research("fluid-handling"),
    _research("oil-gathering"),
    Step("dedicated_materials_coal", "dedicated_materials", {"resource": "coal"}, after=("main_bus_factory",)),
    Step("dedicated_materials_stone", "dedicated_materials", {"resource": "stone"}, after=("main_bus_factory",)),
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


def _same(row: Mapping, step: Step) -> bool:
    return row.get("capability") == step.capability and row.get("parameters", {}) == step.parameters


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


def autopilot_nodes(jobs, queue_nodes, observation, capabilities, *, limit=3) -> list[dict]:
    """Up to ``limit`` new queue nodes for the earliest ready, unsatisfied, unrepresented roadmap steps.

    Node ids are ``"auto_" + step.id``. ``depends_on`` is always empty:
    a step is only proposed once every ``after`` step is satisfied, and
    satisfied dependencies are dropped. Priorities descend along the
    roadmap from ``TOP_PRIORITY``.
    """
    progress = _Progress(tuple(jobs), frozenset(_names(observation.get("technologies"))))
    succeeded = progress.succeeded_capabilities
    steps = _by_id()
    queued_ids = {node.get("id") for node in queue_nodes}
    proposed: list[dict] = []
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
        proposed.append(
            {
                "id": node_id,
                "capability": step.capability,
                "parameters": dict(step.parameters),
                "priority": TOP_PRIORITY - index,
                "depends_on": [],
                "reason": f"autopilot roadmap step {index + 1}/{len(ROADMAP)}: {step.id}",
            }
        )
    return proposed


def _upcoming(jobs, researched: Iterable[str], horizon: int) -> list[Step]:
    """The next ``horizon`` roadmap steps that are not satisfied yet."""
    progress = _Progress(tuple(jobs), frozenset(researched))
    return [step for step in ROADMAP if not progress.satisfied(step)][:horizon]


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
