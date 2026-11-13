"""Taps on the planned main bus (planned_tap.py), checked offline in every bus direction."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

from factorio_agent import planned_bus as pb
from factorio_agent import planned_tap as pt
from factorio_agent import production_site
from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.routing import VECTORS
from factorio_agent.site.busframe import Bus, lane_table
from factorio_agent.site.geometry import Rect
from factorio_agent.transport import plan_connection, xy

ORDER = ("iron-plate", "iron-plate", "copper-plate", "copper-plate", "coal", "stone")
GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)
SPAN = 5


def bus_state(direction, *, length=200, trunk=64):
    """A built planned-bus state: iron and copper trunks of ``trunk`` tiles."""
    bus = Bus((10, -20), direction, 28, length, lane_table(28, ORDER, 4, 2))
    entities, trunks = [], []
    for lane in pb.built_lanes(bus):
        built = pb.trunk_plan(bus, lane, trunk, [], SPAN)
        entities += built["entities"]
        tail = pb.lane_tile(bus, lane, trunk - 1)
        trunks.append(
            {
                "item": lane.material,
                "lane": lane.index,
                "head": pb.lane_tile(bus, lane, 0),
                "tail": tail,
                "length": trunk,
            }
        )
    plan = _plan(entities, pb.trunk_ports(bus, trunks))
    return {"world_id": "w", "bus": bus.to_dict(), "trunks": trunks, "feeds": [], "plan": plan, "phase": "verified"}


# Factorio: a NORTH splitter's left is west; turning the splitter turns its sides with it.
RIGHT_HAND = {0: (1, 0), 4: (0, 1), 8: (-1, 0), 12: (0, -1)}


def native_side(splitter, point):
    """Independent oracle: the native splitter side ``point`` lies on."""
    rx, ry = RIGHT_HAND[splitter["direction"]]
    p = splitter["position"]
    lateral = (point["x"] - p["x"]) * rx + (point["y"] - p["y"]) * ry
    return "right" if lateral > 0 else "left"


def published(direction, **kwargs):
    return pb.publication(bus_state(direction, **kwargs))


def local(view, position):
    return view.bus.local_tile(int(position["x"] - 0.5), int(position["y"] - 0.5))


def follow(entities, pairs, start, goal, limit=80):
    """Walk belts from ``start`` along their directions (undergrounds jump to their pair) to ``goal``."""
    by_tile = {}
    for e in entities:
        for tile in GEOMETRY._occupied_by_plan([e]):
            by_tile[tile] = e
    jumps = {tuple(p["input"]): tuple(p["output"]) for p in pairs}
    tile, seen = start, []
    for _ in range(limit):
        if tile == goal:
            return seen + [tile]
        e = by_tile.get(tile)
        if e is None or e["name"] == "splitter":
            return None
        seen.append(tile)
        if e["name"] == "underground-belt" and e.get("belt_to_ground_type") == "input":
            tile = jumps[tile]
            continue
        dx, dy = VECTORS[e["direction"]]
        tile = (tile[0] + dx, tile[1] + dy)
    return None


class LayoutTests(unittest.TestCase):
    def check(self, direction, item, side, along=20):
        bus = published(direction)
        view = pt.BusView(bus)
        tap = pt.layout(view, item, side, along, span=SPAN)
        self.assertTrue(tap["ok"], (direction, item, side, tap))
        lane = view.lanes[tap["lane"]]
        s = 1 if side == "right" else -1
        # The port is the second tile outside the band on the consumer's side, on the branch row.
        self.assertEqual(
            local(view, tap["port"]["position"]), (along + 2, (view.bus.half if s > 0 else -view.bus.half - 1) + s)
        )
        self.assertFalse(
            view.bus.rect().contains(
                *map(int, (tap["port"]["position"]["x"] - 0.5, tap["port"]["position"]["y"] - 0.5))
            )
        )
        # No tap tiles overlap, and tap hardware lands on built trunk tiles only where a belt is replaced.
        boxes = [GEOMETRY._occupied_by_plan([e]) for e in tap["entities"]]
        self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))
        trunk_tiles = {xy(e) for e in bus["entities"]}
        removed = {xy({"position": p}) for p in tap["remove"]}
        self.assertEqual(set().union(*boxes) & trunk_tiles, removed)
        # Reserved and spare lanes on the branch row are tunnelled (except the splitter partner).
        partner = lane.offset + s
        dived = {view.lanes[i].offset for i in tap["dived_lanes"]}
        used = {local(view, {"x": x, "y": y}) for x, y in set().union(*boxes)}
        for other in view.bus.lanes:
            if other.offset in (lane.offset, partner) or other.offset in dived:
                continue
            between = min(partner, s * 99) <= other.offset <= max(partner, s * 99)
            if between:
                self.assertNotIn((along + 2, other.offset), used, (direction, item, side, other))
        # Plates reach the port from the splitter's branch output.
        out = view.world(along + 1, partner)
        path = follow(tap["entities"], tap["underground_pairs"], xy({"position": out}), xy(tap["port"]))
        self.assertIsNotNone(path, (direction, item, side))
        # A dived trunk still connects: input at a+1 pairs with output at a+3.
        for index in tap["dived_lanes"]:
            first = xy({"position": view.world(along + 1, view.lanes[index].offset)})
            self.assertIn(first, {tuple(p["input"]) for p in tap["underground_pairs"]})
        return view, tap

    def test_every_direction_side_and_plate(self):
        for direction in ("N", "E", "S", "W"):
            for item in ("iron-plate", "copper-plate"):
                for side in ("left", "right"):
                    with self.subTest(direction=direction, item=item, side=side):
                        self.check(direction, item, side)

    def test_tap_splitter_output_priority_is_the_branch_in_every_direction_and_side(self):
        for direction in ("N", "E", "S", "W"):
            for item in ("iron-plate", "copper-plate"):
                for side in ("left", "right"):
                    with self.subTest(direction=direction, item=item, side=side):
                        view, tap = self.check(direction, item, side)
                        splitter = tap["splitter"]
                        lane = view.lanes[tap["lane"]]
                        s = 1 if side == "right" else -1
                        branch = view.world(tap["along"] + 1, lane.offset + s)  # partner output tile
                        trunk = view.world(tap["along"] + 1, lane.offset)  # the bus continues here
                        self.assertEqual(splitter["splitter_output_priority"], native_side(splitter, branch))
                        self.assertNotEqual(native_side(splitter, trunk), native_side(splitter, branch))
                        self.assertEqual(splitter["splitter_input_priority"], "none")
                        self.assertEqual(pb.tap_priorities(tap), {k: splitter[k] for k in pb.PRIORITY_KEYS})
                        spec = next(e for e in tap["entities"] if e["name"] == "splitter")
                        self.assertEqual(spec["splitter_output_priority"], splitter["splitter_output_priority"])

    def test_inner_lanes_dive_outer_built_trunks_and_tunnel_reserved_lanes(self):
        # Lane table: iron -14 (built), iron -13, copper -12 (built), copper -11, gaps, groups ...
        _, copper_left = self.check("N", "copper-plate", "left")
        self.assertEqual(copper_left["dived_lanes"], [0])  # the iron trunk dives under the branch
        _, iron_right = self.check("N", "iron-plate", "right")
        self.assertEqual(iron_right["dived_lanes"], [2])  # the copper trunk dives
        branch = [p for p in iron_right["underground_pairs"] if p["direction"] == 4]
        self.assertGreaterEqual(len(branch), 4)  # reserved copper lane, then four lane groups
        self.assertTrue(all(p["span"] <= SPAN for p in branch))
        _, iron_left = self.check("N", "iron-plate", "left")
        self.assertEqual((iron_left["dived_lanes"], iron_left["underground_pairs"]), ([], []))

    def test_windows_keep_apart_and_partner_may_not_be_a_built_trunk(self):
        bus = published("E")
        view = pt.BusView(bus)
        first = pt.layout(view, "iron-plate", "left", 20, span=SPAN)
        view.add(dict(first, name="a:iron-plate", owner="a", demand_per_minute=10, built=False))
        self.assertFalse(view.window_free(23))
        self.assertTrue(view.window_free(25))
        found, rejected = pt.candidates(view, "copper-plate", "left", 22, 18, 26, span=SPAN)
        self.assertTrue(all(abs(row["along"] - 20) >= 5 for row in found))
        self.assertIn("tap_window_taken", rejected)
        self.assertEqual(pt.layout(view, "iron-plate", "right", 1, span=SPAN)["reason"], "tap_row_too_close_to_head")

    def test_short_span_cannot_tunnel_a_lane_group(self):
        view = pt.BusView(published("S"))
        self.assertEqual(
            pt.layout(view, "copper-plate", "right", 20, span=4)["reason"], "tap_branch_cannot_tunnel_under_lanes"
        )

    def test_tap_past_the_trunk_extends_it_and_past_the_bus_is_a_blocker(self):
        view = pt.BusView(published("W", length=120))
        tap = pt.layout(view, "copper-plate", "left", 80, span=SPAN)
        self.assertTrue(tap["ok"], tap)
        target = 80 + pt.WINDOW_ROWS + pt.EXTEND_MARGIN
        self.assertEqual(tap["extension"], {2: target, 0: target})
        self.assertEqual(tap["remove"], [])  # nothing built there yet
        lane = view.lanes[2]
        belts = {local(view, e["position"]) for e in tap["entities"] if e["name"] == "transport-belt"}
        self.assertIn((64, lane.offset), belts)
        self.assertIn((target - 1, lane.offset), belts)
        self.assertNotIn((80, lane.offset), belts)  # the splitter row
        late = pt.layout(view, "iron-plate", "left", 115, span=SPAN)
        self.assertEqual(late["reason"], "tap_beyond_bus_length")


class FeedLayoutTests(unittest.TestCase):
    """A feed: plates from a block away from the district enter a built trunk (mirrored tap)."""

    def test_every_direction_side_and_plate_reaches_the_trunk_downstream(self):
        for direction in ("N", "E", "S", "W"):
            for item in ("iron-plate", "copper-plate"):
                for side in ("left", "right"):
                    with self.subTest(direction=direction, item=item, side=side):
                        bus = published(direction)
                        view = pt.BusView(bus)
                        feed = pt.feed_layout(view, item, side, 20, span=SPAN)
                        self.assertTrue(feed["ok"], feed)
                        lane = view.lanes[feed["lane"]]
                        s = 1 if side == "right" else -1
                        edge = view.bus.half if s > 0 else -view.bus.half - 1
                        self.assertEqual(local(view, feed["port"]["position"]), (21, edge + s))
                        self.assertEqual(feed["port"]["direction"], "input")
                        boxes = [GEOMETRY._occupied_by_plan([e]) for e in feed["entities"]]
                        self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))
                        trunk_tiles = {xy(e) for e in bus["entities"]}
                        removed = {xy({"position": p}) for p in feed["remove"]}
                        self.assertEqual(set().union(*boxes) & trunk_tiles, removed)
                        # From the port the plates walk to the splitter's partner input tile.
                        path = follow(
                            feed["entities"],
                            feed["underground_pairs"],
                            xy(feed["port"]),
                            xy({"position": view.world(22, lane.offset + s)}),
                        )
                        self.assertIsNotNone(path)
                        splitter = feed["splitter"]
                        self.assertEqual(splitter["position"], view.world(23, min(lane.offset, lane.offset + s) + 0.5))
                        trunk = view.world(24, lane.offset)
                        self.assertEqual(splitter["splitter_output_priority"], native_side(splitter, trunk))
                        self.assertEqual(splitter["splitter_input_priority"], "none")
                        self.assertEqual(pb.tap_priorities(feed), {k: splitter[k] for k in pb.PRIORITY_KEYS})
                        for index in feed["dived_lanes"]:
                            first = xy({"position": view.world(20, view.lanes[index].offset)})
                            last = xy({"position": view.world(22, view.lanes[index].offset)})
                            self.assertIn(
                                (first, last),
                                {(tuple(p["input"]), tuple(p["output"])) for p in feed["underground_pairs"]},
                            )
                        self.assertEqual(
                            pt.station(dict(feed, name="f", owner="o", supply_per_minute=75))["kind"], "planned_feed"
                        )

    def test_feed_windows_keep_apart_from_taps_and_need_the_trunk_past_the_splitter(self):
        view = pt.BusView(published("N"))
        tap = pt.layout(view, "iron-plate", "right", 10, span=SPAN)
        view.add(dict(tap, name="a:iron-plate", owner="a", demand_per_minute=10, built=False))
        found, rejected = pt.feed_candidates(view, "iron-plate", "right", span=SPAN, limit=6)
        # Nearest the head first; rows whose copper dive lies within a span of the tap's dive are refused.
        self.assertEqual([row["along"] for row in found], [2, 3, 19, 20, 21, 22])
        self.assertIn("feed_window_taken", rejected)
        self.assertIn("feed_dive_near_trunk_underground", rejected)
        self.assertEqual(
            pt.feed_layout(view, "iron-plate", "right", 61, span=SPAN)["reason"], "feed_beyond_built_trunk"
        )
        self.assertEqual(
            pt.feed_layout(view, "iron-plate", "right", 1, span=SPAN)["reason"], "feed_row_too_close_to_head"
        )


class Harness:
    """A built planned bus in a runtime dir with observed trunk belts."""

    def __init__(self, root, direction="N"):
        self.root = Path(root)
        self.state = bus_state(direction)
        units = iter(range(500, 100000))
        self.entities = [dict(e, unit_number=next(units)) for e in self.state["plan"]["entities"]]
        (self.root / pb.STATE_FILE).write_text(json.dumps(self.state), encoding="utf-8")
        board = {"world_id": "w", "blocks": {}, "connections": [], "shared_main_bus": pb.publication(self.state)}
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        self.builder = Mock()
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.builder.bootstrap.ensure_item.return_value = {"type": "craft", "item": "splitter", "count": 1}

    def board(self):
        return json.loads((self.root / "pcb-board.json").read_text(encoding="utf-8"))

    def obs(self, entities=None, inventory=None):
        return {"world_id": "w", "tick": 10, "entities": entities or self.entities, "inventory": inventory or {}}


def allocation(side, along0, along1):
    return SimpleNamespace(side=side, along0=along0, along1=along1)


class ConsumerTests(unittest.TestCase):
    @staticmethod
    def route(game, builder, obs, source, destination, reserved, **kwargs):
        ends = [
            dict(name="transport-belt", position=p["position"], direction=p["facing"]) for p in (source, destination)
        ]
        return {"ok": True, "entities": ends, "underground_pairs": [], "route_metrics": {}, "clear_obstacles": []}

    def connect(self, h, side="left", owner="automation_science", ports=(("iron-plate", 20), ("copper-plate", 30))):
        view = pt.BusView(h.board()["shared_main_bus"])
        inputs = [
            {
                "kind": "item",
                "item": item,
                "position": view.world(along, -view.bus.half - 6 if side == "left" else view.bus.half + 5),
                "facing": 0,
                "direction": "input",
            }
            for item, along in ports
        ]
        return pt.connect_inputs(
            None,
            h.builder,
            h.obs(),
            h.board()["shared_main_bus"],
            owner=owner,
            allocation=allocation(side, 10, 40),
            inputs=inputs,
            entities=[],
            demand={"iron-plate": 60, "copper-plate": 30},
            span=SPAN,
            route=self.route,
        )

    def test_two_ports_of_one_item_get_two_named_taps_in_the_bus_state(self):
        # fixpace1 seed 3794581402: the belt row's two iron taps shared one name, the bus
        # state kept one of them and rebuilt the trunk belt under the other splitter.
        with TemporaryDirectory() as root:
            h = Harness(root)
            result = self.connect(h, ports=(("iron-plate", 14), ("iron-plate", 30)))
            self.assertTrue(result["ok"], result)
            names = [t["name"] for t in result["taps"]]
            self.assertEqual(names, ["automation_science:iron-plate", "automation_science:iron-plate#2"])
            self.assertIsNone(pt.publish(h.root, "w", result["taps"]))
            state = json.loads((h.root / pb.STATE_FILE).read_text(encoding="utf-8"))
            self.assertEqual(sorted(t["name"] for t in state["taps"]), sorted(names))

    def test_connect_publish_cut_over_and_mark_built(self):
        with TemporaryDirectory() as root:
            h = Harness(root)
            result = self.connect(h)
            self.assertTrue(result["ok"], result)
            taps = result["taps"]
            self.assertEqual(
                [t["name"] for t in taps], ["automation_science:iron-plate", "automation_science:copper-plate"]
            )
            self.assertGreaterEqual(abs(taps[0]["along"] - taps[1]["along"]), pt.WINDOW_ROWS + pt.WINDOW_GAP)
            self.assertEqual(sorted(result["ignored_units"]), sorted(u for t in taps for u in t["remove_units"]))
            # Publication claims both taps on the board and in the bus state.
            self.assertIsNone(pt.publish(h.root, "w", taps))
            board = h.board()["shared_main_bus"]
            self.assertEqual([s["name"] for s in board["stations"]], [t["name"] for t in taps])
            self.assertTrue(all(t["port"] in board["ports"] for t in taps))
            self.assertEqual(pt.claimed(board, "iron-plate", owner="logistic_science"), 60)
            self.assertEqual(pt.claimed(board, "iron-plate", owner="automation_science"), 0)
            # Cut-over: stock first, then the exact trunk belts are mined, then the taps are built.
            self.assertEqual(pt.cutover(h.root, h.builder, h.obs(), taps)["type"], "craft")
            stock = {"splitter": 2, "transport-belt": 20, "underground-belt": 20}
            action = pt.cutover(h.root, h.builder, h.obs(inventory=stock), taps)
            self.assertEqual(action["type"], "mine")
            self.assertIn(action["expected_entity_unit"], result["ignored_units"])
            removed = {xy({"position": p}) for t in taps for p in t["remove"]}
            rest = [e for e in h.entities if xy(e) not in removed]
            self.assertIsNone(pt.cutover(h.root, h.builder, h.obs(rest, stock), taps))
            state = json.loads((h.root / pb.STATE_FILE).read_text(encoding="utf-8"))
            self.assertTrue(all(t["built"] for t in state["taps"]))
            self.assertTrue(all(s["built"] for s in h.board()["shared_main_bus"]["stations"]))
            # The bus's own plan now carries the taps: replaced belts out, splitters in.
            plan = pb.effective_plan(state)
            names = {xy(e): e["name"] for e in plan["entities"]}
            for tap in taps:
                self.assertEqual(names[xy(tap["splitter"])], "splitter")
            # A second consumer may not take the same rows.
            again = self.connect(h, owner="logistic_science")
            self.assertTrue(again["ok"], again)
            mine = {t["along"] for t in taps}
            self.assertTrue(all(abs(t["along"] - a) >= 5 for t in again["taps"] for a in mine))

    def test_cutover_configures_each_built_tap_splitter_once(self):
        with TemporaryDirectory() as root:
            h = Harness(root, direction="W")
            taps = self.connect(h, side="right")["taps"]
            pt.publish(h.root, "w", taps)
            removed = {xy({"position": p}) for t in taps for p in t["remove"]}
            units = iter(range(9000, 10000))
            hardware = [dict(e, unit_number=next(units)) for t in taps for e in t["entities"]]
            for e in hardware:
                if e["name"] == "splitter":
                    e.update(splitter_input_priority="none", splitter_output_priority="none")
            live = [e for e in h.entities if xy(e) not in removed] + hardware
            # Hardware stands: the taps count as built, but cutover is not done before the priorities.
            action = pt.cutover(h.root, h.builder, h.obs(live), taps)
            self.assertEqual(action["type"], "configure_splitter")
            first = next(e for e in hardware if e["name"] == "splitter")
            self.assertEqual(action["expected_splitter_unit"], first["unit_number"])
            self.assertEqual(action["output_priority"], pb.tap_priorities(taps[0])["splitter_output_priority"])
            self.assertTrue(all(t["built"] for t in taps))
            # The bus proof wants the same priorities for every recorded tap.
            state = json.loads((h.root / pb.STATE_FILE).read_text(encoding="utf-8"))
            wanted = {xy(s): w for s, w in pb.wanted_splitters(state)}
            self.assertEqual(wanted[xy(taps[0]["splitter"])], pb.tap_priorities(taps[0]))
            for e in hardware:
                if e["name"] == "splitter":
                    e.update(wanted[xy(e)])
            self.assertIsNone(pt.cutover(h.root, h.builder, h.obs(live), taps))
            # An older tap record without priorities on its spec is configured the same way.
            for tap in taps:
                for key in pb.PRIORITY_KEYS:
                    tap["splitter"].pop(key)
            self.assertIsNone(pt.cutover(h.root, h.builder, h.obs(live), taps))
            first.update(splitter_output_priority="none")
            self.assertEqual(pt.cutover(h.root, h.builder, h.obs(live), taps)["type"], "configure_splitter")

    def test_the_taps_own_belt_on_a_removed_trunk_tile_is_not_a_changed_trunk(self):
        # pace1 seed 2692229611: the copper tap's branch belt crosses the iron lane on a
        # removed tile; once placed, the next pass blocked planned_bus_tap_trunk_identity_changed.
        with TemporaryDirectory() as root:
            h = Harness(root)
            taps = self.connect(h)["taps"]
            tap = taps[-1]
            tile = tap["remove"][0]
            trunk = next(e for e in h.entities if xy(e) == xy({"position": tile}))
            crossing = {"name": "transport-belt", "position": dict(tile), "direction": (trunk["direction"] + 4) % 16}
            tap["entities"] = [e for e in tap["entities"] if xy(e) != xy(crossing)] + [crossing]
            pt.publish(h.root, "w", taps)
            removed = {xy({"position": p}) for t in taps for p in t["remove"]}
            placed = dict(crossing, unit_number=99999)
            live = [e for e in h.entities if xy(e) not in removed] + [placed]
            stock = {"splitter": 2, "transport-belt": 20, "underground-belt": 20}
            result = pt.cutover(h.root, h.builder, h.obs(live, stock), taps)
            self.assertNotEqual((result or {}).get("reason"), "planned_bus_tap_trunk_identity_changed", result)
            h.builder.ensure_plan.assert_called()
            # A foreign belt on that tile (other direction than the tap's) is still a changed trunk.
            other = dict(placed, direction=trunk["direction"])
            for t in taps:
                t["built"] = False
            result = pt.cutover(h.root, h.builder, h.obs(live[:-1] + [other], stock), taps)
            self.assertEqual(result["reason"], "planned_bus_tap_trunk_identity_changed")

    def test_the_bus_builds_around_a_built_consumer_tap(self):
        # pace1 seeds 1634401926, 1193005910: a produce row tapped the iron trunk while the
        # bus was still in its build phase; the bus wanted its trunk belt back under the splitter.
        with TemporaryDirectory() as root:
            h = Harness(root)
            taps = self.connect(h)["taps"]
            state = dict(h.state, taps=[dict(t, built=False) for t in taps])
            staged = state["plan"]
            self.assertIs(pb.build_plan(state, staged), staged)  # not cut over yet: the belt stays
            # aud-1114-0614 seed 375147794: the owner's cut-over has begun (splitter standing, or a
            # recorded trunk belt mined) before it marks the tap built: the bus wants no belt back.
            tap = state["taps"][0]
            trunk = [
                {"name": "transport-belt", "position": dict(p), "direction": 0, "unit_number": u}
                for t in state["taps"]
                for p, u in zip(t["remove"], t["remove_units"])
            ]
            self.assertIs(pb.build_plan(state, staged, {"entities": trunk}), staged)
            mined = {"entities": trunk[len(tap["remove"]) :]}
            self.assertIn(xy(tap["splitter"]), {xy(e) for e in pb.build_plan(state, staged, mined)["entities"]})
            standing = {"entities": trunk + [dict(tap["splitter"])]}
            self.assertIsNot(pb.build_plan(state, staged, standing), staged)
            state["taps"][0]["built"] = True
            plan = pb.build_plan(state, staged)
            names = {xy(e): e["name"] for e in plan["entities"]}
            self.assertEqual(names[xy(taps[0]["splitter"])], "splitter")
            removed = {xy({"position": p}) for p in taps[0]["remove"]}
            self.assertFalse(
                any(
                    e["name"] == "transport-belt" and xy(e) in removed and e not in taps[0]["entities"]
                    for e in plan["entities"]
                )
            )
            self.assertEqual(plan["ports"], staged["ports"])
            for p in taps[1]["remove"]:
                self.assertIn(xy({"position": p}), {xy(e) for e in plan["entities"] if e["name"] == "transport-belt"})

    def test_a_restarted_consumer_reuses_its_built_taps(self):
        with TemporaryDirectory() as root:
            h = Harness(root)
            taps = self.connect(h)["taps"]
            for tap in taps:
                tap["built"] = True
            pt.publish(h.root, "w", taps)
            again = self.connect(h)
            self.assertTrue(again["ok"])
            self.assertEqual(again["taps"], [])
            self.assertEqual(
                {p["position"]["x"] for p in again["sources"].values()}, {t["port"]["position"]["x"] for t in taps}
            )

    def test_supply_rule(self):
        self.assertEqual(pt.tap_budget(150, 60), 90)
        # Over-subscribed: half the trunk is an allowance; branch-priority taps upstream are served first.
        self.assertEqual(pt.tap_budget(60, 60), 30)


class ProduceTests(unittest.TestCase):
    def test_produce_routes_plate_inputs_from_a_planned_tap(self):
        with TemporaryDirectory() as root:
            h = Harness(root, "E")
            board = h.board()
            view = pt.BusView(board["shared_main_bus"])
            port = {
                "kind": "item",
                "item": "iron-plate",
                "position": view.world(30, view.bus.half + 4),
                "facing": 0,
                "direction": "input",
            }
            line = {
                "entities": [],
                "inputs": [port],
                "main": {"recipe": "iron-gear-wheel", "ports": [port], "crafts_per_machine": 1, "count": 2},
                "sub": None,
            }
            catalog = SimpleNamespace(
                recipes={"iron-gear-wheel": {"ingredients": [{"name": "iron-plate", "amount": 2}]}}
            )
            game = SimpleNamespace(query=Mock(return_value={"ok": True, "max_span": SPAN}))
            calls = []

            def route(game, builder, obs, source, destination, reserved, **kwargs):
                calls.append(source)
                return ConsumerTests.route(game, builder, obs, source, destination, reserved)

            original = production_site.plan_connection
            production_site.plan_connection = route
            try:
                routed = production_site._routes(
                    game, h.builder, h.obs(), board, line, "production_row_iron-gear-wheel", "iron-gear-wheel",
                    allocation("right", 20, 40), catalog,
                )  # fmt: skip
            finally:
                production_site.plan_connection = original
            self.assertTrue(routed["ok"], routed)
            self.assertEqual(
                routed["sources"], [{"item": "iron-plate", "kind": "planned_bus_tap", "block": "shared_main_bus"}]
            )
            tap = routed["taps"][0]
            self.assertEqual(tap["demand_per_minute"], 4)
            self.assertEqual(calls[0]["name"], "production_row_iron-gear-wheel:iron-plate")
            self.assertEqual(routed["connections"][0]["source"], tap["port"]["position"])


class BandTests(unittest.TestCase):
    def test_routes_keep_out_of_the_bus_band(self):
        with TemporaryDirectory() as root:
            bus = Bus((0, 0), "N", 28, 160, lane_table(28, ORDER, 4, 2))
            plan = {"world_id": "w", "plan": {"bus": bus.to_dict()}}
            (Path(root) / "site-plan.json").write_text(json.dumps(plan), encoding="utf-8")
            game = SimpleNamespace(
                cfg=SimpleNamespace(runtime_dir=root),
                query=Mock(return_value={"ok": True, "blocked": [], "max_span": SPAN}),
            )
            builder = SimpleNamespace(_occupied_by_plan=GEOMETRY._occupied_by_plan, can_place=lambda es: {"ok": True})
            obs = {"world_id": "w", "entities": []}
            rect = bus.rect()
            self.assertEqual(rect, Rect(-14, -160, 14, 0))
            inside = {
                "kind": "item",
                "item": "iron-plate",
                "position": {"x": 0.5, "y": -40.5},
                "facing": 0,
                "direction": "input",
            }
            west = {
                "kind": "item",
                "item": "iron-plate",
                "position": {"x": -20.5, "y": -40.5},
                "facing": 0,
                "direction": "output",
            }
            east = {
                "kind": "item",
                "item": "iron-plate",
                "position": {"x": 20.5, "y": -40.5},
                "facing": 0,
                "direction": "input",
            }
            self.assertEqual(
                plan_connection(game, builder, obs, west, inside)["reason"], "route_endpoint_inside_bus_band"
            )
            # Crossing the whole band is impossible (wider than any underground span).
            self.assertFalse(plan_connection(game, builder, obs, west, east).get("ok"))
            # The bus's own feed may end inside it; a route beside the band is fine.
            south = dict(west, position={"x": -20.5, "y": -10.5})
            self.assertTrue(
                plan_connection(
                    game, builder, obs, south, dict(west, position={"x": -20.5, "y": -60.5}, direction="input")
                )["ok"]
            )
            head = dict(inside, position={"x": 0.5, "y": -0.5})
            below = dict(west, position={"x": 0.5, "y": 6.5})
            self.assertTrue(plan_connection(game, builder, obs, below, head, bus_route=True)["ok"])
            self.assertEqual(
                plan_connection(game, builder, obs, below, head)["reason"], "route_endpoint_inside_bus_band"
            )


if __name__ == "__main__":
    unittest.main()
