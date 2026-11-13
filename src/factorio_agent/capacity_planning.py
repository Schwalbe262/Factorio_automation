"""Need-driven starter capacity decisions from measured supply and upcoming demand.

Pure functions, no IO. Inputs are the observe receipt's
``resource_supply_options`` (item -> {action, reason, evidence}) and a
``{item: amount}`` mapping of raw items the upcoming roadmap will need beyond
the current construction bill (``progression.lookahead_demand``).

Decision rule per raw item (all times in seconds, rates in items/minute):

- ``required = max(0, shortfall) + max(0, demand[item] - spare stock)`` (items),
  where spare stock is what is on hand beyond the current bill.
- Waiting: ``T(0) = 60 * required / net`` (infinite when ``net <= 0``).
- Expanding by ``k = 1..max_step`` cells: the cells cost
  ``k * raw_cost_per_cell``. The capacity job runs at top priority, so it gets
  newly produced items first: the item's own share is funded from current
  stock (``available`` or ``collectable``) and then from the gross
  ``produced_per_minute``. Every other raw item in the cell cost must be in
  stock or being produced, or be hand-gathered (wood:
  ``hand_gathered_seconds`` per item); otherwise the option is impossible.
  The longest of their fund times is added. Then ``k * build_seconds`` to build, after
  which the item's net rate is ``net + k * cell_rate`` (coal: minus a new
  coal drill's own fuel, and minus the fuel new cells of other items burn).
  ``T(k) = fund + build + 60 * (required + own_cell_cost) / new_net``.
- Expand to the fastest ``k`` only when it really beats waiting:
  ``T(0) - T(k) >= min_saving_seconds`` and ``T(k) <= T(0) * max_ratio``.
- Fuel rule (coal): sustained deficit ``produced < consumed * fuel_margin``,
  where ``consumed`` includes the fuel planned cells of other items will
  burn, adds at least one coal cell even with no bill.
- Unmeasured production or an unknown ``raw_cost_per_cell`` means wait.
- Decisions are ordered by ``T(0)`` descending (worst bottleneck first). A
  later decision is dropped when its cell cost uses an item an earlier
  decision is expanding, so scarce items go to the worst bottleneck first.

Plate cells (iron, copper) follow ``PLATE_CELL_POLICY`` (one switch, below):

- ``"demand"`` (default since 2026-11-14): no fixed count. The demand is the bill the
  cells serve until they retire (``progression.retirement_demand``: every on-path step
  through the main bus); waiting and expanding both end no earlier than the research
  gate (``gate_seconds``: the bill cannot finish before it, however many plates there
  are); an expansion is taken when it saves ``min_saving_seconds`` and each new cell
  pays back (its plates before the bill is done exceed its own plate cost); coal cells
  must be able to fuel it (``fuel_cells_needed <= max_cells``, else the step shrinks).
  The capability's absolute limit (``max_cells``) still holds.
- ``"cap"``: the earlier rule, at most ``max_plate_cells`` (6) and the
  ``max_ratio`` test, the electric-mining unlock closing plate cells
  (``mining.closed_by_unlock``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# Item produced by a starter cell -> the ``resource`` parameter of the
# ``starter_capacity`` capability ("iron-ore|copper-ore|coal|stone").
RESOURCE_FOR_ITEM = {
    "iron-plate": "iron-ore",
    "copper-plate": "copper-ore",
    "coal": "coal",
    "stone": "stone",
}
# Items whose starter cells are burner drills feeding stone furnaces.
SMELTED_ITEMS = frozenset({"iron-plate", "copper-plate"})
FUEL_ITEM = "coal"

# THE SWITCH for plate starter cells (orchestrator decision 2026-11-14, owner guidance "when
# plate supply is short the plant must grow by itself instead of waiting"; it reverses the
# earlier explicit "6 cells" cap). "demand": grow by payback against the retirement bill (module
# docstring). "cap": the old fixed cap of max_plate_cells (6) and the unlock closing.
# Turning it back is this one line: PLATE_CELL_POLICY = PLATE_CELLS_CAP. Read by
# mining.closed_by_unlock and progression.capacity_nodes too. docs/v2/PLANNERS.md "Starter capacity".
PLATE_CELLS_DEMAND = "demand"
PLATE_CELLS_CAP = "cap"
PLATE_CELL_POLICY = PLATE_CELLS_DEMAND
# The starter_capacity capability's absolute target limit (mining.validate_parameters).
MAX_STARTER_CELLS = 12


def plate_policy(config: "CapacityConfig | None" = None) -> str:
    """The plate cell policy: ``config.plate_cell_policy`` when set, else ``PLATE_CELL_POLICY``."""
    chosen = getattr(config, "plate_cell_policy", None) if config is not None else None
    return chosen if chosen in (PLATE_CELLS_DEMAND, PLATE_CELLS_CAP) else PLATE_CELL_POLICY


def demand_driven(config: "CapacityConfig | None" = None) -> bool:
    return plate_policy(config) == PLATE_CELLS_DEMAND


@dataclass(frozen=True)
class CapacityConfig:
    """Tunable limits; units are stated per field."""

    # Most cells added by one decision, and the capability's absolute limit.
    max_step: int = 2
    # The capability's absolute limit per resource (MAX_STARTER_CELLS; also the coal cap, below
    # which coal follows the burners' need). Was 8; the demand policy's payback rule found 10
    # iron cells on the aud-1114-0436 bill (docs/v2/PLANNERS.md "Starter capacity").
    max_cells: int = MAX_STARTER_CELLS
    # None: the module switch PLATE_CELL_POLICY; "demand" or "cap" overrides it (tests).
    plate_cell_policy: str | None = None
    # Plate and stone cells: at most max_plate_cells; how many below that is
    # the time comparison against the lookahead demand. A fixed cap of 4
    # starved the early game (seed 283895142: smelting finished at 48.8 min,
    # the next steps needed 1000+ iron while 4 iron cells made 60/min). The
    # fuel new cells burn is covered by the coal rule (extra_fuel_burn) and
    # hand-fuelling by FUEL_BATCH; an earlier 1 -> 8 iron build-up predates both.
    # 6, not 8 (user rule): the cells are demolished once the smelting district
    # and the bus exist, and every cell is hand-fuelled and hand-emptied, so
    # each one adds hauling (16 served cells were 43% of all actions).
    max_plate_cells: int = 6
    # Items/minute of one starter cell when the receipt has no measurement:
    # a burner mining drill mines 0.25 ore/s = 15/min.
    default_cell_rate: float = 15.0
    # Coal is short when produced < consumed * fuel_margin.
    fuel_margin: float = 1.2
    # Coal/minute one new smelting cell burns: burner drill 150 kW + stone
    # furnace 90 kW = 240 kJ/s = 14.4 MJ/min; coal holds 4 MJ -> 3.6/min.
    fuel_per_cell_per_minute: float = 3.6
    # Coal/minute of a drill-only cell (stone, or a coal drill's own fuel):
    # 150 kJ/s * 60 s = 9 MJ/min / 4 MJ = 2.25/min.
    fuel_per_drill_per_minute: float = 2.25
    # Seconds to place one cell once its items are on hand.
    build_seconds_per_cell: float = 15.0
    # Expansion must save at least this many seconds and be this much faster.
    min_saving_seconds: float = 60.0
    max_ratio: float = 0.85
    # Seconds per item of a raw input no starter cell produces but the actor
    # gathers by hand (a tree gives 4 wood). The wooden chest of every coal
    # and stone cell costs 2 wood, and ``resource_supply_options`` has no wood
    # row, so without this every coal option was infinite and coal never
    # expanded (live, seed 283895142: one coal cell, power waited 6 min).
    hand_gathered_seconds: Mapping[str, float] = field(default_factory=lambda: {"wood": 2.0})


@dataclass(frozen=True)
class CapacityDecision:
    """Expand ``resource`` starter cells from ``current_cells`` to ``target_cells``."""

    item: str
    resource: str
    current_cells: int
    target_cells: int
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


def finite_number(value) -> float | None:
    """A finite int/float, else None (bools are not numbers here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _evidence(supply: Mapping, item: str) -> Mapping | None:
    row = supply.get(item) if isinstance(supply, Mapping) else None
    evidence = row.get("evidence") if isinstance(row, Mapping) else None
    return evidence if isinstance(evidence, Mapping) else None


def stock_on_hand(evidence: Mapping) -> float:
    """Items on hand now: ``available`` (held + collectable) or ``collectable``."""
    for key in ("available", "collectable"):
        value = finite_number(evidence.get(key))
        if value is not None:
            return max(0.0, value)
    return 0.0


def _cost_per_cell(evidence: Mapping) -> dict[str, float]:
    raw = evidence.get("raw_cost_per_cell")
    if not isinstance(raw, Mapping):
        return {}
    cost = {}
    for name, amount in raw.items():
        value = finite_number(amount)
        if value is not None and value > 0:
            cost[name] = value
    return cost


def fund_seconds(need: float, stock: float, produced: float | None) -> float:
    """Seconds until ``need`` items are on hand from stock, then gross production."""
    missing = need - stock
    if missing <= 0:
        return 0.0
    if produced is None or produced <= 0:
        return math.inf
    return 60.0 * missing / produced


def _seconds(amount: float, rate: float) -> float:
    if amount <= 0:
        return 0.0
    return 60.0 * amount / rate if rate > 0 else math.inf


def _fmt(seconds: float) -> str:
    return "inf" if math.isinf(seconds) else f"{seconds:.0f}s"


@dataclass(frozen=True)
class _Facts:
    item: str
    required: float
    shortfall: float
    demand: float
    produced: float | None
    consumed: float | None
    net: float
    cell_rate: float
    current: int
    stock: float
    cost: dict[str, float]
    build_seconds: float


def _facts(item: str, evidence: Mapping, demand: Mapping, config: CapacityConfig) -> _Facts:
    shortfall = max(0.0, finite_number(evidence.get("shortfall")) or 0.0)
    extra = max(0.0, finite_number(demand.get(item)) or 0.0) if isinstance(demand, Mapping) else 0.0
    # Stock beyond the current bill (plates sitting in furnaces and chests)
    # already covers part of the upcoming demand; do not expand to make it twice.
    available = finite_number(evidence.get("available"))
    billed = finite_number(evidence.get("required"))
    if available is not None and billed is not None:
        extra = max(0.0, extra - max(0.0, available - billed))
    produced = finite_number(evidence.get("produced_per_minute"))
    productive = finite_number(evidence.get("productive_cells"))
    cell_rate = finite_number(evidence.get("cell_capacity_per_minute"))
    if cell_rate is None or cell_rate <= 0:
        if produced is not None and produced > 0 and productive:
            cell_rate = produced / productive
        else:
            cell_rate = config.default_cell_rate
    current = evidence.get("installed_live_cells")
    if isinstance(current, bool) or not isinstance(current, int):
        current = int(productive) if productive else 0
    build = finite_number(evidence.get("build_seconds"))
    return _Facts(
        item=item,
        required=shortfall + extra,
        shortfall=shortfall,
        demand=extra,
        produced=produced,
        consumed=finite_number(evidence.get("consumed_per_minute")),
        net=finite_number(evidence.get("net_per_minute")) or 0.0,
        cell_rate=cell_rate,
        current=max(0, current),
        stock=stock_on_hand(evidence),
        cost=_cost_per_cell(evidence),
        build_seconds=build if build is not None and build >= 0 else config.build_seconds_per_cell,
    )


def _option_seconds(facts: _Facts, k: int, supply: Mapping, net: float, config: CapacityConfig) -> dict:
    """Time until ``required`` is on hand after adding ``k`` cells (see module rules)."""
    own_cost = k * facts.cost.get(facts.item, 0.0)
    fund = fund_seconds(own_cost, facts.stock, facts.produced)
    cross = 0.0
    for name, per_cell in facts.cost.items():
        if name == facts.item:
            continue
        other = _evidence(supply, name)
        if other is None:
            gather = finite_number(config.hand_gathered_seconds.get(name))
            if gather is None or gather < 0:
                cross = math.inf
                break
            cross = max(cross, k * per_cell * gather)
            continue
        cross = max(
            cross, fund_seconds(k * per_cell, stock_on_hand(other), finite_number(other.get("produced_per_minute")))
        )
    gain = facts.cell_rate
    if facts.item == FUEL_ITEM:
        gain -= config.fuel_per_drill_per_minute
    new_net = net + k * gain
    build = k * facts.build_seconds
    total = fund + cross + build + _seconds(facts.required + own_cost, new_net)
    if facts.required + own_cost > 0 and new_net <= 0:
        total = math.inf
    return {
        "extra_cells": k,
        "own_cell_cost": own_cost,
        "fund_seconds": fund + cross,
        "build_seconds": build,
        "new_net_per_minute": new_net,
        "seconds": total,
    }


def _payback(facts: _Facts, option: dict, config: CapacityConfig) -> dict:
    """Plates one new cell yields before the bill is done (its retirement) against its own plate cost.

    online = fund + build seconds; done = the option's seconds (the bill's completion, after
    which the district and bus retire the cells, starter_retirement); yield =
    cell_rate x (done - online) / 60. The cell pays when the yield exceeds its own cost.
    """
    online = option["fund_seconds"] + option["build_seconds"]
    done = option["seconds"]
    cost = facts.cost.get(facts.item, 0.0)
    plates = facts.cell_rate * max(0.0, done - online) / 60.0 if math.isfinite(done) else 0.0
    return {
        "online_seconds": round(online, 1),
        "retire_seconds": round(done, 1) if math.isfinite(done) else None,
        "yield_per_cell": round(plates, 1),
        "cost_per_cell": cost,
        "pays": math.isfinite(done) and plates > cost,
    }


def _assess(
    facts: _Facts,
    supply: Mapping,
    config: CapacityConfig,
    extra_fuel_burn: float = 0.0,
    fuel_cells_needed: int | None = None,
    gate_seconds: float | None = None,
) -> CapacityDecision | None:
    """One decision for ``facts.item``, or None when waiting is as good.

    An unmeasured production rate or an unknown cell cost never yields an
    expansion: a missing cost must not turn a shortage into a free cell.
    """
    if not facts.cost or facts.produced is None:
        return None
    # Cells already installed but not yet producing at their rate (just built,
    # still being fuelled) are counted at nominal output. Without this the
    # measured rate lags every expansion and the planner stacks a second one
    # on top (seed 2099054105: coal 1 -> 3 -> 5 within two minutes).
    pending = max(0.0, facts.current * facts.cell_rate - facts.produced)
    if facts.item == FUEL_ITEM:
        # Coal is burned in the background, so what accumulates is the net rate.
        net = facts.net + pending - extra_fuel_burn
    else:
        # Plates and stone are consumed by the very construction this bill
        # describes. Subtracting that consumption counted the demand twice,
        # made every wait look endless and drove the cells to the cap
        # (seed 4176033748: iron 1 -> 7 cells in 8 minutes at "net 1.3/min").
        net = facts.produced + pending
    wait = _seconds(facts.required, net)
    by_demand = facts.item in SMELTED_ITEMS and demand_driven(config)
    gate = max(0.0, finite_number(gate_seconds) or 0.0) if by_demand else 0.0
    if facts.item == FUEL_ITEM:
        cap = config.max_cells
    elif by_demand:
        cap = config.max_cells  # no fixed plate count: payback below decides
    else:
        cap = min(config.max_cells, config.max_plate_cells)
    if facts.item == FUEL_ITEM and fuel_cells_needed is not None:
        # Coal never gets more cells than the burners need (one coal cell
        # fuels about 3.5 smelting cells); measured rates do not raise this.
        cap = min(cap, fuel_cells_needed)
    room = min(config.max_step, cap - facts.current)
    if room < 1:
        return None
    options = [_option_seconds(facts, k, supply, net, config) for k in range(1, room + 1)]
    if by_demand:
        # The bill ends no earlier than its research gate, waiting or not (rule a).
        wait = max(wait, gate)
        for row in options:
            row["payback"] = _payback(facts, row, config)
            row["seconds"] = max(row["seconds"], gate)
    feasible = [row for row in options if math.isfinite(row["seconds"])]
    if by_demand:
        feasible = [row for row in feasible if row["payback"]["pays"]]  # rule b
    best = min(feasible, key=lambda row: (row["seconds"], row["extra_cells"])) if feasible else None
    chosen = 0
    if facts.required > 0 and best is not None:
        saving = wait - best["seconds"]
        # The demand policy drops max_ratio: on a 1500-plate bill +2 cells at 8 saved ~100 s
        # (ratio 0.87) and were refused; payback and the absolute saving decide instead.
        clear = by_demand or best["seconds"] <= wait * config.max_ratio
        if saving >= config.min_saving_seconds and clear:
            chosen = best["extra_cells"]
    fuel_deficit = False
    if facts.item == FUEL_ITEM and fuel_cells_needed is not None:
        fuel_deficit = facts.current < fuel_cells_needed
        if fuel_deficit and chosen == 0 and math.isfinite(options[0]["seconds"]):
            chosen = 1
            best = options[0]
    if chosen == 0:
        return None
    picked = options[chosen - 1]
    saved = wait - picked["seconds"]
    if facts.required > 0:
        priority = wait
    else:
        priority = math.inf if net <= 0 else 0.0
    evidence = {
        "required": facts.required,
        "shortfall": facts.shortfall,
        "lookahead_demand": facts.demand,
        "produced_per_minute": facts.produced,
        "consumed_per_minute": facts.consumed,
        "net_per_minute": net,
        "pending_installed_per_minute": pending,
        "extra_fuel_burn_per_minute": extra_fuel_burn,
        "cell_rate_per_minute": facts.cell_rate,
        "wait_seconds": wait,
        "chosen_extra_cells": chosen,
        "expand_seconds": picked["seconds"],
        "seconds_saved": saved,
        "fuel_deficit": fuel_deficit,
        "raw_cost_per_cell": dict(facts.cost),
        "options": options,
        "priority_seconds": priority,
    }
    if by_demand:
        evidence.update(plate_cell_policy=PLATE_CELLS_DEMAND, gate_seconds=gate, payback=picked["payback"])
    reason = (
        f"{facts.item}: required {facts.required:.0f} (shortfall {facts.shortfall:.0f} + lookahead "
        f"{facts.demand:.0f}), net {net:.1f}/min; wait T(0)={_fmt(wait)} vs +{chosen} cells "
        f"T({chosen})={_fmt(picked['seconds'])}, saves {_fmt(saved)}; "
        f"new net {picked['new_net_per_minute']:.1f}/min"
    )
    if fuel_deficit:
        reason += f"; fuel: {facts.current} coal cells < {fuel_cells_needed} needed by the burner cells"
    if by_demand:
        pay = picked["payback"]
        reason += (
            f"; demand policy: gate {_fmt(gate)}, a cell yields {pay['yield_per_cell']:.0f} > "
            f"{pay['cost_per_cell']:.0f} before the bill is done"
        )
    return CapacityDecision(
        item=facts.item,
        resource=RESOURCE_FOR_ITEM[facts.item],
        current_cells=facts.current,
        target_cells=facts.current + chosen,
        reason=reason,
        evidence=evidence,
    )


def _ordered(decisions: list[CapacityDecision]) -> list[CapacityDecision]:
    """Worst bottleneck first; drop a decision whose cell cost uses an item already being expanded."""
    ranked = sorted(decisions, key=lambda d: (-d.evidence["priority_seconds"], d.item))
    kept: list[CapacityDecision] = []
    for decision in ranked:
        cost = decision.evidence["raw_cost_per_cell"]
        # The few coal pieces that start a new cell do not compete with a coal
        # expansion; without this exception coal went first and iron, the item
        # every job was waiting for, lost a planning round (seed 4176033748).
        if any(earlier.item in cost and earlier.item != FUEL_ITEM for earlier in kept):
            continue
        kept.append(decision)
    return kept


def _fuel_burn(decision: CapacityDecision, config: CapacityConfig) -> float:
    """Coal/minute the new cells of a non-coal decision will burn."""
    extra = decision.target_cells - decision.current_cells
    per_cell = config.fuel_per_cell_per_minute if decision.item in SMELTED_ITEMS else config.fuel_per_drill_per_minute
    return extra * per_cell


def fuel_cells_needed(
    supply: Mapping, decisions: list[CapacityDecision], config: CapacityConfig, hand_fed_coal_per_minute: float = 0.0
) -> int:
    """Coal cells that fuel every burner cell, counted from the cells themselves.

    burn = smelting cells x fuel_per_cell + drill-only cells x fuel_per_drill
    + the hand-fed boiler's coal/min (``power_coal_feed.hand_fed_coal_per_minute``,
    0 once its dedicated feed exists), including the cells the kept decisions
    add; one coal cell nets default_cell_rate - fuel_per_drill. At least one.
    Measured coal rates are not used: they lag each expansion and once drove
    coal to 5 cells while iron had 3 (seed 2099054105).
    """
    targets = {decision.item: decision.target_cells for decision in decisions}
    burn = max(0.0, finite_number(hand_fed_coal_per_minute) or 0.0)
    for item in RESOURCE_FOR_ITEM:
        if item == FUEL_ITEM:
            continue
        evidence = _evidence(supply, item)
        cells = targets.get(item)
        if cells is None:
            if evidence is None:
                continue
            cells = _facts(item, evidence, {}, config).current
        per_cell = config.fuel_per_cell_per_minute if item in SMELTED_ITEMS else config.fuel_per_drill_per_minute
        burn += cells * per_cell
    net_per_coal_cell = config.default_cell_rate - config.fuel_per_drill_per_minute
    return max(1, math.ceil(burn * config.fuel_margin / net_per_coal_cell))


def _fuel_covered(
    supply: Mapping, kept: list[CapacityDecision], config: CapacityConfig, hand_fed: float
) -> list[CapacityDecision]:
    """Demand policy rule c: shrink plate decisions until coal cells (at most ``max_cells``) can fuel them."""
    kept = list(kept)
    while kept and fuel_cells_needed(supply, kept, config, hand_fed) > config.max_cells:
        index = max(
            (i for i, d in enumerate(kept) if d.item in SMELTED_ITEMS and d.target_cells > d.current_cells),
            default=None,
        )
        if index is None:
            break
        decision = kept[index]
        if decision.target_cells - 1 <= decision.current_cells:
            kept.pop(index)
            continue
        kept[index] = CapacityDecision(
            item=decision.item,
            resource=decision.resource,
            current_cells=decision.current_cells,
            target_cells=decision.target_cells - 1,
            reason=decision.reason + "; trimmed: coal cells cannot fuel more",
            evidence={**decision.evidence, "fuel_trimmed": True},
        )
    return kept


def plan_capacity(
    supply,
    demand,
    *,
    config: CapacityConfig = CapacityConfig(),
    hand_fed_coal_per_minute: float = 0.0,
    gate_seconds: float | None = None,
) -> list[CapacityDecision]:
    """Starter capacity expansions that finish the required items sooner than waiting.

    Items without evidence are skipped; partial receipts never raise. Coal is
    assessed last so it can include the fuel of the other decisions' new
    cells (only decisions that survive the ordering rule count) and the
    hand-fed boiler (``hand_fed_coal_per_minute``, counted in the cells needed;
    its measured burn is already in the coal row's consumption). ``gate_seconds``
    (demand policy, plate items only): the research gate of the retirement bill.
    """
    if not isinstance(supply, Mapping):
        return []
    demand = demand if isinstance(demand, Mapping) else {}
    others: list[CapacityDecision] = []
    for item in RESOURCE_FOR_ITEM:
        evidence = _evidence(supply, item)
        if item == FUEL_ITEM or evidence is None:
            continue
        decision = _assess(_facts(item, evidence, demand, config), supply, config, gate_seconds=gate_seconds)
        if decision is not None:
            others.append(decision)
    kept = _ordered(others)
    if demand_driven(config):
        kept = _fuel_covered(supply, kept, config, hand_fed_coal_per_minute)
    coal = _evidence(supply, FUEL_ITEM)
    if coal is None:
        return kept
    burn = sum(_fuel_burn(decision, config) for decision in kept)
    needed = fuel_cells_needed(supply, kept, config, hand_fed_coal_per_minute)
    facts = _facts(FUEL_ITEM, coal, demand, config)
    decision = _assess(facts, supply, config, extra_fuel_burn=burn, fuel_cells_needed=needed)
    if decision is None:
        return kept
    return _ordered(kept + [decision])
