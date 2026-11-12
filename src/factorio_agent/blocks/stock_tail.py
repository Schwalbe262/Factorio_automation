"""Stock tail: the output belt of a row ends in one bounded buffer chest.

Geometry (world frame of the row; ``O`` = the row's output port tile, ``f`` =
the port's facing, ``left`` = the side of the output belt the machines are
on, i.e. ``f`` turned a quarter counter-clockwise)::

    left
     ^ +   +  +         tail pole at O + f + left (powers the arm); bridge poles on the
    >>>>>>>>>O i C      line O + left in the machine gaps, back to the last built pole
                        belt end O, tail inserter i at O + f, chest C at O + 2f

Bridge poles stand only on gap columns (a whole number of machine pitches
from O), where neither future machines nor their output inserters go, so the
tail never blocks ``extend_row``.

The tail inserter faces back onto the belt (direction ``f + 8``): with the
repo-wide convention it picks up at ``position + reach * VECTORS[d]`` = O and
drops at ``position - reach * VECTORS[d]`` = C. Items stop at the belt end,
so the arm always finds them on O.

Stock bound (parameter-free rule): the chest bar is ``STOCK_STACKS`` (2)
stacks of the product, so a stock row holds at most 2 stacks in the chest
plus whatever backs up on its belt, then the machines stop. The tail
carries at most one inserter's rate (``params.inserter_rate``); the plan
reports ``stock_rate_per_minute = min(row rate, tail inserter rate)``.
"""

from __future__ import annotations

import math
from typing import Any

from ..board import entity
from ..routing import VECTORS
from .geometry import footprint, step, xy
from .params import DEFAULT_LIMITS, RowLimits, inserter_rate, inserter_reach, pole_radius, pole_reach

STOCK_STACKS = 2


def _output_port(plan: dict) -> dict:
    ports = [p for p in plan["ports"] if p.get("direction") == "output"]
    if len(ports) != 1:
        raise ValueError("a stock tail needs a row with exactly one output port")
    return ports[0]


def stock_tail(
    catalog: Any,
    plan: dict,
    *,
    inserter: str = "inserter",
    chest: str = "iron-chest",
    stacks: int = STOCK_STACKS,
    limits: RowLimits = DEFAULT_LIMITS,
) -> dict:
    """Tail entities, the chest output port and the bar for ``plan`` (a row plan).

    The returned dict is JSON-safe: ``entities`` (arm, chest, pole), ``port``
    (the chest as an ``output`` port of the product), ``arm``, ``chest``,
    ``pole``, ``bar_slots`` and ``stock_rate_per_minute``.
    """
    if type(stacks) is not int or stacks < 1:
        raise ValueError("stock tail stacks must be a positive integer")
    port = _output_port(plan)
    facing = port["facing"]
    left = (facing + 12) % 16
    end = xy(port)
    arm_at = step(end, facing)
    chest_at = step(end, facing, 2)
    pole_at = step(arm_at, left)
    pole_name = (plan.get("layout") or {}).get("pole") or "small-electric-pole"
    arm = entity(inserter, *arm_at, (facing + 8) % 16)
    box = entity(chest, *chest_at)
    pole = entity(pole_name, *pole_at)
    bridges = _bridge(catalog, plan, end, facing, left, pole, limits)
    rated = float(plan.get("rated_per_minute") or 0)
    return {
        "entities": [arm, box, pole, *bridges],
        "arm": arm,
        "chest": box,
        "pole": pole,
        "port": {
            "kind": "item",
            "item": port["item"],
            "direction": "output",
            "name": chest,
            "position": dict(box["position"]),
        },
        "belt_end": dict(port["position"]),
        "bar_slots": stacks,
        "stock_rate_per_minute": min(rated, inserter_rate(catalog, inserter, limits)),
    }


def _poles(catalog: Any, entities: list[dict]) -> list[dict]:
    return [e for e in entities if (catalog.entities.get(e["name"]) or {}).get("type") == "electric-pole"]


def _wired(catalog: Any, a: dict, b: dict, limits: RowLimits) -> bool:
    reach = min(pole_reach(catalog, a["name"], limits), pole_reach(catalog, b["name"], limits))
    return math.dist(xy(a), xy(b)) <= reach + 1e-9


