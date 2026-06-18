"""Structured router that places a compiled production cell into a connected 2D blueprint.

Takes a :class:`factorio_ai.cell_compiler.CellSpec` plus a bounding box (the W x H budget the LLM
hands down, constraint C5) and lays the machines, **continuous single-item belt lanes**, inserters
and power poles into a concrete blueprint that is actually wired: every machine is fed from the
cell's boundary INPUT (one belt source per ingredient) and its product flows on a continuous belt
to the cell's boundary OUTPUT (the destination). Co-located intermediates (e.g. copper-cable inside
an electronic-circuit cell) are produced in their own row and routed by belt into the consuming
machines.

Design grounded in Patterson, Espasa, Chang & Hoffmann, "Towards Automatic Design of Factorio
Blueprints" (ModRef 2023). The paper formalises a blueprint as **boundary sources (one per input
item, with a rate) -> a single destination**, decomposed into (1) recipe/inserter-count selection,
(2) bin-packing, (3) routing that connects every assembler to its ingredients and the product to the
destination. It also confirms the modular single-product cell (C1): "if each intermediate item is
given its own blueprint, a modular factory could be designed". We adopt the paper's correctness
rules — each belt tile carries ONE item type; inserter count per item ~ ceil(rate / inserter_rate);
routes are CONTINUOUS from source to consumer to destination (no disconnected stubs) — but use a
deterministic *structured* layout (neat stage rows + straight lanes), which the paper notes is the
human-preferred, tileable/expandable choice over an unintuitive densely-packed optimum.

Geometry obeys the validator coordinate model in ``planner._blueprint_operability_report``: a
machine occupies its centre +/-1.5; an inserter at ``p`` with direction ``d`` picks up at
``p + dir*reach`` and drops at ``p - dir*reach`` (reach 1, or 2 for long-handed); a belt/chest tile
is a source/sink at its exact tile. Real steady-state throughput remains the sandbox's job (the
paper likewise leaves flow-rate fidelity out of the routing stage).

Pure module: returns entities; no RCON / file I/O.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import ceil, hypot
from typing import Any

from . import knowledge
from .cell_compiler import CellSpec
from .monitor import EAST as _EAST_DIR, NORTH, WEST as _WEST_DIR

# Validator direction whose inserter picks up to the NORTH and drops to the SOUTH (see module
# docstring): a north input lane feeds the machine below it, and a machine drops to the south lane,
# both with a NORTH-facing inserter (the geometry is symmetric about the inserter tile).
_BELT = "transport-belt"
_INSERTER = "inserter"
_LONG_INSERTER = "long-handed-inserter"

# Inserter tiers by belt<->machine throughput (items/min, approximate). Used to pick a single
# inserter fast enough for a link instead of a base inserter that bottlenecks high-rate flows (the
# vanilla-reference technique: high-rate intermediate links use fast/bulk inserters).
_INSERTER_TIERS: list[tuple[str, float]] = [
    ("inserter", 57.0),
    ("fast-inserter", 138.0),
    ("bulk-inserter", 280.0),
]


def _inserter_for_rate(rate_per_min: float, available: set[str] | None) -> str:
    """Cheapest available inserter whose throughput covers ``rate_per_min``; else the fastest
    available (the placer may add a 2nd inserter if even that is short)."""
    avail = available if available else {"inserter"}
    usable = [(n, t) for n, t in _INSERTER_TIERS if n in avail]
    if not usable:
        usable = [("inserter", 57.0)]
    for name, tput in usable:
        if tput >= rate_per_min:
            return name
    return usable[-1][0]

# Lane offsets from a machine-row centre line (validator-safe for 3x3 and 2x2 machines):
#   primary input lane   : row_y - 3   (normal inserter at row_y - 2)
#   secondary input lane : row_y - 4   (long-handed inserter at row_y - 2, reach 2)
#   output lane          : row_y + 3   (normal inserter at row_y + 2)
_PRIMARY_IN_DY = -3
_SECONDARY_IN_DY = -4
_OUT_DY = 3
_INSERTER_IN_DY = -2
_INSERTER_OUT_DY = 2

# Items a base inserter moves per minute (paper uses ~50; a yellow inserter belt<->machine is
# ~0.9/s). Used only to size how many inserters per ingredient match the recipe ratio.
_INSERTER_ITEMS_PER_MIN = 57.0

# Spare margin so lanes visibly enter/leave the boundary (the "source"/"destination" tiles).
_BOUNDARY_MARGIN = 2


@dataclass
class BoundingBox:
    width: float
    height: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlacedCell:
    entities: list[dict[str, Any]]
    fits: bool
    used_bounds: dict[str, float]
    required_box: dict[str, float]  # minimum W x H the cell actually needs
    io_corridors: list[dict[str, Any]]
    pole: str
    power_coverage_ok: bool
    warnings: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)       # boundary inputs (per item)
    destination: dict[str, Any] | None = None                         # boundary output (product)
    connectivity_ok: bool = True                                      # every machine wired in+out
    archetype: str = "belt_row"                                       # which layout template produced this

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _add(entities: list[dict[str, Any]], name: str, x: float, y: float, *, direction: int | None = None,
         recipe: str | None = None, items: dict[str, Any] | None = None) -> None:
    entity: dict[str, Any] = {"name": name, "position": {"x": float(x), "y": float(y)}}
    if direction is not None:
        entity["direction"] = direction
    if recipe:
        entity["recipe"] = recipe
    if items:
        entity["items"] = items
    entities.append(entity)


def _modules_to_items(modules: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in modules:
        out[m] = out.get(m, 0) + 1
    return out


@dataclass
class _Stage:
    recipe: str | None
    machine: str
    count: int
    product: str
    modules: list[str]
    inputs: list[str]            # solid ingredient items (fluids handled separately/deferred)
    input_rates: dict[str, float]  # items/min consumed by the WHOLE stage, per item
    is_furnace: bool


def _stages(spec: CellSpec) -> list[_Stage]:
    """Sub-stages (co-located intermediates) first, then the main product. Each stage's solid
    inputs + per-item consumption rate come from the recipe scaled to the stage's craft rate."""
    out: list[_Stage] = []
    for sub in spec.substages:
        out.append(_make_stage(sub.recipe_name, sub.machine, sub.machine_count, sub.item, [], sub.rate_per_minute))
    out.append(_make_stage(spec.recipe_name, spec.machine, spec.machine_count, spec.target_item,
                           spec.modules, spec.achieved_rate))
    return out


