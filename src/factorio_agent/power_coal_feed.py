"""Starter power in two phases: a hand-fed boiler now, the dedicated coal belt later.

Why: the dedicated coal drill and its belt (about 100 hand-crafted belts on
live maps: 95 and 141) kept ``power`` running from minute 4 to 12 and held
back research, the startup science cell and the supply cell that machine-makes
belts. A player fuels the first boiler by hand and lays the belt once belts
come from assemblers.

Rules (docs/v2/PLANNERS.md "Starter power in two phases"):

1. ``power`` (``FactoryBuilder._ensure_hand_fed_power``) builds the plant,
   reserves the feed (``coal_plan`` saved, not built) and succeeds on native
   plant evidence with ``hand_fed: true``.
2. While ``coal_feed_mode`` is ``hand_fed``, ``boiler_fuel_action`` keeps the
   boiler stocked; it runs in ``StarterExecutors.starter_upkeep`` and
   ``idle_action`` before the burner cells' fuel (``maintain_fuel``).
3. ``feed_step_ready`` proposes the roadmap step ``power_coal_feed`` once the
   machine-made belts in stock cover the saved feed (receipt
   ``coal_feed_belts``: carried + the supply cell's belt chest >= the
   ``coal_plan`` belts; the cell's job need not have succeeded), or belts are
   machine-made (a succeeded supply cell stocking transport belts or a
   succeeded ``produce`` row for them), or ``FEED_FALLBACK_TICKS`` after power,
   or at once on an old map whose power job already built the feed.
4. ``FactoryBuilder.ensure_coal_feed`` builds and proves the feed; success sets
   ``power_mode: belt_fed`` and hand-fuelling stops (rule 2 no longer applies).
5. ``publish_planned_feed`` keeps the unbuilt feed on the board as block
   ``power_coal_feed`` so no other block is placed on its route; the block is
   removed once the feed is belt-fed (its hardware then stands on the map).
6. ``hand_fed_coal_per_minute`` is the boiler's coal burn for the capacity
   planner's coal rule while the boiler is hand-fed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy

from .core.deterministic_builder import (
    HAND_FED,
    boiler_coal_per_minute,
    coal_feed_mode,
    coal_fuel_value,
)
from .core.deterministic_state import _atomic_json

CAPABILITY = "power_coal_feed"
BLOCK = "power_coal_feed"
# Refill when the boiler holds less than this many seconds of full-load burn;
# full load is the worst case, so an unexpected load never outruns a trip.
BOILER_LEAD_SECONDS = 30.0
# One fill covers this many minutes of full-load burn, capped by the fuel slot.
BOILER_FILL_MINUTES = 2.0
BOILER_FUEL_SLOTS = 1  # base boiler: one fuel slot (one stack of coal)
FUEL_BATCH = 8  # as construction_supply.FUEL_BATCH: never a smaller trip unless the boiler is out
# Without machine-made belts, the feed is still built this long after power
# (15 game minutes), so hand-fuelling never lasts the whole game.
FEED_FALLBACK_TICKS = 54000
# Boiler load assumed for the coal rule when the receipt has no measured
# generation (``electricity.networks[].generation_kw``): a lab and a few
# assemblers draw about a quarter of one boiler early on.
UNMEASURED_LOAD_SHARE = 0.25
MACHINE_MADE_BELT = "transport-belt"


def boiler_sizing(catalog) -> dict:
    """Coal figures of the hand-fed boiler from catalog energy values.

    ``burn``: coal/min at full load; ``refill_below``: coal-equivalent stored
    in the boiler below which a trip starts (at least ``FUEL_BATCH``);
    ``target``: coal in the fuel slot after a fill.
    """
    burn = boiler_coal_per_minute(catalog)
    stack = (getattr(catalog, "items", None) or {}).get("coal", {}).get("stack_size")
    slot = int(stack) * BOILER_FUEL_SLOTS if isinstance(stack, int) and stack > 0 else 50 * BOILER_FUEL_SLOTS
    target = min(slot, math.ceil(burn * BOILER_FILL_MINUTES))
    refill = min(target, max(FUEL_BATCH, math.ceil(burn * BOILER_LEAD_SECONDS / 60)))
    return {"burn_per_minute": burn, "refill_below": refill, "target": target}


def _boiler_spec(state: Mapping) -> dict | None:
    return next((e for e in (state.get("power_plan") or {}).get("entities", []) if e.get("name") == "boiler"), None)


def _observed(obs, spec):
    p = spec["position"]
    return next(
        (
            e
            for e in obs.get("entities", [])
            if e.get("name") == spec["name"]
            and abs((e.get("position") or {}).get("x", math.inf) - p["x"]) < 0.2
            and abs((e.get("position") or {}).get("y", math.inf) - p["y"]) < 0.2
        ),
        None,
    )


def boiler_fuel_action(builder, bootstrap, catalog, obs) -> dict | None:
    """One batched coal action for the hand-fed boiler, or None.

    Only while the builder's ``coal_feed_mode`` is ``hand_fed`` and the
    plant's boiler stands. A trip starts when the boiler holds less than
    ``refill_below`` coal (fuel slot plus the burning remainder); it then
    fills the slot to ``target``. With at least the fill or ``FUEL_BATCH``
    carried the coal goes in; an empty boiler takes any carried coal;
    otherwise the coal is collected from the starter coal chests
    (``ensure_item``). A plain wait returns None, so the job keeps working.
    """
    state = getattr(builder, "state", None) or {}
    if state.get("world_id") != obs.get("world_id") or coal_feed_mode(state) != HAND_FED:
        return None
    spec = _boiler_spec(state)
    boiler = _observed(obs, spec) if spec else None
    if boiler is None:
        return None
    sizing = boiler_sizing(catalog)
    held = int((boiler.get("inventory") or {}).get("coal", 0))
    stored = held + float(boiler.get("remaining_burning_fuel", 0) or 0) / coal_fuel_value(catalog)
    if stored >= sizing["refill_below"]:
        return None
    fill = sizing["target"] - held
    if fill <= 0:
        return None
    carried = int((obs.get("inventory") or {}).get("coal", 0))
    if carried >= min(fill, FUEL_BATCH) or (stored <= 0 and carried > 0):
        move = builder._move(obs, spec["position"])
        if move:
            return move
        return {
            "type": "insert",
            "name": spec["name"],
            "position": deepcopy(spec["position"]),
            "item": "coal",
            "inventory": "fuel",
            "count": min(carried, fill),
            "reason": "hand-fed boiler: refill before the dedicated coal feed exists",
        }
    result = bootstrap.ensure_item(obs, "coal", fill)
    return result if isinstance(result, dict) and result.get("type") else None


def boiler_upkeep(ex, obs) -> dict | None:
    """``boiler_fuel_action`` for a ``StarterExecutors``; None when it lacks a builder or bootstrap."""
    builder, bootstrap = getattr(ex, "builder", None), getattr(ex, "bootstrap", None)
    if builder is None or bootstrap is None:
        return None
    return boiler_fuel_action(builder, bootstrap, getattr(ex, "catalog", None), obs)


# ----- planner rules (pure: jobs + observe receipt) ---------------------------------


def _power_job(jobs) -> Mapping | None:
    rows = [j for j in jobs if j.get("capability") == "power" and j.get("status") == "succeeded"]
    return rows[-1] if rows else None


def _feed_done(jobs) -> bool:
    return any(j.get("capability") == CAPABILITY and j.get("status") == "succeeded" for j in jobs)


def legacy_power(job: Mapping) -> bool:
    """A power job from before the split: its evidence measured the feed belt and is not hand-fed."""
    evidence = job.get("evidence") or {}
    return "coal_on_belts" in evidence and not evidence.get("hand_fed")


def belts_machine_made(jobs) -> bool:
    """A succeeded supply cell stocking transport belts, or a succeeded ``produce`` row for them."""
    for j in jobs:
        if j.get("status") != "succeeded":
            continue
        parameters = j.get("parameters") or {}
        if j.get("capability") == "supply_cell" and MACHINE_MADE_BELT in (parameters.get("products") or ()):
            return True
        if j.get("capability") == "produce" and parameters.get("item") == MACHINE_MADE_BELT:
            return True
    return False


def belts_stocked(observation) -> bool:
    """Receipt ``coal_feed_belts``: machine-made belts in stock (supply cell chest + carried) cover the feed."""
    row = (observation or {}).get(BELT_RECEIPT)
    if not isinstance(row, Mapping):
        return False
    needed, stocked = row.get("needed"), row.get("stocked")
    numbers = all(isinstance(v, int) and not isinstance(v, bool) for v in (needed, stocked))
    return numbers and needed > 0 and stocked >= needed


def feed_step_ready(jobs, observation) -> bool:
    """Whether the roadmap may propose ``power_coal_feed`` now (rule 3 of the module doc)."""
    jobs = list(jobs)
    power = _power_job(jobs)
    if power is None:
        return False
    if legacy_power(power) or belts_stocked(observation) or belts_machine_made(jobs):
        return True
    since = (power.get("evidence") or {}).get("tick")
    tick = (observation or {}).get("tick")
    numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (since, tick))
    return numbers and tick - since >= FEED_FALLBACK_TICKS


BELT_RECEIPT = "coal_feed_belts"
# Belts of a feed whose reservation failed (POWER_COAL_FEED_BUILDS estimate; saved plans 95 and 141).
UNPLANNED_FEED_BELTS = 100


def feed_belt_receipt(root, observation) -> dict | None:
    """Observe receipt ``coal_feed_belts``: ``{"needed", "stocked", "chest"}`` while the boiler is hand-fed.

    needed  = transport belts of the saved ``coal_plan`` (``UNPLANNED_FEED_BELTS`` without one)
    stocked = transport belts carried + in the supply cell's transport-belt product chest
    None when the plant is belt-fed, power is not built, or the files belong to another world.
    """
    import json
    from pathlib import Path

    def read(name):
        path = Path(root) / name
        try:
            value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    world = observation.get("world_id")
    builder = read("factory-builder.json")
    if builder.get("world_id") != world or not builder.get("power_plan") or coal_feed_mode(builder) != HAND_FED:
        return None
    plan = builder.get("coal_plan") or {}
    needed = sum(1 for e in plan.get("entities") or () if e.get("name") == MACHINE_MADE_BELT) or UNPLANNED_FEED_BELTS
    stocked = int((observation.get("inventory") or {}).get(MACHINE_MADE_BELT, 0) or 0)
    cell = read("supply-cell.json")
    row = next((r for r in cell.get("products") or () if r.get("item") == MACHINE_MADE_BELT), None)
    chest = None
    if cell.get("world_id") == world and cell.get("phase") != "retired" and row and row.get("output"):
        chest = row["output"]
        box = next(
            (
                e
                for e in observation.get("entities", [])
                if e.get("position") == chest and isinstance(e.get("inventory"), Mapping)
            ),
            None,
        )
        stocked += int((box or {}).get("inventory", {}).get(MACHINE_MADE_BELT, 0) or 0)
    return {"needed": needed, "stocked": stocked, "chest": chest}


def hand_fed_coal_per_minute(jobs, observation) -> float:
    """Coal/min the hand-fed boiler burns, for the capacity planner's coal rule; 0 once belt-fed.

    Measured generation (receipt ``electricity.networks[].generation_kw``)
    divided by the coal fuel value, at most the boiler's full-load burn; with
    no measurement ``UNMEASURED_LOAD_SHARE`` of full load.
    """
    jobs = list(jobs)
    power = _power_job(jobs)
    if power is None or _feed_done(jobs):
        return 0.0
    evidence = power.get("evidence") or {}
    full = evidence.get("boiler_full_load_coal_per_minute")
    fuel = evidence.get("coal_fuel_value")
    if not evidence.get("hand_fed") or not isinstance(full, (int, float)) or full <= 0:
        return 0.0
    networks = ((observation or {}).get("electricity") or {}).get("networks")
    kw = [n.get("generation_kw") for n in networks or () if isinstance(n, Mapping)]
    kw = [v for v in kw if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0]
    if kw and isinstance(fuel, (int, float)) and fuel > 0:
        return round(min(float(full), sum(kw) * 1000 * 60 / fuel), 3)
    return round(float(full) * UNMEASURED_LOAD_SHARE, 3)


def feed_in_bill(state: Mapping) -> bool:
    """Whether the saved ``coal_plan`` belongs to the current construction bill.

    A feed only reserved by ``power`` is not (its belts are demand of the later
    ``power_coal_feed`` step, ``POWER_COAL_FEED_BUILDS``); once that step has
    started (``coal_feed_started``) or the plant is belt-fed, it is.
    """
    return coal_feed_mode(state) != HAND_FED or bool(state.get("coal_feed_started"))


# ----- board -------------------------------------------------------------------------


def publish_planned_feed(ex, obs) -> dict | None:
    """Keep the reserved, unbuilt feed on the board while the boiler is hand-fed; drop it once belt-fed.

    The block has no ports (its coal goes only to the boiler) and no reserved
    area, so ``board.planned_unbuilt_hardware``, ``foreign_belts`` and
    ``SiteService.mark_board_hardware`` see each of its entities. Returns a
    blocked status on a board of another world/catalog, else None.
    """
    from .board import Board

    builder = ex.builder
    state = getattr(builder, "state", None) or {}
    plan = state.get("coal_plan")
    hand_fed = coal_feed_mode(state) == HAND_FED
    board = Board(ex.game, builder, ex.catalog)
    board.refresh(force=True)
    if not board.state:
        if not (hand_fed and plan):
            return None
        board.state = {
            "schema_version": 1,
            "world_id": obs["world_id"],
            "catalog_fingerprint": ex.catalog.fingerprint,
            "blocks": {},
        }
    if board.state.get("world_id") != obs["world_id"] or board.state.get("catalog_fingerprint") != getattr(
        ex.catalog, "fingerprint", None
    ):
        return {"status": "blocked", "reason": "power_coal_feed_board_identity_changed"}
    blocks = board.state.setdefault("blocks", {})
    wanted = feed_block(plan) if hand_fed and plan else None
    if blocks.get(BLOCK) == wanted:
        return None
    if wanted is None:
        blocks.pop(BLOCK, None)
    else:
        blocks[BLOCK] = wanted
    _atomic_json(board.path, board.state)
    return None


def feed_block(plan: Mapping) -> dict:
    """Board block of the reserved feed: its entities, no ports, owner ``power``.

    Many controllers read every block's ``reserved_area``, so the block has
    one: only the 2x2 drill footprint. The fuel arm and the routed belt lie
    outside it, so they count as the block's external hardware (marked by
    ``SiteService.mark_board_hardware``) instead of reserving the route's
    whole bounding box.
    """
    drill = plan.get("drill") or {}
    p = drill.get("position") or {}
    x, y = p.get("x", 0), p.get("y", 0)
    return {
        "ok": True,
        "reason": "",
        "entities": deepcopy(list(plan.get("entities") or [])),
        "ports": [],
        "required_items": deepcopy(dict(plan.get("required_items") or {})),
        "reserved_area": [[x - 1, y - 1], [x + 1, y + 1]],
        "template": "power_coal_feed",
        "owner": "power",
    }
