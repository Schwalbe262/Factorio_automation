import unittest

from factorio_ai.planner import (
    AutomationScienceSkill,
    BeltSmeltingLineSkill,
    CopperPlateSkill,
    ElectronicCircuitSkill,
    IronPlateSkill,
)


def base_observation():
    return {
        "tick": 1,
        "player": {"position": {"x": 0, "y": 0}},
        "inventory": {
            "coal": 10,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
        },
        "craftable": {},
        "resources": [
            {"name": "iron-ore", "position": {"x": 4, "y": 0}, "distance": 4},
            {"name": "copper-ore", "position": {"x": 8, "y": 0}, "distance": 8},
            {"name": "coal", "position": {"x": 2, "y": 0}, "distance": 2},
        ],
        "entities": [],
    }


class PlannerTests(unittest.TestCase):
    def test_done_when_target_reached(self):
        obs = base_observation()
        obs["inventory"]["iron-plate"] = 10
        decision = IronPlateSkill(target_count=10).next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_places_burner_mining_drill_first(self):
        decision = IronPlateSkill(target_count=10).next_action(base_observation())
        self.assertFalse(decision.done)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-mining-drill")
        self.assertTrue(decision.action["allow_nearby"])

    def test_moves_next_to_ore_before_building_distant_drill(self):
        obs = base_observation()
        obs["resources"][0]["position"] = {"x": 100, "y": 0}
        obs["resources"][0]["distance"] = 100
        decision = IronPlateSkill(target_count=10).next_action(obs)
        self.assertEqual(decision.action["type"], "move_to")
        self.assertEqual(decision.action["position"], {"x": 102.0, "y": 0.0})

    def test_mines_ore_when_drill_and_furnace_exist(self):
        obs = base_observation()
        obs["inventory"].pop("burner-mining-drill")
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 101,
                "position": {"x": 4, "y": 0},
                "distance": 4,
                "inventories": {},
            },
            {
                "name": "stone-furnace",
                "unit_number": 102,
                "position": {"x": 5, "y": 0},
                "distance": 5,
                "inventories": {},
            },
        ]
        decision = IronPlateSkill(target_count=10).next_action(obs)
        self.assertEqual(decision.action["type"], "mine")
        self.assertEqual(decision.action["name"], "iron-ore")

    def test_iron_skill_ignores_distant_empty_furnace(self):
        obs = base_observation()
        obs["player"]["position"] = {"x": 102, "y": 0}
        obs["inventory"] = {"coal": 8, "stone-furnace": 1}
        obs["resources"][0]["position"] = {"x": 100, "y": 0}
        obs["resources"][0]["distance"] = 2
        obs["entities"] = [
            {
                "name": "burner-mining-drill",
                "unit_number": 101,
                "position": {"x": 100, "y": 0},
                "distance": 2,
                "inventories": {},
            },
            {
                "name": "stone-furnace",
                "unit_number": 102,
                "position": {"x": 0, "y": 0},
                "distance": 102,
                "inventories": {},
            },
        ]
        decision = IronPlateSkill(target_count=10).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "stone-furnace")
        self.assertEqual(decision.action["position"], {"x": 103.0, "y": 0.0})

    def test_science_skill_crafts_science_when_prerequisites_exist(self):
        obs = base_observation()
        obs["inventory"] = {
            "iron-plate": 10,
            "iron-gear-wheel": 1,
            "copper-plate": 1,
        }
        obs["craftable"] = {"automation-science-pack": 1}
        decision = AutomationScienceSkill(target_count=1).next_action(obs)
        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "automation-science-pack")

    def test_science_skill_builds_second_furnace_for_copper(self):
        obs = base_observation()
        obs["inventory"] = {
            "iron-plate": 10,
            "stone-furnace": 1,
            "coal": 8,
        }
        obs["entities"] = [
            {
                "name": "stone-furnace",
                "unit_number": 201,
                "position": {"x": 4, "y": 0},
                "distance": 4,
                "inventories": {"2": {"iron-plate": 1}},
            }
        ]
        decision = AutomationScienceSkill(target_count=1).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "stone-furnace")

    def test_science_skill_takes_copper_from_furnace_before_waiting(self):
        obs = base_observation()
        obs["inventory"] = {
            "iron-plate": 10,
            "iron-gear-wheel": 1,
            "coal": 8,
            "automation-science-pack": 4,
        }
        obs["entities"] = [
            {
                "name": "stone-furnace",
                "unit_number": 201,
                "position": {"x": 4, "y": 0},
                "distance": 4,
                "inventories": {"3": {"copper-plate": 4}},
            }
        ]
        decision = AutomationScienceSkill(target_count=5).next_action(obs)
        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "copper-plate")

    def test_copper_skill_done_when_target_reached(self):
        obs = base_observation()
        obs["inventory"]["copper-plate"] = 10
        decision = CopperPlateSkill(target_count=10).next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_copper_skill_uses_single_empty_furnace(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 8, "copper-ore": 8}
        obs["entities"] = [
            {
                "name": "stone-furnace",
                "unit_number": 301,
                "position": {"x": 9, "y": 0},
                "distance": 9,
                "inventories": {},
            }
        ]
        decision = CopperPlateSkill(target_count=5).next_action(obs)
        self.assertEqual(decision.action["type"], "insert")
        self.assertEqual(decision.action["item"], "copper-ore")

    def test_copper_skill_builds_furnace_when_only_iron_furnace_exists(self):
        obs = base_observation()
        obs["inventory"] = {"iron-plate": 10, "stone-furnace": 1, "coal": 8}
        obs["entities"] = [
            {
                "name": "stone-furnace",
                "unit_number": 302,
                "position": {"x": 4, "y": 0},
                "distance": 4,
                "inventories": {"2": {"iron-ore": 4}},
            }
        ]
        decision = CopperPlateSkill(target_count=5).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "stone-furnace")

    def test_electronic_circuit_skill_done_when_target_reached(self):
        obs = base_observation()
        obs["inventory"]["electronic-circuit"] = 5
        decision = ElectronicCircuitSkill(target_count=5).next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_electronic_circuit_skill_crafts_circuits_when_ready(self):
        obs = base_observation()
        obs["inventory"] = {"iron-plate": 5, "copper-cable": 15}
        obs["craftable"] = {"electronic-circuit": 5}
        decision = ElectronicCircuitSkill(target_count=5).next_action(obs)
        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "electronic-circuit")

    def test_electronic_circuit_skill_crafts_cable_before_circuit(self):
        obs = base_observation()
        obs["inventory"] = {"iron-plate": 5, "copper-plate": 8}
        obs["craftable"] = {"copper-cable": 8}
        decision = ElectronicCircuitSkill(target_count=5).next_action(obs)
        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "copper-cable")

    def test_electronic_circuit_skill_takes_iron_from_furnace_for_inventory_target(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 8, "copper-cable": 15}
        obs["entities"] = [
            {
                "name": "stone-furnace",
                "unit_number": 401,
                "position": {"x": 4, "y": 0},
                "distance": 4,
                "inventories": {"3": {"iron-plate": 5}},
            }
        ]
        decision = ElectronicCircuitSkill(target_count=5).next_action(obs)
        self.assertEqual(decision.action["type"], "take")
        self.assertEqual(decision.action["item"], "iron-plate")

    def test_electronic_circuit_skill_requests_copper_plate_when_cable_cannot_be_crafted(self):
        obs = base_observation()
        obs["inventory"] = {"iron-plate": 5, "coal": 8, "stone-furnace": 1}
        decision = ElectronicCircuitSkill(target_count=5).next_action(obs)
        self.assertIn(decision.action["type"], {"build", "mine", "move_to", "insert", "take", "wait"})

    def test_belt_smelting_skill_crafts_belt_when_missing(self):
        obs = base_observation()
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
            "iron-plate": 2,
            "iron-gear-wheel": 1,
        }
        obs["craftable"] = {"transport-belt": 1}
        decision = BeltSmeltingLineSkill(target_count=10).next_action(obs)
        self.assertEqual(decision.action["type"], "craft")
        self.assertEqual(decision.action["recipe"], "transport-belt")

    def test_belt_smelting_skill_places_belt_first_when_parts_exist(self):
        obs = base_observation()
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
            "transport-belt": 2,
        }
        decision = BeltSmeltingLineSkill(target_count=10).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "transport-belt")
        self.assertEqual(decision.action["direction"], 4)

    def test_belt_smelting_skill_places_inserter_with_pickup_from_belt(self):
        obs = base_observation()
        obs["inventory"] = {
            "coal": 12,
            "burner-mining-drill": 1,
            "stone-furnace": 1,
            "burner-inserter": 1,
        }
        obs["entities"] = [
            {
                "name": "transport-belt",
                "unit_number": 501,
                "position": {"x": 6, "y": 0},
                "distance": 6,
                "inventories": {},
            },
            {
                "name": "transport-belt",
                "unit_number": 502,
                "position": {"x": 7, "y": 0},
                "distance": 7,
                "inventories": {},
            },
        ]
        decision = BeltSmeltingLineSkill(target_count=10).next_action(obs)
        self.assertEqual(decision.action["type"], "build")
        self.assertEqual(decision.action["name"], "burner-inserter")
        self.assertEqual(decision.action["direction"], 12)

    def test_belt_smelting_skill_done_after_line_outputs_plates(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 12}
        obs["entities"] = [
            {
                "name": "transport-belt",
                "unit_number": 501,
                "position": {"x": 6, "y": 0},
                "distance": 6,
                "inventories": {},
            },
            {
                "name": "transport-belt",
                "unit_number": 505,
                "position": {"x": 7, "y": 0},
                "distance": 7,
                "inventories": {},
            },
            {
                "name": "burner-inserter",
                "unit_number": 502,
                "position": {"x": 8, "y": 0},
                "distance": 8,
                "inventories": {},
            },
            {
                "name": "stone-furnace",
                "unit_number": 503,
                "position": {"x": 9, "y": 0},
                "distance": 9,
                "inventories": {"3": {"iron-plate": 10}},
            },
            {
                "name": "burner-mining-drill",
                "unit_number": 504,
                "position": {"x": 4, "y": 0},
                "distance": 4,
                "inventories": {},
            },
        ]
        decision = BeltSmeltingLineSkill(target_count=10).next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)

    def test_belt_smelting_skill_done_after_line_starts_and_inventory_has_target(self):
        obs = base_observation()
        obs["inventory"] = {"coal": 12, "iron-plate": 10}
        obs["entities"] = [
            {
                "name": "transport-belt",
                "unit_number": 501,
                "position": {"x": 6, "y": 0},
                "distance": 6,
                "inventories": {},
            },
            {
                "name": "transport-belt",
                "unit_number": 505,
                "position": {"x": 7, "y": 0},
                "distance": 7,
                "inventories": {},
            },
            {
                "name": "burner-inserter",
                "unit_number": 502,
                "position": {"x": 8, "y": 0},
                "distance": 8,
                "inventories": {},
            },
            {
                "name": "stone-furnace",
                "unit_number": 503,
                "position": {"x": 9, "y": 0},
                "distance": 9,
                "inventories": {"2": {"iron-ore": 1}},
            },
            {
                "name": "burner-mining-drill",
                "unit_number": 504,
                "position": {"x": 4, "y": 0},
                "distance": 4,
                "inventories": {},
            },
        ]
        decision = BeltSmeltingLineSkill(target_count=10).next_action(obs)
        self.assertTrue(decision.done)
        self.assertIsNone(decision.action)


if __name__ == "__main__":
    unittest.main()
