"""Return feed: a chest, an inserter, a side belt and an input-priority splitter that replaces one belt of a line.

One block, two uses (docs/v2/LAYOUT_PRINCIPLES.md "Return feed"):

* ``wood_burner_feed``: carried wood into the belt-fed boilers through the coal feed;
* ``trunk_return_feed``: carried surplus coal / ore back onto its patch's mine output trunk,
  just downstream of the trunk head and upstream of every tap.

The actor drops items into the chest; the inserter puts them on the side belt;
the splitter, which replaced one straight belt ``T`` of the line, takes its side
input first (INPUT priority on the side) and fills the line's own output first
(OUTPUT priority on the ``T`` side, so nothing leaves on the unused output).
Downstream consumers then take the returned items before freshly mined ones.

Shapes (flow direction ``v``, line belt ``T`` with a straight belt behind and
ahead, side tile ``S = T + s``, ``s`` one of the two lateral vectors)::

    column                              elbow
    S - 3v  chest                       S - v + 2s  chest
    S - 2v  inserter (picks at S - 3v)  S - v + s   inserter (picks at S - v + 2s)
    S - v   belt (v)                    S - v       belt (v)
    T + s/2 splitter (v)                T + s/2     splitter (v)
    S + v   stays free                  S + v       stays free

``S + v`` is the splitter's unused output and is never built on. The column
is the wood feed's historical shape; the elbow fits where the column would
reach back beside the drills (the trunk head).

Pure geometry plus the paid steps every use shares: :func:`splice` (the
line owner's plan is republished/released by the caller's ``republish``
before the one belt is mined by unit, then the splitter is built and its
priorities configured), :func:`seed_arm`, :func:`register_chest`,
:func:`stock_action` and the fresh-observation check :func:`standing`.
"""

from __future__ import annotations

import math
from copy import deepcopy

from .core.deterministic_builder import _plan

CHEST = "wooden-chest"
BURNER_ARM = "burner-inserter"
ELECTRIC_ARM = "inserter"
ARMS = (BURNER_ARM, ELECTRIC_ARM)
ARM_SEED = 2  # fuel put into a burner arm once; it then fuels itself from the fuel it moves
VECTORS = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}
BELTS = frozenset({"transport-belt", "underground-belt", "splitter"})
PRIORITY_KEYS = ("splitter_input_priority", "splitter_output_priority")
SHAPES = ("column", "elbow")


def _xy(entity) -> tuple:
    p = entity["position"]
    return (p["x"], p["y"])


def _pos(x, y) -> dict:
    return {"x": round(x, 3), "y": round(y, 3)}


def splitter_side(splitter: dict, point: dict) -> str:
    """``"left"`` / ``"right"`` of ``point`` seen along the splitter's facing (``planned_bus.splitter_side``)."""
    from .planned_bus import splitter_side as side

    return side(splitter, point)


def _facing(vector) -> int:
    return next(d for d, v in VECTORS.items() if v == vector)


def layout_at(belt: dict, sign: int, shape: str = "column", *, arm: str = BURNER_ARM, chest: str = CHEST) -> dict:
    """The return feed replacing ``belt`` with its side towards lateral ``sign`` (module doc), unchecked.

    Keys: ``belt`` (the replaced line belt), ``splitter`` (input priority on the
    side, output priority on the line), ``free`` (``S + v``), ``side`` (``S``),
    ``entities`` (side belt, inserter, chest in that order) and, for the elbow,
    ``shape``.
    """
    d = belt.get("direction", 0)
    vx, vy = VECTORS[d]
    x, y = _xy(belt)
    sx, sy = -vy * sign, vx * sign
    side = (x + sx, y + sy)
    feed = (side[0] - vx, side[1] - vy)
    if shape == "column":
        arm_at, chest_at = (side[0] - 2 * vx, side[1] - 2 * vy), (side[0] - 3 * vx, side[1] - 3 * vy)
        pickup = (-vx, -vy)
    elif shape == "elbow":
        arm_at, chest_at = (feed[0] + sx, feed[1] + sy), (feed[0] + 2 * sx, feed[1] + 2 * sy)
        pickup = (sx, sy)
    else:
        raise ValueError("shape must be column or elbow")
    splitter = {"name": "splitter", "position": _pos(x + sx / 2, y + sy / 2), "direction": d}
    splitter["splitter_input_priority"] = splitter_side(splitter, _pos(*side))
    splitter["splitter_output_priority"] = splitter_side(splitter, _pos(x, y))
    row = {
        "belt": deepcopy(belt),
        "splitter": splitter,
        "free": _pos(side[0] + vx, side[1] + vy),
        "side": _pos(*side),
        "entities": [
            {"name": "transport-belt", "position": _pos(*feed), "direction": d},
            {"name": arm, "position": _pos(*arm_at), "direction": _facing(pickup)},  # pickup side
            {"name": chest, "position": _pos(*chest_at), "direction": 0},
        ],
    }
    if shape != "column":
        row["shape"] = shape
    return row


