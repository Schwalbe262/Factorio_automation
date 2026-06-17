"""Parametric placement of a compiled production cell into a 2D blueprint.

Takes a :class:`factorio_ai.cell_compiler.CellSpec` plus a bounding box (the W x H budget the LLM
hands down, constraint C5) and lays the machines, I/O belt lanes, inserters, and POWER POLES into
a concrete blueprint entity list. Modular single product (C1), reserved I/O corridors + train pad
(C4), and full power-pole coverage (the user's requirement: every powered machine overlaps a
pole's supply area, and poles are within wire reach so the network is connected).

v1 uses deterministic parametric templates (no general solver). The geometry follows the proven
green-circuit / smelting generators in ``planner.py`` and the validator's coordinate model
(machine position = centre, 3x3 footprint = centre +/-1.5; inserter pickup/drop at integer
offsets) so output passes ``planner._blueprint_operability_report`` and, in turn, the real
Factorio sandbox.

Pure module: returns entities; no RCON / file I/O.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import ceil, hypot
from typing import Any

from . import knowledge
from .cell_compiler import CellSpec
from .monitor import EAST, NORTH, SOUTH, WEST

# A solid machine "tile" is 3x3 centred on an integer; neighbours need a 1-tile belt/inserter
# gutter, so the per-machine pitch is 4 along a row and 7 across an aisle (machine + belt + machine).
_MACHINE_PITCH_X = 7
_MACHINE_PITCH_Y = 4
_BELT_OFFSET = 3   # belt sits 3 tiles from the machine centre (validator: pickup at centre+/-3)
_INSERTER_OFFSET = 2


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _add(entities: list[dict[str, Any]], name: str, x: float, y: float, *, direction: int | None = None,
         recipe: str | None = None, items: dict[str, Any] | None = None) -> None:
    entity: dict[str, Any] = {"name": name, "position": {"x": x, "y": y}}
    if direction is not None:
        entity["direction"] = direction
    if recipe:
        entity["recipe"] = recipe
    if items:
        entity["items"] = items
    entities.append(entity)


def _belt_name(io_rate: Any, default: str = "transport-belt") -> str:
    tier = getattr(io_rate, "belt_tier", None)
    return tier or default


def _machine_stages(spec: CellSpec) -> list[dict[str, Any]]:
    """Flatten the cell into placement stages: co-located sub-stages first (they feed the main
    stage), then the main product. Each stage is a homogeneous group of machines on one recipe."""
    stages: list[dict[str, Any]] = []
    for sub in spec.substages:
        stages.append({"recipe": sub.recipe_name, "machine": sub.machine, "count": sub.machine_count,
                       "modules": [], "product": sub.item})
    stages.append({"recipe": spec.recipe_name, "machine": spec.machine, "count": spec.machine_count,
                   "modules": spec.modules, "product": spec.target_item})
    return stages


def place_cell(
    spec: CellSpec,
    box: BoundingBox | None = None,
    *,
    pole: str = "small-electric-pole",
    input_sides: list[int] | None = None,
    output_sides: list[int] | None = None,
) -> PlacedCell:
    """Place ``spec`` into ``box`` (or an auto-sized box). Returns the blueprint entities plus a
    fit report and a power-coverage report."""

    warnings: list[str] = []
    if not spec.ok or not spec.machine:
        return PlacedCell([], False, {}, {"width": 0, "height": 0}, [], pole, False, ["cell spec is not ok"])

    stages = _machine_stages(spec)
    entities: list[dict[str, Any]] = []

    # Lay each stage as a row of machines; stages stack vertically (sub-stage rows above the main).
    # Inputs enter on the WEST belt lane, a second input on the EAST lane, output drops to the
    # SOUTH belt lane of each machine. This per-machine pattern is validator-safe for <=2 solid
    # inputs; >2 solid inputs are flagged (rare; deferred to the general solver).
    row_y = 0
    machine_centers: list[tuple[float, float, float, float]] = []  # (cx, cy, w, h)
    for stage in stages:
        prof = knowledge.machine_profile(stage["machine"])
        mw = prof.tile_width if prof else 3
        mh = prof.tile_height if prof else 3
        recipe = knowledge.recipe_for_product(stage["product"])
        solid_inputs = [i for i in (recipe.ingredients if recipe else {}) if not knowledge.is_fluid(i)]
        if len(solid_inputs) > 2:
            warnings.append(f"{stage['recipe']} has {len(solid_inputs)} solid inputs; v1 wires only 2")
        modules_items = _modules_to_items(stage["modules"])
        # Furnaces auto-select their recipe from the inserted ore — a blueprint must NOT pin a
        # recipe on them (it would be invalid). Only recipe-selectable machines get the recipe.
        recipe_field = None if "furnace" in stage["machine"] else stage["recipe"]
        for col in range(stage["count"]):
            cx = col * _MACHINE_PITCH_X
            cy = row_y
            machine_centers.append((cx, cy, mw, mh))
            _add(entities, stage["machine"], cx, cy, recipe=recipe_field, items=modules_items or None)
            # input lane(s)
            if solid_inputs:
                _add(entities, "transport-belt", cx - _BELT_OFFSET, cy, direction=SOUTH)
                _add(entities, "inserter", cx - _INSERTER_OFFSET, cy, direction=WEST)
            if len(solid_inputs) >= 2:
                _add(entities, "transport-belt", cx + _BELT_OFFSET, cy, direction=SOUTH)
                _add(entities, "inserter", cx + _INSERTER_OFFSET, cy, direction=EAST)
            # output lane (south)
            _add(entities, "transport-belt", cx, cy + _BELT_OFFSET, direction=EAST)
            _add(entities, "inserter", cx, cy + _INSERTER_OFFSET, direction=NORTH)
        row_y += (mh + _MACHINE_PITCH_Y)

    # --- power poles: cover every machine + keep poles wire-connected -----------------------
    pole_entities, coverage_ok, pole_warn = _place_poles(machine_centers, pole)
    entities.extend(pole_entities)
    warnings.extend(pole_warn)

    used = _bounds(entities)
    req_w = round(used["max_x"] - used["min_x"] + 1, 1)
    req_h = round(used["max_y"] - used["min_y"] + 1, 1)
    required_box = {"width": req_w, "height": req_h}

    fits = True
    if box is not None:
        fits = req_w <= box.width and req_h <= box.height
        if not fits:
            warnings.append(f"cell needs {req_w}x{req_h} but box is {box.width}x{box.height}")

    io_corridors = _io_corridors(spec, used)
    return PlacedCell(entities, fits, used, required_box, io_corridors, pole, coverage_ok, warnings)


def _modules_to_items(modules: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in modules:
        out[m] = out.get(m, 0) + 1
    return out


def _place_poles(machines: list[tuple[float, float, float, float]], pole: str) -> tuple[list[dict[str, Any]], bool, list[str]]:
    """Place poles on a grid so (a) every machine footprint overlaps a pole's supply area and
    (b) adjacent poles are within wire reach (connected network)."""
    if not machines:
        return [], True, []
    prof = knowledge.pole_profile(pole) or knowledge.pole_profile("small-electric-pole")
    warnings: list[str] = []
    min_x = min(cx - w / 2 for cx, cy, w, h in machines)
    max_x = max(cx + w / 2 for cx, cy, w, h in machines)
    min_y = min(cy - h / 2 for cx, cy, w, h in machines)
    max_y = max(cy + h / 2 for cx, cy, w, h in machines)

    # Grid step must satisfy BOTH: coverage (<= 2*supply_radius so squares tile the area) and
    # connectivity (<= wire_reach so neighbours wire up). Use the tighter of the two.
    step = max(1, int(min(2 * prof.supply_radius, prof.wire_reach)))
    poles: list[tuple[float, float]] = []
    y = min_y
    while True:
        x = min_x
        while True:
            poles.append((round(x), round(y)))
            if x >= max_x:
                break
            x += step
        if y >= max_y:
            break
        y += step

    entities = [{"name": pole, "position": {"x": px, "y": py}} for px, py in poles]
    coverage_ok = _coverage_ok(machines, poles, prof) and _connectivity_ok(poles, prof.wire_reach)
    if not coverage_ok:
        warnings.append("power-pole coverage/connectivity check failed")
    return entities, coverage_ok, warnings


def _coverage_ok(machines: list[tuple[float, float, float, float]], poles: list[tuple[float, float]],
                 prof: knowledge.PoleProfile) -> bool:
    """Every machine footprint must overlap at least one pole's (square) supply area."""
    for cx, cy, w, h in machines:
        covered = False
        for px, py in poles:
            # axis-aligned overlap test between the machine rect and the pole's supply square.
            if (abs(px - cx) <= prof.supply_radius + w / 2) and (abs(py - cy) <= prof.supply_radius + h / 2):
                covered = True
                break
        if not covered:
            return False
    return True


def _connectivity_ok(poles: list[tuple[float, float]], wire_reach: float) -> bool:
    """All poles form one network: each pole has a neighbour within wire reach (graph connected)."""
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


def _io_corridors(spec: CellSpec, used: dict[str, float]) -> list[dict[str, Any]]:
    """Reserved belt-lane / train-station corridors around the cell (C4) for the territory model."""
    corridors: list[dict[str, Any]] = []
    for io in spec.inputs:
        corridors.append({"role": "input", "item": io.item, "belt_tier": io.belt_tier,
                          "lanes": io.belt_lanes_needed, "side": "west"})
    for io in spec.outputs:
        corridors.append({"role": "output", "item": io.item, "belt_tier": io.belt_tier,
                          "lanes": io.belt_lanes_needed, "side": "south"})
    return corridors


def _bounds(entities: list[dict[str, Any]]) -> dict[str, float]:
    xs = [float(e["position"]["x"]) for e in entities if isinstance(e.get("position"), dict)]
    ys = [float(e["position"]["y"]) for e in entities if isinstance(e.get("position"), dict)]
    if not xs:
        return {"min_x": 0, "max_x": 0, "min_y": 0, "max_y": 0}
    return {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)}