def _make_stage(recipe_name: str | None, machine: str, count: int, product: str,
                modules: list[str], output_per_minute: float) -> _Stage:
    recipe = knowledge.recipe_for_product(product)
    inputs: list[str] = []
    rates: dict[str, float] = {}
    if recipe is not None:
        product_amount = float(recipe.products.get(product) or 1.0) or 1.0
        crafts_per_min = output_per_minute / product_amount if product_amount else output_per_minute
        for item, amount in recipe.ingredients.items():
            if knowledge.is_fluid(item):
                continue
            inputs.append(item)
            rates[item] = crafts_per_min * amount
    return _Stage(recipe_name, machine, max(1, count), product, list(modules), inputs, rates,
                  "furnace" in machine)


def _inserter_count(rate_per_min: float, machine_count: int) -> int:
    """Inserters needed on ONE machine to move ``rate_per_min`` of an item (paper: ratio-matched)."""
    per_machine = rate_per_min / max(1, machine_count)
    return max(1, ceil(per_machine / _INSERTER_ITEMS_PER_MIN))


def place_cell(
    spec: CellSpec,
    box: BoundingBox | None = None,
    *,
    pole: str = "small-electric-pole",
    long_inserter_available: bool = True,
    available_inserters: set[str] | None = None,
    # kept for backwards compatibility (ignored: the structured router fixes I/O sides):
    input_sides: list[int] | None = None,
    output_sides: list[int] | None = None,
) -> PlacedCell:
    """Place ``spec`` as a connected cell. Returns the first applicable archetype (most specific
    first); use :func:`place_cell_candidates` to get all of them for sandbox-judged selection."""
    candidates = place_cell_candidates(spec, box, pole=pole, long_inserter_available=long_inserter_available,
                                       available_inserters=available_inserters)
    return candidates[0] if candidates else _place_belt_row(
        spec, box, pole=pole, long_inserter_available=long_inserter_available)


def place_cell_candidates(
    spec: CellSpec,
    box: BoundingBox | None = None,
    *,
    pole: str = "small-electric-pole",
    long_inserter_available: bool = True,
    available_inserters: set[str] | None = None,
) -> list[PlacedCell]:
    """Generate the applicable layout archetypes for ``spec`` (most-specific first), each tagged with
    its ``archetype``. The pipeline prechecks + ranks these and lets the sandbox pick the winner."""
    if not spec.ok or not spec.machine:
        return [PlacedCell([], False, {}, {"width": 0, "height": 0}, [], pole, False, ["cell spec is not ok"])]
    out: list[PlacedCell] = []
    di = _place_direct_insertion(spec, box, pole=pole, long_inserter_available=long_inserter_available,
                                 available_inserters=available_inserters)
    if di is not None:
        out.append(di)
    # belt_row is the general fallback and always applicable.
    sm = _place_smelting_column(spec, box, pole=pole, available_inserters=available_inserters)
    if sm is not None:
        out.append(sm)
    out.append(_place_belt_row(spec, box, pole=pole, long_inserter_available=long_inserter_available))
    return out


