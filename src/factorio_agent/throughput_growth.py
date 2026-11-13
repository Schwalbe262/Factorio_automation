"""Plate throughput growth: the district's ore and furnace stages follow upcoming demand, by rule.

One demand model and one supply model per plate (iron, copper) of the paired
smelting district, then one growth rule in the shape of ``capacity_planning``
(time comparison) and ``science_growth`` (evidence first). Pure functions
except ``growth_receipt``, which reads the saved district, mining and board
state of the runtime (read-only).

Demand over the next ``DEMAND_HORIZON_MINUTES``::

    taps     = declared demand of built bus taps on the plate, except stock rows
               (``production_row_*``: their output is construction stock, already in the bill)
    bill     = shortfall + max(0, lookahead - spare stock)     (capacity_planning; the hand
               collection for construction is this bill)
    work     = bill + taps * H                                  plates to deliver in the horizon
    demand   = work / H                                         plates/min

Supply::

    furnaces = nominal plates/min of built furnaces (smelting_throughput_bounds)
    ore      = connected nominal ore/min (drills x drill rate, depleted drills excluded)
    belt     = 2 lanes x unstacked lane capacity of the plate port
    capacity = min(furnaces, ore, belt)            built-but-not-yet-producing units count
                                                   at nominal rate: a build never looks like a deficit
    measured = engine production counter (reported; includes starter cells)
    signals  = built furnaces idle for ore / with full output, drills backed up

Rule (``decide``)::

    covered    capacity >= demand * COVER_MARGIN               -> no growth
    bottleneck evidence first: furnace output full -> demand; furnaces idle for ore and
               drills backed -> transport; furnaces idle for ore -> ore; drills backed ->
               furnaces; else the smaller nominal stage; equal -> balanced (ore first)
    step       ore: one drill pair in the bank's reserved slots (mining_outpost target
               + pair rate; a full bank gets a connected module from outposts.py);
               furnaces: one slot per row (smelting_capacity, rate <= ore);
               balanced: the ore step first; a step after which the other stage binds
               below demand is valued together with that stage's next step (only
               the first is proposed; the second is re-decided once it is measured)
    pays       T0 = 60 * work / capacity
               T1 = fund + build + 60 * (work + own plate cost) / (capacity + gain)
               grow when T0 - T1 >= MIN_SAVING_SECONDS and T1 <= MAX_RATIO * T0
    once       one step per plate; nothing while a growth or outpost job of the
               resource is open or the last one finished < SETTLE_TICKS ago
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .capacity_planning import finite_number, fund_seconds, stock_on_hand

PLATES = {"iron-plate": "iron-ore", "copper-plate": "copper-ore"}
FURNACE_NAMES = frozenset({"stone-furnace", "steel-furnace", "electric-furnace"})
MINING = "mining_outpost"
SMELTING = "smelting_capacity"
GROWTH_CAPABILITIES = (MINING, SMELTING)
NODE_PREFIX = "auto_throughput_"
OPEN_NODE_STATUSES = frozenset({"pending", "ready", "running", "waiting"})
# Slack growth fills a free job slot just above science growth (plates feed science);
# growth that removes what the critical path waits for ranks like smelting_capacity.
SLACK_PRIORITY = -599
CRITICAL_PRIORITY = -52

# Horizon of the demand model: the bill of the next roadmap steps is due within it.
DEMAND_HORIZON_MINUTES = 10.0
# Supply covers demand with this margin: no growth (surplus output wastes materials and build time).
COVER_MARGIN = 1.1
# Expansion must clearly beat waiting (same thresholds as capacity_planning).
MIN_SAVING_SECONDS = 60.0
MAX_RATIO = 0.85
# Share of built furnaces idle for lack of ore / drills waiting for belt space that counts as a signal.
STARVED_SHARE = 0.5
BACKED_SHARE = 0.5
# After a growth step finished, evidence settles (belt transit, first crafts) before the next decision.
SETTLE_TICKS = 3600
# Actor time of one step: a round trip to the site plus placing each entity.
TRIP_SECONDS = 30.0
SECONDS_PER_ENTITY = 3.0
# Hand-gathered inputs (a tree gives 4 wood), as capacity_planning.
HAND_GATHERED_SECONDS = {"wood": 2.0}
EPSILON = 1e-6

IDLE_FOR_ORE = frozenset({"no_ingredients", "item_ingredient_shortage"})
OUTPUT_FULL = frozenset({"full_output"})
DRILL_BACKED = frozenset({"waiting_for_space_in_destination", "full_output"})


def bottleneck_blocks_critical_path(plate: str, jobs: Iterable[Mapping], observation: Mapping | None = None) -> bool:
    """Whether an active job the roadmap waits on is waiting for ``plate``.

    Placeholder for the shared critical-path rule: a job (other than a growth
    capability) whose reason starts "waiting" and names the plate or its evidence
    item (``early_start.contested_plates``), or that waits for "district plates"
    (``planned_main_bus`` lane tails: both plates).
    """
    from .early_start import ACTIVE_STATUSES, contested_plates

    jobs = list(jobs)
    if plate in contested_plates(jobs, exclude=GROWTH_CAPABILITIES):
        return True
    return any(
        job.get("status") in ACTIVE_STATUSES
        and job.get("capability") not in GROWTH_CAPABILITIES
        and re.match(r"waiting\b.*\bdistrict plates\b", str(job.get("reason") or ""))
        for job in jobs
    )


@dataclass(frozen=True)
class PlateFacts:
    """Numbers of one plate's district bank, its ore banks and its consumers (rates per minute)."""

    plate: str
    resource: str
    tap_rate: float
    bill: float
    furnace_nominal: float
    furnace_each: float
    per_side: int
    capacity_slots: int
    ore_nominal: float
    drill_rate: float
    pairs_built: int
    pair_slots: int
    belt_cap: float | None = None
    measured: float | None = None
    furnaces: int = 0
    furnaces_idle_for_ore: int = 0
    furnaces_output_full: int = 0
    drills: int = 0
    drills_backed: int = 0
    ore_step_cost: Mapping[str, float] = field(default_factory=dict)
    ore_step_entities: int = 0
    furnace_step_cost: Mapping[str, float] = field(default_factory=dict)
    furnace_step_entities: int = 0
    # item -> (stock on hand, produced per minute) for the step's non-own inputs
    stocks: Mapping[str, tuple[float, float | None]] = field(default_factory=dict)
    settling: str = ""


