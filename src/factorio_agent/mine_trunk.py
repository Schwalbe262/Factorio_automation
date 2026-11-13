"""One mining block per patch, one output trunk, consumers on splitter taps.

The rule (docs/v2/LAYOUT_PRINCIPLES.md "Mine output trunk"):

1. A resource patch has one mining block: the paired drill bank that
   ``mining_outpost`` planned first (``mining_layout_policy.prepare_new_bank``),
   grown in place along its reserved strip (``prepare_extension``). A later
   consumer of that resource does not open a second drill group with its own
   outgoing belt on the patch (:func:`patch_bank`). Burner starter cells with
   chests are exempt (they are retired by ``starter_retirement``).
2. The patch's output is a trunk of ``N = ceil(total planned demand / belt
   rate)`` parallel belts leaving the bank's output port in one direction (the
   port's facing, chosen by the site fit), with room reserved beside it for
   the next lane (:func:`trunk_bus`, :func:`lane_count`). Two or more lanes are
   evened at the patch edge by an input-balanced merge (:func:`balancer_plan`).
3. Consumers take from the trunk with priority splitter taps, reusing the bus
   tap geometry (``planned_tap.layout`` on a :class:`planned_tap.BusView` of
   the trunk). ``serve`` decides the native output priority: ``first`` gives
   the branch priority (power's coal), ``after`` lets the downstream consumer
   be served first and the branch takes the rest.
4. Every tap records its declared demand in ``mine-trunks.json``; the observe
   receipt (:func:`trunk_receipt`) compares the patch's total demand with the
   bank's nominal supply and :func:`mine_trunk_nodes` proposes a
   ``mining_outpost`` target that grows the same bank in place.

Existing maps: a supply prepared before this rule keeps its plan; only new
demand goes through the trunk. The trunk belt a tap replaces is first removed
from its owner's saved plan and board copy (:func:`release_trunk_belts`), then
mined and replaced by the splitter (:func:`cutover`).

Pure functions except the ledger/republish helpers, which read and write the
runtime directory, and :func:`cutover`, which returns one paid action.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import math
from pathlib import Path

from .board import entity
from .core.deterministic_builder import _plan
from .core.deterministic_state import _atomic_json
from .planned_bus import branch_priorities, configure_action, splitter_side
from .planned_tap import FIRST_ROW, WINDOW_ROWS, BusView, candidates
from .site.busframe import DIRECTIONS, Bus, right_of
from .site.planmodel import Lane
from .transport import xy

STATE_FILE = "mine-trunks.json"
# A bank belongs to the patch whose seed (``obs["resources"][resource]``) lies within
# this many tiles of its mining area centre (the bank survey spans 48 tiles each way).
PATCH_REACH = 64
# Items per minute of one belt, both lanes (catalog: belt speed x 8 items/tile x 60 x 60).
BELT_RATE_PER_MINUTE = {
    "transport-belt": 900.0,
    "fast-transport-belt": 1800.0,
    "express-transport-belt": 2700.0,
    "turbo-transport-belt": 3600.0,
}
RESERVED_LANES = 1  # free lanes kept beside the built trunk for its next lane
MAX_TRACE = 128  # trunk tiles followed from the port
ATTEMPTS = 3  # tap rows tried per side (each costs a native preflight)
EXTENDED_ATTEMPTS = 6  # further rows per side and run before a consumer may open its own drill group
DEFAULT_SPAN = 4  # underground span when the game is not asked (a tap needs none on a mine trunk)
FACING_LETTER = {0: "N", 4: "E", 8: "S", 12: "W"}
SERVES = ("first", "after")
NODE_PREFIX = "auto_mine_trunk_"
LANE_CAPABILITY = "mine_trunk_lane"
NODE_PRIORITY = -53  # right after critical plate growth (throughput_growth.CRITICAL_PRIORITY -52)
OPEN_NODE_STATUSES = frozenset({"pending", "ready", "running", "waiting"})


# ----- bandwidth --------------------------------------------------------------------
def belt_rate(belt: str = "transport-belt") -> float:
    return BELT_RATE_PER_MINUTE[belt]


def lane_count(demand_per_minute: float, rate_per_belt: float) -> int:
    """Parallel belts the trunk needs: ``ceil(demand / belt rate)``, at least one."""
    if rate_per_belt <= 0:
        raise ValueError("belt rate must be positive")
    return max(1, math.ceil(max(0.0, float(demand_per_minute)) / rate_per_belt - 1e-9))


def lane_saturation(drills: int, rate_per_drill: float, rate_per_belt: float) -> float:
    """Drills on one collector x drill rate / belt rate; above 1.0 the belt cannot carry it."""
    return drills * rate_per_drill / rate_per_belt


def balancer_plan(lanes: int) -> dict | None:
    """The input-balanced merge at the patch edge for ``lanes`` parallel belts (None for one).

    A 2-2 balancer is one splitter across both lanes (implemented). For more
    lanes the space is reserved and the shape recorded; the general N-to-M
    generator is a later task (docs/v2/BALANCERS.md).
    """
    if lanes < 2:
        return None
    return {
        "inputs": lanes,
        "outputs": lanes,
        "kind": "splitter_2_2" if lanes == 2 else "reserved_n_n",
        "implemented": lanes == 2,
    }


# ----- the patch's one bank -----------------------------------------------------------
def _area_centre(area) -> tuple[float, float]:
    return ((area[0][0] + area[1][0]) / 2, (area[0][1] + area[1][1]) / 2)


def bank_area(bank: dict):
    return bank.get("mining_reserved_area") or bank["plan"].get("mining_reserved_area") or bank.get("reserved_area")


def on_patch(bank: dict, resource: str, seed) -> bool:
    """``bank`` mines ``resource`` on the patch around ``seed`` (any patch when the seed is unknown)."""
    if bank.get("plan", {}).get("resource") != resource or not bank["plan"].get("ports"):
        return False
    area = bank_area(bank)
    if not isinstance(seed, dict) or not area:
        return True
    x, y = _area_centre(area)
    return abs(x - seed["x"]) <= PATCH_REACH and abs(y - seed["y"]) <= PATCH_REACH


def patch_seed(obs: dict, resource: str):
    known = (obs.get("resources") or {}).get(resource) or {}
    position = known.get("position") if isinstance(known, dict) else None
    if isinstance(position, dict) and all(isinstance(position.get(k), (int, float)) for k in ("x", "y")):
        return position
    return None


def patch_bank(banks, resource: str, seed=None, *, exclude_supply=None) -> dict | None:
    """The patch's mining block: its oldest proven bank, a non-material one first.

    ``exclude_supply`` (a ``material_supply_id``) skips the caller's own bank.
    None when the patch has no proven bank yet (then the caller plans the first).
    """
    rows = [
        (bool(b.get("material_supply_id")), index, b)
        for index, b in enumerate(banks or [])
        if b.get("ready")
        and on_patch(b, resource, seed)
        and not (exclude_supply and b.get("material_supply_id") == exclude_supply)
    ]
    return min(rows, key=lambda row: row[:2])[2] if rows else None


def trunk_key(bank: dict) -> str:
    port = bank["plan"]["ports"][0]
    return "%s@%s,%s" % (bank["plan"]["resource"], port["position"]["x"], port["position"]["y"])


def find_bank(banks, key: str) -> dict | None:
    return next((b for b in banks or [] if b.get("plan", {}).get("ports") and trunk_key(b) == key), None)


# ----- the trunk as a bus view ----------------------------------------------------------
def _tile(position) -> tuple[int, int]:
    return (math.floor(position["x"]), math.floor(position["y"]))


def _surface(obs) -> dict:
    """Tile -> observed belt or splitter (both tiles of a splitter)."""
    tiles = {}
    for e in obs.get("entities", []):
        if e.get("name") == "transport-belt":
            tiles[_tile(e["position"])] = e
        elif e.get("name") == "splitter":
            x, y = e["position"]["x"], e["position"]["y"]
            if e.get("direction", 0) in (0, 8):
                spots = ((x - 0.5, y), (x + 0.5, y))
            else:
                spots = ((x, y - 0.5), (x, y + 0.5))
            for sx, sy in spots:
                tiles[(math.floor(sx), math.floor(sy))] = e
    return tiles


def trunk_segments(obs, port: dict, *, limit: int = MAX_TRACE) -> list[tuple[dict, list[dict]]]:
    """The observed trunk from the output port as straight runs ``[(head, run)]``, following its turns.

    ``head`` is a port-like ``{"position", "facing"}`` on the run's first tile;
    ``run`` its belts and tap splitters in flow order (a splitter of the run's
    facing passes, so a built tap never ends the trunk). A turn is a belt on the
    next tile facing sideways that nothing feeds from behind (a curve); a belt
    fed from behind is another line the trunk side-loads into, and the trace
    stops there, as it does at a gap, a reversed belt or ``limit`` tiles.
    """
    facing = port.get("facing")
    if facing not in FACING_LETTER:
        return []
    tiles = _surface(obs)
    x, y = _tile(port["position"])
    segments, run, head = [], [], {"position": dict(port["position"]), "facing": facing}
    for _ in range(limit):
        here = tiles.get((x, y))
        if here is None:
            break
        direction = here.get("direction", 0)
        if direction != facing:
            bx, by = DIRECTIONS[FACING_LETTER.get(direction, "N")]
            behind = tiles.get((x - bx, y - by))
            turn = (
                here.get("name") == "transport-belt"
                and direction in FACING_LETTER
                and direction != (facing + 8) % 16
                and not (behind is not None and behind.get("direction", 0) == direction)
            )
            if not turn or not run:
                break
            segments.append((head, run))
            facing, run = direction, []
            head = {"position": {"x": x + 0.5, "y": y + 0.5}, "facing": facing}
        run.append(here)
        dx, dy = DIRECTIONS[FACING_LETTER[facing]]
        x, y = x + dx, y + dy
    if run:
        segments.append((head, run))
    return segments


def trace_trunk(obs, port: dict) -> list[dict]:
    """Observed straight run from the output port along its facing: belts (and tap splitters) in flow order."""
    segments = trunk_segments(obs, port)
    return segments[0][1] if segments else []


def trunk_bus(
    bank: dict, obs, side: str, *, lanes_built: int = 1, reserved: int = RESERVED_LANES, stations=(), segment=None
):
    """A ``planned_tap`` bus document for the bank's trunk with taps towards ``side``.

    The built lanes and the reserved next lane lie on the side away from the
    taps; the tap side's band edge is the tile beside the outer trunk lane, so
    a branch leaves the trunk in two tiles and never crosses a trunk lane.
    ``segment`` (``(head, run)`` of :func:`trunk_segments`) frames a later
    straight run of the trunk instead of the first.
    """
    if side not in ("left", "right"):
        raise ValueError("side must be left or right")
    port = bank["plan"]["ports"][0]
    if segment is None:
        run = trace_trunk(obs, port)
    else:
        port, run = segment
    letter = FACING_LETTER.get(port.get("facing"))
    if not run or letter is None:
        return {"ok": False, "reason": "mine_trunk_not_observed"}
    total = lanes_built + reserved
    half = max(1, math.ceil(total / 2))
    first = half - 1 if side == "right" else -half
    step = -1 if side == "right" else 1
    dx, dy = DIRECTIONS[letter]
    rx, ry = right_of(letter)
    px, py = port["position"]["x"], port["position"]["y"]
    # Tile (along 0, lateral ``first``) is the port tile.
    origin = (round(px - 0.5 * dx - (first + 0.5) * rx), round(py - 0.5 * dy - (first + 0.5) * ry))
    item = bank["plan"]["resource"]
    lanes = [Lane(i, 0, first + step * i, item if i < lanes_built else "") for i in range(total)]
    frame = Bus(origin, letter, 2 * half, len(run), tuple(lanes))
    # Trunk belts as observed; a built tap's splitter is no plain belt, so no later tap
    # window may replace its row (``planned_tap.layout``); windows keep their gap by ``along``.
    entities = [e for e in run if e["name"] == "transport-belt"]
    return {
        "ok": True,
        "frame": frame.to_dict(),
        "trunks": [{"item": item, "lane": 0, "length": len(run)}],
        "entities": entities,
        "stations": [{k: v for k, v in s.items() if k != "entities"} for s in stations],
    }


# ----- taps -------------------------------------------------------------------------
def tap_priorities(tap: dict) -> dict:
    """Native priorities: ``first`` -> branch output first; ``after`` -> trunk continuation first.

    A trunk lane's 2-2 splitter (:func:`lane_plan`) has none on either side: it balances.
    """
    if tap.get("kind") == "lane":
        return {"splitter_input_priority": "none", "splitter_output_priority": "none"}
    wanted = branch_priorities(tap["splitter"], tap["port"]["position"])
    if tap.get("serve", "after") == "first":
        return wanted
    branch = wanted["splitter_output_priority"]
    return dict(wanted, splitter_output_priority="left" if branch == "right" else "right")


def plan_tap(bank, obs, *, owner, name, demand, serve="after", sides=("right", "left"), stations=(), accept=None):
    """Feasible splitter taps on the bank's trunk, nearest the patch first, per side.

    Search order (the trunk is extended before anyone opens a second drill group):
    the first ``ATTEMPTS`` fitting rows per side on every straight run of the
    observed trunk (the first run from the port, then the runs after each turn,
    :func:`trunk_segments`); when none is accepted, up to ``EXTENDED_ATTEMPTS``
    further rows per side and run. Each tap records its ``segment``.

    Returns ``{"ok": True, "taps": [...]}`` (candidates in preference order) or a failure
    with ``rows_rejected`` and ``segments`` (runs searched).
    """
    if serve not in SERVES:
        raise ValueError("serve must be first or after")
    port = bank["plan"]["ports"][0]
    segments = trunk_segments(obs, port)
    if not segments or FACING_LETTER.get(port.get("facing")) is None:
        return {"ok": False, "reason": "mine_trunk_not_observed"}
    found, rejected, tried = [], {}, set()
    live = {(e["name"], *_tile(e["position"])): e for e in obs.get("entities", [])}
    item = bank["plan"]["resource"]
    # A built second lane lies on the far side of the first run: taps there come from the near side.
    lane_sides = {s.get("tap_side") for s in stations if s.get("kind") == "lane"}
    for limit in (ATTEMPTS, ATTEMPTS + EXTENDED_ATTEMPTS):
        for index, segment in enumerate(segments):
            own = [s for s in stations if s.get("segment", 0) == index]
            for side in sides:
                if index == 0 and lane_sides and side not in lane_sides:
                    continue
                bus = trunk_bus(bank, obs, side, stations=own, segment=segment)
                if not bus.get("ok"):
                    continue
                view = BusView(bus)
                rows, why = candidates(
                    view, item, side, FIRST_ROW, FIRST_ROW, view.bus.length, span=DEFAULT_SPAN, limit=limit
                )
                for reason, count in why.items():
                    rejected[reason] = rejected.get(reason, 0) + count
                for row in rows:
                    if (index, side, row["along"]) in tried:
                        continue
                    tried.add((index, side, row["along"]))
                    units = [live.get(("transport-belt", *_tile(p)), {}).get("unit_number") for p in row["remove"]]
                    if not all(type(u) is int for u in units):
                        rejected["mine_trunk_belt_not_observed"] = rejected.get("mine_trunk_belt_not_observed", 0) + 1
                        continue
                    tap = dict(
                        row,
                        name=name,
                        owner=owner,
                        serve=serve,
                        demand_per_minute=round(float(demand), 3),
                        primary_demand=round(float(bank.get("nominal_rate_per_minute", 0)), 3),
                        remove_units=units,
                        trunk=trunk_key(bank),
                        segment=index,
                        built=False,
                        frame=bus["frame"],
                    )
                    tap["splitter"].update(tap_priorities(tap))  # the same dict as in ``entities``
                    if accept is None or accept(tap):
                        found.append(tap)
        if found:
            break
    if not found:
        return {
            "ok": False,
            "reason": "mine_trunk_tap_unavailable",
            "rows_rejected": rejected,
            "segments": len(segments),
        }
    return {"ok": True, "taps": found}


def hub(tap: dict, keys: tuple[str, str]) -> dict:
    """A two-output even splitter just past the tap port (``keys``: upstream row, downstream row ports)."""
    frame = Bus.from_dict(tap["frame"])
    s = 1 if tap["side"] == "right" else -1
    a = tap["along"]
    port_lateral = tap["frame"]["width"] // 2 + 1 if s > 0 else -(tap["frame"]["width"] // 2) - 2
    facing = tap["port"]["facing"]

    def world(along, lateral):
        x, y = frame.local_point(along + 0.5, lateral + 0.5)
        return {"x": x, "y": y}

    centre = world(a + 2.5, port_lateral + s)
    splitter = dict(
        entity("splitter", centre["x"], centre["y"], facing),
        _width=2,
        _height=1,
        splitter_input_priority="none",
        splitter_output_priority="none",
    )
    outputs = [world(a + 2, port_lateral + 2 * s), world(a + 3, port_lateral + 2 * s)]
    entities = [splitter] + [entity("transport-belt", p["x"], p["y"], facing) for p in outputs]
    ports = {
        key: {"kind": "item", "item": tap["item"], "position": p, "facing": facing, "direction": "output"}
        for key, p in zip(keys, outputs)
    }
    access = [world(a + 2, port_lateral + 3 * s), world(a + 3, port_lateral + 3 * s)]
    return {"splitter": splitter, "entities": entities, "ports": ports, "output_access": access}


LANE_NAME = "trunk_lane:1"


def lane_plan(bank, obs, *, stations=(), accept=None) -> dict:
    """The second trunk lane beside the built one, with its 2-2 input-balanced splitter at the head.

    Rule (principle 4, docs/v2/BALANCERS.md): when the planned demand needs two
    belts, the second lane runs beside the first on the side away from the taps
    (the lane ``trunk_bus`` reserves), over the first straight run of the trunk.
    At row ``a`` (the first row whose tap window is free) one splitter spans both
    lanes: the first lane's belt there is replaced, both inputs are drained
    evenly and both outputs filled evenly (no priority). The lane's head port
    (row ``a - 1``) is where a second row lane of the patch (the next row pair or
    a connected module, ``MiningOutposts.prepare_connected_bank``) joins; until
    lane two has its own consumers the splitter merges both into lane one,
    lane by lane (merge by saturation). More than two lanes stay reserved and
    recorded (``balancer_plan``). Returns ``{"ok": True, "lane": ...}`` or a failure.
    """
    tapped = {s.get("side") for s in stations if s.get("kind") not in ("lane", "return") and s.get("segment", 0) == 0}
    if len(tapped) > 1:
        return {"ok": False, "reason": "mine_trunk_lane_both_sides_tapped"}
    side = next(iter(tapped)) if tapped else "right"
    bus = trunk_bus(bank, obs, side, stations=[s for s in stations if s.get("segment", 0) == 0])
    if not bus.get("ok"):
        return bus
    view = BusView(bus)
    lanes = {lane.index: lane for lane in view.bus.lanes}
    first, second = lanes[0].offset, lanes[1].offset
    facing = view.facing
    item = bank["plan"]["resource"]
    live = {(e["name"], *_tile(e["position"])): e for e in obs.get("entities", [])}
    taken = set()
    for e in obs.get("entities", []):
        taken.add(_tile(e["position"]))
    length = view.bus.length
    rejected = {}
    for a in range(FIRST_ROW + 1, length - 1):
        if not view.window_free(a):
            rejected["mine_trunk_lane_window_taken"] = rejected.get("mine_trunk_lane_window_taken", 0) + 1
            continue
        lane0 = view.world(a, first)
        belt = live.get(("transport-belt", *_tile(lane0)))
        if belt is None or type(belt.get("unit_number")) is not int or belt.get("direction", 0) != facing:
            rejected["mine_trunk_lane_belt_not_observed"] = rejected.get("mine_trunk_lane_belt_not_observed", 0) + 1
            continue
        tiles = [view.world(r, second) for r in range(a - 1, length)]
        if any(_tile(p) in taken for p in tiles):
            rejected["mine_trunk_lane_tiles_taken"] = rejected.get("mine_trunk_lane_tiles_taken", 0) + 1
            continue
        centre = view.world(a, (first + second) / 2)
        splitter = dict(
            entity("splitter", centre["x"], centre["y"], facing),
            _width=2,
            _height=1,
            splitter_input_priority="none",
            splitter_output_priority="none",
        )
        belts = [entity("transport-belt", p["x"], p["y"], facing) for p in tiles[2:]]
        head = tiles[0]
        lane = {
            "kind": "lane",
            "name": LANE_NAME,
            "lane": 1,
            "tap_side": side,
            "along": a,
            "segment": 0,
            "item": item,
            "splitter": splitter,
            "entities": [splitter] + belts,
            "remove": [lane0],
            "remove_units": [belt["unit_number"]],
            "port": {"kind": "item", "item": item, "position": head, "facing": facing, "direction": "input"},
            "trunk": trunk_key(bank),
            "balancer": balancer_plan(2),
            "built": False,
        }
        if accept is None or accept(lane):
            return {"ok": True, "lane": lane}
        rejected["mine_trunk_lane_not_accepted"] = rejected.get("mine_trunk_lane_not_accepted", 0) + 1
    return {"ok": False, "reason": "mine_trunk_lane_unavailable", "rows_rejected": rejected}


def lane_head(root, obs, bank: dict) -> dict | None:
    """The free head port of ``bank``'s built second lane as a connection target, or None.

    ``{"kind": "lane", "destination": port, "target": first lane belt (bound to its unit)}``;
    None when no lane is built or something already stands on the head tile (joined).
    """
    if not bank.get("plan", {}).get("ports"):
        return None
    trunk = ledger(root, obs.get("world_id"))["trunks"].get(trunk_key(bank), {})
    lane = (trunk.get("lanes") or {}).get("1")
    if not lane or not lane.get("built"):
        return None
    head = _tile(lane["port"]["position"])
    if any(_tile(e["position"]) == head for e in obs.get("entities", [])):
        return None
    first = next((e for e in lane["entities"] if e["name"] == "transport-belt"), None)
    live = next(
        (
            e
            for e in obs.get("entities", [])
            if first and e.get("name") == "transport-belt" and _tile(e["position"]) == _tile(first["position"])
        ),
        None,
    )
    if live is None or type(live.get("unit_number")) is not int:
        return None
    return {
        "kind": "lane",
        "destination": deepcopy(lane["port"]),
        "target": {k: deepcopy(live[k]) for k in ("name", "position", "direction", "unit_number")},
    }


def lanes_built(trunk: dict) -> int:
    """Trunk belts standing: the bank's own plus every built lane of the ledger."""
    return 1 + sum(1 for lane in (trunk.get("lanes") or {}).values() if lane.get("built"))


