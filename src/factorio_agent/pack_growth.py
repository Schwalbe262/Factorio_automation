"""Growth of every staged science pack block beside the red row (pack-agnostic; red: ``science_growth``).

The red block (``science_growth``) owns the labs; every other pack block (green today,
chemical once its block has slots) follows the same chain for its own pack:

    plates / intermediates -> pack machines (with their sub-assemblers, one unit) -> labs

``block_view`` reads a saved pack block (``PACK_STATES``) into one shape: built pack
machines, the reserved in-place slots (``growth_slots`` of a slot-row frame), the
sub-assemblers' capacity (a split row's sub-clusters are sized for its frame), the declared
tap draw and the real growth unit (``unit_bill``: one reserved slot = pack machine +
inserters + pole; without a slot the next unit is a whole second block).

``growth_decision`` (observe receipt ``pack_growth[pack]``, proposed by
``progression.science_growth_nodes`` as ``science_growth {pack, machines}``):

* labs missing this pack (``missing_science_packs`` with none of it in the lab) and no
  backlog of it (lab belt / every lab / a pack machine at full output) -> the pack block is
  the limit; a pack machine short of ingredients -> plates or intermediates are the limit;
* wanted = machines the built labs consume over the queue (``lab_pack_rate``), at most the
  frame and the sub-assemblers; the plate draw of that target is declared at once
  (``plate_draw_per_minute``, ``undeclared_plate_draw``: throughput_growth growth_taps), so the
  plate supply grows before the machines it feeds;
* how many now: ``research_capacity.plan_research_capacity`` on the queue with this pack's
  machines, the slot unit's real raw cost and build time, plates capped by the evidence plus
  the district's unclaimed capacity (``science_growth.district_spare``); ``build_ahead`` may
  take a larger option towards ``recommended_rates[pack]``;
* no free slot (a cluster, or a full frame) -> ``next_step`` (``beyond``): a second unit or a
  partial rebuild cluster -> split row by the same wait-vs-spend rule (replacement first: the
  old block runs until the new one does; its machines are reused). Not a capability yet.

Pure functions except ``growth_receipt`` / ``unit_bills`` (read the saved states).
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from .capacity_planning import finite_number
from .research_capacity import (
    GREEN_PACK,
    RED_PACK,
    ResearchCapacityConfig,
    ResearchHardware,
    ResearchMaterials,
    expansion_costs,
    hardware_from_observation,
    hardware_option,
    lab_pack_rate,
    pack_rate_per_assembler,
    plan_research_capacity,
    research_queue_seconds,
    split_producible,
    with_spare,
)
from .science_growth import (
    BELT_BACKUP_PACKS,
    ENTITY_BUILD_SECONDS,
    LAB_BACKUP_PACKS,
    MACHINE_INGREDIENT_SHORT,
    PACK_SHORT,
    _key,
    build_ahead,
    built_labs,
    plate_machine_limit,
)

# Pack -> saved block state of its (non-red) production block. Chemical science has a fixed
# 2-4 machine core without reserved slots (``chemical_science.py``): no entry until it has one.
PACK_STATES = {GREEN_PACK: "logistic-science.json"}
PLATES = ("iron-plate", "copper-plate")
# Second units / rebuild sizes the beyond-frame comparison tries (machines of the new unit).
BEYOND_STEP = 2


def _plan_names(entities) -> Counter:
    return Counter(e["name"] for e in entities if isinstance(e, Mapping) and e.get("name"))


def block_view(state, pack, catalog=None) -> dict | None:
    """One shape for a saved pack block (module doc), or None when ``state`` holds no block of ``pack``."""
    if not isinstance(state, Mapping) or not isinstance(state.get("plan"), Mapping):
        return None
    core = state.get("core") if isinstance(state.get("core"), Mapping) else {}
    entities = [e for e in state["plan"].get("entities") or () if isinstance(e, Mapping)]
    machines = [e for e in entities if e.get("recipe") == pack]
    if not machines:
        return None
    form = core.get("form") or ("cluster" if core.get("nodes") else "fixed")
    slots = [list(g) for g in core.get("growth_slots") or () if isinstance(g, list) and g]
    subs = core.get("sub_clusters") if isinstance(core.get("sub_clusters"), Mapping) else {}
    sub_rate = min(
        (finite_number(s.get("rated_per_minute")) or 0.0 for s in subs.values() if isinstance(s, Mapping)),
        default=None,
    )
    declared = {}
    for tap in state.get("taps") or ():
        if isinstance(tap, Mapping) and isinstance(tap.get("item"), str):
            declared[tap["item"]] = declared.get(tap["item"], 0.0) + float(tap.get("demand_per_minute") or 0.0)
    return {
        "pack": pack,
        "form": form,
        "phase": state.get("phase"),
        "world_id": state.get("world_id"),
        "machines": len(machines),
        "machine_entities": machines,
        "slots": slots,
        "frame": len(machines) + len(slots),
        "sub_rate_per_minute": sub_rate,
        "declared_per_minute": {k: round(v, 3) for k, v in declared.items()},
        "slot_bill": dict(_plan_names(slots[0])) if slots else None,
        "block_bill": dict(_plan_names(core.get("entities") or entities)),
    }


def second_unit_bill(catalog, view, machines=1) -> dict:
    """Entities of the next whole unit when no reserved slot is left: a second block of the same form.

    A cluster is one pack machine with its intermediates (``green_science.green_cluster``); a
    split row starts ``machines`` pack machines with sub-clusters for its frame
    (``green_layout.split_block``). Taps, routes and the lab entry are map-dependent (unpriced).
    """
    if view["pack"] == GREEN_PACK and catalog is not None:
        if view["form"] == "split":
            from .green_layout import split_block

            return dict(_plan_names(split_block(catalog, built=max(1, machines))["entities"]))
        from .green_science import green_cluster

        return {k: n * machines for k, n in _plan_names(green_cluster(catalog, (0.5, 0.5))["entities"]).items()}
    return {k: n * machines for k, n in view["block_bill"].items()}


def unit_bill(catalog, view) -> dict:
    """The real unit of one more pack machine: a reserved slot, else a whole second unit."""
    return dict(view["slot_bill"]) if view.get("slot_bill") else second_unit_bill(catalog, view)


def _sub_cap(view, each) -> int:
    """Pack machines the sub-assemblers carry (a split row's sub-clusters), else the frame."""
    rate = view.get("sub_rate_per_minute")
    if rate is None or not each:
        return view["frame"]
    return max(view["machines"], int(math.floor(rate / each + 1e-9)))


def _per_machine_plates(catalog, pack, each) -> dict:
    from .progression import raw_cost

    return {item: round(rate, 4) for item, rate in raw_cost(catalog, {pack: each}).items() if item in PLATES}


def lab_evidence(science_state, observation, pack) -> dict:
    """Built labs of the red block and ``pack``: statuses, per-lab stock, belt stock, labs missing it."""
    actual = {
        _key(e): e
        for e in observation.get("entities") or ()
        if isinstance(e, Mapping) and "position" in e and "name" in e
    }
    labs = [actual.get(_key(e)) for e in built_labs(science_state)] if science_state else []
    belts = [
        actual.get(_key(e))
        for e in ((science_state or {}).get("labs") or {}).get("entities") or ()
        if e.get("name") == "transport-belt"
    ]
    stock = [int((((e or {}).get("inventory") or {}).get(pack, 0)) or 0) for e in labs]
    status = [(e or {}).get("status_name") for e in labs]
    return {
        "labs": len(labs),
        "lab_status": status,
        "lab_packs": stock,
        "belt_packs": sum(int(((e or {}).get("belt_inventory") or {}).get(pack, 0) or 0) for e in belts),
        "labs_missing": sum(1 for s, n in zip(status, stock) if s == PACK_SHORT and n == 0),
    }


def _fmt(seconds) -> str:
    return "inf" if seconds is None or not math.isfinite(seconds) else f"{seconds:.0f}s"


def _json(value):
    if isinstance(value, float):
        return round(value, 1) if math.isfinite(value) else None
    return value


def beyond(catalog, view, targets, hardware, materials, config=ResearchCapacityConfig()) -> dict:
    """No free slot: a second unit (k = 1..BEYOND_STEP pack machines) or the rebuild cluster -> split row.

    Same ``fund + build + research`` model as ``science_growth.beyond_frame``. The rebuild is built
    beside the running cluster (replacement first, no lost output), then the cluster is mined
    (``ENTITY_BUILD_SECONDS`` per entity, machines ``build_seconds_per_machine``); the cluster's
    entities return to the inventory and are reused (subtracted from the split's bill).
    """
    from .progression import raw_cost

    pack, machines = view["pack"], view["machines"]
    wait = research_queue_seconds(targets, hardware)

    def seconds_of(bill):
        made = sum(n for name, n in bill.items() if name.startswith("assembling-machine"))
        return made * config.build_seconds_per_machine + (sum(bill.values()) - made) * ENTITY_BUILD_SECONDS

    def moved(count):
        return ResearchHardware(
            dict(hardware.assemblers, **{pack: count}),
            hardware.pack_rate_per_assembler,
            hardware.labs,
            hardware.lab_speed,
            hardware.pack_stock,
        )

    options = []
    for k in range(1, BEYOND_STEP + 1):
        bill = second_unit_bill(catalog, view, k)
        row = hardware_option(
            targets,
            hardware,
            moved(machines + k),
            materials,
            cost=raw_cost(catalog, bill),
            build_seconds=seconds_of(bill),
            pack=pack,
        )
        options.append(dict(row, option=f"second_unit_{k}", machines=machines + k, bill=bill))
    if view["form"] == "cluster" and pack == GREEN_PACK:
        from .green_layout import FRAME

        old = view["block_bill"]
        for k in range(2, min(FRAME, max(2, machines + BEYOND_STEP)) + 1):
            split = dict(second_unit_bill(catalog, dict(view, form="split"), k))
            reused = {name: max(0, n - old.get(name, 0)) for name, n in split.items()}
            build = seconds_of(split) + sum(old.values()) * ENTITY_BUILD_SECONDS
            row = hardware_option(
                targets, hardware, moved(k), materials, cost=raw_cost(catalog, reused), build_seconds=build, pack=pack
            )
            options.append(dict(row, option=f"rebuild_split_{k}", machines=k, bill=split, removes=dict(old)))
    feasible = [o for o in options if math.isfinite(o["seconds"])]
    best = min(feasible, key=lambda o: o["seconds"]) if feasible else None
    choice = "wait"
    if best is not None and (
        not math.isfinite(wait)
        or (
            wait >= config.min_remaining_seconds
            and wait - best["seconds"] >= config.min_saving_seconds
            and best["seconds"] <= wait * config.max_ratio
        )
    ):
        choice = best["option"]
    if choice == "wait":
        reason = f"wait {_fmt(wait)}" + (f"; best {best['option']} takes {_fmt(best['seconds'])}" if best else "")
        reason += ", no option saves enough"
    else:
        reason = (
            f"next step {choice}: {_fmt(best['seconds'])} (fund {_fmt(best['fund_seconds'])} + build "
            f"{_fmt(best['build_seconds'])} + research {_fmt(best['research_seconds'])}) vs wait {_fmt(wait)}; "
            "not a capability yet (a second lab entry, or the cluster's removal after the split runs)"
        )
    return {
        "choice": choice,
        "wait_seconds": _json(wait),
        "options": [{k: _json(v) for k, v in o.items() if k != "bill"} for o in options],
        "implemented": False,
        "reason": reason[:300],
    }


def growth_decision(
    catalog, pack, state, science_state, observation, targets, evidence, *, expected=None, plate_spare=None
) -> dict:
    """Pack machines to add into reserved slots of ``pack``'s block now (module rule), JSON-safe.

    ``grow`` is True only when ``target_machines`` exceeds the built machines; ``reason`` names
    the rule that held growth back; ``plate_draw_per_minute`` is declared with the decision.
    """
    view = block_view(state, pack, catalog)
    if view is None:
        return {"pack": pack, "grow": False, "reason": f"no saved {pack} block"}
    if view["phase"] != "verified":
        return {"pack": pack, "grow": False, "reason": f"{pack} block not verified yet"}
    each = pack_rate_per_assembler(catalog, pack)
    per_machine = _per_machine_plates(catalog, pack, each)
    machines = view["machines"]
    row = {
        "pack": pack,
        "form": view["form"],
        "grow": False,
        "machines": machines,
        "target_machines": machines,
        "wanted_machines": machines,
        "per_machine_plates": per_machine,
        "pack_rate_per_machine": round(each, 4),
        "declared_per_minute": view["declared_per_minute"],
    }

    def finish(out, declare=None):
        out = dict(out)
        count = max(machines, out["target_machines"], declare or 0)
        draw = {item: round(rate * count, 3) for item, rate in per_machine.items()}
        out["plate_draw_per_minute"] = draw
        out["undeclared_plate_draw_per_minute"] = {
            item: round(max(0.0, rate - view["declared_per_minute"].get(item, 0.0)), 3) for item, rate in draw.items()
        }
        out["reason"] = str(out.get("reason", ""))[:300]
        return out

    actual = {_key(e): e for e in observation.get("entities") or () if isinstance(e, Mapping) and "position" in e}
    status = [(actual.get(_key(e)) or {}).get("status_name") for e in view["machine_entities"]]
    labs = lab_evidence(science_state, observation, pack)
    row["evidence"] = dict(labs, machine_status=status)
    if any(s is None for s in status) or not labs["labs"]:
        return finish(dict(row, reason=f"{pack} machines or labs not observed"))
    produced = {RED_PACK} | {
        e.get("recipe") for e in observation.get("entities") or () if isinstance(e, Mapping) and e.get("recipe")
    }
    targets, blocked = split_producible(targets or (), produced)
    if blocked:
        row["blocked_by_missing_pack"] = blocked
    using = [t for t in targets if t.packs_per_unit.get(pack)]
    research = observation.get("research")
    if not isinstance(research, str) or not research or not using:
        return finish(dict(row, reason=f"no research in progress or queued that uses {pack}"))
    backlog = (
        labs["belt_packs"] >= BELT_BACKUP_PACKS
        or all(n >= LAB_BACKUP_PACKS for n in labs["lab_packs"])
        or "full_output" in status
    )
    starving = labs["labs_missing"] > 0 and not backlog
    short = any(s in MACHINE_INGREDIENT_SHORT for s in status)
    busy = all(s == "working" for s in status)
    consume = lab_pack_rate(using, labs["labs"], pack) or 0.0
    room = min(view["frame"], _sub_cap(view, each))
    wanted = max(machines, min(room, math.ceil(consume / each - 1e-9)))
    row["wanted_machines"] = wanted
    row["lab_demand_per_minute"] = round(consume, 2)
    row["chain"] = "labs" if not starving else "plates" if short else "packs"
    hardware = hardware_from_observation(
        catalog, observation, sorted({p for t in targets for p in t.packs_per_unit} | {pack})
    )
    bill = unit_bill(catalog, view)
    costs = expansion_costs(catalog, pack, current_assemblers=machines, unit_bill=bill)
    row["unit"] = {
        "bill": costs["unit_bill"],
        "machines": costs["machines_per_assembler"],
        "raw_cost": costs["assembler"],
    }
    evidence = with_spare(evidence or {}, plate_spare)
    held = observation.get("inventory") if isinstance(observation.get("inventory"), Mapping) else {}
    for item in costs["assembler"]:
        if item not in evidence:  # a raw item without supply evidence (a pole's wood): what the actor holds
            evidence[item] = {"available": float(finite_number(held.get(item)) or 0.0), "produced_per_minute": 0.0}
    hardware = ResearchHardware(
        dict(hardware.assemblers, **{pack: machines}),
        dict(hardware.pack_rate_per_assembler, **{pack: each}),
        hardware.labs,
        hardware.lab_speed,
        dict(hardware.pack_stock, **{pack: float(sum(labs["lab_packs"]) + labs["belt_packs"])}),
    )
    from .progression import raw_cost

    pack_costs = {p: raw_cost(catalog, {p: 1}) for p in hardware.assemblers}
    materials = ResearchMaterials(
        evidence=evidence,
        assembler_cost={pack: costs["assembler"]},
        lab_cost=costs["lab"],
        pack_cost=pack_costs,
        machines_per_assembler={pack: costs["machines_per_assembler"]},
    )
    plates = plate_machine_limit(machines, per_machine, evidence, research_queue_seconds(using, hardware))
    free = max(0, min(room, plates) - machines)
    row["frame"] = {
        "slots": len(view["slots"]),
        "frame": view["frame"],
        "sub_assembler_limit": _sub_cap(view, each),
        "plate_limit": plates,
        "machine_limit": min(room, plates),
        "plate_spare_per_minute": {k: round(v, 2) for k, v in (plate_spare or {}).items()},
    }
    if not starving:
        why = (
            f"{pack} backs up (lab belt {labs['belt_packs']}, labs {labs['lab_packs']}): not the limit"
            if backlog
            else f"no lab misses {pack}"
        )
        return finish(dict(row, reason=why))
    # From here the labs starve for this pack: its labs' draw is real demand, declared now.
    if short:
        return finish(
            dict(
                row,
                reason=f"labs starve for {pack} and its machines lack ingredients: plates or intermediates are the "
                f"limit (declared {wanted} machines' plates to throughput_growth)",
            ),
            declare=wanted,
        )
    if not busy:
        return finish(dict(row, reason=f"labs starve for {pack} but a {pack} machine is not working ({status})"))
    if room <= machines:
        row["next_step"] = beyond(catalog, view, using, hardware, materials)
        return finish(
            dict(
                row,
                reason=f"labs starve for {pack}; no reserved slot left ({view['form']}): {row['next_step']['reason']}",
            )
        )
    if free <= 0:
        return finish(
            dict(
                row,
                reason=f"labs starve for {pack} and the plate supply carries no further machine (limit {plates}); "
                f"declared {wanted} machines' plates to throughput_growth",
            ),
            declare=wanted,
        )
    config = ResearchCapacityConfig(max_step=free, max_assemblers=machines + free, max_labs=hardware.labs)
    decision = plan_research_capacity(using, hardware, materials, config=config)
    row["decision"] = {
        k: v for k, v in decision.as_dict().items() if k in ("pack", "wait_seconds", "chosen_seconds", "seconds_saved")
    }
    target = decision.target_assemblers if decision.pack == pack else machines
    reason = decision.reason
    ahead = build_ahead(decision, expected, machines=machines, labs=hardware.labs) if decision.pack == pack else None
    if ahead is not None:
        extra, _labs, row["build_ahead"] = ahead
        if machines + extra != target:
            target = machines + extra
            b = row["build_ahead"]
            reason = (
                f"build ahead for {b['expected_rate_per_minute']}/min: {b['chosen']} {b['chosen_seconds']}s + revisit "
                f"{b['chosen_revisit_seconds']}s < {b['quickest']} {b['quickest_seconds']}s; " + reason
            )
    row.update(target_machines=max(machines, min(target, machines + free)), reason=reason)
    row["grow"] = row["target_machines"] > machines
    return finish(row, declare=wanted)


def _read(root, name, world_id):
    path = Path(root) / name
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) and value.get("world_id") == world_id else {}


