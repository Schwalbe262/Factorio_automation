"""Unexplained route failures of fleets aud-1114-0436 / aud-1114-0526, replayed offline.

* seed 3098273696 (and 2446866888's twin layout): the bus feeds must cross. Iron comes from the
  north (-45.5, -24.5) to the southern lane head (-48.5, -18.5), copper from the south (-45.5, -5.5)
  to the northern head (-48.5, -20.5). Either feed alone routes; whichever went first took the
  tile in front of the other head, (-47.5, -20.5) or (-47.5, -18.5), where no underground fits, so
  ``planned_bus_feed_route_failed`` held 17 minutes. An earlier feed now keeps off the later feeds'
  head approach tiles (``transport.plan_connection(keep_clear=)``) on a second pair of orders.
* seed 2446866888: the iron bank's power bridge ran a pole column inside the header corridor
  (x 20..24) and its pole (23.5, 10.5) stood in front of the iron lane head. Bridges keep out of
  the site plan's belt corridors (``power_grid._plan_corridors``) when any other hop route exists.
* seed 3242269094: copper ore (-8.5, -52.5) -> (-39.5, -88.5) was widened to the planned copper
  corridor at (60..247, 156..314) (the bank stood on another patch): every district site failed
  ``belt_corridor_survey_budget_exceeded``. A guide too large to survey is dropped.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factorio_agent import planned_bus as pb
from factorio_agent import transport
from factorio_agent.power_grid import plan_connection as pole_connection


def belt(x, y, direction=12):
    return {"name": "transport-belt", "position": {"x": x, "y": y}, "direction": direction}


class CrossingFeedsTests(unittest.TestCase):
    HEADS = {"iron-plate": {"x": -48.5, "y": -18.5}, "copper-plate": {"x": -48.5, "y": -20.5}}

    def run_feeds(self):
        calls = []

        def route(game, builder, obs, source, destination, reserved, *, keep_clear=(), **kw):
            item = source["item"]
            calls.append((item, tuple(keep_clear)))
            taken = {(e["position"]["x"], e["position"]["y"]) for e in reserved}
            other = "copper-plate" if item == "iron-plate" else "iron-plate"
            approach = (self.HEADS[item]["x"] + 1, self.HEADS[item]["y"])
            if approach in taken:
                return {"ok": False, "reason": "no_clean_trunk_corridor"}
            # Each feed alone runs a column through the other head's approach tile, unless kept clear.
            column = -47.5 if (-47.5, self.HEADS[other]["y"]) not in set(keep_clear) else -46.5
            ys = range(-24, -18) if item == "iron-plate" else range(-20, -5)
            path = [belt(column, y + 0.5) for y in ys] + [belt(-47.5, self.HEADS[item]["y"])]
            return {"ok": True, "entities": path, "route_metrics": {"length": len(path)}}

        bus = pb.PlannedBus.__new__(pb.PlannedBus)
        bus.game = bus.builder = None
        bus.route = route
        feeds = [
            {"item": "iron-plate", "source": {"item": "iron-plate", "position": {"x": -45.5, "y": -24.5}}},
            {"item": "copper-plate", "source": {"item": "copper-plate", "position": {"x": -45.5, "y": -5.5}}},
        ]
        lanes = [SimpleNamespace(material=item) for item in self.HEADS]
        with patch.object(pb, "lane_tile", lambda b, lane, along: dict(self.HEADS[lane.material])):
            result = bus._route_feeds({}, SimpleNamespace(direction="W"), lanes, feeds, [], None)
        return result, calls

    def test_crossing_feeds_keep_the_later_head_approach_clear(self):
        result, calls = self.run_feeds()
        self.assertTrue(result["ok"], result)
        self.assertEqual([c[1] for c in calls[:4]], [(), (), (), ()])  # the plain orders first
        self.assertIn(("iron-plate", ((-47.5, -20.5),)), calls)
        self.assertEqual([m["item"] for m in result["metrics"]], ["iron-plate", "copper-plate"])

    def test_keep_clear_tiles_are_hard_for_the_router(self):
        bounds = []

        def query(body):
            args = json.loads(
                json.loads(body.split("helpers.json_to_table(", 1)[1].split(");local b=args.bounds", 1)[0])
            )
            bounds.append(args["bounds"])
            return {"ok": True, "blocked": [], "clearable": [], "max_span": 5, "exit_prefix": [], "crossable_pipes": []}

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=temp.name), query=query)
        builder = SimpleNamespace(
            _occupied_by_plan=lambda es: {(e["position"]["x"], e["position"]["y"]) for e in es},
            can_place=lambda es: {"ok": True, "blocked": []},
        )
        obs = {"world_id": "w", "entities": []}
        source = {"item": "iron-plate", "position": {"x": 0.5, "y": 0.5}, "facing": 4, "direction": "output"}
        end = {"item": "iron-plate", "position": {"x": 6.5, "y": 0.5}, "facing": 4, "direction": "input"}
        straight = transport.plan_connection(game, builder, obs, source, end)
        self.assertTrue(straight["ok"], straight)
        self.assertIn((3.5, 0.5), {(e["position"]["x"], e["position"]["y"]) for e in straight["entities"]})
        around = transport.plan_connection(game, builder, obs, source, end, keep_clear=[(3.5, 0.5)])
        self.assertTrue(around["ok"], around)
        self.assertNotIn((3.5, 0.5), {(e["position"]["x"], e["position"]["y"]) for e in around["entities"]})

        # seed 3242269094: a guide far beyond the survey budget only widened the box; it is dropped.
        far = (60, 156, 247, 314)
        guided = transport.plan_connection(game, builder, obs, source, end, guide=far)
        self.assertTrue(guided["ok"], guided)
        self.assertLess(bounds[-1]["max_x"], 60)
        near = (0, -20, 10, 20)
        transport.plan_connection(game, builder, obs, source, end, guide=near)
        self.assertLessEqual(bounds[-1]["min_y"], -19.5)  # a surveyable guide still widens the box


class BridgeCorridorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "pcb-board.json").write_text(json.dumps({"world_id": "w", "blocks": {}}), encoding="utf-8")

        def query(lua):
            payload = lua.split("helpers.json_to_table(", 1)[1].split(");local clear", 1)[0]
            return {"ok": True, "clear": list(range(1, len(json.loads(json.loads(payload))["points"]) + 1))}

        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root), query=query)
        self.builder = SimpleNamespace(
            _occupied_by_plan=lambda es: {(e["position"]["x"], e["position"]["y"]) for e in es}
        )
        engine = {"name": "steam-engine", "position": {"x": 0, "y": -40}, "electric_network_id": 1, "energy": 1}
        pole = {"name": "small-electric-pole", "position": {"x": 22.5, "y": -30.5}, "electric_network_id": 1}
        self.obs = {"world_id": "w", "entities": [engine, pole]}

    def corridor(self, rect):
        plan = {
            "zones": [],
            "strips": [],
            "corridors": [{"name": "corridor:smelting->bus", "segments": [{"rect": rect}]}],
        }
        (self.root / "site-plan.json").write_text(json.dumps({"world_id": "w", "plan": plan}), encoding="utf-8")

    def test_a_bridge_along_the_header_corridor_keeps_out_of_it(self):
        along = pole_connection(self.game, self.builder, self.obs, {"x": 22.5, "y": 30.5})
        self.assertTrue(any(20 < e["position"]["x"] < 24 for e in along["entities"][:-1]))
        self.corridor([20, -100, 24, 100])
        around = pole_connection(self.game, self.builder, self.obs, {"x": 26.5, "y": 30.5})
        self.assertTrue(around["ok"], around)
        for e in around["entities"]:
            self.assertFalse(19.5 < e["position"]["x"] < 24.5, e)

    def test_a_goal_inside_a_corridor_box_is_still_reached(self):
        self.corridor([20, -100, 24, 100])
        inside = pole_connection(self.game, self.builder, self.obs, {"x": 22.5, "y": 30.5})
        self.assertTrue(inside["ok"], inside)


if __name__ == "__main__":
    unittest.main()