def record_lane(root, world_id: str, lane: dict, *, block=None) -> dict | None:
    """Record ``lane`` on its trunk (idempotent) and publish its hardware as a board block."""
    root = Path(root)
    state = ledger(root, world_id)
    trunk = state["trunks"].setdefault(lane["trunk"], {"taps": {}})
    lanes = trunk.setdefault("lanes", {})
    if lanes.get(str(lane["lane"])) != lane:
        lanes[str(lane["lane"])] = deepcopy(lane)
        _atomic_json(root / STATE_FILE, state)
    if block is None:
        return None
    board_path = root / "pcb-board.json"
    board = _read(board_path)
    if board.get("world_id") != world_id:
        return {"status": "blocked", "reason": "mine_trunk_board_world_changed"}
    name, value = block
    blocks = board.setdefault("blocks", {})
    if blocks.get(name) != value:
        blocks[name] = value
        _atomic_json(board_path, board)
    return None


def tap_area(entities) -> list:
    """Bounding box of ``entities`` (one-tile entities and 2x1 splitters)."""
    xs, ys = [], []
    for e in entities:
        x, y = e["position"]["x"], e["position"]["y"]
        wide = e["name"] == "splitter"
        horizontal = wide and e.get("direction", 0) in (0, 8)
        hx = 1.0 if horizontal else 0.5
        hy = 1.0 if wide and not horizontal else 0.5
        xs += [x - hx, x + hx]
        ys += [y - hy, y + hy]
    return [[min(xs), min(ys)], [max(xs), max(ys)]]


