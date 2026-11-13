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
   ``power_coal_feed`` so no other block is placed on its route or on the
   electric drill site that replaces its burner drill; the block is removed
   once the feed is belt-fed and the drill is electric.
6. ``hand_fed_coal_per_minute`` is the boiler's coal burn for the capacity
   planner's coal rule while the boiler is hand-fed.
7. Carried wood is burned before coal in the hand-fed boiler (rule 2,
   ``boiler_fuel_action``); one slot of wood is held for it
   (``hand_fed_wood_reserve``). Belt-fed boilers get wood through
   ``wood_burner_feed``.
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
WOOD = "wood"


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


def wood_fuel_value(catalog) -> float:
    """Joules in one wood from the catalog (2 MJ in the base game)."""
    value = ((getattr(catalog, "items", None) or {}).get(WOOD) or {}).get("fuel_value")
    return float(value) if isinstance(value, (int, float)) and value > 0 else 2_000_000.0


def wood_slot(catalog) -> int:
    """Wood one boiler fuel slot holds (one wood stack)."""
    stack = ((getattr(catalog, "items", None) or {}).get(WOOD) or {}).get("stack_size")
    return int(stack) * BOILER_FUEL_SLOTS if isinstance(stack, int) and stack > 0 else 100 * BOILER_FUEL_SLOTS


def _hand_fed_boiler(builder, obs):
    state = getattr(builder, "state", None) or {}
    if state.get("world_id") != obs.get("world_id") or coal_feed_mode(state) != HAND_FED:
        return None, None
    spec = _boiler_spec(state)
    return spec, (_observed(obs, spec) if spec else None)


def hand_fed_wood_reserve(builder, catalog, obs, item) -> int:
    """Carried wood held for the hand-fed boiler (one fuel slot of wood); 0 otherwise (``carried_surplus.reserve``)."""
    if item != WOOD:
        return 0
    _, boiler = _hand_fed_boiler(builder, obs)
    return wood_slot(catalog) if boiler is not None else 0


def boiler_fuel_action(builder, bootstrap, catalog, obs) -> dict | None:
    """One batched fuel action for the hand-fed boiler, or None.

    Only while the builder's ``coal_feed_mode`` is ``hand_fed`` and the
    plant's boiler stands. A trip starts when the boiler holds less than
    ``refill_below`` coal-equivalent (fuel slot plus the burning remainder).
    Carried wood is burned first (owner, 2026-11-13), but only once more than
    one stack of wood is carried: the slot is then filled with wood; coal still in the
    slot is taken out first (one fuel slot holds one fuel kind; the coal is
    saved for the burners). Otherwise coal fills the slot to ``target``: with
    at least the fill or ``FUEL_BATCH`` carried the coal goes in (wood left
    in the slot burns out first; it is never taken back out); an empty boiler takes any carried coal;
    else the coal is collected (``ensure_item``). A plain wait returns None,
    so the job keeps working.
    """
    spec, boiler = _hand_fed_boiler(builder, obs)
    if boiler is None:
        return None
    sizing = boiler_sizing(catalog)
    coal_value, wood_value = coal_fuel_value(catalog), wood_fuel_value(catalog)
    inventory = boiler.get("inventory") or {}
    held, held_wood = int(inventory.get("coal", 0)), int(inventory.get(WOOD, 0))
    burning = float(boiler.get("remaining_burning_fuel", 0) or 0)
    stored = (held * coal_value + held_wood * wood_value + burning) / coal_value
    if stored >= sizing["refill_below"]:
        return None
    wood_fill = min(wood_slot(catalog), math.ceil(sizing["target"] * coal_value / wood_value)) - held_wood
    wood = int((obs.get("inventory") or {}).get(WOOD, 0))
    if wood_fill > 0 and wood > wood_slot(catalog):  # only above one carried stack (owner, 2026-11-13)
        move = builder._move(obs, spec["position"])
        if move:
            return move
        if held > 0:
            return _take_fuel(spec, "coal", held, "hand-fed boiler: take the coal out so carried wood burns instead")
        return _insert_fuel(spec, WOOD, min(wood, wood_fill), "hand-fed boiler: burn carried wood before coal")
    fill = sizing["target"] - held
    if fill <= 0:
        return None
    carried = int((obs.get("inventory") or {}).get("coal", 0))
    if carried >= min(fill, FUEL_BATCH) or (stored <= 0 and carried > 0):
        move = builder._move(obs, spec["position"])
        if move:
            return move
        if held_wood > 0:
            # Let the wood in the slot burn out; taking it out put it back in the hand and the
            # wood rule above inserted it again (take/insert loop on map 4177156614).
            return None
        return _insert_fuel(
            spec, "coal", min(carried, fill), "hand-fed boiler: refill before the dedicated coal feed exists"
        )
    result = bootstrap.ensure_item(obs, "coal", fill)
    return result if isinstance(result, dict) and result.get("type") else None


