"""Deterministic production-cell compiler.

Given a target item + rate (and the machines/modules/belts available at the current research
level), compute a fully-specified MODULAR single-product cell: how many machines, what belt tier
each input/output needs, the power draw, and a footprint estimate. This is the deterministic core
that replaces LLM guesswork for the calculable part of factory design (the user's steps 1-3).

Because each site makes ONE product (constraint C1), the math is a single-recipe `ceil` calc — no
linear-programming solver is needed (LP is only for multi-recipe/byproduct optimization).

Pure module: no RCON, no file I/O — fully unit-testable. Reads physical specs from
:mod:`factorio_ai.knowledge` (machine/module/belt profiles, recipes, fluids).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import ceil
from typing import Any

from . import knowledge


@dataclass(frozen=True)
class PowerSituation:
    """Live power context for the C2 power-vs-size trade-off.

    ``size_vs_power_pref`` is the knob: 0.0 = minimise footprint (accept more power),
    1.0 = minimise power (accept more machines/footprint). 0.5 = balanced.
    """

    available_headroom_kw: float = float("inf")
    satisfaction: float = 1.0
    size_vs_power_pref: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CellIORate:
    item: str
    per_minute: float
    is_fluid: bool
    belt_tier: str | None  # None for fluids (pipe) or when no belt is fast enough alone
    belt_lanes_needed: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SubStage:
    """A co-located intermediate stage (e.g. copper-cable inside an electronic-circuit cell).

    Some intermediates (copper-cable, iron-gear-wheel) transport poorly and are conventionally
    made on-site, so the cell still counts as ONE final product (constraint C1) while internally
    crafting these. The cell's external inputs become the sub-stage's inputs (e.g. copper-plate
    instead of copper-cable)."""

    item: str
    recipe_name: str
    machine: str
    machine_count: int
    rate_per_minute: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CellSpec:
    target_item: str
    target_rate: float
    recipe_name: str | None
    category: str | None
    machine: str | None
    machine_count: int
    modules: list[str]
    effective_speed: float
    per_machine_rate: float
    achieved_rate: float
    inputs: list[CellIORate]
    outputs: list[CellIORate]
    substages: list[SubStage]
    total_power_kw: float
    footprint: dict[str, float]
    archetype: str
    ok: bool
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["inputs"] = [i.to_dict() if isinstance(i, CellIORate) else i for i in self.inputs]
        data["outputs"] = [o.to_dict() if isinstance(o, CellIORate) else o for o in self.outputs]
        data["substages"] = [s.to_dict() if isinstance(s, SubStage) else s for s in self.substages]
        return data


# Map a crafting category to the placement archetype the placer should use.
_CATEGORY_ARCHETYPE = {
    "smelting": "smelting_column",
    "chemistry": "fluid_machine_block",
    "chemistry-or-cryogenics": "fluid_machine_block",
    "oil-processing": "fluid_machine_block",
    "cryogenics": "fluid_machine_block",
}

_MIN_EFFECT_MULT = 0.2  # Factorio clamps speed/consumption multipliers at +/-80% (min 0.2x).
_PACKING_FACTOR = 1.8   # footprint inflation for inserters/belts/poles around the machines.

# Belt-feedability (must match the cell_placer row geometry): a machine in a placed row exposes a
# limited number of input-inserter slots, and a base inserter moves ~this many items/min. If one
# machine's whole solid-input flow can't be carried by that many inserters, the sandbox shows
# "item_ingredient_shortage" even though the build is valid, so we add machines until each is
# feedable. (Patterson et al. ModRef 2023: inserter count must match the recipe consumption ratio.)
_NORTH_INPUT_SLOTS = 3
_INSERTER_ITEMS_PER_MIN = 57.0

# Coal fuel for burner machines. Coal fuel value ~8 MJ; burner machines report 0 electric draw, so
# we size fuel from their real energy consumption (a small per-tier table; the sandbox confirms).
_FUEL_ITEM = "coal"
_COAL_ENERGY_KJ = 8000.0
_BURNER_POWER_KW = {
    "stone-furnace": 90.0,
    "steel-furnace": 90.0,
    "burner-mining-drill": 150.0,
}


def _coal_per_minute(machine: knowledge.MachineProfile, machine_count: int) -> float:
    """Coal/min a burner machine type consumes at full utilisation (0 for electric machines)."""
    if machine is None or not getattr(machine, "is_burner", False) or machine_count <= 0:
        return 0.0
    power_kw = _BURNER_POWER_KW.get(machine.name) or (machine.energy_kw if machine.energy_kw > 0 else 90.0)
    return machine_count * power_kw * 60.0 / _COAL_ENERGY_KJ

# Intermediates conventionally crafted on-site (poor transport ratio), so a cell making a
# downstream product co-locates them and takes THEIR inputs from belts instead. Keeps one final
# product per site (C1) while matching real factory practice (e.g. green-circuit cells).
_CO_LOCATED_INTERMEDIATES = {"copper-cable", "iron-gear-wheel"}


def _belt_for_rate(per_minute: float, belts: list[knowledge.BeltProfile]) -> tuple[str | None, int]:
    """Smallest belt tier whose throughput >= the per-minute flow; else the fastest belt with the
    number of parallel lanes needed (underground-mix territory — flagged by lanes>1)."""
    if per_minute <= 0 or not belts:
        return (None, 0)
    needed_ips = per_minute / 60.0
    ordered = sorted(belts, key=lambda b: b.items_per_second)
    for belt in ordered:
        if belt.items_per_second >= needed_ips:
            return (belt.name, 1)
    fastest = ordered[-1]
    return (fastest.name, max(1, ceil(needed_ips / fastest.items_per_second)))


def _module_loadouts(machine: knowledge.MachineProfile, available_modules: list[str]) -> list[list[str]]:
    """Candidate uniform loadouts to evaluate for the C2 trade-off: empty + each available module
    type filling all slots. Uniform (not mixed) keeps the search tiny while still spanning the
    power-vs-size curve (speed/prod -> fewer machines + more power; efficiency -> less power)."""
    loadouts: list[list[str]] = [[]]
    if machine.module_slots <= 0:
        return loadouts
    for name in available_modules:
        if knowledge.module_profile(name) is not None:
            loadouts.append([name] * machine.module_slots)
    return loadouts


def _loadout_effects(modules: list[str]) -> tuple[float, float, float]:
    """(speed_mult, productivity_mult, consumption_mult) for a list of installed modules."""
    speed = prod = cons = 0.0
    for name in modules:
        prof = knowledge.module_profile(name)
        if prof is None:
            continue
        speed += prof.speed
        prod += prof.productivity
        cons += prof.consumption
    speed_mult = max(_MIN_EFFECT_MULT, 1.0 + speed)
    prod_mult = max(1.0, 1.0 + prod)
    cons_mult = max(_MIN_EFFECT_MULT, 1.0 + cons)
    return speed_mult, prod_mult, cons_mult


def _select_machine(category: str | None, available_machines: list[str] | None) -> knowledge.MachineProfile | None:
    names = knowledge.machines_for_category(category)
    if available_machines is not None:
        allowed = set(available_machines)
        names = [n for n in names if n in allowed]
    profiles = [knowledge.machine_profile(n) for n in names]
    profiles = [p for p in profiles if p is not None]
    if not profiles:
        return None
    # Fastest tier; tie-break toward more module slots (e.g. electric- over steel-furnace).
    return max(profiles, key=lambda p: (p.crafting_speed, p.module_slots))


def compile_cell(
    target_item: str,
    target_rate: float,
    *,
    available_machines: list[str] | None = None,
    available_modules: list[str] | None = None,
    belt_tiers_available: list[str] | None = None,
    power_situation: PowerSituation | None = None,
) -> CellSpec:
    """Compile a single-product cell spec for ``target_rate`` items/min of ``target_item``."""

    power = power_situation or PowerSituation()
    available_modules = available_modules or []
    warnings: list[str] = []

    recipe = knowledge.recipe_for_product(target_item)
    if recipe is None:
        return _empty_spec(target_item, target_rate, ["no crafting recipe (raw resource?)"])
    category = knowledge.recipe_category_for(target_item)
    machine = _select_machine(category, available_machines)
    if machine is None:
        return _empty_spec(
            target_item, target_rate,
            [f"no available machine for category {category!r}"], recipe_name=recipe.name, category=category,
        )

    product_amount = float(recipe.products.get(target_item) or next(iter(recipe.products.values()), 1.0)) or 1.0
    belts = [knowledge.belt_profile(b) for b in belt_tiers_available] if belt_tiers_available else knowledge.all_belt_profiles()
    belts = [b for b in belts if b is not None] or knowledge.all_belt_profiles()

    # Evaluate candidate module loadouts and pick by the power-vs-size preference (C2).
    candidates: list[dict[str, Any]] = []
    for modules in _module_loadouts(machine, available_modules):
        speed_mult, prod_mult, cons_mult = _loadout_effects(modules)
        per_machine_rate = 60.0 * machine.crafting_speed * speed_mult * product_amount * prod_mult / max(recipe.time_seconds, 0.001)
        if per_machine_rate <= 0:
            continue
        machine_count = max(1, ceil(target_rate / per_machine_rate))
        active_kw = machine.energy_kw * cons_mult
        total_power = machine_count * (active_kw + machine.drain_kw)
        candidates.append({
            "modules": modules,
            "speed_mult": speed_mult,
            "prod_mult": prod_mult,
            "per_machine_rate": per_machine_rate,
            "machine_count": machine_count,
            "total_power": total_power,
        })

    feasible = [c for c in candidates if c["total_power"] <= power.available_headroom_kw]
    pool = feasible or candidates
    if not feasible and candidates:
        warnings.append("no module loadout fits the power headroom; picked lowest-power option")
        pool = sorted(candidates, key=lambda c: c["total_power"])[:1]
    chosen = _pick_loadout(pool, power.size_vs_power_pref)

    machine_count = chosen["machine_count"]
    per_machine_rate = chosen["per_machine_rate"]
    achieved_rate = round(machine_count * per_machine_rate, 3)
    crafts_per_minute = achieved_rate / product_amount

    # Per-machine input feedability note: a single machine runs at a fixed crafting speed and so has
    # a fixed input flow regardless of machine count; if that flow exceeds what a base inserter on
    # each input slot can carry, the cell needs faster/stack inserters or direct insertion to hit
    # full rate (the placer flags the specific shortfall). Adding machines does NOT help here.
    in_rate_per_machine = sum(amount for item, amount in recipe.ingredients.items()
                              if not knowledge.is_fluid(item)) * (per_machine_rate / product_amount)
    if in_rate_per_machine > _NORTH_INPUT_SLOTS * _INSERTER_ITEMS_PER_MIN:
        warnings.append(
            f"each {machine.name} needs ~{round(in_rate_per_machine)}/min of inputs — above base-inserter "
            f"feed ({_NORTH_INPUT_SLOTS}x{round(_INSERTER_ITEMS_PER_MIN)}/min); use fast/stack inserters for full rate"
        )

    # Expand co-located intermediates into sub-stages so the cell's external inputs are the
    # raw-er items (copper-plate, not copper-cable) — the user's modular-cell intent (C1).
    external_demand: dict[str, float] = {}
    substages: list[SubStage] = []
    for item, amount in recipe.ingredients.items():
        demand = crafts_per_minute * amount  # items/min this cell consumes of `item`
        sub_recipe = knowledge.recipe_for_product(item) if item in _CO_LOCATED_INTERMEDIATES else None
        if sub_recipe is not None and machine_count and not knowledge.is_fluid(item):
            sub_cat = knowledge.recipe_category_for(item)
            sub_machine = _select_machine(sub_cat, available_machines) or machine
            sub_prod_amount = float(sub_recipe.products.get(item) or 1.0) or 1.0
            sub_per_machine = 60.0 * sub_machine.crafting_speed * sub_prod_amount / max(sub_recipe.time_seconds, 0.001)
            sub_count = max(1, ceil(demand / sub_per_machine)) if sub_per_machine > 0 else 1
            substages.append(SubStage(item, sub_recipe.name, sub_machine.name, sub_count, round(demand, 3)))
            sub_crafts = demand / sub_prod_amount
            for sub_item, sub_amt in sub_recipe.ingredients.items():
                external_demand[sub_item] = external_demand.get(sub_item, 0.0) + sub_crafts * sub_amt
        else:
            external_demand[item] = external_demand.get(item, 0.0) + demand

    inputs: list[CellIORate] = []
    for item, demand in external_demand.items():
        rate = round(demand, 3)
        is_fluid = knowledge.is_fluid(item)
        belt_tier, lanes = (None, 0) if is_fluid else _belt_for_rate(rate, belts)
        if not is_fluid and lanes > 1:
            warnings.append(f"{item} needs {lanes} belt lanes ({belt_tier}); consider underground-belt mixing")
        inputs.append(CellIORate(item, rate, is_fluid, belt_tier, lanes))

    # Burner machines (stone/steel furnace) consume COAL fuel, not electricity — the cell must feed
    # coal as a material input. Size it from the machines' fuel-energy throughput at full utilisation.
    fuel_rate = _coal_per_minute(machine, machine_count)
    for sub in substages:
        sub_prof = knowledge.machine_profile(sub.machine)
        if sub_prof is not None and sub_prof.is_burner:
            fuel_rate += _coal_per_minute(sub_prof, sub.machine_count)
    if fuel_rate > 0:
        fuel_rate = round(fuel_rate, 3)
        belt_tier, lanes = _belt_for_rate(fuel_rate, belts)
        inputs.append(CellIORate(_FUEL_ITEM, fuel_rate, False, belt_tier, lanes))

    outputs: list[CellIORate] = []
    for item, amount in recipe.products.items():
        rate = round(crafts_per_minute * amount, 3)
        is_fluid = knowledge.is_fluid(item)
        belt_tier, lanes = (None, 0) if is_fluid else _belt_for_rate(rate, belts)
        outputs.append(CellIORate(item, rate, is_fluid, belt_tier, lanes))

    # Include co-located sub-stage machines in power + footprint.
    sub_tiles = 0.0
    sub_power = 0.0
    for sub in substages:
        sub_prof = knowledge.machine_profile(sub.machine)
        if sub_prof is None:
            continue
        sub_tiles += sub.machine_count * sub_prof.tile_width * sub_prof.tile_height
        sub_power += sub.machine_count * (sub_prof.energy_kw + sub_prof.drain_kw)
    tile_area = machine.tile_width * machine.tile_height
    total_machine_tiles = machine_count * tile_area + sub_tiles
    footprint = {
        "machine_tiles": round(total_machine_tiles, 1),
        "area": round(total_machine_tiles * _PACKING_FACTOR, 1),
        "machine_w": machine.tile_width,
        "machine_h": machine.tile_height,
    }
    archetype = _CATEGORY_ARCHETYPE.get(category or "", "assembler_row_block")
    if substages and archetype == "assembler_row_block":
        archetype = "multistage_block"

    return CellSpec(
        target_item=target_item,
        target_rate=round(target_rate, 3),
        recipe_name=recipe.name,
        category=category,
        machine=machine.name,
        machine_count=machine_count,
        modules=list(chosen["modules"]),
        effective_speed=round(machine.crafting_speed * chosen["speed_mult"], 4),
        per_machine_rate=round(per_machine_rate, 3),
        achieved_rate=achieved_rate,
        inputs=inputs,
        outputs=outputs,
        substages=substages,
        total_power_kw=round(chosen["total_power"] + sub_power, 2),
        footprint=footprint,
        archetype=archetype,
        ok=True,
        warnings=warnings,
    )


def _pick_loadout(pool: list[dict[str, Any]], pref: float) -> dict[str, Any]:
    if len(pool) == 1:
        return pool[0]
    pref = min(1.0, max(0.0, pref))
    max_power = max(c["total_power"] for c in pool) or 1.0
    max_area = max(c["machine_count"] for c in pool) or 1.0

    def score(c: dict[str, Any]) -> float:
        return pref * (c["total_power"] / max_power) + (1.0 - pref) * (c["machine_count"] / max_area)

    return min(pool, key=score)


def _empty_spec(
    target_item: str,
    target_rate: float,
    warnings: list[str],
    *,
    recipe_name: str | None = None,
    category: str | None = None,
) -> CellSpec:
    return CellSpec(
        target_item=target_item,
        target_rate=round(target_rate, 3),
        recipe_name=recipe_name,
        category=category,
        machine=None,
        machine_count=0,
        modules=[],
        effective_speed=0.0,
        per_machine_rate=0.0,
        achieved_rate=0.0,
        inputs=[],
        outputs=[],
        substages=[],
        total_power_kw=0.0,
        footprint={"area": 0.0},
        archetype="assembler_row_block",
        ok=False,
        warnings=warnings,
    )