# ----- ledger and republication ---------------------------------------------------------
def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def ledger(root, world_id: str) -> dict:
    state = _read(Path(root) / STATE_FILE)
    if state.get("world_id") != world_id:
        return {"world_id": world_id, "trunks": {}}
    state.setdefault("trunks", {})
    return state


def record_tap(root, world_id: str, tap: dict, *, block=None) -> dict | None:
    """Record ``tap`` on its trunk (idempotent) and publish its hardware as a board block."""
    root = Path(root)
    state = ledger(root, world_id)
    trunk = state["trunks"].setdefault(tap["trunk"], {"taps": {}})
    keep = {k: v for k, v in tap.items() if k not in ("frame",)}
    if trunk["taps"].get(tap["name"]) != keep:
        trunk["taps"][tap["name"]] = deepcopy(keep)
        _atomic_json(root / STATE_FILE, state)
    if block is None:
        return None
    board_path = root / "pcb-board.json"
    board = _read(board_path)
    if board.get("world_id") != world_id:
        return {"status": "blocked", "reason": "mine_trunk_board_world_changed"}
    name, value = block
    blocks = board.setdefault("blocks", {})
    if blocks.get(name) != value:
        blocks[name] = value
        _atomic_json(board_path, board)
    return None


def record_return(root, world_id: str, feed: dict) -> None:
    """Record a return feed (``trunk_return_feed``) on its trunk, idempotent; one per trunk."""
    root = Path(root)
    state = ledger(root, world_id)
    trunk = state["trunks"].setdefault(feed["trunk"], {"taps": {}})
    if trunk.get("returns", {}).get(feed["name"]) != feed:
        trunk["returns"] = {feed["name"]: deepcopy(feed)}
        _atomic_json(root / STATE_FILE, state)


