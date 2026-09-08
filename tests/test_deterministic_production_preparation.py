from copy import deepcopy
import unittest

from factorio_ai.deterministic_routine_fairness import RoutineFairness
from tests import test_deterministic_routine_fairness as fixtures


def success(**fields):
    return {"ok": True, "status": "succeeded", **fields}


class ProductionPreparationTests(unittest.TestCase):
    def setUp(self):
        fixtures.FairnessSchedulingTests.setUp(self)
        self.due()
        self.supervisor.armaments.next_action.reset_mock()

    choose = fixtures.FairnessSchedulingTests.choose
    due = fixtures.FairnessSchedulingTests.due

    def test_four_collected_belts_reach_factory_build_before_routine_can_spend_them(self):
        s = self.supervisor
        inventory = {"transport-belt": 0}
        take = {"type": "take", "name": "wooden-chest", "position": {"x": -3.5, "y": -36.5},
                "item": "transport-belt", "count": 4}
        build = {"type": "build_many", "actions": [
            {"type": "build", "name": "transport-belt", "position": {"x": x, "y": y}}
            for x, y in ((6.5, 34.5), (6.5, 33.5), (7.5, 33.5), (11.5, 33.5))]}
        s.factory.next_action.side_effect = lambda obs: take if inventory["transport-belt"] < 4 else build
        self.assertEqual(self.choose(), take)
        inventory["transport-belt"] += 4
        s.fairness.record(take, success(moved=4))
        # Restart preserves the due lane, but no action or safety authorization.
        s.fairness = RoutineFairness(s.game)
        self.assertEqual(s.fairness.state["completed"], 3)
        self.assertIsNone(s.fairness.selection)
        self.assertFalse(s.fairness.safety["ok"])
        self.assertEqual(self.choose(), build)
        s.armaments.next_action.assert_not_called()
        inventory["transport-belt"] -= 4
        s.fairness.record(build, success(completed=4, built=4, reused=0))
        self.assertEqual(inventory["transport-belt"], 0)
        self.assertEqual(s.fairness.state["completed"], 0)
        routine_build = deepcopy(build)
        for entity in routine_build["actions"]:
            entity["position"]["x"] -= 20
        s.armaments.next_action.side_effect = lambda obs: take if inventory["transport-belt"] < 4 else routine_build
        self.assertEqual(self.choose(), take)
        inventory["transport-belt"] += 4
        s.fairness.record(take, success(moved=4))
        self.assertEqual(s.fairness.state["completed"], 1)
        self.assertEqual(self.choose(), routine_build)
        inventory["transport-belt"] -= 4
        s.fairness.record(routine_build, success(completed=4, built=4, reused=0))
        self.assertEqual(inventory["transport-belt"], 0)
        self.assertEqual(s.fairness.state["completed"], 2)

    def test_preparation_and_nonstructural_receipts_keep_production_due(self):
        s = self.supervisor
        cases = [({"type": kind}, success(moved=2)) for kind in ("take", "insert", "recover_equipped_ammo")]
        cases += [({"type": "mine", "name": "tree-02"}, success()),
                  ({"type": "mine", "name": "iron-ore"}, success(mined=2)),
                  ({"type": "craft", "recipe": "inserter"}, {"ok": True, "status": "running", "started": 1}),
                  ({"type": "move"}, success()),
                  ({"type": "build"}, success(unit_number=7, reused=True)),
                  ({"type": "build"}, success()),
                  ({"type": "build"}, {"ok": False, "status": "failed"})]
        batch = {"type": "build_many", "actions": [{"type": "build"}] * 2}
        cases += [(batch, success(completed=2, built=0, reused=2)),
                  (batch, success(completed=1, built=1)),
                  (batch, success(completed=2, built=2, failed_index=0)),
                  (batch, success(completed=2, built=2, uncertain_index=0))]
        for action, receipt in cases:
            with self.subTest(action=action, receipt=receipt):
                s.factory.next_action.return_value = action
                self.assertEqual(self.choose(), action)
                s.fairness.record(action, receipt)
                self.assertEqual(s.fairness.state["completed"], 3)
        s.armaments.next_action.assert_not_called()

    def test_fresh_safety_and_higher_priorities_still_preempt_after_collection(self):
        s = self.supervisor
        take = {"type": "take", "item": "transport-belt", "count": 4}
        s.factory.next_action.return_value = take
        s.fairness.record(self.choose(), success(moved=4))
        s.factory.next_action.reset_mock()
        s.game.query.return_value = {"ok": True, "quiet": False, "routes_ready": True, "tick": 1000}
        self.assertEqual(self.choose(), self.routine)
        s.factory.next_action.assert_not_called()
        self.assertEqual(s.fairness.state["completed"], 3)
        urgent = {"type": "build", "name": "boiler"}
        s.energy.next_action.return_value = urgent
        self.assertEqual(self.choose(), urgent)
        s.fairness.record(urgent, success(unit_number=42))
        self.assertEqual(s.fairness.state["completed"], 3)

    def test_waiting_can_fall_through_but_factory_failure_is_not_hidden(self):
        s = self.supervisor
        s.factory.next_action.return_value = {"type": "take", "item": "transport-belt", "count": 4}
        s.fairness.record(self.choose(), success(moved=4))
        s.factory.next_action.return_value = {"status": "waiting", "reason": "waiting for materials"}
        self.assertEqual(self.choose(), self.routine)
        self.assertEqual(s.fairness.state["completed"], 3)
        failure = {"status": "blocked", "reason": "production route blocked"}
        s.factory.next_action.return_value = failure
        s.armaments.next_action.reset_mock()
        self.assertEqual(self.choose(), failure)
        s.armaments.next_action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
