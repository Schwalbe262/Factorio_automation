"""The main bus trunk, built from the site plan on any seed (capability ``planned_main_bus``).

Geometry comes only from the master plan (``site.busframe.Bus``): the bus
head ``origin``, its flow ``direction`` (any of N/E/S/W) and the lane table.
For each built material (``BUILT_MATERIALS``) the first lane of that material
gets a straight trunk of ``TRUNK_TILES`` belts (capped at the bus length)
from the head along the flow direction. Foreign belts crossing the lane, built
or only planned by another board block (``board.foreign_belts``), are passed
with ordinary underground pairs (``transport.compile_crossings``); a trunk that
would still meet or side-load planned hardware is refused (``board.surface_conflict``).
The other lanes of the table stay reserved, nothing is built on them.

Feed: red science takes each smelting district plate port and green science
splits off one tile upstream of the port on one side. The bus splits off
three tiles upstream on the *other* side (district canonical frame, plates
leave east)::

    canonical, port P at (x, y) facing east
      (x-3, y)      district output belt  -> replaced by a splitter facing east
      (x-3, y+1)    second splitter tile
      (x-2..x+2, y+1) branch belts facing east; (x+2, y+1) lies outside the
                    district reservation and is the feed source port

so red keeps the port and green keeps its anchor belt at (x-1, y). The feed
splitter has output priority to the bus branch (input priority none, one
input): plates go to the bus first, the district port gets what the bus does
not take (all of it once the dead-ended trunk and its taps back up).
The branch is turned with the district's ``site_transform``, so rotated or
mirrored districts need nothing special. The feed is routed to the lane head
with ``transport.plan_connection`` guided by the reserved
``corridor:smelting->bus``.

Splitter priorities (``feed_priorities``, ``tap_priorities``): native sides are
relative to the splitter's own facing, so they are derived from the facing
and a point on the branch (``splitter_side``), never from map axes. Rule:
a one-input splitter that hands a branch off a belt gives the branch output
priority (feed: bus branch; tap: consumer branch); where two belts merge onto
a bus lane (a later lane injection, a second lane of a material) the input
priority goes to the bus lane (not built yet). The specs carry
``splitter_input_priority`` / ``splitter_output_priority``; commissioning
issues ``configure_splitter`` (unit and world guarded) for every built feed
and tap splitter whose live priorities differ, also on older saved states
(``annotate_state``), and the proof requires them.

Phases (``planned-main-bus.json``): ``build`` (trunks, feeds, branch belts),
``splice`` (district plan gets the splitter, then the old belt is mined and
the splitter built: the only break in red science's supply is these few
actions, the splitter is in the inventory before the belt is mined),
``commission`` (until the proof passes), ``verified``.

Publication: ``board["shared_main_bus"]`` with ``template`` ``planned_main_bus``,
the bus frame, the lane table (built / reserved / spare), trunks, per-material
lane-band ``reservations`` (whole bus length; ``transport`` keeps other routes
out of them), head/tail ``ports``, ``entities`` and ``underground_pairs``.
Taps (``planned_tap.py``) are planned by each consumer together with its
block and recorded in the state's ``taps``; ``effective_plan`` is the bus
plan with every tap applied (publication and proof use it). The old
``bus_tap.BusTap`` only understands the development-map bus; see
``docs/v2/MAIN_BUS.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

from .board import blocks_without_bus, entity, foreign_belts, surface_conflict
from .core.deterministic_builder import FactoryBuilder, _plan
from .core.deterministic_state import _atomic_json
from .planning import preflight
from .plate_supply import district_ports
from .routing import VECTORS
from .site.busframe import Bus
from .site.transform import Transform, apply as apply_transform
from .smelting import merge_entities
from .transport import compile_crossings, plan_connection, underground_proof, xy

STATE_FILE = "planned-main-bus.json"
OWNER = "planned_main_bus"
TEMPLATE = "planned_main_bus"
HEADER_CORRIDOR = "corridor:smelting->bus"
BUILT_MATERIALS = ("iron-plate", "copper-plate")
# Initial trunk per built lane; extended later as districts move down the bus.
TRUNK_TILES = 64
FACING = {"N": 0, "E": 4, "S": 8, "W": 12}
BELTS = ("transport-belt", "underground-belt", "splitter")
MAX_SPAN_LUA = 'return {ok=true,max_span=prototypes.entity["underground-belt"].max_underground_distance}'
PRIORITY_KEYS = ("splitter_input_priority", "splitter_output_priority")
_FORWARD = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}


# ----- splitter priorities (pure) ------------------------------------------------
def splitter_side(splitter: dict, point: dict) -> str:
    """``"left"`` or ``"right"``: the side of ``splitter``, seen along its facing, that ``point`` lies on.

    Native splitter priorities name sides relative to the splitter's own
    facing, so the side is derived from the facing vector and the point, never
    from map axes: any bus direction, tap side, rotation or mirror.
    """
    dx, dy = _FORWARD[splitter["direction"]]
    rx, ry = -dy, dx  # right of the facing (map y grows south)
    p = splitter["position"]
    lateral = (point["x"] - p["x"]) * rx + (point["y"] - p["y"]) * ry
    if lateral == 0:
        raise ValueError("point lies on the splitter centre line")
    return "right" if lateral > 0 else "left"


def branch_priorities(splitter: dict, branch_point: dict) -> dict:
    """Native priorities of a one-input splitter whose branch leaves towards ``branch_point``.

    Output priority to the branch: the consumer behind it takes what it can
    use, only the surplus continues on the other output. A single input needs
    no input priority (``"none"``).
    """
    return {"splitter_input_priority": "none", "splitter_output_priority": splitter_side(splitter, branch_point)}


def feed_priorities(feed: dict) -> dict:
    """District feed splitter: plates go to the bus branch first, the district port takes the rest."""
    return branch_priorities(feed["splitter"], feed["source"]["position"])


def tap_priorities(tap: dict) -> dict:
    """Tap splitter: its consumer branch (towards the tap port) first, the surplus continues down the bus.

    A feed (``planned_tap.feed_layout``, kind ``feed``) carries its own: output to the trunk side,
    no input priority."""
    if tap.get("kind") == "feed":
        return {k: tap["splitter"][k] for k in PRIORITY_KEYS}
    return branch_priorities(tap["splitter"], tap["port"]["position"])


def wanted_splitters(state: dict) -> list[tuple[dict, dict]]:
    """``(splitter spec, wanted priorities)`` of every feed and recorded tap splitter."""
    rows = [(f["splitter"], feed_priorities(f)) for f in state.get("feeds", [])]
    return rows + [(t["splitter"], tap_priorities(t)) for t in state.get("taps", [])]


def priorities_match(live: dict | None, wanted: dict) -> bool:
    return live is not None and all(live.get(k) == v for k, v in wanted.items())


def configure_action(builder, obs, rows) -> dict | None:
    """The next paid ``configure_splitter`` for a built splitter of ``rows`` whose priorities differ, else None.

    Idempotent: a splitter that already matches, or is not built yet, needs
    nothing. Guarded by the observed unit number and world id.
    """
    live = {xy(e): e for e in obs["entities"] if e["name"] == "splitter"}
    for splitter, wanted in rows:
        found = live.get(xy(splitter))
        if found is None or found.get("direction") != splitter.get("direction") or priorities_match(found, wanted):
            continue
        move = getattr(builder, "_move", None)  # character backend: walk into reach first
        step = move(obs, found["position"]) if callable(move) else None
        if isinstance(step, dict):
            return step
        return {
            "type": "configure_splitter",
            "name": "splitter",
            "position": found["position"],
            "input_priority": wanted["splitter_input_priority"],
            "output_priority": wanted["splitter_output_priority"],
            "expected_splitter_unit": found["unit_number"],
            "expected_splitter_world_id": obs["world_id"],
            "reason": "set planned bus splitter priorities: branch output first",
        }
    return None


def _annotate(spec: dict, wanted: dict) -> bool:
    """Put ``wanted`` priorities on a splitter spec; True when it changed."""
    if all(spec.get(k) == v for k, v in wanted.items()):
        return False
    spec.update(wanted)
    return True


def annotate_state(state: dict) -> bool:
    """Wanted priorities on every feed / tap splitter spec of a bus state (older states lack them)."""
    changed = False
    by_tile = {}
    for splitter, wanted in wanted_splitters(state):
        changed |= _annotate(splitter, wanted)
        by_tile[xy(splitter)] = wanted
    plans = [state.get("plan"), state.get("staged_plan")] + [{"entities": t["entities"]} for t in state.get("taps", [])]
    for plan in plans:
        for e in (plan or {}).get("entities", []):
            if e["name"] == "splitter" and xy(e) in by_tile:
                changed |= _annotate(e, by_tile[xy(e)])
    return changed


# ----- geometry (pure) ---------------------------------------------------------
def built_lanes(bus: Bus, materials=BUILT_MATERIALS) -> list:
    """The first lane of each material in ``materials`` (lane table order); missing ones are skipped."""
    lanes = []
    for item in materials:
        lane = next((lane for lane in bus.lanes if lane.material == item), None)
        if lane is not None:
            lanes.append(lane)
    return lanes


def lane_tile(bus: Bus, lane, along: int) -> dict:
    """Map position of the lane's tile ``along`` tiles from the head."""
    x, y = bus.local_point(along + 0.5, lane.offset + 0.5)
    return {"x": x, "y": y}


