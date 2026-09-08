"""Exercise the emitted Lua guard without requiring a Factorio test process.

The tiny comparison evaluator executes the four actual guard comparisons, not
a second copy of the contact predicate. Engine placement and pathfinding replies
are controlled fixture observations; the live read-only reach replay is separate.
"""
import json
import operator
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_navigation import CharacterNavigator


class TouchingBuildBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.bounds = {"left": 28.3515625, "top": -44.6484375,
                       "right": 28.6484375, "bottom": -44.3515625}
        self.actor = {"left_top": {"x": 27.953125, "y": -44.7109375},
                      "right_bottom": {"x": 28.3515625, "y": -44.3125}}
        self.action = {"type": "build", "name": "small-electric-pole", "item": "small-electric-pole",
                       "position": {"x": 28.5, "y": -44.5}, "direction": 0}
        self.engine_can_place = False
        self.existing = False
        self.standing = {"x": 27.5515625, "y": -44.5}
        self.moves = []
        self.game = SimpleNamespace(backend="character", query=Mock(side_effect=self.query),
                                    act=Mock(return_value={"ok": True, "status": "succeeded"}))
        self.navigator = CharacterNavigator(self.game)
        self.navigator.pending_action = Mock(return_value=None)

    def contact(self, body):
        guard = re.search(r'if (actor_box\..*?) then\s+local best=', body, re.S).group(1)
        comparisons = guard.split(" and ") if "\n" not in guard else re.split(r'\s+and\s+', guard)
        self.assertEqual(len(comparisons), 4)
        ops = {">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le}
        values = []
        for comparison in comparisons:
            match = re.fullmatch(r'actor_box\.(left_top|right_bottom)\.([xy])\s*(>=|<=|>|<)\s*(left|top|right|bottom)', comparison.strip())
            self.assertIsNotNone(match, comparison)
            corner, axis, op, edge = match.groups()
            values.append(ops[op](self.actor[corner][axis], self.bounds[edge]))
        return all(values)

    def query(self, body):
        if "local actor_box=a.bounding_box" in body:
            action = self.decode(body)
            if action["type"] == "build" and not self.existing and not self.engine_can_place and self.contact(body):
                if self.standing is None:
                    return {"ok": False, "reason": "no_character_build_standing_position"}
                return {"ok": True, "within": False, "approach": self.standing}
            return {"ok": True, "within": True}
        if "s.request_path" in body:
            self.assertNotIn("teleport", body)
            self.moves.append(self.decode(body))
            return {"ok": True, "status": "running"}
        return {"ok": True}

    @staticmethod
    def decode(body):
        encoded = re.search(r'helpers.json_to_table\(("(?:[^"\\]|\\.)*")\)', body).group(1)
        return json.loads(json.loads(encoded))

    def test_all_four_quantized_touching_edges_request_normal_walk_before_build(self):
        cases = (
            {"left_top": {"x": 27.953125, "y": -44.7109375}, "right_bottom": {"x": 28.3515625, "y": -44.3125}},
            {"left_top": {"x": 28.6484375, "y": -44.7109375}, "right_bottom": {"x": 29.046875, "y": -44.3125}},
            {"left_top": {"x": 28.30078125, "y": -45.046875}, "right_bottom": {"x": 28.69921875, "y": -44.6484375}},
            {"left_top": {"x": 28.30078125, "y": -44.3515625}, "right_bottom": {"x": 28.69921875, "y": -43.953125}},
        )
        for actor in cases:
            with self.subTest(actor=actor):
                self.actor = actor
                self.moves.clear()
                result = self.navigator.execute(self.action, {})
                self.assertEqual(result["status"], "running")
                self.assertEqual(self.moves, [{"type": "move", "position": self.standing}])
                self.game.act.assert_not_called()

    def test_observed_walk_clearance_allows_the_original_normal_build(self):
        self.navigator.execute(self.action, {})
        self.game.act.assert_not_called()
        # A subsequent engine observation says the owned actor walked clear.
        self.actor["right_bottom"]["x"] = 27.75
        self.engine_can_place = True
        result = self.navigator.execute(self.action, {})
        self.assertEqual(result["status"], "succeeded")
        self.game.act.assert_called_once_with(self.action)
        self.assertEqual(len(self.moves), 1)

    def test_other_object_blockage_with_owned_actor_clear_does_not_trigger_displacement(self):
        self.actor["right_bottom"]["x"] = self.bounds["left"] - 1 / 256
        self.game.act.return_value = {"ok": False, "reason": "placement_blocked"}
        result = self.navigator.execute(self.action, {})
        self.assertEqual(result["reason"], "placement_blocked")
        self.assertEqual(self.moves, [])
        self.game.act.assert_called_once_with(self.action)

    def test_blocked_standing_positions_never_fall_through_to_build(self):
        self.standing = None
        result = self.navigator.execute(self.action, {})
        self.assertEqual(result["reason"], "no_character_build_standing_position")
        self.assertEqual(self.moves, [])
        self.game.act.assert_not_called()

    def test_existing_entity_and_non_build_actions_do_not_trigger_displacement(self):
        self.existing = True
        self.navigator.execute(self.action, {})
        self.assertEqual(self.moves, [])
        self.existing = False
        action = {**self.action, "type": "take", "item": "iron-plate", "count": 1}
        self.navigator.execute(action, {})
        self.assertEqual(self.moves, [])
        self.assertEqual(self.game.act.call_count, 2)


if __name__ == "__main__":
    unittest.main()