def _place_smelting_column(
    spec: CellSpec,
    box: BoundingBox | None = None,
    *,
    pole: str = "small-electric-pole",
    available_inserters: set[str] | None = None,
) -> PlacedCell | None:
    """Smelting archetype for 2x2 burner furnaces (stone/steel). A row of furnaces shares an ore+coal
    input belt to the NORTH and emits plates on a SOUTH output belt. Geometry confirmed by live RCON:
    a 2x2 furnace at integer centre (cx,cy) occupies tiles {cx-1,cx}x{cy-1,cy}; the input inserter at
    (cx-1, cy-2) NORTH drops into it from a belt at cy-3, and the output inserter at (cx-1, cy+1)
    NORTH picks from it onto a belt at cy+2. Burner fuel (coal) rides the input belt's second lane
    (the half-lane technique) — the boundary source supplies ore on one lane + coal on the other.

    Electric furnaces (3x3) fall through to belt_row. Returns None when not a 2x2 furnace cell."""
    main = _stages(spec)[-1]
    if not main.is_furnace:
        return None
    prof = knowledge.machine_profile(main.machine)
    if prof is None or int(prof.tile_width) != 2:
        return None  # 2x2 burner furnaces only; electric-furnace (3x3) uses belt_row
    recipe = knowledge.recipe_for_product(spec.target_item)
    ore = next((i for i in (recipe.ingredients if recipe else {})
                if not knowledge.is_fluid(i) and i != "coal"), None)
    if ore is None:
        return None
    has_coal = any(getattr(i, "item", None) == "coal" for i in spec.inputs)
    n = max(1, main.count)
    per_furnace_out = spec.achieved_rate / n
    per_furnace_ore = (main.input_rates.get(ore, spec.achieved_rate)) / n
    ore_ins = _inserter_for_rate(per_furnace_ore, available_inserters)
    out_ins = _inserter_for_rate(per_furnace_out, available_inserters)

    entities: list[dict[str, Any]] = []
    machine_centers: list[tuple[float, float, float, float]] = []
    sources: list[dict[str, Any]] = []
    io_corridors: list[dict[str, Any]] = []
    warnings: list[str] = []

    inner_x1 = 2 * (n - 1) + 1
    west_x = -4
    east_x = inner_x1 + 4
    for k in range(n):
        cx = 2 * k + 1  # furnace tiles {2k, 2k+1} (packed adjacent, pitch 2)
        _add(entities, main.machine, cx, 0)  # furnace: no recipe (auto-smelts the belt's ore)
        machine_centers.append((float(cx), 0.0, 2, 2))
        _add(entities, ore_ins, cx - 1, -2, direction=NORTH)   # input belt (cx-1,-3) -> furnace
        _add(entities, out_ins, cx - 1, 1, direction=NORTH)    # furnace -> output belt (cx-1,+2)

    # input belt (ore + coal on its two lanes) along the north, extended to the west boundary source.
    _lay_lane(entities, -3, west_x, inner_x1, item=ore)
    sources.append({"item": ore, "x": west_x, "y": -3})
    io_corridors.append({"role": "input", "item": ore, "x": west_x, "y": -3, "side": "west"})
    if has_coal:
        # coal shares the input belt's second lane (half-lane); the boundary supplies it.
        sources.append({"item": "coal", "x": west_x, "y": -3, "lane": "half"})
        io_corridors.append({"role": "input", "item": "coal", "x": west_x, "y": -3, "side": "west", "note": "half-lane"})

    out_y = 2
    _lay_lane(entities, out_y, -1, east_x, item=spec.target_item)
    destination = {"item": spec.target_item, "x": east_x, "y": out_y, "rate": spec.achieved_rate}
    io_corridors.append({"role": "output", "item": spec.target_item, "x": east_x, "y": out_y, "side": "east"})

    return _finalize(entities, machine_centers, sources, destination, io_corridors,
                     warnings, True, box, pole, archetype="smelting_column")