def trunk_segments(bus: Bus, lane, length: int) -> list[dict]:
    facing = FACING[bus.direction]
    return [{"position": lane_tile(bus, lane, a), "direction": facing} for a in range(length)]


def trunk_plan(bus: Bus, lane, length: int, foreign, max_span: int) -> dict:
    """Straight trunk belts; foreign belts on the lane are crossed underground."""
    segments = trunk_segments(bus, lane, length)
    route = {"ok": True, "segments": segments, "metrics": {"length": length, "turns": 0}}
    return compile_crossings(route, foreign, maximum_span=max_span)


def lane_band(bus: Bus, item: str) -> list[list[int]]:
    """``[[x0, y0], [x1, y1]]`` of every lane of ``item`` over the whole bus length."""
    offsets = [lane.offset for lane in bus.lanes if lane.material == item]
    rect = bus.local_rect(0, bus.length, min(offsets), max(offsets) + 1)
    return [[rect.x0, rect.y0], [rect.x1, rect.y1]]


def feed_branch(district_plan: dict, port: dict, upstream: int = 3) -> dict:
    """Splitter, branch belts, feed source and anchor for one district plate port (see module doc).

    ``upstream``: tiles from the port to the anchor belt (3 in the module doc; ``FEED_UPSTREAM``
    lists the further ones ``prepare`` tries when the district plan stands beside the default).
    """
    turn = Transform.from_dict(district_plan.get("site_transform"))
    canonical = apply_transform(turn.inverse(), port)
    if canonical.get("facing") != 4:
        raise ValueError("district plate port does not leave east in the district frame")
    x, y = canonical["position"]["x"], canonical["position"]["y"]
    shape = {
        "anchor": {"x": x - upstream, "y": y},
        "splitter": dict(entity("splitter", x - upstream, y + 0.5, 4), _width=2, _height=1),
        "belts": [entity("transport-belt", x + dx, y + 1, 4) for dx in range(1 - upstream, 3)],
        "source": {
            "kind": "item",
            "item": port["item"],
            "position": {"x": x + 2, "y": y + 1},
            "facing": 4,
            "direction": "output",
        },
    }
    feed = dict(apply_transform(turn, shape), item=port["item"], district_port=dict(port["position"]))
    feed["splitter"].update(feed_priorities(feed))
    if upstream != 3:
        feed["upstream"] = upstream
    return feed


