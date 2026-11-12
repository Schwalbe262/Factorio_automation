from copy import deepcopy
import unittest
from unittest.mock import Mock

from factorio_agent.construction_supply import V2Bootstrap


class CoalRestartPriorityTests(unittest.TestCase):
    def setUp(self):
        self.bootstrap = V2Bootstrap.__new__(V2Bootstrap)
        self.iron = dict(name="stone-furnace", position={"x": 3, "y": 4}, inventory={})
        self.coal = dict(name="burner-mining-drill", position={"x": 5, "y": 6}, inventory={})
        self.obs = dict(inventory={"coal": 1}, entities=[self.iron, self.coal])
        self.bootstrap._existing_cells = Mock(
            return_value={
                "ok": True,
                "cells": [
                    dict(
                        drill={k: self.coal[k] for k in ("name", "position")},
                        receiver={"name": "wooden-chest"},
                        remaining=100,
                    )
                ],
            }
        )
        self.bootstrap.ensure_item = Mock(return_value={"status": "waiting", "reason": "normal_supply_wait"})

    def test_last_real_coal_restarts_producer_before_iron_consumer(self):
        original = deepcopy(self.obs)
        action = self.bootstrap.maintain_fuel(self.obs)
        self.assertEqual(action["position"], self.coal["position"])
        self.assertEqual(action["count"], 1)
        self.assertEqual(action["type"], "insert")
        self.assertEqual(action["item"], "coal")
        self.assertEqual(self.obs, original)
        self.bootstrap.ensure_item.assert_not_called()

    def test_running_stocked_producer_allows_consumer_refueling(self):
        self.coal["inventory"]["coal"] = 2
        action = self.bootstrap.maintain_fuel(self.obs)
        self.assertEqual(action["position"], self.iron["position"])
        self.assertEqual(action["count"], 1)

    def test_empty_carried_fuel_uses_existing_normal_seed_supply_path(self):
        self.obs["inventory"]["coal"] = 0
        result = self.bootstrap.maintain_fuel(self.obs)
        self.assertEqual(result["reason"], "normal_supply_wait")
        self.bootstrap.ensure_item.assert_called_once()
        args = self.bootstrap.ensure_item.call_args.args
        self.assertEqual(args[1:], ("coal", 24))
        self.assertEqual(args[0]["inventory"], self.obs["inventory"])

    def test_satisfied_burners_do_not_add_a_native_survey(self):
        for e in self.obs["entities"]:
            e["inventory"]["coal"] = 2
        self.assertIsNone(self.bootstrap.maintain_fuel(self.obs))
        self.bootstrap._existing_cells.assert_not_called()

    def test_missing_or_exhausted_coal_cells_do_not_become_priority_producers(self):
        for cells in [
            [],
            [dict(drill={k: self.coal[k] for k in ("name", "position")}, receiver=None, remaining=100)],
            [
                dict(
                    drill={k: self.coal[k] for k in ("name", "position")},
                    receiver={"name": "wooden-chest"},
                    remaining=0,
                )
            ],
        ]:
            self.bootstrap._existing_cells.return_value = {"ok": True, "cells": cells}
            self.assertEqual(self.bootstrap.maintain_fuel(self.obs)["position"], self.iron["position"])

    def test_failed_native_survey_does_not_spend_remaining_fuel(self):
        self.bootstrap._existing_cells.return_value = {"ok": False}
        result = self.bootstrap.maintain_fuel(self.obs)
        self.assertEqual(result["status"], "blocked")
        self.bootstrap.ensure_item.assert_not_called()
