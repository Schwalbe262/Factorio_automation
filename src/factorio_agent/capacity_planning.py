"""Need-driven starter capacity decisions from measured supply and upcoming demand.

Pure functions, no IO. Inputs are the observe receipt's
``resource_supply_options`` (item -> {action, reason, evidence}) and a
``{item: amount}`` mapping of raw items the upcoming roadmap will need beyond
the current construction bill (``progression.lookahead_demand``).

Decision rule per raw item (all times in seconds, rates in items/minute):

- ``required = max(0, shortfall) + demand[item]`` (items).
- Waiting: ``T(0) = 60 * required / net`` (infinite when ``net <= 0``).
- Expanding by ``k = 1..max_step`` cells: the cells cost
  ``k * raw_cost_per_cell``. The capacity job runs at top priority, so it gets
  newly produced items first: the item's own share is funded from current
  stock (``available`` or ``collectable``) and then from the gross
  ``produced_per_minute``. Every other raw item in the cell cost must be in
  stock or being produced (otherwise the option is impossible); the longest
  of their fund times is added. Then ``k * build_seconds`` to build, after
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


@dataclass(frozen=True)
class CapacityConfig:
    """Tunable limits; units are stated per field."""

    # Most cells added by one decision, and the capability's absolute limit.
    max_step: int = 2
    # Every burner cell is hand-fuelled by the one actor, a cost the time
    # comparison does not see. On a live run the planner grew iron 1 -> 8
    # cells in four steps, then fuelling them slowed everything else. The
    # capability allows 8 (max_cells, still the cap for coal, which must fuel
    # the rest); plate and stone cells stop at max_plate_cells because
    # electric mining replaces them soon after.
    max_cells: int = 8
    max_plate_cells: int = 4
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


@dataclass(frozen=True)
class CapacityDecision:
    """Expand ``resource`` starter cells from ``current_cells`` to ``target_cells``."""

    item: str
    resource: str
    current_cells: int
    target_cells: int
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


def _number(value) -> float | None:
    """A finite int/float, else None (bools are not numbers here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _evidence(supply: Mapping, item: str) -> Mapping | None:
    row = supply.get(item) if isinstance(supply, Mapping) else None
    evidence = row.get("evidence") if isinstance(row, Mapping) else None
    return evidence if isinstance(evidence, Mapping) else None


def _stock(evidence: Mapping) -> float:
    """Items on hand now: ``available`` (held + collectable) or ``collectable``."""
    for key in ("available", "collectable"):
        value = _number(evidence.get(key))
        if value is not None:
            return max(0.0, value)
    return 0.0


def _cost_per_cell(evidence: Mapping) -> dict[str, float]:
    raw = evidence.get("raw_cost_per_cell")
    if not isinstance(raw, Mapping):
        return {}
    cost = {}
    for name, amount in raw.items():
        value = _number(amount)
        if value is not None and value > 0:
            cost[name] = value
    return cost


def _fund_seconds(need: float, stock: float, produced: float | None) -> float:
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
    shortfall = max(0.0, _number(evidence.get("shortfall")) or 0.0)
    extra = max(0.0, _number(demand.get(item)) or 0.0) if isinstance(demand, Mapping) else 0.0
    produced = _number(evidence.get("produced_per_minute"))
    productive = _number(evidence.get("productive_cells"))
    cell_rate = _number(evidence.get("cell_capacity_per_minute"))
    if cell_rate is None or cell_rate <= 0:
        if produced is not None and produced > 0 and productive:
            cell_rate = produced / productive
        else:
            cell_rate = config.default_cell_rate
    current = evidence.get("installed_live_cells")
    if isinstance(current, bool) or not isinstance(current, int):
        current = int(productive) if productive else 0
    build = _number(evidence.get("build_seconds"))
    return _Facts(
        item=item,
        required=shortfall + extra,
        shortfall=shortfall,
        demand=extra,
        produced=produced,
        consumed=_number(evidence.get("consumed_per_minute")),
        net=_number(evidence.get("net_per_minute")) or 0.0,
        cell_rate=cell_rate,
        current=max(0, current),
        stock=_stock(evidence),
        cost=_cost_per_cell(evidence),
        build_seconds=build if build is not None and build >= 0 else config.build_seconds_per_cell,
    )


def _option_seconds(facts: _Facts, k: int, supply: Mapping, net: float, config: CapacityConfig) -> dict:
    """Time until ``required`` is on hand after adding ``k`` cells (see module rules)."""
    own_cost = k * facts.cost.get(facts.item, 0.0)
    fund = _fund_seconds(own_cost, facts.stock, facts.produced)
    cross = 0.0
    for name, per_cell in facts.cost.items():
        if name == facts.item:
            continue
        other = _evidence(supply, name)
        if other is None:
            cross = math.inf
            break
        cross = max(cross, _fund_seconds(k * per_cell, _stock(other), _number(other.get("produced_per_minute"))))
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


def _assess(
    facts: _Facts, supply: Mapping, config: CapacityConfig, extra_fuel_burn: float = 0.0
) -> CapacityDecision | None:
    """One decision for ``facts.item``, or None when waiting is as good.

    An unmeasured production rate or an unknown cell cost never yields an
    expansion: a missing cost must not turn a shortage into a free cell.
    """
    if not facts.cost or facts.produced is None:
        return None
    net = facts.net - extra_fuel_burn if facts.item == FUEL_ITEM else facts.net
    wait = _seconds(facts.required, net)
    cap = config.max_cells if facts.item == FUEL_ITEM else min(config.max_cells, config.max_plate_cells)
    room = min(config.max_step, cap - facts.current)
    if room < 1:
        return None
    options = [_option_seconds(facts, k, supply, net, config) for k in range(1, room + 1)]
    feasible = [row for row in options if math.isfinite(row["seconds"])]
    best = min(feasible, key=lambda row: (row["seconds"], row["extra_cells"])) if feasible else None
    chosen = 0
    if facts.required > 0 and best is not None:
        saving = wait - best["seconds"]
        if saving >= config.min_saving_seconds and best["seconds"] <= wait * config.max_ratio:
            chosen = best["extra_cells"]
    fuel_deficit = False
    if facts.item == FUEL_ITEM and facts.produced is not None and facts.consumed is not None:
        burning = facts.consumed + extra_fuel_burn
        fuel_deficit = facts.produced < burning * config.fuel_margin
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
    reason = (
        f"{facts.item}: required {facts.required:.0f} (shortfall {facts.shortfall:.0f} + lookahead "
        f"{facts.demand:.0f}), net {net:.1f}/min; wait T(0)={_fmt(wait)} vs +{chosen} cells "
        f"T({chosen})={_fmt(picked['seconds'])}, saves {_fmt(saved)}; "
        f"new net {picked['new_net_per_minute']:.1f}/min"
    )
    if fuel_deficit:
        reason += f"; fuel deficit: produced {facts.produced:.1f}/min < {config.fuel_margin} x burned {facts.consumed + extra_fuel_burn:.1f}/min"
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
        if any(earlier.item in cost for earlier in kept):
            continue
        kept.append(decision)
    return kept


def _fuel_burn(decision: CapacityDecision, config: CapacityConfig) -> float:
    """Coal/minute the new cells of a non-coal decision will burn."""
    extra = decision.target_cells - decision.current_cells
    per_cell = config.fuel_per_cell_per_minute if decision.item in SMELTED_ITEMS else config.fuel_per_drill_per_minute
    return extra * per_cell


def plan_capacity(supply, demand, *, config: CapacityConfig = CapacityConfig()) -> list[CapacityDecision]:
    """Starter capacity expansions that finish the required items sooner than waiting.

    Items without evidence are skipped; partial receipts never raise. Coal is
    assessed last so it can include the fuel of the other decisions' new
    cells (only decisions that survive the ordering rule count).
    """
    if not isinstance(supply, Mapping):
        return []
    demand = demand if isinstance(demand, Mapping) else {}
    others: list[CapacityDecision] = []
    for item in RESOURCE_FOR_ITEM:
        evidence = _evidence(supply, item)
        if item == FUEL_ITEM or evidence is None:
            continue
        decision = _assess(_facts(item, evidence, demand, config), supply, config)
        if decision is not None:
            others.append(decision)
    kept = _ordered(others)
    coal = _evidence(supply, FUEL_ITEM)
    if coal is None:
        return kept
    burn = sum(_fuel_burn(decision, config) for decision in kept)
    decision = _assess(_facts(FUEL_ITEM, coal, demand, config), supply, config, extra_fuel_burn=burn)
    if decision is None:
        return kept
    return _ordered(kept + [decision])