# Anchor distances the feed tries in order. The district plan may cross its own plate output belt
# right beside the default anchor (a coal or ore route diving under it): aud-1114-0436 seeds 418128958
# and 790557522 had a district underground on the feed splitter's second tile; the fixed geometry
# met it on every re-plan (790: built before the bus planned, preflight failed 5 times; 418: still
# unbuilt, so the bus mined the district's plate belt and then could not place its splitter).
# 2 comes second: a crossing at the default anchor column also lies under every longer branch;
# 2 keeps green science's anchor at x-1 and red's port at x (module doc).
FEED_UPSTREAM = (3, 2, 4, 5, 6)
GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)


def _footprint(e: dict) -> dict:
    """A plan entity with the splitter's 2x1 size when the plan leaves it out."""
    if e.get("name") == "splitter" and "_width" not in e:
        return dict(e, _width=2, _height=1)
    return e


def feed_meets_district(feed: dict, district_plan: dict) -> bool:
    """True when the feed's splitter or branch belts stand on a district plan tile other than its anchor belt."""
    anchor = _at(feed["anchor"])
    others = [
        _footprint(e)
        for e in district_plan.get("entities", ())
        if not (e["name"] == "transport-belt" and xy(e) == anchor)
    ]
    planned = GEOMETRY._occupied_by_plan(others)
    return bool(planned & GEOMETRY._occupied_by_plan([feed["splitter"], *feed["belts"]]))


def _at(position) -> tuple:
    return (position["x"], position["y"])


def _inside(area, position) -> bool:
    (x0, y0), (x1, y1) = area
    return x0 < position["x"] < x1 and y0 < position["y"] < y1


def corridor_box(corridor):
    """Bounding box ``(x0, y0, x1, y1)`` of a plan corridor, or None."""
    if corridor is None or not corridor.segments:
        return None
    rects = corridor.rects
    return (min(r.x0 for r in rects), min(r.y0 for r in rects), max(r.x1 for r in rects), max(r.y1 for r in rects))


def spliced_district_plan(plan: dict, feeds: list[dict]) -> dict:
    """The district plan with each feed's anchor belt replaced by its splitter (idempotent)."""
    anchors = {(f["anchor"]["x"], f["anchor"]["y"]) for f in feeds}
    kept = [e for e in plan["entities"] if not (e["name"] == "transport-belt" and xy(e) in anchors)]
    extra = {k: v for k, v in plan.items() if k not in ("ok", "reason", "entities", "ports", "required_items")}
    return _plan(merge_entities(kept, [f["splitter"] for f in feeds]), plan["ports"], **extra)


def trunk_ports(bus: Bus, trunks: list[dict]) -> list[dict]:
    """Head (input) and tail (output) port of every built trunk."""
    facing = FACING[bus.direction]
    ports = []
    for row in trunks:
        for role, key in (("input", "head"), ("output", "tail")):
            ports.append(
                {
                    "kind": "item",
                    "item": row["item"],
                    "position": row[key],
                    "facing": facing,
                    "direction": role,
                    "lane": row["lane"],
                    "role": key,
                }
            )
    return ports


def _tapped(plan: dict, taps: list, ports: list) -> dict:
    removed = {_at(p) for tap in taps for p in tap["remove"]}
    kept = [e for e in plan["entities"] if not (e["name"] == "transport-belt" and xy(e) in removed)]
    entities = merge_entities(kept, *(tap["entities"] for tap in taps))
    pairs = plan.get("underground_pairs", []) + [p for tap in taps for p in tap["underground_pairs"]]
    extra = {k: v for k, v in plan.items() if k not in ("ok", "reason", "entities", "ports", "required_items")}
    extra["underground_pairs"] = pairs
    return _plan(entities, ports, **extra)


