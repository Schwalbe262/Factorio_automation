"""Power growth: steam pairs sized from measured demand vs capacity plus known upcoming loads.

The first steam plant is the ``power`` step; ``steam_expansion`` (roadmap step, stage 1) adds the
first boiler/engine pair. Nothing grew power after that, while every lab, assembler, drill and
furnace block adds load and the rocket silo alone draws about 4 MW (eval-1575138435, 257 min:
one pair, 1.8 MW, labs/assemblers/drills ``low_power``). Rules:

1. Receipt (:func:`power_receipt`, observe ``power_growth``): capacity = generating steam engines x
   their nameplate; demand = the nameplate draw of every electric consumer that is working or
   short of power (inserters at ``INSERTER_DUTY``); brownout = the share of electric consumers
   ``low_power`` / ``no_power``; upcoming = known loads not built yet (``UPCOMING``: a queued or
   open rocket silo, a job waiting on power headroom). Needed = demand (at least capacity + one
   pair while browned out) + upcoming, x ``MARGIN``.
2. Step: ``pairs`` = ceil((needed - capacity) / pair nameplate), at least one; one
   ``steam_expansion`` stage at a time (``{"stage": k}``, stage 1 is the roadmap's ``{}``), each
   stage one more pair (``steam_expansion`` plans it beside the plant or on a free site with its
   coal from the coal patch trunk).
3. Providers: the bottleneck auditor's ``no_power`` rows (``growth_providers.power_options``) and
   the roadmap tail (``power_growth_nodes`` in the autopilot) read the same receipt, so a
   ``*_power_headroom_*`` wait leads to a proposed stage, not an idle wait.
"""

from __future__ import annotations

import math
from typing import Mapping

CAPABILITY = "steam_expansion"
MARGIN = 1.15  # headroom over the estimated need
INSERTER_DUTY = 0.25  # inserters draw their nameplate only while swinging
STAGE_MAX = 12  # steam_expansion stages after the roadmap's first (hard cap of the rule)
NODE_PRIORITY = -51  # just before critical plate growth: power starves every electric machine
SHORT_STATES = frozenset({"low_power", "no_power"})
ACTIVE_STATES = frozenset({"working", "low_power"})
# Known loads that are not built yet, per capability of an open job or queue node (entity, count).
UPCOMING = {"rocket_silo": ("rocket-silo", 1)}
HEADROOM_MARK = "power_headroom"  # a job waiting with this in its reason declares a power shortfall
OPEN = frozenset({"pending", "ready", "running", "waiting", "blocked", "paused"})


def _kw(catalog, name) -> float:
    row = (getattr(catalog, "entities", None) or (catalog or {}).get("entities") or {}).get(name) or {}
    usage = row.get("energy_usage") or 0
    return float(usage) * 60 / 1000 if isinstance(usage, (int, float)) else 0.0


def pair_kw(catalog) -> float:
    """Nameplate of one boiler with two engines (900 kW each in vanilla)."""
    row = (getattr(catalog, "entities", None) or (catalog or {}).get("entities") or {}).get("steam-engine") or {}
    production = row.get("energy_production") or 0
    return 2 * float(production) * 60 / 1000 if isinstance(production, (int, float)) else 0.0


def _electric(e: Mapping) -> bool:
    return (
        e.get("name") not in ("steam-engine", "small-electric-pole", "medium-electric-pole", "big-electric-pole")
        and (e.get("electric_network_connected") or e.get("electric_network_id") is not None)
        and "pole" not in str(e.get("name"))
    )


def stage_of(parameters) -> int:
    """``steam_expansion`` stage of ``parameters``: 1 for ``{}``, else ``{"stage": k}`` with 2 <= k <= STAGE_MAX."""
    if not parameters:
        return 1
    stage = parameters.get("stage") if isinstance(parameters, Mapping) else None
    if set(parameters) != {"stage"} or type(stage) is not int or not 2 <= stage <= STAGE_MAX:
        raise ValueError("steam_expansion accepts {} or {'stage': 2..%d}" % STAGE_MAX)
    return stage


def _stages(jobs) -> tuple[int, bool]:
    """(highest succeeded stage, whether any stage is still open)."""
    done, open_ = 0, False
    for job in jobs or ():
        if job.get("capability") != CAPABILITY:
            continue
        try:
            stage = stage_of(job.get("parameters") or {})
        except ValueError:
            continue
        if job.get("status") == "succeeded":
            done = max(done, stage)
        elif job.get("status") in OPEN:
            open_ = True
    return done, open_