def stations(root, world_id: str, key: str, *, exclude=None) -> list[dict]:
    """Taps, lanes and the return feed recorded on trunk ``key`` (with their entities), other than ``exclude``."""
    trunk = ledger(root, world_id)["trunks"].get(key, {})
    rows = [dict(t) for name, t in sorted(trunk.get("taps", {}).items()) if name != exclude]
    rows += [dict(r) for name, r in sorted((trunk.get("returns") or {}).items()) if name != exclude]
    return rows + [dict(lane) for _, lane in sorted((trunk.get("lanes") or {}).items()) if lane.get("name") != exclude]


def tap_passthrough(root, world_id: str, bank: dict) -> dict:
    """Tile -> tap splitter for every recorded tap on ``bank``'s trunk (for ``bus_factory.trace_corridor``).

    A tap's trunk belt is released from its owner's plan before it is mined
    (:func:`release_trunk_belts`), so a trace of that plan meets a gap there;
    the splitter is the trunk's hardware on that tile.
    """
    if not bank.get("plan", {}).get("ports"):
        return {}
    trunk = ledger(root, world_id)["trunks"].get(trunk_key(bank), {})
    tiles = {}
    recorded = [*trunk.get("taps", {}).values(), *(trunk.get("lanes") or {}).values()]
    for tap in recorded + list((trunk.get("returns") or {}).values()):
        splitter = tap.get("splitter")
        if not isinstance(splitter, dict):
            continue
        for p in tap.get("remove", []):
            tiles[(p["x"], p["y"])] = {k: splitter[k] for k in ("name", "position", "direction")}
    return tiles