def effective_plan(state: dict) -> dict:
    """The bus plan with every recorded tap applied: replaced trunk belts out, tap hardware and ports in."""
    plan = state["plan"]
    taps = state.get("taps", [])
    if not taps:
        return plan
    ports = trunk_ports(Bus.from_dict(state["bus"]), state["trunks"]) + [tap["port"] for tap in taps]
    return _tapped(plan, taps, ports)


def build_plan(state: dict, plan: dict, obs: dict | None = None) -> dict:
    """``plan`` (staged or full) with every built tap applied, for the bus's own build and splice.

    A consumer may cut its tap into a built trunk while the bus is still
    building (a produce row on the iron lane): the bus must not want its old
    trunk belt back under that splitter (pace1 seeds 1634401926, 1193005910:
    "reserved block placement is obstructed" for 15 minutes, no bus, no red science).
    A recorded tap that is not built yet keeps the belt until its owner cuts it over.
    With ``obs``, a tap whose cut-over has started counts too: its splitter stands, or one of
    the trunk belts it recorded (``remove_units``) is gone. The owner marks the tap built only
    on its next step; in between the bus wanted its belt back under the splitter (aud-1114-0614
    seed 375147794: the transport-belt row mined (-82.5,52.5) and built its splitter; the bus
    blocked "reserved block placement is obstructed" for the rest of the run).
    """
    taps = [tap for tap in state.get("taps", []) if tap.get("built") or (obs is not None and _cutting(obs, tap))]
    return _tapped(plan, taps, plan["ports"]) if taps else plan


def _cutting(obs: dict, tap: dict) -> bool:
    """The tap's owner has begun its cut-over: its splitter stands, or a recorded trunk belt unit is gone."""
    live = {(e["name"], *xy(e)): e for e in obs.get("entities", ())}
    splitter = tap.get("splitter")
    if isinstance(splitter, dict) and ("splitter", *xy(splitter)) in live:
        return True
    units = tap.get("remove_units") or ()
    for position, unit in zip(tap.get("remove") or (), units):
        old = live.get(("transport-belt", *xy({"position": position})))
        if unit is not None and (old is None or old.get("unit_number") != unit):
            return True
    return False


def publication(state: dict) -> dict:
    """``board["shared_main_bus"]`` for a prepared bus (see module doc)."""
    from .planned_tap import station

    bus = Bus.from_dict(state["bus"])
    built = {row["item"]: row for row in state["trunks"]}
    lanes = []
    for lane in bus.lanes:
        row = dict(lane.to_dict(), head=lane_tile(bus, lane, 0))
        trunk = built.get(lane.material)
        if trunk is not None and trunk["lane"] == lane.index:
            row.update(status="built", tail=trunk["tail"], length=trunk["length"])
        else:
            row["status"] = "reserved" if lane.material else "spare"
        lanes.append(row)
    materials = list(dict.fromkeys(lane.material for lane in bus.lanes if lane.material))
    reservations = [
        {
            "item": item,
            "lanes": [lane.index for lane in bus.lanes if lane.material == item],
            "area": lane_band(bus, item),
            "status": "active" if item in built else "reserved",
        }
        for item in materials
    ]
    plan = effective_plan(state)
    return {
        "template": TEMPLATE,
        "version": 1,
        "world_id": state["world_id"],
        "flow_direction": FACING[bus.direction],
        "frame": bus.to_dict(),
        "lanes": lanes,
        "trunks": state["trunks"],
        "reservations": reservations,
        "ports": plan["ports"],
        "entities": plan["entities"],
        "underground_pairs": plan.get("underground_pairs", []),
        "sources": [
            {k: f[k] for k in ("item", "source", "district_port")} | {"splitter": f["splitter"]["position"]}
            for f in state["feeds"]
        ],
        "stations": [station(tap) for tap in state.get("taps", [])],
    }


# ----- controller ----------------------------------------------------------------
def failure_scalars(detail: dict) -> dict:
    """Scalar fields of a nested preflight failure: the job ledger keeps only scalars as evidence."""
    out = {"failed_check": str(detail.get("reason"))[:200]}
    entity = detail.get("entity")
    if isinstance(entity, dict):
        out["failed_entity"] = str(entity.get("name"))
        if isinstance(entity.get("position"), dict):
            out["position"] = dict(entity["position"])
    elif isinstance(entity, str):
        # planning.preflight names the obstruction by name and position at the top level
        # (790557522 kept only failed_check; the post-mortem found a district underground).
        out["failed_entity"] = entity[:80]
        if isinstance(detail.get("position"), dict):
            out["position"] = dict(detail["position"])
    blockers = detail.get("blockers")
    if isinstance(blockers, list) and blockers and isinstance(blockers[0], dict):
        out["first_blocker"] = f"{blockers[0].get('name')}: {blockers[0].get('reason')}"[:200]
    return out