def _place_direct_insertion(
    spec: CellSpec,
    box: BoundingBox | None = None,
    *,
    pole: str = "small-electric-pole",
    long_inserter_available: bool = True,
    available_inserters: set[str] | None = None,
) -> PlacedCell | None:
    """Direct-insertion archetype for a single consumer fed by a co-located intermediate (the
    electronic-circuit shape): the intermediate's producers FLANK the consumer and an inserter moves
    the intermediate straight in (no belt -> no flow-direction bug). The raw input enters on a north
    belt and the product leaves on a south belt (all boundary I/O is belts, no chests).

    Generalised to N consumer machines: each consumer is its own unit (consumer + flanking producers
    + north raw belt + south product belt), stacked vertically. Applies when there is one
    intermediate from <=2 flanking producers per consumer and <=1 extra raw input, all 3x3
    assemblers; returns None otherwise so a more general archetype runs."""
    if len(spec.substages) != 1:
        return None
    main = _stages(spec)[-1]
    sub = spec.substages[0]
    if main.is_furnace or "furnace" in str(sub.machine or ""):
        return None
    if _machine_w(main.machine) != 3 or _machine_w(sub.machine) != 3:
        return None
    main_recipe = knowledge.recipe_for_product(spec.target_item)
    if main_recipe is None:
        return None
    solid_ing = [i for i in main_recipe.ingredients if not knowledge.is_fluid(i)]
    if sub.item not in solid_ing:
        return None
    raws = [i for i in solid_ing if i != sub.item]
    if len(raws) > 1:
        return None
    raw_item = raws[0] if raws else None
    sub_recipe = knowledge.recipe_for_product(sub.item)
    sub_inputs = [i for i in (sub_recipe.ingredients if sub_recipe else {}) if not knowledge.is_fluid(i)]
    if len(sub_inputs) != 1:
        return None
    sub_in = sub_inputs[0]

    n_ec = max(1, main.count)
    # Flanking only makes sense when producers >= consumers (each consumer gets >=1 dedicated
    # producer). With far fewer producers than consumers (e.g. 1 gear for 8 science) flanking would
    # massively over-produce, so defer to belt_row's shared intermediate lane.
    if sub.machine_count < n_ec:
        return None
    flank = min(2, max(1, ceil(sub.machine_count / n_ec)))  # producers flanking each consumer (<=2)
    # Per-link flow rates -> pick an inserter tier fast enough for each link.
    cable_per_inserter = sub.rate_per_minute / max(1, sub.machine_count)
    iron_rate = (main.input_rates.get(raw_item, 0.0) / n_ec) if raw_item else 0.0
    sub_prod_amt = float(sub_recipe.products.get(sub.item) or 1.0) if sub_recipe else 1.0
    copper_per_amt = float(sub_recipe.ingredients.get(sub_in) or 1.0) if sub_recipe else 1.0
    copper_per_inserter = (sub.rate_per_minute / sub_prod_amt * copper_per_amt) / max(1, sub.machine_count)
    out_rate = spec.achieved_rate / n_ec
    cable_ins = _inserter_for_rate(cable_per_inserter, available_inserters)
    copper_ins = _inserter_for_rate(copper_per_inserter, available_inserters)
    iron_ins = _inserter_for_rate(iron_rate, available_inserters)
    out_ins = _inserter_for_rate(out_rate, available_inserters)

    entities: list[dict[str, Any]] = []
    machine_centers: list[tuple[float, float, float, float]] = []
    sources: list[dict[str, Any]] = []
    io_corridors: list[dict[str, Any]] = []
    warnings: list[str] = []
    ecx = 4
    west_x = -6
    east_x = ecx + 10  # 14
    pitch_y = 8  # one unit spans ry-3..ry+3 (7 tall) + a 1-tile gap
    modules_items = _modules_to_items(main.modules) or None
    destination: dict[str, Any] | None = None

    for k in range(n_ec):
        ry = k * pitch_y
        _add(entities, main.machine, ecx, ry, recipe=main.recipe, items=modules_items)
        machine_centers.append((float(ecx), float(ry), 3, 3))
        cable_xs = [0] + ([8] if flank == 2 else [])
        for cx in cable_xs:
            _add(entities, sub.machine, cx, ry, recipe=sub.recipe_name)
            machine_centers.append((float(cx), float(ry), 3, 3))
            if cx < ecx:  # west producer -> direct insert east into the consumer; copper from west belt
                _add(entities, cable_ins, 2, ry, direction=_WEST_DIR)
                _lay_lane(entities, ry, west_x, cx - 3, item=sub_in)
                _add(entities, copper_ins, cx - 2, ry, direction=_WEST_DIR)
                sources.append({"item": sub_in, "x": west_x, "y": ry})
                io_corridors.append({"role": "input", "item": sub_in, "x": west_x, "y": ry, "side": "west"})
            else:  # east producer -> direct insert west; copper from east belt
                _add(entities, cable_ins, 6, ry, direction=_EAST_DIR)
                _lay_lane(entities, ry, cx + 3, east_x, item=sub_in, flow_west=True)
                _add(entities, copper_ins, cx + 2, ry, direction=_EAST_DIR)
                sources.append({"item": sub_in, "x": east_x, "y": ry})
                io_corridors.append({"role": "input", "item": sub_in, "x": east_x, "y": ry, "side": "east"})
        if raw_item is not None:
            _lay_lane(entities, ry - 3, west_x, ecx, item=raw_item)
            _add(entities, iron_ins, ecx, ry - 2, direction=NORTH)
            sources.append({"item": raw_item, "x": west_x, "y": ry - 3})
            io_corridors.append({"role": "input", "item": raw_item, "x": west_x, "y": ry - 3, "side": "north"})
        out_y = ry + 3
        _lay_lane(entities, out_y, ecx, east_x, item=main.product)
        _add(entities, out_ins, ecx, ry + 2, direction=NORTH)
        io_corridors.append({"role": "output", "item": main.product, "x": east_x, "y": out_y, "side": "east"})
        if destination is None:
            destination = {"item": main.product, "x": east_x, "y": out_y, "rate": spec.achieved_rate}

    if flank * n_ec < sub.machine_count:
        warnings.append(f"{spec.target_item}: {flank} producer(s)/consumer may underfeed; sandbox confirms rate")

    return _finalize(entities, machine_centers, sources, destination, io_corridors,
                     warnings, True, box, pole, archetype="direct_insertion")


