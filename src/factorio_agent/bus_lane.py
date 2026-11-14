"""Bus lane extension: a lane of an already built planned bus gets its trunk (capability ``bus_lane``).

Why (owner layout rule): blocks beside the bus take their belt inputs from bus lanes. The bus
band is closed to every other route (``transport.bus_band``), so a consumer on the far side of
the band from its item's producer can only be served by a lane of that item
(eval-1575138435: the plastic cells east of the bus had coal only west of it; every coal route
failed). ``planned_main_bus`` builds the plate lanes only; this capability builds any other
lane on a built bus, staged.

Rules:

1. Lane (:func:`choose_lane`): the first reserved lane of the item in the bus's lane table; with
   none, the first spare lane (material ``""``) is assigned to the item (a lane added inside the
   band). No reserved and no spare lane: blocked ``bus_lane_no_room_in_band`` (widening the band
   reservation is not implemented: the site plan's band is the bus's land).
2. Trunk: ``LANE_TILES`` straight belts from the lane head (``planned_bus.trunk_plan``): every
   belt on the lane's tiles, built or planned by another block, also the bus's own tap and feed
   hardware, is crossed underground (``transport.compile_crossings``); a line that would still
   meet planned hardware is refused (``board.surface_conflict``). Staged: the trunk is only as
   long as needed now; a consumer's tap extends it (``planned_tap.layout`` extension) to its row.
3. Feed: the item's source routed onto the lane head (a bus route): a splitter tap on the item's
   patch bank trunk first (``mine_trunk.plan_tap``, demand ``DEMAND_PER_MINUTE`` recorded in the
   trunk ledger so the bank grows for it), else a free board output port of the item
   (``production_sources.free_board_ports``). The route ends on the lane head, else at a header
   waypoint beside the band where a fixed head approach (:func:`head_approaches`) takes over:
   the bus's own plate feeds cross the rows behind the heads. A prepare stops after
   ``PLANNING_SECONDS``; a failed feed is a ``route`` need (``plan_needs``), never a bare plan.
4. Build order: the board block ``bus_lane:<item>`` (trunk + feed route) and the tap block are
   published first; trunk and route are built, then the patch trunk is cut over
   (``mine_trunk.cutover``: everything carried, one exact trunk belt mined, splitter built).
5. Publication: once the item reaches the trunk's tail, the trunk joins the bus state
   (``planned-main-bus.json`` ``trunks``, ``plan``, the lane's material) and the board's
   ``shared_main_bus`` is republished, so ``planned_tap`` gives consumers taps on it
   (``planned_tap.carries``) and the bus proof walks it like the plate lanes.
6. Trigger (:func:`bus_lane_nodes`): a blocked job whose needs name ``ACROSS_BAND`` for an item
   (``chemical_production``: coal for sites beside the bus) or ``OFF_BUS`` (``planned_furnaces``:
   stone/coal for steel and brick blocks whose private belts fail or run too long) while the bus
   has a reserved or a spare lane for it (observe receipt ``bus_lanes``, :func:`receipt`); once per item.
"""

from __future__ import annotations

import json
from pathlib import Path
import time

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json

from . import mine_trunk, planned_tap
from .board import blocks_without_bus, foreign_belts, surface_conflict
from .planned_bus import FACING, STATE_FILE as BUS_FILE, lane_tile, publication, trunk_plan
from .planning import preflight
from .production_sources import free_board_ports, route_tap_sites, tapped_board_ports
from .site.busframe import Bus
from .site.planmodel import Lane
from .smelting import merge_entities
from .transport import compile_crossings, plan_connection, xy

CAPABILITY = "bus_lane"
OWNER = "bus_lane"
NODE_PREFIX = "auto_bus_lane_"
NODE_PRIORITY = -60
LANE_TILES = 16  # initial trunk; consumers' taps extend it to their rows
DEMAND_PER_MINUTE = 60.0  # declared draw of a new lane on its source (one belt-fed consumer block)
SOURCES = 8  # feed sources routed per prepare, nearest the lane head first
ROUTE_TAP_SITES = 600  # splitter sites listed per tapped route (pure); only the nearest are checked and routed
ROUTE_TAPS_FILE = "route-taps.json"  # route taps cut by bus lanes: tile -> splitter (owners' proofs accept it)
ACROSS_BAND = "item_source_across_bus_band"
# A shared raw material (stone, coal) a furnace block beside the bus cannot get by a short private
# belt: it travels on the bus (planned_furnaces.bus_lane_needs).
OFF_BUS = "shared_material_not_on_bus"
TRIGGER_REASONS = frozenset({ACROSS_BAND, OFF_BUS})
OPEN = ("pending", "ready", "running", "blocked", "waiting", "submitted")


