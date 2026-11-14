"""Planned-but-unbuilt hardware of the published bus is visible to every route.

aud-1114-0703 seed 317419567: the bus's iron feed ran north along x 31.5 from the district to the
bus head (``board["shared_main_bus"].entities``, outside every lane-band reservation). The
inserter row was planned while the feed was unbuilt and put its underground exit on
(31.5,-102.5); the bus built its feed belt there first and the row blocked
"reserved block placement is obstructed" for the rest of the run.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from factorio_agent.board import entity
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.transport import _production_keepouts, plan_connection, unbuilt_external_keepouts

FEED_X = 31.5


def feed_belts():
    """The run's iron feed column (31.5, -111.5 .. -95.5), flowing north."""
    return [entity("transport-belt", FEED_X, -95.5 - i, 0) for i in range(17)]


def bus(extra=()):
    return {
        "template": "planned_main_bus",
        "world_id": "w",
        "reservations": [{"item": "iron-plate", "area": [[32, -134], [352, -132]]}],
        "entities": feed_belts() + list(extra),
        "underground_pairs": [],
        "ports": [],
        "stations": [],
    }


class BusKeepoutTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.builder = FactoryBuilder.__new__(FactoryBuilder)
        self.builder.can_place = lambda entities: {"ok": True}
        self.obs = {"world_id": "w", "entities": []}
        survey = {"ok": True, "blocked": [], "clearable": [], "max_span": 4}
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root), query=lambda body: survey)

    def write(self, board_bus, taps=()):
        board = {"world_id": "w", "catalog_fingerprint": "c", "blocks": {}, "shared_main_bus": board_bus}
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        state = {"world_id": "w", "taps": list(taps)}
        (self.root / "planned-main-bus.json").write_text(json.dumps(state), encoding="utf-8")

    def keepouts(self, source, destination, **kw):
        bounds = {"min_x": 20, "max_x": 45, "min_y": -115, "max_y": -90}
        return _production_keepouts(self.game, self.builder, self.obs, source, destination, [], bounds, **kw)

    def test_the_unbuilt_bus_feed_is_crossable_and_its_sides_kept_free(self):
        self.write(bus())
        source = {"item": "copper-plate", "position": {"x": 36.5, "y": -102.5}, "facing": 12}
        destination = {"item": "copper-plate", "position": {"x": 26.5, "y": -102.5}, "facing": 12}
        found = self.keepouts(source, destination)
        self.assertTrue(found["ok"], found)
        self.assertIn((FEED_X, -102.5), {(e["position"]["x"], e["position"]["y"]) for e in found["planned_belts"]})
        self.assertIn((FEED_X, -102.5), found["hard"])
        self.assertIn((FEED_X + 1, -102.5), found["planned_soft"])
        # The bus's own feed route is not kept off its own plan.
        own = self.keepouts(source, destination, bus_route=True)
        self.assertNotIn((FEED_X, -102.5), own["hard"])

    def test_a_route_across_the_unbuilt_feed_tunnels_under_it(self):
        self.write(bus())
        source = {"item": "copper-plate", "position": {"x": 36.5, "y": -102.5}, "facing": 12}
        destination = {"item": "copper-plate", "position": {"x": 26.5, "y": -102.5}, "facing": 12}
        route = plan_connection(self.game, self.builder, self.obs, source, destination)
        self.assertTrue(route["ok"], route)
        tiles = {(e["position"]["x"], e["position"]["y"]) for e in route["entities"]}
        self.assertNotIn((FEED_X, -102.5), tiles)
        self.assertTrue(any(e["name"] == "underground-belt" for e in route["entities"]))

    def test_a_consumer_is_not_kept_off_its_own_published_tap(self):
        mine = entity("transport-belt", 38.5, -104.5, 8)
        theirs = entity("transport-belt", 40.5, -104.5, 8)
        taps = [
            {"name": "production_row_inserter:copper-plate", "owner": "production_row_inserter", "entities": [mine]},
            {"name": "automation_science:copper-plate", "owner": "automation_science", "entities": [theirs]},
        ]
        self.write(bus([mine, theirs]), taps)
        source = {
            "item": "copper-plate",
            "name": "production_row_inserter:copper-plate",
            "position": {"x": 36.5, "y": -104.5},
            "facing": 8,
        }
        destination = {"item": "copper-plate", "position": {"x": 36.5, "y": -96.5}, "facing": 8}
        found = self.keepouts(source, destination)
        self.assertTrue(found["ok"], found)
        self.assertNotIn((38.5, -104.5), found["hard"])
        self.assertIn((40.5, -104.5), found["hard"])

    def test_a_block_with_several_reserved_areas_keeps_only_its_outside_hardware(self):
        block = {
            "reserved_areas": [[[0, 0], [4, 4]]],
            "entities": [entity("transport-belt", 2.5, 2.5, 0), entity("transport-belt", 9.5, 2.5, 0)],
        }
        hard = unbuilt_external_keepouts(self.builder, {"entities": []}, {"b": block})
        self.assertIn((9.5, 2.5), hard)
        self.assertNotIn((2.5, 2.5), hard)
        self.assertEqual(unbuilt_external_keepouts(self.builder, {"entities": []}, {"b": {"entities": []}}), set())


if __name__ == "__main__":
    unittest.main()
