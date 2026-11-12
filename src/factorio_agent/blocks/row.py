"""One recipe-driven main-bus production row, written once for an east flow.

Canonical layout (flow east, u = +x, v = +y; origin = first pole, v = 0)::

    v=-4  @>>>>>>>>>>>>>>>>   far input belt (only for 3-4 solid items), port @ at u=0
    v=-3  @>>>>>>>>>>>>>>>>   near input belt (one item per lane, or one item on both lanes)
    v=-2  .iiL.iiL.........   input inserters: plain (i) from near belt, long-handed (L) from far
    v=-1  .MMM.MMM.:::.:::.
    v= 0  +MMM+MMM+:::.:::.   3x3 machines, a pole (+) in every 1-tile gap, ':' = reserved slot
    v=+1  .MMM.MMM.:::.:::.
    v=+2  ..i...i..........   output inserters
    v=+3  >>>>>>>>>>>>>>>>O   output belt, product on the far (right) lane, port O at the end

Machine slot ``i`` is centred at u = pitch * i + half + 1 with pitch = size + 1;
poles stand at u = pitch * k. Belts span all reserved slots, so ``extend_row``
fills the next slot in place and every port stays where it is.

Inserter convention (repo-wide, see ``mall_cells.recipe_cell``): direction d
picks up at ``position + reach * VECTORS[d]`` and drops at
``position - reach * VECTORS[d]``. Every canonical inserter has d = 0 (north).
Other flows rotate the canonical layout clockwise (``geometry.Frame``).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from typing import Any

from ..board import entity
from ..core.deterministic_builder import _plan
from ..lanes import inserter_output_lane
from .geometry import CANONICAL_FLOW, Frame, bounding_box, flow_direction, footprint, require_tile_centre
from .params import DEFAULT_LIMITS, RowLimits, inserter_rate, inserter_reach, pole_radius, pole_reach
from .sizing import BeltLanes, RowSizing, row_supports_size, size_row

TEMPLATE = "recipe_row_v1"
NORTH = 0


@dataclass(frozen=True)
class RowShape:
    """Canonical u/v offsets for a square machine of side ``size``."""

    size: int

    @property
    def half(self) -> int:
        return self.size // 2

    @property
    def pitch(self) -> int:
        return self.size + 1

    @property
    def input_arm_v(self) -> int:
        return -(self.half + 1)

    @property
    def output_arm_v(self) -> int:
        return self.half + 1

    @property
    def output_belt_v(self) -> int:
        return self.half + 2

    def input_belt_v(self, index: int) -> int:
        return -(self.half + 2 + index)

    def machine_u(self, slot: int) -> int:
        return self.pitch * slot + self.half + 1

    def columns(self) -> list[int]:
        return list(range(-self.half, self.half + 1))


@dataclass(frozen=True)
class RowSpec:
    """Everything needed to rebuild the same row with more machines (JSON-safe)."""

    origin: tuple[float, float]
    flow: int
    inserter: str
    long_inserter: str
    belt: str
    pole: str
    slots: int
    near_arms: int
    far_arms: int
    output_arms: int


def _need(demand: float, rate: float) -> int:
    return max(1, math.ceil(demand / rate - 1e-9))


def arm_counts(catalog: Any, sizing: RowSizing, inserter: str, limits: RowLimits) -> tuple[int, int, int]:
    """Inserters per machine (near belt, far belt, output) from the rate model in ``params``."""
    names = (inserter, limits.long_inserter)
    counts = []
    for index, belt in enumerate(sizing.input_belts):
        if inserter_reach(catalog, names[index], limits) != index + 1:
            raise ValueError(f"{names[index]} cannot reach input belt {index} at {index + 1} tile(s)")
        demand = sum(sizing.flow(item).per_machine for item in belt.items)
        counts.append(_need(demand, inserter_rate(catalog, names[index], limits)))
    near, far = counts[0], (counts[1] if len(counts) > 1 else 0)
    out = _need(sizing.product.per_machine, inserter_rate(catalog, inserter, limits))
    if near + far > sizing.machine_size or out > sizing.machine_size:
        raise ValueError(
            f"{sizing.recipe}: needs {near}+{far} input / {out} output {inserter}s per machine but a "
            f"{sizing.machine_size}-tile edge fits {sizing.machine_size}; use a faster inserter"
        )
    return near, far, out


def _check_power(catalog: Any, shape: RowShape, pole: str, limits: RowLimits) -> None:
    if not row_supports_size(shape.size, pole_radius(catalog, pole, limits)):
        raise ValueError(f"{pole} in the machine gaps cannot power {shape.size}x{shape.size} rows")
    if shape.pitch > pole_reach(catalog, pole, limits):
        raise ValueError(f"{pole} wire reach is shorter than the machine pitch {shape.pitch}")


def canonical_entities(sizing: RowSizing, spec: RowSpec, count: int) -> list[tuple]:
    """``(name, u, v, canonical_direction, recipe)`` for the east-flowing layout."""
    shape = RowShape(sizing.machine_size)
    cols = shape.columns()
    out_cols = sorted(cols, key=lambda c: (abs(c), c))[: spec.output_arms]
    far_cols = cols[len(cols) - spec.far_arms :] if spec.far_arms else []
    rows: list[tuple] = [(spec.pole, 0, 0, NORTH, None)]
    for slot in range(count):
        mu = shape.machine_u(slot)
        rows.append((sizing.machine, mu, 0, NORTH, sizing.recipe))
        rows += [(spec.inserter, mu + c, shape.input_arm_v, NORTH, None) for c in cols[: spec.near_arms]]
        rows += [(spec.long_inserter, mu + c, shape.input_arm_v, NORTH, None) for c in far_cols]
        rows += [(spec.inserter, mu + c, shape.output_arm_v, NORTH, None) for c in out_cols]
        rows.append((spec.pole, shape.pitch * (slot + 1), 0, NORTH, None))
    for u in range(shape.pitch * spec.slots + 1):
        for index in range(len(sizing.input_belts)):
            rows.append((spec.belt, u, shape.input_belt_v(index), CANONICAL_FLOW, None))
        rows.append((spec.belt, u, shape.output_belt_v, CANONICAL_FLOW, None))
    return rows


def _place(frame: Frame, rows: list[tuple], size: int, machine: str) -> list[dict]:
    placed = []
    for name, u, v, direction, recipe in rows:
        x, y = frame.point(u, v)
        e = entity(name, x, y, frame.direction(direction), recipe=recipe)
        if name == machine:
            e = dict(e, direction=0, _width=size, _height=size)
        placed.append(e)
    return placed


def _belt_ports(frame: Frame, shape: RowShape, belts: tuple[BeltLanes, ...]) -> list[dict]:
    ports = []
    for index, belt in enumerate(belts):
        x, y = frame.point(0, shape.input_belt_v(index))
        base = {"kind": "item", "position": {"x": x, "y": y}, "facing": frame.flow, "direction": "input"}
        if belt.left == belt.right:
            ports.append(dict(base, item=belt.left, belt_index=index))
        else:
            ports += [
                dict(base, item=item, lane=lane, belt_index=index) for lane, item in zip(("left", "right"), belt.items)
            ]
    return ports


def _output_port(frame: Frame, shape: RowShape, spec: RowSpec, product: str) -> dict:
    x, y = frame.point(shape.pitch * spec.slots, shape.output_belt_v)
    lane = inserter_output_lane(frame.direction(NORTH), frame.flow)
    return {"kind": "item", "item": product, "position": {"x": x, "y": y}, "facing": frame.flow}, lane


def _build(catalog: Any, sizing: RowSizing, spec: RowSpec, count: int) -> dict:
    """Shared by ``row_block`` and ``extend_row``: same spec + more machines = same ports."""
    if count > spec.slots:
        raise ValueError(f"{count} machines exceed the {spec.slots} reserved slots")
    shape = RowShape(sizing.machine_size)
    frame = Frame(tuple(spec.origin), spec.flow)
    entities = _place(frame, canonical_entities(sizing, spec, count), shape.size, sizing.machine)
    output, lane = _output_port(frame, shape, spec, sizing.product.item)
    ports = _belt_ports(frame, shape, sizing.input_belts) + [dict(output, direction="output", lane=lane)]
    tiles = set().union(*(footprint(catalog, e) for e in entities))
    slots = []
    for slot in range(count, spec.slots):
        x, y = frame.point(shape.machine_u(slot), 0)
        slots.append(
            {
                "purpose": "future_machine",
                "name": sizing.machine,
                "recipe": sizing.recipe,
                "position": {"x": x, "y": y},
                "reserved_only": True,
            }
        )
    gx, gy = frame.point(1, 0)
    ox, oy = frame.point(0, 0)
    first_pole = {"name": spec.pole, "position": {"x": ox, "y": oy}}
    return _plan(
        entities,
        ports,
        template=TEMPLATE,
        layout_policy="recipe-row-main-bus-v1",
        recipe=sizing.recipe,
        machine=sizing.machine,
        count=count,
        target_per_minute=sizing.target_per_minute,
        rated_per_minute=sizing.product.per_machine * count,
        reserved_area=bounding_box(tiles, margin=1),
        flow=spec.flow,
        growth={
            "direction": spec.flow,
            "axis": "y" if gx == ox else "x",
            "step": [gx - ox, gy - oy],
            "pitch": shape.pitch,
            "remaining_slots": spec.slots - count,
        },
        expansion_slots=slots,
        input_belts=[{"left": b.left, "right": b.right} for b in sizing.input_belts],
        power_port=first_pole,
        layout=asdict(spec),
        scope="nameplate machine rate and conservative inserter/belt budgets; not a measured rate",
    )


def row_block(
    catalog: Any,
    sizing: RowSizing,
    *,
    origin: tuple[float, float] = (0.5, 0.5),
    flow: str | int = "east",
    inserter: str = "inserter",
    belt: str = "transport-belt",
    pole: str = "small-electric-pole",
    spare: int | None = None,
    limits: RowLimits = DEFAULT_LIMITS,
) -> dict:
    """Lay out ``sizing`` as one straight row; ``origin`` is the first pole (a tile centre).

    ``spare`` reserved slots (default: as many as the lane budget allows, at
    most ``sizing.count``) are covered by the belts already, for ``extend_row``.
    """
    if belt != sizing.belt:
        raise ValueError(f"row sized for {sizing.belt}, not {belt}; size it with the same belt")
    shape = RowShape(sizing.machine_size)
    _check_power(catalog, shape, pole, limits)
    near, far, out = arm_counts(catalog, sizing, inserter, limits)
    room = sizing.max_count - sizing.count
    spare = min(sizing.count, room) if spare is None else spare
    if not 0 <= spare <= room:
        raise ValueError(f"spare must be within 0..{room} (lane budget)")
    spec = RowSpec(
        require_tile_centre(origin),
        flow_direction(flow),
        inserter,
        limits.long_inserter,
        belt,
        pole,
        sizing.count + spare,
        near,
        far,
        out,
    )
    return _build(catalog, sizing, spec, sizing.count)


def extend_row(plan: dict, catalog: Any, extra_machines: int, *, limits: RowLimits = DEFAULT_LIMITS) -> dict:
    """Fill ``extra_machines`` more reserved slots; ports, belts and origin stay fixed."""
    if not isinstance(extra_machines, int) or extra_machines < 1:
        raise ValueError("extra_machines must be a positive integer")
    spec = RowSpec(**dict(plan["layout"], origin=tuple(plan["layout"]["origin"])))
    sizing = size_row(
        catalog,
        plan["recipe"],
        plan["target_per_minute"],
        machine=plan["machine"],
        belt=spec.belt,
        pole=spec.pole,
        limits=limits,
    )
    sizing = sizing.with_count(plan["count"] + extra_machines)
    if [{"left": b.left, "right": b.right} for b in sizing.input_belts] != plan["input_belts"]:
        raise ValueError("catalog lane plan changed since the row was laid out")
    if arm_counts(catalog, sizing, spec.inserter, limits) != (spec.near_arms, spec.far_arms, spec.output_arms):
        raise ValueError("inserter budget changed since the row was laid out")
    extended = _build(catalog, sizing, spec, sizing.count)
    extended["target_per_minute"] = max(plan["target_per_minute"], extended["rated_per_minute"])
    if extended["ports"] != plan["ports"]:
        raise ValueError("extension would move ports")
    return extended


def main(argv: list[str] | None = None) -> int:
    """``python -m factorio_agent.blocks.row <catalog.json> <recipe> <rate>``: print an ASCII block."""
    from ..core.world_catalog import WorldCatalog
    from .render import render_ascii

    parser = argparse.ArgumentParser(prog="python -m factorio_agent.blocks.row")
    parser.add_argument("catalog")
    parser.add_argument("recipe")
    parser.add_argument("rate", type=float)
    parser.add_argument("--machine")
    parser.add_argument("--inserter", default="inserter")
    parser.add_argument("--flow", default="east")
    parser.add_argument("--spare", type=int)
    parser.add_argument("--all-researched", action="store_true", help="treat every technology as researched")
    args = parser.parse_args(argv)
    with open(args.catalog, encoding="utf-8") as handle:
        catalog = WorldCatalog.from_dict(json.load(handle))
    researched = list(catalog.technologies) if args.all_researched else None
    try:
        sizing = size_row(catalog, args.recipe, args.rate, machine=args.machine, researched=researched)
        plan = row_block(catalog, sizing, flow=args.flow, inserter=args.inserter, spare=args.spare)
    except ValueError as error:
        print("rejected:", error, file=sys.stderr)
        return 2
    print(f"{plan['recipe']}: {plan['count']} x {plan['machine']} = {plan['rated_per_minute']:g}/min")
    print(render_ascii(catalog, plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