def _drop_belts(entities, positions) -> tuple[list, bool]:
    wanted = {(p["x"], p["y"]) for p in positions}
    kept = [e for e in entities if not (e["name"] == "transport-belt" and xy(e) in wanted)]
    return kept, len(kept) != len(entities)


def release_trunk_belts(root, world_id: str, tap: dict) -> list[str]:
    """Remove the trunk belts ``tap`` replaces from every owner's saved plan and board copy, first.

    Owners: the smelting district plan (its ore/coal routes) and the mining
    banks (collector exits). The board copy of a block is rewritten only where
    it equalled the saved plan. Idempotent; returns the owners changed.
    """
    root = Path(root)
    positions = tap["remove"]
    changed = []
    board_path = root / "pcb-board.json"
    board = _read(board_path)
    blocks = board.get("blocks", {}) if board.get("world_id") == world_id else {}
    board_changed = False
    district_path = root / "smelting-district.json"
    district = _read(district_path)
    if district.get("world_id") == world_id and isinstance(district.get("plan"), dict):
        before = deepcopy(district["plan"])
        kept, dropped = _drop_belts(district["plan"].get("entities", []), positions)
        if dropped:
            district["plan"]["entities"] = kept
            district["plan"].setdefault("released_to_mine_trunk_taps", []).append(tap["name"])
            _atomic_json(district_path, district)
            changed.append("smelting_district")
            if blocks.get("smelting_district") == before:
                blocks["smelting_district"] = deepcopy(district["plan"])
                board_changed = True
    mining_path = root / "mining-outposts.json"
    mining = _read(mining_path)
    if mining.get("world_id") == world_id:
        dirty = False
        for bank in mining.get("banks", []):
            kept, dropped = _drop_belts(bank["plan"].get("entities", []), positions)
            if dropped:
                bank["plan"]["entities"] = kept
                bank["plan"].setdefault("released_to_mine_trunk_taps", []).append(tap["name"])
                dirty = True
                changed.append(trunk_key(bank))
        if dirty:
            _atomic_json(mining_path, mining)
    for name, block in list(blocks.items()):
        if name == "smelting_district" or not isinstance(block, dict) or not block.get("entities"):
            continue
        kept, dropped = _drop_belts(block["entities"], positions)
        if dropped:
            blocks[name] = dict(block, entities=kept)
            board_changed = True
    if board_changed:
        _atomic_json(board_path, board)
    return changed


