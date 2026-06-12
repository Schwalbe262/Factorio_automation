import unittest

from factorio_ai.monitor import estimate_bottlenecks, estimate_net_rates, production_target_status
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


if __name__ == "__main__":
    unittest.main()
