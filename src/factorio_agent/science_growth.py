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

Growth rule (``growth_decision``, see ``docs/v2/PLANNERS.md``) follows the chain
plates -> pack machines -> labs: a lab idle for packs (``missing_science_packs``)
is a pack shortage, so the pack machines grow while every one works (the plate
supply is the limit when they lack ingredients); labs grow only when packs pile
up, or every lab works and ``research_capacity`` finds labs the limit. Only units
idle for lack of demand (no research, low power) hold growth back. How many is
decided by ``research_capacity.plan_research_capacity`` (the receipt's time
comparison) with those permissions as its limits; once that growth is justified,
``build_ahead`` may take a larger option of the same room when it covers the
longer-horizon science rate (``recommended_rates``) and its time now plus the
revisit cost (``throughput_growth.revisit_seconds``) is lower. Pure functions, no IO.
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
# A lab with this status waits for packs (supply), not for research (demand).
PACK_SHORT = "missing_science_packs"
MACHINE_INGREDIENT_SHORT = frozenset({"item_ingredient_shortage", "no_ingredients"})
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
    recommended = ((observation.get("research_capacity") or {}).get("recommended_rates") or {}).get(RED_PACK)
    return growth_decision(
        catalog, state, observation, targets, _evidence_from_supply(observation), expected=recommended
    )


def build_ahead(decision, expected, *, machines, labs):
    """``(extra_machines, extra_labs, receipt)``: the option minimising T + revisit cost (build ahead).

    A justified growth (``decision.expand``) may take a larger option of the same
    ``plan_research_capacity`` call (its room = the permitted reserved slots) when its
    time now plus the revisit cost (``throughput_growth.revisit_seconds``) is lower:
    ``expected`` is the longer-horizon red rate and lab count (``recommended_science_rate``,
    research_capacity receipt ``recommended_rates``); coverage of an option =
    min(pack rate / expected rate, labs / expected labs), the full option is the best
    coverage in the room (capped at 1); never slower than waiting. Without ``expected``
    the decision stands (flat demand).
    """
    from .throughput_growth import revisit_seconds

    base = next(
        (
            o
            for o in decision.options
            if o["extra_assemblers"] == decision.target_assemblers - decision.current_assemblers
            and o["extra_labs"] == decision.target_labs - decision.current_labs
        ),
        None,
    )
    rate = expected.get("rate_per_minute") if isinstance(expected, Mapping) else None
    want_labs = expected.get("labs") if isinstance(expected, Mapping) else None
    if base is None or not decision.expand or not rate or rate <= 0 or not want_labs or want_labs <= 0:
        return None

    # Coverage counts only what this decision may grow (a kind without room is not its gap).
    grow_machines = any(o["extra_assemblers"] for o in decision.options)
    grow_labs = any(o["extra_labs"] for o in decision.options)

    def cover(o):
        shares = [1.0]
        if grow_machines:
            shares.append(o["pack_rate_per_minute"] / rate)
        if grow_labs:
            shares.append((labs + o["extra_labs"]) / want_labs)
        return min(shares)

    wait = decision.wait_seconds
    larger = [
        o
        for o in decision.options
        if o["extra_assemblers"] >= base["extra_assemblers"]
        and o["extra_labs"] >= base["extra_labs"]
        and o is not base
        and math.isfinite(o["seconds"])
        and (not math.isfinite(wait) or o["seconds"] < wait)
    ]
    pool = [base] + larger
    full = max(pool, key=lambda o: (cover(o), -(o["extra_assemblers"] + o["extra_labs"])))

    def revisit(o):
        machines_left = max(0, full["extra_assemblers"] - o["extra_assemblers"])
        labs_left = max(0, full["extra_labs"] - o["extra_labs"])
        entities = machines_left * len(MACHINE_SLOT) + labs_left * len(LAB_SLOT)
        return revisit_seconds(cover(o), cover(full), 1.0, (machines_left > 0) + (labs_left > 0), entities)

    chosen = min(pool, key=lambda o: (round(o["seconds"] + revisit(o), 6), o["extra_assemblers"] + o["extra_labs"]))
    return (
        chosen["extra_assemblers"],
        chosen["extra_labs"],
        {
            "expected_rate_per_minute": rate,
            "expected_labs": want_labs,
            "quickest": f"+{base['extra_assemblers']} machines +{base['extra_labs']} labs",
            "quickest_seconds": round(base["seconds"]),
            "quickest_revisit_seconds": round(revisit(base)),
            "chosen": f"+{chosen['extra_assemblers']} machines +{chosen['extra_labs']} labs",
            "chosen_seconds": round(chosen["seconds"]),
            "chosen_revisit_seconds": round(revisit(chosen)),
            "coverage": round(cover(chosen), 2),
        },
    )


