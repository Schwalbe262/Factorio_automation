import unittest

from factorio_ai.strategy import heuristic_strategy, make_strategy_payload, normalize_strategy_response, skill_catalog_payload


class StrategyTests(unittest.TestCase):
    def test_electronic_circuit_goal_detects_iron_bottleneck(self):
        result = heuristic_strategy(
            "전자회로를 만들어야함",
            {
                "inventory": {"iron-plate": 2, "copper-plate": 40},
                "entities": [],
            },
        )
        self.assertEqual(result["selected_skill"], "expand_iron_smelting")
        self.assertIn("iron plate throughput", result["blockers"])

    def test_rocket_goal_starts_with_red_science_after_iron(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20},
                "entities": [],
            },
        )
        self.assertEqual(result["selected_skill"], "produce_automation_science_pack")

    def test_normalize_rejects_unknown_skill(self):
        result = normalize_strategy_response({"selected_skill": "teleport_to_rocket", "priority": 100})
        self.assertEqual(result["selected_skill"], "launch_rocket_program")

    def test_catalog_exposes_llm_scope(self):
        catalog = skill_catalog_payload()
        self.assertTrue(any(item["name"] == "produce_electronic_circuit" for item in catalog))
        self.assertTrue(all("llm_scope" in item for item in catalog))

    def test_strategy_payload_exposes_spatial_planning_context(self):
        payload = make_strategy_payload(
            "launch_rocket_program",
            {
                "player": {"position": {"x": 0, "y": 0}},
                "inventory": {},
                "entities": [{"name": "stone-furnace", "position": {"x": 10, "y": 4}}],
                "resources": [{"name": "iron-ore", "position": {"x": 120, "y": 0}, "amount": 1000}],
            },
        )
        self.assertIn("spatial_planning", payload)
        self.assertIn("site_selection", payload["spatial_planning"])
        self.assertIn("rail_network", payload["spatial_planning"])
        self.assertEqual(
            payload["spatial_planning"]["rail_network"]["planning_inputs"]["known_remote_resources"][0]["name"],
            "iron-ore",
        )


if __name__ == "__main__":
    unittest.main()
