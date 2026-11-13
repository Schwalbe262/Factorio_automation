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

so red keeps the port, green keeps its anchor belt at (x-1, y) and the bus
gets an ordinary splitter share (a backed-up bus sends everything to science).
The branch is turned with the district's ``site_transform``, so rotated or
mirrored districts need nothing special. The feed is routed to the lane head
with ``transport.plan_connection`` guided by the reserved
``corridor:smelting->bus``.

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
from .core.deterministic_builder import _plan
from .core.deterministic_state import _atomic_json
from .planning import preflight
from .plate_supply import district_ports
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


def feed_branch(district_plan: dict, port: dict) -> dict:
    """Splitter, branch belts, feed source and anchor for one district plate port (see module doc)."""
    turn = Transform.from_dict(district_plan.get("site_transform"))
    canonical = apply_transform(turn.inverse(), port)
    if canonical.get("facing") != 4:
        raise ValueError("district plate port does not leave east in the district frame")
    x, y = canonical["position"]["x"], canonical["position"]["y"]
    shape = {
        "anchor": {"x": x - 3, "y": y},
        "splitter": dict(entity("splitter", x - 3, y + 0.5, 4), _width=2, _height=1),
        "belts": [entity("transport-belt", x + dx, y + 1, 4) for dx in (-2, -1, 0, 1, 2)],
        "source": {
            "kind": "item",
            "item": port["item"],
            "position": {"x": x + 2, "y": y + 1},
            "facing": 4,
            "direction": "output",
        },
    }
    return dict(apply_transform(turn, shape), item=port["item"], district_port=dict(port["position"]))


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


def effective_plan(state: dict) -> dict:
    """The bus plan with every recorded tap applied: replaced trunk belts out, tap hardware and ports in."""
    plan = state["plan"]
    taps = state.get("taps", [])
    if not taps:
        return plan
    removed = {_at(p) for tap in taps for p in tap["remove"]}
    kept = [e for e in plan["entities"] if not (e["name"] == "transport-belt" and xy(e) in removed)]
    entities = merge_entities(kept, *(tap["entities"] for tap in taps))
    pairs = plan.get("underground_pairs", []) + [p for tap in taps for p in tap["underground_pairs"]]
    ports = trunk_ports(Bus.from_dict(state["bus"]), state["trunks"]) + [tap["port"] for tap in taps]
    extra = {k: v for k, v in plan.items() if k not in ("ok", "reason", "entities", "ports", "required_items")}
    extra["underground_pairs"] = pairs
    return _plan(entities, ports, **extra)


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
        feeds, anchors = [], []
        for item in BUILT_MATERIALS:
            try:
                feed = feed_branch(district["plan"], ports[item])
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
                return {"ok": False, "reason": "planned_bus_feed_anchor_missing", "item": item}
            feed["anchor_unit"] = anchor["unit_number"]
            anchors.append(anchor["unit_number"])
            feeds.append(feed)
            entities = merge_entities(entities, [feed["splitter"], *feed["belts"]])
        routed = self._route_feeds(obs, bus, lanes, feeds, entities, corridor_box(master.corridor(HEADER_CORRIDOR)))
        if not routed["ok"]:
            return routed
        entities = routed["entities"]
        pairs += routed["pairs"]
        whole = self.check(self.game, self.builder, obs, entities, ignored_units=anchors)
        if not whole.get("ok"):
            return {"ok": False, "reason": "planned_bus_whole_preflight_failed", "detail": whole}
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
        for order in (feeds, feeds[::-1]):
            es, pairs, metrics, good = list(entities), [], [], True
            for feed in order:
                # The feed ends on the lane head inside the bus band: a bus route.
                extra = {"bus_route": True} if guide is None else {"guide": guide, "bus_route": True}
                route = self.route(self.game, self.builder, obs, feed["source"], heads[feed["item"]], es, **extra)
                if not route.get("ok"):
                    failures.append({"item": feed["item"], "reason": route.get("reason")})
                    good = False
                    break
                try:
                    es = merge_entities(es, route["entities"])
                except ValueError:
                    failures.append({"item": feed["item"], "reason": "feed_route_conflicts_with_bus"})
                    good = False
                    break
                pairs += route.get("underground_pairs", [])
                metrics.append({"item": feed["item"], **(route.get("route_metrics") or {})})
            if good:
                return {"ok": True, "entities": es, "pairs": pairs, "metrics": metrics}
        return {"ok": False, "reason": "planned_bus_feed_route_failed", "failures": failures[-4:]}

    # ----- execution ---------------------------------------------------------------
    def _save(self):
        _atomic_json(self.path, self.state)

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
            result = self.builder.ensure_plan(obs, self.state["staged_plan"])
            if result.get("status") != "succeeded":
                return result
            self.state["phase"] = "splice"
            self._save()
        if self.state["phase"] == "splice":
            for action in (self._splice_district(obs), self._cut_over(obs)):
                if action is not None:
                    return action
            result = self.builder.ensure_plan(obs, self.state["plan"])
            if result.get("status") != "succeeded":
                return result
            self.state["phase"] = "commission"
            self._save()
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
        plan = effective_plan(state)
        if not underground_proof(obs, plan):
            return None
        live = {(e["name"], *xy(e)): e for e in obs["entities"]}
        for wanted in plan["entities"]:
            found = live.get((wanted["name"], *xy(wanted)))
            if found is None or (wanted["name"] in BELTS and found.get("direction") != wanted.get("direction")):
                return None
        bus = Bus.from_dict(state["bus"])
        lanes = {lane.index: lane for lane in bus.lanes}
        evidence = []
        for trunk in state["trunks"]:
            item = trunk["item"]
            belts = [
                live[("transport-belt", *_at(row["position"]))]
                for row in trunk_segments(bus, lanes[trunk["lane"]], trunk["length"])
                if ("transport-belt", *_at(row["position"])) in live
            ]
            contents = [e.get("belt_inventory") or {} for e in belts]
            if any(set(c) - {item} for c in contents):
                return None
            tail = live.get(("transport-belt", *_at(trunk["tail"])))
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
            "scope": "plates observed along every built lane up to its tail; no throughput or tap certification",
        }