def _seconds_to_deliver(amount: float, rate: float) -> float:
    if amount <= 0:
        return 0.0
    return 60.0 * amount / rate if rate > 0 else math.inf


def _fund(cost: Mapping[str, float], facts: PlateFacts) -> float:
    """Seconds until the step's inputs other than its own plate are on hand (inf: unknown input)."""
    longest = 0.0
    for item, amount in cost.items():
        if item == facts.plate or amount <= 0:
            continue
        if item in HAND_GATHERED_SECONDS:
            longest = max(longest, amount * HAND_GATHERED_SECONDS[item])
            continue
        if item not in facts.stocks:
            return math.inf
        stock, produced = facts.stocks[item]
        longest = max(longest, fund_seconds(amount, stock, produced))
    return longest


def bottleneck(facts: PlateFacts) -> tuple[str, str]:
    """``(stage, why)``: ``ore``, ``furnaces``, ``balanced``, ``demand``, ``transport`` or ``belt``.

    Evidence first: a full furnace output means the consumers do not take
    plates; furnaces idle for ore mean ore is short whatever the nameplate
    says (unless the drills are backed up too: then the ore belt is at fault);
    backed-up drills mean the furnaces are short. Without evidence the smaller
    nominal stage is the bottleneck.
    """
    ore, furnaces = facts.ore_nominal, facts.furnace_nominal
    if facts.furnaces_output_full:
        return "demand", f"{facts.furnaces_output_full}/{facts.furnaces} built furnaces hold full output"
    if facts.belt_cap is not None and min(ore, furnaces) >= facts.belt_cap - EPSILON:
        return "belt", f"stages reach the plate belt ({facts.belt_cap:g}/min)"
    starved = facts.furnaces and facts.furnaces_idle_for_ore / facts.furnaces >= STARVED_SHARE
    backed = facts.drills and facts.drills_backed / facts.drills >= BACKED_SHARE
    if starved and backed:
        return "transport", "furnaces idle for ore while drills are backed up: the ore belt, not a stage"
    if starved:
        return "ore", f"{facts.furnaces_idle_for_ore}/{facts.furnaces} furnaces idle for ore"
    if backed:
        return "furnaces", f"{facts.drills_backed}/{facts.drills} drills backed up"
    if ore < furnaces - EPSILON:
        return "ore", f"ore {ore:g}/min < furnaces {furnaces:g}/min"
    if furnaces < ore - EPSILON:
        return "furnaces", f"furnaces {furnaces:g}/min < ore {ore:g}/min"
    return "balanced", f"ore = furnaces = {ore:g}/min"