def tiles(layout: dict) -> list[tuple]:
    """Every tile the feed covers besides ``T``: side, free output, side belt, inserter, chest."""
    points = [layout["side"], layout["free"]]
    return [(p["x"], p["y"]) for p in points] + [_xy(e) for e in layout["entities"]]


def layouts(entities, *, cell=(), clearance=0.0, taken=(), shapes=("column",), arm=BURNER_ARM, chest=CHEST, ahead=None):
    """Every feed layout on a straight run of the belts in ``entities``, unsorted.

    ``T`` is a ``transport-belt`` whose upstream neighbour is a belt and whose
    downstream neighbour is a belt (or an entity in ``ahead``, e.g. the next
    tap's splitter: a dict tile -> entity) of the same direction. Belts within
    ``clearance`` (Chebyshev) of a ``cell`` point are skipped; a layout whose
    tiles meet ``taken`` (default: the tiles of ``entities``) is skipped.
    """
    entities = list(entities)
    belts = {_xy(e): e for e in entities if e["name"] == "transport-belt"}
    following = dict(ahead or {})
    following.update(belts)
    taken = set(taken) if taken else {_xy(e) for e in entities}
    rows = []
    for (x, y), belt in belts.items():
        d = belt.get("direction", 0)
        if d not in VECTORS:
            continue
        vx, vy = VECTORS[d]
        before, after = belts.get((x - vx, y - vy)), following.get((x + vx, y + vy))
        if not before or not after or before.get("direction") != d or after.get("direction") != d:
            continue
        if any(max(abs(px - x), abs(py - y)) <= clearance for px, py in cell):
            continue
        for shape in shapes:
            for sign in (1, -1):
                row = layout_at(belt, sign, shape, arm=arm, chest=chest)
                if any(t in taken for t in tiles(row)):
                    continue
                rows.append(row)
    return rows


def reserved_area(layout: dict) -> list:
    """Bounding box of the side hardware and the free output (``T`` stays the line's own hardware)."""
    free = layout["free"]
    points = [_xy(e) for e in layout["entities"]] + [(free["x"], free["y"])]
    xs, ys = [t[0] for t in points], [t[1] for t in points]
    return [[math.floor(min(xs)), math.floor(min(ys))], [math.ceil(max(xs)), math.ceil(max(ys))]]


def spliced_plan(plan: dict, layout: dict) -> dict:
    """``plan`` with the belt at ``T`` replaced by the feed splitter (idempotent)."""
    at = _xy(layout["belt"])
    kept = [e for e in plan["entities"] if not (e["name"] == "transport-belt" and _xy(e) == at)]
    if not any(e["name"] == "splitter" and _xy(e) == _xy(layout["splitter"]) for e in kept):
        kept.append(deepcopy(layout["splitter"]))
    extra = {k: v for k, v in plan.items() if k not in ("ok", "reason", "entities", "ports", "required_items")}
    return _plan(kept, plan["ports"], **extra)


def side_plan(layout: dict, *, clear_obstacles=(), world_id=None, template=None) -> dict:
    """The side hardware (belt, inserter, chest) as a builder plan."""
    return _plan(
        deepcopy(layout["entities"]),
        [],
        reserved_area=reserved_area(layout),
        clear_obstacles=deepcopy(list(clear_obstacles)),
        clearance_world_id=world_id,
        template=template,
    )


def block(layout: dict, *, template: str, owner: str, **extra) -> dict:
    """Board block of the feed: side hardware + splitter, reserved area, owner."""
    return dict(
        _plan(deepcopy(layout["entities"]) + [deepcopy(layout["splitter"])], []),
        reserved_area=reserved_area(layout),
        template=template,
        owner=owner,
        **extra,
    )


def arm_spec(layout: dict) -> dict:
    return next(e for e in layout["entities"] if e["name"] in ARMS)


def chest_spec(layout: dict) -> dict:
    return layout["entities"][-1]


