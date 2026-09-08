from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_energy import EnergyExpansion


class InheritedCoalPowerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.arm = {"name": "long-handed-inserter", "position": {"x": -63.5, "y": 16.5}, "direction": 12}
        self.pole = {"name": "small-electric-pole", "position": {"x": -63.5, "y": 14.5}, "direction": 0}
        self.drill = {"name": "burner-mining-drill", "position": {"x": -65, "y": -1}, "direction": 4}
        self.old = {"ok": True, "entities": [self.arm, self.pole], "ports": []}
        self.plan = {"ok": True, "drill": self.drill, "entities": [self.drill, self.arm], "ports": []}
        self.factory = SimpleNamespace(state={"blocks": {"energy:feed:3": deepcopy(self.old)}},
            register_plan=Mock(side_effect=lambda key, plan, obs: deepcopy(plan)),
            ensure_power_connection=Mock(return_value={"status": "succeeded"}))
        self.builder = SimpleNamespace(ensure_plan=Mock(), _seed=Mock(return_value=None))
        self.energy = EnergyExpansion(SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name))),
                                      Mock(), self.builder, self.factory, SimpleNamespace(fingerprint="catalog"))
        self.energy.state = {"feeds": [{"plan": deepcopy(self.plan), "complete": True, "seeded": True}]}
        self.obs = {"world_id": "world", "tick": 100}
        self.row = {"remaining": 1000, "fuel": 100, "belt_coal": 20}

    def test_missing_shared_pole_is_normally_rebuilt_without_rebuilding_retired_drill(self):
        action = {"type": "build", **self.pole}
        self.builder.ensure_plan.side_effect = lambda obs, plan: action if self.pole in plan["entities"] else {"status": "succeeded"}
        self.assertEqual(self.energy._ensure_feed(self.obs, 0, self.row), action)
        plan = self.energy.state["feeds"][0]["plan"]
        self.assertEqual(plan["entities"], [self.drill, self.arm, self.pole])
        self.assertEqual(plan["required_items"]["small-electric-pole"], 1)
        self.assertEqual(self.factory.state["blocks"]["energy:feed:3"], self.old)
        self.factory.ensure_power_connection.assert_not_called()
        self.builder.ensure_plan.side_effect = None
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.assertIsNone(self.energy._ensure_feed(self.obs, 0, self.row))
        self.factory.ensure_power_connection.assert_called_once_with(self.obs, "energy:feed:0", plan)
        self.factory.register_plan.assert_called_once()

    def test_failed_reservation_or_different_arm_identity_cannot_change_feed(self):
        self.factory.register_plan.return_value = {"ok": False, "reason": "reserved collision"}
        self.factory.register_plan.side_effect = None
        self.assertEqual(self.energy._ensure_feed(self.obs, 0, self.row)["reason"], "reserved collision")
        self.assertEqual(self.energy.state["feeds"][0]["plan"], self.plan)
        self.builder.ensure_plan.assert_not_called()
        self.factory.state["blocks"]["energy:feed:3"]["entities"][0]["direction"] = 4
        self.factory.register_plan.reset_mock()
        self.assertIsNone(self.energy._inherit_feed_power(self.obs, 0))
        self.factory.register_plan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