def proven_length(state: dict, trunk: dict, bus: Bus) -> int:
    """Trunk tiles the proof walks: the prepared trunk plus the extensions of BUILT taps only.

    ``planned_tap.publish`` extends ``trunk["length"]`` (and its tail) when a consumer records its
    tap, before the tap's extension belts exist. The proof walked to that unbuilt tail: on
    aud-1114-0436 seed 2311522763 an unbuilt inserter row's tap moved both tails from 64 to 72 and
    the fully built bus, 64 tiles loaded with plates, waited from 28.8 min to the end of the run.
    """
    length = min(TRUNK_TILES, bus.length)
    for tap in state.get("taps", ()):
        if not tap.get("built"):
            continue
        extension = tap.get("extension") or {}
        target = extension.get(trunk["lane"], extension.get(str(trunk["lane"])))
        if isinstance(target, int) and not isinstance(target, bool):
            length = max(length, target)
    return min(length, trunk["length"])


def _splitter_over(live: dict, belt: dict):
    """A live splitter with the belt's direction covering the belt's tile, or None."""
    x, y = xy(belt)
    for dx, dy in ((0.5, 0), (-0.5, 0), (0, 0.5), (0, -0.5)):
        found = live.get(("splitter", x + dx, y + dy))
        if found is not None and found.get("direction") == belt.get("direction"):
            return found
    return None