def _ore_step(facts: PlateFacts) -> dict[str, Any]:
    pair = 2 * facts.drill_rate
    target = round(min(960.0, facts.ore_nominal + pair), 1)
    full = facts.pairs_built >= facts.pair_slots
    return {
        "stage": "ore",
        "capability": MINING,
        "parameters": {"resource": facts.resource, "rate_per_minute": target},
        "ore_after": target,
        "cost": dict(facts.ore_step_cost),
        "entities": facts.ore_step_entities,
        "unit": (
            "another 2-drill module joined to the ore trunk (outposts.prepare_connected_bank)"
            if full
            else f"drill pair {facts.pairs_built + 1}/{facts.pair_slots} in the bank's reserved slots"
        ),
    }


def _furnace_step(facts: PlateFacts, ore: float) -> dict[str, Any] | None:
    """One more furnace per row, rate capped by the ore (smelting_capacity refuses rate > ore); None when impossible."""
    if facts.per_side >= facts.capacity_slots or facts.furnace_each <= 0:
        return None
    full = 2 * (facts.per_side + 1) * facts.furnace_each
    rate = round(min(full, ore, 450.0), 1)
    if math.ceil(rate / facts.furnace_each / 2 - 1e-9) <= facts.per_side:
        return None
    return {
        "stage": "furnaces",
        "capability": SMELTING,
        "parameters": {"resource": facts.resource, "rate_per_minute": rate},
        "furnaces_after": full,
        "cost": dict(facts.furnace_step_cost),
        "entities": facts.furnace_step_entities,
        "unit": f"furnace slot {facts.per_side + 1}/{facts.capacity_slots} in both rows",
    }


def _capacity(ore: float, furnaces: float, belt: float | None) -> float:
    return min(ore, furnaces, belt if belt is not None else math.inf)


