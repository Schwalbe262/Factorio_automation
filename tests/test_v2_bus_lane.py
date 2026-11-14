"""Bus lane extension on a built planned bus (bus_lane.py)."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent import bus_lane, planned_tap
from factorio_agent.board import entity
from factorio_agent.planned_bus import lane_tile, publication, trunk_ports, trunk_segments
from factorio_agent.site.busframe import Bus, lane_table

ORDER = (
    "iron-plate",
    "iron-plate",
    "copper-plate",
    "copper-plate",
    "coal",
    "stone",
    "steel-plate",
    "electronic-circuit",
)


def bus() -> Bus:
    """eval-1575138435's bus: north from (96, -48), 28 wide, 5 groups of 4 lanes, gap 2."""
    return Bus((96, -48), "N", 28, 320, lane_table(28, ORDER, 4, 2))


def bus_state(root: Path) -> dict:
    frame = bus()
    trunks, entities = [], []
    for index, item in ((0, "iron-plate"), (2, "copper-plate")):
        lane = frame.lanes[index]
        trunks.append(
            {
                "item": item,
                "lane": index,
                "head": lane_tile(frame, lane, 0),
                "tail": lane_tile(frame, lane, 63),
                "length": 64,
            }
        )
        entities += [
            entity("transport-belt", s["position"]["x"], s["position"]["y"], s["direction"])
            for s in trunk_segments(frame, lane, 64)
        ]
    plan = {"ok": True, "entities": entities, "ports": trunk_ports(frame, trunks), "underground_pairs": []}
    state = {
        "ok": True,
        "world_id": "w",
        "catalog_fingerprint": "c",
        "bus": frame.to_dict(),
        "trunks": trunks,
        "feeds": [],
        "plan": plan,
        "staged_plan": json.loads(json.dumps(plan)),
        "taps": [],
        "phase": "verified",
    }
    (root / "planned-main-bus.json").write_text(json.dumps(state))
    return state


class LaneChoice(unittest.TestCase):
    def test_reserved_lane_first_unless_no_tap_reaches_the_consumers_side(self):
        frame = bus()
        trunks = [{"lane": 0, "item": "iron-plate"}, {"lane": 2, "item": "copper-plate"}]
        lane, assigned = bus_lane.choose_lane(frame, trunks, "coal")
        self.assertEqual((lane.index, assigned), (4, False))
        # Left side: past the partner tile lies the group gap, a tap fits.
        self.assertEqual(bus_lane.choose_lane(frame, trunks, "coal", "left")[0].index, 4)
        # Right side: lane 4 sits between reserved lanes (eval-1575138435: every east tap failed
        # tap_branch_cannot_tunnel_under_lanes); the spare lane at the right edge is chosen.
        lane, assigned = bus_lane.choose_lane(frame, trunks, "coal", "right")
        self.assertEqual((lane.index, lane.offset, lane.material, assigned), (19, 13, "coal", True))
        self.assertTrue(bus_lane.tappable(frame, lane, "right"))
        self.assertFalse(bus_lane.tappable(frame, frame.lanes[4], "right"))

    def test_an_item_without_a_reserved_lane_takes_a_spare_one_and_none_is_no_room(self):
        frame = bus()
        lane, assigned = bus_lane.choose_lane(frame, [], "plastic-bar")
        self.assertTrue(assigned)
        self.assertEqual(lane.material, "plastic-bar")
        full = Bus(frame.origin, frame.direction, frame.width, frame.length, frame.lanes[:8])
        built = [{"lane": i} for i in range(8)]
        self.assertIsNone(bus_lane.choose_lane(full, built, "plastic-bar"))

    def test_header_waypoints_sit_beside_the_band_and_face_the_head(self):
        points = bus_lane.header_waypoints(bus())
        self.assertEqual(points[0], (80.5, -44.5, 4))  # west of the band, facing east
        self.assertEqual(points[1], (111.5, -44.5, 12))  # east of the band, facing west