# ----- paid steps ------------------------------------------------------------------------
def splice(builder, obs, layout, republish, *, belt_unit, reason, identity_reason) -> dict | None:
    """Splitter in hand, then the owner's plan is republished, the old belt mined (exact unit), the splitter built.

    ``republish()`` runs right before the mine (and again before the build), so
    no other job's repair sees the old belt still planned once it is gone.
    Returns one action/status, or None once the splitter stands with its priorities.
    """
    from .planned_bus import configure_action

    live = {(e["name"], *_xy(e)): e for e in obs.get("entities", [])}
    splitter = layout["splitter"]
    if ("splitter", *_xy(splitter)) not in live:
        if int((obs.get("inventory") or {}).get("splitter", 0)) < 1:
            result = builder.bootstrap.ensure_item(obs, "splitter", 1)
            if result.get("type") or result.get("status") != "succeeded":
                return result
        old = live.get(("transport-belt", *_xy(layout["belt"])))
        if old is not None:
            if old.get("unit_number") != belt_unit:
                return {"status": "blocked", "reason": identity_reason}
            move = builder._move(obs, old["position"])
            if move:
                return move
            blocked = republish()
            if blocked:
                return blocked
            return {
                "type": "mine",
                "name": old["name"],
                "position": old["position"],
                "count": 1,
                "expected_entity_unit": old["unit_number"],
                "expected_entity_world_id": obs["world_id"],
                "reason": reason,
            }
    blocked = republish()
    if blocked:
        return blocked
    result = builder.ensure_plan(obs, _plan([deepcopy(splitter)], []))
    if result.get("status") != "succeeded":
        return result
    return configure_action(builder, obs, [(splitter, {k: splitter[k] for k in PRIORITY_KEYS})])


def seed_arm(builder, obs, layout, fuel: str, reason: str) -> dict | None:
    """A burner arm gets ``ARM_SEED`` ``fuel`` once; None when it holds fuel or is electric."""
    spec = arm_spec(layout)
    if spec["name"] != BURNER_ARM:
        return None
    arm = next((e for e in obs.get("entities", []) if e.get("name") == BURNER_ARM and _xy(e) == _xy(spec)), None)
    if arm is None:
        return {"status": "waiting", "reason": "reobserve the paid return feed arm"}
    if sum((arm.get("inventory") or {}).values()) > 0 or float(arm.get("remaining_burning_fuel") or 0) > 0:
        return None
    carried = int((obs.get("inventory") or {}).get(fuel, 0))
    if carried < 1:
        result = builder.bootstrap.ensure_item(obs, fuel, ARM_SEED)
        return result if result.get("type") or result.get("status") != "succeeded" else None
    move = builder._move(obs, spec["position"])
    if move:
        return move
    return {
        "type": "insert",
        "name": BURNER_ARM,
        "position": deepcopy(spec["position"]),
        "item": fuel,
        "inventory": "fuel",
        "count": min(ARM_SEED, carried),
        "reason": reason,
    }


def register_chest(obs, layout) -> dict:
    """``{"chest": {name, position, unit_number}}`` of the built chest, or a waiting status."""
    spec = chest_spec(layout)
    found = next(
        (e for e in obs.get("entities", []) if e.get("name") == spec["name"] and _xy(e) == _xy(spec)),
        None,
    )
    if found is None or type(found.get("unit_number")) is not int:
        return {"status": "waiting", "reason": "reobserve the paid return feed chest"}
    return {"chest": {"name": spec["name"], "position": dict(found["position"]), "unit_number": found["unit_number"]}}


def find_chest(obs, saved) -> dict | None:
    """The registered chest ``saved`` standing in ``obs``, else None."""
    if not saved:
        return None
    return next(
        (
            e
            for e in obs.get("entities", [])
            if e.get("name") == saved["name"] and e.get("unit_number") == saved["unit_number"]
        ),
        None,
    )


def stock_action(builder, obs, chest, item, catalog, *, keep=0, reason) -> dict | None:
    """Deposit carried ``item`` above ``keep`` into the feed chest; None when nothing is left or the chest is full."""
    from .carried_surplus import DEPOSIT_LIMIT, chest_room

    room = chest_room(chest, item, catalog)
    spare = int((obs.get("inventory") or {}).get(item, 0)) - max(0, int(keep))
    if room <= 0 or spare <= 0:
        return None
    move = builder._move(obs, chest["position"])
    if move:
        return move
    return {
        "type": "deposit",
        "name": chest["name"],
        "position": dict(chest["position"]),
        "item": item,
        "count": int(min(spare, room, DEPOSIT_LIMIT)),
        "expected_entity_unit": chest["unit_number"],
        "expected_entity_world_id": obs["world_id"],
        "reason": reason,
    }


def standing(obs, layout) -> dict | None:
    """The live splitter when every feed entity stands as planned with its priorities and ``T`` is gone, else None."""
    live = {(e["name"], *_xy(e)): e for e in obs.get("entities", [])}
    for spec in [*layout["entities"], layout["splitter"]]:
        found = live.get((spec["name"], *_xy(spec)))
        if found is None or (spec is not chest_spec(layout) and found.get("direction") != spec.get("direction")):
            return None
    split = live[("splitter", *_xy(layout["splitter"]))]
    if any(split.get(k) != layout["splitter"][k] for k in PRIORITY_KEYS):
        return None
    if ("transport-belt", *_xy(layout["belt"])) in live:
        return None
    return split
