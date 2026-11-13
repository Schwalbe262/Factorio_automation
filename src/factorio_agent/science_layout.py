"""Red science block as a layout family: genome -> science part + lab part + pack link.

``docs/v2/LAYOUT_PRINCIPLES.md`` ("Science blocks"). Like the produce rows
(``layout_variants``), the block is never stored as entities: a
:class:`ScienceGenome` names the few choices and :func:`compose` rebuilds the
canonical block from them, pure and deterministic. Every candidate is audited
(:func:`audit`, a failing genome is dropped, never repaired), described
(:func:`describe`), binned into a niche and ranked on the real site by
``layout_selection.score`` (``science.AutomaticScience.prepare``).

Genome fields:

* ``machines``: the pack machine row. ``single`` = the proven template
  (``science.science_row``): one assembler row between a plate/gear input belt
  and a pack belt, one shared gear machine at the head side-loading gears;
  ``direct`` = direct-insertion pairs feeding a pack belt: per two pack machines
  one gear machine between them (``[R][G][R]``, one inserter per pair edge), iron
  and copper side-loaded onto the two lanes of one input belt at its head.
* ``labs``: the lab arrangement along the lab belt (westbound in the canonical
  frame, its head at the east end where red packs enter). ``row`` = the proven
  eight-lab row (``science.lab_row``, the only one a later blue extension knows,
  ``chemical_science.blue_lab_extension``); ``both_sides`` = labs on both sides of
  a half-length belt; ``chain`` = a belt-fed row and a second row behind its first
  columns, fed lab-to-lab by inserters.
* ``lab_slots``: lab slots reserved (8).

Composition (canonical, block corner at the origin): the lab part on top, the
machine part below, plate inputs at the west end (the bus edge after the site turn),
packs flowing east. The pack link is part of the generated block: the pack belt runs
on to the link column one tile east of the lab head, north up that column and west
into the head (the column the routed link of the historical block took; green
science's lab entry tunnels under it, ``manufacturing.lab_green_entry_outside``).
Only turns, no side-load, so red stays on one lane of the lab belt. When the machine
row is longer than the lab part, the labs move east until their head meets the
column. The link entities live outside both part reservations and are published
with the block's routes (``science.routes_block``).

Staged growth (``science_growth``): the machine part reserves ``frame`` slots (the
frame sized from the research bill), the lab part ``lab_slots``; slot entities come
first in each plan, in build order (``slot_shapes`` declares a direct pair's two slot
shapes), so a built stage is a prefix; ``frame_slot_groups`` never widens a generated
frame. Growth into a reserved slot never touches built entities (audited per prefix).

Descriptor (:func:`describe`): rate at the start stage and at the full frame, the
in-place ceiling and what caps it (frame, the head gear arm), footprint, raw plates of
the full frame and of the start, entities now (start stage) and at the full frame,
power (start / full) and pollution, plate draw per minute at full frame, lane loads,
growth slots, whether green's lab entry fits and whether the blue lab extension can
be added later (``blue_lab_extension``; a lab part that cannot carries
``rebuild_ahead``: its later rebuild in tile-equivalents, a score term).
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .board import entity
from .core.deterministic_builder import FactoryBuilder, _plan
from .routing import VECTORS
from .smelting import port

GENERATOR_VERSION = "science-block-v1"
SECTION = "science_block"  # key of this family in data/layouts/automation-science-pack.json
MACHINE_FORMS = ("single", "direct")
LAB_FORMS = ("row", "both_sides", "chain")
LAB_SLOTS = 8
RED = "automation-science-pack"
GEAR = "iron-gear-wheel"
AM1 = "assembling-machine-1"
INSERTER = "inserter"
POLE = "small-electric-pole"
BELT = "transport-belt"
SLOT_A = (AM1, INSERTER, INSERTER, POLE, AM1, INSERTER, INSERTER)  # pack machine + its gear machine
SLOT_B = (AM1, INSERTER, INSERTER, POLE, INSERTER, POLE)  # the pair's second pack machine
ELECTRIC = frozenset({AM1, "assembling-machine-2", "lab", INSERTER, "long-handed-inserter", "fast-inserter"})
MACHINES = frozenset({AM1, "assembling-machine-2", "lab"})
ROW_LAB_AREA = 32 * 16  # the eight-lab row's reservation (``science.lab_row``)
POLE_RADIUS = 2.5
POLE_REACH = 7.5
_GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)


@dataclass(frozen=True)
class ScienceGenome:
    """The choices a red science block is rebuilt from (module doc)."""

    machines: str = "single"
    labs: str = "row"
    lab_slots: int = LAB_SLOTS
    modules: tuple = ()  # reserved, as ``layout_variants.Genome``
    beacons: tuple = ()

    def key(self) -> str:
        return f"sci-{self.machines}-{self.labs.replace('_', '')}"

    def to_dict(self) -> dict:
        return {
            "form": "science",
            "machines": self.machines,
            "labs": self.labs,
            "lab_slots": self.lab_slots,
            "modules": {},
            "beacons": [],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ScienceGenome":
        if data.get("form", "science") != "science":
            raise ValueError("not a science block genome")
        return cls(str(data["machines"]), str(data["labs"]), int(data.get("lab_slots", LAB_SLOTS)))


def genomes() -> list[ScienceGenome]:
    """The bounded genome space (every machine form x lab arrangement)."""
    return [ScienceGenome(m, lab) for m in MACHINE_FORMS for lab in LAB_FORMS]


# ----- parts ------------------------------------------------------------------------------
def _x(e) -> float:
    return e["position"]["x"]


def _y(e) -> float:
    return e["position"]["y"]


def direct_row(x, y, frame):
    """Direct-insertion pairs ``[R][G][R]`` on one plate belt (module doc); ``(x, y)`` = first pack machine."""
    es, shapes = [], []
    for i in range(frame):
        pair, second = divmod(i, 2)
        base = x + 12 * pair
        if not second:
            es += [
                entity(AM1, base, y, recipe=RED),
                entity(INSERTER, base, y - 2, 0),  # copper (and nothing else) from the plate belt
                entity(INSERTER, base, y + 2, 0),  # packs onto the pack belt
                entity(POLE, base + 2, y),
                entity(AM1, base + 4, y, recipe=GEAR),
                entity(INSERTER, base + 4, y - 2, 0),  # iron into the gear machine
                entity(INSERTER, base + 2, y + 1, 4),  # gears into the first pack machine
            ]
            shapes.append(list(SLOT_A))
        else:
            xb = base + 8
            es += [
                entity(AM1, xb, y, recipe=RED),
                entity(INSERTER, xb, y - 2, 0),
                entity(INSERTER, xb, y + 2, 0),
                entity(POLE, xb - 2, y),
                entity(INSERTER, xb - 2, y + 1, 12),  # gears into the second pack machine
                entity(POLE, xb + 2, y),  # wire link to the next pair
            ]
            shapes.append(list(SLOT_B))
    last_machine = max(_x(e) for e in es if e["name"] == AM1)
    end = max(max(_x(e) for e in es), last_machine + 1)  # the pack belt ends past the plate belt (link column)
    es += [entity(BELT, xx, y - 3, 4) for xx in _span(x - 3, last_machine + 1)]
    es += [entity(BELT, xx, y + 3, 4) for xx in _span(x, end)]
    es += [
        entity(BELT, x - 3, y - 4, 8),
        entity(BELT, x - 4, y - 4, 4),
        entity(BELT, x - 3, y - 2, 0),
        entity(BELT, x - 4, y - 2, 4),
    ]
    ports = [
        dict(port("copper-plate", x - 4, y - 4, 4), lane="left"),
        dict(port("iron-plate", x - 4, y - 2, 4), lane="right"),
        port(RED, end, y + 3, 4, "output"),
    ]
    return _plan(
        es,
        ports,
        template="permanent_red_science_row",
        form="direct",
        count=frame,
        slot_shapes=shapes[:2],
        reserved_area=[[x - 5.5, y - 5.5], [end + 0.5, y + 4.5]],
    )


def _span(a: float, b: float) -> list[float]:
    return [a + i for i in range(int(round(b - a)) + 1)] if b >= a else []


def lab_part(form, x, y, slots=LAB_SLOTS):
    """The lab arrangement ``form`` with its first lab at ``(x, y)`` and the belt at ``y - 3`` (module doc)."""
    from .science import lab_row

    if form == "row":
        if slots != LAB_SLOTS:
            raise ValueError("the lab row template holds eight labs")
        return dict(lab_row(x, y, slots), arrangement="row")
    es = []
    if form == "both_sides":
        for i in range(slots):
            column, north = divmod(i, 2)
            lx = x + 4 * column
            if not north:
                es += [entity("lab", lx, y), entity(INSERTER, lx, y - 2, 0), entity(POLE, lx + 2, y)]
            else:
                es += [entity("lab", lx, y - 6), entity(INSERTER, lx, y - 4, 8), entity(POLE, lx + 2, y - 6)]
    elif form == "chain":
        columns = math.ceil((slots + 1) / 2)
        behind = slots - columns  # second-row labs, under the first columns only (green's entry passes the last)
        for column in range(columns):
            lx = x + 4 * column
            es += [entity("lab", lx, y), entity(INSERTER, lx, y - 2, 0), entity(POLE, lx + 2, y)]
            if column < behind:
                es += [entity("lab", lx, y + 4), entity(INSERTER, lx, y + 2, 0), entity(POLE, lx + 2, y + 4)]
    else:
        raise ValueError(f"unknown lab arrangement {form}")
    head = max(_x(e) for e in es if e["name"] == "lab") + 2
    es += [entity(BELT, xx, y - 3, 12) for xx in _span(x - 1, head)]
    return _plan(
        es,
        [port(RED, head, y - 3, 12)],
        template="belt_fed_lab_row",
        arrangement=form,
        count=slots,
        reserved_area=[[x - 1.5, y - 8.5], [head + 0.5, y + 7.5]],
    )


def compose(genome: ScienceGenome, frame: int, ox: int = 0, oy: int = 0) -> dict:
    """The canonical block of ``genome`` with ``frame`` machine slots, corner at ``(ox, oy)`` (module doc).

    ``{"science", "labs", "link": {"entities"}, "reserved_area", "genome"}``; ValueError when
    the genome does not generate.
    """
    from .science import science_row

    if genome.modules or genome.beacons:
        raise ValueError("module slots and beacons are reserved genome fields; placing them is not implemented")
    if not 1 <= frame <= 12:
        raise ValueError("science frame must hold 1..12 machine slots")
    lab_x, lab_y = ox + 1.5, oy + 8.5
    labs = lab_part(genome.labs, lab_x, lab_y, genome.lab_slots)
    if genome.machines == "single":
        science = dict(science_row(ox + 8.5, oy + 22.5, frame, frame=frame), form="single")
    elif genome.machines == "direct":
        science = direct_row(ox + 5.5, oy + 21.5, frame)
    else:
        raise ValueError(f"unknown machine form {genome.machines}")
    out = science["ports"][-1]["position"]
    lab_max = max(_x(e) for e in labs["entities"] if e["name"] == "lab")
    column = max(out["x"] + 1, lab_max + 3)
    if column > lab_max + 3:  # a longer machine row: the labs move east until their head meets the column
        labs = lab_part(genome.labs, lab_x + (column - lab_max - 3), lab_y, genome.lab_slots)
    belt_y = lab_y - 3
    link = [entity(BELT, xx, out["y"], 4) for xx in _span(out["x"] + 1, column - 1)]
    link.append(entity(BELT, column, out["y"], 0))
    link += [entity(BELT, column, yy, 0) for yy in _span(belt_y + 1, out["y"] - 1)]
    link.append(entity(BELT, column, belt_y, 12))
    bottom = science["reserved_area"][1][1]
    # Even sides: a quarter turn about the block centre keeps tiles on the grid (``site.fit``).
    east = column + 0.5 + (column + 0.5 - ox) % 2
    bottom += (bottom - oy) % 2
    return {
        "science": science,
        "labs": labs,
        "link": {"entities": link},
        "reserved_area": [[ox, oy], [east, bottom]],
        "genome": genome.to_dict(),
    }


def entities(block: dict) -> list[dict]:
    return list(block["science"]["entities"]) + list(block["labs"]["entities"]) + list(block["link"]["entities"])


# ----- audit ------------------------------------------------------------------------------
def _tiles(e) -> set:
    return _GEOMETRY._occupied_by_plan([e])


def _key(e):
    return (e["name"], _x(e), _y(e))


def stage_entities(block: dict, machines: int, labs: int) -> list[dict]:
    """The block with only the first ``machines`` / ``labs`` slots built (belts, head and link always)."""
    from .science_growth import lab_slots, machine_slots

    deferred = set()
    for group in (machine_slots(block["science"]) or [])[machines:]:
        deferred |= {_key(e) for e in group}
    for group in (lab_slots(block["labs"]) or [])[labs:]:
        deferred |= {_key(e) for e in group}
    return [e for e in entities(block) if _key(e) not in deferred]


def _endpoints(e):
    vx, vy = VECTORS[e["direction"]]
    x, y = _x(e), _y(e)
    return (x + vx, y + vy), (x - vx, y - vy)


def _power_findings(es: list[dict]) -> list[str]:
    poles = [(_x(e), _y(e)) for e in es if e["name"] == POLE]
    out = []
    for e in es:
        if e["name"] not in ELECTRIC:
            continue
        if not any(
            any(abs(tx - px) < POLE_RADIUS + 0.5 and abs(ty - py) < POLE_RADIUS + 0.5 for tx, ty in _tiles(e))
            for px, py in poles
        ):
            out.append(f"unpowered:{e['name']}@{_x(e)},{_y(e)}")
    if poles:
        seen, frontier = {poles[0]}, [poles[0]]
        while frontier:
            p = frontier.pop()
            for q in poles:
                if q not in seen and math.dist(p, q) <= POLE_REACH + 1e-9:
                    seen.add(q)
                    frontier.append(q)
        if len(seen) != len(poles):
            out.append(f"poles_not_one_network:{len(poles) - len(seen)}")
    return out


def _belt_trace(belts: dict, start, stop: set) -> tuple[list, str | None]:
    """Tiles from ``start`` along the belt facings until a ``stop`` tile; the lane changes on the way.

    Returns ``(path, side_load)``: ``side_load`` names the first tile where the belt runs into the
    side of another (a side-load moves the items to that side's lane), None when every joint is a
    straight run or a curve (a perpendicular feed into a belt with no other input keeps the lane).
    """
    path, p, seen = [start], start, {start}
    while p not in stop:
        d = belts[p]["direction"]
        q = (p[0] + VECTORS[d][0], p[1] + VECTORS[d][1])
        if q not in belts or q in seen:
            return path, "dead_end" if q not in belts else "cycle"
        dq = belts[q]["direction"]
        if dq != d:
            behind = (q[0] - VECTORS[dq][0], q[1] - VECTORS[dq][1])
            feeders = [
                s
                for s in ((q[0] + VECTORS[k][0], q[1] + VECTORS[k][1]) for k in VECTORS if k not in (dq, (dq + 8) % 16))
                if s in belts
                and (s[0] + VECTORS[belts[s]["direction"]][0], s[1] + VECTORS[belts[s]["direction"]][1]) == q
            ]
            fed_behind = behind in belts and belts[behind]["direction"] == dq
            if fed_behind or len(feeders) != 1:
                return path + [q], f"side_load@{q}"
        path.append(q)
        seen.add(q)
        p = q
    return path, None


def audit(catalog: Any, block: dict, *, start_machines: int = 2, start_labs: int = 2) -> list[str]:
    """Hard findings of one composed block (``[]`` = OK; module doc).

    Slot structure, no overlap, every entity of a part inside its reservation, every inserter
    between a belt and a machine (or two machines / labs), every pack machine's packs reach
    the lab belt on one lane by turns only, every lab reachable by packs, every stage prefix
    (machines 1..frame x labs 1..lab slots) powered by one pole network with every built
    inserter's both ends built, lane loads at the full frame within the lane capacity, the
    shape rules of the machine part (``cluster_audit.layout_findings``: no crossing, no belt
    cycle, belt and density bounds) and green's lab entry fitting beside the lab part.
    """
    from .blocks.cluster_audit import layout_findings
    from .blocks.params import lane_capacity
    from .science_growth import lab_slots, machine_slots

    findings = []
    science, labs = block["science"], block["labs"]
    slots, lab_groups = machine_slots(science), lab_slots(labs)
    if not slots or len(slots) != science.get("count"):
        return ["machine_slots_unreadable"]
    if not lab_groups or len(lab_groups) != labs.get("count"):
        return ["lab_slots_unreadable"]
    es = entities(block)
    taken: dict = {}
    for e in es:
        for t in _tiles(e):
            if t in taken:
                findings.append(f"overlap:{e['name']}/{taken[t]}@{t}")
            taken[t] = e["name"]
    for part in (science, labs):
        (x0, y0), (x1, y1) = part["reserved_area"]
        for e in part["entities"]:
            if any(not (x0 < tx < x1 and y0 < ty < y1) for tx, ty in _tiles(e)):
                findings.append(f"outside_reservation:{e['name']}@{_x(e)},{_y(e)}")
    (bx0, by0), (bx1, by1) = block["reserved_area"]
    for e in block["link"]["entities"]:
        if not (bx0 < _x(e) < bx1 and by0 < _y(e) < by1):
            findings.append(f"link_outside_block:{_x(e)},{_y(e)}")
    belts = {(_x(e), _y(e)): e for e in es if e["name"] == BELT}
    machine_tiles = {t: e for e in es if e["name"] in MACHINES for t in _tiles(e)}
    for e in es:
        if e["name"] != INSERTER:
            continue
        for end in _endpoints(e):
            if end not in belts and end not in machine_tiles:
                findings.append(f"inserter_end_free:{_x(e)},{_y(e)}")
    # Packs: every pack machine's output reaches the lab belt, on one lane, by turns only.
    head = (labs["ports"][0]["position"]["x"], labs["ports"][0]["position"]["y"])
    lab_belts = set()  # the lab belt as packs ride it: from the head along the facings
    p = head
    while p in belts and p not in lab_belts:
        lab_belts.add(p)
        d = belts[p]["direction"]
        p = (p[0] + VECTORS[d][0], p[1] + VECTORS[d][1])
    lanes = set()
    from .lanes import inserter_output_lane

    for e in es:
        if e["name"] != INSERTER:
            continue
        pickup, drop = _endpoints(e)
        source = machine_tiles.get(pickup)
        if not source or source.get("recipe") != RED or drop not in belts:
            continue
        lanes.add(inserter_output_lane(e["direction"], belts[drop]["direction"]))
        path, side = _belt_trace(belts, drop, {head})
        if path[-1] != head:
            findings.append(f"packs_do_not_reach_labs:{_x(e)},{_y(e)}:{side}")
        elif side:
            findings.append(f"pack_lane_changes:{side}")
    if len(lanes) > 1:
        findings.append("packs_on_both_lanes")
    elif lanes and lanes != {"right"}:
        findings.append("packs_on_the_green_lane")  # green side-loads onto the left lane (lab_green_entry)
    # Labs: fed from the lab belt or from a fed lab (chain).
    lab_of = {t: e for e in labs["entities"] if e["name"] == "lab" for t in _tiles(e)}
    arms = [_endpoints(e) for e in labs["entities"] if e["name"] == INSERTER]
    fed: set = set()
    changed = True
    while changed:
        changed = False
        for pickup, drop in arms:
            target = lab_of.get(drop)
            if target is None or _key(target) in fed:
                continue
            if pickup in lab_belts or (pickup in lab_of and _key(lab_of[pickup]) in fed):
                fed.add(_key(target))
                changed = True
    for e in labs["entities"]:
        if e["name"] == "lab" and _key(e) not in fed:
            findings.append(f"lab_unreachable:{_x(e)},{_y(e)}")
    # Stages: each prefix powered and every built inserter has both ends built.
    # Each part has its own pole network (``science.prepare`` links each to the grid).
    labs_now = min(start_labs, len(lab_groups))
    for m in range(1, len(slots) + 1):
        for n in sorted({1, labs_now, len(lab_groups)}) if m in (1, len(slots)) else (labs_now,):
            built = stage_entities(block, m, n)
            filled = {t for e in built if e["name"] in MACHINES or e["name"] == BELT for t in _tiles(e)}
            for e in built:
                if e["name"] == INSERTER and any(end not in filled for end in _endpoints(e)):
                    findings.append(f"stage_{m}m{n}l_inserter_end_unbuilt:{_x(e)},{_y(e)}")
            keys = {_key(e) for e in built}
            for part in (science, labs):
                own = [e for e in part["entities"] if _key(e) in keys]
                findings += [f"stage_{m}m{n}l_{f}" for f in _power_findings(own)]
    # Bandwidth at the full frame.
    loads = lane_loads(catalog, block)
    capacity = lane_capacity(catalog, BELT)
    for name, load in loads.items():
        if load > capacity + 1e-9:
            findings.append(f"lane_capacity_exceeded:{name}:{load:g}>{capacity:g}")
    shape = [f for f in layout_findings(catalog, science, slots=len(slots)) if not f.startswith("belt_wraps_machine")]
    findings += [f"shape:{f}" for f in shape]
    if not green_entry_fits(block):
        findings.append("green_lab_entry_blocked")
    return sorted(set(findings))


def green_entry_fits(block: dict) -> bool:
    """Green's lab entry (``manufacturing.lab_green_entry_outside``) clears the block and leaves it."""
    from .manufacturing import lab_green_entry_outside

    try:
        entry = lab_green_entry_outside(block["labs"])
    except (ValueError, KeyError):
        return False
    own = set().union(*(_tiles(e) for e in entities(block)))
    if any(_tiles(e) & own for e in entry["entities"]):
        return False
    (x0, y0), (x1, y1) = block["reserved_area"]
    p = entry["port"]["position"]
    return not (x0 < p["x"] < x1 and y0 < p["y"] < y1)


def _nominal(catalog: Any, recipe: str) -> float:
    speed = float(((catalog.entities.get(AM1) or {}).get("crafting_speed")) or 0.5)
    return 60 * speed / float(catalog.recipes[recipe]["energy"])


def _amount(catalog, recipe, item) -> float:
    return sum(float(r["amount"]) for r in catalog.recipes[recipe]["ingredients"] if r["name"] == item)


def plate_draw(catalog: Any, machines: int) -> dict:
    """Iron and copper plates per minute ``machines`` red machines draw at nameplate."""
    packs = machines * _nominal(catalog, RED)
    gears_per_pack = _amount(catalog, RED, GEAR)
    gear_yield = sum(float(p["amount"]) for p in catalog.recipes[GEAR]["products"] if p["name"] == GEAR) or 1.0
    iron = packs * gears_per_pack * _amount(catalog, GEAR, "iron-plate") / gear_yield
    return {"copper-plate": round(packs * _amount(catalog, RED, "copper-plate"), 3), "iron-plate": round(iron, 3)}


def lane_loads(catalog: Any, block: dict) -> dict:
    """Items/minute per belt lane at the full frame: plate lanes, gear lane, pack lanes."""
    frame = block["science"]["count"]
    packs = frame * _nominal(catalog, RED)
    draw = plate_draw(catalog, frame)
    gears = packs * _amount(catalog, RED, GEAR)
    if block["science"].get("form") == "direct":
        loads = {"plate_belt.copper": draw["copper-plate"], "plate_belt.iron": draw["iron-plate"]}
    else:
        loads = {
            "plate_belt.copper": draw["copper-plate"],
            "plate_belt.gears": gears,
            "iron_column": draw["iron-plate"],
        }
    loads.update({"pack_belt": round(packs, 3), "lab_belt.red": round(packs, 3)})
    return {k: round(v, 3) for k, v in loads.items()}


def ceiling(catalog: Any, block: dict) -> tuple[float, str]:
    """``(packs/min, limit)`` the block reaches in place: the frame, or the head gear arm of a single row."""
    from .blocks.params import inserter_rate

    frame = block["science"]["count"]
    full = frame * _nominal(catalog, RED)
    if block["science"].get("form") != "direct":
        arm = inserter_rate(catalog, INSERTER)  # one arm moves every gear onto the plate belt
        per_pack = _amount(catalog, RED, GEAR)
        if per_pack and arm / per_pack < full - 1e-9:
            return arm / per_pack, "gear_output_inserter"
    return full, "frame_slots"


# ----- descriptor ---------------------------------------------------------------------------
def describe(catalog: Any, genome: ScienceGenome, block: dict, request: dict) -> dict:
    from .layout_selection import W_AREA, W_ENTITY
    from .layout_variants import _side, energy_metrics, raw_cost

    frame = block["science"]["count"]
    start = max(1, min(int(request.get("start_machines", 2)), frame))
    start_labs = max(1, min(int(request.get("start_labs", 2)), genome.lab_slots))
    full = entities(block)
    now = stage_entities(block, start, start_labs)
    names = Counter(e["name"] for e in full)
    (x0, y0), (x1, y1) = block["reserved_area"]
    w, h = int(x1 - x0), int(y1 - y0)
    inputs = [p for p in block["science"]["ports"] if p["direction"] == "input"]
    ports = [
        {
            "item": p["item"],
            "role": "in",
            "side": _side(p, block["reserved_area"]),
            **({"lane": p["lane"]} if p.get("lane") else {}),
        }
        for p in inputs
    ]
    input_sides = sorted({p["side"] for p in ports}, key="NESW".index)
    along, depth = (h, w)  # plates enter on the west end: the input edge is the block's height
    aspect = "flat" if along >= 1.4 * depth else ("tall" if depth >= 1.4 * along else "square")

    def raw(counts):
        try:
            return raw_cost(catalog, dict(counts))
        except (KeyError, TypeError, ValueError):  # a catalog without the entity recipes: count items
            return dict(counts)

    raw_full = raw(names)
    raw_now = raw(Counter(e["name"] for e in now))
    per = _nominal(catalog, RED)
    top, limit = ceiling(catalog, block)
    lab_entities = len(block["labs"]["entities"])
    blue = blue_extension_fits(block)
    power_now = energy_metrics(catalog, genome, {"entities": now})
    power_full = energy_metrics(catalog, genome, {"entities": full})
    return {
        "id": genome.key(),
        "form": "science",
        "machines": genome.machines,
        "labs": genome.labs,
        "footprint": [w, h],
        "area": w * h,
        "counts": {
            "machines": sum(1 for e in now if e.get("recipe")),
            "belts": sum(1 for e in now if e["name"] == BELT),
            "inserters": sum(1 for e in now if e["name"] == INSERTER),
            "poles": sum(1 for e in now if e["name"] == POLE),
            "labs": sum(1 for e in now if e["name"] == "lab"),
            "entities": len(now),  # built now (the start stage): the build-time term of the score
        },
        "counts_full": {
            "machines": sum(1 for e in full if e.get("recipe")),
            "labs": names["lab"],
            "belts": names[BELT],
            "entities": len(full),
        },
        "raw_cost": raw_full,
        "raw_total": round(sum(raw_full.values()), 1),  # the full frame: what the block costs as it grows
        "raw_start": round(sum(raw_now.values()), 1),
        "ports": ports,
        "input_sides": input_sides,
        "output_side": "-",
        "port_relation": "internal",
        "aspect": aspect,
        "edges": {
            GEAR: "direct" if genome.machines == "direct" else "belt",
            "copper-plate": "port",
            "iron-plate": "port",
        },
        "rate_now": round(start * per, 3),
        "rate_full": round(frame * per, 3),
        "rate_requested": float(request.get("rate", 0)),
        "power_kw": power_now["power_kw"],
        "power_kw_full": power_full["power_kw"],
        "pollution_per_min": power_now["pollution_per_min"],
        "pollution_per_min_full": power_full["pollution_per_min"],
        "plates_per_minute_full": plate_draw(catalog, frame),
        "lane_loads": lane_loads(catalog, block),
        "growth": {
            "kind": "in_place",
            "slots": frame - start,
            "lab_slots": genome.lab_slots - start_labs,
            "max_rate_in_place": round(top, 3),
            "limit": limit,
            "beyond": "second_block_or_rebuild",
        },
        "green_entry": green_entry_fits(block),
        "blue_lab_extension": blue,
        # A lab part the blue extension cannot use is rebuilt later as the eight-lab row: every lab
        # part entity mined and placed again, and the row's reservation found then.
        "rebuild_ahead": 0.0 if blue else round(2 * W_ENTITY * lab_entities + W_AREA * ROW_LAB_AREA, 1),
    }


def blue_extension_fits(block: dict) -> bool:
    from .chemical_science import blue_lab_extension

    try:
        blue_lab_extension(block["labs"])
    except (ValueError, KeyError):
        return False
    return True


def niche(descriptor: dict) -> str:
    """Archive bin: aspect / machine form / lab arrangement."""
    return "/".join((descriptor["aspect"], descriptor["machines"], descriptor["labs"]))


def archive_cost(descriptor: dict) -> tuple:
    return (
        descriptor["raw_total"],
        descriptor["counts_full"]["belts"],
        descriptor["area"],
        descriptor["counts_full"]["entities"],
        descriptor["id"],
    )


def compact(descriptor: dict) -> dict:
    from .layout_variants import compact as base

    out = base(descriptor)
    out.update(
        machines=descriptor["machines"],
        labs=descriptor["labs"],
        rate_full=descriptor["rate_full"],
        power_kw_full=descriptor["power_kw_full"],
        raw_start=descriptor["raw_start"],
        blue_lab_extension=descriptor["blue_lab_extension"],
        rebuild_ahead=descriptor["rebuild_ahead"],
    )
    out["growth"]["lab_slots"] = descriptor["growth"]["lab_slots"]
    return out


# ----- candidates ----------------------------------------------------------------------------
def request_for(rate: float, frame: int, *, start_machines: int = 2, start_labs: int = 2) -> dict:
    return {
        "product": RED,
        "rate": float(rate),
        "frame": int(frame),
        "start_machines": start_machines,
        "start_labs": start_labs,
    }


def evaluate(catalog: Any, genome: ScienceGenome, request: dict) -> tuple[dict | None, str | None]:
    try:
        block = compose(genome, request["frame"])
    except (ValueError, KeyError) as exc:
        return None, f"does_not_generate: {str(exc)[:120]}"
    findings = audit(
        catalog, block, start_machines=request.get("start_machines", 2), start_labs=request.get("start_labs", 2)
    )
    if findings:
        return None, "audit: " + ",".join(findings[:3])
    descriptor = describe(catalog, genome, block, request)
    return {
        "genome": genome,
        "line": block,
        "descriptor": descriptor,
        "compact": compact(descriptor),
        "niche": niche(descriptor),
    }, None


def library_path(directory: Path | None = None) -> Path:
    from .layout_variants import library_path as base

    return base(RED, directory)


def load_section(directory: Path | None = None) -> dict | None:
    try:
        data = json.loads(library_path(directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    section = data.get(SECTION) if isinstance(data, dict) else None
    return section if isinstance(section, dict) and isinstance(section.get("entries"), dict) else None


def candidates(catalog: Any, request: dict, *, directory: Path | None = None, library: bool = True) -> dict:
    """``{"candidates", "library", "skipped", "generated"}`` like ``layout_variants.candidates``."""
    found, skipped, source = [], [], None
    section = load_section(directory) if library else None
    if section is not None:
        source = f"{library_path(directory).name}#{SECTION}"
        if section.get("generator") != GENERATOR_VERSION:
            skipped.append({"library": source, "why": f"generator {section.get('generator')} != {GENERATOR_VERSION}"})
        else:
            for key, entry in sorted(section["entries"].items()):
                try:
                    genome = ScienceGenome.from_dict(entry["genome"])
                except (KeyError, TypeError, ValueError):
                    skipped.append({"niche": key, "why": "unreadable genome"})
                    continue
                candidate, why = evaluate(catalog, genome, request)
                if candidate is None:
                    skipped.append({"niche": key, "id": genome.key(), "why": why})
                    continue
                candidate.update(source="library", evidence=entry.get("evidence") or {})
                found.append(candidate)
    known = {c["genome"] for c in found}
    pool = genomes()
    for genome in pool:
        if genome in known:
            continue
        candidate, why = evaluate(catalog, genome, request)
        if candidate is None:
            skipped.append({"id": genome.key(), "why": why})
            continue
        candidate.update(source="generated", evidence={})
        found.append(candidate)
    best: dict = {}
    for c in found:  # one per niche; a library entry keeps its niche
        if c["niche"] not in best or (
            best[c["niche"]]["source"] != "library"
            and archive_cost(c["descriptor"]) < archive_cost(best[c["niche"]]["descriptor"])
        ):
            best[c["niche"]] = c
    return {
        "candidates": list(dict(sorted(best.items())).values()),
        "library": source,
        "skipped": skipped,
        "generated": len(pool),
    }


def build_section(catalog: Any, request: dict, *, previous: dict | None = None) -> dict:
    """The ``science_block`` library section (``tools/layouts.py build``); evidence carried over."""
    old = {
        json.dumps(e.get("genome"), sort_keys=True): e.get("evidence") or {}
        for e in ((previous or {}).get("entries") or {}).values()
    }
    entries = {}
    for c in candidates(catalog, request, library=False)["candidates"]:
        genome = c["genome"].to_dict()
        entries[c["niche"]] = {
            "genome": genome,
            "descriptor": compact(c["descriptor"]),
            "scores": {"archive_cost": list(archive_cost(c["descriptor"])[:4])},
            "evidence": old.get(json.dumps(genome, sort_keys=True), {}),
        }
    return {
        "generator": GENERATOR_VERSION,
        "catalog_fingerprint": catalog.fingerprint,
        "request": {k: request[k] for k in ("rate", "frame", "start_machines", "start_labs")},
        "entries": entries,
    }


# ----- site -----------------------------------------------------------------------------------
NAME = "automation_science"


def size(candidate: dict) -> tuple[int, int]:
    (x0, y0), (x1, y1) = candidate["line"]["reserved_area"]
    return int(x1 - x0), int(y1 - y0)


def placements(site, world_id, candidate: dict, frame: int, *, count: int | None = None) -> list:
    """``allocated_placements`` of one candidate (plates towards the tap band), block split per part."""
    from .site.placement import DEFAULT_COUNT, allocated_placements

    genome = candidate["genome"]
    w, h = size(candidate)

    def build(x0, y0):
        return compose(genome, frame, int(x0), int(y0))

    def inputs(block):
        return [p for p in block["science"]["ports"] if p["direction"] == "input"]

    found = []
    for shape in ((h, w), (w, h)) if w != h else ((w, h),):
        found = allocated_placements(
            site,
            world_id,
            kind="science",
            name=NAME,
            size=(w, h),
            block=shape,
            build=build,
            inputs=inputs,
            count=count or DEFAULT_COUNT,
        )
        if found:
            break
    out = []
    for allocation, placement in found:
        block = placement.plan
        transform = placement.transform.to_dict()
        for key in ("science", "labs"):
            block[key]["site_transform"] = transform
        block["allocation"] = allocation
        block["port_cost"] = placement.score
        out.append(block)
    return out


def rank(site, world_id, found: list[dict], frame: int, demand, config, seed) -> tuple[list[dict], dict]:
    """Feasible options best first (each probed on its first allocation) and the exploration choice."""
    from . import layout_selection as selection
    from .site.placement import settle

    options, infeasible = [], []
    for candidate in found[: selection.PROBE_LIMIT]:
        probe = placements(site, world_id, candidate, frame, count=1)
        settle(site, world_id, NAME, None)
        if not probe:
            infeasible.append({"variant": candidate["descriptor"]["id"], "reason": "no_allocation"})
            continue
        options.append(selection.option(candidate, demand, probe[0]["port_cost"]))
    options = selection.ranked(options)
    index, pool = selection.choose([o["score"] for o in options], config, seed) if options else (0, 0)
    return options, {"index": index, "pool": pool, "config": config, "infeasible": infeasible}


def probe_context(candidate: dict, frame: int, ctx) -> float | None:
    """Best ``site.fit`` port cost on a synthetic ``layout_selection.SiteContext`` (tools, tests)."""
    from .layout_selection import _edge_point, _site_rects
    from .site.fit import orientations

    w, h = size(candidate)
    best = None
    for (width, height), target in _site_rects(ctx, (w, h)):
        tap = _edge_point(ctx.bus, width, height)

        def wants(block, tap=tap):
            return [(p, tap, "in", ctx.bus) for p in block["science"]["ports"] if p["direction"] == "input"]

        found = orientations(lambda x0, y0: compose(candidate["genome"], frame, x0, y0), (w, h), target, wants)
        if found and (best is None or found[0].score < best):
            best = found[0].score
    return best


def rank_for_context(found: list[dict], frame: int, demand, ctx) -> list[dict]:
    from . import layout_selection as selection

    options = []
    for candidate in found:
        cost = probe_context(candidate, frame, ctx)
        if cost is not None:
            options.append(selection.option(candidate, demand, cost))
    return selection.ranked(options)


def contexts():
    """Typical science sites: square, shallow (along the bus) and narrow allocations, bus W."""
    from .layout_selection import SiteContext

    return (
        SiteContext("square 40x40, bus W", 40, 40, "W", "E"),
        SiteContext("shallow: 44 along, 28 deep", 44, 28, "W", "E"),
        SiteContext("narrow: 26 along, 44 deep", 26, 44, "W", "E"),
        SiteContext("bus N, 40x40", 40, 40, "N", "S"),
    )


def legacy_record(state: dict) -> dict:
    """The ``layout`` record of a block planned before this family (existing maps keep their block)."""
    form = (state.get("science") or {}).get("form") or "single"
    labs = (state.get("labs") or {}).get("arrangement") or "row"
    genome = ScienceGenome(form, labs)
    return {
        "variant": genome.key() + "-template",
        "niche": "built",
        "source": "built",
        "genome": genome.to_dict(),
        "descriptor": {"machines": form, "labs": labs, "power_kw": None, "pollution_per_min": None},
        "explored": False,
        "chosen_tick": None,
        "verified_tick": None,
    }
