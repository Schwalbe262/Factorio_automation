from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_input_links import _geometry, _path
from factorio_ai.deterministic_underground_geometry import underground_edges
from factorio_ai.deterministic_underground_routes import plan_underground_route


def entity(name, x, y, direction=0, **extra):
    return {"name": name, "position": {"x": x, "y": y}, "direction": direction, **extra}


class UndergroundRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(temporary.name)), query=Mock())
        self.catalog = SimpleNamespace(fingerprint="one", recipes={}, entities={}, technologies={})
        self.builder = FactoryBuilder(self.game, Mock(), self.catalog)
        self.builder.can_place = Mock(return_value={"ok": True})
        self.builder.ensure_plan = Mock(return_value={"type": "build", "name": "underground-belt"})
        self.factory = DeterministicFactory(self.game, Mock(), self.builder, self.catalog)
        self.obs = {"world_id": "one", "tick": 100, "entities": []}
        self.factory._sync(self.obs)
        self.factory._port_clearances = Mock(return_value=set())
        # MAIN's real endpoint/facing and two5-span south escape shape, with
        # only the relevant free components retained in this obstacle fixture.
        self.source = {"x": -2.5, "y": -34.5}
        self.destination = {"x": -13.5, "y": -20.5}
        openings = {(-2.5, -34.5), (-1.5, -34.5), (-1.5, -33.5),
                    (-1.5, -28.5), (-1.5, -27.5)}
        blocked = [{"x": x + .5, "y": y + .5} for x in range(-62, 46) for y in range(-83, -23)
                   if (x + .5, y + .5) not in openings]
        self.survey = {"ok": True, "world_id": "one", "tick": 100, "maximum": 5,
                       "blocked": blocked, "belts": [], "mouths": []}
        self.game.query.side_effect = lambda body: deepcopy(self.survey)

    def route(self, reserved=None, end_direction=8):
        return plan_underground_route(self.factory, self.source, self.destination, reserved or [],
                                      start_direction=4, end_direction=end_direction)

    def test_trapped_source_uses_two_explicit_paid_pairs_with_a_complete_directed_path(self):
        result = self.route()
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["flow_verified"])
        self.assertEqual(len(result["underground_pairs"]), 2)
        pairs = result["underground_pairs"]
        self.assertEqual([(p["input"]["position"]["y"], p["output"]["position"]["y"]) for p in pairs],
                         [(-33.5, -28.5), (-27.5, -22.5)])
        plan = {"entities": result["segments"], "underground_pairs": pairs}
        self.assertEqual(len(underground_edges(plan)), 2)
        belts, edges, arms = _geometry(plan)
        path = _path(belts, edges, tuple(self.source.values()), tuple(self.destination.values()))
        self.assertIsNotNone(path)
        self.assertEqual(len(path), len(result["segments"]))
        self.assertEqual(arms, [])
        self.assertEqual(path[0]["direction"], 4)
        self.assertEqual(path[-1]["direction"], 8)
        self.assertEqual(sum(r["name"] == "underground-belt" for r in path), 4)
        self.builder.can_place.assert_called_once_with(result["segments"])

    def test_locked_or_short_live_range_cannot_invent_a_tunnel(self):
        self.survey.update(ok=False, reason="underground belts are locked")
        self.assertIn("locked", self.route()["reason"])
        self.survey.update(ok=True, maximum=4)
        self.assertFalse(self.route()["ok"])
        self.builder.can_place.assert_not_called()

    def test_world_tick_rollback_and_invalid_range_are_rejected_before_placement(self):
        for field, value in (("world_id", "other"), ("tick", 99), ("tick", True),
                             ("maximum", 1), ("maximum", 17), ("maximum", True)):
            with self.subTest(field=field, value=value):
                original = self.survey[field]
                self.survey[field] = value
                self.assertFalse(self.route()["ok"])
                self.survey[field] = original
        self.builder.can_place.assert_not_called()

    def test_existing_and_reserved_mouths_outside_pair_span_still_prevent_interception(self):
        for y, direction, role in ((-36.5, 8, "input"), (-18.5, 0, "output")):
            for saved in (False, True):
                with self.subTest(saved=saved, y=y, direction=direction, role=role):
                    old = entity("underground-belt", -1.5, y, direction, belt_to_ground_type=role)
                    self.survey["mouths"] = [] if saved else [old]
                    self.assertFalse(self.route([old] if saved else [])["ok"])
        self.builder.can_place.assert_not_called()

    def test_higher_tier_belt_outputs_never_become_free_source_approaches(self):
        for name in ("fast-transport-belt", "express-transport-belt", "turbo-transport-belt"):
            for point, end_direction in (((-1.5, -35.5), 8), ((-14.5, -21.5), 4)):
                for saved in (False, True):
                    with self.subTest(name=name, saved=saved, end_direction=end_direction):
                        # Eastward arrival at the finish requires its west tile;
                        # a southbound foreign belt must keep that tile blocked.
                        row = entity(name, *point, 8)
                        self.survey["belts"] = [] if saved else [row]
                        self.assertFalse(self.route([row] if saved else [], end_direction)["ok"])
        self.builder.can_place.assert_not_called()

    def test_final_placement_rejection_never_returns_the_candidate(self):
        self.builder.can_place.return_value = {"ok": False}
        result = self.route()
        self.assertFalse(result["ok"])
        self.assertIn("placement changed", result["reason"])
        self.assertNotIn("segments", result)
        self.builder.can_place.assert_called_once()

    def test_normal_consumer_connect_preserves_pairs_and_reuses_exact_plan_after_reload(self):
        route = self.route()
        self.assertTrue(route["ok"], route)
        source = {"kind": "item", "item": "transport-belt", "direction": "output",
                  "position": self.source, "facing": 4}
        consumer = {"kind": "item", "item": "transport-belt", "direction": "input",
                    "position": self.destination, "facing": 8}
        self.factory._consumer_material_route = Mock(return_value=route)
        result = self.factory.connect_input(self.obs, source, consumer, "recipe:green:belts")
        self.assertEqual(result["type"], "build")
        saved = deepcopy(self.factory.state["links"]["recipe:green:belts"])
        self.assertEqual(saved["underground_pairs"], route["underground_pairs"])
        self.assertEqual(sum(r["name"] == "underground-belt" for r in saved["entities"]), 4)
        restored = DeterministicFactory(self.game, Mock(), self.builder, self.catalog)
        restored._consumer_material_route = Mock(side_effect=AssertionError("saved route must not be replanned"))
        self.assertEqual(restored.connect_input(self.obs, source, consumer, "recipe:green:belts")["type"], "build")
        self.assertEqual(restored.state["links"]["recipe:green:belts"], saved)


if __name__ == "__main__":
    unittest.main()