def state_file(item: str) -> str:
    return "bus-lane-%s.json" % item


def block_name(item: str) -> str:
    return "bus_lane:" + item


SIDES = ("left", "right")
LANES_TRIED = 3  # candidate lanes planned per prepare (each with its feed sources)


def validate_parameters(parameters) -> None:
    if (
        not isinstance(parameters, dict)
        or not set(parameters) <= {"item", "side"}
        or not isinstance(parameters.get("item"), str)
    ):
        raise ValueError("bus_lane takes an item and optionally the consumers' side")
    if not parameters["item"] or parameters["item"] in ("iron-plate", "copper-plate"):
        raise ValueError("bus_lane item must be a non-plate bus material (plate lanes are planned_main_bus)")
    if parameters.get("side") not in (None, *SIDES):
        raise ValueError("bus_lane side must be left or right")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def tappable(bus: Bus, lane, side: str | None, built_offsets=()) -> bool:
    """A tap of ``lane`` towards ``side`` fits ``planned_tap.layout``: the tile past its partner is no
    unbuilt lane (the branch turns on the partner tile and cannot tunnel at once; eval-1575138435:
    coal lane 4 between reserved lanes, every east tap ``tap_branch_cannot_tunnel_under_lanes``),
    and the partner tile is no built trunk (``tap_partner_tile_is_a_built_trunk``: the stone lane 5
    beside a built coal lane 4 has no west tap)."""
    if side is None:
        return True
    s = 1 if side == "right" else -1
    if lane.offset + s in set(built_offsets):
        return False
    lanes = {other.offset for other in bus.lanes} - set(built_offsets)
    return lane.offset + s not in lanes and lane.offset + 2 * s not in lanes


def choose_lanes(bus: Bus, trunks, item: str, side: str | None = None) -> list:
    """``[(lane, assigned)]`` best first (rule 1): the item's unbuilt reserved lanes, then spare lanes.

    With the consumers' ``side``: lanes a tap reaches that side from (:func:`tappable`) first, and
    among spare lanes the one nearest that bus edge (the shortest tap branch, nothing to tunnel).
    """
    built = {t["lane"] for t in trunks}
    offsets = {lane.offset for lane in bus.lanes if lane.index in built}
    reserved = [(lane, False) for lane in bus.lanes if lane.material == item and lane.index not in built]
    spare = [
        (Lane(lane.index, lane.group, lane.offset, item), True)
        for lane in bus.lanes
        if not lane.material and lane.index not in built
    ]
    if side is not None:
        s = 1 if side == "right" else -1
        spare.sort(key=lambda row: -s * row[0].offset)
    ranked = reserved + spare
    if side is not None:
        ranked = [r for r in ranked if tappable(bus, r[0], side, offsets)] + [
            r for r in ranked if not tappable(bus, r[0], side, offsets)
        ]
    return ranked[:LANES_TRIED]


def choose_lane(bus: Bus, trunks, item: str, side: str | None = None):
    """The best of :func:`choose_lanes`, or None."""
    found = choose_lanes(bus, trunks, item, side)
    return found[0] if found else None


def with_lane(bus: Bus, lane: Lane) -> Bus:
    """``bus`` with ``lane`` in its table (a spare lane given its material)."""
    lanes = tuple(lane if row.index == lane.index else row for row in bus.lanes)
    return Bus(bus.origin, bus.direction, bus.width, bus.length, lanes)


HEADER_ROW = -4  # bus-local row of the header corridor (district side of the head) the waypoints sit on
WAYPOINT_OFFSETS = (2, 6, 12)  # tiles outside the band edge
APPROACH_ROWS = (HEADER_ROW, -3, -5, -6)  # header rows a head approach may run along, best first
PLANNING_SECONDS = 120.0  # wall-clock budget of one prepare (checked before every feed route)
BUDGET_EXHAUSTED = "bus_lane_planning_budget_exhausted"


def header_waypoints(bus: Bus, row: int = HEADER_ROW) -> list[tuple[float, float, int, int]]:
    """``(x, y, facing, lateral)`` points beside the bus head, outside the band on either side, facing the head.

    The tiles right behind a head are the heads' approaches, so a new lane's feed comes along the
    header row from beside the band (eval-1575138435: the coal lane head took a feed from the west
    along that row; no corridor from the trunk tap direct).
    """
    from .site.busframe import compass_of, right_of

    rx, ry = right_of(bus.direction)
    points = []
    for d in WAYPOINT_OFFSETS:
        for side in (-1, 1):
            lateral = (-bus.half - d) if side < 0 else (bus.half - 1 + d)
            x, y = bus.local_point(row + 0.5, lateral + 0.5)
            facing = FACING[compass_of((rx * -side, ry * -side))]
            points.append((x, y, facing, lateral))
    return points