class PlannedBus:
    """Plan, build and prove the iron/copper trunk of the planned main bus."""

    def __init__(self, game, builder, catalog, smelting, site=None, *, route=plan_connection, check=preflight):
        self.game, self.builder, self.catalog, self.smelting, self.site = game, builder, catalog, smelting, site
        self.route, self.check = route, check
        self.root = Path(game.cfg.runtime_dir)
        self.path = self.root / STATE_FILE
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def _board(self) -> dict:
        path = self.root / "pcb-board.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    # ----- planning --------------------------------------------------------------
    def prepare(self, obs) -> dict:
        """Pure planning: lanes, trunks, feed branches and routes, whole preflight. No ``act``."""
        if self.site is None:
            return {"ok": False, "reason": "planned_bus_requires_site_plan"}
        try:
            master = self.site.plan(obs["world_id"])
        except Exception as exc:  # noqa: BLE001 - survey or cache failure is a blocker, not a crash
            return {"ok": False, "reason": "planned_bus_site_plan_unavailable", "detail": str(exc)[:200]}
        bus = master.bus
        board = self._board()
        district = self.smelting.state
        if board.get("world_id") != obs["world_id"] or district.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "planned_bus_district_world_changed"}
        if "smelting_district" not in board.get("blocks", {}):
            return {"ok": False, "reason": "planned_bus_district_not_registered"}
        other = board.get("shared_main_bus")
        if other and other.get("template") != TEMPLATE:
            return {"ok": False, "reason": "planned_bus_board_has_other_main_bus"}
        lanes = built_lanes(bus)
        if [lane.material for lane in lanes] != list(BUILT_MATERIALS):
            return {"ok": False, "reason": "planned_bus_lane_table_lacks_material"}
        ports = {p["item"]: p for p in district_ports(self.root, board, obs["world_id"])}
        missing = [item for item in BUILT_MATERIALS if item not in ports]
        if missing:
            return {"ok": False, "reason": "planned_bus_district_port_missing", "item": missing[0]}
        span = self.game.query(MAX_SPAN_LUA)
        if not isinstance(span, dict) or not span.get("ok"):
            return {"ok": False, "reason": "planned_bus_underground_span_unknown"}
        length = min(TRUNK_TILES, bus.length)
        # Other blocks' planned-but-unbuilt belts are crossed like built ones, and
        # no trunk tile may meet or side-load their planned hardware (board.foreign_belts).
        blocks = blocks_without_bus(board)
        observed = foreign_belts(obs, blocks)
        entities, pairs, trunks = [], [], []
        for lane in lanes:
            trunk = trunk_plan(bus, lane, length, observed, span["max_span"])
            if not trunk.get("ok"):
                return {"ok": False, "reason": "planned_bus_trunk_blocked", "item": lane.material, "detail": trunk}
            clash = surface_conflict(self.builder, obs, blocks, trunk["entities"])
            if clash is not None:
                return {
                    "ok": False,
                    "reason": "planned_bus_trunk_meets_planned_hardware",
                    "item": lane.material,
                    "detail": clash,
                }
            entities = merge_entities(entities, trunk["entities"])
            pairs += trunk.get("underground_pairs", [])
            trunks.append(
                {
                    "item": lane.material,
                    "lane": lane.index,
                    "head": lane_tile(bus, lane, 0),
                    "tail": lane_tile(bus, lane, length - 1),
                    "length": length,
                }
            )
        # Each port's feed at the first anchor distance (FEED_UPSTREAM) whose splitter and branch stay
        # off the district plan; then routes and the whole preflight. A failed route or preflight tries
        # the next distance before the bus blocks with the first (default) failure.
        trunk_entities, failure = entities, None
        for first in range(len(FEED_UPSTREAM)):
            feeds = self._feeds(obs, district, ports, first)
            if not feeds["ok"]:
                failure = failure or feeds
                continue
            entities = merge_entities(trunk_entities, *([f["splitter"], *f["belts"]] for f in feeds["feeds"]))
            anchors = [f["anchor_unit"] for f in feeds["feeds"]]
            guide = corridor_box(master.corridor(HEADER_CORRIDOR))
            routed = self._route_feeds(obs, bus, lanes, feeds["feeds"], entities, guide)
            if not routed["ok"]:
                failure = failure or routed
                continue
            whole = self.check(self.game, self.builder, obs, routed["entities"], ignored_units=anchors)
            if not whole.get("ok"):
                failure = failure or {
                    "ok": False,
                    "reason": "planned_bus_whole_preflight_failed",
                    "detail": whole,
                    **failure_scalars(whole),
                }
                continue
            feeds, entities = feeds["feeds"], routed["entities"]
            pairs += routed["pairs"]
            break
        else:
            return failure
        ports_out = trunk_ports(bus, trunks)
        common = dict(
            layout_policy="pcb-planned-main-bus-v1",
            underground_pairs=pairs,
            clear_obstacles=whole.get("clear_obstacles", []),
            clearance_world_id=obs["world_id"],
        )
        splitters = {xy(f["splitter"]) for f in feeds}
        return {
            "ok": True,
            "world_id": obs["world_id"],
            "catalog_fingerprint": self.catalog.fingerprint,
            "bus": bus.to_dict(),
            "trunks": trunks,
            "feeds": feeds,
            "routes": routed["metrics"],
            "plan": _plan(entities, ports_out, **common),
            "staged_plan": _plan(
                [e for e in entities if not (e["name"] == "splitter" and xy(e) in splitters)], ports_out, **common
            ),
            "phase": "build",
        }

    def _feeds(self, obs, district: dict, ports: dict, first: int) -> dict:
        """Per built material the feed at the first distance of ``FEED_UPSTREAM[first:]`` that fits.

        Fits: an observed district belt in the splitter's direction on the anchor tile that the
        district plan owns, and the splitter and branch belts on no other district plan tile.
        """
        feeds = []
        for item in BUILT_MATERIALS:
            failure = None
            for upstream in FEED_UPSTREAM[first:]:
                try:
                    feed = feed_branch(district["plan"], ports[item], upstream)
                except (KeyError, ValueError) as exc:
                    return {"ok": False, "reason": "planned_bus_feed_branch_invalid", "item": item, "detail": str(exc)}
                if _inside(district["plan"].get("reserved_area", [[0, 0], [0, 0]]), feed["source"]["position"]):
                    return {"ok": False, "reason": "planned_bus_feed_source_inside_district", "item": item}
                anchor = next(
                    (
                        e
                        for e in obs["entities"]
                        if e["name"] == "transport-belt"
                        and _at(feed["anchor"]) == xy(e)
                        and e["direction"] == feed["splitter"]["direction"]
                    ),
                    None,
                )
                owned = any(
                    e["name"] == "transport-belt" and xy(e) == _at(feed["anchor"]) for e in district["plan"]["entities"]
                )
                if anchor is None or not owned:
                    failure = failure or {"ok": False, "reason": "planned_bus_feed_anchor_missing", "item": item}
                    continue
                if feed_meets_district(feed, district["plan"]):
                    failure = failure or {
                        "ok": False,
                        "reason": "planned_bus_feed_meets_district_plan",
                        "item": item,
                        "upstream": upstream,
                    }
                    continue
                feed["anchor_unit"] = anchor["unit_number"]
                feeds.append(feed)
                break
            else:
                return failure or {"ok": False, "reason": "planned_bus_feed_anchor_missing", "item": item}
        return {"ok": True, "feeds": feeds}

    def _route_feeds(self, obs, bus, lanes, feeds, entities, guide) -> dict:
        """Route each feed source to its lane head; the second order is tried when the first fails."""
        facing = FACING[bus.direction]
        heads = {
            lane.material: {
                "kind": "item",
                "item": lane.material,
                "position": lane_tile(bus, lane, 0),
                "facing": facing,
                "direction": "input",
            }
            for lane in lanes
        }
        failures = []
        # The feed ends on the lane head inside the bus band: a bus route.
        extra = {"bus_route": True} if guide is None else {"guide": guide, "bus_route": True}

        vx, vy = VECTORS[facing]
        approach = {
            feed["item"]: (heads[feed["item"]]["position"]["x"] - vx, heads[feed["item"]]["position"]["y"] - vy)
            for feed in feeds
        }

        def route(feed, reserved, keep_clear=()):
            more = {"keep_clear": keep_clear} if keep_clear else {}
            return self.route(
                self.game, self.builder, obs, feed["source"], heads[feed["item"]], reserved, **extra, **more
            )

        attempts = [(order, False) for order in (feeds, feeds[::-1])]
        if len(feeds) > 1:
            # Feeds whose sources lie beyond each other's lane head must cross (seed 3098273696: iron
            # from the north to the southern head, copper from the south to the northern one; either
            # feed alone fits, the second never: the first took the tile in front of the other head,
            # where no underground fits). An earlier feed keeps off the later feeds' head approach
            # tiles, so the later one can pass under its straight leg.
            attempts += [(order, True) for order in (feeds, feeds[::-1])]
        for order, spaced in attempts:
            es, pairs, metrics, good = list(entities), [], [], True
            for index, feed in enumerate(order):
                later = [approach[f["item"]] for f in order[index + 1 :]] if spaced else []
                result = route(feed, es, later)
                if not result.get("ok"):
                    failures.append({"item": feed["item"], "reason": result.get("reason")})
                    good = False
                    break
                try:
                    es = merge_entities(es, result["entities"])
                except ValueError:
                    failures.append({"item": feed["item"], "reason": "feed_route_conflicts_with_bus"})
                    good = False
                    break
                pairs += result.get("underground_pairs", [])
                metrics.append({"item": feed["item"], **(result.get("route_metrics") or {})})
            if good:
                return {"ok": True, "entities": es, "pairs": pairs, "metrics": metrics}
        return {
            "ok": False,
            "reason": "planned_bus_feed_route_failed",
            "failures": failures[-4:],
            # The job ledger keeps only scalar result fields as evidence (all-1113-2107: three
            # feed-route blocks with evidence {}, cause unknowable after the run).
            "route_failures": "; ".join(f"{f['item']}: {f.get('reason')}" for f in failures[-4:])[:400],
        }

    # ----- execution ---------------------------------------------------------------
    def _save(self):
        _atomic_json(self.path, self.state)

    def _annotate_priorities(self):
        """Wanted priorities onto older splitter specs; re-read first, consumers append taps to the file."""
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        if annotate_state(self.state):
            self._save()

    def _publish(self, obs) -> dict | None:
        path = self.root / "pcb-board.json"
        board = self._board()
        if board.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": "planned_bus_board_world_changed"}
        published = publication(self.state)
        changed = board.get("shared_main_bus") != published
        board["shared_main_bus"] = published
        connections = board.setdefault("connections", [])
        for feed in self.state["feeds"]:
            row = {
                "item": feed["item"],
                "source": feed["source"]["position"],
                "destination": next(t["head"] for t in self.state["trunks"] if t["item"] == feed["item"]),
                "owner": OWNER,
            }
            if row not in connections:
                connections.append(row)
                changed = True
        if changed:
            _atomic_json(path, board)
        return None

    def _splice_district(self, obs) -> dict | None:
        """Put the splitters into the district plan (state file and board block) once; idempotent."""
        district = self.smelting.state
        if district.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": "planned_bus_district_world_changed"}
        spliced = spliced_district_plan(district["plan"], self.state["feeds"])
        if spliced == district["plan"]:
            return None
        district["plan"] = spliced
        _atomic_json(self.smelting.path, district)
        path = self.root / "pcb-board.json"
        board = self._board()
        board["blocks"]["smelting_district"] = spliced
        _atomic_json(path, board)
        return None

    def _cut_over(self, obs) -> dict | None:
        """Splitter stock first, then mine each old anchor belt (exact unit only)."""
        live = {(e["name"], *xy(e)): e for e in obs["entities"]}
        pending = [f for f in self.state["feeds"] if ("splitter", *xy(f["splitter"])) not in live]
        if not pending:
            return None
        need = len(pending)
        if obs.get("inventory", {}).get("splitter", 0) < need:
            result = self.builder.bootstrap.ensure_item(obs, "splitter", need)
            if result.get("type") or result.get("status") != "succeeded":
                return result
        for feed in pending:
            old = live.get(("transport-belt", *_at(feed["anchor"])))
            if old is None:
                continue
            if old.get("unit_number") != feed["anchor_unit"]:
                return {"status": "blocked", "reason": "planned_bus_feed_anchor_identity_changed", "item": feed["item"]}
            # Never cut the district's plate line for a splitter that cannot stand there (418128958:
            # the anchor was mined, then a district underground on the second tile kept the splitter out).
            room = self.check(self.game, self.builder, obs, [feed["splitter"]], ignored_units=[feed["anchor_unit"]])
            if not room.get("ok"):
                return {
                    "status": "blocked",
                    "reason": "planned_bus_feed_splitter_obstructed",
                    "item": feed["item"],
                    **failure_scalars(room),
                }
            return {
                "type": "mine",
                "name": old["name"],
                "position": old["position"],
                "count": 1,
                "expected_entity_unit": old["unit_number"],
                "expected_entity_world_id": obs["world_id"],
                "reason": "normally recover one owned district plate belt for the paid bus feed splitter",
            }
        return None

    def step(self, obs, parameters=None) -> dict:
        if not self.state:
            prepared = self.prepare(obs)
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state = prepared
            self._save()
        if self.state["world_id"] != obs["world_id"] or self.state["catalog_fingerprint"] != self.catalog.fingerprint:
            return {"status": "blocked", "reason": "planned_bus_world_or_catalog_changed"}
        blocked = self._publish(obs)
        if blocked:
            return blocked
        if self.state["phase"] == "build":
            from .early_start import deferral

            # Early start: trunks and feed routes are built while the district is
            # still building; entities waiting for coming research or contested plates wait.
            defer = deferral(self.root, obs, self.catalog, "planned_main_bus")
            staged = build_plan(self.state, self.state["staged_plan"], obs)
            result = self.builder.ensure_plan(obs, staged, **({"defer": defer} if defer else {}))
            if result.get("status") != "succeeded":
                return result
            self.state["phase"] = "splice"
            self._save()
        if self.state["phase"] == "splice":
            from .early_start import capability_pending

            if capability_pending(self.root, obs["world_id"], "smelting_district"):
                # The splice rewrites the district plan and mines one of its belts: never during its build.
                return {
                    "status": "waiting",
                    "reason": "waiting for smelting_district to succeed before the bus feed splice",
                }
            for action in (self._splice_district(obs), self._cut_over(obs)):
                if action is not None:
                    return action
            result = self.builder.ensure_plan(obs, build_plan(self.state, self.state["plan"], obs))
            if result.get("status") != "succeeded":
                return result
            self.state["phase"] = "commission"
            self._save()
        # Commission: every built feed / tap splitter gets its native priorities first.
        self._annotate_priorities()
        action = configure_action(self.builder, obs, wanted_splitters(self.state))
        if action is not None:
            return action
        evidence = self.proof(obs)
        if not evidence:
            return {"status": "waiting", "reason": "waiting for district plates to reach every built bus lane tail"}
        if self.state["phase"] != "verified":
            self.state["phase"] = "verified"
            self._save()
        return {
            "status": "succeeded",
            "reason": "district plates flow along every built bus lane",
            "evidence": evidence,
        }

    def proof(self, obs) -> dict | None:
        """Fresh native evidence: every planned belt as planned, each built lane pure and loaded at its tail."""
        state = self.state
        if (
            not state
            or state.get("world_id") != obs["world_id"]
            or state.get("phase") not in ("commission", "verified")
        ):
            return None
        # Built taps only: a consumer records its tap before it cuts it in, and
        # until then the trunk belt stays under it (a blocked row's recorded tap
        # must not hold the bus proof).
        plan = build_plan(state, state["plan"])
        if not underground_proof(obs, plan):
            return None
        # A built tap that dives one lane under its branch puts that branch's
        # belt (another item, crossing) on the dived lane's trunk tile: that tile
        # is tap hardware now, not trunk (all-1113-2107: 4 of 12 buses waited
        # 10-15 minutes on the crossing belt of their own produce rows' taps).
        replaced = {_at(p) for tap in state.get("taps", []) if tap.get("built") for p in tap.get("remove", ())}
        # A tap past the old trunk end builds its own trunk extension, so a dived lane's crossing
        # tile was never a trunk belt to remove: its branch belt (sideways, another item) is tap
        # hardware too (aud-1114-0703 seeds 1520940511, 2247342255: copper on the iron lane's
        # crossing tile held both buses unproven for 15-25 minutes).
        facing = FACING[Bus.from_dict(state["bus"]).direction]
        replaced |= {
            _at(e["position"])
            for tap in state.get("taps", [])
            if tap.get("built") and tap.get("dived_lanes")
            for e in tap.get("entities", ())
            if e["name"] == "transport-belt" and e.get("direction") != facing
        }
        live = {(e["name"], *xy(e)): e for e in obs["entities"]}
        for wanted in plan["entities"]:
            found = live.get((wanted["name"], *xy(wanted)))
            if found is None and wanted["name"] == "transport-belt":
                # A tap splitter replaces one trunk belt. A consumer that cut its
                # tap in and then blocked left a splitter this state never
                # recorded; the lane still carries plates through it (seed
                # 2644599892: the bus waited 50 minutes on one "missing" belt).
                found = _splitter_over(live, wanted)
            if found is None or (wanted["name"] in BELTS and found.get("direction") != wanted.get("direction")):
                return None
        splitters = wanted_splitters(dict(state, taps=[t for t in state.get("taps", []) if t.get("built")]))
        if not all(priorities_match(live.get(("splitter", *xy(s))), wanted) for s, wanted in splitters):
            return None
        bus = Bus.from_dict(state["bus"])
        lanes = {lane.index: lane for lane in bus.lanes}
        evidence = []
        for trunk in state["trunks"]:
            item = trunk["item"]
            tiles = [
                _at(row["position"])
                for row in trunk_segments(bus, lanes[trunk["lane"]], proven_length(state, trunk, bus))
                if _at(row["position"]) not in replaced
            ]
            if not tiles:
                return None
            belts = [live[("transport-belt", *tile)] for tile in tiles if ("transport-belt", *tile) in live]
            contents = [e.get("belt_inventory") or {} for e in belts]
            if any(set(c) - {item} for c in contents):
                return None
            # The last trunk tile still a trunk belt (a tap may have replaced the tail tile).
            tail = live.get(("transport-belt", *tiles[-1]))
            if tail is None or (tail.get("belt_inventory") or {}).get(item, 0) < 1:
                return None
            evidence.append(
                {
                    "item": item,
                    "lane": trunk["lane"],
                    "tail": trunk["tail"],
                    "tail_items": tail["belt_inventory"][item],
                    "items_on_trunk": sum(c.get(item, 0) for c in contents),
                    "surface_belts": len(belts),
                }
            )
        return {
            "tick": obs["tick"],
            "direction": bus.direction,
            "lanes": evidence,
            "underground_pairs": len(plan.get("underground_pairs", [])),
            "splitters": [f["splitter"]["position"] for f in state["feeds"]],
            "splitter_priorities": [{"position": s["position"], **wanted} for s, wanted in splitters],
            "scope": "plates observed along every built lane up to its tail; no throughput or tap certification",
        }