def power_receipt(observation: Mapping, catalog, jobs=(), queue_nodes=()) -> dict:
    """``power_growth`` receipt: capacity, demand, brownout, upcoming loads, needed pairs (pure)."""
    from .power_grid import generating

    entities = [e for e in observation.get("entities") or () if isinstance(e, Mapping)]
    engines = [e for e in entities if e.get("name") == "steam-engine"]
    unit = pair_kw(catalog) / 2
    capacity = round(unit * sum(1 for e in engines if generating(e)), 1)
    consumers = [e for e in entities if _electric(e)]
    short = sum(1 for e in consumers if e.get("status_name") in SHORT_STATES)
    demand = 0.0
    for e in consumers:
        if e.get("status_name") in ACTIVE_STATES:
            duty = INSERTER_DUTY if "inserter" in str(e.get("name")) else 1.0
            demand += duty * _kw(catalog, e.get("name"))
    brownout = round(short / len(consumers), 3) if consumers else 0.0
    upcoming, sources = 0.0, []
    rows = [j for j in jobs or () if j.get("status") in OPEN] + [
        n for n in queue_nodes or () if n.get("status", "pending") in OPEN
    ]
    for row in rows:
        known = UPCOMING.get(str(row.get("capability")))
        if known and row.get("capability") not in {s["capability"] for s in sources}:
            load = _kw(catalog, known[0]) * known[1]
            upcoming += load
            sources.append({"capability": row.get("capability"), "kw": round(load, 1)})
        elif HEADROOM_MARK in str(row.get("reason") or "") and row.get("capability") not in {
            s["capability"] for s in sources
        }:
            sources.append({"capability": row.get("capability"), "kw": None, "reason": str(row.get("reason"))[:120]})
    pair = pair_kw(catalog)
    need = max(demand, capacity + pair if brownout >= 0.1 else 0.0) + upcoming
    if any(s["kw"] is None for s in sources):
        need = max(need, capacity + pair)  # a declared headroom wait without a known load: one pair more
    needed = round(need * MARGIN, 1)
    pairs = max(0, math.ceil((needed - capacity) / pair - 1e-9)) if pair > 0 else 0
    done, open_ = _stages(jobs)
    return {
        "capacity_kw": capacity,
        "engines": len(engines),
        "demand_kw": round(demand, 1),
        "brownout_share": brownout,
        "upcoming_kw": round(upcoming, 1),
        "upcoming": sources,
        "needed_kw": needed,
        "pair_kw": round(pair, 1),
        "pairs_short": pairs,
        "grow": pairs > 0 and bool(engines),
        "stages_done": done,
        "stage_open": open_,
        "scope": "nameplate draw of active electric consumers (inserters at a duty share) and known upcoming loads",
    }


def power_growth_nodes(jobs, queue_nodes, observation, capabilities) -> list[dict]:
    """One ``steam_expansion`` stage node while the receipt is short of power and no stage is open."""
    if CAPABILITY not in capabilities:
        return []
    receipt = observation.get("power_growth")
    if not isinstance(receipt, Mapping) or not receipt.get("grow") or receipt.get("stage_open"):
        return []
    jobs = list(jobs)
    done, open_ = _stages(jobs)
    if open_ or done < 1:
        return []  # stage 1 is the roadmap's step (its requires and order); growth follows it
    succeeded = {j.get("capability") for j in jobs if j.get("status") == "succeeded"}
    if not set(capabilities[CAPABILITY].get("requires", [])) <= succeeded:
        return []
    stage = done + 1
    if stage > STAGE_MAX:
        return []
    parameters = {"stage": stage}
    if any(
        n.get("capability") == CAPABILITY and (n.get("parameters") or {}) == parameters
        for n in list(queue_nodes) + jobs
    ):
        return []
    return [
        {
            "id": f"auto_power_growth_{stage}",
            "capability": CAPABILITY,
            "parameters": parameters,
            "priority": NODE_PRIORITY,
            "depends_on": [],
            "reason": (
                f"power short {receipt.get('pairs_short')} pair(s): need {receipt.get('needed_kw')} kW "
                f"(demand {receipt.get('demand_kw')}, upcoming {receipt.get('upcoming_kw')}, brownout "
                f"{receipt.get('brownout_share')}) over {receipt.get('capacity_kw')} kW; steam stage {stage}"
            )[:300],
        }
    ]