def head_approaches(bus: Bus, lane, item: str, trunk_entities, foreign, span: int) -> list[dict]:
    """Fixed head approaches of a new lane (pure), one per band side at most, best row first.

    The bus's own plate feeds run across the rows right behind the heads (br-steel-1114 seeds
    649402784, 3371980627: the copper feed along the row behind every other lane's head). A
    route into the head then needs an underground whose exit is the head tile, which a route
    never ends with (``compile_crossings`` keeps both mouths inside the route), and the head's
    port apron meets the foreign belt (``production_port_apron_belt_identity_changed``): every
    feed failed. An approach is planned here instead: from a header waypoint beside the band
    (:func:`header_waypoints`) along a header row to the lane's column, then along the lane over
    the head onto the trunk's second tile, the feed rows crossed underground (the exit mouth may
    be the head tile). The feed route then only has to reach the waypoint (``end``, outside the
    band). The trunk's first two tiles must be plain belts (they are replaced by the approach's).
    """
    facing = FACING[bus.direction]
    first = [lane_tile(bus, lane, a) for a in (0, 1)]
    plain = {
        (e["position"]["x"], e["position"]["y"])
        for e in trunk_entities
        if e["name"] == "transport-belt" and e.get("direction", 0) == facing
    }
    if any((p["x"], p["y"]) not in plain for p in first):
        return []

    def world(along, lateral):
        x, y = bus.local_point(along + 0.5, lateral + 0.5)
        return {"x": x, "y": y}

    found, sides = [], set()
    for row in APPROACH_ROWS:
        for x, y, inward, lateral in header_waypoints(bus, row):
            s = 1 if lateral > 0 else -1
            if s in sides:
                continue
            segments = [
                {"position": world(row, lat), "direction": inward if lat != lane.offset else facing}
                for lat in range(lateral - s, lane.offset - s, -s)
            ]
            segments += [{"position": world(a, lane.offset), "direction": facing} for a in range(row + 1, 2)]
            route = {"ok": True, "segments": segments, "metrics": {"length": len(segments), "turns": 1}}
            compiled = compile_crossings(route, foreign, maximum_span=span)
            if not compiled.get("ok"):
                continue
            sides.add(s)
            found.append(
                {
                    "end": {
                        "kind": "item",
                        "item": item,
                        "position": {"x": x, "y": y},
                        "facing": inward,
                        "direction": "input",
                    },
                    "entities": compiled["entities"],
                    "underground_pairs": list(compiled.get("underground_pairs", [])),
                    "replaces": [(p["x"], p["y"]) for p in first],
                    "side": "right" if s > 0 else "left",
                    "row": row,
                }
            )
        if len(sides) == 2:
            break
    return found


def receipt(root, obs) -> dict | None:
    """Observe receipt: items the bus carries, items with an unbuilt reserved lane, spare lanes left."""
    state = _read(Path(root) / BUS_FILE)
    if state.get("world_id") != obs.get("world_id") or not state.get("bus"):
        return None
    bus = Bus.from_dict(state["bus"])
    built = {t["lane"] for t in state.get("trunks", [])}
    return {
        "carried": sorted({t["item"] for t in state.get("trunks", [])}),
        "reserved": sorted({lane.material for lane in bus.lanes if lane.material and lane.index not in built}),
        "spare": sum(1 for lane in bus.lanes if not lane.material and lane.index not in built),
        "phase": state.get("phase"),
    }


def bus_lane_nodes(jobs, queue_nodes, observation, capabilities) -> list[dict]:
    """One ``bus_lane`` node per item a blocked job cannot get across the bus band (module rule 6)."""
    if CAPABILITY not in capabilities:
        return []
    lanes = observation.get("bus_lanes") if isinstance(observation, dict) else None
    if not isinstance(lanes, dict) or lanes.get("phase") not in ("commission", "verified"):
        return []
    jobs = list(jobs)
    nodes = []
    for job in jobs:
        needs = job.get("needs") if isinstance(job.get("needs"), dict) else {}
        item = needs.get("item")
        if job.get("status") != "blocked" or needs.get("reason") not in TRIGGER_REASONS or not isinstance(item, str):
            continue
        if item in lanes.get("carried", ()) or item in ("iron-plate", "copper-plate"):
            continue
        if item not in lanes.get("reserved", ()) and not lanes.get("spare"):
            continue

        def mine(row, item=item):
            return row.get("capability") == CAPABILITY and (row.get("parameters") or {}).get("item") == item

        if any(mine(n) and n.get("status", "pending") in OPEN for n in list(queue_nodes) + nodes):
            continue
        if any(mine(j) and j.get("status") != "succeeded" for j in jobs):
            continue
        node_id = NODE_PREFIX + item
        if any(n.get("id") == node_id for n in queue_nodes):
            continue
        parameters = {"item": item}
        if needs.get("side") in SIDES:
            parameters["side"] = needs["side"]
        nodes.append(
            {
                "id": node_id,
                "capability": CAPABILITY,
                "parameters": parameters,
                "priority": NODE_PRIORITY,
                "depends_on": [],
                "reason": f"{job.get('capability')} needs {item} across the bus band: a bus lane of it",
            }
        )
    return nodes