def _insert_fuel(spec, item, count, reason) -> dict:
    return {
        "type": "insert",
        "name": spec["name"],
        "position": deepcopy(spec["position"]),
        "item": item,
        "inventory": "fuel",
        "count": int(count),
        "reason": reason,
    }


def _take_fuel(spec, item, count, reason) -> dict:
    return {
        "type": "take",
        "name": spec["name"],
        "position": deepcopy(spec["position"]),
        "item": item,
        "inventory": "fuel",
        "count": int(min(count, 50)),
        "reason": reason,
    }


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
    """Keep the reserved feed on the board while the boiler is hand-fed or the drill is still a burner.

    Dropped once the feed is belt-fed and ``electric_coal`` replaced the drill
    (called from the ``power``, ``power_coal_feed`` and ``electric_coal`` steps).

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
    # A belt-fed feed keeps a block it already has (the upgrade site); an old
    # map that never had one does not get one now.
    keep = hand_fed or (upgrade_pending(plan) and BLOCK in blocks)
    wanted = feed_block(plan) if plan and keep else None
    if wanted is not None:
        from .planning import overlap

        others = [b.get("reserved_area") for k, b in blocks.items() if k != BLOCK and isinstance(b, dict)]
        if any(isinstance(a, list) and len(a) == 2 and overlap(wanted["reserved_area"], a) for a in others):
            # Planned before the upgrade site was reserved: keep the drill footprint only.
            wanted = feed_block(plan, upgrade_site=False)
    if blocks.get(BLOCK) == wanted:
        return None
    if wanted is None:
        blocks.pop(BLOCK, None)
    else:
        blocks[BLOCK] = wanted
    _atomic_json(board.path, board.state)
    return None


def upgrade_pending(plan: Mapping) -> bool:
    """Whether the feed's burner drill still waits for its electric replacement (``electric_coal``)."""
    return ((plan or {}).get("drill") or {}).get("name") == "burner-mining-drill"


def feed_block(plan: Mapping, *, upgrade_site: bool = True) -> dict:
    """Board block of the reserved feed: its entities, no ports, owner ``power``.

    Many controllers read every block's ``reserved_area``, so the block has
    one: the burner drill's 2x2 footprint joined with the 3x3 footprint of
    its electric replacement (``electric_mining.replacement_for``: centred
    half a tile up-left), the site ``electric_coal`` later needs. With only
    the 2x2 footprint reserved, starter cells took the replacement's extra
    row and column (seeds 3794581402, 1634401926:
    ``electric_upgrade_footprint_obstructed``). The block stays on the board
    after the feed is belt-fed until the drill is electric
    (:func:`upgrade_pending`). The fuel arm and the routed belt lie outside
    the area, so they count as the block's external hardware (marked by
    ``SiteService.mark_board_hardware``) instead of reserving the route's
    whole bounding box.
    """
    drill = plan.get("drill") or {}
    p = drill.get("position") or {}
    x, y = p.get("x", 0), p.get("y", 0)
    area = [[x - 1, y - 1], [x + 1, y + 1]]
    if upgrade_site and upgrade_pending(plan):
        area = [[x - 2, y - 2], [x + 1, y + 1]]
    return {
        "ok": True,
        "reason": "",
        "entities": deepcopy(list(plan.get("entities") or [])),
        "ports": [],
        "required_items": deepcopy(dict(plan.get("required_items") or {})),
        "reserved_area": area,
        "template": "power_coal_feed",
        "owner": "power",
    }