def _place_belt_row(
    spec: CellSpec,
    box: BoundingBox | None = None,
    *,
    pole: str = "small-electric-pole",
    long_inserter_available: bool = True,
) -> PlacedCell:
    """Stacked-row archetype: each stage a row, continuous single-item belt lanes, boundary I/O."""

    if not spec.ok or not spec.machine:
        return PlacedCell([], False, {}, {"width": 0, "height": 0}, [], pole, False, ["cell spec is not ok"])

    stages = _stages(spec)
    warnings: list[str] = []
    entities: list[dict[str, Any]] = []
    machine_centers: list[tuple[float, float, float, float]] = []
    sources: list[dict[str, Any]] = []
    io_corridors: list[dict[str, Any]] = []
    connectivity_ok = True

    main = stages[-1]
    subs = stages[:-1]
    intermediate_items = {s.product for s in subs}

    # --- x layout: every stage row is left-aligned at the same column origin; the widest row sets
    # the inner extent. Lanes run horizontally across the full inner width and out to the boundary.
    mw = _machine_w(main.machine)
    pitch_x = mw + 1
    inner_x0 = 0.0
    max_cols = max(s.count for s in stages)
    inner_x1 = inner_x0 + (max_cols - 1) * pitch_x  # x of the last machine centre in the widest row

    # --- y layout: sub-stage rows stack above the main row. A sub's output inserters sit at
    # sub_y+2 and drop onto a lane at sub_y+3, which is exactly one of the main's two north input
    # lanes (primary = main_y-3 normal-inserter; secondary = main_y-4 long-inserter). To keep the
    # sub's output-inserter row (sub_y+2) from colliding with the main's *other* north lane, we
    # route the INTERMEDIATE onto the secondary lane (main_y = last_sub_y + 7 so main_y-4 = sub_y+3)
    # and the extra RAW input onto the primary lane; with only the intermediate, the gap is 6 and it
    # uses the primary (normal) lane. No sub -> the main is the only row.
    main_inputs = list(main.inputs)
    if len(main_inputs) > 2:
        warnings.append(f"{main.recipe} has {len(main_inputs)} solid inputs; v1 routes 2 cleanly")
    raw_inputs = [i for i in main_inputs if i not in intermediate_items]
    inter_inputs = [i for i in main_inputs if i in intermediate_items]

    sub_gap = 7
    sub_positions: list[tuple[_Stage, float]] = []
    sy = 0.0
    for sub in subs:
        sub_positions.append((sub, sy))
        sy += sub_gap
    last_sub_y = sub_positions[-1][1] if sub_positions else None

    if subs:
        main_y = last_sub_y + (7 if len(main_inputs) >= 2 else 6)
    else:
        main_y = 0.0
    primary_y = main_y + _PRIMARY_IN_DY    # main_y - 3 (normal inserter)
    secondary_y = main_y + _SECONDARY_IN_DY  # main_y - 4 (long-handed inserter)

    west_x = inner_x0 - 4 - _BOUNDARY_MARGIN
    east_x = inner_x1 + 4 + _BOUNDARY_MARGIN

    # Assign main north lanes (item -> (lane_y, needs_long)).
    lane_y: dict[str, float] = {}
    in_lane_specs: list[tuple[str, float, bool]] = []
    if subs and len(main_inputs) >= 2:
        inter = inter_inputs[0] if inter_inputs else main_inputs[0]
        lane_y[inter] = secondary_y          # = last_sub_y + 3 (the sub's output drop)
        in_lane_specs.append((inter, secondary_y, True))
        raw0 = raw_inputs[0] if raw_inputs else None
        if raw0 is not None:
            lane_y[raw0] = primary_y
            in_lane_specs.append((raw0, primary_y, False))
    elif subs:
        inter = inter_inputs[0] if inter_inputs else main_inputs[0]
        lane_y[inter] = primary_y            # = last_sub_y + 3 (gap 6)
        in_lane_specs.append((inter, primary_y, False))
    else:
        if main_inputs:
            lane_y[main_inputs[0]] = primary_y
            in_lane_specs.append((main_inputs[0], primary_y, False))
        if len(main_inputs) >= 2:
            lane_y[main_inputs[1]] = secondary_y
            in_lane_specs.append((main_inputs[1], secondary_y, True))

    # --- lay sub-stage rows: raw input lane to the north (boundary source); the intermediate
    # output lane is the main north lane the sub feeds (sub_y+3). ---------------------------------
    for sub, syi in sub_positions:
        sub_in = sub.inputs[0] if sub.inputs else None
        in_lane_y = syi + _PRIMARY_IN_DY
        out_lane_y = syi + _OUT_DY  # == the main lane carrying sub.product (by construction above)
        if sub_in is not None:
            _lay_lane(entities, in_lane_y, west_x, inner_x1, item=sub_in)
            sources.append({"item": sub_in, "x": west_x, "y": in_lane_y, "rate": sub.input_rates.get(sub_in)})
            io_corridors.append({"role": "input", "item": sub_in, "x": west_x, "y": in_lane_y, "side": "west"})
        _lay_lane(entities, out_lane_y, inner_x0 - 1, inner_x1 + 1, item=sub.product)
        ok = _lay_machine_row(entities, machine_centers, sub, syi, pitch_x,
                              in_lanes=[(sub_in, in_lane_y, False)] if sub_in else [],
                              out_lane_y=out_lane_y, long_ok=long_inserter_available, warnings=warnings)
        connectivity_ok = connectivity_ok and ok

    # --- lay the main row: raw input lanes to the west boundary (sources); product lane to the
    # east boundary (the single destination). Intermediate lanes were already laid by the subs. --
    for item, ly, _needs_long in in_lane_specs:
        if item in intermediate_items:
            continue
        _lay_lane(entities, ly, west_x, inner_x1, item=item)
        sources.append({"item": item, "x": west_x, "y": ly, "rate": main.input_rates.get(item)})
        io_corridors.append({"role": "input", "item": item, "x": west_x, "y": ly, "side": "west"})

    out_lane_y = main_y + _OUT_DY
    _lay_lane(entities, out_lane_y, inner_x0 - 1, east_x, item=main.product)
    destination = {"item": main.product, "x": east_x, "y": out_lane_y, "rate": spec.achieved_rate}
    io_corridors.append({"role": "output", "item": main.product, "x": east_x, "y": out_lane_y, "side": "east"})

    ok = _lay_machine_row(entities, machine_centers, main, main_y, pitch_x,
                          in_lanes=in_lane_specs, out_lane_y=out_lane_y,
                          long_ok=long_inserter_available, warnings=warnings)
    connectivity_ok = connectivity_ok and ok

    return _finalize(entities, machine_centers, sources, destination, io_corridors,
                     warnings, connectivity_ok, box, pole, archetype="belt_row")


