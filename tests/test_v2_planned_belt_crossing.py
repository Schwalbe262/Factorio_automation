"""A route crosses another block's planned (unbuilt) surface belt like a built one.

Live map eval-1575138435 (seed 1575138435, owner's GUI map): the hand-fed power's coal feed
(board block ``power_coal_feed``) was planned north along x 54.5 from y 27.5 to 8.5, right in
front of the coal bank's east port at (47.5, 20.5). Unbuilt, its belt and both neighbour columns
were hard for every route, so the coal trunk to all 17 legal district sites failed
(``failure_stage_counts: {"corridor": 18}``, ``no_clean_trunk_corridor``). The district planned
at once after the feed was built, crossing the same belt with an underground pair.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from factorio_agent.board import entity
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.transport import plan_connection, unbuilt_external_keepouts


def feed_block():
    belts = [entity("transport-belt", 54.5, y + 0.5, 0) for y in range(8, 28)]
    drill = entity("burner-mining-drill", 50, 26, 4)
    return {"entities": [drill] + belts, "ports": [], "reserved_area": [[48, 24], [51, 27]], "owner": "power"}


class PlannedBeltCrossingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        board = {"world_id": "w", "catalog_fingerprint": "c", "blocks": {"power_coal_feed": feed_block()}}
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        self.builder = FactoryBuilder.__new__(FactoryBuilder)
        self.builder.can_place = lambda entities: {"ok": True}

    def test_unbuilt_belts_are_listed_as_crossable_not_hard(self):
        crossable = []
        hard = unbuilt_external_keepouts(self.builder, {"entities": []}, {"f": feed_block()}, crossable_belts=crossable)
        self.assertEqual(len(crossable), 20)
        self.assertNotIn((54.5, 20.5), hard)
        self.assertNotIn((53.5, 20.5), hard)
        self.assertIn((54.5, 20.5), unbuilt_external_keepouts(self.builder, {"entities": []}, {"f": feed_block()}))

    def test_the_coal_trunk_crosses_the_planned_feed_with_an_underground_pair(self):
        survey = {"ok": True, "blocked": [], "clearable": [], "max_span": 4}
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root), query=lambda body: survey)
        obs = {"world_id": "w", "entities": []}
        source = {"item": "coal", "position": {"x": 47.5, "y": 20.5}, "facing": 4}
        destination = {"item": "coal", "position": {"x": 64.5, "y": 20.5}, "facing": 4}
        route = plan_connection(game, self.builder, obs, source, destination)
        self.assertTrue(route["ok"], route)
        names = {(e["name"], e["position"]["x"]) for e in route["entities"]}
        self.assertNotIn(("transport-belt", 54.5), names)  # the planned belt's tile is tunnelled
        self.assertTrue(any(e["name"] == "underground-belt" for e in route["entities"]))

    def test_no_surface_belt_runs_beside_the_planned_belt(self):
        survey = {"ok": True, "blocked": [], "clearable": [], "max_span": 4}
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root), query=lambda body: survey)
        source = {"item": "coal", "position": {"x": 52.5, "y": 34.5}, "facing": 0}
        destination = {"item": "coal", "position": {"x": 52.5, "y": 1.5}, "facing": 0}
        route = plan_connection(game, self.builder, {"world_id": "w", "entities": []}, source, destination)
        if route.get("ok"):
            beside = [
                e
                for e in route["entities"]
                if e["name"] == "transport-belt"
                and e["position"]["x"] in (53.5, 55.5)
                and 8 <= e["position"]["y"] <= 28
            ]
            self.assertEqual(beside, [])


if __name__ == "__main__":
    unittest.main()