def _bridge(catalog: Any, plan: dict, end, facing: int, left: int, pole: dict, limits: RowLimits) -> list[dict]:
    """Gap-column poles from the tail pole back to the row's nearest built pole."""
    row_poles = _poles(catalog, plan["entities"])
    pitch = int((plan.get("growth") or {}).get("pitch") or 0)
    if not row_poles or pitch < 1:
        return []
    occupied = set().union(*(footprint(catalog, e) for e in plan["entities"]))
    line = step(end, left)
    chain = [pole]
    for gap in range(64):
        if any(_wired(catalog, chain[-1], q, limits) for q in row_poles):
            break
        at = step(line, (facing + 8) % 16, pitch * gap)
        if at in occupied:
            break
        candidate = entity(pole["name"], *at)
        if _wired(catalog, chain[-1], candidate, limits):
            chain.append(candidate)
    return chain[1:]


def _powered(catalog: Any, e: dict, poles: list[dict], limits: RowLimits) -> bool:
    tiles = footprint(catalog, e)
    for pole in poles:
        radius = pole_radius(catalog, pole["name"], limits)
        px, py = xy(pole)
        if any(abs(t[0] - px) < radius + 0.5 and abs(t[1] - py) < radius + 0.5 for t in tiles):
            return True
    return False


def audit_stock_tail(catalog: Any, plan: dict, tail: dict, *, limits: RowLimits = DEFAULT_LIMITS) -> list[str]:
    """Independent check of a tail against its row plan; ``[]`` means no finding."""
    errors = []
    port = _output_port(plan)
    row_tiles = set().union(*(footprint(catalog, e) for e in plan["entities"]))
    tail_tiles = [footprint(catalog, e) for e in tail["entities"]]
    if any(tiles & row_tiles for tiles in tail_tiles) or len(set().union(*tail_tiles)) != sum(map(len, tail_tiles)):
        errors.append("stock_tail_overlap")
    arm, chest = tail["arm"], tail["chest"]
    if (catalog.entities.get(arm["name"]) or {}).get("type") != "inserter":
        errors.append("stock_tail_arm_not_inserter")
    if (catalog.entities.get(chest["name"]) or {}).get("type") != "container":
        errors.append("stock_tail_chest_not_container")
    try:
        reach = inserter_reach(catalog, arm["name"], limits)
    except ValueError:
        return sorted(set(errors + ["stock_tail_arm_reach_unknown"]))
    vx, vy = VECTORS[arm["direction"]]
    pickup = (xy(arm)[0] + reach * vx, xy(arm)[1] + reach * vy)
    drop = (xy(arm)[0] - reach * vx, xy(arm)[1] - reach * vy)
    if pickup != xy(port):
        errors.append("stock_tail_pickup_not_belt_end")
    if drop not in footprint(catalog, chest):
        errors.append("stock_tail_drop_not_chest")
    belt_end = step(xy(port), port["facing"])
    if belt_end in row_tiles:
        errors.append("stock_tail_belt_continues")
    row_poles = _poles(catalog, plan["entities"])
    tail_poles = _poles(catalog, tail["entities"])
    if not _powered(catalog, arm, row_poles + tail_poles, limits):
        errors.append("stock_tail_arm_unpowered")
    network = list(row_poles)
    pending = list(tail_poles)
    while pending:
        joined = [q for q in pending if any(_wired(catalog, q, r, limits) for r in network)]
        if not joined:
            errors.append("stock_tail_pole_not_wired")
            break
        network += joined
        pending = [q for q in pending if q not in joined]
    tail_area = set().union(*tail_tiles)
    for slot in plan.get("expansion_slots", []):
        if footprint(catalog, dict(slot, direction=0)) & tail_area:
            errors.append("stock_tail_blocks_expansion")
    if tail["port"]["item"] != port["item"] or tail["port"]["position"] != chest["position"]:
        errors.append("stock_tail_port_mismatch")
    if type(tail.get("bar_slots")) is not int or tail["bar_slots"] < 1:
        errors.append("stock_tail_unbounded")
    return sorted(set(errors))
