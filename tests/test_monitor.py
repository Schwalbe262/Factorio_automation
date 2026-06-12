import unittest

from factorio_ai.monitor import estimate_bottlenecks, estimate_net_rates, estimate_production, production_target_status
from factorio_ai.monitor import ConsumptionEstimate, ProductionEstimate


class MonitorTests(unittest.TestCase):
    def test_target_status_detects_satisfied_and_deficit_items(self):
        production = [ProductionEstimate("iron-plate", 30.0, 2, 0.7, [])]
        status = production_target_status({"iron-plate": 20.0, "copper-plate": 10.0}, production)
        self.assertFalse(status["all_satisfied"])
        rows = {item["item"]: item for item in status["items"]}
        self.assertTrue(rows["iron-plate"]["satisfied"])
        self.assertEqual(rows["copper-plate"]["deficit_per_minute"], 10.0)

    def test_bottleneck_includes_target_deficit(self):
        bottlenecks = estimate_bottlenecks(
            "launch_rocket_program",
            {"inventory": {}, "entities": []},
            production=[ProductionEstimate("iron-plate", 5.0, 1, 0.7, [])],
            production_targets={"iron-plate": 20.0},
        )
        self.assertEqual(bottlenecks[0].item, "iron-plate")
        self.assertIn("target deficit", bottlenecks[0].reason)

    def test_net_rate_subtracts_consumption(self):
        net = estimate_net_rates(
            [ProductionEstimate("iron-plate", 30.0, 2, 0.7, [])],
            [ConsumptionEstimate("iron-plate", 12.0, 1, 0.7, [])],
        )
        self.assertEqual(net["iron-plate"], 18.0)

    def test_estimates_powered_assembler_recipe_output(self):
        estimates = estimate_production(
            {
                "entities": [
                    {
                        "name": "assembling-machine-1",
                        "recipe": "electronic-circuit",
                        "electric_network_connected": True,
                        "inventories": {},
                    }
                ]
            }
        )
        by_item = {item.item: item for item in estimates}
        self.assertIn("electronic-circuit", by_item)
        self.assertEqual(by_item["electronic-circuit"].per_minute, 60.0)

    def test_ignores_unpowered_assembler_output(self):
        estimates = estimate_production(
            {
                "entities": [
                    {
                        "name": "assembling-machine-1",
                        "recipe": "electronic-circuit",
                        "electric_network_connected": False,
                        "inventories": {},
                    }
                ]
            }
        )
        self.assertEqual(estimates, [])

    def test_estimates_complete_belt_iron_smelting_line(self):
        estimates = estimate_production(
            {
                "entities": [
                    {"name": "burner-mining-drill", "position": {"x": 4, "y": 0}, "inventories": {"1": {"coal": 1}}},
                    {"name": "transport-belt", "position": {"x": 6, "y": 0}, "inventories": {}},
                    {"name": "transport-belt", "position": {"x": 7, "y": 0}, "inventories": {}},
                    {"name": "burner-inserter", "position": {"x": 8, "y": 0}, "inventories": {"1": {"coal": 1}}},
                    {"name": "stone-furnace", "position": {"x": 9, "y": 0}, "inventories": {"1": {"coal": 1}}},
                ],
                "resources": [],
            }
        )
        by_item = {item.item: item for item in estimates}
        self.assertIn("iron-plate", by_item)
        self.assertEqual(by_item["iron-plate"].per_minute, 18.75)
        self.assertNotIn("copper-plate", by_item)

    def test_ignores_unfueled_complete_belt_smelting_line(self):
        estimates = estimate_production(
            {
                "entities": [
                    {"name": "burner-mining-drill", "position": {"x": 4, "y": 0}, "inventories": {}},
                    {"name": "transport-belt", "position": {"x": 6, "y": 0}, "inventories": {}},
                    {"name": "transport-belt", "position": {"x": 7, "y": 0}, "inventories": {}},
                    {"name": "burner-inserter", "position": {"x": 8, "y": 0}, "inventories": {}},
                    {"name": "stone-furnace", "position": {"x": 9, "y": 0}, "inventories": {}},
                ],
                "resources": [],
            }
        )
        by_item = {item.item: item for item in estimates}
        self.assertNotIn("iron-plate", by_item)

    def test_ignores_unfueled_stone_furnace_inventory_output(self):
        estimates = estimate_production(
            {
                "entities": [
                    {
                        "name": "stone-furnace",
                        "inventories": {"2": {"iron-ore": 1}, "3": {"iron-plate": 1}},
                    }
                ]
            }
        )
        self.assertEqual(estimates, [])

    def test_estimates_complete_belt_copper_smelting_line(self):
        estimates = estimate_production(
            {
                "entities": [
                    {"name": "burner-mining-drill", "position": {"x": 8, "y": 0}, "inventories": {"1": {"coal": 1}}},
                    {"name": "transport-belt", "position": {"x": 10, "y": 0}, "inventories": {}},
                    {"name": "transport-belt", "position": {"x": 11, "y": 0}, "inventories": {}},
                    {"name": "burner-inserter", "position": {"x": 12, "y": 0}, "inventories": {"1": {"coal": 1}}},
                    {"name": "stone-furnace", "position": {"x": 13, "y": 0}, "inventories": {"1": {"coal": 1}}},
                ],
                "resources": [{"name": "copper-ore", "position": {"x": 8, "y": 0}}],
            }
        )
        by_item = {item.item: item for item in estimates}
        self.assertIn("copper-plate", by_item)
        self.assertEqual(by_item["copper-plate"].per_minute, 18.75)
        self.assertNotIn("iron-plate", by_item)


if __name__ == "__main__":
    unittest.main()