def decide(facts: PlateFacts) -> dict[str, Any]:
    """The growth decision for one plate (module rule), JSON-safe; ``grow`` True only for a step that pays."""
    horizon = DEMAND_HORIZON_MINUTES
    work = max(0.0, facts.bill) + max(0.0, facts.tap_rate) * horizon
    demand = work / horizon
    capacity = _capacity(facts.ore_nominal, facts.furnace_nominal, facts.belt_cap)
    row: dict[str, Any] = {
        "plate": facts.plate,
        "resource": facts.resource,
        "grow": False,
        "demand": {
            "taps_per_minute": round(facts.tap_rate, 2),
            "bill_plates": round(facts.bill, 1),
            "horizon_minutes": horizon,
            "per_minute": round(demand, 2),
        },
        "supply": {
            "measured_per_minute": None if facts.measured is None else round(facts.measured, 2),
            "furnaces_per_minute": round(facts.furnace_nominal, 2),
            "ore_per_minute": round(facts.ore_nominal, 2),
            "belt_per_minute": facts.belt_cap,
            "capacity_per_minute": round(capacity, 2),
            "furnaces": f"{facts.per_side * 2}/{facts.capacity_slots * 2}",
            "drill_pairs": f"{facts.pairs_built}/{facts.pair_slots}",
        },
        "signals": {
            "furnaces_idle_for_ore": facts.furnaces_idle_for_ore,
            "furnaces_output_full": facts.furnaces_output_full,
            "drills_backed": facts.drills_backed,
            "furnaces_observed": facts.furnaces,
            "drills_observed": facts.drills,
        },
    }
    stage, why = bottleneck(facts)
    row["bottleneck"] = stage
    row["bottleneck_reason"] = why
    if facts.settling:
        return dict(row, reason=f"settling: {facts.settling}")
    if capacity >= demand * COVER_MARGIN:
        return dict(row, reason=f"covered: capacity {capacity:g}/min >= demand {demand:.1f}/min x {COVER_MARGIN:g}")
    if stage in ("demand", "transport"):
        return dict(row, reason=f"no stage to grow: {why}")
    if stage == "belt":
        return dict(row, reason=f"{why}; next unit: another bank in the zone's free half (not automated)")
    ore_step = _ore_step(facts)
    if stage == "furnaces":
        step = _furnace_step(facts, facts.ore_nominal)
        if step is None:
            return dict(
                row,
                reason="paired_bank_full_next_growth_is_another_bank: every reserved furnace slot is built; "
                "next unit: a second paired bank of the same plate in the district zone's free half, "
                "fed by a splitter off the ore trunk, output merged onto the same bus lane (not automated)",
                next_unit="another_bank",
            )
        steps = [step]
        ore_after, furnaces_after = facts.ore_nominal, step["furnaces_after"]
        if ore_after < min(furnaces_after, demand):  # the ore binds next: value the drill pair with it
            steps.append(ore_step)
            ore_after = ore_step["ore_after"]
    else:  # ore (balanced: ore first, smelting_capacity never runs above the ore)
        steps = [ore_step]
        ore_after, furnaces_after = ore_step["ore_after"], facts.furnace_nominal
        follow = _furnace_step(facts, ore_after) if furnaces_after < min(ore_after, demand) else None
        if follow:  # the furnaces bind next: value the pair with the furnace step it enables
            steps.append(follow)
            furnaces_after = follow["furnaces_after"]
    gain = _capacity(ore_after, furnaces_after, facts.belt_cap) - capacity
    cost: Counter = Counter()
    for step in steps:
        cost.update(step["cost"])
    entities = sum(step["entities"] for step in steps)
    wait = _seconds_to_deliver(work, capacity)
    fund = _fund(cost, facts)
    build = len(steps) * TRIP_SECONDS + entities * SECONDS_PER_ENTITY
    own = cost.get(facts.plate, 0.0)
    expand = fund + build + _seconds_to_deliver(work + own, capacity + gain) if gain > EPSILON else math.inf
    first = steps[0]
    row.update(
        step={
            "stage": first["stage"],
            "capability": first["capability"],
            "parameters": first["parameters"],
            "unit": first["unit"],
            "valued_with": [s["unit"] for s in steps[1:]],
            "gain_per_minute": round(gain, 2),
            "cost": {k: round(v, 1) for k, v in sorted(cost.items())},
        },
        times={
            "wait_seconds": None if math.isinf(wait) else round(wait),
            "expand_seconds": None if math.isinf(expand) else round(expand),
            "fund_seconds": None if math.isinf(fund) else round(fund),
            "build_seconds": round(build),
            "saving_seconds": None if math.isinf(expand) else (None if math.isinf(wait) else round(wait - expand)),
        },
    )
    if gain <= EPSILON:
        return dict(row, reason=f"{first['unit']} adds no plates: {why}")
    if math.isinf(expand):
        return dict(row, reason=f"step inputs cannot be funded ({', '.join(sorted(cost))})")
    if not (wait - expand >= MIN_SAVING_SECONDS and expand <= MAX_RATIO * wait):
        return dict(row, reason=f"waiting is as fast: wait {wait:.0f}s, expand {expand:.0f}s")
    row.update(
        grow=True,
        capability=first["capability"],
        parameters=first["parameters"],
        reason=(
            f"{why}; demand {demand:.1f}/min > capacity {capacity:g}/min: {first['unit']} "
            f"+{gain:g}/min saves {wait - expand:.0f}s (wait {wait:.0f}s, expand {expand:.0f}s)"
        ),
    )
    return row


# ----- receipt (reads saved state, read-only) ---------------------------------------------
def _key(e: Mapping) -> tuple:
    return e["name"], e["position"]["x"], e["position"]["y"]


def _statuses(observation: Mapping) -> dict[tuple, str | None]:
    return {
        _key(e): e.get("status_name")
        for e in observation.get("entities") or ()
        if isinstance(e, Mapping) and isinstance(e.get("position"), Mapping) and "name" in e
    }


def _bill(plate: str, observation: Mapping) -> float:
    """``capacity_planning`` required: shortfall + lookahead beyond spare stock."""
    supply = observation.get("resource_supply_options")
    evidence = (supply.get(plate) or {}).get("evidence") if isinstance(supply, Mapping) else None
    evidence = evidence if isinstance(evidence, Mapping) else {}
    lookahead = observation.get("roadmap_material_demand")
    extra = max(0.0, finite_number((lookahead or {}).get(plate)) or 0.0) if isinstance(lookahead, Mapping) else 0.0
    available, billed = finite_number(evidence.get("available")), finite_number(evidence.get("required"))
    if available is not None and billed is not None:
        extra = max(0.0, extra - max(0.0, available - billed))
    return max(0.0, finite_number(evidence.get("shortfall")) or 0.0) + extra