class LaneNodes(unittest.TestCase):
    def setUp(self):
        self.caps = {bus_lane.CAPABILITY: {}}
        self.obs = {"bus_lanes": {"phase": "verified", "carried": ["iron-plate"], "reserved": ["coal"], "spare": 12}}
        self.job = {
            "key": "chem",
            "capability": "chemical_production",
            "status": "blocked",
            "needs": {"kind": "route", "reason": bus_lane.ACROSS_BAND, "item": "coal", "side": "right"},
        }

    def test_a_consumer_blocked_across_the_band_proposes_the_lane_once(self):
        nodes = bus_lane.bus_lane_nodes([self.job], [], self.obs, self.caps)
        self.assertEqual([n["parameters"] for n in nodes], [{"item": "coal", "side": "right"}])
        self.assertEqual(bus_lane.bus_lane_nodes([self.job], nodes, self.obs, self.caps), [])
        running = {"capability": bus_lane.CAPABILITY, "parameters": {"item": "coal"}, "status": "waiting"}
        self.assertEqual(bus_lane.bus_lane_nodes([self.job, running], [], self.obs, self.caps), [])

    def test_no_node_when_carried_no_room_or_another_reason(self):
        carried = dict(self.obs["bus_lanes"], carried=["iron-plate", "coal"])
        self.assertEqual(bus_lane.bus_lane_nodes([self.job], [], {"bus_lanes": carried}, self.caps), [])
        full = dict(self.obs["bus_lanes"], reserved=[], spare=0)
        self.assertEqual(bus_lane.bus_lane_nodes([self.job], [], {"bus_lanes": full}, self.caps), [])
        other = dict(self.job, needs=dict(self.job["needs"], reason="no_clean_trunk_corridor"))
        self.assertEqual(bus_lane.bus_lane_nodes([other], [], self.obs, self.caps), [])
        self.assertEqual(bus_lane.bus_lane_nodes([self.job], [], {}, self.caps), [])