# ----- cut-over ---------------------------------------------------------------------
def tap_plan(tap: dict, extra=(), *, world_id=None) -> dict:
    return _plan(
        list(tap["entities"]) + list(extra),
        [tap["port"]],
        underground_pairs=list(tap.get("underground_pairs", [])),
        clear_obstacles=[],
        clearance_world_id=world_id,
        layout_policy="pcb-mine-trunk-tap-v1",
    )


def configure(builder, obs, tap: dict, extra=()) -> dict | None:
    rows = [(tap["splitter"], tap_priorities(tap))]
    rows += [(s, {k: s[k] for k in ("splitter_input_priority", "splitter_output_priority")}) for s in extra]
    return configure_action(builder, obs, rows)


def cutover(builder, obs, tap: dict, extra=()) -> dict | None:
    """Build the tap (and ``extra`` hub hardware): stock, mine the one owned trunk belt, build, configure.

    Returns one action or status, or None once everything stands and the
    splitters carry their priorities. The trunk is interrupted only between
    the mine and the splitter build: every item is carried first.
    """
    entities = list(tap["entities"]) + list(extra)
    live = {(e["name"], *_tile(e["position"])): e for e in obs["entities"]}
    present = all(
        (found := live.get((e["name"], *_tile(e["position"])))) is not None
        and found.get("direction", 0) == e.get("direction", 0)
        for e in entities
    )
    splitters = [e for e in extra if e["name"] == "splitter"]
    if tap.get("built") or present:
        tap["built"] = True
        return configure(builder, obs, tap, splitters)
    missing = Counter(e["name"] for e in entities if (e["name"], *_tile(e["position"])) not in live)
    inventory = obs.get("inventory", {})
    for name, count in sorted(missing.items()):
        if inventory.get(name, 0) < count:
            result = builder.bootstrap.ensure_item(obs, name, count)
            if result.get("type") or result.get("status") != "succeeded":
                return result
    own = {(*xy(e), e.get("direction")) for e in entities if e["name"] == "transport-belt"}
    for position, unit in zip(tap["remove"], tap["remove_units"]):
        old = live.get(("transport-belt", *_tile(position)))
        if old is None or (old.get("unit_number") != unit and (*xy(old), old.get("direction")) in own):
            continue
        if old.get("unit_number") != unit:
            return {"status": "blocked", "reason": "mine_trunk_tap_trunk_identity_changed", "tap": tap["name"]}
        return {
            "type": "mine",
            "name": old["name"],
            "position": old["position"],
            "count": 1,
            "expected_entity_unit": unit,
            "expected_entity_world_id": obs["world_id"],
            "reason": "normally recover one trunk belt of the patch bank for a fully stocked splitter tap",
        }
    result = builder.ensure_plan(obs, tap_plan(tap, extra, world_id=obs["world_id"]))
    if result.get("status") != "succeeded":
        return result
    tap["built"] = True
    return configure(builder, obs, tap, splitters)


