from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_input_links import _geometry, _path


def entity(name, x, y, direction=0):
    return {"name": name, "position": {"x": x, "y": y}, "direction": direction}


def point(row):
    return row["position"]["x"], row["position"]["y"]


class ComponentCrossingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(temporary.name)), query=Mock())
        self.catalog = SimpleNamespace(fingerprint="one", recipes={}, entities={}, technologies={})
        self.builder = FactoryBuilder(self.game, Mock(), self.catalog)
        self.builder.can_place = Mock(return_value={"ok": True})
        self.builder.route = Mock(side_effect=AssertionError("crossing legs must use the shared survey"))
        self.factory = DeterministicFactory(self.game, Mock(), self.builder, self.catalog)
        self.factory._sync({"world_id": "one", "tick": 100})
        self.fixture()

    def fixture(self, count=3):
        self.source = {"x": .5, "y": count * 8 + 3.5}
        self.destination = {"x": .5, "y": .5}
        self.facing = 0
        self.barriers, self.blockers, self.gates = [], [], []
        for index in range(count):
            y, gate_x = index * 8 + 5.5, index * 4 - 3.5
            self.barriers += [entity("transport-belt", x + .5, y, 4) for x in range(-70, 71)]
            # Only one arm site crosses each wall. Consecutive gates have
            # different x coordinates, so an aligned two-crossing shortcut fails.
            self.blockers += [entity("stone-wall", x + .5, y + 1)
                              for x in range(-70, 71) if x + .5 != gate_x]
            self.gates.append((gate_x, y + 1))
        self.game.query.side_effect = self.survey

    def survey(self, body):
        self.assertIn("bounded belt crossing survey", body)
        return {"ok": True, "belts": deepcopy(self.barriers),
                "blocked": [deepcopy(row["position"]) for row in self.barriers + self.blockers]}

    def route(self):
        return self.factory._belt_bridge_route(
            self.source, self.destination, self.barriers + self.blockers,
            start_direction=self.facing, end_direction=self.facing)

    def assert_paid_connected_route(self, result, arm_count):
        self.assertTrue(result["ok"], result)
        rows = result["segments"]
        self.assertFalse(result["flow_verified"])
        self.assertEqual(sum(row.get("name") == "long-handed-inserter" for row in rows), arm_count)
        poles = [row for row in rows if row.get("name") == "small-electric-pole"]
        arms = [row for row in rows if row.get("name") == "long-handed-inserter"]
        for arm in arms:
            self.assertTrue(any(max(abs(point(pole)[axis] - point(arm)[axis]) for axis in (0, 1)) <= 2
                                for pole in poles), arm)
        occupied = {point(row) for row in self.barriers + self.blockers}
        self.assertTrue(all(point(row) not in occupied for row in rows))
        identities = [(row.get("name", "transport-belt"), point(row)) for row in rows]
        self.assertEqual(len(identities), len(set(identities)))
        belts, edges, inlets = _geometry({"entities": rows})
        source, destination = (self.source["x"], self.source["y"]), (self.destination["x"], self.destination["y"])
        self.assertEqual(belts[source]["direction"], self.facing)
        self.assertEqual(belts[destination]["direction"], self.facing)
        path = _path(belts, edges, source, destination)
        self.assertIsNotNone(path)
        self.assertEqual(sum(row["name"] == "long-handed-inserter" for row in path), arm_count)
        self.assertEqual(inlets, [])

    def test_three_offset_barriers_have_one_connected_paid_route(self):
        before = deepcopy((self.barriers, self.blockers, self.source, self.destination))
        result = self.route()
        self.assert_paid_connected_route(result, 3)
        self.assertEqual({point(row) for row in result["segments"]
                          if row.get("name") == "long-handed-inserter"}, set(self.gates))
        self.assertEqual((self.barriers, self.blockers, self.source, self.destination), before)
        self.game.query.assert_called_once()
        self.builder.route.assert_not_called()

    def test_rotated_three_crossings_preserve_both_endpoint_directions(self):
        def rotate(position):
            return {"x": -position["y"], "y": position["x"]}
        self.source, self.destination = rotate(self.source), rotate(self.destination)
        self.barriers = [{**row, "position": rotate(row["position"]), "direction": 8}
                         for row in self.barriers]
        self.blockers = [{**row, "position": rotate(row["position"])} for row in self.blockers]
        self.facing = 4
        self.assert_paid_connected_route(self.route(), 3)
        self.game.query.assert_called_once()

    def test_four_required_crossings_fail_after_one_survey_without_fallback(self):
        self.fixture(count=4)
        with patch("factorio_ai.deterministic_belt_crossings.MAX_CROSSINGS", 3):
            result = self.route()
        self.assertFalse(result["ok"], result)
        self.game.query.assert_called_once()
        self.builder.route.assert_not_called()

    def test_occupied_only_arm_site_cannot_cross_the_barrier(self):
        self.fixture(count=1)
        self.blockers.append(entity("stone-wall", *self.gates[0]))
        result = self.route()
        self.assertFalse(result["ok"], result)
        self.game.query.assert_called_once()

    def test_closest_crossing_with_blocked_facing_does_not_hide_clear_alternative(self):
        for boundary, y in (("pickup approach", 9.5), ("drop departure", 3.5)):
            with self.subTest(boundary=boundary):
                self.fixture(count=1)
                self.game.query.reset_mock()
                # The near arm and both endpoints fit, but its required belt
                # facing is blocked. The offset gate remains fully usable.
                self.blockers = [row for row in self.blockers if point(row) != (.5, 6.5)]
                self.blockers.append(entity("stone-wall", .5, y))
                result = self.route()
                self.assert_paid_connected_route(result, 1)
                self.assertEqual([point(row) for row in result["segments"]
                                  if row.get("name") == "long-handed-inserter"], [self.gates[0]])
                self.assertEqual(result["crossing_candidates_checked"], 1)
                self.game.query.assert_called_once()

    def test_live_pole_placement_denial_rejects_unpowered_crossing(self):
        self.fixture(count=1)
        self.builder.can_place.side_effect = lambda rows: {
            "ok": not any(row.get("name") == "small-electric-pole" for row in rows)}
        result = self.route()
        self.assertFalse(result["ok"], result)
        self.assertTrue(any(any(row.get("name") == "small-electric-pole" for row in call.args[0])
                            for call in self.builder.can_place.call_args_list))
        self.game.query.assert_called_once()

    def test_combined_placement_failure_rejects_individually_clear_equipment(self):
        self.builder.can_place.side_effect = lambda rows: {"ok": len(rows) <= 16}
        result = self.route()
        self.assertFalse(result["ok"], result)
        self.assertTrue(any(len(call.args[0]) > 16 for call in self.builder.can_place.call_args_list))
        self.game.query.assert_called_once()

    def test_incomplete_survey_fails_closed_before_placement(self):
        self.game.query.side_effect = None
        for response in ({"ok": True, "belts": []}, {"ok": True, "blocked": []}):
            with self.subTest(response=response):
                self.game.query.return_value = response
                self.assertFalse(self.route()["ok"])
        self.builder.can_place.assert_not_called()

    def test_invalid_survey_rows_fail_closed_before_placement(self):
        self.game.query.side_effect = None
        invalid = [
            {"blocked": [None]},
            {"blocked": [{"x": .5}]},
            {"blocked": [{"x": float("nan"), "y": .5}]},
            {"blocked": [{"x": True, "y": .5}]},
            {"belts": [None]},
            {"belts": [{"position": {"x": .5, "y": .5}, "direction": 4}]},
            {"belts": [entity("transport-belt", .5, .5, 3)]},
            {"belts": [entity("transport-belt", .5, .5, False)]},
            {"belts": [entity("transport-belt", .5, .5, [4])]},
        ]
        for fields in invalid:
            with self.subTest(fields=fields):
                self.game.query.return_value = {"ok": True, "blocked": [], "belts": [], **fields}
                self.assertFalse(self.route()["ok"])
        self.builder.can_place.assert_not_called()

    def test_empty_factorio_tables_are_valid_empty_survey_arrays(self):
        self.game.query.side_effect = None
        self.game.query.return_value = {"ok": True, "blocked": {}, "belts": {}}
        self.assert_paid_connected_route(self.route(), 3)


if __name__ == "__main__":
    unittest.main()