class BusLane:
    """Plan, build, publish and prove one lane of ``item`` on the built planned bus."""

    def __init__(self, game, builder, catalog, *, outposts, item, route=None, check=None, clock=None):
        self.game, self.builder, self.catalog, self.outposts, self.item = game, builder, catalog, outposts, item
        self.route = route or plan_connection
        self.check = check or preflight
        self.clock = clock or time.monotonic
        self.deadline = float("inf")
        self.root = Path(game.cfg.runtime_dir)
        self.path = self.root / state_file(item)
        self.state = _read(self.path)
        self.last_proof_failure = None

    def _save(self):
        _atomic_json(self.path, self.state)

    # ----- planning ------------------------------------------------------------------
    def _sources(self, obs, board, blocks, head) -> list:
        """``[(kind, port, tap)]``: patch-trunk taps of the item first, then free board ports (rule 3)."""
        rows = []
        mining = self.outposts.state if self.outposts.state.get("world_id") == obs["world_id"] else {}
        bank = mine_trunk.patch_bank(mining.get("banks"), self.item, mine_trunk.patch_seed(obs, self.item))
        name = "%s:%s" % (OWNER, self.item)
        if bank is not None:
            key = mine_trunk.trunk_key(bank)

            def accept(tap):
                if surface_conflict(self.builder, obs, blocks, tap["entities"]) is not None:
                    return False
                check = self.check(
                    self.game, self.builder, obs, tap["entities"], ignored_units=tuple(tap["remove_units"])
                )
                return bool(check.get("ok"))

            planned = mine_trunk.plan_tap(
                bank,
                obs,
                owner=OWNER,
                name=name,
                demand=DEMAND_PER_MINUTE,
                serve="after",
                stations=mine_trunk.stations(self.root, obs["world_id"], key, exclude=name),
                accept=accept,
            )
            for tap in planned.get("taps") or []:
                rows.append(("mine_trunk_tap", dict(tap["port"], name=name), tap))
        for row in free_board_ports(board, self.item, exclude=(OWNER,)):
            rows.append(("board_port", row["port"], None))
        # A splitter on a built route of the item that leaves a taken port (production_sources.route_tap_sites):
        # trunk continuation first, so the route's own consumer keeps its supply (serve "after").
        for row in tapped_board_ports(board, self.item, exclude=(OWNER,)):
            for tap in route_tap_sites(obs, row["port"], item=self.item, name=name, limit=ROUTE_TAP_SITES):
                tap["serve"] = "after"
                tap["splitter"].update(mine_trunk.tap_priorities(tap))
                tap["tapped_block"] = row["block"]
                tap["tapped_owner"] = (row.get("connection") or {}).get("owner")
                rows.append(("route_tap", dict(tap["port"], name=name), tap))

        def distance(row):  # nearest the lane head first: the shortest feed
            p = row[1]["position"]
            return abs(p["x"] - head["position"]["x"]) + abs(p["y"] - head["position"]["y"])

        chosen = []
        for row in sorted(rows, key=distance):
            if len(chosen) >= SOURCES:
                break
            tap = row[2]
            if row[0] == "route_tap":
                if surface_conflict(self.builder, obs, blocks, tap["entities"]) is not None:
                    continue
                check = self.check(
                    self.game, self.builder, obs, tap["entities"], ignored_units=tuple(tap["remove_units"])
                )
                if not check.get("ok"):
                    continue
            chosen.append(row)
        return chosen

    def _feed_route(self, obs, source, end, reserved, ignored) -> dict:
        """Source -> lane head or a head approach's waypoint (a bus route)."""
        return self.route(self.game, self.builder, obs, source, end, reserved, ignored_units=ignored, bus_route=True)

    def prepare(self, obs, side: str | None = None) -> dict:
        """Pure planning: lane, trunk, feed source and route, whole preflight (no ``act``)."""
        bus_state = _read(self.root / BUS_FILE)
        if bus_state.get("world_id") != obs["world_id"] or bus_state.get("phase") not in ("commission", "verified"):
            return {"ok": False, "reason": "bus_lane_needs_a_built_planned_bus"}
        if any(t.get("item") == self.item for t in bus_state.get("trunks", [])):
            return {"ok": False, "reason": "bus_lane_already_built", "item": self.item}
        frame = Bus.from_dict(bus_state["bus"])
        lanes = choose_lanes(frame, bus_state.get("trunks", []), self.item, side)
        if not lanes:
            return {
                "ok": False,
                "reason": "bus_lane_no_room_in_band",
                "item": self.item,
                "needs": "a reserved or spare lane in the bus band (widening the band is not implemented)",
            }
        board = _read(self.root / "pcb-board.json")
        if board.get("world_id") != obs["world_id"]:
            return {"ok": False, "reason": "bus_lane_board_world_changed"}
        span = planned_tap.max_span(self.game)
        if span is None:
            return {"ok": False, "reason": "bus_lane_underground_span_unknown"}
        blocks = blocks_without_bus(board)
        # Every belt on the lane's tiles is crossed: other blocks' (built or planned) and the bus's own
        # tap/feed hardware (observed belts include it; the planned but unbuilt taps are added here).
        unbuilt_taps = [
            e
            for tap in bus_state.get("taps", [])
            if not tap.get("built")
            for e in tap.get("entities", [])
            if e.get("name") in ("transport-belt", "underground-belt", "splitter")
        ]
        foreign = foreign_belts(obs, blocks) + unbuilt_taps
        # One prepare may not hold the controller for many minutes (br-steel-1114 seed 3371980627:
        # a 620 s bus_lane prepare left the controller unobserved; the fleet watcher stopped the run).
        self.deadline = self.clock() + PLANNING_SECONDS
        failures = []
        for lane, assigned in lanes:
            planned = self._plan_lane(obs, board, blocks, with_lane(frame, lane), lane, assigned, foreign, span)
            if planned.get("ok"):
                return planned
            failures.append(dict(planned, lane=lane.index))
            if planned.get("reason") == BUDGET_EXHAUSTED:
                break
        last = failures[-1]
        needs = next((f["plan_needs"] for f in failures if f.get("plan_needs")), None)
        return {
            "ok": False,
            "reason": last.get("reason", "bus_lane_feed_route_failed"),
            "item": self.item,
            "lanes_tried": [f["lane"] for f in failures],
            "route_failures": "; ".join(
                f"lane {f['lane']}: {f.get('route_failures') or f.get('reason')}" for f in failures
            )[:600],
            **({"plan_needs": needs} if needs else {}),
        }

    def _plan_lane(self, obs, board, blocks, bus, lane, assigned, foreign, span) -> dict:
        length = min(LANE_TILES, bus.length)
        trunk = trunk_plan(bus, lane, length, foreign, span)
        if not trunk.get("ok"):
            return {"ok": False, "reason": "bus_lane_trunk_blocked", "detail": trunk.get("reason")}
        clash = surface_conflict(self.builder, obs, blocks, trunk["entities"])
        if clash is not None:
            return {"ok": False, "reason": "bus_lane_trunk_meets_planned_hardware", "detail": clash}
        head = {
            "kind": "item",
            "item": self.item,
            "position": lane_tile(bus, lane, 0),
            "facing": FACING[bus.direction],
            "direction": "input",
        }
        sources = self._sources(obs, board, blocks, head)
        if not sources:
            return {"ok": False, "reason": "bus_lane_item_source_missing"}
        # Ends: the head itself first, then the fixed head approaches (:func:`head_approaches`).
        approaches = []
        for row in head_approaches(bus, lane, self.item, trunk["entities"], foreign, span):
            if surface_conflict(self.builder, obs, blocks, row["entities"]) is None:
                approaches.append(row)
        failures = []
        for approach in [None, *approaches]:
            end = head if approach is None else approach["end"]
            replaced = set(approach["replaces"]) if approach else set()
            lane_entities = [e for e in trunk["entities"] if xy(e) not in replaced]
            if approach is not None:
                lane_entities = merge_entities(lane_entities, approach["entities"])
            for kind, source, tap in sources:
                if self.clock() > self.deadline:
                    return self._feed_failure(failures, head, sources, reason=BUDGET_EXHAUSTED)
                extra = list(tap["entities"]) if tap else []
                ignored = tuple((tap or {}).get("remove_units", []))
                found = self._feed_route(obs, source, end, lane_entities + extra, ignored)
                if not found.get("ok"):
                    failures.append(
                        {
                            "source": kind,
                            "reason": found.get("reason"),
                            "at": source["position"],
                            "to": end["position"],
                            "approach": approach["side"] if approach else None,
                        }
                    )
                    continue
                failed = self._accept(obs, lane_entities, found, extra, ignored)
                if isinstance(failed, str):
                    failures.append({"source": kind, "reason": failed, "at": source["position"]})
                    continue
                entities, check = failed
                return self._planned(
                    obs,
                    bus,
                    lane,
                    assigned,
                    length,
                    trunk,
                    head,
                    entities,
                    extra,
                    found,
                    check,
                    kind,
                    source,
                    tap,
                    approach=approach,
                )
        return self._feed_failure(failures, head, sources)

    def _accept(self, obs, lane_entities, found, extra, ignored):
        """``(entities, check)`` of the lane with its feed route, or the reason it is refused."""
        try:
            entities = merge_entities(lane_entities, found["entities"])
            whole = merge_entities(entities, extra)
        except ValueError:
            return "bus_lane_route_conflicts_with_trunk"
        check = self.check(self.game, self.builder, obs, whole, ignored_units=ignored)
        if not check.get("ok"):
            return check.get("reason", "bus_lane_preflight_failed")
        return entities, check

    def _feed_failure(self, failures, head, sources, *, reason="bus_lane_feed_route_failed") -> dict:
        """The lane's feed failure with a route need (never a bare ``plan`` need): the nearest
        source to the head, and what would make the feed fit."""
        nearest = sources[0][1]["position"] if sources else None
        counts: dict = {}
        for f in failures:
            counts[str(f.get("reason"))] = counts.get(str(f.get("reason")), 0) + 1
        return {
            "ok": False,
            "reason": reason,
            "route_failures": "; ".join(f"{f['source']}: {f['reason']}" for f in failures)[:300],
            "plan_needs": {
                "kind": "route",
                "reason": reason,
                "item": self.item,
                "from": nearest,
                "to": head["position"],
                "tried": len(failures),
                "rejected_by": counts,
                "change": (
                    f"a clean {self.item} corridor from its source to the lane head or a header waypoint beside "
                    f"the band, or a {self.item} source nearer the bus head (a bank trunk tap or a free board port)"
                ),
            },
        }

    def _planned(
        self,
        obs,
        bus,
        lane,
        assigned,
        length,
        trunk,
        head,
        entities,
        extra,
        found,
        check,
        kind,
        source,
        tap,
        *,
        approach,
    ) -> dict:
        tapped = {(e["name"], *xy(e)) for e in extra}
        own = [e for e in entities if (e["name"], *xy(e)) not in tapped]
        pairs = list(trunk.get("underground_pairs", [])) + list(found.get("underground_pairs", []))
        trunk_entities = trunk["entities"]
        if approach is not None:
            # The approach's tiles on the lane join the bus plan only as plain trunk belts (an
            # underground exit on the head belongs to the feed, with its pair, in this plan).
            replaced = set(approach["replaces"])
            trunk_entities = [e for e in trunk_entities if xy(e) not in replaced] + [
                e for e in approach["entities"] if xy(e) in replaced and e["name"] == "transport-belt"
            ]
            pairs += approach["underground_pairs"]
        return {
            "ok": True,
            "world_id": obs["world_id"],
            "catalog_fingerprint": self.catalog.fingerprint,
            "item": self.item,
            "lane": lane.to_dict(),
            "assigned_spare_lane": assigned,
            "trunk": {
                "item": self.item,
                "lane": lane.index,
                "head": head["position"],
                "tail": lane_tile(bus, lane, length - 1),
                "length": length,
            },
            "trunk_entities": trunk_entities,
            "trunk_pairs": list(trunk.get("underground_pairs", [])),
            "plan": _plan(
                own,
                [head],
                underground_pairs=pairs,
                clear_obstacles=check.get("clear_obstacles", []),
                clearance_world_id=obs["world_id"],
                layout_policy="pcb-bus-lane-v1",
                owner=OWNER,
            ),
            "source": {"kind": kind, "port": source, "tap": tap},
            "connection": {
                "item": self.item,
                "source": source["position"],
                "destination": head["position"],
                "owner": OWNER,
            },
            "route_metrics": found.get("route_metrics"),
            "head_approach": {"side": approach["side"], "row": approach["row"]} if approach else None,
            "phase": "build",
        }

    # ----- execution -------------------------------------------------------------------
    def _publish(self, obs) -> dict | None:
        """Board block (trunk + feed route), its connection and the patch tap's block (idempotent)."""
        path = self.root / "pcb-board.json"
        board = _read(path)
        if board.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": "bus_lane_board_world_changed"}
        changed = False
        name = block_name(self.item)
        if board.setdefault("blocks", {}).get(name) != self.state["plan"]:
            board["blocks"][name] = self.state["plan"]
            changed = True
        if self.state["connection"] not in board.setdefault("connections", []):
            board["connections"].append(self.state["connection"])
            changed = True
        if changed:
            _atomic_json(path, board)
        tap = self.state["source"].get("tap")
        if tap:
            area = mine_trunk.tap_area([e for e in tap["entities"] if e["name"] != "splitter"] or tap["entities"])
            block = (name + ":tap", dict(mine_trunk.tap_plan(tap), reserved_area=area, owner=OWNER))
            if self.state["source"]["kind"] == "mine_trunk_tap":
                blocked = mine_trunk.record_tap(self.root, obs["world_id"], tap, block=block)
                if blocked is not None:
                    return blocked
            else:
                board = _read(path)
                if board["blocks"].get(block[0]) != block[1]:
                    board["blocks"][block[0]] = block[1]
                    _atomic_json(path, board)
        return None

    def _join_bus(self, obs) -> dict | None:
        """Rule 5: the built trunk enters the bus state and the board's shared bus (idempotent)."""
        path = self.root / BUS_FILE
        state = _read(path)
        if state.get("world_id") != obs["world_id"]:
            return {"status": "blocked", "reason": "bus_lane_bus_world_changed"}
        if any(t.get("item") == self.item for t in state.get("trunks", [])):
            return None
        lane = self.state["lane"]
        for row in state["bus"]["lanes"]:
            if row["index"] == lane["index"]:
                if row.get("material") and row["material"] != self.item:
                    return {"status": "blocked", "reason": "bus_lane_lane_taken", "lane": lane["index"]}
                row["material"] = self.item
        state["trunks"].append(dict(self.state["trunk"]))
        for key in ("plan", "staged_plan"):
            plan = state.get(key)
            if not isinstance(plan, dict):
                continue
            plan["entities"] = merge_entities(plan["entities"], self.state["trunk_entities"])
            plan["underground_pairs"] = list(plan.get("underground_pairs", [])) + list(self.state["trunk_pairs"])
            plan.setdefault("ports", [])
            plan["ports"] = list(plan["ports"]) + [
                p for p in _trunk_ports(state, self.state["trunk"]) if p not in plan["ports"]
            ]
        _atomic_json(path, state)
        board_path = self.root / "pcb-board.json"
        board = _read(board_path)
        if board.get("world_id") == obs["world_id"]:
            board["shared_main_bus"] = publication(state)
            _atomic_json(board_path, board)
        return None

    def step(self, obs, parameters=None) -> dict:
        if not self.state:
            prepared = self.prepare(obs, (parameters or {}).get("side"))
            if not prepared.get("ok"):
                return {"status": "blocked", **prepared}
            self.state = prepared
            self._save()
        if (
            self.state.get("world_id") != obs["world_id"]
            or self.state.get("catalog_fingerprint") != self.catalog.fingerprint
        ):
            return {"status": "blocked", "reason": "bus_lane_world_or_catalog_changed"}
        blocked = self._publish(obs)
        if blocked is not None:
            return blocked
        result = self.builder.ensure_plan(obs, self.state["plan"])
        if result.get("status") != "succeeded":
            return result
        tap = self.state["source"].get("tap")
        if tap and not tap.get("built"):
            # The replaced belt leaves its owners' plans first (republished), and a route tap is
            # recorded so the tapped block's proof accepts the splitter on that tile.
            mine_trunk.release_trunk_belts(self.root, obs["world_id"], tap)
            if self.state["source"]["kind"] == "route_tap":
                record_route_tap(self.root, obs["world_id"], tap)
                release_owner_plan(self.root, obs["world_id"], tap)
            action = mine_trunk.cutover(self.builder, obs, tap)
            if tap.get("built"):
                if self.state["source"]["kind"] == "mine_trunk_tap":
                    mine_trunk.record_tap(self.root, obs["world_id"], tap)
                self._save()
            if action is not None:
                return action
        evidence = self.proof(obs)
        if evidence is None:
            return {
                "status": "waiting",
                "reason": f"waiting for {self.item} at the bus lane's tail",
                "detail": self.last_proof_failure,
            }
        blocked = self._join_bus(obs)
        if blocked is not None:
            return blocked
        if self.state.get("phase") != "verified":
            self.state["phase"] = "verified"
            self._save()
        return {
            "status": "succeeded",
            "reason": f"a bus lane carries {self.item} from its source to its tail",
            "evidence": evidence,
        }

    def proof(self, obs, parameters=None) -> dict | None:
        """Trunk and feed route stand as planned, the tap stands, the item (only it) on the trunk's tail."""
        self.last_proof_failure = None
        if not self.state or self.state.get("world_id") != obs.get("world_id"):
            return None
        live = {(e["name"], *xy(e)): e for e in obs["entities"]}
        for spec in self.state["plan"]["entities"]:
            found = live.get((spec["name"], *xy(spec)))
            if found is None or (
                spec["name"] in ("transport-belt", "underground-belt", "splitter")
                and found.get("direction", 0) != spec.get("direction", 0)
            ):
                self.last_proof_failure = {
                    "reason": "bus_lane_entity_missing",
                    "entity": spec["name"],
                    "position": spec["position"],
                }
                return None
        tap = self.state["source"].get("tap")
        if tap and not mine_trunk.tap_standing(obs, tap):
            self.last_proof_failure = {"reason": "bus_lane_source_tap_not_standing"}
            return None
        tail = live.get(("transport-belt", *xy({"position": self.state["trunk"]["tail"]})))
        contents = (tail or {}).get("belt_inventory") or {}
        if not tail or contents.get(self.item, 0) < 1 or set(contents) - {self.item}:
            self.last_proof_failure = {"reason": "bus_lane_tail_without_item", "contents": contents}
            return None
        return {
            "tick": obs.get("tick"),
            "item": self.item,
            "lane": self.state["trunk"]["lane"],
            "tail": self.state["trunk"]["tail"],
            "tail_items": contents[self.item],
            "source": self.state["source"]["kind"],
            "scope": "the item observed on the new lane's tail; no throughput certification",
        }