def _finalize(
    entities: list[dict[str, Any]],
    machine_centers: list[tuple[float, float, float, float]],
    sources: list[dict[str, Any]],
    destination: dict[str, Any] | None,
    io_corridors: list[dict[str, Any]],
    warnings: list[str],
    connectivity_ok: bool,
    box: BoundingBox | None,
    pole: str,
    *,
    archetype: str,
) -> PlacedCell:
    """Shared tail for every archetype: place power poles on free tiles, compute bounds + box fit."""
    occupied = _occupied_tiles(entities)
    pole_entities, coverage_ok, pole_warn = _place_poles(machine_centers, pole, occupied)
    entities.extend(pole_entities)
    warnings = list(warnings) + list(pole_warn)
    used = _bounds(entities)
    req_w = round(used["max_x"] - used["min_x"] + 1, 1)
    req_h = round(used["max_y"] - used["min_y"] + 1, 1)
    required_box = {"width": req_w, "height": req_h}
    fits = True
    if box is not None:
        fits = req_w <= box.width and req_h <= box.height
        if not fits:
            warnings.append(f"cell needs {req_w}x{req_h} but box is {box.width}x{box.height}")
    if not connectivity_ok:
        warnings.append("a machine could not be fully wired (input/output inserter shortfall)")
    return PlacedCell(entities, fits, used, required_box, io_corridors, pole, coverage_ok,
                      warnings, sources=sources, destination=destination,
                      connectivity_ok=connectivity_ok, archetype=archetype)


def _machine_w(machine: str) -> int:
    prof = knowledge.machine_profile(machine)
    return prof.tile_width if prof else 3


def _lay_lane(entities: list[dict[str, Any]], y: float, x_start: float, x_end: float, *, item: str,
              flow_west: bool = False) -> None:
    """Lay a continuous horizontal transport-belt lane from x_start..x_end at row y.

    A single continuous lane carries ONE item type (the paper's per-tile single-item rule) and gives
    every inserter along it a real belt tile to pick from / drop onto. ``flow_west`` reverses the
    belt direction so an intermediate lane delivers its producer's output toward consumers sitting at
    the west of the row (otherwise items run off the east end un-consumed)."""
    x0 = int(round(min(x_start, x_end)))
    x1 = int(round(max(x_start, x_end)))
    direction = _WEST_DIR if flow_west else _EAST_DIR
    for x in range(x0, x1 + 1):
        _add(entities, _BELT, x, y, direction=direction)


