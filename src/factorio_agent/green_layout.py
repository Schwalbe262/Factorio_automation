"""Green science layout candidates: the direct-insertion cluster and the split row.

``docs/v2/LAYOUT_PRINCIPLES.md`` ("Green science"). Two geometries, both generated
from the recipe tree (``blocks.cluster``), never copied:

* ``cluster`` (``green_science.green_cluster``): one pack machine and one machine
  per intermediate, everything by direct insertion, plates on one two-lane belt.
  5 packs/min with assembling machine 1; growth = a second cluster (or a rebuild):
  a second pack machine turns the shared gear and the inserter/belt edges into
  belt edges (``cluster.grow_decision``).
* ``split``: the two sub-assemblers as their own direct-insertion clusters (the
  inserter cluster: cable -> circuit -> inserter <- gear; the belt cluster: gear ->
  belt), sized for the whole frame, both dropping onto one intermediate belt, the
  inserter on one lane, the belt on the other (no side-load: the inserter
  cluster's port tile feeds straight into the belt cluster's); a pack row along that
  belt, one pack machine built first and ``FRAME`` slots reserved, packs on a pack
  belt to the output port at the far end. Growth = extending the row in place up
  to the frame (the sub-clusters already carry it), limited by the sub-clusters'
  one output arm each (``inserter_rate``); the reserved slots are built by
  ``science_growth {pack, machines}`` (``pack_growth.py``).

Plate ports of both face the bus edge (west in the canonical frame); the cluster's
pack port is on a free side of its pack machine (the placement picks it), the
split's at the east end of its pack belt (opposite the plates).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .board import entity
from .core.deterministic_builder import FactoryBuilder, _plan
from .routing import VECTORS
from .smelting import port

PACK = "logistic-science-pack"
PLATES = ("iron-plate", "copper-plate")
AM1 = "assembling-machine-1"
INSERTER = "inserter"
POLE = "small-electric-pole"
BELT = "transport-belt"
FRAME = 6  # pack machine slots of the split row: 30 packs/min, what one output arm of each sub-cluster moves
START = 1  # pack machines built first (start small, as the cluster)
_GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)


@dataclass(frozen=True)
class GreenGenome:
    form: str = "cluster"  # cluster | split
    output_side: str = ""  # cluster only: the pack machine's free side; chosen per placement when empty
    frame: int = FRAME  # split only

    def key(self) -> str:
        return "green-cluster" if self.form == "cluster" else f"green-split-{self.frame}"

    def to_dict(self) -> dict:
        return {"form": self.form, "output_side": self.output_side, "frame": self.frame, "modules": {}, "beacons": []}

    @classmethod
    def from_dict(cls, data: dict) -> "GreenGenome":
        return cls(str(data.get("form", "cluster")), str(data.get("output_side") or ""), int(data.get("frame", FRAME)))


def _nominal(catalog: Any, recipe: str) -> float:
    return 60 * float(catalog.entities[AM1]["crafting_speed"]) / float(catalog.recipes[recipe]["energy"])


def _x(e) -> float:
    return e["position"]["x"]


def _y(e) -> float:
    return e["position"]["y"]


def _shift(plan: dict, dx: float, dy: float) -> dict:
    from .site.transform import Transform, apply

    moved = apply(Transform(0, None, (0, 0)), plan)

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("x"), (int, float)) and isinstance(value.get("y"), (int, float)):
                value["x"] += dx
                value["y"] += dy
            for k, v in value.items():
                if k in ("reserved_area",) and isinstance(v, list):
                    value[k] = [[v[0][0] + dx, v[0][1] + dy], [v[1][0] + dx, v[1][1] + dy]]
                else:
                    walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(moved)
    return moved


def split_block(catalog: Any, frame: int = FRAME, origin: tuple[int, int] = (0, 0), *, built: int = START) -> dict:
    """The split green geometry (module doc); its reserved corner at ``origin``.

    Returns a plan whose ``entities`` are the start stage (``built`` pack machines),
    ``growth_slots`` the reserved pack slots (entity groups), ``cells`` the three pole
    groups (inserter cluster, belt cluster, pack row) for ``connect_power_groups``.
    ValueError when a sub-cluster cannot carry the frame (``cluster_block``).
    """
    from .blocks.cluster import cluster_block
    from .site.transform import Transform, apply

    if not 1 <= built <= frame <= 12:
        raise ValueError("split green row: 1 <= built <= frame <= 12")
    rate = frame * _nominal(catalog, PACK)  # inserters and belts per minute at the full frame
    sourced = PLATES.__contains__
    ins = cluster_block(catalog, "inserter", rate, sourced=sourced, chest=None, output_side="bottom")
    if ins["rated_per_minute"] + 1e-9 < rate:
        raise ValueError(f"split green: the inserter cluster makes {ins['rated_per_minute']:g}/min < {rate:g}")
    belts = cluster_block(catalog, "transport-belt", rate, sourced=sourced, chest=None, output_side="bottom")
    if belts["rated_per_minute"] + 1e-9 < rate:
        raise ValueError(f"split green: the belt cluster makes {belts['rated_per_minute']:g}/min < {rate:g}")
    pi = ins["ports"][-1]["position"]
    pb = belts["ports"][-1]["position"]
    # The belt cluster mirrored about its port row (its machines below the intermediate belt), its port one
    # tile downstream of the inserter cluster's port tile.
    belts = apply(Transform(0, "y", (pb["x"], pb["y"])), belts)
    belts = _shift(belts, pi["x"] + 1 - pb["x"], pi["y"] - pb["y"])
    y = pi["y"]  # the intermediate belt row
    # First pack machine centre: its footprint starts at the inserter cluster's reservation edge (the pack
    # row stands on the inserter cluster's side of the intermediate belt).
    u0 = ins["reserved_area"][1][0] + 1.5
    slots = []
    for k in range(frame):
        u = u0 + 4 * k
        slots.append(
            [
                entity(AM1, u, y - 3, recipe=PACK),
                entity(INSERTER, u, y - 1, 8),  # from the intermediate belt (inserter and belt lanes)
                entity(INSERTER, u, y - 5, 8),  # packs onto the pack belt
                entity(POLE, u + 2, y - 3),
            ]
        )
    last = u0 + 4 * (frame - 1)
    middle = [entity(BELT, x, y, 4) for x in _span(pi["x"] + 2, last + 1)]
    end = last + 2
    pack_belt = [entity(BELT, x, y - 6, 4) for x in _span(u0, end)]
    ports_out = port(PACK, end, y - 6, 4, "output")
    row = [e for group in slots[:built] for e in group] + middle + pack_belt
    growth = [group for group in slots[built:]]
    # Each plate input's own draw per pack (its sub-cluster's subtree), so a tap declares what that
    # port takes and not the pack's whole draw of the item (two iron inputs).
    inputs = [
        dict(p, per_pack=_per_pack(catalog, "inserter", p["item"])) for p in ins["ports"] if p["direction"] == "input"
    ]
    inputs += [
        dict(p, per_pack=_per_pack(catalog, "transport-belt", p["item"]))
        for p in belts["ports"]
        if p["direction"] == "input"
    ]
    entities = list(ins["entities"]) + list(belts["entities"]) + row
    tiles = set()
    for e in entities + [e for g in growth for e in g]:
        tiles |= _GEOMETRY._occupied_by_plan([e])
    xs, ys = [t[0] for t in tiles], [t[1] for t in tiles]
    x0, y0, x1, y1 = min(xs) - 1.5, min(ys) - 1.5, max(xs) + 1.5, max(ys) + 1.5
    x1 += (x1 - x0) % 2
    y1 += (y1 - y0) % 2
    plan = _plan(
        entities,
        inputs + [ports_out],
        template="green_split_row_v1",
        form="split",
        machine=AM1,
        count=built,
        frame=frame,
        rated_per_minute=built * _nominal(catalog, PACK),
        frame_rate_per_minute=rate,
        reserved_area=[[x0, y0], [x1, y1]],
        growth_slots=growth,
        cells={
            "inserters": {"entities": list(ins["entities"])},
            "belts": {"entities": list(belts["entities"])},
            "packs": {"entities": row},
        },
        sub_clusters={"inserter": _summary(ins), "transport-belt": _summary(belts)},
    )
    return _shift(plan, origin[0] - x0, origin[1] - y0)


def _per_pack(catalog: Any, sub: str, item: str) -> float:
    """Raw ``item`` per green pack through the ``sub`` intermediate (pack recipe amount x the sub's raw draw)."""
    from .manufacturing import raw_demand

    recipe = catalog.recipes[PACK]
    made = sum(p["amount"] for p in recipe["products"] if p["name"] == PACK) or 1
    amount = sum(i["amount"] for i in recipe["ingredients"] if i["name"] == sub) / made
    return round(amount * raw_demand(catalog, sub, 1.0)["raw_items_per_minute"].get(item, 0.0), 6)


def _summary(plan: dict) -> dict:
    return {
        "nodes": plan["nodes"],
        "edges": [{k: e[k] for k in ("item", "from", "to", "per_minute", "inserters")} for e in plan["edges"]],
        "rated_per_minute": plan["rated_per_minute"],
    }


def _span(a: float, b: float) -> list[float]:
    return [a + i for i in range(int(round(b - a)) + 1)] if b >= a else []


def audit_split(catalog: Any, plan: dict) -> list[str]:
    """Hard findings: overlap, every inserter between a belt and a machine, both intermediates on their own
    lane of the intermediate belt, every pack slot (built or reserved) fed and reaching the output port, the
    pack row's poles one network, lane loads at the full frame within capacity."""
    from .blocks.params import inserter_rate, lane_capacity
    from .lanes import inserter_output_lane

    findings = []
    growth = [e for g in plan.get("growth_slots") or [] for e in g]
    es = list(plan["entities"]) + growth
    taken = {}
    for e in es:
        for t in _GEOMETRY._occupied_by_plan([e]):
            if t in taken:
                findings.append(f"overlap:{e['name']}/{taken[t]}@{t}")
            taken[t] = e["name"]
    belts = {(_x(e), _y(e)): e for e in es if e["name"] == BELT}
    machines = {t: e for e in es if e["name"] == AM1 for t in _GEOMETRY._occupied_by_plan([e])}
    lanes: dict = {}
    for e in es:
        if e["name"] != INSERTER:
            continue
        v = VECTORS[e["direction"]]
        pickup, drop = (_x(e) + v[0], _y(e) + v[1]), (_x(e) - v[0], _y(e) - v[1])
        for end in (pickup, drop):
            if end not in belts and end not in machines:
                findings.append(f"inserter_end_free:{_x(e)},{_y(e)}")
        source = machines.get(pickup)
        if source and drop in belts and source.get("recipe") in ("inserter", "transport-belt"):
            lanes[source["recipe"]] = inserter_output_lane(e["direction"], belts[drop]["direction"])
    if len(lanes) != 2 or len(set(lanes.values())) != 2:
        findings.append(f"intermediates_share_a_lane:{lanes}")
    out = plan["ports"][-1]["position"]
    out = (out["x"], out["y"])
    for e in es:
        if e["name"] != INSERTER:
            continue
        v = VECTORS[e["direction"]]
        if (machines.get((_x(e) + v[0], _y(e) + v[1])) or {}).get("recipe") == PACK:
            p = (_x(e) - v[0], _y(e) - v[1])
            seen = set()
            while p in belts and p != out and p not in seen:
                seen.add(p)
                d = belts[p]["direction"]
                p = (p[0] + VECTORS[d][0], p[1] + VECTORS[d][1])
            if p != out:
                findings.append(f"packs_do_not_reach_the_port:{_x(e)},{_y(e)}")
    poles = [(_x(e), _y(e)) for g in plan["growth_slots"] for e in g if e["name"] == POLE]
    poles = [(_x(e), _y(e)) for e in plan["cells"]["packs"]["entities"] if e["name"] == POLE] + poles
    for a, b in zip(poles, poles[1:]):
        if math.dist(a, b) > 7.5:
            findings.append(f"pack_row_poles_apart:{a}-{b}")
    rate = float(plan["frame_rate_per_minute"])
    arm = inserter_rate(catalog, INSERTER)
    if rate > lane_capacity(catalog, BELT):
        findings.append("lane_capacity_exceeded")
    for name, summary in plan["sub_clusters"].items():
        if float(summary["rated_per_minute"]) + 1e-9 < rate:
            findings.append(f"sub_cluster_short:{name}")
    if 2 * _nominal(catalog, PACK) > arm + 1e-9:
        findings.append("pack_input_arm_short")
    return sorted(set(findings))


def describe(catalog: Any, genome: GreenGenome, plan: dict, rate: float) -> dict:
    """A comparable descriptor (``layout_selection.score`` keys + growth story)."""
    from .blocks.params import inserter_rate
    from .layout_variants import energy_metrics, raw_cost

    names = Counter(e["name"] for e in plan["entities"])
    (x0, y0), (x1, y1) = plan["reserved_area"]
    try:
        raw = raw_cost(catalog, dict(names))
    except (KeyError, TypeError, ValueError):
        raw = dict(names)
    each = _nominal(catalog, PACK)
    if genome.form == "split":
        arm = inserter_rate(catalog, INSERTER)
        top = min(plan["frame"] * each, arm)  # each sub-cluster's one output arm
        growth = {
            "kind": "in_place",
            "slots": plan["frame"] - plan["count"],
            "max_rate_in_place": round(top, 3),
            "limit": "sub_cluster_output_arm" if top < plan["frame"] * each else "frame_slots",
            "beyond": "second_split_row",
            # The reserved pack slots are built by science_growth {pack, machines} (pack_growth.py).
            "capability": "science_growth",
        }
        sides = ["W"]
        relation = "opposite"
    else:
        growth = {
            "kind": "none",
            "slots": 0,
            "max_rate_in_place": round(each, 3),
            "limit": "ratio_flip",
            "beyond": "second_cluster_or_rebuild",
        }
        sides = ["W"]
        relation = "placement"
    metrics = energy_metrics(catalog, genome, plan)
    return {
        "id": genome.key(),
        "form": genome.form,
        "footprint": [int(x1 - x0), int(y1 - y0)],
        "area": int((x1 - x0) * (y1 - y0)),
        "counts": {
            "machines": sum(1 for e in plan["entities"] if e.get("recipe")),
            "belts": names[BELT],
            "inserters": names[INSERTER],
            "poles": names[POLE],
            "entities": len(plan["entities"]),
        },
        "raw_cost": raw,
        "raw_total": round(sum(raw.values()), 1),
        "input_sides": sides,
        "output_side": "E" if genome.form == "split" else "chosen",
        "port_relation": relation,
        "aspect": "flat" if (y1 - y0) >= 1.4 * (x1 - x0) else ("tall" if (x1 - x0) >= 1.4 * (y1 - y0) else "square"),
        "rate_now": round(min(float(plan.get("rated_per_minute") or each), each * plan.get("count", 1)), 3),
        "rate_requested": float(rate),
        **metrics,
        "growth": growth,
    }


def compact(descriptor: dict) -> dict:
    keep = (
        "id",
        "form",
        "footprint",
        "port_relation",
        "aspect",
        "input_sides",
        "output_side",
        "rate_now",
        "rate_requested",
        "raw_total",
        "power_kw",
        "pollution_per_min",
    )
    out = {k: descriptor[k] for k in keep}
    out["counts"] = {k: descriptor["counts"][k] for k in ("machines", "belts", "inserters", "poles")}
    out["growth"] = {k: descriptor["growth"][k] for k in ("kind", "slots", "max_rate_in_place", "limit")}
    return out
