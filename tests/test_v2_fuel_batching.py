"""Hand fuelling must not loop on single coal pieces or feed cells nobody is emptying."""

import unittest
from unittest.mock import Mock

from factorio_agent.construction_supply import FUEL_BATCH, FUEL_SKIP_STOCK, V2Bootstrap


def cell(drill, receiver, **extra):
    return dict(
        drill={k: drill[k] for k in ("name", "position")},
        receiver={k: receiver[k] for k in ("name", "position")},
        remaining=1000,
        **extra,
    )


class FuelBatchingTests(unittest.TestCase):
    def setUp(self):
        self.bootstrap = V2Bootstrap.__new__(V2Bootstrap)
        self.coal_drill = dict(name="burner-mining-drill", position={"x": 1, "y": 1}, inventory={"coal": 1})
        self.coal_chest = dict(name="wooden-chest", position={"x": 1.5, "y": -0.5}, inventory={"coal": 1})
        self.iron_drill = dict(name="burner-mining-drill", position={"x": 9, "y": 1}, inventory={})
        self.iron_furnace = dict(
            name="stone-furnace",
            position={"x": 9, "y": -1},
            inventory={"iron-plate": 10},
            output_inventory={"iron-plate": 10},
        )
        self.obs = dict(
            inventory={},
            resources={"iron-ore": {}, "coal": {}},
            entities=[self.coal_drill, self.coal_chest, self.iron_drill, self.iron_furnace],
        )
        self.coal_cells = [cell(self.coal_drill, self.coal_chest, fuel=1, burning=True)]
        self.iron_cells = [cell(self.iron_drill, self.iron_furnace)]
        self.bootstrap._existing_cells = Mock(
            side_effect=lambda resource: {
                "ok": True,
                "cells": self.coal_cells if resource == "coal" else self.iron_cells,
            }
        )
        self.bootstrap.ensure_item = Mock(return_value={"type": "take", "item": "coal", "count": FUEL_BATCH})

    def test_single_coal_piece_is_left_to_pile_up_while_a_coal_drill_runs(self):
        self.assertIsNone(self.bootstrap.maintain_fuel(self.obs))
        self.bootstrap.ensure_item.assert_not_called()

    def test_a_full_batch_is_fetched_in_one_trip(self):
        self.coal_chest["inventory"]["coal"] = FUEL_BATCH
        action = self.bootstrap.maintain_fuel(self.obs)
        self.assertEqual(action["type"], "take")
        self.assertEqual(self.bootstrap.ensure_item.call_args.args[1:], ("coal", 24))

    def test_stalled_coal_supply_still_uses_the_emergency_path(self):
        self.coal_drill["inventory"] = {}
        self.coal_cells[0].update(fuel=0, burning=False)
        self.bootstrap.maintain_fuel(self.obs)
        self.bootstrap.ensure_item.assert_called_once()

    def test_carried_coal_goes_to_the_coal_drill_first_in_one_insert(self):
        self.coal_drill["inventory"] = {}
        self.obs["inventory"] = {"coal": 12}
        action = self.bootstrap.maintain_fuel(self.obs)
        self.assertEqual(action["type"], "insert")
        self.assertEqual(action["position"], self.coal_drill["position"])
        self.assertEqual(action["count"], 8)

    def test_cell_with_enough_finished_output_gets_no_fuel(self):
        self.obs["inventory"] = {"coal": 12}
        self.iron_furnace["inventory"] = {"iron-plate": FUEL_SKIP_STOCK}
        self.iron_furnace["output_inventory"] = {"iron-plate": FUEL_SKIP_STOCK}
        self.coal_drill["inventory"] = {"coal": 5}
        self.assertIsNone(self.bootstrap.maintain_fuel(self.obs))

    def test_cell_short_of_output_is_still_fuelled(self):
        self.obs["inventory"] = {"coal": 12}
        self.coal_drill["inventory"] = {"coal": 5}
        action = self.bootstrap.maintain_fuel(self.obs)
        self.assertEqual(action["type"], "insert")
        self.assertIn(action["position"], (self.iron_drill["position"], self.iron_furnace["position"]))

    def test_a_few_carried_pieces_are_not_spent_one_at_a_time(self):
        self.obs["inventory"] = {"coal": 2}
        self.coal_drill["inventory"] = {"coal": 5}
        self.assertIsNone(self.bootstrap.maintain_fuel(self.obs))
        self.coal_chest["inventory"]["coal"] = FUEL_BATCH
        action = self.bootstrap.maintain_fuel(self.obs)
        self.assertEqual(action["type"], "take")
        self.assertEqual(self.bootstrap.ensure_item.call_args.args[1:], ("coal", 2 + 3 * FUEL_BATCH))

    def test_any_job_collecting_fuel_waits_for_a_batch(self):
        from factorio_agent.core.deterministic_bootstrap import DeterministicBootstrap
        from unittest.mock import patch

        self.bootstrap.catalog = None
        self.bootstrap._unreserved_stock_observation = lambda obs, item: obs
        single = {"type": "take", "item": "coal", "count": 1}
        with patch.object(DeterministicBootstrap, "_take_output", return_value=single):
            self.assertIsNone(self.bootstrap._take_output(self.obs, "coal", 16))
            self.assertEqual(self.bootstrap._take_output(self.obs, "coal", 1), single)
        batch = {"type": "take", "item": "coal", "count": FUEL_BATCH}
        with patch.object(DeterministicBootstrap, "_take_output", return_value=batch):
            self.assertEqual(self.bootstrap._take_output(self.obs, "coal", 16), batch)

    def test_stock_check_uses_chest_contents_for_direct_ore_cells(self):
        stone_chest = dict(name="wooden-chest", position={"x": 20.5, "y": 0.5}, inventory={"stone": 200})
        stone_drill = dict(name="burner-mining-drill", position={"x": 20, "y": 2}, inventory={})
        self.obs["entities"] = [self.coal_drill, self.coal_chest, stone_drill, stone_chest]
        self.obs["inventory"] = {"coal": 12}
        self.coal_drill["inventory"] = {"coal": 5}
        self.iron_cells = [cell(stone_drill, stone_chest)]
        self.assertIsNone(self.bootstrap.maintain_fuel(self.obs))


if __name__ == "__main__":
    unittest.main()
