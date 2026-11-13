"""Mining patch as a layout family: genome -> whole-patch frame, descriptor, audit, selection.

``docs/v2/LAYOUT_PRINCIPLES.md`` ("Candidate layouts", "Mine output trunk"). A mine is never
stored as entities: a :class:`MineGenome` names the few choices and :func:`frame` rebuilds the
whole-patch frame from them on a patch survey (``mining_layout_policy.survey_patch`` snapshot),
pure and deterministic, with the Mining Patch Planner principles of ``patch_grid``: paired drill
rows facing one collector, the lattice shift searched for coverage, every collector flowing to
one patch edge, collectors merged by saturation into N trunk belts there.

Genome fields:

* ``spacing``: ``simple`` = the paired bank of ``paired_mining.mining_pair`` (drills one tile
  apart, a pole in each gap, one free service tile between row pairs; what ``patch_grid`` and
  the bank builder lay out); ``compact`` = drills touching along the row, one pole per two
  columns standing on the collector with an underground pair hopping under it, no service lane;
  ``sparse`` = drills spaced so their mining areas tile without overlap (fewest drills covering
  the patch).
* ``edge``: the output edge, a ``mining_layout_policy.FLOWS`` flow (downstream side).
* ``merge``: ``saturate`` = ``ceil`` lanes: a collector joins a trunk belt head-on (both lanes)
  or side-loads onto its near lane while that lane has room, else it opens the next trunk belt
  (2 belts: the 2-2 input-balanced splitter of ``mine_trunk.lane_plan``; more: reserved);
  ``single`` = one trunk belt whatever the frame delivers (the rest clipped).
* ``start_pairs``: paired columns built at the start (staged from the output edge).
* ``drill``: ``electric-mining-drill`` (the burner drill is excluded); ``big-mining-drill`` only
  when the catalog has it, on the ``simple`` spacing.

``buildable``: what the bank builder lays out today (``simple``, ``saturate``, electric); the
others are audited and ranked (``tools/layouts.py show mining-patch``) but never chosen at
runtime (:func:`choose_bank`), as green's split ceiling is not credited before its capability.

Descriptor (:func:`describe`): ore tiles and amount covered, unreachable ore, drills at the full
frame, rate at the start stage and at the full frame (lane- and trunk-capped), belts /
undergrounds / poles now and full, raw cost, kW and pollution now and full, footprint (the frame
rects on the map, ``frame_rects``) and its tile count (the area term), reservations respected,
trunk belts and balancer, growth steps (drills per stage: the start row from its edge-most full
pair, then along the row, then the other rows nearest the start first).

Audit (:func:`audit`, hard): no two entities on one tile, every drill's mining area only this
resource (at least ``GRID_MIN_ORE_TILES``), no frame tile on water, cliffs, other ore or a
reservation, every drill drops onto its row's collector (a belt or underground, never a pole),
every drill inside a pole's supply area and every row's poles one chain, every collector
continuous to its port with underground gaps within reach, and the bandwidth of the merge plan:
no collector lane and no trunk lane above its capacity (``saturate``); ``single`` records the
clipped rate instead.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .board import entity
from .mining_border import tile_rects

GENERATOR_VERSION = "mine-patch-v1"
LIBRARY_ITEM = "mining-patch"  # data/layouts/mining-patch.json
SPACINGS = ("simple", "compact", "sparse")
MERGES = ("saturate", "single")
ELECTRIC = "electric-mining-drill"
BIG = "big-mining-drill"
DRILLS = {
    # size (tiles), mining radius (the prototype's mining_drill_radius; not in the catalog export)
    ELECTRIC: {"size": 3, "radius": 2.49},
    BIG: {"size": 5, "radius": 6.49},
}
EDGES = {"west_to_east": "E", "east_to_west": "W", "north_to_south": "S", "south_to_north": "N"}
POLE = "small-electric-pole"
BELT = "transport-belt"
UNDERGROUND = "underground-belt"
SPLITTER = "splitter"
POLE_SUPPLY = 2.5
POLE_WIRE = 7.5
UNDERGROUND_GAP = 4  # yellow underground: up to four tiles between entrance and exit
GRID_MIN_ORE_TILES = 4
MIN_AMOUNT = 1800  # a drill site holds at least this much ore (``candidate_sites``)
MAX_ROWS = 16
MAX_START_PAIRS = 8


@dataclass(frozen=True)
class MineGenome:
    """The choices a mining patch frame is rebuilt from (module doc)."""

    spacing: str = "simple"
    edge: str = "west_to_east"
    merge: str = "saturate"
    start_pairs: int = 1
    drill: str = ELECTRIC
    modules: tuple = ()  # reserved, as ``layout_variants.Genome``
    beacons: tuple = ()

    def key(self) -> str:
        tier = "" if self.drill == ELECTRIC else "-big"
        return f"mine-{self.spacing}-{EDGES[self.edge]}-{self.merge[:3]}-s{self.start_pairs}{tier}"

    @property
    def buildable(self) -> bool:
        return self.spacing == "simple" and self.merge == "saturate" and self.drill == ELECTRIC

    def to_dict(self) -> dict:
        return {
            "form": "mine",
            "spacing": self.spacing,
            "edge": self.edge,
            "merge": self.merge,
            "start_pairs": self.start_pairs,
            "drill": self.drill,
            "modules": {},
            "beacons": [],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MineGenome":
        if data.get("form", "mine") != "mine":
            raise ValueError("not a mine genome")
        genome = cls(
            str(data["spacing"]),
            str(data.get("edge", "west_to_east")),
            str(data.get("merge", "saturate")),
            int(data.get("start_pairs", 1)),
            str(data.get("drill", ELECTRIC)),
        )
        if genome.spacing not in SPACINGS or genome.edge not in EDGES or genome.merge not in MERGES:
            raise ValueError("unknown mine genome value")
        if genome.drill not in DRILLS or not 1 <= genome.start_pairs <= MAX_START_PAIRS:
            raise ValueError("unknown drill or start size")
        return genome


def drills_available(catalog: Any) -> list[str]:
    """Drill tiers of the family the catalog knows (never the burner drill)."""
    entities = getattr(catalog, "entities", None) or {}
    return [d for d in (ELECTRIC, BIG) if d == ELECTRIC or d in entities]


def genomes(
    catalog: Any, *, edges=None, starts=(1, 2), spacings=SPACINGS, merges=MERGES, tiers=(ELECTRIC,)
) -> list[MineGenome]:
    """The bounded genome space: spacing x edge x merge x start x drill (big only on ``simple``).

    ``tiers`` defaults to the electric drill: the big drill needs research the early game never
    has, and its recipe is not an assembler recipe, so its raw cost is not comparable yet.
    """
    out = []
    for drill in [d for d in drills_available(catalog) if d in tiers]:
        for spacing in spacings:
            if drill != ELECTRIC and spacing != "simple":
                continue
            for edge in edges or EDGES:
                for merge in merges:
                    for start in starts:
                        out.append(MineGenome(spacing, edge, merge, int(start), drill))
    return out


# ----- geometry ------------------------------------------------------------------------------
def pitches(spacing: str, drill: str) -> dict:
    """Lattice of one spacing (canonical EAST frame): ``along`` / ``across`` pitch, row offsets."""
    size = DRILLS[drill]["size"]
    area = 2 * math.floor(DRILLS[drill]["radius"]) + 1
    row_gap = size + 1  # the two drill rows of a pair: drill, collector, drill
    across = {"simple": 2 * size + 2, "compact": 2 * size + 1, "sparse": row_gap + area}[spacing]
    along = {"simple": size + 1, "compact": size, "sparse": area}[spacing]
    return {"along": along, "across": across, "row_gap": row_gap, "collector": row_gap // 2, "size": size}


def _span(a: float, b: float) -> list[float]:
    """Tile centres from ``a`` to ``b`` inclusive (half-tile values)."""
    return [a + i for i in range(int(round(b - a)) + 1)]


def _tiles(x, y, size):
    half = size // 2
    return {(x + i, y + j) for i in range(-half, half + 1) for j in range(-half, half + 1)}


def _row(ore, other, blocked, reserved, bounds, ox, top, columns, genome, radius):
    """One row pair on the lattice (canonical): its drills, poles, collector and frame tiles, or None."""
    from .planning import overlap

    g = pitches(genome.spacing, genome.drill)
    size, along, cy = g["size"], g["along"], top + g["collector"]
    span = math.floor(radius)
    lane_poles = genome.spacing == "compact"

    def blocked_tile(t):
        if t in blocked or t in other:
            return True
        box = [[t[0] - 0.49, t[1] - 0.49], [t[0] + 0.49, t[1] + 0.49]]
        return any(overlap(box, r) for r in reserved)

    def site(x, y):
        if not (bounds[0][0] <= x - radius and x + radius < bounds[1][0]):
            return None
        if not (bounds[0][1] <= y - radius and y + radius < bounds[1][1]):
            return None
        body = _tiles(x, y, size)
        extra = set() if lane_poles else {(x + along // 2, y)}
        if any(blocked_tile(t) for t in body | extra | {(x, cy)}):
            return None
        area = [(x + i, y + j) for i in range(-span, span + 1) for j in range(-span, span + 1)]
        if any(p in other for p in area):
            return None  # a border drill: another resource in its area (``mining_border``)
        cells = [p for p in area if p in ore]
        if len(cells) < GRID_MIN_ORE_TILES or sum(ore[p] for p in cells) < MIN_AMOUNT:
            return None
        return cells

    drills = []
    for x in columns:
        for y, direction in ((top, 8), (top + g["row_gap"], 0)):
            cells = site(x, y)
            if cells is not None:
                drills.append((x, y, direction, cells))
    if not drills:
        return None
    return drills


def frame(snapshot: dict, genome: MineGenome, reservations=(), *, forbidden=(), origin=None) -> dict:
    """The whole-patch frame of ``genome`` on ``snapshot`` (``patch_grid`` principles), map frame.

    Returns ``ok`` and, canonical (EAST flow, for audit and pictures): ``entities``,
    ``rows`` (drills, poles, collector tiles, port per row), ``edge_x``; map frame:
    ``frame_rects``, ``ports``, ``origin``; coverage ``covered_tiles`` / ``covered_amount`` /
    ``unreachable_tiles``, ``shifts_scored``.
    """
    from . import mining_layout_policy as mlp

    resource = snapshot["resource"]
    policy = mlp.make_policy(
        snapshot["world_id"], snapshot["catalog_fingerprint"], resource, flow=genome.edge, site_oriented=True
    )
    transform, tiles, bounds, reserved = mlp._canonical_inputs(snapshot, policy, reservations)
    back = transform.inverse()
    ore = {mlp._xy(t): t["amount"] for t in tiles if t["name"] == resource}
    other = {mlp._xy(t): t["name"] for t in tiles if t["name"] != resource}
    if not ore:
        return {"ok": False, "reason": "mining_frame_patch_has_no_resource"}
    blocked = {back.point(x, y) for x, y in forbidden}
    radius = DRILLS[genome.drill]["radius"] if genome.drill != ELECTRIC else snapshot["native"]["mining_radius"]
    g = pitches(genome.spacing, genome.drill)
    base = policy["grid_origin"]
    if origin is not None:
        shifts = [back.point(origin["x"], origin["y"])]
    else:
        shifts = [(base["x"] + dx, base["y"] + dy) for dx in range(g["along"]) for dy in range(g["across"])]
    xs = [p[0] for p in ore]
    ys = [p[1] for p in ore]
    best = None
    for ox, oy in shifts:
        col0 = ox + g["along"] * math.floor((min(xs) - radius - ox) / g["along"])
        row0 = oy + g["across"] * math.floor((min(ys) - radius - g["row_gap"] - oy) / g["across"])
        columns = [col0 + g["along"] * j for j in range(int((max(xs) + radius - col0) // g["along"]) + 1)]
        tops = [row0 + g["across"] * k for k in range(int((max(ys) + radius - row0) // g["across"]) + 1)]
        rows, covered, count = [], set(), 0
        for top in tops[:MAX_ROWS]:
            found = _row(ore, other, blocked, reserved, bounds, ox, top, columns, genome, radius)
            if found:
                rows.append({"top_y": top, "drills": found})
                count += len(found)
                for *_, cells in found:
                    covered.update(cells)
        if not rows:
            continue
        score = (len(covered), -count, -(len(ore) - len(covered)))
        if best is None or score > best["score"]:
            best = {"origin": (ox, oy), "rows": rows, "covered": covered, "drills": count, "score": score}
    if best is None:
        return {"ok": False, "reason": "mining_frame_has_no_drill_site", "shifts_scored": len(shifts)}
    return _assemble(snapshot, genome, best, ore, other, blocked, reserved, transform, len(shifts))


def _assemble(snapshot, genome, best, ore, other, blocked, reserved, transform, shifts) -> dict:
    """Entities and records of the chosen lattice (canonical), frame rects and ports on the map."""
    from .planning import overlap

    g = pitches(genome.spacing, genome.drill)
    size, along = g["size"], g["along"]
    edge = max(max(x for x, *_ in row["drills"]) for row in best["rows"])
    port_x = edge + (size + 1) // 2

    def free(t):
        box = [[t[0] - 0.49, t[1] - 0.49], [t[0] + 0.49, t[1] + 0.49]]
        return t not in blocked and t not in other and not any(overlap(box, r) for r in reserved)

    kept = []
    for row in best["rows"]:
        # A collector tile on water, other ore or a reservation cuts off every drill behind it.
        cy = row["top_y"] + g["collector"]
        west = min(x for x, *_ in row["drills"]) - (size - 1) // 2
        cut = [x for x in _span(west, port_x) if not free((x, cy))]
        drills = [d for d in row["drills"] if not cut or d[0] - (size - 1) // 2 > max(cut)]
        if drills:
            kept.append(dict(row, drills=drills))
    if not kept:
        return {"ok": False, "reason": "mining_frame_collectors_blocked", "shifts_scored": shifts}
    best = dict(best, rows=kept, drills=sum(len(r["drills"]) for r in kept))
    best["covered"] = {c for r in kept for *_, cells in r["drills"] for c in cells}
    entities, rows = [], []
    for row in best["rows"]:
        top = row["top_y"]
        cy = top + g["collector"]
        xs = sorted({x for x, *_ in row["drills"]})
        drills = [entity(genome.drill, x, y, d) for x, y, d, _ in row["drills"]]
        lattice = [xs[0] + along * j for j in range(int(round((xs[-1] - xs[0]) / along)) + 1)]
        if genome.spacing == "compact":
            # One pole on the collector per two lattice columns, between their drop tiles.
            poles = [entity(POLE, x + 1, cy, 0) for x in lattice[::2]]
        else:
            # A pole in the gap after every lattice column of each drill line (empty columns too,
            # so the line stays one chain across a gap in the ore).
            poles = []
            for y in sorted({y for _, y, _, _ in row["drills"]}):
                line = [x for x, yy, _, _ in row["drills"] if yy == y]
                poles += [entity(POLE, x + along // 2, y, 0) for x in lattice if min(line) <= x <= max(line)]
        poles = [p for p in poles if free((p["position"]["x"], p["position"]["y"]))]
        start = min(xs) - (size - 1) // 2
        lane = []
        pole_x = {p["position"]["x"] for p in poles if p["position"]["y"] == cy}
        x = start
        while x <= port_x:
            if x + 1 in pole_x:
                under = entity(UNDERGROUND, x, cy, 4)
                under["belt_to_ground_type"] = "input"
                out = entity(UNDERGROUND, x + 2, cy, 4)
                out["belt_to_ground_type"] = "output"
                lane += [under, out]
                x += 3
                continue
            lane.append(entity(BELT, x, cy, 4))
            x += 1
        ore_under = Counter()
        for x_, y_, d, cells in row["drills"]:
            ore_under[x_] += len(cells)
        rows.append(
            {
                "top_y": top,
                "collector_y": cy,
                "columns": xs,
                "drills": drills,
                "poles": poles,
                "collector": lane,
                "port": (port_x, cy),
                "pairs": [x_ for x_ in xs if sum(1 for e in drills if e["position"]["x"] == x_) == 2],
                "ore_under": dict(ore_under),
            }
        )
        entities += drills + poles + lane
    frame_tiles = set()
    for e in entities:
        frame_tiles |= _tiles(e["position"]["x"], e["position"]["y"], size if e["name"] == genome.drill else 1)

    def world(x, y):
        mx, my = transform.point(x, y)
        return {"x": mx, "y": my}

    covered = best["covered"]
    return {
        "ok": True,
        "genome": genome,
        "entities": entities,
        "rows": rows,
        "edge_x": edge,
        "origin": world(*best["origin"]),
        "ports": [world(*r["port"]) for r in rows],
        "frame_rects": tile_rects({transform.point(x, y) for x, y in frame_tiles}),
        "frame_tiles": len(frame_tiles),
        "off_ore_tiles": len(frame_tiles - ore.keys()),
        "covered_tiles": len(covered),
        "covered_amount": sum(ore[p] for p in covered),
        "unreachable_tiles": len(ore) - len(covered),
        "drills": best["drills"],
        "shifts_scored": shifts,
        "_check": {"ore": ore, "other": other, "blocked": blocked, "reserved": reserved},
    }


# ----- rates and merge plan ----------------------------------------------------------------
def drill_rate(catalog: Any, snapshot: dict, drill: str) -> float:
    """Ore per minute of one drill (mining speed x 60 / the resource's mining time x yield)."""
    native = snapshot["native"]
    speed = native["mining_speed"] if drill == ELECTRIC else None
    proto = (getattr(catalog, "entities", None) or {}).get(drill) or {}
    if speed is None:
        speed = float(proto.get("mining_speed") or 0.0)
    return speed * 60 / native["mining_time"] * (1 + native.get("force_productivity_bonus", 0))


def lane_capacity(snapshot: dict) -> float:
    return snapshot["native"]["belt_speed"] * 60 * 4 * 60  # items per minute on one belt lane


def row_lanes(row: dict, rate: float, cap: float, *, pairs: int | None = None) -> tuple[float, float]:
    """(left, right) lane loads of a row's collector, each capped at the lane (``pairs`` edge-most columns only)."""
    columns = sorted(row["columns"], reverse=True)[:pairs] if pairs else row["columns"]
    keep = set(columns)
    left = sum(rate for e in row["drills"] if e["position"]["x"] in keep and e["direction"] == 8)
    right = sum(rate for e in row["drills"] if e["position"]["x"] in keep and e["direction"] == 0)
    return min(left, cap), min(right, cap)


def merge_plan(rows: list[dict], rate: float, cap: float, merge: str) -> dict:
    """Collectors -> trunk belts at the edge (module doc). ``belts``: per trunk belt its rows and lanes."""
    loads = [row_lanes(r, rate, cap) for r in rows]
    offered = sum(rate for r in rows for _ in r["drills"])  # before the collector lanes clip
    belts = []
    for index, (left, right) in sorted(enumerate(loads), key=lambda q: -(q[1][0] + q[1][1])):
        if belts and (merge == "single" or belts[-1]["near"] + left + right <= cap + 1e-9):
            b = belts[-1]
            b["rows"].append(index)
            b["near"] += left + right  # a side-load puts both feeder lanes onto the near lane
            continue
        belts.append({"rows": [index], "far": left, "near": right})  # the head row joins head-on
    delivered = sum(min(b["far"], cap) + min(b["near"], cap) for b in belts)
    count = len(belts)
    return {
        "merge": merge,
        "belts": count,
        "balancer": "none" if count == 1 else ("2-2 splitter" if count == 2 else f"reserved {count}-{count}"),
        "offered_per_minute": round(offered, 2),
        "delivered_per_minute": round(delivered, 2),
        "clipped_per_minute": round(offered - delivered, 2),
        "lanes": [{"rows": b["rows"], "far": round(b["far"], 2), "near": round(b["near"], 2)} for b in belts],
    }


def start_row(rows: list[dict], pairs: int) -> int | None:
    """The row the staged build starts on: most ore under its ``pairs`` edge-most full pairs, then longest."""
    best = None
    for index, row in enumerate(rows):
        full = sorted(row["pairs"], reverse=True)
        if len(full) < pairs:
            continue
        ore = sum(row["ore_under"].get(x, 0) for x in full[:pairs])
        key = (ore, len(row["columns"]), -index)
        if best is None or key > best[0]:
            best = (key, index)
    return None if best is None else best[1]


def growth_steps(rows: list[dict], first: int, pairs: int) -> list[int]:
    """Drills per stage: the start, then along its row, then the other rows nearest the start first."""
    steps = [2 * pairs]
    row = rows[first]
    rest = len(row["drills"]) - 2 * pairs
    if rest > 0:
        steps.append(rest)
    order = sorted((i for i in range(len(rows)) if i != first), key=lambda i: (abs(i - first), i))
    steps += [len(rows[i]["drills"]) for i in order]
    return steps


def stage_entities(built: dict, first: int, pairs: int) -> list[dict]:
    """The start stage: ``pairs`` edge-most full pair columns of the start row, their poles and collector."""
    row = built["rows"][first]
    keep = set(sorted(row["pairs"], reverse=True)[:pairs])
    west = min(keep) - (pitches(built["genome"].spacing, built["genome"].drill)["size"] - 1) // 2
    drills = [e for e in row["drills"] if e["position"]["x"] in keep]
    along = pitches(built["genome"].spacing, built["genome"].drill)["along"]
    # The poles beside the kept columns (each column's own gap pole, or the collector poles reaching them).
    poles = [p for p in row["poles"] if min(keep) - along < p["position"]["x"] <= max(keep) + along / 2]
    lane = [e for e in row["collector"] if e["position"]["x"] >= west]
    return drills + poles + lane


# ----- audit -------------------------------------------------------------------------------
def audit(snapshot: dict, built: dict, rate: float) -> list[str]:
    """Hard findings of a frame (module doc); empty when it passes."""
    from .planning import overlap

    genome = built["genome"]
    size = pitches(genome.spacing, genome.drill)["size"]
    radius = DRILLS[genome.drill]["radius"] if genome.drill != ELECTRIC else snapshot["native"]["mining_radius"]
    check = built["_check"]
    findings = []
    seen = {}
    for e in built["entities"]:
        for t in _tiles(e["position"]["x"], e["position"]["y"], size if e["name"] == genome.drill else 1):
            if t in seen:
                findings.append("entity_overlap")
                break
            seen[t] = e["name"]
            if t in check["blocked"] or t in check["other"]:
                findings.append("frame_on_forbidden_or_foreign_tile")
                break
            box = [[t[0] - 0.49, t[1] - 0.49], [t[0] + 0.49, t[1] + 0.49]]
            if any(overlap(box, r) for r in check["reserved"]):
                findings.append("frame_on_reservation")
                break
    span = math.floor(radius)
    for row in built["rows"]:
        cy = row["collector_y"]
        lane = {(e["position"]["x"], e["position"]["y"]): e for e in row["collector"]}
        for d in row["drills"]:
            x, y = d["position"]["x"], d["position"]["y"]
            area = [(x + i, y + j) for i in range(-span, span + 1) for j in range(-span, span + 1)]
            if any(p in check["other"] for p in area) or sum(p in check["ore"] for p in area) < GRID_MIN_ORE_TILES:
                findings.append("drill_area_not_clean_ore")
            drop = lane.get((x, cy))
            if drop is None or drop["name"] not in (BELT, UNDERGROUND):
                findings.append("drill_drop_not_on_collector")
            if not any(
                abs(p["position"]["x"] - x) <= POLE_SUPPLY + size / 2 - 0.01
                and abs(p["position"]["y"] - y) <= POLE_SUPPLY + size / 2 - 0.01
                for p in row["poles"]
            ):
                findings.append("drill_unpowered")
        poles = sorted(row["poles"], key=lambda p: (p["position"]["x"], p["position"]["y"]))
        if not _chained(poles):
            findings.append("row_poles_not_one_chain")
        xs = sorted(lane)
        expected = xs[0][0]
        for x, _ in xs:
            if x - expected > UNDERGROUND_GAP + 1:
                findings.append("collector_gap")
                break
            expected = x + 1
        if xs[-1][0] != row["port"][0]:
            findings.append("collector_not_at_port")
    if genome.merge == "saturate":
        cap = lane_capacity(snapshot)
        plan = merge_plan(built["rows"], rate, cap, "saturate")
        if any(b["near"] > cap + 1e-6 or b["far"] > cap + 1e-6 for b in plan["lanes"]):
            findings.append("trunk_lane_capacity_exceeded")  # bandwidth: a side-load past the near lane
    return sorted(set(findings))


def _chained(poles: list[dict]) -> bool:
    if len(poles) <= 1:
        return True
    left = {0}
    todo = [0]
    while todo:
        i = todo.pop()
        for j, p in enumerate(poles):
            if (
                j not in left
                and math.dist(
                    (p["position"]["x"], p["position"]["y"]), (poles[i]["position"]["x"], poles[i]["position"]["y"])
                )
                <= POLE_WIRE
            ):
                left.add(j)
                todo.append(j)
    return len(left) == len(poles)


# ----- descriptor -----------------------------------------------------------------------------
def _raw(catalog: Any, counts: Counter) -> dict:
    from .layout_variants import raw_cost

    try:
        return raw_cost(catalog, dict(counts))
    except (AttributeError, KeyError, TypeError, ValueError):  # a catalog without recipes: count items
        return {k: float(v) for k, v in counts.items()}


def _energy(catalog: Any, entities: list[dict]) -> dict:
    from .layout_variants import Genome, energy_metrics

    return energy_metrics(catalog, Genome("row", "-"), {"entities": entities})


def describe(catalog: Any, snapshot: dict, built: dict, genome: MineGenome) -> dict | None:
    """The comparable descriptor of one genome on one frame (module doc); None when the start does not fit."""
    rate = drill_rate(catalog, snapshot, genome.drill)
    cap = lane_capacity(snapshot)
    first = start_row(built["rows"], genome.start_pairs)
    if first is None:
        return None
    now = stage_entities(built, first, genome.start_pairs)
    full = built["entities"]
    plan = merge_plan(built["rows"], rate, cap, genome.merge)
    trunk = [entity(SPLITTER, 0, 0, 0)] if plan["belts"] == 2 else []
    drills_now = [e for e in now if e["name"] == genome.drill]
    left = min(cap, rate * sum(1 for e in drills_now if e["direction"] == 8))
    right = min(cap, rate * sum(1 for e in drills_now if e["direction"] == 0))
    names_now, names_full = Counter(e["name"] for e in now), Counter(e["name"] for e in full + trunk)
    # Every trunk belt beyond the first runs along the output edge past every port (reserved, paid later).
    ports_along = [p for r in built["rows"] for p in [r["port"][1]]]
    names_full[BELT] += (plan["belts"] - 1) * int(max(ports_along) - min(ports_along) + 1)
    raw_now, raw_full = _raw(catalog, names_now), _raw(catalog, names_full)
    energy_now, energy_full = _energy(catalog, now), _energy(catalog, full)
    xs = [x for r in built["frame_rects"] for x in (r[0][0], r[1][0])]
    ys = [y for r in built["frame_rects"] for y in (r[0][1], r[1][1])]
    steps = growth_steps(built["rows"], first, genome.start_pairs)
    pairs_full = sum(len(r["columns"]) for r in built["rows"])

    def count(names):
        return {
            "drills": names[genome.drill],
            "belts": names[BELT],
            "undergrounds": names[UNDERGROUND],
            "splitters": names[SPLITTER],
            "poles": names[POLE],
            "entities": sum(names.values()),
        }

    return {
        "id": genome.key(),
        "form": "mine",
        "spacing": genome.spacing,
        "edge": EDGES[genome.edge],
        "flow": genome.edge,
        "merge": genome.merge,
        "drill": genome.drill,
        "start_pairs": genome.start_pairs,
        "buildable": genome.buildable,
        "footprint": [round(max(xs) - min(xs)), round(max(ys) - min(ys))],
        # The area term: frame tiles off the ore (ore is no other block's ground anyway);
        # ``frame_tiles`` is the whole reservation.
        "area": built["off_ore_tiles"],
        "frame_tiles": built["frame_tiles"],
        "frame_rects": built["frame_rects"],
        "ports": built["ports"],
        "origin": built["origin"],
        "rows": len(built["rows"]),
        "start_row": first,
        "ore_covered_tiles": built["covered_tiles"],
        "ore_covered_amount": built["covered_amount"],
        "ore_unreachable_tiles": built["unreachable_tiles"],
        "drills_full": built["drills"],
        "counts": count(names_now),
        "counts_full": count(names_full),
        "raw_cost": raw_full,
        # The plates term: what is paid now (the start stage); the frame grows only on demand.
        "raw_total": round(sum(raw_now.values()), 1),
        "raw_start": round(sum(raw_now.values()), 1),
        "raw_full": round(sum(raw_full.values()), 1),
        "rate_now": round(left + right, 2),
        "rate_full": plan["delivered_per_minute"],
        "rate_per_drill": round(rate, 3),
        "power_kw": energy_now["power_kw"],
        "power_kw_full": energy_full["power_kw"],
        "pollution_per_min": energy_now["pollution_per_min"],
        "pollution_per_min_full": energy_full["pollution_per_min"],
        "trunk": {k: plan[k] for k in ("belts", "balancer", "clipped_per_minute", "lanes")},
        "neighbours_respected": True,  # every frame tile was checked against the reservations
        "growth": {
            "kind": "in_place",
            "slots": max(0, pairs_full - genome.start_pairs),
            "max_rate_in_place": plan["delivered_per_minute"],
            "limit": "trunk_single_belt" if plan["clipped_per_minute"] > 0 else "patch",
            "beyond": "next_patch",
        },
        "growth_steps": steps,
    }


def niche(descriptor: dict) -> str:
    """Archive bin: edge / spacing / merge / start / drill (every genome its own niche)."""
    tier = "electric" if descriptor["drill"] == ELECTRIC else "big"
    return "/".join(
        (descriptor["edge"], descriptor["spacing"], descriptor["merge"], f"s{descriptor['start_pairs']}", tier)
    )


def library_niche(genome: MineGenome) -> str:
    """Library bin: spacing / merge / drill (edge and start are chosen on the patch)."""
    return "/".join((genome.spacing, genome.merge, "electric" if genome.drill == ELECTRIC else "big"))


def compact(descriptor: dict) -> dict:
    keep = (
        "id",
        "form",
        "spacing",
        "edge",
        "merge",
        "drill",
        "start_pairs",
        "buildable",
        "footprint",
        "rows",
        "ore_covered_tiles",
        "ore_unreachable_tiles",
        "drills_full",
        "rate_now",
        "rate_full",
        "raw_total",
        "raw_start",
        "power_kw",
        "power_kw_full",
        "pollution_per_min",
        "pollution_per_min_full",
        "growth_steps",
    )
    out = {k: descriptor[k] for k in keep}
    out["counts"] = {k: descriptor["counts"][k] for k in ("drills", "belts", "undergrounds", "poles", "entities")}
    out["counts_full"] = {k: descriptor["counts_full"][k] for k in ("drills", "belts", "undergrounds", "poles")}
    out["trunk"] = {k: descriptor["trunk"][k] for k in ("belts", "balancer", "clipped_per_minute")}
    out["growth"] = {k: descriptor["growth"][k] for k in ("kind", "slots", "max_rate_in_place", "limit")}
    return out


# ----- candidates ------------------------------------------------------------------------------
def library_path(directory: Path | None = None) -> Path:
    from .layout_variants import library_path as base

    return base(LIBRARY_ITEM, directory)


def load_library(directory: Path | None = None) -> dict | None:
    try:
        data = json.loads(library_path(directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("entries"), dict) else None


def candidates(
    catalog: Any,
    snapshot: dict,
    reservations=(),
    *,
    forbidden=(),
    edges=None,
    starts=(1, 2),
    buildable_only=False,
    tiers=(ELECTRIC,),
    directory: Path | None = None,
    library: bool = True,
) -> dict:
    """``{"candidates", "library", "skipped", "generated"}`` on one patch, like ``layout_variants.candidates``.

    Library genomes (their spacing / merge / drill) are regenerated for every edge and start
    on this patch and re-audited first, then the rest of the genome space; a failing genome
    is skipped with its reason. ``buildable_only`` keeps what the bank builder lays out today.
    """
    pool = genomes(catalog, edges=edges, starts=starts, tiers=tiers)
    skipped, source = [], None
    data = load_library(directory) if library else None
    preferred = []
    if data is not None:
        source = library_path(directory).name
        if data.get("generator") != GENERATOR_VERSION:
            skipped.append({"library": source, "why": f"generator {data.get('generator')} != {GENERATOR_VERSION}"})
        else:
            for key, entry in sorted(data["entries"].items()):
                try:
                    preferred.append(MineGenome.from_dict(entry["genome"]))
                except (KeyError, TypeError, ValueError):
                    skipped.append({"niche": key, "why": "unreadable genome"})
    libs = {library_niche(g) for g in preferred}
    if buildable_only:
        pool = [g for g in pool if g.buildable]
    frames: dict = {}
    found = []
    for genome in pool:
        key = (genome.spacing, genome.edge, genome.drill)
        if key not in frames:
            frames[key] = frame(snapshot, genome, reservations, forbidden=forbidden)
        built = frames[key]
        if not built.get("ok"):
            skipped.append({"id": genome.key(), "why": built.get("reason")})
            continue
        rate = drill_rate(catalog, snapshot, genome.drill)
        if rate <= 0:
            skipped.append({"id": genome.key(), "why": "drill_rate_unknown"})
            continue
        findings = audit(snapshot, dict(built, genome=genome), rate)
        if findings:
            skipped.append({"id": genome.key(), "why": "audit: " + ",".join(findings[:3])})
            continue
        descriptor = describe(catalog, snapshot, dict(built, genome=genome), genome)
        if descriptor is None:
            skipped.append({"id": genome.key(), "why": "no_row_with_the_start_pairs"})
            continue
        found.append(
            {
                "genome": genome,
                "line": dict(built, genome=genome),
                "descriptor": descriptor,
                "compact": compact(descriptor),
                "niche": niche(descriptor),
                "source": "library" if library_niche(genome) in libs else "generated",
                "evidence": {},
            }
        )
    return {"candidates": found, "library": source, "skipped": skipped, "generated": len(pool)}


def build_library(catalog: Any, snapshot: dict, *, previous: dict | None = None) -> dict:
    """``data/layouts/mining-patch.json`` (``tools/layouts.py build mining-patch``) from a reference patch."""
    old = {
        json.dumps(e.get("genome"), sort_keys=True): e.get("evidence") or {}
        for e in ((previous or {}).get("entries") or {}).values()
    }
    found = candidates(catalog, snapshot, edges=("west_to_east",), starts=(1,), library=False)["candidates"]
    entries = {}
    for c in found:
        genome = c["genome"].to_dict()
        entries[library_niche(c["genome"])] = {
            "genome": genome,
            "descriptor": c["compact"],
            "scores": {"archive_cost": [c["descriptor"]["raw_total"], c["descriptor"]["counts_full"]["entities"]]},
            "evidence": old.get(json.dumps(genome, sort_keys=True), {}),
        }
    return {
        "format": "factorio-agent-layout-library",
        "generator": GENERATOR_VERSION,
        "catalog_fingerprint": catalog.fingerprint,
        "item": LIBRARY_ITEM,
        "request": {"patch": "reference rectangle 40 x 24 (tools/layouts.py)", "edge": "E", "start_pairs": 1},
        "entries": entries,
    }


# ----- selection ----------------------------------------------------------------------------------
def port_cost(descriptor: dict, consumer) -> float:
    """Tiles from the frame's ports to a consumer point (map ``(x, y)``), plus a detour around the frame."""
    if consumer is None:
        return 0.0
    ports = descriptor["ports"]
    mx = sum(p["x"] for p in ports) / len(ports)
    my = sum(p["y"] for p in ports) / len(ports)
    cost = abs(consumer[0] - mx) + abs(consumer[1] - my)
    from .mining_layout_policy import FLOWS, VECTORS

    ux, uy = VECTORS[FLOWS[descriptor["flow"]]]
    if (consumer[0] - mx) * ux + (consumer[1] - my) * uy < 0:
        w, h = descriptor["footprint"]
        cost += w if ux else h  # the trunk leaves away from the consumer and has to go around the frame
    return round(cost, 2)


def demand_for(rate_now: float, *, horizon: float | None = None, tick=None):
    """``layout_selection.Demand`` of a mine: the consumer's rate now and over the horizon (build-ahead)."""
    from . import layout_selection as selection

    base = selection.demand_for(rate_now, tick=tick)
    return selection.Demand(base.now, max(base.horizon, float(horizon or 0)), base.plates_scarce)


def rank(found: list[dict], demand, consumer=None) -> list[dict]:
    """Feasible options best first (``layout_selection.score`` + :func:`port_cost`)."""
    from . import layout_selection as selection

    options = [
        selection.option(c, demand, port_cost(c["descriptor"], consumer), buildable=c["genome"].buildable)
        for c in found
    ]
    return selection.ranked(options)


def choose_bank(catalog, snapshot, reservations, *, flow, pairs, root=None, forbidden=(), tick=None, horizon=None):
    """The bank's ``layout`` record and its start pairs (``prepare_new_bank``), or None without a frame.

    Runtime options are the buildable genomes on the planned edge with ``pairs`` or one more
    start pair (build-ahead); the score picks ``pairs`` unless exploration (``layout_selection``
    settings of ``root``) takes the other option within the margin.
    """
    from . import layout_selection as selection

    starts = tuple(sorted({pairs, min(MAX_START_PAIRS, pairs + 1)}))
    found = candidates(
        catalog, snapshot, reservations, forbidden=forbidden, edges=(flow,), starts=starts, buildable_only=True
    )
    if not found["candidates"]:
        return None
    rate = found["candidates"][0]["descriptor"]["rate_per_drill"] * 2 * pairs
    demand = demand_for(rate, horizon=horizon, tick=tick)
    options = rank(found["candidates"], demand)
    config = selection.settings(root)
    seed = selection.seed_for(snapshot.get("world_id"), "mining", snapshot.get("resource"))
    index, size = selection.choose([o["score"] for o in options], config, seed)
    chosen = options[index]
    record = selection.record(
        options,
        chosen,
        config=config,
        pool_size=size,
        tick=tick,
        demand={"now": demand.now, "horizon": demand.horizon},
        buildable_only=True,
        skipped=found["skipped"][:6],
    )
    return {"record": record, "start_pairs": chosen["candidate"]["genome"].start_pairs}


def contexts():
    """Typical mine contexts for ``tools/layouts.py``: consumer direction, demand, patch shape."""
    return (
        ("consumer E, 60/min now", "E", 60.0, None),
        ("consumer S, 60/min now, 600/min horizon", "S", 60.0, 600.0),
        ("consumer W (behind the patch), 120/min", "W", 120.0, None),
    )


def consumer_point(snapshot: dict, side: str, distance: float = 48.0):
    """A consumer ``distance`` tiles beyond the patch on ``side`` (map frame)."""
    tiles = [t["position"] for t in snapshot["tiles"] if t["name"] == snapshot["resource"]]
    x0, x1 = min(p["x"] for p in tiles), max(p["x"] for p in tiles)
    y0, y1 = min(p["y"] for p in tiles), max(p["y"] for p in tiles)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return {
        "E": (x1 + distance, cy),
        "W": (x0 - distance, cy),
        "S": (cx, y1 + distance),
        "N": (cx, y0 - distance),
    }[side]


def reference_snapshot(width: int = 40, height: int = 24, *, amount: int = 600, ragged: bool = False) -> dict:
    """A synthetic patch survey (tools, tests, the library build): a rectangle, optionally ragged edges."""
    tiles = []
    for x in range(width):
        for y in range(height):
            if ragged and (x + 2 * y) % 11 == 0 and (x < 3 or x > width - 4 or y < 2 or y > height - 3):
                continue
            if ragged and x > width - 6 and y > height // 2:
                continue
            tiles.append({"name": "iron-ore", "position": {"x": x + 0.5, "y": y + 0.5}, "amount": amount})
    return {
        "ok": True,
        "tick": 0,
        "world_id": "reference",
        "catalog_fingerprint": "reference",
        "resource": "iron-ore",
        "area": [[-48, -48], [width + 48, height + 48]],
        "tiles": tiles,
        "native": {
            "mining_speed": 0.5,
            "mining_radius": 2.49,
            "mining_time": 1,
            "belt_speed": 0.03125,
            "force_productivity_bonus": 0.0,
        },
    }