def _stocks(observation: Mapping) -> dict[str, tuple[float, float | None]]:
    supply = observation.get("resource_supply_options")
    rows = {}
    for item, row in supply.items() if isinstance(supply, Mapping) else ():
        evidence = row.get("evidence") if isinstance(row, Mapping) else None
        if isinstance(evidence, Mapping):
            rows[item] = (stock_on_hand(evidence), finite_number(evidence.get("produced_per_minute")))
    return rows


def _settling(resource: str, jobs: Iterable[Mapping], tick) -> str:
    for job in jobs:
        parameters = job.get("parameters") if isinstance(job.get("parameters"), Mapping) else {}
        if job.get("capability") not in GROWTH_CAPABILITIES or parameters.get("resource") != resource:
            continue
        if job.get("status") not in ("succeeded", "blocked", "failed"):
            return f"{job.get('capability')} for {resource} is open"
        last = job.get("last_tick")
        if job.get("status") == "succeeded" and type(last) is int and type(tick) is int and tick - last < SETTLE_TICKS:
            return f"{job.get('capability')} for {resource} finished {tick - last} ticks ago"
    return ""


def plate_facts(catalog, plate, bank, bound, mining_banks, stations, jobs, observation) -> PlateFacts:
    """``PlateFacts`` of one district bank from saved state rows and the observe receipt."""
    from .progression import MINING_OUTPOST_BUILDS, raw_cost

    resource = PLATES[plate]
    statuses = _statuses(observation)
    furnaces = [e for e in bank.get("entities", []) if e.get("name") in FURNACE_NAMES]
    nominal = float(bound["nominal_capacity_per_minute"])
    per_side = int(bank.get("per_side_count", len(furnaces) // 2))
    furnace_status = [statuses.get(_key(e)) for e in furnaces]
    next_slots = [g for g in bank.get("growth_slots", []) if g.get("slot") == per_side]
    slot_items = Counter(e["name"] for g in next_slots for e in g.get("entities", []))
    first = mining_banks[0] if mining_banks else {}
    plan = first.get("plan") or {}
    pairs = plan.get("pair_count") or 0
    slots = first.get("reserved_pair_slots", pairs) or 0
    drills = [
        e
        for b in mining_banks
        for e in (b.get("plan") or {}).get("entities", [])
        if e["name"] == "electric-mining-drill"
    ]
    profile = first.get("capacity_profile") or {}
    drill_rate = finite_number(profile.get("planned_per_drill_per_minute"))
    if drill_rate is None:
        count = sum(e["name"] == "electric-mining-drill" for e in plan.get("entities", []))
        drill_rate = float(first.get("nominal_rate_per_minute", 0)) / count if count else 0.0
    contract = first.get("layout_contract") or plan.get("layout_contract") or {}
    growth = Counter(e["name"] for e in contract.get("growth_entities", []))
    if slots > pairs and growth:
        pair_items = {name: math.ceil(n / (slots - pairs)) for name, n in growth.items()}
    else:
        pair_items = dict(MINING_OUTPOST_BUILDS)
    belt = finite_number(bound.get("unstacked_belt_capacity_per_lane"))
    measured = (
        ((observation.get("production_rate_measurement") or {}).get("rates_per_game_minute") or {}).get(plate) or {}
    ).get("produced")
    return PlateFacts(
        plate=plate,
        resource=resource,
        tap_rate=sum(
            float(s.get("demand_per_minute") or 0)
            for s in stations
            if isinstance(s, Mapping)
            and s.get("item") == plate
            and s.get("built", True)
            and not str(s.get("owner") or "").startswith("production_row")
        ),
        bill=_bill(plate, observation),
        furnace_nominal=nominal,
        furnace_each=nominal / len(furnaces) if furnaces else 0.0,
        per_side=per_side,
        capacity_slots=int(bank.get("capacity_slots", per_side)),
        ore_nominal=float(bound["connected_nominal_ore_supply"]),
        drill_rate=drill_rate,
        pairs_built=int(pairs),
        pair_slots=int(slots),
        belt_cap=None if belt is None else 2 * belt,
        measured=finite_number(measured),
        furnaces=sum(s is not None for s in furnace_status),
        furnaces_idle_for_ore=sum(s in IDLE_FOR_ORE for s in furnace_status),
        furnaces_output_full=sum(s in OUTPUT_FULL for s in furnace_status),
        drills=sum(statuses.get(_key(e)) is not None for e in drills),
        drills_backed=sum(statuses.get(_key(e)) in DRILL_BACKED for e in drills),
        ore_step_cost=raw_cost(catalog, pair_items),
        ore_step_entities=sum(pair_items.values()),
        furnace_step_cost=raw_cost(catalog, slot_items),
        furnace_step_entities=sum(slot_items.values()),
        stocks=_stocks(observation),
        settling=_settling(resource, jobs, observation.get("tick")),
    )


def growth_receipt(root, catalog, jobs, observation) -> dict | None:
    """``{"plates": [decide(...)]}`` for this runtime's paired district, or None (no district of this world)."""
    import json
    from pathlib import Path

    def read(name):
        path = Path(root) / name
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return value if isinstance(value, dict) and value.get("world_id") == observation.get("world_id") else {}

    district = read("smelting-district.json")
    if not district.get("banks"):
        return None
    mining = read("mining-outposts.json")
    bus = read("pcb-board.json").get("shared_main_bus")
    stations = bus.get("stations", []) if isinstance(bus, Mapping) else []
    bounds = observation.get("smelting_throughput_bounds")
    bound_rows = {r.get("resource"): r for r in (bounds or {}).get("banks", []) if isinstance(r, Mapping)}
    jobs = list(jobs)
    rows = []
    for plate, resource in PLATES.items():
        bank = next((b for b in district["banks"] if b.get("resource") == resource), None)
        bound = bound_rows.get(resource)
        if bank is None or "growth_slots" not in bank or not bound:
            continue
        banks = [
            b for b in mining.get("banks", []) if b.get("ready") and (b.get("plan") or {}).get("resource") == resource
        ]
        if not banks:
            rows.append({"plate": plate, "resource": resource, "grow": False, "reason": "no ready mining bank"})
            continue
        facts = plate_facts(catalog, plate, bank, bound, banks, stations, jobs, observation)
        rows.append(decide(facts))
    return {
        "plates": rows,
        "scope": "nominal stage capacities, declared taps and the roadmap bill; module throughput_growth",
    }


# ----- autopilot nodes ---------------------------------------------------------------------
def throughput_growth_nodes(jobs, queue_nodes, observation, capabilities, *, critical: bool) -> list[dict]:
    """One node per plate whose receipt row grows, ``critical`` selecting the critical-path class.

    The receipt (``growth_receipt``) holds the rule; here only: the capability
    exists and its ``requires`` succeeded, nothing of the resource's growth
    capabilities is queued or open, and the same step has not stalled.
    """
    receipt = observation.get("throughput_growth")
    rows = receipt.get("plates") if isinstance(receipt, Mapping) else None
    if not isinstance(rows, list):
        return []
    jobs = list(jobs)
    succeeded = {job.get("capability") for job in jobs if job.get("status") == "succeeded"}
    queued_ids = {node.get("id") for node in queue_nodes}
    nodes = []
    for row in rows:
        if not isinstance(row, Mapping) or not row.get("grow"):
            continue
        capability, parameters = row.get("capability"), row.get("parameters")
        if (
            capability not in GROWTH_CAPABILITIES
            or capability not in capabilities
            or not isinstance(parameters, Mapping)
        ):
            continue
        if "smelting_district" not in succeeded or not set(capabilities[capability].get("requires", [])) <= succeeded:
            continue
        if bottleneck_blocks_critical_path(row.get("plate"), jobs, observation) != critical:
            continue
        resource = parameters.get("resource")

        def same_resource(r):
            p = r.get("parameters")
            return (
                r.get("capability") in GROWTH_CAPABILITIES and isinstance(p, Mapping) and p.get("resource") == resource
            )

        if any(same_resource(n) and n.get("status", "pending") in OPEN_NODE_STATUSES for n in queue_nodes):
            continue
        if any(same_resource(j) and j.get("status") not in ("succeeded", "blocked", "failed") for j in jobs):
            continue
        if any(
            j.get("capability") == capability
            and j.get("parameters") == parameters
            and j.get("status") in ("blocked", "failed")
            for j in jobs
        ):
            continue
        node_id = f"{NODE_PREFIX}{resource}_{capability}_{int(parameters.get('rate_per_minute', 0))}"
        if node_id in queued_ids:
            continue
        nodes.append(
            {
                "id": node_id,
                "capability": capability,
                "parameters": dict(parameters),
                "priority": CRITICAL_PRIORITY if critical else SLACK_PRIORITY,
                "depends_on": [],
                "reason": f"{'critical' if critical else 'slack'} plate growth: {row.get('reason', '')}"[:300],
            }
        )
    return nodes