def _lay_machine_row(
    entities: list[dict[str, Any]],
    machine_centers: list[tuple[float, float, float, float]],
    stage: _Stage,
    row_y: float,
    pitch_x: float,
    *,
    in_lanes: list[tuple[str, float, bool]],   # (item, lane_y, needs_long_handed)
    out_lane_y: float,
    long_ok: bool,
    warnings: list[str],
) -> bool:
    """Place a row of identical machines and wire each one: input inserters from the north lane(s)
    and output inserter(s) to the south lane. Returns False if any machine could not be fully wired
    (every machine needs >=1 input inserter per ingredient and >=1 output inserter)."""
    prof = knowledge.machine_profile(stage.machine)
    mw = prof.tile_width if prof else 3
    mh = prof.tile_height if prof else 3
    recipe_field = None if stage.is_furnace else stage.recipe
    modules_items = _modules_to_items(stage.modules) or None
    all_wired = True

    for col in range(stage.count):
        cx = float(col * pitch_x)
        cy = float(row_y)
        machine_centers.append((cx, cy, mw, mh))
        _add(entities, stage.machine, cx, cy, recipe=recipe_field, items=modules_items)

        # North input inserters. Slots along the machine's north edge: cx-1, cx, cx+1.
        # RESERVE one slot per ingredient first (so every input is wired = connected), then hand
        # leftover slots to the highest-demand items to approach the recipe ratio (paper Stage 1).
        north_slots = [cx - 1, cx, cx + 1]
        feasible = [(item, ly, nl) for (item, ly, nl) in in_lanes if long_ok or not nl]
        for item, ly, needs_long in in_lanes:
            if needs_long and not long_ok and (item, ly, needs_long) not in feasible:
                all_wired = False
                warnings.append(
                    f"{stage.product}: '{item}' needs a long-handed inserter (research long-inserters) "
                    f"to reach its belt lane; left unwired"
                )
        desired = {item: _inserter_count(stage.input_rates.get(item, 0.0), stage.count)
                   for (item, ly, nl) in feasible}
        alloc = {item: (1 if feasible else 0) for (item, ly, nl) in feasible}
        remaining = len(north_slots) - len(feasible)
        # distribute remaining slots to the items still short of their desired count, by demand.
        order = sorted(feasible, key=lambda t: -desired[t[0]])
        i = 0
        while remaining > 0 and order:
            item = order[i % len(order)][0]
            if alloc[item] < desired[item]:
                alloc[item] += 1
                remaining -= 1
            elif all(alloc[it] >= desired[it] for it, _, _ in feasible):
                break
            i += 1
            if i > 64:  # safety
                break
        slot_i = 0
        for item, ly, needs_long in feasible:
            for _ in range(alloc[item]):
                if slot_i >= len(north_slots):
                    break
                slot_x = north_slots[slot_i]
                slot_i += 1
                _add(entities, _LONG_INSERTER if needs_long else _INSERTER,
                     slot_x, cy + _INSERTER_IN_DY, direction=NORTH)
            if alloc[item] < desired[item]:
                warnings.append(
                    f"{stage.product}: '{item}' wants {desired[item]} inserters/machine but only "
                    f"{alloc[item]} fit (throughput may be inserter-limited; sandbox will confirm)"
                )

        # South output inserter(s) -> output lane.
        out_count = _inserter_count(_stage_output_rate(stage), stage.count)
        out_slots = [cx, cx - 1, cx + 1]
        placed_out = 0
        for i in range(min(out_count, len(out_slots))):
            _add(entities, _INSERTER, out_slots[i], cy + _INSERTER_OUT_DY, direction=NORTH)
            placed_out += 1
        if placed_out == 0:
            all_wired = False

    return all_wired


def _stage_output_rate(stage: _Stage) -> float:
    # crafts/min * product amount ~ approximated by max input rate / typical ratio; use the largest
    # input rate as a proxy for output flow (good enough to size output inserters).
    if stage.input_rates:
        return max(stage.input_rates.values())
    return float(stage.count) * _INSERTER_ITEMS_PER_MIN


