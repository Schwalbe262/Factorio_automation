"""Staged build of the permanent red science block: reserve every slot, build a small start, grow by evidence.

``science.science_block`` reserves a full row (``science_row`` machine slots
and eight ``lab_row`` lab slots, belts sized for the full row). The first
build installs only ``START_LABS`` labs and the science machines those labs
and the connected plate supply justify; the other slots stay in the board
blocks (occupied for every other planner) and are listed as
``stage["growth_entities"]`` in ``automation-science.json``.

Slot geometry is the template's entity order, which a site transform keeps:
``science_row`` emits four entities per machine slot (assembler, two
inserters, pole), ``lab_row`` three per lab slot (lab, inserter, pole). A
built subset is always a prefix of the slots, so its poles stay one chain
and the block's first pole (slot 0, wired to the grid) is always built.

Growth rule (``growth_decision``, see ``docs/v2/PLANNERS.md``): labs grow
only while every built lab is working and packs back up; science machines
grow only while labs starve for packs and the machines are not short of
plates; how many is decided by ``research_capacity.plan_research_capacity``
with those two permissions as its limits. Pure functions, no IO.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from .research_capacity import (
    RED_PACK,
    ResearchCapacityConfig,
    ResearchHardware,
    ResearchMaterials,
    expansion_costs,
    lab_pack_rate,
    plan_research_capacity,
)

# Two labs: the cheapest pair that already doubles research over the startup
# lab; each further lab (10 circuits, 10 gears, 4 belts) waits for evidence.
START_LABS = 2
# At most two red machines at first: two assembling-machine-1 make 12 packs/min,
# what two labs consume on 10 s units; slower units justify fewer.
START_SCIENCE_MACHINES = 2
# A working lab holding this many packs has packs waiting for it.
LAB_BACKUP_PACKS = 2
# Red packs on the lab row belt that no lab takes: the feed is backing up.
BELT_BACKUP_PACKS = 8
LAB_SLOT = ("lab", "inserter", "small-electric-pole")
MACHINE_SLOT = ("assembling-machine-1", "inserter", "inserter", "small-electric-pole")


def _key(e):
    return (e["name"], e["position"]["x"], e["position"]["y"])


def _slots(plan, shape):
    count = plan.get("count") if isinstance(plan, Mapping) else None
    entities = plan.get("entities") if isinstance(plan, Mapping) else None
    if type(count) is not int or count < 1 or not isinstance(entities, list) or len(entities) < count * len(shape):
        return None
    slots = []
    for i in range(count):
        group = entities[i * len(shape) : (i + 1) * len(shape)]
        if tuple(e.get("name") for e in group) != shape:
            return None
        slots.append(group)
    return slots


def lab_slots(labs_plan):
    """Per lab slot ``[lab, inserter, pole]`` of a ``belt_fed_lab_row`` plan, or None for another layout."""
    if not isinstance(labs_plan, Mapping) or labs_plan.get("template") != "belt_fed_lab_row":
        return None
    return _slots(labs_plan, LAB_SLOT)


def machine_slots(science_plan):
    """Per machine slot ``[assembler, inserter, inserter, pole]`` of a red science row, or None."""
    if not isinstance(science_plan, Mapping) or science_plan.get("template") != "permanent_red_science_row":
        return None
    return _slots(science_plan, MACHINE_SLOT)


def start_machines(targets, nominal_each, plate_machines, labs=START_LABS):
    """Science machines the first build installs: what ``labs`` consume, within plates and ``START_SCIENCE_MACHINES``.

    ``ceil(lab_pack_rate(targets, labs) / nominal_each)``; one machine when no
    queued technology uses red packs (growth adds more on evidence).
    """
    cap = min(START_SCIENCE_MACHINES, plate_machines)
    rate = lab_pack_rate(targets, labs) if targets else None
    if rate is None or not nominal_each or nominal_each <= 0:
        return 1
    return max(1, min(cap, math.ceil(rate / nominal_each - 1e-9)))


def new_stage(state, *, labs, machines, machine_limit):
    """The ``stage`` record for ``state`` (labs/machines built, slot counts, growth entities), or None when unstaged."""
    lab_rows, machine_rows = lab_slots(state.get("labs")), machine_slots(state.get("science"))
    if lab_rows is None or machine_rows is None:
        return None
    stage = {
        "labs": max(1, min(labs, len(lab_rows))),
        "machines": max(1, min(machines, len(machine_rows))),
        "lab_slots": len(lab_rows),
        "machine_slots": len(machine_rows),
        "machine_limit": max(1, min(machine_limit, len(machine_rows))),
    }
    stage["growth_entities"] = growth_entities(state, stage)
    return stage


def growth_entities(state, stage=None, *, labs=None, machines=None):
    """Reserved slot entities that are not part of the built stage (deep rows of the block plans)."""
    stage = stage if stage is not None else state.get("stage")
    if not stage:
        return []
    labs = stage["labs"] if labs is None else labs
    machines = stage["machines"] if machines is None else machines
    rows = []
    for group in (lab_slots(state["labs"]) or [])[labs:]:
        rows += [dict(e) for e in group]
    for group in (machine_slots(state["science"]) or [])[machines:]:
        rows += [dict(e) for e in group]
    return rows


def build_plan(state, *, labs=None, machines=None):
    """The builder plan for the built stage (or for ``labs``/``machines`` slots): the reserved plan minus growth slots."""
    plan = state["plan"]
    if not state.get("stage"):
        return plan
    deferred = {_key(e) for e in growth_entities(state, labs=labs, machines=machines)}
    if not deferred:
        return plan
    entities = [e for e in plan["entities"] if _key(e) not in deferred]
    extra = {k: v for k, v in plan.items() if k not in ("ok", "reason", "entities", "ports", "required_items")}
    from .core.deterministic_builder import _plan

    return _plan(entities, plan["ports"], **extra)


def built_labs(state):
    """Planned lab entities of the built stage (every planned lab for an unstaged state)."""
    labs = [e for e in state["labs"]["entities"] if e["name"] == "lab"]
    stage = state.get("stage")
    return labs[: stage["labs"]] if stage else labs


def built_machines(state):
    """Planned red science assemblers of the built stage (every planned one for an unstaged state)."""
    machines = [e for e in state["science"]["entities"] if e.get("recipe") == RED_PACK]
    stage = state.get("stage")
    return machines[: stage["machines"]] if stage else machines


def _observed(observation):
    return {_key(e): e for e in observation.get("entities") or () if isinstance(e, Mapping) and "position" in e}


def _powered(e):
    return bool(e) and e.get("electric_network_connected") is not False and (e.get("energy") or 0) > 0


def lab_activity(state, observation):
    """Observed evidence of the built labs and machines (statuses, lab and belt pack stock)."""
    actual = _observed(observation)
    labs = [actual.get(_key(e)) for e in built_labs(state)]
    machines = [actual.get(_key(e)) for e in built_machines(state)]
    belts = [actual.get(_key(e)) for e in state["labs"]["entities"] if e["name"] == "transport-belt"]
    return {
        "labs_observed": sum(1 for e in labs if e),
        "labs_powered": sum(1 for e in labs if _powered(e)),
        "lab_status": [(e or {}).get("status_name") for e in labs],
        "lab_packs": [int(((e or {}).get("inventory") or {}).get(RED_PACK, 0) or 0) for e in labs],
        "machines_observed": sum(1 for e in machines if e),
        "machine_status": [(e or {}).get("status_name") for e in machines],
        "belt_packs": sum(int(((e or {}).get("belt_inventory") or {}).get(RED_PACK, 0) or 0) for e in belts),
    }


def growth_receipt(root, catalog, jobs, observation):
    """``growth_decision`` for this runtime's saved science block, or None (no staged block of this world)."""
    import json
    from pathlib import Path

    from .research_capacity import _evidence_from_supply, queue_targets

    path = Path(root) / "automation-science.json"
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not state.get("stage") or state.get("world_id") != observation.get("world_id"):
        return None
    targets = queue_targets(catalog, jobs, observation)
    return growth_decision(catalog, state, observation, targets, _evidence_from_supply(observation))


