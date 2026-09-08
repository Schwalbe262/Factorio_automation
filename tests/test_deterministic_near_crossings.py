from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_input_links import _geometry, _path


def entity(name, x, y, direction=0):
    return {"name": name, "position": {"x": x, "y": y}, "direction": direction}


def point(row):
    return row["position"]["x"], row["position"]["y"]


class NearCrossingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(temporary.name)), query=Mock())
        catalog = SimpleNamespace(fingerprint="one", recipes={}, entities={}, technologies={})
        self.builder = FactoryBuilder(self.game, Mock(), catalog)
        self.builder.can_place = Mock(return_value={"ok": True})
        self.builder.route = Mock(side_effect=AssertionError("reuse the crossing survey"))
        self.factory = DeterministicFactory(self.game, Mock(), self.builder, catalog)
        self.factory._sync({"world_id": "one", "tick": 1})
        self.source, self.destination = {"x": .5, "y": 1.5}, {"x": .5, "y": 12.5}
        self.trunks = [entity("transport-belt", x + .5, y, 4)
                       for y in (3.5, 7.5) for x in range(-61, 62)]
        self.blockers = [entity("stone-wall", x + .5, y)
                         for y in (.5, 4.5, 8.5) for x in range(-61, 62)
                         if y == .5 or x != 0]
        self.facing = 8

    def route(self):
        self.game.query.return_value = {"ok": True, "belts": self.trunks,
            "blocked": [row["position"] for row in self.trunks + self.blockers]}
        return self.factory._belt_bridge_route(self.source, self.destination, self.trunks + self.blockers,
                                               start_direction=self.facing, end_direction=self.facing)

    def assert_safe_connected(self, route):
        self.assertTrue(route["ok"], route)
        belts, edges, _ = _geometry({"entities": route["segments"]})
        path = _path(belts, edges, point({"position": self.source}), point({"position": self.destination}))
        self.assertIsNotNone(path)
        self.assertEqual(sum(row["name"] == "long-handed-inserter" for row in path), 2)
        self.assertEqual(sum(row["name"] == "small-electric-pole" for row in route["segments"]), 2)
        from factorio_ai.factory_templates import DIRECTIONS
        foreign = {point(row) for row in self.trunks}
        for position, row in belts.items():
            dx, dy = DIRECTIONS[row["direction"]]
            self.assertNotIn((position[0] + dx, position[1] + dy), foreign,
                             "pickup belt must never discharge into a crossed trunk")
        self.assertEqual(route["segments"][0]["direction"], self.facing)
        self.assertEqual(route["segments"][-1]["direction"], self.facing)
        self.game.query.assert_called_once()
        self.builder.can_place.assert_called_once_with(route["segments"])
        return belts

    def test_two_pickup_side_crossings_share_one_safe_side_facing_belt(self):
        original = deepcopy(self.trunks)
        belts = self.assert_safe_connected(self.route())
        self.assertEqual(belts[(.5, 2.5)]["direction"], 12)
        self.assertEqual(belts[(.5, 6.5)]["direction"], 12)
        self.assertEqual(self.trunks, original)

    def test_rotated_crossings_preserve_side_feed_and_endpoint_directions(self):
        def rotate(position):
            return {"x": -position["y"], "y": position["x"]}
        self.source, self.destination = rotate(self.source), rotate(self.destination)
        self.trunks = [{**row, "position": rotate(row["position"]), "direction": 8} for row in self.trunks]
        self.blockers = [{**row, "position": rotate(row["position"])} for row in self.blockers]
        self.facing = 12
        self.assert_safe_connected(self.route())

    def test_saved_pickup_discharge_tiles_remain_reserved_for_future_blocks(self):
        route = self.route()
        self.assert_safe_connected(route)
        self.factory.state["links"]["crossing"] = {"ok": True, "entities": route["segments"]}
        self.factory.state = json.loads(json.dumps(self.factory.state))
        self.assertEqual(self.factory._port_clearances(), {(-.5, 2.5), (-.5, 6.5)})

    def test_occupied_pickup_side_selects_the_other_safe_facing(self):
        self.trunks.append(entity("transport-belt", -.5, 2.5, 8))
        belts = self.assert_safe_connected(self.route())
        self.assertEqual(belts[(.5, 2.5)]["direction"], 4)

    def test_pickup_with_two_occupied_sides_cannot_discharge_into_foreign_belts(self):
        self.trunks += [entity("transport-belt", x, 2.5, 8) for x in (-.5, 1.5)]
        self.assertFalse(self.route()["ok"])
        self.builder.can_place.assert_not_called()

    def test_live_combined_placement_rejection_cannot_reserve_a_crossing(self):
        self.builder.can_place.return_value = {"ok": False}
        self.assertFalse(self.route()["ok"])
        self.assertEqual(self.factory.state["links"], {})


if __name__ == "__main__":
    unittest.main()