def unit_bills(root, catalog, world_id) -> dict:
    """Pack -> the real growth unit of its built block (``research_capacity_receipt`` ``units``)."""
    out = {}
    for pack, name in PACK_STATES.items():
        view = block_view(_read(root, name, world_id), pack, catalog)
        if view is not None:
            out[pack] = unit_bill(catalog, view)
    return out


def receipt_inputs(root, catalog, observation) -> tuple[dict, dict]:
    """``(units, plate_spare)`` for ``research_capacity_receipt``: real growth units, unclaimed district plates."""
    from .science_growth import district_spare

    world = observation.get("world_id")
    try:
        units = unit_bills(root, catalog, world)
    except (KeyError, TypeError, ValueError, AttributeError):
        units = {}
    return units, district_spare(_read(root, "smelting-district.json", world), observation)


def growth_receipt(root, catalog, jobs, observation) -> dict | None:
    """Observe receipt ``pack_growth``: ``{pack: growth_decision}`` for every saved pack block of this world."""
    from .research_capacity import _evidence_from_supply, queue_targets
    from .science_growth import district_spare

    world = observation.get("world_id")
    science = _read(root, "automation-science.json", world)
    rows = {}
    targets = None
    for pack, name in PACK_STATES.items():
        state = _read(root, name, world)
        if not state:
            continue
        if targets is None:
            targets = queue_targets(catalog, jobs, observation)
        recommended = ((observation.get("research_capacity") or {}).get("recommended_rates") or {}).get(pack)
        rows[pack] = growth_decision(
            catalog,
            pack,
            state,
            science,
            observation,
            targets,
            _evidence_from_supply(observation),
            expected=recommended,
            plate_spare=district_spare(_read(root, "smelting-district.json", world), observation),
        )
    return rows or None


def undeclared_plate_draw(receipt) -> dict:
    """Plate -> plates/minute every pack block draws (or is about to) beyond its declared taps (growth_taps)."""
    out: dict = {}
    for row in (receipt or {}).values() if isinstance(receipt, Mapping) else ():
        for item, rate in ((row or {}).get("undeclared_plate_draw_per_minute") or {}).items():
            value = finite_number(rate)
            if value and value > 0:
                out[item] = round(out.get(item, 0.0) + value, 3)
    return out
