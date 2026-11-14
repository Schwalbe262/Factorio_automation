"""A starter-fuel wait does not stop a job's own construction (handlers.upkeep.shared_upkeep).

Fleet aud-1114-0841: "waiting for automated coal output" from the burner fuel upkeep held the
smelting district, the main bus and the mining outposts for minutes while coal cells refilled.
"""

import unittest
from unittest.mock import Mock

from factorio_agent.core.deterministic_bootstrap import FUEL_WAIT, DeterministicBootstrap
from factorio_agent.handlers.upkeep import shared_upkeep


def _bootstrap(fuel):
    bootstrap = DeterministicBootstrap.__new__(DeterministicBootstrap)
    bootstrap._world_id = "w"
    bootstrap._recipes = {}
    bootstrap.maintain_fuel = Mock(return_value=fuel)
    return bootstrap


def _executor(upkeep):
    ex = Mock()
    ex.starter_observation.return_value = {"filtered": True}
    ex.starter_upkeep.return_value = upkeep
    ex._continue_background_construction.return_value = None
    ex.supply_cell = None
    return ex


class FuelWaitTests(unittest.TestCase):
    def test_fuel_wait_is_marked(self):
        wait = {"status": "waiting", "reason": "waiting for automated coal output", "evidence": {}}
        result = _bootstrap(wait).next_action({"ok": True, "world_id": "w", "entities": []})
        self.assertEqual(result["reason"], wait["reason"])
        self.assertTrue(result[FUEL_WAIT])
        self.assertNotIn(FUEL_WAIT, wait)

    def test_fuel_action_is_not_marked(self):
        insert = {"type": "insert", "item": "coal", "count": 8, "reason": "fuel starter production"}
        result = _bootstrap(insert).next_action({"ok": True, "world_id": "w", "entities": []})
        self.assertIs(result, insert)
        self.assertNotIn(FUEL_WAIT, result)

    def test_fuel_blocked_is_not_marked(self):
        blocked = {"status": "blocked", "reason": "starter_coal_priority_survey_failed"}
        result = _bootstrap(blocked).next_action({"ok": True, "world_id": "w", "entities": []})
        self.assertNotIn(FUEL_WAIT, result)

    def test_job_keeps_building_through_a_fuel_wait(self):
        ex = _executor({"status": "waiting", "reason": "waiting for automated coal output", FUEL_WAIT: True})
        early, filtered = shared_upkeep(ex, {"tick": 1}, "research")
        self.assertIsNone(early)
        self.assertEqual(filtered, {"filtered": True})
        ex._continue_background_construction.assert_called_once()

    def test_other_upkeep_waits_still_stop_the_job(self):
        wait = {"status": "waiting", "reason": "waiting for natural 50-iron-plate steam-power unlock"}
        early, _ = shared_upkeep(_executor(wait), {"tick": 1}, "research")
        self.assertIs(early, wait)
        action = {"type": "insert", "item": "coal", "count": 8}
        early, _ = shared_upkeep(_executor(action), {"tick": 1}, "research")
        self.assertIs(early, action)


if __name__ == "__main__":
    unittest.main()