class LaneBuild(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = bus_state(self.root)
        # A free coal port west of the bus head (the item's source).
        self.port = {
            "kind": "item",
            "item": "coal",
            "direction": "output",
            "position": {"x": 70.5, "y": -45.5},
            "facing": 4,
        }
        board = {
            "world_id": "w",
            "catalog_fingerprint": "c",
            "blocks": {"coal_supply": {"reserved_area": [[66, -47], [70, -44]], "entities": [], "ports": [self.port]}},
            "connections": [],
        }
        board["shared_main_bus"] = publication(self.state)
        (self.root / "pcb-board.json").write_text(json.dumps(board))
        self.game = SimpleNamespace(
            cfg=SimpleNamespace(runtime_dir=self.root), query=Mock(return_value={"ok": True, "max_span": 4})
        )
        self.builder = SimpleNamespace(ensure_plan=Mock(return_value={"status": "succeeded"}))
        self.outposts = SimpleNamespace(state={"world_id": "w", "banks": []})
        self.routes = []

        def route(game, builder, obs, source, destination, reserved, **kwargs):
            self.routes.append((source["position"], destination["position"], kwargs.get("bus_route")))
            belts = [entity("transport-belt", x + 0.5, -45.5, 4) for x in range(71, 88)]
            return {"ok": True, "entities": belts, "underground_pairs": [], "route_metrics": {"length": 17}}

        self.lane = bus_lane.BusLane(
            self.game,
            self.builder,
            SimpleNamespace(fingerprint="c"),
            outposts=self.outposts,
            item="coal",
            route=route,
            check=Mock(return_value={"ok": True, "clear_obstacles": []}),
        )
        self.obs = {"world_id": "w", "tick": 5, "entities": []}

    def test_the_lane_is_planned_built_then_joins_the_bus_and_consumers_can_tap_it(self):
        planned = self.lane.prepare(self.obs)
        self.assertTrue(planned["ok"], planned)
        self.assertEqual(planned["lane"]["index"], 4)
        self.assertEqual(planned["trunk"]["head"], {"x": 88.5, "y": -48.5})
        self.assertEqual(self.routes[0][1:], ({"x": 88.5, "y": -48.5}, True))  # a bus route onto the head
        self.assertEqual(len(planned["trunk_entities"]), bus_lane.LANE_TILES)
        result = self.lane.step(self.obs)
        self.assertEqual(result["status"], "waiting")  # built, no coal at the tail yet
        board = json.loads((self.root / "pcb-board.json").read_text())
        self.assertIn(bus_lane.block_name("coal"), board["blocks"])
        self.assertFalse(planned_tap.carries(board["shared_main_bus"], "coal"))  # not before coal arrives
        tail = planned["trunk"]["tail"]
        self.obs["entities"] = [
            dict(e, unit_number=100 + i, belt_inventory={"coal": 3} if e["position"] == tail else {})
            for i, e in enumerate(self.lane.state["plan"]["entities"])
        ]
        result = self.lane.step(self.obs)
        self.assertEqual(result["status"], "succeeded", result)
        state = json.loads((self.root / "planned-main-bus.json").read_text())
        self.assertEqual([t["item"] for t in state["trunks"]], ["iron-plate", "copper-plate", "coal"])
        board = json.loads((self.root / "pcb-board.json").read_text())
        self.assertTrue(planned_tap.carries(board["shared_main_bus"], "coal"))
        lanes = {row["index"]: row for row in board["shared_main_bus"]["lanes"]}
        self.assertEqual(lanes[4]["status"], "built")
        # A consumer east of the bus now finds a coal tap row on the lane (spare-lane case below).
        view = planned_tap.BusView(board["shared_main_bus"])
        self.assertEqual(view.items["coal"], 4)

    def test_a_spare_lane_given_to_the_item_is_published_with_its_material(self):
        planned = self.lane.prepare(self.obs, "right")
        self.assertTrue(planned["ok"], planned)
        self.assertTrue(planned["assigned_spare_lane"])
        self.lane.state = planned
        tail = planned["trunk"]["tail"]
        self.obs["entities"] = [
            dict(e, unit_number=100 + i, belt_inventory={"coal": 3} if e["position"] == tail else {})
            for i, e in enumerate(planned["plan"]["entities"])
        ]
        self.assertEqual(self.lane.step(self.obs)["status"], "succeeded")
        board = json.loads((self.root / "pcb-board.json").read_text())
        row = next(r for r in board["shared_main_bus"]["lanes"] if r["index"] == planned["lane"]["index"])
        self.assertEqual((row["material"], row["status"]), ("coal", "built"))
        found, why = planned_tap.candidates(
            planned_tap.BusView(board["shared_main_bus"]), "coal", "right", 40, 30, 50, span=4
        )
        self.assertTrue(found, why)  # the east tap fits: nothing to tunnel past the partner tile

    def test_no_source_and_no_room_are_precise_blockers(self):
        board = json.loads((self.root / "pcb-board.json").read_text())
        board["blocks"] = {}
        (self.root / "pcb-board.json").write_text(json.dumps(board))
        self.assertEqual(self.lane.prepare(self.obs)["reason"], "bus_lane_item_source_missing")
        self.assertEqual(
            bus_lane.BusLane(
                self.game, self.builder, SimpleNamespace(fingerprint="c"), outposts=self.outposts, item="iron-plate"
            ).prepare(self.obs)["reason"],
            "bus_lane_already_built",
        )


class RouteTaps(unittest.TestCase):
    def test_a_recorded_route_tap_stands_for_the_belt_it_replaced_and_leaves_the_remote_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            belt = entity("transport-belt", 110.5, 6.5, 4)
            remote = {"world_id": "w", "plan": {"entities": [belt, entity("transport-belt", 111.5, 6.5, 4)]}}
            (root / "remote-smelting-iron-ore.json").write_text(json.dumps(remote))
            tap = {
                "name": "bus_lane:coal",
                "splitter": {"name": "splitter", "position": {"x": 110.5, "y": 6.0}, "direction": 4},
                "remove": [{"x": 110.5, "y": 6.5}],
                "tapped_owner": "remote_smelting:iron-ore",
            }
            bus_lane.record_route_tap(root, "w", tap)
            self.assertTrue(bus_lane.release_owner_plan(root, "w", tap))
            self.assertFalse(bus_lane.release_owner_plan(root, "w", tap))  # idempotent
            plan = json.loads((root / "remote-smelting-iron-ore.json").read_text())["plan"]
            self.assertNotIn(belt, plan["entities"])
            tiles = bus_lane.route_tap_tiles(root, "w")
            actual = {("splitter", 110.5, 6.0): {"name": "splitter", "direction": 4}}
            self.assertTrue(bus_lane.tapped_belt_stands(actual, tiles, belt))
            self.assertFalse(bus_lane.tapped_belt_stands({}, tiles, belt))
            self.assertEqual(bus_lane.route_tap_tiles(root, "other"), {})


if __name__ == "__main__":
    unittest.main()