def tap_standing(obs, tap: dict, extra=()) -> bool:
    """Every tap entity stands and the tap splitter carries its wanted priorities."""
    live = {(e["name"], *_tile(e["position"])): e for e in obs["entities"]}
    for e in list(tap["entities"]) + list(extra):
        found = live.get((e["name"], *_tile(e["position"])))
        if found is None or found.get("direction", 0) != e.get("direction", 0):
            return False
    found = live.get(("splitter", *_tile(tap["splitter"]["position"])))
    wanted = tap_priorities(tap)
    return found is not None and all(found.get(k) == v for k, v in wanted.items())


def priority_side(tap: dict) -> str:
    """``branch`` or ``trunk``: which output the tap splitter serves first (for receipts and tests)."""
    side = splitter_side(tap["splitter"], tap["port"]["position"])
    return "branch" if tap_priorities(tap)["splitter_output_priority"] == side else "trunk"


# ----- demand, supply, growth ---------------------------------------------------------------
def primary_demand(trunk: dict) -> float:
    """Demand of the trunk's first consumer: the patch bank's nominal rate when the first tap was planned.

    Recorded on every tap at planning time; the smallest is the one before any trunk growth
    (a bank only grows), so growth the receipt itself proposes never raises the demand
    again (no growth loop) and a later tap does not count earlier taps' growth twice.
    """
    rows = [float(t.get("primary_demand", 0)) for t in trunk.get("taps", {}).values()]
    return min((r for r in rows if r > 0), default=0.0)


def trunk_receipt(root, jobs, obs, *, belt: str = "transport-belt") -> dict | None:
    """Per trunk: demand (first consumer + every tap), supply (proven banks of the patch), lanes.

    ``jobs`` is accepted for the observe receipt's common signature.
    Also: ``independent_groups`` (supplies that opened their own drill group because
    the trunk had no tap site, :func:`fallback_groups`) and ``power_tap``
    (``power_coal_tap.tap_receipt``: whether the boiler feed can move onto the coal trunk).
    """
    root = Path(root)
    state = ledger(root, obs.get("world_id"))
    mining = _read(root / "mining-outposts.json")
    rows = _trunk_rows(state, mining, obs, belt) if mining.get("world_id") == obs.get("world_id") else []
    from .power_coal_tap import tap_receipt

    receipt = {"trunks": rows}
    fallbacks = fallback_groups(root, obs.get("world_id"))
    if fallbacks:
        receipt["independent_groups"] = fallbacks
    power = tap_receipt(root, obs)
    if power is not None:
        receipt["power_tap"] = power
    return receipt if rows or len(receipt) > 1 else None


