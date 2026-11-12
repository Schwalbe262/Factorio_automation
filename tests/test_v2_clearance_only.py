from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.power_layout import V2FactoryBuilder


class ClearanceOnlyTests(unittest.TestCase):
    def builder(self, result):
        builder = V2FactoryBuilder.__new__(V2FactoryBuilder)
        builder.game = SimpleNamespace(query=Mock(return_value=result))
        return builder

    def plan(self, trees=None, world="w"):
        return _plan([], [], clear_obstacles=trees or [], clearance_world_id=world)

    def test_approved_cleared_world_bound_plan_succeeds_without_base_construction(self):
        for trees in ([], [{"name": "tree-02", "position": {"x": 1.5, "y": 2.5}, "unit_number": 7}]):
            builder = self.builder({"ok": True})
            with self.subTest(trees=trees), patch.object(FactoryBuilder, "ensure_plan") as base:
                result = builder.ensure_plan({"world_id": "w", "tick": 1}, self.plan(trees))
                self.assertEqual(result["status"], "succeeded")
                base.assert_not_called()

    def test_pending_approved_nature_is_still_normally_mined(self):
        tree = {"name": "tree-02", "position": {"x": 1.5, "y": 2.5}}
        builder = self.builder({"ok": True, "obstacle": tree})
        result = builder.ensure_plan({"world_id": "w", "tick": 1}, self.plan([tree]))
        self.assertEqual(result["type"], "mine")
        self.assertEqual(result["position"], tree["position"])

    def test_world_change_and_native_clearance_identity_failure_are_not_success(self):
        builder = self.builder({"ok": False, "reason": "approved_natural_obstacle_changed"})
        result = builder.ensure_plan({"world_id": "w", "tick": 1}, self.plan(world="other"))
        self.assertEqual(result["reason"], "clearance_plan_world_changed")
        builder.game.query.assert_not_called()
        tree = {"name": "tree-02", "position": {"x": 1.5, "y": 2.5}}
        result = builder.ensure_plan({"world_id": "w", "tick": 1}, self.plan([tree]))
        self.assertEqual(result["reason"], "approved_natural_obstacle_changed")

    def test_empty_plan_without_clearance_authorization_keeps_base_validation(self):
        builder = self.builder({"ok": True})
        with patch.object(
            FactoryBuilder, "ensure_plan", return_value={"status": "blocked", "reason": "empty or invalid block plan"}
        ) as base:
            result = builder.ensure_plan({"world_id": "w", "tick": 1}, _plan([], []))
            self.assertEqual(result["status"], "blocked")
            base.assert_called_once()


if __name__ == "__main__":
    unittest.main()
