import unittest

from factorio_agent.power_layout import compact_coal_cell


class CompactCoalGeometryTests(unittest.TestCase):
    def test_drill_output_passes_fuel_pickup_before_main_port(self):
        plan=compact_coal_cell({"x":0,"y":0})
        belts={(e["position"]["x"],e["position"]["y"]):e for e in plan["entities"] if e["name"]=="transport-belt"}
        self.assertEqual(len(belts),4)
        # Native east-facing drill drops at (1.5,-0.5); east-pickup inserter
        # at (1.5,0.5) takes from (2.5,0.5) and drops inside the drill.
        self.assertIn((1.5,-.5),belts)
        self.assertIn((2.5,.5),belts)
        self.assertEqual(plan["ports"][0]["facing"],4)
        inserter=next(e for e in plan["entities"] if e["name"]=="burner-inserter")
        self.assertEqual(inserter["position"],{"x":1.5,"y":.5})
        self.assertEqual(inserter["direction"],4)
        self.assertEqual(plan["required_items"]["transport-belt"],4)


if __name__ == "__main__":
    unittest.main()