def fallback_groups(root, world_id) -> list[dict]:
    """Dedicated-material supplies that opened their own drill group (``second_group_reason``)."""
    rows = []
    for name in ("material-supply.json", "material-supply-chemistry.json"):
        state = _read(Path(root) / name)
        if state.get("world_id") != world_id:
            continue
        for resource, entry in sorted((state.get("supplies") or {}).items()):
            why = entry.get("second_group_reason") if isinstance(entry, dict) else None
            if why:
                reason = why.get("reason") if isinstance(why, dict) else str(why)
                rows.append({"state": name, "resource": resource, "reason": reason})
    return rows


def _trunk_rows(state, mining, obs, belt) -> list[dict]:
    rows = []
    for key, trunk in sorted(state["trunks"].items()):
        bank = find_bank(mining.get("banks"), key)
        if bank is None:
            continue
        resource = bank["plan"]["resource"]
        seed = patch_seed(obs, resource)
        supply = sum(
            float(b.get("nominal_rate_per_minute", 0))
            for b in mining.get("banks", [])
            if b.get("ready") and on_patch(b, resource, seed) and not b.get("material_supply_id")
        )
        taps = sum(float(t.get("demand_per_minute", 0)) for t in trunk.get("taps", {}).values())
        demand = primary_demand(trunk) + taps
        rate = belt_rate(belt)
        lanes = lane_count(demand, rate)
        built = lanes_built(trunk)
        rows.append(
            {
                "trunk": key,
                "resource": resource,
                "demand_per_minute": round(demand, 3),
                "supply_per_minute": round(supply, 3),
                "taps": sorted(trunk.get("taps", {})),
                "lanes_needed": lanes,
                "lanes_built": built,
                "balancer": balancer_plan(lanes),
                "grow": demand > supply + 1e-9,
                "second_lane_required": lanes > 1 and built < 2,
                # More than two lanes: reserved and recorded only (docs/v2/BALANCERS.md).
                **({"lanes_reserved": lanes - 2} if lanes > 2 else {}),
            }
        )
    return rows


def mine_trunk_nodes(jobs, queue_nodes, observation, capabilities) -> list[dict]:
    """One ``mining_outpost`` node per trunk whose demand exceeds its bank (grows the same bank in place)."""
    receipt = observation.get("mine_trunks")
    rows = receipt.get("trunks") if isinstance(receipt, dict) else None
    if not isinstance(rows, list) or "mining_outpost" not in capabilities:
        return []
    jobs = list(jobs)
    queued = {node.get("id") for node in queue_nodes}
    nodes = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("second_lane_required") or LANE_CAPABILITY not in capabilities:
            continue
        resource = row.get("resource")
        node_id = f"{NODE_PREFIX}lane_{resource}"
        if node_id in queued or any(
            r.get("capability") == LANE_CAPABILITY and (r.get("parameters") or {}).get("resource") == resource
            for r in jobs + list(queue_nodes)
        ):
            continue  # proposed once per resource; a stalled lane is not proposed again
        nodes.append(
            {
                "id": node_id,
                "capability": LANE_CAPABILITY,
                "parameters": {"resource": resource},
                "priority": NODE_PRIORITY,
                "depends_on": [],
                "reason": f"mine trunk {row.get('trunk')}: demand {row.get('demand_per_minute')}/min needs "
                f"{row.get('lanes_needed')} belts; build the second lane and its 2-2 splitter"[:300],
            }
        )
    for row in rows:
        if not isinstance(row, dict) or not row.get("grow"):
            continue
        resource = row.get("resource")
        rate = min(960, math.ceil(float(row.get("demand_per_minute", 0))))
        parameters = {"resource": resource, "rate_per_minute": rate}

        def mining(r):
            p = r.get("parameters")
            return r.get("capability") == "mining_outpost" and isinstance(p, dict) and p.get("resource") == resource

        if any(mining(n) and n.get("status", "pending") in OPEN_NODE_STATUSES for n in queue_nodes):
            continue
        if any(mining(j) and j.get("status") not in ("succeeded", "blocked", "failed") for j in jobs):
            continue
        if any(mining(j) and j.get("parameters") == parameters for j in jobs):
            continue  # the same target ran already; a stalled one is not proposed again
        node_id = f"{NODE_PREFIX}{resource}_{rate}"
        if node_id in queued:
            continue
        nodes.append(
            {
                "id": node_id,
                "capability": "mining_outpost",
                "parameters": parameters,
                "priority": NODE_PRIORITY,
                "depends_on": [],
                "reason": f"mine trunk {row.get('trunk')}: demand {row.get('demand_per_minute')}/min above "
                f"supply {row.get('supply_per_minute')}/min; grow the patch bank in place"[:300],
            }
        )
    return nodes


__all__ = [
    "WINDOW_ROWS",
    "balancer_plan",
    "cutover",
    "hub",
    "lane_count",
    "lane_saturation",
    "mine_trunk_nodes",
    "patch_bank",
    "plan_tap",
    "record_tap",
    "release_trunk_belts",
    "tap_priorities",
    "trace_trunk",
    "trunk_bus",
    "trunk_receipt",
]
