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
    measured = force-wide engine production counter, split (reported):
               connected = district furnaces on the bus (working share x nominal, <= measured)
               off_bus   = measured - connected (starter cells: hand-collectable stock only,
                           already in the bill's ``available``; nothing once they retire)
    signals  = built furnaces idle for ore / with full output, drills backed up

Rule (``decide``)::

    covered    capacity >= demand * COVER_MARGIN               -> no growth
    bottleneck evidence first: furnace output full -> demand; furnaces idle for ore and
               drills backed -> transport; furnaces idle for ore -> ore; drills backed ->
               furnaces; else the smaller nominal stage; equal -> balanced (ore first)
    packages   every (k drill pairs, f furnace slots per row) along drills -> ore belt ->
               furnaces -> plate belt: k up to the free reserved pairs plus one connected
               module of MODULE_PAIRS_MAX pairs (outposts.prepare_connected_bank), f up to
               the free reserved slots; capacity_after = min(ore + 2 k drill, 2 (per_side + f)
               furnace, belt, SMELTING_RATE_MAX); only efficient packages (dropping one unit
               of either stage lowers capacity_after), with a unit of the bottleneck stage
    time       the bill is served after the taps (taps take their rate first):
               S(bill, rate) = 60 bill / (rate - taps)        (inf when rate <= taps)
               T0   = S(bill, capacity)
               prep = max(fund of other inputs, fund_seconds(claimed + own plates,
                      plate stock, capacity)) + stages x TRIP + entities x SECONDS_PER_ENTITY
                      (claimed = plates critical jobs reserve: a slack growth job gets them last)
               T1   = prep + S(bill + own - (capacity - taps) prep / 60, capacity_after)
                      (the plant keeps producing while the package is funded and built)
    usable     gain x (T1 - prep) / 60 >= own plates: the known demand uses the package
               long enough to repay its plates (no capacity the known demand cannot use)
    choose     smallest package first; a larger one replaces it only when its T1 is
               MIN_SAVING_SECONDS lower (ties go to the smaller package)
    grow when  T0 - T1 >= MIN_SAVING_SECONDS and T1 <= MAX_RATIO T0, or
               T0 > payback = prep + 60 own / gain and T1 < T0 (a wait longer than the
               package needs to repay itself is never accepted on a thin margin)
    stages     the package runs as its existing jobs in order (mining_outpost into the
               reserved pairs, mining_outpost beyond them = connected module,
               smelting_capacity), chained by ``depends_on``; nothing granted
    once       one package per plate; nothing while a growth or outpost job/node of the
               resource is open or the last one finished < SETTLE_TICKS ago
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
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
# One connected module adds at most 8 drills (outposts.prepare_extension / prepare_connected_bank).
MODULE_PAIRS_MAX = 4
# smelting.validate_capacity: a bank never runs above this rate; mining_outpost targets stop at 960.
SMELTING_RATE_MAX = 450.0
ORE_RATE_MAX = 960.0
# Receipt size: the best packages listed beside the chosen one.
RECEIPT_ALTERNATIVES = 5
# Starter-cell phases after which their output no longer funds anything (starter_retirement.json).
RETIRING_PHASES = frozenset({"retiring", "retired"})

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
    # cost of one drill pair beyond the reserved slots (connected module, MINING_OUTPOST_BUILDS)
    module_step_cost: Mapping[str, float] | None = None
    module_step_entities: int | None = None
    # plates of this kind that critical jobs reserve (critical_path claims): a slack growth job gets them last
    claimed: float = 0.0
    # measured split: district furnaces on the bus vs everything else (starter cells off the bus)
    connected_measured: float | None = None
    off_bus_measured: float | None = None
    starter_phase: str = ""
    patch_rows: int = 0


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


def _capacity(ore: float, furnaces: float, belt: float | None) -> float:
    return min(ore, furnaces, belt if belt is not None else math.inf)


def _serve_seconds(bill: float, rate: float, taps: float) -> float:
    """Seconds until ``bill`` plates are delivered at ``rate`` while the taps take theirs first (inf: never)."""
    if rate < taps - EPSILON:
        return math.inf  # the taps alone are never served
    if bill <= 0:
        return 0.0
    net = rate - taps
    return 60.0 * bill / net if net > EPSILON else math.inf


def _after(facts: PlateFacts, pairs: int, slots: int) -> tuple[float, float, float]:
    """``(ore, furnaces, capacity)`` per minute after ``pairs`` more drill pairs and ``slots`` more slots per row."""
    ore = min(ORE_RATE_MAX, facts.ore_nominal + pairs * 2 * facts.drill_rate)
    furnaces = 2 * (facts.per_side + slots) * facts.furnace_each if slots else facts.furnace_nominal
    return ore, furnaces, min(_capacity(ore, furnaces, facts.belt_cap), SMELTING_RATE_MAX)


def _span(first: int, last: int) -> str:
    return f"{first}" if first == last else f"{first}-{last}"


def _package(facts: PlateFacts, pairs: int, slots: int, capacity: float) -> dict[str, Any] | None:
    """The stages of one package in build order (existing jobs), or None when a stage cannot be expressed."""
    ore_after, furnaces_after, after = _after(facts, pairs, slots)
    free_pairs = max(0, facts.pair_slots - facts.pairs_built)
    reserved, module = min(pairs, free_pairs), max(0, pairs - free_pairs)
    stages: list[dict[str, Any]] = []
    cost: Counter = Counter()
    entities = 0
    if reserved:
        stages.append(
            {
                "stage": "ore",
                "capability": MINING,
                "parameters": {
                    "resource": facts.resource,
                    "rate_per_minute": round(facts.ore_nominal + reserved * 2 * facts.drill_rate, 1),
                },
                "unit": f"drill pair {_span(facts.pairs_built + 1, facts.pairs_built + reserved)}/{facts.pair_slots} "
                "in the bank's reserved slots",
            }
        )
        cost.update({k: v * reserved for k, v in facts.ore_step_cost.items()})
        entities += facts.ore_step_entities * reserved
    if module:
        rows = f"; patch_grid {facts.patch_rows} rows" if facts.patch_rows else ""
        stages.append(
            {
                "stage": "ore",
                "capability": MINING,
                "parameters": {"resource": facts.resource, "rate_per_minute": round(ore_after, 1)},
                "unit": f"connected module of {2 * module} drills beside the full bank "
                f"(another 2-drill module per pair, outposts.prepare_connected_bank{rows})",
            }
        )
        module_cost = facts.module_step_cost if facts.module_step_cost is not None else facts.ore_step_cost
        module_entities = (
            facts.module_step_entities if facts.module_step_entities is not None else facts.ore_step_entities
        )
        cost.update({k: v * module for k, v in module_cost.items()})
        entities += module_entities * module
    if slots:
        # smelting_capacity refuses rate > connected ore: the mining stages run first (depends_on).
        rate = round(min(furnaces_after, ore_after, SMELTING_RATE_MAX), 1)
        if math.ceil(rate / facts.furnace_each / 2 - 1e-9) != facts.per_side + slots:
            return None
        stages.append(
            {
                "stage": "furnaces",
                "capability": SMELTING,
                "parameters": {"resource": facts.resource, "rate_per_minute": rate},
                "unit": f"furnace slot {_span(facts.per_side + 1, facts.per_side + slots)}/{facts.capacity_slots} "
                "in both rows",
            }
        )
        cost.update({k: v * slots for k, v in facts.furnace_step_cost.items()})
        entities += facts.furnace_step_entities * slots
    return {
        "pairs": pairs,
        "furnace_slots": slots,
        "ore_after": round(ore_after, 2),
        "furnaces_after": round(furnaces_after, 2),
        "capacity_after": round(after, 2),
        "gain": after - capacity,
        "stages": stages,
        "cost": dict(cost),
        "entities": entities,
        "build": len(stages) * TRIP_SECONDS + entities * SECONDS_PER_ENTITY,
    }


def packages(facts: PlateFacts, capacity: float, stage: str, target: float = math.inf) -> list[dict[str, Any]]:
    """Efficient packages along drills -> ore belt -> furnaces -> plate belt, smallest first.

    ``k`` drill pairs (free reserved pairs, then one connected module of up to
    MODULE_PAIRS_MAX pairs) and ``f`` furnace slots per row (free reserved
    slots). A package must add plates, contain a unit of the bottleneck stage
    and be efficient: dropping one pair or one slot lowers its capacity.
    Demand cap: no package above the smallest capacity that reaches ``target``
    (demand x COVER_MARGIN); when none reaches it, every package stays.
    """
    free_pairs = max(0, facts.pair_slots - facts.pairs_built)
    free_slots = max(0, facts.capacity_slots - facts.per_side)
    max_pairs = free_pairs + MODULE_PAIRS_MAX if facts.drill_rate > 0 else 0
    max_slots = free_slots if facts.furnace_each > 0 else 0
    found = []
    for k in range(max_pairs + 1):
        for f in range(max_slots + 1):
            if (k, f) == (0, 0) or (stage == "ore" and not k) or (stage == "furnaces" and not f):
                continue
            after = _after(facts, k, f)[2]
            if after <= capacity + EPSILON:
                continue
            if (k and _after(facts, k - 1, f)[2] >= after - EPSILON) or (
                f and _after(facts, k, f - 1)[2] >= after - EPSILON
            ):
                continue
            package = _package(facts, k, f, capacity)
            if package is not None:
                found.append(package)
    covering = [p["capacity_after"] for p in found if p["capacity_after"] >= target - EPSILON]
    if covering:
        cap = min(covering)
        found = [p for p in found if p["capacity_after"] <= cap + EPSILON]
    return sorted(found, key=lambda p: (p["entities"], p["capacity_after"], p["pairs"]))


def _evaluate(facts: PlateFacts, package: dict[str, Any], capacity: float, bill: float, taps: float) -> None:
    """Adds ``fund``, ``prep``, ``expand``, ``payback``, ``usable`` (seconds) to ``package`` (module docstring)."""
    cost = package["cost"]
    own = max(0.0, cost.get(facts.plate, 0.0))
    other = _fund(cost, facts)
    stock = facts.stocks.get(facts.plate, (0.0, None))[0]
    # A slack growth job is refused plates a critical job claims: its own plates come after the claim.
    own_wait = fund_seconds(max(0.0, facts.claimed) + own, stock, capacity) if own > 0 else 0.0
    fund = max(other, own_wait)
    prep = fund + package["build"]
    if math.isinf(prep):
        expand = math.inf
    else:
        net = max(0.0, capacity - taps)
        remaining = bill + own - net * prep / 60.0
        if remaining <= 0 and capacity >= taps - EPSILON:  # the bill is served before the package lands
            expand = _serve_seconds(bill + own, capacity, taps)
        else:
            expand = prep + _serve_seconds(max(0.0, remaining), package["capacity_after"], taps)
    gain = package["gain"]
    package.update(
        fund=fund,
        prep=prep,
        expand=expand,
        payback=prep + 60.0 * own / gain if gain > EPSILON else math.inf,
        # The known demand uses the package long enough to repay its own plates.
        usable=math.isfinite(expand) and gain * (expand - prep) / 60.0 >= own - EPSILON,
    )


def _seconds(value: float) -> int | None:
    return None if math.isinf(value) else round(value)


def _package_row(package: dict[str, Any], wait: float) -> dict[str, Any]:
    return {
        "pairs": package["pairs"],
        "furnace_slots": package["furnace_slots"],
        "capacity_after": package["capacity_after"],
        "gain_per_minute": round(package["gain"], 2),
        "entities": package["entities"],
        "expand_seconds": _seconds(package["expand"]),
        "saving_seconds": None
        if math.isinf(package["expand"]) or math.isinf(wait)
        else round(wait - package["expand"]),
        "usable": package["usable"],
    }


def _describe(package: dict[str, Any]) -> str:
    parts = []
    if package["pairs"]:
        parts.append(f"+{package['pairs']} drill pair{'s' if package['pairs'] > 1 else ''}")
    if package["furnace_slots"]:
        n = 2 * package["furnace_slots"]
        parts.append(f"+{n} furnace{'s' if n > 1 else ''}")
    return " ".join(parts) + f" -> {package['capacity_after']:g}/min"


def _choose(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Smallest usable package first; a larger one wins only with T1 at least MIN_SAVING_SECONDS lower."""
    best = None
    for package in candidates:
        if not package["usable"]:
            continue
        if best is None or package["expand"] < best["expand"] - MIN_SAVING_SECONDS:
            best = package
    return best


def decide(facts: PlateFacts) -> dict[str, Any]:
    """The growth decision for one plate (module rule), JSON-safe; ``grow`` True only for a package that pays."""
    horizon = DEMAND_HORIZON_MINUTES
    bill, taps = max(0.0, facts.bill), max(0.0, facts.tap_rate)
    work = bill + taps * horizon
    demand = work / horizon
    capacity = _capacity(facts.ore_nominal, facts.furnace_nominal, facts.belt_cap)
    row: dict[str, Any] = {
        "plate": facts.plate,
        "resource": facts.resource,
        "grow": False,
        "demand": {
            "taps_per_minute": round(taps, 2),
            "bill_plates": round(bill, 1),
            "horizon_minutes": horizon,
            "per_minute": round(demand, 2),
            "claimed_by_critical_jobs": round(facts.claimed, 1),
        },
        "supply": {
            # force-wide counter; the rule compares against the district (connected) capacity only
            "measured_per_minute": None if facts.measured is None else round(facts.measured, 2),
            "measured_connected_per_minute": (
                None if facts.connected_measured is None else round(facts.connected_measured, 2)
            ),
            "measured_off_bus_per_minute": None if facts.off_bus_measured is None else round(facts.off_bus_measured, 2),
            "starter_cells": facts.starter_phase or None,
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
    if stage == "furnaces" and facts.per_side >= facts.capacity_slots:
        return dict(
            row,
            reason="paired_bank_full_next_growth_is_another_bank: every reserved furnace slot is built; "
            "next unit: a second paired bank of the same plate in the district zone's free half, "
            "fed by a splitter off the ore trunk, output merged onto the same bus lane (not automated)",
            next_unit="another_bank",
        )
    target = demand * COVER_MARGIN
    candidates = packages(facts, capacity, stage, target)
    if not candidates:
        return dict(row, reason=f"no package adds plates: {why}")
    wait = _serve_seconds(bill, capacity, taps)
    for package in candidates:
        _evaluate(facts, package, capacity, bill, taps)
    chosen = _choose(candidates)
    taps_short = math.isinf(wait) and capacity < taps - EPSILON
    if chosen is None and taps_short:
        # The taps alone exceed every package: the largest closes most of the gap (none is above the taps).
        fundable = [p for p in candidates if math.isfinite(p["prep"])]
        chosen = max(fundable, key=lambda p: (p["capacity_after"], -p["entities"])) if fundable else None
    shown = chosen or min(candidates, key=lambda p: (p["expand"], p["entities"]))
    expand = shown["expand"]
    first = shown["stages"][0]
    ranked = sorted(candidates, key=lambda p: (p["expand"], p["entities"]))[:RECEIPT_ALTERNATIVES]
    row.update(
        step={
            "stage": first["stage"],
            "capability": first["capability"],
            "parameters": first["parameters"],
            "unit": first["unit"],
            "valued_with": [s["unit"] for s in shown["stages"][1:]],
            "gain_per_minute": round(shown["gain"], 2),
            "cost": {k: round(v, 1) for k, v in sorted(shown["cost"].items())},
        },
        package=dict(
            _package_row(shown, wait),
            stages=[dict(s) for s in shown["stages"]],
            summary=_describe(shown),
            demand_cap_per_minute=round(target, 2),
        ),
        alternatives=[_package_row(p, wait) for p in ranked],
        times={
            "wait_seconds": _seconds(wait),
            "expand_seconds": _seconds(expand),
            "fund_seconds": _seconds(shown["fund"]),
            "build_seconds": round(shown["build"]),
            "saving_seconds": None if math.isinf(expand) or math.isinf(wait) else round(wait - expand),
            "payback_seconds": _seconds(shown["payback"]),
        },
    )
    if math.isinf(shown["prep"]):
        return dict(row, reason=f"package inputs cannot be funded ({', '.join(sorted(shown['cost']))})")
    summary = _describe(shown)
    if chosen is None:
        if not shown["usable"]:
            return dict(
                row,
                reason=f"waiting is as fast: wait {wait:.0f}s; best package {summary} lands after the known demand "
                f"is served or never repays its plates (expand {expand:.0f}s, prep {shown['prep']:.0f}s)",
            )
        return dict(row, reason=f"waiting is as fast: wait {wait:.0f}s, best package {summary} expand {expand:.0f}s")
    clearly = wait - expand >= MIN_SAVING_SECONDS and expand <= MAX_RATIO * wait
    bounded = wait > chosen["payback"] and expand < wait  # the wait outlasts the package's own payback
    if not (clearly or bounded or taps_short):
        return dict(
            row,
            reason=f"waiting is as fast: wait {wait:.0f}s, best package {summary} expand {expand:.0f}s "
            f"(saves {wait - expand:.0f}s < {MIN_SAVING_SECONDS:g}s or > {MAX_RATIO:g} of the wait; "
            f"wait {wait:.0f}s <= payback {chosen['payback']:.0f}s)",
        )
    row.update(
        grow=True,
        capability=first["capability"],
        parameters=first["parameters"],
        stages=[{k: s[k] for k in ("capability", "parameters", "unit")} for s in chosen["stages"]],
        reason=(
            f"{why}; demand {demand:.1f}/min > capacity {capacity:g}/min: package {summary} "
            f"saves {wait - expand:.0f}s (wait {wait:.0f}s, expand {expand:.0f}s"
            + ("" if clearly else f", wait > payback {chosen['payback']:.0f}s")
            + ")"
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


def _measured_split(measured, furnace_status, each: float, nominal: float) -> tuple[float | None, float | None]:
    """``(connected, off_bus)`` plates/min: the force-wide counter split into district furnaces and the rest.

    The production counter counts every furnace of the force, also starter cells
    whose plates never reach the bus (eval-4177156614: 148.9/min measured under a
    112.5/min district). Connected = working district furnaces x nominal each
    (all at nominal when none is observed), never above the measurement.
    """
    if measured is None:
        return None, None
    observed = [s for s in furnace_status if s is not None]
    connected = each * sum(s == "working" for s in observed) if observed else nominal
    connected = min(max(0.0, measured), connected)
    return connected, max(0.0, measured - connected)


def critical_plate_claims(catalog, jobs, observation) -> dict[str, float]:
    """Plates critical jobs reserve now (``critical_path.allocation``; a claim without units is the whole bill: inf)."""
    from .critical_path import allocation
    from .jobs import CAPABILITIES

    keyed = {str(job.get("key") or i): job for i, job in enumerate(jobs) if isinstance(job, Mapping)}
    claimed = allocation(keyed, observation, CAPABILITIES, catalog).claimed
    rows = {}
    for plate in PLATES:
        row = claimed.get(plate)
        if isinstance(row, Mapping):
            units = finite_number(row.get("units"))
            rows[plate] = math.inf if units is None else max(0.0, units)
    return rows


def plate_facts(
    catalog, plate, bank, bound, mining_banks, stations, jobs, observation, *, claimed=0.0, starter_phase=""
) -> PlateFacts:
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
    measured = finite_number(
        (
            ((observation.get("production_rate_measurement") or {}).get("rates_per_game_minute") or {}).get(plate) or {}
        ).get("produced")
    )
    each = nominal / len(furnaces) if furnaces else 0.0
    connected, off_bus = _measured_split(measured, furnace_status, each, nominal)
    bill = _bill(plate, observation)
    module_items = dict(MINING_OUTPOST_BUILDS)
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
        bill=bill,
        furnace_nominal=nominal,
        furnace_each=each,
        per_side=per_side,
        capacity_slots=int(bank.get("capacity_slots", per_side)),
        ore_nominal=float(bound["connected_nominal_ore_supply"]),
        drill_rate=drill_rate,
        pairs_built=int(pairs),
        pair_slots=int(slots),
        belt_cap=None if belt is None else 2 * belt,
        measured=measured,
        connected_measured=connected,
        off_bus_measured=off_bus,
        starter_phase=starter_phase,
        claimed=min(bill, claimed),
        furnaces=sum(s is not None for s in furnace_status),
        furnaces_idle_for_ore=sum(s in IDLE_FOR_ORE for s in furnace_status),
        furnaces_output_full=sum(s in OUTPUT_FULL for s in furnace_status),
        drills=sum(statuses.get(_key(e)) is not None for e in drills),
        drills_backed=sum(statuses.get(_key(e)) in DRILL_BACKED for e in drills),
        ore_step_cost=raw_cost(catalog, pair_items),
        ore_step_entities=sum(pair_items.values()),
        furnace_step_cost=raw_cost(catalog, slot_items),
        furnace_step_entities=sum(slot_items.values()),
        module_step_cost=raw_cost(catalog, module_items),
        module_step_entities=sum(module_items.values()),
        patch_rows=len((first.get("patch_grid") or {}).get("rows") or ()),
        stocks=_stocks(observation),
        settling=_settling(resource, jobs, observation.get("tick")),
    )


def _starter_phase(retirement: Mapping, resource: str, jobs: Iterable[Mapping]) -> str:
    """``retired`` / ``retiring`` / saved phase / "" for the resource's starter cells."""
    for job in jobs:
        parameters = job.get("parameters") if isinstance(job.get("parameters"), Mapping) else {}
        if job.get("capability") == "starter_retirement" and parameters.get("resource") == resource:
            if job.get("status") == "succeeded":
                return "retired"
    row = (retirement.get("resources") or {}).get(resource)
    return str(row.get("phase") or "") if isinstance(row, Mapping) else ""


def _bus_funding(facts: Mapping[str, PlateFacts]) -> dict[str, PlateFacts]:
    """Plate inputs of a package are funded from the plate that reaches the bus.

    Production of a plate = connected district output, plus the off-bus starter
    output only while those cells are not retiring (afterwards they are mined,
    their stock is collected once and already counts in the bill's ``available``).
    """
    rates = {}
    for plate, row in facts.items():
        if row.connected_measured is None:
            continue
        off_bus = 0.0 if row.starter_phase in RETIRING_PHASES else (row.off_bus_measured or 0.0)
        rates[plate] = row.connected_measured + off_bus
    result = {}
    for plate, row in facts.items():
        stocks = dict(row.stocks)
        for item, rate in rates.items():
            if item in stocks:
                stocks[item] = (stocks[item][0], rate)
        result[plate] = replace(row, stocks=stocks)
    return result


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
    retirement = read("starter-retirement.json")
    try:
        claims = critical_plate_claims(catalog, jobs, observation)
    except Exception:  # a claim estimate never breaks the receipt: no claim known
        claims = {}
    facts_by_plate: dict[str, PlateFacts] = {}
    order: list[str | dict] = []
    for plate, resource in PLATES.items():
        bank = next((b for b in district["banks"] if b.get("resource") == resource), None)
        bound = bound_rows.get(resource)
        if bank is None or "growth_slots" not in bank or not bound:
            continue
        banks = [
            b for b in mining.get("banks", []) if b.get("ready") and (b.get("plan") or {}).get("resource") == resource
        ]
        if not banks:
            order.append({"plate": plate, "resource": resource, "grow": False, "reason": "no ready mining bank"})
            continue
        facts_by_plate[plate] = plate_facts(
            catalog,
            plate,
            bank,
            bound,
            banks,
            stations,
            jobs,
            observation,
            claimed=claims.get(plate, 0.0),
            starter_phase=_starter_phase(retirement, resource, jobs),
        )
        order.append(plate)
    funded = _bus_funding(facts_by_plate)
    rows = [decide(funded[entry]) if isinstance(entry, str) else entry for entry in order]
    return {
        "plates": rows,
        "scope": "nominal stage capacities, declared taps and the roadmap bill; module throughput_growth",
    }


# ----- autopilot nodes ---------------------------------------------------------------------
def throughput_growth_nodes(jobs, queue_nodes, observation, capabilities, *, critical: bool) -> list[dict]:
    """The chosen package of each growing plate as chained nodes, ``critical`` selecting the critical-path class.

    The receipt (``growth_receipt``) holds the rule; here only: each stage's
    capability exists and its ``requires`` succeeded, nothing of the resource's
    growth capabilities is queued or open (one package per plate in flight), and
    no stage of the package has stalled before. Stage ``i + 1`` depends on stage
    ``i`` (smelting_capacity refuses a rate above the connected ore).
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
        stages = row.get("stages")
        if not isinstance(stages, list) or not stages:
            stages = [{"capability": row.get("capability"), "parameters": row.get("parameters")}]
        if not all(
            isinstance(stage, Mapping)
            and stage.get("capability") in GROWTH_CAPABILITIES
            and stage.get("capability") in capabilities
            and isinstance(stage.get("parameters"), Mapping)
            for stage in stages
        ):
            continue
        if "smelting_district" not in succeeded or not all(
            set(capabilities[stage["capability"]].get("requires", [])) <= succeeded for stage in stages
        ):
            continue
        if bottleneck_blocks_critical_path(row.get("plate"), jobs, observation) != critical:
            continue
        resource = stages[0]["parameters"].get("resource")

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
            j.get("capability") == stage["capability"]
            and j.get("parameters") == stage["parameters"]
            and j.get("status") in ("blocked", "failed")
            for j in jobs
            for stage in stages
        ):
            continue
        chain: list[dict] = []
        for stage in stages:
            capability, parameters = stage["capability"], stage["parameters"]
            node_id = f"{NODE_PREFIX}{resource}_{capability}_{int(parameters.get('rate_per_minute', 0))}"
            if node_id in queued_ids or any(n["id"] == node_id for n in chain):
                break  # an earlier node of that id exists: the chain ends before it (re-decided after landing)
            chain.append(
                {
                    "id": node_id,
                    "capability": capability,
                    "parameters": dict(parameters),
                    "priority": CRITICAL_PRIORITY if critical else SLACK_PRIORITY,
                    "depends_on": [chain[-1]["id"]] if chain else [],
                    "reason": (
                        f"{'critical' if critical else 'slack'} plate growth"
                        + (f" (stage {len(chain) + 1}/{len(stages)})" if len(stages) > 1 else "")
                        + f": {row.get('reason', '')}"
                    )[:300],
                }
            )
        nodes.extend(chain)
    return nodes
