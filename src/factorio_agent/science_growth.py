"""Staged build of the permanent red science block: reserve every slot, build a small start, grow by evidence.

``science.science_block`` reserves a full row (``science_row`` machine slots
and eight ``lab_row`` lab slots, belts sized for the full row). The machine
slots are the growth frame sized at plan time (``frame_size``: research_capacity
on the research bill ahead, bounded by ``SITE_MACHINE_SLOTS``). The first
build installs only ``START_LABS`` labs and the science machines those labs
and the connected plate supply justify; the other slots stay in the board
blocks (occupied for every other planner) and are listed as
``stage["growth_entities"]`` in ``automation-science.json``. A block reserved
with fewer slots than its frame (``frame_slot_groups``) widens its reservation
when growth needs it (``extended_state``, republished before it is built);
the machine limit is read live (frame and plate supply), never frozen.

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
revisit cost (``throughput_growth.revisit_seconds``) is lower. A full frame turns
to ``beyond_frame`` (second block or partial rebuild, same time comparison).
Pure functions, no IO except ``growth_receipt``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from .capacity_planning import finite_number, stock_on_hand
from .research_capacity import (
    RED_PACK,
    ResearchCapacityConfig,
    ResearchHardware,
    ResearchMaterials,
    expansion_costs,
    hardware_option,
    lab_pack_rate,
    plan_research_capacity,
    research_queue_seconds,
    split_producible,
)

# Two labs: the cheapest pair that already doubles research over the startup
# lab; each further lab (10 circuits, 10 gears, 4 belts) waits for evidence.
START_LABS = 2
# At most two red machines at first: two assembling-machine-1 make 12 packs/min,
# what two labs consume on 10 s units; slower units justify fewer.
START_SCIENCE_MACHINES = 2
# Machine slots one science row holds: its input/output belts and reserved
# square (``science.science_row``) are drawn for six slots whatever ``count`` is.
SITE_MACHINE_SLOTS = 6
# Technologies ahead that size the reserved frame at plan time (``frame_size``).
FRAME_HORIZON = 12
# Seconds to place one non-machine entity of another block (belt, pole), as throughput_growth.
ENTITY_BUILD_SECONDS = 3.0
# A machine swapped in place is mined and placed: two machine placements.
REBUILD_PLACEMENTS = 2
FAST_MACHINE = "assembling-machine-2"
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
    """Slot entity groups: ``count`` groups of ``shape``, or of the plan's own cycle ``slot_shapes``.

    A generated science variant (``science_layout``) whose slots differ (a direct-insertion
    pair: the gear machine belongs to the first slot of each pair) declares ``slot_shapes``,
    a list of name tuples taken in turn; the template rows have none and use ``shape``.
    """
    count = plan.get("count") if isinstance(plan, Mapping) else None
    entities = plan.get("entities") if isinstance(plan, Mapping) else None
    if type(count) is not int or count < 1 or not isinstance(entities, list):
        return None
    declared = plan.get("slot_shapes")
    cycle = [tuple(s) for s in declared] if isinstance(declared, list) and declared else [shape]
    slots, index = [], 0
    for i in range(count):
        names = cycle[i % len(cycle)]
        group = entities[index : index + len(names)]
        if len(group) != len(names) or tuple(e.get("name") for e in group) != names:
            return None
        slots.append(group)
        index += len(names)
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


def _shift(e, dx, dy):
    return dict(e, position={"x": e["position"]["x"] + dx, "y": e["position"]["y"] + dy})


def _tiles(entities):
    from .site.geometry import plan_rect

    return {tile for e in entities for tile in plan_rect(e).tiles()}


def _inside(area, entities):
    from .site.geometry import plan_rect

    (x0, y0), (x1, y1) = area
    for e in entities:
        r = plan_rect(e)
        if r.x0 < x0 - 1e-9 or r.y0 < y0 - 1e-9 or r.x1 > x1 + 1e-9 or r.y1 > y1 + 1e-9:
            return False
    return True


def _served(group, belts):
    """Every inserter of a slot reaches a planned belt on its outer side (input and output lines of the row)."""
    machine = group[0]["position"]
    for e in group:
        if e["name"] != "inserter":
            continue
        dx, dy = e["position"]["x"] - machine["x"], e["position"]["y"] - machine["y"]
        ux, uy = (dx > 0) - (dx < 0), (dy > 0) - (dy < 0)
        if (e["position"]["x"] + ux, e["position"]["y"] + uy) not in belts:
            return False
    return True


def frame_slot_groups(science_plan, others=()):
    """Machine slots the science row's frame holds: the planned slots, then the next ones along the slot pitch.

    A further slot is the last slot shifted by the pitch (slot 1 minus slot 0)
    while every entity lies inside the row's ``reserved_area``, its inserters
    reach the row's planned input and output belts (``_served``), it overlaps
    no planned entity (the row's, or ``others``: the block's routes and power
    links), and at most ``SITE_MACHINE_SLOTS``. A block planned with
    fewer slots (eval-4177156614: 2) therefore still has a frame of six: the
    belts and the reserved square were always sized for the full row.
    """
    slots = machine_slots(science_plan)
    if not slots:
        return []
    groups = [list(group) for group in slots]
    if science_plan.get("slot_shapes"):
        # A generated variant reserves its whole frame at plan time (``science_layout``).
        return groups
    if len(groups) < 2 or not science_plan.get("reserved_area"):
        return groups
    a, b = groups[0][0]["position"], groups[1][0]["position"]
    dx, dy = b["x"] - a["x"], b["y"] - a["y"]
    if not dx and not dy:
        return groups
    belts = {
        (e["position"]["x"], e["position"]["y"]) for e in science_plan["entities"] if e["name"] == "transport-belt"
    }
    taken = _tiles(list(science_plan["entities"]) + list(others))
    while len(groups) < SITE_MACHINE_SLOTS:
        nxt = [_shift(e, dx, dy) for e in groups[-1]]
        if not _inside(science_plan["reserved_area"], nxt) or not _served(nxt, belts) or _tiles(nxt) & taken:
            break
        taken |= _tiles(nxt)
        groups.append(nxt)
    return groups


def frame_capacity(state):
    """Machine slots growth may use: the frame (``frame_slot_groups``), or the reserved slots once an extension failed."""
    stage = state.get("stage") or {}
    reserved = stage.get("machine_slots", 0)
    if (state.get("frame") or {}).get("extension_blocked"):
        return reserved
    others = (state.get("plan") or {}).get("entities") or ()
    return max(reserved, len(frame_slot_groups(state.get("science"), others)))


def _fingerprint(plan):
    import hashlib
    import json

    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def extended_state(state, slots):
    """``state`` with the science row reserving ``slots`` machine slots (pure; the built stage is unchanged).

    New slots are inserted after the planned ones (entity order stays slot
    prefixes, then the belts) in the science plan and in the merged plan, the
    stage's ``machine_slots`` and growth entities follow, and ``frame``
    records what was replaced (``replaces``: fingerprint of the old science
    plan, so a board still holding it is republished, never refused).
    """
    from .core.deterministic_builder import _plan

    science = state["science"]
    old = machine_slots(science) or []
    groups = frame_slot_groups(science, state["plan"]["entities"])[:slots]
    if len(groups) <= len(old):
        return state
    added = [dict(e) for group in groups[len(old) :] for e in group]
    cut = len(old) * len(MACHINE_SLOT)
    keep = ("ok", "reason", "entities", "ports", "required_items")
    grown = _plan(
        science["entities"][:cut] + added + science["entities"][cut:],
        science["ports"],
        **{k: v for k, v in science.items() if k not in keep},
    )
    grown["count"] = len(groups)
    plan = state["plan"]
    merged = _plan(plan["entities"] + added, plan["ports"], **{k: v for k, v in plan.items() if k not in keep})
    out = dict(state, science=grown, plan=merged)
    out["stage"] = dict(state["stage"], machine_slots=len(groups))
    out["stage"]["growth_entities"] = growth_entities(out)
    previous = (state.get("frame") or {}).get("replaces") or []
    out["frame"] = {
        "extended_from": (state.get("frame") or {}).get("extended_from", len(old)),
        "replaces": previous + [_fingerprint(science)],
    }
    return out


def frame_size(catalog, targets, *, requested, start=START_SCIENCE_MACHINES, lab_slots=8):
    """Machine slots to reserve at plan time: ``research_capacity`` on the research bill ahead, bounded by the site.

    The planner runs on ``targets`` (the technologies ahead) from the staged
    start with funding assumed free (a reservation costs no items, only a
    built unit does), ``max_step`` and ``max_assemblers`` at the site's
    ``SITE_MACHINE_SLOTS`` and ``max_labs`` at ``lab_slots``: its target is
    the largest row whose last machine still pays off in research time.
    Never below ``requested`` slots; ``requested`` when nothing is queued.
    """
    if not targets:
        return min(SITE_MACHINE_SLOTS, requested)
    costs = expansion_costs(catalog, RED_PACK, current_assemblers=start)
    ample = {"available": 1e9, "produced_per_minute": 1e9, "net_per_minute": 1e9}
    items = set(costs["assembler"]) | set(costs["lab"]) | set(costs["pack"])
    decision = plan_research_capacity(
        targets,
        ResearchHardware({RED_PACK: start}, {RED_PACK: costs["pack_rate_per_assembler"]}, START_LABS),
        ResearchMaterials(
            evidence={item: dict(ample) for item in items},
            assembler_cost={RED_PACK: costs["assembler"]},
            lab_cost=costs["lab"],
            pack_cost={RED_PACK: costs["pack"]},
            machines_per_assembler={RED_PACK: costs["machines_per_assembler"]},
        ),
        config=ResearchCapacityConfig(
            max_step=SITE_MACHINE_SLOTS, max_assemblers=SITE_MACHINE_SLOTS, max_labs=max(START_LABS, lab_slots)
        ),
    )
    target = decision.target_assemblers if decision.pack == RED_PACK else start
    return max(1, min(SITE_MACHINE_SLOTS, max(requested, target)))


def plate_draw(state, machines):
    """Plates/minute ``machines`` red machines of ``state`` draw, per plate, from the declared taps.

    The taps were declared for ``stage["tap_machines"]`` machines (older
    states: ``machine_limit``, the plate-supported count they were sized
    for), so one machine draws ``declared / tap_machines`` of each plate.
    Empty without bus taps.
    """
    stage = state.get("stage") or {}
    basis = stage.get("tap_machines", stage.get("machine_limit"))
    taps = [t for t in state.get("taps") or () if isinstance(t, Mapping)]
    if not taps or type(basis) is not int or basis < 1:
        return {}
    declared = {}
    for tap in taps:
        item = tap.get("item")
        if isinstance(item, str):
            declared[item] = declared.get(item, 0.0) + float(tap.get("demand_per_minute") or 0.0)
    return {item: round(rate / basis * machines, 3) for item, rate in declared.items()}


def undeclared_plate_draw(state, receipt=None):
    """Plates/minute the science block draws (or is about to draw) beyond its declared taps, per plate.

    The draw follows the built stage and, while the receipt grows the
    machines, the growth target: ``throughput_growth`` adds it to the tap
    rate so plate growth sees the science growth before it is built.
    """
    stage = state.get("stage") if isinstance(state, Mapping) else None
    if not stage:
        return {}
    machines = stage.get("machines", 0)
    if isinstance(receipt, Mapping) and receipt.get("grow") and type(receipt.get("target_machines")) is int:
        machines = max(machines, receipt["target_machines"])
    declared = plate_draw(state, stage.get("tap_machines", stage.get("machine_limit")) or 0)
    wanted = plate_draw(state, machines)
    return {item: round(max(0.0, rate - declared.get(item, 0.0)), 3) for item, rate in wanted.items()}


def plate_machine_limit(machines, per_machine, evidence, seconds, spare=None):
    """Science machines the current plate supply carries: built plus what the headroom feeds, per plate the least.

    Headroom of a plate = ``max(0, net_per_minute, spare) + 60 * stock / seconds``
    (stock spread over the remaining research ``seconds``, as the planner's
    materials cap); a plate without evidence adds no machine. ``spare`` (plate ->
    per minute, ``district_spare``) is the district's unclaimed capacity while its
    furnaces hold full output: measured net = produced - consumed is then capped by
    consumption and says nothing about headroom.
    """
    extra = math.inf
    for item, rate in per_machine.items():
        if rate <= 0:
            continue
        row = (evidence or {}).get(item)
        if not isinstance(row, Mapping):
            return machines
        net = max(0.0, finite_number(row.get("net_per_minute")) or 0.0, (spare or {}).get(item, 0.0))
        stock = stock_on_hand(row)
        headroom = net + (60.0 * stock / seconds if math.isfinite(seconds) and seconds > 0 else 0.0)
        extra = min(extra, math.floor(headroom / rate + 1e-9))
    return machines + (0 if not math.isfinite(extra) else max(0, int(extra)))


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
    """The ``stage`` record for ``state`` (labs/machines built, slot counts, growth entities), or None when unstaged.

    ``machine_limit`` is the plate-supported count at plan time, kept as
    ``tap_machines`` (what the bus taps were declared for); growth reads the
    limit live (``growth_decision``), never from here.
    """
    lab_rows, machine_rows = lab_slots(state.get("labs")), machine_slots(state.get("science"))
    if lab_rows is None or machine_rows is None:
        return None
    limit = max(1, min(machine_limit, len(machine_rows)))
    stage = {
        "labs": max(1, min(labs, len(lab_rows))),
        "machines": max(1, min(machines, len(machine_rows))),
        "lab_slots": len(lab_rows),
        "machine_slots": len(machine_rows),
        "machine_limit": limit,
        "tap_machines": limit,
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
        # Every pack each built lab holds: a lab missing packs while red backs up misses another pack.
        "lab_stock": [
            {k: int(v or 0) for k, v in ((e or {}).get("inventory") or {}).items() if str(k).endswith("science-pack")}
            for e in labs
        ],
        "machines_observed": sum(1 for e in machines if e),
        "machine_status": [(e or {}).get("status_name") for e in machines],
        "belt_packs": sum(int(((e or {}).get("belt_inventory") or {}).get(RED_PACK, 0) or 0) for e in belts),
    }


LAB_BLOCKS = frozenset({"laboratories", "automation_science"})


def _provider_option(provider, **fields):
    """``growth_providers._option`` (imported lazily); the same field list when that module is absent."""
    try:
        from .growth_providers import _option
    except ImportError:
        keys = ("gain_per_minute", "cost", "seconds_saved")
        return {
            "provider": provider,
            "kind": fields.get("kind", "in_place"),
            "capability": fields.get("capability"),
            "parameters": fields.get("parameters"),
            "nodes": list(fields.get("nodes") or ()),
            **{k: fields.get(k) for k in keys},
            "feasible": bool(fields.get("feasible", True)),
            "reason": str(fields.get("reason", ""))[:240],
        }
    return _option(provider, **fields)


def growth_options(bottleneck, observation, state) -> list[dict]:
    """Growth options for a science-pack or lab bottleneck of any pack type (bottleneck auditor hook).

    The red block and labs read the ``science_growth`` receipt, every other pack its
    ``pack_growth[pack]`` receipt (``pack_growth.growth_decision``): one rule, one receipt.
    ``state`` holds ``jobs``, ``queue_nodes`` and ``capabilities`` (``growth_providers``).
    A growing receipt gives an in-place option with its ``science_growth`` node; a waiting one an
    infeasible option naming why; no slot left adds the next unit (second unit / rebuild,
    not built yet). Labs without research in progress are a research-queue problem, never a lab
    or pack surplus: the option says so and nothing grows.
    """
    pack = str((bottleneck or {}).get("item") or "")
    red = pack == RED_PACK or not pack.endswith("science-pack") or (bottleneck or {}).get("block") in LAB_BLOCKS
    research = observation.get("research")
    if not isinstance(research, str) or not research:
        return [
            _provider_option(
                "science",
                feasible=False,
                reason="no research in progress: the research queue is the limit (research_queue), not labs or packs",
            )
        ]
    if red:
        row, parameters = observation.get("science_growth"), None
        if isinstance(row, Mapping):
            parameters = {"labs": row.get("target_labs"), "machines": row.get("target_machines")}
        missing = row.get("missing_pack") if isinstance(row, Mapping) else None
        if missing and not row.get("grow"):
            # Red backs up while the labs miss another pack: that pack's block is the bottleneck.
            row = ((observation.get("pack_growth") or {}).get(missing)) or row
            parameters = {"pack": missing, "machines": row.get("target_machines")}
    else:
        row = (observation.get("pack_growth") or {}).get(pack)
        parameters = {"pack": pack, "machines": (row or {}).get("target_machines")}
    if not isinstance(row, Mapping):
        return [_provider_option("science", feasible=False, reason=f"no growth receipt for {pack or 'the labs'}")]
    saved = (row.get("decision") or {}).get("seconds_saved") if isinstance(row.get("decision"), Mapping) else None
    gain, each = None, row.get("pack_rate_per_machine")
    if isinstance(each, (int, float)) and isinstance(row.get("target_machines"), int):
        gain = round(each * (row["target_machines"] - int(row.get("machines") or 0)), 3)
    options = []
    if row.get("grow"):
        from .progression import science_growth_nodes

        nodes = science_growth_nodes(
            state.get("jobs") or [], state.get("queue_nodes") or [], observation, state.get("capabilities") or {}
        )
        nodes = [n for n in nodes if n.get("parameters") == parameters]
        options.append(
            _provider_option(
                "science",
                capability="science_growth",
                parameters=parameters,
                nodes=nodes,
                gain_per_minute=gain,
                cost=(row.get("unit") or {}).get("raw_cost"),
                seconds_saved=saved,
                reason=str(row.get("reason") or ""),
            )
        )
    else:
        options.append(_provider_option("science", feasible=False, reason=str(row.get("reason") or "science waits")))
    step = row.get("next_step") if isinstance(row.get("next_step"), Mapping) else None
    if step:
        for o in step.get("options") or ():
            if isinstance(o, Mapping):
                options.append(
                    _provider_option(
                        "science",
                        kind="new_unit" if str(o.get("option", "")).startswith("second") else "rebuild",
                        feasible=False,
                        cost=o.get("raw_cost"),
                        reason=f"{o.get('option')}: {o.get('seconds')}s vs wait {step.get('wait_seconds')}s; "
                        "not a capability yet",
                    )
                )
    return options


def missing_pack(activity, status, targets):
    """The non-red pack of the running research that a lab missing packs holds none of, or None."""
    running = next(iter(targets or ()), None)
    packs = sorted(p for p in getattr(running, "packs_per_unit", {}) if p != RED_PACK)
    stock = activity.get("lab_stock") or []
    for pack in packs:
        if any(s == PACK_SHORT and not (stock[i] if i < len(stock) else {}).get(pack) for i, s in enumerate(status)):
            return pack
    return None


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
    district = path.with_name("smelting-district.json")
    district = json.loads(district.read_text(encoding="utf-8")) if district.exists() else {}
    return growth_decision(
        catalog,
        state,
        observation,
        targets,
        _evidence_from_supply(observation),
        expected=recommended,
        plate_spare=district_spare(district, observation),
    )


def district_spare(district, observation):
    """Plate -> unclaimed district capacity per minute, only for banks whose built furnaces hold full output.

    ``smelting_throughput_bounds``: partial upper bound (ore, furnaces, belt) minus the
    declared bus demand of that plate. A bank without a full-output furnace gives nothing
    (its measured net is the evidence).
    """
    if not isinstance(district, Mapping) or district.get("world_id") != observation.get("world_id"):
        return {}
    statuses = {
        _key(e): e.get("status_name")
        for e in observation.get("entities") or ()
        if isinstance(e, Mapping) and isinstance(e.get("position"), Mapping) and "name" in e
    }
    bounds = {
        r.get("product"): r
        for r in (observation.get("smelting_throughput_bounds") or {}).get("banks", [])
        if isinstance(r, Mapping)
    }
    spare = {}
    for bank in district.get("banks") or ():
        bound = bounds.get(bank.get("product"))
        furnaces = [e for e in bank.get("entities", []) if "furnace" in str(e.get("name"))]
        if not bound or not any(statuses.get(_key(e)) == "full_output" for e in furnaces):
            continue
        upper = finite_number(bound.get("partial_upper_bound_per_minute")) or 0.0
        claimed = finite_number(bound.get("claimed_bus_demand_per_minute")) or 0.0
        spare[bank["product"]] = max(0.0, upper - claimed)
    return spare


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


def growth_decision(catalog, state, observation, targets, evidence, *, expected=None, plate_spare=None):
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
    # Size only what can be researched: a technology whose pack no block makes is reported
    # (the roadmap proposes that producer) and never turns the queue infinite.
    produced = {RED_PACK} | {
        e.get("recipe") for e in observation.get("entities") or () if isinstance(e, Mapping) and e.get("recipe")
    }
    targets, blocked = split_producible(targets or (), produced)
    if blocked:
        row["blocked_by_missing_pack"] = blocked
    if not isinstance(research, str) or not research or not targets:
        return dict(row, reason="no research in progress" if not blocked else "no researchable technology queued")
    # Bottleneck chain plates -> pack machines -> labs. A lab idle for packs is a pack
    # shortage, not a lack of demand: the staged guard only holds units idle for demand.
    status = activity["lab_status"]
    idle_for_demand = [s for s in status if s not in ("working", PACK_SHORT)]
    backlog = (
        activity["belt_packs"] >= BELT_BACKUP_PACKS
        or all(n >= LAB_BACKUP_PACKS for n in activity["lab_packs"])
        or "full_output" in activity["machine_status"]
    )
    # A red shortage: a lab missing packs holds no red (one holding red misses another pack: missing_pack).
    starving = any(s == PACK_SHORT and n == 0 for s, n in zip(status, activity["lab_packs"])) and not backlog
    short = any(s in MACHINE_INGREDIENT_SHORT for s in activity["machine_status"])
    machines_busy = all(s == "working" for s in activity["machine_status"])
    labs_busy = all(s == "working" for s in status)
    costs = expansion_costs(catalog, RED_PACK, current_assemblers=machines)
    row["pack_rate_per_machine"] = costs["pack_rate_per_assembler"]
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
    # Machine limit, live: the frame's slots and what the plate supply carries now.
    per_machine = {item: amount * costs["pack_rate_per_assembler"] for item, amount in costs["pack"].items()}
    frame = frame_capacity(state)
    plates = plate_machine_limit(
        machines, per_machine, evidence, research_queue_seconds(targets, hardware), spare=plate_spare
    )
    limit = min(frame, plates)
    row["frame"] = {
        "machine_slots": stage["machine_slots"],
        "frame_slots": frame,
        "plate_limit": plates,
        "plate_spare_per_minute": {k: round(v, 2) for k, v in (plate_spare or {}).items()},
        "machine_limit": limit,
        "lab_slots": stage["lab_slots"],
    }
    free_labs = stage["lab_slots"] - labs
    free_machines = max(0, limit - machines)
    # Labs only while every lab works: with packs piling up, or when research_capacity finds labs the limit.
    may_labs = labs_busy and free_labs > 0
    wants_machines = starving and not short and machines_busy
    may_machines = wants_machines and free_machines > 0
    row["chain"] = "demand" if idle_for_demand else "plates" if starving and short else "packs" if starving else "labs"
    missing = missing_pack(activity, status, targets)
    if missing is not None and backlog:
        # Red backs up and a lab misses another pack of the running research: that pack's block is the
        # limit (pack_growth), not the lab feed (eval-1575138435: 8 labs, red 24/min backing up, green 5/min).
        row["chain"] = f"packs:{missing}"
        row["missing_pack"] = missing
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
        elif wants_machines and machines >= frame:
            beyond = beyond_frame(catalog, targets, hardware, materials, observation, machines)
            row["next_step"] = beyond
            why = "labs starve for packs and every slot of the frame is built: " + beyond["reason"]
        elif wants_machines:
            why = f"labs starve for packs and the plate supply carries no further machine (limit {plates})"
        elif not labs_busy and row.get("missing_pack"):
            why = (
                f"labs miss {row['missing_pack']} while red packs back up: the {row['missing_pack']} block is the "
                "limit (pack_growth receipt), not the red stage or the lab feed"
            )
        elif not labs_busy:
            why = "a lab misses packs while packs back up: the lab feed, not a stage (bandwidth receipt lab_feed)"
        else:
            why = "every lab slot is built"
        return dict(row, reason=why[:300])

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
    if row["target_machines"] > stage["machine_slots"]:
        row["frame"]["extend_to"] = frame  # science.grow republishes the wider frame before it builds
    # Declared with the decision: throughput_growth adds what exceeds the taps (undeclared_plate_draw).
    row["plate_draw_per_minute"] = plate_draw(state, row["target_machines"])
    return row


def _second_block_bill(slots):
    """Entities of a second science row of ``slots`` machines (the row template; taps and routes are not known)."""
    from collections import Counter

    from .science import science_row

    return Counter(e["name"] for e in science_row(0.5, 0.5, slots)["entities"])


def _json_value(value):
    if isinstance(value, float):
        return round(value, 1) if math.isfinite(value) else None
    return value


def beyond_frame(catalog, targets, hardware, materials, observation, machines, config=ResearchCapacityConfig()):
    """What follows a full frame: a second science block or a partial rebuild, by the planner's wait-vs-spend rule.

    Options (each ``fund + build + research`` by ``research_capacity.hardware_option``):

    - ``second_block_<k>``: k = 1..max_step more machines in a new row; cost
      and build time of the row template (machines ``build_seconds_per_machine``,
      other entities ``ENTITY_BUILD_SECONDS``); its taps and routes are not
      priced (unknown until sited), so this is a lower bound.
    - ``rebuild``: every machine replaced in place by ``FAST_MACHINE`` once
      its recipe is enabled; cost = the new machines (the old ones return to
      the inventory, not credited), ``REBUILD_PLACEMENTS`` placements each;
      research is not credited during the swap (the row's output is lost).

    The fastest option is chosen when it saves ``min_saving_seconds`` and is
    at most ``max_ratio`` of the wait (any finite option for an infinite
    wait), else ``wait``. Neither build is a capability yet: ``implemented``
    is False and the receipt names the next step.
    """
    from .progression import raw_cost

    wait = research_queue_seconds(targets, hardware)
    options = []
    for k in range(1, config.max_step + 1):
        bill = _second_block_bill(k)
        new = ResearchHardware(
            {RED_PACK: machines + k},
            hardware.pack_rate_per_assembler,
            hardware.labs,
            hardware.lab_speed,
            hardware.pack_stock,
        )
        made = sum(n for name, n in bill.items() if name.startswith("assembling-machine"))
        build = made * config.build_seconds_per_machine + (sum(bill.values()) - made) * ENTITY_BUILD_SECONDS
        option = hardware_option(
            targets, hardware, new, materials, cost=raw_cost(catalog, bill), build_seconds=build, pack=RED_PACK
        )
        options.append(dict(option, option=f"second_block_{k}", machines=machines + k))
    entities = getattr(catalog, "entities", None) or {}
    enabled = (observation.get("enabled_recipes") or {}).get(FAST_MACHINE)
    slow = finite_number((entities.get("assembling-machine-1") or {}).get("crafting_speed"))
    fast = finite_number((entities.get(FAST_MACHINE) or {}).get("crafting_speed"))
    if enabled and slow and fast and fast > slow:
        rate = hardware.pack_rate_per_assembler.get(RED_PACK, 0.0)
        new = ResearchHardware(
            {RED_PACK: machines}, {RED_PACK: rate * fast / slow}, hardware.labs, hardware.lab_speed, hardware.pack_stock
        )
        option = hardware_option(
            targets,
            hardware,
            new,
            materials,
            cost=raw_cost(catalog, {FAST_MACHINE: machines}),
            build_seconds=machines * REBUILD_PLACEMENTS * config.build_seconds_per_machine,
            pack=RED_PACK,
        )
        options.append(dict(option, option="rebuild", machines=machines, machine=FAST_MACHINE))
    feasible = [o for o in options if math.isfinite(o["seconds"])]
    best = min(feasible, key=lambda o: o["seconds"]) if feasible else None
    choice = "wait"
    if best is not None:
        if not math.isfinite(wait):
            choice = best["option"]
        elif (
            wait >= config.min_remaining_seconds
            and wait - best["seconds"] >= config.min_saving_seconds
            and best["seconds"] <= wait * config.max_ratio
        ):
            choice = best["option"]

    def fmt(seconds):
        return "inf" if not math.isfinite(seconds) else f"{seconds:.0f}s"

    if choice == "wait":
        reason = f"wait {fmt(wait)}" + (f"; best {best['option']} takes {fmt(best['seconds'])}" if best else "")
        reason += ", no option saves enough"
    else:
        reason = (
            f"next step {choice}: {fmt(best['seconds'])} (fund {fmt(best['fund_seconds'])} + build "
            f"{fmt(best['build_seconds'])} + research {fmt(best['research_seconds'])}) vs wait {fmt(wait)}; "
            "not a capability yet"
        )
    return {
        "choice": choice,
        "wait_seconds": _json_value(wait),
        "options": [{k: _json_value(v) for k, v in o.items()} for o in options],
        "implemented": False,
        "reason": reason[:240],
    }