def record_route_tap(root, world_id: str, tap: dict) -> None:
    """Record a route tap (before its belt is mined): the tile it replaces and its splitter (idempotent)."""
    path = Path(root) / ROUTE_TAPS_FILE
    state = _read(path)
    if state.get("world_id") != world_id:
        state = {"world_id": world_id, "taps": {}}
    row = {
        "splitter": {k: tap["splitter"][k] for k in ("name", "position", "direction")},
        "remove": list(tap["remove"]),
        "tapped_block": tap.get("tapped_block"),
    }
    if state["taps"].get(tap["name"]) != row:
        state["taps"][tap["name"]] = row
        _atomic_json(path, state)


def release_owner_plan(root, world_id: str, tap: dict) -> bool:
    """Drop the belt a route tap replaces from the tapped route's owner state plan (idempotent).

    ``mine_trunk.release_trunk_belts`` covers the district, the mining banks and every board copy;
    a remote smelting block keeps its own saved plan (``remote-smelting-<ore>.json``) and republishes
    it, so that copy is released here too. True when a plan changed.
    """
    from .remote_smelting import OWNER_PREFIX, state_file as remote_state_file

    owner = str(tap.get("tapped_owner") or "")
    if not owner.startswith(OWNER_PREFIX):
        return False
    path = Path(root) / remote_state_file(owner[len(OWNER_PREFIX) :])
    state = _read(path)
    if state.get("world_id") != world_id or not isinstance(state.get("plan"), dict):
        return False
    wanted = {(p["x"], p["y"]) for p in tap["remove"]}
    entities = state["plan"].get("entities", [])
    kept = [e for e in entities if not (e["name"] == "transport-belt" and xy(e) in wanted)]
    if len(kept) == len(entities):
        return False
    state["plan"] = dict(state["plan"], entities=kept)
    state["plan"].setdefault("released_to_route_taps", []).append(tap["name"])
    _atomic_json(path, state)
    return True


def route_tap_tiles(root, world_id: str) -> dict:
    """Tile -> splitter of every recorded route tap (:func:`record_route_tap`) of this world."""
    state = _read(Path(root) / ROUTE_TAPS_FILE)
    if state.get("world_id") != world_id:
        return {}
    return {(p["x"], p["y"]): row["splitter"] for row in state.get("taps", {}).values() for p in row.get("remove", [])}


def tapped_belt_stands(actual: dict, tiles: dict, wanted: dict) -> bool:
    """``wanted`` is a plan belt a recorded route tap replaced, and that tap's splitter stands."""
    if wanted.get("name") != "transport-belt":
        return False
    splitter = tiles.get(xy(wanted))
    if splitter is None:
        return False
    live = actual.get(("splitter", *xy(splitter)))
    return live is not None and live.get("direction", 0) == splitter.get("direction", 0)


def _trunk_ports(bus_state: dict, trunk: dict) -> list[dict]:
    from .planned_bus import trunk_ports

    return trunk_ports(Bus.from_dict(bus_state["bus"]), [trunk])
