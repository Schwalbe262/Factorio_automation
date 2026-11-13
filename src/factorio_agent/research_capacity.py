"""Size science production and labs together from research time, stock and plate supply.

Pure functions, no IO. Units: seconds for times, items per minute for rates,
technology ``unit_energy`` is converted by the caller (ticks / 60 = seconds).

Time model for one technology with ``units`` remaining:

- Lab time: ``units * unit_seconds / (labs * lab_speed)``.
- Pack time per pack ``p``: ``need_p = max(0, units * per_unit_p - stock_p)``;
  ``60 * need_p / rate_p + ramp_p`` (``ramp_p`` is one craft cycle of one
  machine, the latency before the first new pack exists), 0 when nothing is
  missing and infinite when packs are missing and nothing produces them.
- ``T = max(lab time, every pack time)``. A queue of technologies sums ``T``;
  pack stock is used up by the earlier technologies first.

Decision rule (same shape as ``capacity_planning``), for options
``(a, l)`` = extra science assemblers for the limiting pack and extra labs,
each ``0..max_step`` and within the absolute ``max_assemblers``/``max_labs``:

- Cost: ``a * assembler_cost[pack] + l * lab_cost`` raw items. ``fund`` is the
  longest per-item ``60 * max(0, cost - stock) / produced``; an item that is
  missing and not produced makes the option impossible, and an unknown cost
  never yields an expansion.
- ``build = (a * machines_per_assembler + l) * build_seconds_per_machine``.
- Materials cap: the extra pack rate ``a * rate_per_assembler`` consumes
  ``pack_cost[pack][item]`` raw items per pack. It is capped at
  ``(max(0, net) + 60 * stock_left / T_uncapped) / per_pack`` for every raw
  item, where ``stock_left`` is the stock not spent on the expansion.
- ``T(a, l) = fund + build + T_queue(new hardware, capped rate)``. Research is
  not credited during funding and building (conservative, as in mining).
- Expand to the fastest option only when the do-nothing queue time is at
  least ``min_remaining_seconds``, the option saves at least
  ``min_saving_seconds`` and ``T(a, l) <= max_ratio * T(0, 0)``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from .capacity_planning import fund_seconds, finite_number, stock_on_hand
from .progression import RAW_ITEMS, raw_cost, roadmap_wanted_technologies

RED_PACK = "automation-science-pack"
GREEN_PACK = "logistic-science-pack"
DEFAULT_MACHINE = "assembling-machine-1"
# Inserters a science assembler needs in the startup cell: one input, one output.
INSERTERS_PER_ASSEMBLER = 2
# Lab research speed is not exported by the world catalog; vanilla lab = 1.0.
DEFAULT_LAB_SPEED = 1.0


@dataclass(frozen=True)
class ResearchCapacityConfig:
    """Tunable limits; units are stated per field."""

    # Most machines of each kind added by one decision.
    max_step: int = 2
    # Absolute machine limits (the caller lowers them to what its layout holds).
    max_assemblers: int = 3
    max_labs: int = 4
    # Seconds to place one machine once its items are on hand.
    build_seconds_per_machine: float = 20.0
    # Expansion must save at least this many seconds and be this much faster.
    min_saving_seconds: float = 60.0
    max_ratio: float = 0.85
    # Never expand for a queue that finishes sooner than this anyway (seconds).
    min_remaining_seconds: float = 120.0


@dataclass(frozen=True)
class ResearchTarget:
    """One technology still to research: ``units`` remaining, lab seconds per unit."""

    technology: str
    units: float
    unit_seconds: float
    packs_per_unit: Mapping[str, float]


@dataclass(frozen=True)
class ResearchHardware:
    """Science production and labs available now."""

    # Science assemblers producing each pack.
    assemblers: Mapping[str, int]
    # Packs/minute one assembler of that pack makes.
    pack_rate_per_assembler: Mapping[str, float]
    labs: int
    lab_speed: float = DEFAULT_LAB_SPEED
    # Packs already made (inventory, chests, labs) that the queue can use.
    pack_stock: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ResearchMaterials:
    """Raw-item evidence and the raw cost of one more machine of each kind."""

    # item -> {available|collectable, produced_per_minute, net_per_minute}
    evidence: Mapping[str, Mapping[str, Any]]
    # pack -> raw items of one more science production unit
    assembler_cost: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    # raw items of one more lab
    lab_cost: Mapping[str, float] = field(default_factory=dict)
    # pack -> raw items consumed per pack
    pack_cost: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    # pack -> machines placed per science production unit (build time)
    machines_per_assembler: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ResearchCapacityDecision:
    """Grow ``pack`` assemblers and labs from current to target counts (or keep them)."""

    technologies: tuple[str, ...]
    pack: str | None
    current_assemblers: int
    target_assemblers: int
    current_labs: int
    target_labs: int
    wait_seconds: float
    chosen_seconds: float
    seconds_saved: float
    bottleneck_before: str
    bottleneck_after: str
    reason: str
    options: tuple[dict, ...] = ()

    @property
    def expand(self) -> bool:
        return self.target_assemblers > self.current_assemblers or self.target_labs > self.current_labs

    def as_dict(self) -> dict:
        """JSON-safe dict (infinite times become None)."""
        row = asdict(self)
        row["expand"] = self.expand
        row["technologies"] = list(self.technologies)
        row["options"] = [{k: _json_number(v) for k, v in option.items()} for option in self.options]
        for key in ("wait_seconds", "chosen_seconds", "seconds_saved"):
            row[key] = _json_number(row[key])
        return row


def _json_number(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, float):
        return round(value, 1)
    return value


def _fmt(seconds: float) -> str:
    return "inf" if math.isinf(seconds) else f"{seconds:.0f}s"


def research_seconds(
    units,
    unit_seconds,
    labs,
    lab_speed,
    pack_rates_per_minute,
    *,
    packs_per_unit=None,
    pack_stock=None,
    ramp_seconds=None,
) -> float:
    """Seconds to finish ``units``, limited by labs and by each pack's production.

    ``pack_rates_per_minute`` maps pack -> packs/minute; ``packs_per_unit``
    defaults to one of every pack in that mapping. ``pack_stock`` (packs) is
    used before production. ``ramp_seconds`` (pack -> seconds) is added to a
    pack's time when that pack still has to be produced.
    """
    if units <= 0:
        return 0.0
    if labs <= 0 or lab_speed <= 0:
        return math.inf
    total = units * unit_seconds / (labs * lab_speed)
    per_unit = packs_per_unit if packs_per_unit is not None else {name: 1.0 for name in pack_rates_per_minute}
    stock = pack_stock or {}
    ramp = ramp_seconds or {}
    for pack, amount in per_unit.items():
        need = max(0.0, units * amount - stock.get(pack, 0.0))
        if need <= 0:
            continue
        rate = pack_rates_per_minute.get(pack, 0.0)
        if rate <= 0:
            return math.inf
        total = max(total, 60.0 * need / rate + ramp.get(pack, 0.0))
    return total


@dataclass(frozen=True)
class _Queue:
    seconds: float
    lab_seconds: float
    pack_seconds: dict[str, float]


def _rates(hardware: ResearchHardware) -> dict[str, float]:
    return {
        pack: count * hardware.pack_rate_per_assembler.get(pack, 0.0) for pack, count in hardware.assemblers.items()
    }


def _ramps(hardware: ResearchHardware) -> dict[str, float]:
    ramps = {}
    for pack, rate in hardware.pack_rate_per_assembler.items():
        ramps[pack] = 60.0 / rate if rate > 0 else 0.0
    return ramps


def _queue(targets, hardware: ResearchHardware, rates=None) -> _Queue:
    """Queue time with ``rates`` (pack -> packs/minute, default from hardware)."""
    rates = _rates(hardware) if rates is None else rates
    ramps = _ramps(hardware)
    stock = {name: max(0.0, float(n)) for name, n in hardware.pack_stock.items()}
    total = 0.0
    lab_total = 0.0
    pack_totals: dict[str, float] = {}
    for target in targets:
        total += research_seconds(
            target.units,
            target.unit_seconds,
            hardware.labs,
            hardware.lab_speed,
            rates,
            packs_per_unit=target.packs_per_unit,
            pack_stock=stock,
            ramp_seconds=ramps,
        )
        if hardware.labs > 0 and hardware.lab_speed > 0:
            lab_total += target.units * target.unit_seconds / (hardware.labs * hardware.lab_speed)
        else:
            lab_total = math.inf
        for pack, amount in target.packs_per_unit.items():
            need = max(0.0, target.units * amount - stock.get(pack, 0.0))
            stock[pack] = max(0.0, stock.get(pack, 0.0) - target.units * amount)
            rate = rates.get(pack, 0.0)
            if need <= 0:
                seconds = 0.0
            elif rate > 0:
                seconds = 60.0 * need / rate + ramps.get(pack, 0.0)
            else:
                seconds = math.inf
            pack_totals[pack] = pack_totals.get(pack, 0.0) + seconds
    return _Queue(total, lab_total, pack_totals)


def research_queue_seconds(targets, hardware: ResearchHardware) -> float:
    """Total seconds to research ``targets`` (a list) in order with ``hardware``."""
    return _queue(_targets(targets), hardware).seconds


def _targets(targets) -> list[ResearchTarget]:
    if isinstance(targets, ResearchTarget):
        return [targets]
    return [target for target in targets if isinstance(target, ResearchTarget)]


def _bottleneck(queue: _Queue) -> str:
    packs = max(queue.pack_seconds.values(), default=0.0)
    return "labs" if queue.lab_seconds >= packs else "packs"


def _add(cost: dict[str, float], extra: Mapping[str, float], times: float) -> None:
    for name, amount in extra.items():
        value = finite_number(amount)
        if value is not None and value > 0 and times > 0:
            cost[name] = cost.get(name, 0.0) + times * value


def _fund(cost: Mapping[str, float], evidence: Mapping) -> float:
    longest = 0.0
    for item, amount in cost.items():
        row = evidence.get(item)
        if not isinstance(row, Mapping):
            return math.inf if amount > 0 else longest
        longest = max(longest, fund_seconds(amount, stock_on_hand(row), finite_number(row.get("produced_per_minute"))))
    return longest


def _capped_extra_rate(extra: float, pack_cost, cost, evidence, uncapped_seconds) -> float:
    """Extra packs/minute the raw items sustain (see module docstring)."""
    allowed = extra
    for item, per_pack in pack_cost.items():
        if per_pack <= 0:
            continue
        row = evidence.get(item)
        if not isinstance(row, Mapping):
            return 0.0
        net = finite_number(row.get("net_per_minute")) or 0.0
        left = max(0.0, stock_on_hand(row) - cost.get(item, 0.0))
        headroom = max(0.0, net)
        if math.isfinite(uncapped_seconds) and uncapped_seconds > 0:
            headroom += 60.0 * left / uncapped_seconds
        allowed = min(allowed, headroom / per_pack)
    return max(0.0, allowed)


def _option(targets, hardware, materials, config, pack, extra_assemblers, extra_labs) -> dict:
    cost: dict[str, float] = {}
    known = True
    if extra_assemblers:
        unit = materials.assembler_cost.get(pack) if pack else None
        known = known and bool(unit)
        _add(cost, unit or {}, extra_assemblers)
    if extra_labs:
        known = known and bool(materials.lab_cost)
        _add(cost, materials.lab_cost, extra_labs)
    fund = _fund(cost, materials.evidence) if known else math.inf
    machines = materials.machines_per_assembler.get(pack, 1.0) if pack else 1.0
    build = (extra_assemblers * machines + extra_labs) * config.build_seconds_per_machine
    assemblers = dict(hardware.assemblers)
    if pack:
        assemblers[pack] = assemblers.get(pack, 0) + extra_assemblers
    new = ResearchHardware(
        assemblers=assemblers,
        pack_rate_per_assembler=hardware.pack_rate_per_assembler,
        labs=hardware.labs + extra_labs,
        lab_speed=hardware.lab_speed,
        pack_stock=hardware.pack_stock,
    )
    rates = _rates(new)
    capped = False
    if pack and extra_assemblers:
        extra = extra_assemblers * hardware.pack_rate_per_assembler.get(pack, 0.0)
        uncapped = _queue(targets, new).seconds
        allowed = _capped_extra_rate(extra, materials.pack_cost.get(pack, {}), cost, materials.evidence, uncapped)
        if allowed < extra - 1e-9:
            capped = True
            rates[pack] = rates[pack] - extra + allowed
    queue = _queue(targets, new, rates)
    total = fund + build + queue.seconds
    bottleneck = _bottleneck(queue)
    if capped and bottleneck == "packs":
        bottleneck = "materials"
    return {
        "extra_assemblers": extra_assemblers,
        "extra_labs": extra_labs,
        "fund_seconds": fund,
        "build_seconds": build,
        "research_seconds": queue.seconds,
        "seconds": total,
        "pack_rate_per_minute": rates.get(pack, 0.0) if pack else 0.0,
        "materials_capped": capped,
        "bottleneck": bottleneck,
        "raw_cost": {name: round(amount, 2) for name, amount in sorted(cost.items())},
    }


def plan_research_capacity(
    targets, hardware: ResearchHardware, materials: ResearchMaterials, *, config=ResearchCapacityConfig()
) -> ResearchCapacityDecision:
    """Assemblers and labs that finish the research queue fastest (see module rules).

    ``targets`` is one ``ResearchTarget`` or a list (the near-term queue);
    one expansion is then justified by the whole queue.
    """
    targets = _targets(targets)
    names = tuple(target.technology for target in targets)
    baseline = _queue(targets, hardware)
    before = _bottleneck(baseline)
    pack = None
    if baseline.pack_seconds:
        pack = max(sorted(baseline.pack_seconds), key=lambda name: baseline.pack_seconds[name])
    current_assemblers = int(hardware.assemblers.get(pack, 0)) if pack else 0
    room_assemblers = max(0, min(config.max_step, config.max_assemblers - current_assemblers)) if pack else 0
    room_labs = max(0, min(config.max_step, config.max_labs - hardware.labs))
    options = []
    for extra_assemblers in range(room_assemblers + 1):
        for extra_labs in range(room_labs + 1):
            options.append(_option(targets, hardware, materials, config, pack, extra_assemblers, extra_labs))
    wait = options[0]["seconds"]
    feasible = [row for row in options[1:] if math.isfinite(row["seconds"])]
    best = (
        min(feasible, key=lambda row: (row["seconds"], row["extra_assemblers"] + row["extra_labs"]))
        if feasible
        else None
    )
    chosen = options[0]
    why = ""
    if not targets:
        why = "no research queued"
    elif not math.isfinite(wait):
        why = "current research cannot finish (missing packs or labs)"
        if best is not None:
            chosen = best
            why = ""
    elif wait < config.min_remaining_seconds:
        why = f"finishes in {_fmt(wait)} < {config.min_remaining_seconds:.0f}s anyway"
    elif best is None:
        why = "no fundable expansion (unknown cost, or an item neither in stock nor produced)"
    elif wait - best["seconds"] < config.min_saving_seconds or best["seconds"] > wait * config.max_ratio:
        why = (
            f"best +{best['extra_assemblers']} assemblers +{best['extra_labs']} labs takes "
            f"{_fmt(best['seconds'])}, saving under {config.min_saving_seconds:.0f}s or "
            f"{(1 - config.max_ratio) * 100:.0f}%"
        )
    else:
        chosen = best
    saved = wait - chosen["seconds"] if math.isfinite(wait) else math.inf
    after = chosen["bottleneck"]
    if chosen is options[0]:
        reason = (
            f"{', '.join(names) or 'none'}: keep {current_assemblers} {pack or 'pack'} assemblers and "
            f"{hardware.labs} labs; wait {_fmt(wait)} ({before}-limited: labs {_fmt(baseline.lab_seconds)}, "
            f"packs {_fmt(max(baseline.pack_seconds.values(), default=0.0))}); {why}"
        )
    else:
        reason = (
            f"{', '.join(names)}: wait {_fmt(wait)} ({before}-limited: labs {_fmt(baseline.lab_seconds)}, "
            f"packs {_fmt(max(baseline.pack_seconds.values(), default=0.0))}) vs +{chosen['extra_assemblers']} "
            f"{pack} assemblers +{chosen['extra_labs']} labs = fund {_fmt(chosen['fund_seconds'])} + build "
            f"{_fmt(chosen['build_seconds'])} + research {_fmt(chosen['research_seconds'])} = "
            f"{_fmt(chosen['seconds'])}, saves {_fmt(saved)}; then {after}-limited"
        )
    return ResearchCapacityDecision(
        technologies=names,
        pack=pack,
        current_assemblers=current_assemblers,
        target_assemblers=current_assemblers + chosen["extra_assemblers"],
        current_labs=hardware.labs,
        target_labs=hardware.labs + chosen["extra_labs"],
        wait_seconds=wait,
        chosen_seconds=chosen["seconds"],
        seconds_saved=saved,
        bottleneck_before=before,
        bottleneck_after=after,
        reason=reason,
        options=tuple(options),
    )


def hardware_option(
    targets,
    hardware: ResearchHardware,
    new: ResearchHardware,
    materials: ResearchMaterials,
    *,
    cost: Mapping[str, float],
    build_seconds: float,
    pack: str | None = None,
) -> dict:
    """``fund + build + research`` seconds of moving from ``hardware`` to ``new`` for ``cost`` raw items.

    The same time model as one planner option, for options the planner's
    ``(a, l)`` grid does not hold (another block, faster machines): the extra
    ``pack`` rate of ``new`` over ``hardware`` is capped by the raw items as
    in the module docstring; an item missing from the evidence and not
    produced makes the option impossible (inf).
    """
    targets = _targets(targets)
    spend = {name: float(amount) for name, amount in cost.items() if finite_number(amount)}
    fund = _fund(spend, materials.evidence) if spend else 0.0
    rates = _rates(new)
    capped = False
    if pack:
        extra = rates.get(pack, 0.0) - _rates(hardware).get(pack, 0.0)
        if extra > 1e-9:
            uncapped = _queue(targets, new).seconds
            allowed = _capped_extra_rate(extra, materials.pack_cost.get(pack, {}), spend, materials.evidence, uncapped)
            if allowed < extra - 1e-9:
                capped = True
                rates[pack] = rates[pack] - extra + allowed
    queue = _queue(targets, new, rates)
    return {
        "fund_seconds": fund,
        "build_seconds": float(build_seconds),
        "research_seconds": queue.seconds,
        "seconds": fund + float(build_seconds) + queue.seconds,
        "pack_rate_per_minute": rates.get(pack, 0.0) if pack else 0.0,
        "materials_capped": capped,
        "raw_cost": {name: round(amount, 2) for name, amount in sorted(spend.items())},
    }


def labs_without_assemblers(row: Mapping, config=ResearchCapacityConfig()) -> int:
    """Labs to build when a saved decision's extra assemblers cannot be built.

    ``row`` is ``ResearchCapacityDecision.as_dict()``. Only options that add
    no assembler are considered, with the same rules as the planner: the best
    one must save ``min_saving_seconds`` and be at most ``max_ratio`` of the
    wait (an infinite wait takes any finite option). Adding labs to a
    pack-limited queue therefore adds none. Returns the target lab count.
    """
    current = int(row.get("current_labs", 1) or 1)
    options = [o for o in row.get("options") or () if isinstance(o, Mapping) and o.get("extra_assemblers") == 0]
    if not options:
        return current

    def seconds(option):
        value = option.get("seconds")
        return math.inf if value is None else float(value)

    wait = seconds(options[0]) if options[0].get("extra_labs") == 0 else math.inf
    feasible = [o for o in options if o.get("extra_labs", 0) > 0 and math.isfinite(seconds(o))]
    if not feasible:
        return current
    best = min(feasible, key=lambda o: (seconds(o), o["extra_labs"]))
    if math.isfinite(wait) and (
        wait < config.min_remaining_seconds
        or wait - seconds(best) < config.min_saving_seconds
        or seconds(best) > wait * config.max_ratio
    ):
        return current
    return current + int(best["extra_labs"])


# --- catalog helpers -------------------------------------------------------


def _deterministic_product_amount(recipe: Mapping, item: str) -> float:
    """Exact ``amount * probability`` of ``item`` per craft; raises without one.

    Deliberately stricter than ``progression._product_amount`` (an estimate
    that averages ``amount_min``/``amount_max`` and defaults to 1): machine
    counts here must not rest on a guessed yield.
    """
    for product in recipe.get("products") or ():
        if isinstance(product, Mapping) and product.get("name") == item:
            amount = finite_number(product.get("amount"))
            probability = finite_number(product.get("probability", 1))
            if amount and probability:
                return amount * probability
    raise ValueError(item + " recipe has no deterministic product amount")


def pack_rate_per_assembler(catalog, pack, machine=DEFAULT_MACHINE) -> float:
    """Packs/minute one ``machine`` makes: ``60 * crafting_speed * yield / energy``."""
    recipe = catalog.recipe_for_product(pack)
    speed = finite_number(catalog.entities[machine].get("crafting_speed"))
    energy = finite_number(recipe.get("energy"))
    if not recipe or not speed or not energy or speed <= 0 or energy <= 0:
        raise ValueError(pack + " machine rate unknown")
    return 60.0 * speed * _deterministic_product_amount(recipe, pack) / energy


def _machine_demand(catalog, item, rate, speed, depth, out) -> None:
    """Fractional machines per recipe to make ``rate`` items/minute of ``item`` and its intermediates."""
    if item in RAW_ITEMS or depth > 8 or rate <= 0:
        return
    recipe = catalog.recipe_for_product(item)
    if not recipe:
        return
    energy = finite_number(recipe.get("energy"))
    if not energy or energy <= 0:
        raise ValueError(item + " recipe energy unknown")
    made = _deterministic_product_amount(recipe, item)
    out[recipe["name"]] = out.get(recipe["name"], 0.0) + rate * energy / (60.0 * speed * made)
    crafts = rate / made
    for ingredient in recipe.get("ingredients") or ():
        if isinstance(ingredient, Mapping) and ingredient.get("type", "item") == "item":
            amount = finite_number(ingredient.get("amount"))
            if amount:
                _machine_demand(catalog, ingredient["name"], crafts * amount, speed, depth + 1, out)


def expansion_costs(catalog, pack, *, current_assemblers=1, machine=DEFAULT_MACHINE) -> dict:
    """Raw cost of one more lab and of one more science production unit for ``pack``.

    Ratio rule for upstream machines: ``n`` consumers making ``r`` packs/min
    each need ``ceil(sum over the chain of n * r * per_pack * energy /
    (60 * speed * yield))`` machines of every intermediate recipe. One more
    unit adds ``ceil(f(n + 1)) - ceil(f(n))`` upstream machines. For red
    science in assembling-machine-1 (gear 0.5 s, pack 5 s, 1 gear per pack)
    ``f(n) = n / 10``: one gear assembler feeds ten science assemblers, so
    the second and third science assemblers need no new gear assembler.
    Every machine of the unit also gets ``INSERTERS_PER_ASSEMBLER`` inserters.
    """
    speed = finite_number(catalog.entities[machine].get("crafting_speed"))
    if not speed or speed <= 0:
        raise ValueError(machine + " crafting speed unknown")
    rate = pack_rate_per_assembler(catalog, pack, machine)
    pack_recipe = catalog.recipe_for_product(pack)["name"]

    def upstream(count):
        demand: dict[str, float] = {}
        _machine_demand(catalog, pack, count * rate, speed, 0, demand)
        demand.pop(pack_recipe, None)
        return demand

    before = upstream(max(0, int(current_assemblers)))
    after = upstream(max(0, int(current_assemblers)) + 1)
    extra_upstream = {
        name: math.ceil(after[name] - 1e-9) - math.ceil(before.get(name, 0.0) - 1e-9) for name in sorted(after)
    }
    extra_upstream = {name: n for name, n in extra_upstream.items() if n > 0}
    machines = 1 + sum(extra_upstream.values())
    unit = raw_cost(catalog, {machine: machines, "inserter": INSERTERS_PER_ASSEMBLER * machines})
    return {
        "assembler": {name: round(amount, 3) for name, amount in unit.items()},
        "lab": {name: round(amount, 3) for name, amount in raw_cost(catalog, {"lab": 1}).items()},
        "pack": {name: round(amount, 3) for name, amount in raw_cost(catalog, {pack: 1}).items()},
        "machines_per_assembler": machines,
        "extra_upstream_machines": extra_upstream,
        "pack_rate_per_assembler": rate,
    }


def research_target(catalog, technology, *, progress=0.0) -> ResearchTarget | None:
    """A fixed-count technology from the catalog (``unit_energy`` ticks -> seconds), else None."""
    tech = (getattr(catalog, "technologies", None) or {}).get(technology)
    if not isinstance(tech, Mapping) or tech.get("count_formula") or tech.get("research_trigger"):
        return None
    units = finite_number(tech.get("unit_count"))
    energy = finite_number(tech.get("unit_energy"))
    if not units or units <= 0 or energy is None or energy <= 0:
        return None
    packs = {}
    for row in tech.get("ingredients") or ():
        amount = finite_number(row.get("amount")) if isinstance(row, Mapping) else None
        if amount:
            packs[row["name"]] = packs.get(row["name"], 0.0) + amount
    if not packs:
        return None
    done = finite_number(progress) or 0.0
    remaining = units * (1.0 - min(1.0, max(0.0, done)))
    return ResearchTarget(technology, remaining, energy / 60.0, packs)


def research_targets(catalog, technologies: Iterable[str]) -> list[ResearchTarget]:
    rows = [research_target(catalog, name) for name in technologies]
    return [row for row in rows if row is not None]


def recommended_science_rate(
    catalog, targets, materials, *, max_rate=30, pack=RED_PACK, lab_speed=DEFAULT_LAB_SPEED
) -> dict | None:
    """Pack rate (packs/min) and lab count that finish ``targets`` fastest within plate supply.

    ``targets`` is a list of ``ResearchTarget`` or technology names, only those
    using ``pack`` count; ``materials`` maps raw item -> evidence with
    ``net_per_minute``. Faster packs never slow research, so the rate is the
    highest of ``max_rate`` and what net plate production sustains
    (``min over items of net / raw per pack``), floored to whole packs; labs
    are the fewest that consume that rate: ``ceil(rate * sum(units *
    unit_seconds) / (60 * lab_speed * sum(units * packs per unit)))``.
    Returns None when the evidence is missing (the caller keeps its default).
    """
    rows = [row if isinstance(row, ResearchTarget) else research_target(catalog, row) for row in targets]
    rows = [row for row in rows if row is not None and row.packs_per_unit.get(pack)]
    if not rows or not isinstance(materials, Mapping):
        return None
    per_pack = raw_cost(catalog, {pack: 1})
    if not per_pack:
        return None
    sustain = math.inf
    for item, amount in per_pack.items():
        row = materials.get(item)
        net = finite_number(row.get("net_per_minute")) if isinstance(row, Mapping) else None
        if net is None or amount <= 0:
            return None
        sustain = min(sustain, max(0.0, net) / amount)
    rate = math.floor(min(float(max_rate), sustain))
    if rate < 1:
        return None
    lab_work = sum(row.units * row.unit_seconds for row in rows)
    packs = sum(row.units * row.packs_per_unit[pack] for row in rows)
    labs = max(1, math.ceil(rate * lab_work / (60.0 * lab_speed * packs) - 1e-9))
    seconds = max(lab_work / (labs * lab_speed), 60.0 * packs / rate)
    return {
        "pack": pack,
        "rate_per_minute": rate,
        "labs": labs,
        "sustainable_rate_per_minute": round(sustain, 2) if math.isfinite(sustain) else None,
        "queue_seconds": round(seconds, 1),
        "technologies": [row.technology for row in rows],
        "reason": (
            f"{pack}: {rate}/min (cap {max_rate}, plates sustain {sustain:.1f}/min) with {labs} labs "
            f"finishes {len(rows)} technologies in {seconds:.0f}s"
        ),
    }


# --- observe receipt -------------------------------------------------------

RECOMMENDED_PACKS = (RED_PACK, GREEN_PACK)


def _evidence_from_supply(observation: Mapping) -> dict[str, Mapping]:
    supply = observation.get("resource_supply_options")
    rows = {}
    if isinstance(supply, Mapping):
        for item, row in supply.items():
            evidence = row.get("evidence") if isinstance(row, Mapping) else None
            if isinstance(evidence, Mapping):
                rows[item] = evidence
    return rows


def _researched(observation: Mapping) -> set[str]:
    value = observation.get("technologies")
    if isinstance(value, Mapping):
        return {name for name, flag in value.items() if flag}
    if isinstance(value, (list, tuple, set)):
        return {name for name in value if isinstance(name, str)}
    return set()


def hardware_from_observation(catalog, observation: Mapping, packs: Iterable[str]) -> ResearchHardware:
    """Machines with a pack recipe and connected labs counted from the receipt's entities."""
    assemblers = {}
    rates = {}
    stock = {}
    labs = 0
    inventory = observation.get("inventory") if isinstance(observation.get("inventory"), Mapping) else {}
    entities = [e for e in observation.get("entities") or () if isinstance(e, Mapping)]
    for pack in packs:
        machines = [e for e in entities if e.get("recipe") == pack]
        assemblers[pack] = len(machines)
        machine = machines[0]["name"] if machines else DEFAULT_MACHINE
        rates[pack] = pack_rate_per_assembler(catalog, pack, machine)
        stock[pack] = float(finite_number(inventory.get(pack)) or 0.0)
    for entity in entities:
        if entity.get("name") != "lab" or entity.get("electric_network_connected") is False:
            continue
        labs += 1
        held = entity.get("inventory") if isinstance(entity.get("inventory"), Mapping) else {}
        for pack in stock:
            stock[pack] += float(finite_number(held.get(pack)) or 0.0)
    return ResearchHardware(assemblers, rates, labs, DEFAULT_LAB_SPEED, stock)