def growth_decision(catalog, state, observation, targets, evidence, *, expected=None):
    """Labs and science machines to add into reserved slots now (module rule), as a JSON-safe dict.

    ``targets`` is the research queue (``research_capacity.queue_targets``),
    ``evidence`` raw item rows as ``resource_supply_options[item]["evidence"]``,
    ``expected`` the longer-horizon red rate and labs (build ahead, ``build_ahead``).
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
    # Bottleneck chain plates -> pack machines -> labs. A lab idle for packs is a pack
    # shortage, not a lack of demand: the staged guard only holds units idle for demand.
    status = activity["lab_status"]
    idle_for_demand = [s for s in status if s not in ("working", PACK_SHORT)]
    backlog = (
        activity["belt_packs"] >= BELT_BACKUP_PACKS
        or all(n >= LAB_BACKUP_PACKS for n in activity["lab_packs"])
        or "full_output" in activity["machine_status"]
    )
    starving = PACK_SHORT in status and not backlog
    short = any(s in MACHINE_INGREDIENT_SHORT for s in activity["machine_status"])
    machines_busy = all(s == "working" for s in activity["machine_status"])
    labs_busy = all(s == "working" for s in status)
    free_labs = stage["lab_slots"] - labs
    free_machines = max(0, stage.get("machine_limit", stage["machine_slots"]) - machines)
    # Labs only while every lab works: with packs piling up, or when research_capacity finds labs the limit.
    may_labs = labs_busy and free_labs > 0
    may_machines = starving and not short and machines_busy and free_machines > 0
    row["chain"] = "demand" if idle_for_demand else "plates" if starving and short else "packs" if starving else "labs"
    if not may_labs and not may_machines:
        if idle_for_demand:
            why = f"built labs are idle without a pack shortage ({', '.join(sorted(set(idle_for_demand)))})"
        elif starving and short:
            why = (
                "labs starve for packs and the pack machines lack plates: the plate supply is the limit "
                "(throughput_growth grows it)"
            )
        elif starving and not machines_busy:
            why = "labs starve for packs but a pack machine is not working"
        elif starving:
            why = "labs starve for packs and every machine slot is built"
        elif not labs_busy:
            why = "a lab misses packs while packs back up: the lab feed, not a stage"
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

    def plan(lab_room):
        config = ResearchCapacityConfig(
            max_assemblers=machines + (free_machines if may_machines else 0), max_labs=labs + lab_room
        )
        return plan_research_capacity(targets, hardware, materials, config=config)

    decision = plan(free_labs if may_labs else 0)
    if decision.target_labs > labs and not backlog and decision.bottleneck_before != "labs":
        # Busy labs without a pack backlog: labs grow only when research_capacity finds them the limit.
        decision = plan(0)
    target_machines = decision.target_assemblers if decision.pack == RED_PACK else machines
    target_labs = decision.target_labs
    reason = decision.reason
    ahead = build_ahead(decision, expected, machines=machines, labs=labs) if decision.pack == RED_PACK else None
    if ahead is not None:
        extra_machines, extra_labs, row["build_ahead"] = ahead
        if (extra_machines, extra_labs) != (target_machines - machines, target_labs - labs):
            target_machines, target_labs = machines + extra_machines, labs + extra_labs
            b = row["build_ahead"]
            reason = (
                f"build ahead for {b['expected_rate_per_minute']}/min, {b['expected_labs']} labs: {b['chosen']} "
                f"{b['chosen_seconds']}s + revisit {b['chosen_revisit_seconds']}s < {b['quickest']} "
                f"{b['quickest_seconds']}s + revisit {b['quickest_revisit_seconds']}s; " + reason
            )
    row.update(
        target_labs=max(labs, target_labs),
        target_machines=max(machines, target_machines),
        reason=reason[:300],
        permitted={"labs": may_labs, "machines": may_machines},
    )
    row["grow"] = row["target_labs"] > labs or row["target_machines"] > machines
    return row