def _entity_tiles(name: str, x: float, y: float) -> set[tuple[int, int]]:
    """Integer tiles an entity at centre (x,y) occupies (odd sizes centre on a tile, even sizes on
    a tile corner). Belts/inserters/poles are 1x1."""
    xi, yi = int(round(x)), int(round(y))
    prof = knowledge.machine_profile(name)
    w = prof.tile_width if prof else 1
    h = prof.tile_height if prof else 1
    xr = range(xi - w // 2, xi + w // 2 + 1) if w % 2 else range(xi - w // 2, xi + w // 2)
    yr = range(yi - h // 2, yi + h // 2 + 1) if h % 2 else range(yi - h // 2, yi + h // 2)
    return {(px, py) for px in xr for py in yr}


def _occupied_tiles(entities: list[dict[str, Any]]) -> set[tuple[int, int]]:
    occ: set[tuple[int, int]] = set()
    for e in entities:
        pos = e.get("position")
        if isinstance(pos, dict):
            occ |= _entity_tiles(str(e.get("name") or ""), pos.get("x", 0), pos.get("y", 0))
    return occ


def _place_poles(
    machines: list[tuple[float, float, float, float]],
    pole: str,
    occupied: set[tuple[int, int]],
) -> tuple[list[dict[str, Any]], bool, list[str]]:
    """Cover every machine with a pole placed on a FREE tile (never on a belt/inserter/machine),
    then add bridge poles so the network is wire-connected. Coverage-driven, not a blind grid."""
    if not machines:
        return [], True, []
    prof = knowledge.pole_profile(pole) or knowledge.pole_profile("small-electric-pole")
    warnings: list[str] = []
    sr = prof.supply_radius
    poles: list[tuple[int, int]] = []
    pole_tiles: set[tuple[int, int]] = set()

    def is_free(px: int, py: int) -> bool:
        return (px, py) not in occupied and (px, py) not in pole_tiles

    def covers(px: int, py: int, cx: float, cy: float, w: float, h: float) -> bool:
        return abs(px - cx) <= sr + w / 2 and abs(py - cy) <= sr + h / 2

    def machine_covered(cx: float, cy: float, w: float, h: float) -> bool:
        return any(covers(px, py, cx, cy, w, h) for px, py in poles)

    # 1. coverage: one pole per uncovered machine, on the nearest free tile within supply range.
    reach = int(sr + 2)
    for cx, cy, w, h in machines:
        if machine_covered(cx, cy, w, h):
            continue
        cands = sorted(
            ((dx, dy) for dx in range(-reach, reach + 1) for dy in range(-reach, reach + 1)),
            key=lambda d: abs(d[0]) + abs(d[1]),
        )
        for dx, dy in cands:
            px, py = int(round(cx)) + dx, int(round(cy)) + dy
            if covers(px, py, cx, cy, w, h) and is_free(px, py):
                poles.append((px, py))
                pole_tiles.add((px, py))
                break
        else:
            warnings.append(f"no free tile to power machine at ({cx:g},{cy:g})")

    # 2. connectivity: while the pole graph is disconnected, add a bridge pole on a free tile that
    # links the two nearest poles from different components.
    for _ in range(len(poles) * 2 + 4):
        comps = _pole_components(poles, prof.wire_reach)
        if len(comps) <= 1:
            break
        added = _add_bridge_pole(poles, comps, prof.wire_reach, is_free)
        if added is None:
            break
        poles.append(added)
        pole_tiles.add(added)

    entities = [{"name": pole, "position": {"x": float(px), "y": float(py)}} for px, py in poles]
    coverage_ok = _coverage_ok(machines, poles, prof) and _connectivity_ok(poles, prof.wire_reach)
    if not coverage_ok:
        warnings.append("power-pole coverage/connectivity check failed")
    return entities, coverage_ok, warnings


def _pole_components(poles: list[tuple[int, int]], wire_reach: float) -> list[list[int]]:
    n = len(poles)
    seen: set[int] = set()
    comps: list[list[int]] = []
    for start in range(n):
        if start in seen:
            continue
        comp = [start]
        seen.add(start)
        frontier = [start]
        while frontier:
            i = frontier.pop()
            for j in range(n):
                if j in seen:
                    continue
                if hypot(poles[i][0] - poles[j][0], poles[i][1] - poles[j][1]) <= wire_reach:
                    seen.add(j)
                    comp.append(j)
                    frontier.append(j)
        comps.append(comp)
    return comps


def _add_bridge_pole(poles, comps, wire_reach, is_free):
    """Find a free tile that connects two components (within wire reach of a pole in each)."""
    best: tuple[int, int] | None = None
    best_d = float("inf")
    a, b = comps[0], comps[1]
    for i in a:
        for j in b:
            mx = (poles[i][0] + poles[j][0]) // 2
            my = (poles[i][1] + poles[j][1]) // 2
            for dx in range(-3, 4):
                for dy in range(-3, 4):
                    px, py = mx + dx, my + dy
                    if not is_free(px, py):
                        continue
                    di = hypot(px - poles[i][0], py - poles[i][1])
                    dj = hypot(px - poles[j][0], py - poles[j][1])
                    if di <= wire_reach and dj <= wire_reach and (di + dj) < best_d:
                        best_d = di + dj
                        best = (px, py)
    return best


def _coverage_ok(machines: list[tuple[float, float, float, float]], poles: list[tuple[float, float]],
                 prof: knowledge.PoleProfile) -> bool:
    for cx, cy, w, h in machines:
        if not any(
            (abs(px - cx) <= prof.supply_radius + w / 2) and (abs(py - cy) <= prof.supply_radius + h / 2)
            for px, py in poles
        ):
            return False
    return True


def _connectivity_ok(poles: list[tuple[float, float]], wire_reach: float) -> bool:
    if len(poles) <= 1:
        return True
    seen = {0}
    frontier = [0]
    while frontier:
        i = frontier.pop()
        for j, (px, py) in enumerate(poles):
            if j in seen:
                continue
            if hypot(px - poles[i][0], py - poles[i][1]) <= wire_reach:
                seen.add(j)
                frontier.append(j)
    return len(seen) == len(poles)


def _bounds(entities: list[dict[str, Any]]) -> dict[str, float]:
    xs = [float(e["position"]["x"]) for e in entities if isinstance(e.get("position"), dict)]
    ys = [float(e["position"]["y"]) for e in entities if isinstance(e.get("position"), dict)]
    if not xs:
        return {"min_x": 0, "max_x": 0, "min_y": 0, "max_y": 0}
    return {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)}