def queue_targets(catalog, jobs, observation: Mapping, *, horizon=4) -> list[ResearchTarget]:
    """The running research (with its progress), the native queue behind it, then the next roadmap technologies.

    At most ``horizon``; the native queue (``observation["research_queue"]``)
    is what the labs research next, so it ranks before the roadmap.
    """
    researched = _researched(observation)
    names = []
    current = observation.get("research")
    targets = []
    if isinstance(current, str) and current not in researched:
        target = research_target(catalog, current, progress=observation.get("research_progress"))
        if target is not None:
            targets.append(target)
            names.append(current)
    queued = observation.get("research_queue")
    queued = [name for name in queued if isinstance(name, str)] if isinstance(queued, list) else []
    for name in queued + roadmap_wanted_technologies(list(jobs or ()), researched):
        if len(targets) >= horizon:
            break
        if name in names or name in researched:
            continue
        target = research_target(catalog, name)
        if target is not None:
            targets.append(target)
            names.append(name)
    return targets


def lab_pack_rate(targets, labs: int, pack=RED_PACK, lab_speed=DEFAULT_LAB_SPEED) -> float | None:
    """Packs/minute of ``pack`` that ``labs`` busy labs consume over ``targets`` (None without a target using it).

    Same weighting as ``recommended_science_rate``:
    ``labs * lab_speed * 60 * sum(units * packs) / sum(units * unit_seconds)``.
    """
    rows = [row for row in targets if row.packs_per_unit.get(pack) and row.units > 0 and row.unit_seconds > 0]
    if not rows:
        return None
    work = sum(row.units * row.unit_seconds for row in rows)
    packs = sum(row.units * row.packs_per_unit[pack] for row in rows)
    return labs * lab_speed * 60.0 * packs / work