def growth_decision(catalog, state, observation, targets, evidence):
    """Labs and science machines to add into reserved slots now (module rule), as a JSON-safe dict.

    ``targets`` is the research queue (``research_capacity.queue_targets``),
    ``evidence`` raw item rows as ``resource_supply_options[item]["evidence"]``.
    ``grow`` is True only when ``target_labs``/``target_machines`` exceed the
    built stage; ``reason`` names the rule that held growth back.
    """
    stage = state.get("stage")
    if not stage:
        return {"grow": False, "reason": "science block is not staged"}
    labs, machines = stage["labs"], stage["machines"]
    row = {"grow": False, "labs": labs, "machines": machines, "target_labs": labs, "target_machines": machines}
    activity = lab_activity(state, observation)
    row["evidence"] = activity
    if activity["labs_powered"] < labs or activity["machines_observed"] < machines:
        return dict(row, reason="built stage not fully observed and powered")
    research = observation.get("research")
    if not isinstance(research, str) or not research or not targets:
        return dict(row, reason="no research in progress")
    status = activity["lab_status"]
    idle = [s for s in status if s != "working"]
    backlog = (
        activity["belt_packs"] >= BELT_BACKUP_PACKS
        or all(n >= LAB_BACKUP_PACKS for n in activity["lab_packs"])
        or "full_output" in activity["machine_status"]
    )
    starving = "missing_science_packs" in status and activity["belt_packs"] == 0
    short = any(s in ("item_ingredient_shortage", "no_ingredients") for s in activity["machine_status"])
    free_labs = stage["lab_slots"] - labs
    free_machines = max(0, stage.get("machine_limit", stage["machine_slots"]) - machines)
    may_labs = not idle and backlog and free_labs > 0
    may_machines = starving and not short and free_machines > 0
    if not may_labs and not may_machines:
        if idle and not starving:
            why = "built labs are idle"
        elif starving and short:
            why = "labs starve but science machines lack plates"
        elif starving:
            why = "labs starve and every machine slot is built"
        elif not backlog:
            why = "labs are busy but packs do not back up"
        else:
            why = "every lab slot is built"
        return dict(row, reason=why)
    costs = expansion_costs(catalog, RED_PACK, current_assemblers=machines)
    stock = sum(activity["lab_packs"]) + activity["belt_packs"]
    hardware = ResearchHardware(
        {RED_PACK: machines}, {RED_PACK: costs["pack_rate_per_assembler"]}, labs, pack_stock={RED_PACK: stock}
    )
    materials = ResearchMaterials(
        evidence=evidence or {},
        assembler_cost={RED_PACK: costs["assembler"]},
        lab_cost=costs["lab"],
        pack_cost={RED_PACK: costs["pack"]},
        machines_per_assembler={RED_PACK: costs["machines_per_assembler"]},
    )
    config = ResearchCapacityConfig(
        max_assemblers=machines + (free_machines if may_machines else 0),
        max_labs=labs + (free_labs if may_labs else 0),
    )
    decision = plan_research_capacity(targets, hardware, materials, config=config)
    target_machines = decision.target_assemblers if decision.pack == RED_PACK else machines
    row.update(
        target_labs=max(labs, decision.target_labs),
        target_machines=max(machines, target_machines),
        reason=decision.reason[:300],
        permitted={"labs": may_labs, "machines": may_machines},
    )
    row["grow"] = row["target_labs"] > labs or row["target_machines"] > machines
    return row
