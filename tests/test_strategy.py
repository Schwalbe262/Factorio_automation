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

    def test_rocket_goal_researches_automation_after_iron(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20},
                "entities": [],
            },
        )
        self.assertEqual(result["selected_skill"], "research_automation")

    def test_rocket_goal_requests_next_executor_after_automation_researched(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20},
                "entities": [],
                "research": {
                    "technologies": {
                        "automation": {"researched": True},
                    },
                },
            },
        )
        self.assertEqual(result["selected_skill"], "automate_electronic_circuit_line")

    def test_rocket_goal_researches_logistics_after_circuit_cell_ready(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20, "electronic-circuit": 6},
                "entities": [
                    {
                        "name": "assembling-machine-1",
                        "recipe": "copper-cable",
                        "electric_network_connected": True,
                    },
                    {
                        "name": "assembling-machine-1",
                        "recipe": "electronic-circuit",
                        "electric_network_connected": True,
                    },
                ],
                "research": {
                    "technologies": {
                        "automation": {"researched": True},
                        "logistics": {"researched": False},
                    },
                },
            },
        )
        self.assertEqual(result["selected_skill"], "research_logistics")

    def test_electronic_circuit_bottleneck_uses_automation_when_available(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20, "electronic-circuit": 1},
                "entities": [],
                "research": {
                    "technologies": {
                        "automation": {"researched": True},
                    },
                },
            },
            production_targets={"electronic-circuit": 20.0},
        )
        self.assertEqual(result["selected_skill"], "automate_electronic_circuit_line")

    def test_copper_target_bottleneck_expands_copper_smelting(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 20, "copper-plate": 1},
                "entities": [],
            },
            production_targets={"copper-plate": 45.0},
        )
        self.assertEqual(result["selected_skill"], "expand_copper_smelting")

    def test_normalize_rejects_unknown_skill(self):
        result = normalize_strategy_response({"selected_skill": "teleport_to_rocket", "priority": 100})
        self.assertEqual(result["selected_skill"], "launch_rocket_program")

    def test_catalog_exposes_llm_scope(self):
        catalog = skill_catalog_payload()
        self.assertTrue(any(item["name"] == "produce_electronic_circuit" for item in catalog))
        self.assertTrue(any(item["name"] == "build_belt_smelting_line" for item in catalog))
        self.assertTrue(any(item["name"] == "expand_copper_smelting" for item in catalog))
        self.assertTrue(any(item["name"] == "research_automation" for item in catalog))
        self.assertTrue(any(item["name"] == "automate_electronic_circuit_line" for item in catalog))
        self.assertTrue(any(item["name"] == "research_logistics" for item in catalog))
        self.assertTrue(any(item["name"] == "build_starter_defense" for item in catalog))
        self.assertTrue(any(item["name"] == "build_rail_supply_line" for item in catalog))
        self.assertEqual(
            next(item for item in catalog if item["name"] == "produce_electronic_circuit")["executor"],
            "ElectronicCircuitSkill",
        )
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
            payload["spatial_planning"]["rail_network"]["planning_inputs"]["rail_candidate_distance_tiles"],
            160,
        )
        self.assertEqual(
            payload["spatial_planning"]["rail_network"]["planning_inputs"]["known_remote_resources"][0]["name"],
            "iron-ore",
        )

    def test_strategy_payload_exposes_enemy_threat_context(self):
        payload = make_strategy_payload(
            "launch_rocket_program",
            {
                "player": {"position": {"x": 0, "y": 0}},
                "inventory": {},
                "entities": [],
                "resources": [],
                "enemies": [
                    {"name": "small-biter", "type": "unit", "distance": 22, "position": {"x": 20, "y": 0}},
                    {"name": "biter-spawner", "type": "unit-spawner", "distance": 140, "position": {"x": 140, "y": 0}},
                ],
            },
        )
        self.assertEqual(payload["threats"]["enemy_count"], 2)
        self.assertEqual(payload["threats"]["danger_level"], "critical")
        self.assertEqual(payload["threats"]["nearest_enemy"]["name"], "small-biter")
        self.assertEqual(payload["threats"]["nearest_spawner"]["name"], "biter-spawner")
        self.assertEqual(payload["threats"]["armed_gun_turret_count"], 0)

    def test_critical_enemy_threat_requests_defense_skill(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 100},
                "entities": [],
                "enemies": [{"name": "small-biter", "type": "unit", "distance": 20}],
            },
        )
        self.assertEqual(result["selected_skill"], "build_starter_defense")
        self.assertIn("enemy threat", result["blockers"])

    def test_armed_starter_turret_prevents_repeated_defense_loop(self):
        result = heuristic_strategy(
            "launch_rocket_program",
            {
                "inventory": {"iron-plate": 100},
                "entities": [
                    {
                        "name": "gun-turret",
                        "unit_number": 10,
                        "position": {"x": 0, "y": 0},
                        "inventories": {"1": {"firearm-magazine": 10}},
                    }
                ],
                "enemies": [{"name": "small-biter", "type": "unit", "distance": 70}],
            },
        )
        self.assertNotEqual(result["selected_skill"], "build_starter_defense")


if __name__ == "__main__":
    unittest.main()