def split_producible(targets, produced: Iterable[str], pack_stock: Mapping[str, float] | None = None):
    """``(researchable, blocked)``: technologies whose every pack has a producer, and the rest.

    A pack counts when something makes it (``produced``: a built block or a pre-bus cell
    with that recipe) or the stock already covers the technology's need. A technology
    needing a pack nobody makes is ``{"technology", "pack"}`` in ``blocked``: it would make
    the whole queue infinite and the sizing degenerate to "keep", so it is reported, not sized.
    """
    produced = set(produced)
    stock = dict(pack_stock or {})
    ok, blocked = [], []
    for target in _targets(targets):
        missing = next(
            (
                pack
                for pack, per_unit in sorted(target.packs_per_unit.items())
                if per_unit > 0 and pack not in produced and stock.get(pack, 0.0) < target.units * per_unit - 1e-9
            ),
            None,
        )
        if missing is None:
            ok.append(target)
        else:
            blocked.append({"technology": target.technology, "pack": missing})
    return ok, blocked


def research_capacity_receipt(catalog, jobs, observation: Mapping, *, horizon=4) -> dict:
    """The planner's decision for the running (or next) research queue, for the observe receipt.

    Sized on the technologies whose packs have a producer (``split_producible``); the
    others are listed in ``blocked_by_missing_pack``.
    """
    researched = _researched(observation)
    targets = queue_targets(catalog, jobs, observation, horizon=horizon)
    evidence = _evidence_from_supply(observation)
    receipt: dict[str, Any] = {}
    if targets:
        packs = sorted({pack for target in targets for pack in target.packs_per_unit})
        hardware = hardware_from_observation(catalog, observation, packs)
        # Red packs always have a producer (the startup path hand-crafts and then machine-builds them).
        produced = {pack for pack, count in hardware.assemblers.items() if count > 0} | {RED_PACK}
        targets, blocked = split_producible(targets, produced, hardware.pack_stock)
        if blocked:
            receipt["blocked_by_missing_pack"] = blocked
    receipt["technologies"] = [target.technology for target in targets]
    if targets:
        packs = sorted({pack for target in targets for pack in target.packs_per_unit})
        hardware = hardware_from_observation(catalog, observation, packs)
        costs = {pack: expansion_costs(catalog, pack, current_assemblers=hardware.assemblers[pack]) for pack in packs}
        materials = ResearchMaterials(
            evidence=evidence,
            assembler_cost={pack: row["assembler"] for pack, row in costs.items()},
            lab_cost=next(iter(costs.values()))["lab"],
            pack_cost={pack: row["pack"] for pack, row in costs.items()},
            machines_per_assembler={pack: row["machines_per_assembler"] for pack, row in costs.items()},
        )
        receipt["decision"] = plan_research_capacity(targets, hardware, materials).as_dict()
    wanted = [name for name in roadmap_wanted_technologies(list(jobs or ()), researched, horizon=12)]
    recommended = {}
    for pack in RECOMMENDED_PACKS:
        row = recommended_science_rate(catalog, wanted, evidence, pack=pack)
        if row is not None:
            recommended[pack] = row
    receipt["recommended_rates"] = recommended
    receipt["scope"] = "time model and rule: research_capacity.py docstring; lab speed assumed 1.0"
    return receipt
