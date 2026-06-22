import unittest

from factorio_ai.factory_readiness import build_factory_readiness


def _automation_research() -> dict:
    return {"technologies": {"automation": {"researched": True}}}


def _powered_mall_observation() -> dict:
    return {
        "player": {"position": {"x": 0, "y": 0}, "character_valid": False},
        "inventory": {},
        "entities": [
            {
                "name": "assembling-machine-1",
                "unit_number": 10,
                "recipe": "iron-gear-wheel",
                "position": {"x": 0, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            },
            {
                "name": "assembling-machine-1",
                "unit_number": 11,
                "recipe": "transport-belt",
                "position": {"x": 4, "y": 0},
                "electric_network_connected": True,
                "inventories": {},
            },
            {
                "name": "stone-furnace",
                "unit_number": 20,
                "recipe": "iron-plate",
                "position": {"x": 10, "y": 0},
                "inventories": {"2": {"iron-plate": 8}},
            },
        ],
        "research": _automation_research(),
    }


class FactoryReadinessTests(unittest.TestCase):
    def test_virtual_agent_allows_bootstrap_seed_only_when_belt_line_unbuildable(self):
        obs = _powered_mall_observation()

        readiness = build_factory_readiness(obs)

        self.assertTrue(readiness.virtual_agent)
        self.assertTrue(readiness.bootstrap_seed_allowed)
        self.assertEqual(readiness.failure_root, "belt_line_unbuildable")
        self.assertEqual(readiness.repair_skill, "bootstrap_build_item_mall")

        obs["inventory"] = {"transport-belt": 12}
        ready_with_belts = build_factory_readiness(obs)
        self.assertFalse(ready_with_belts.bootstrap_seed_allowed)
        self.assertTrue(ready_with_belts.belt_line_buildable)

    def test_real_player_does_not_allow_seed_even_when_belts_missing(self):
        obs = _powered_mall_observation()
        obs["player"] = {"position": {"x": 0, "y": 0}, "character_valid": True}

        readiness = build_factory_readiness(obs)

        self.assertFalse(readiness.virtual_agent)
        self.assertFalse(readiness.bootstrap_seed_allowed)
        self.assertEqual(readiness.repair_skill, "build_gear_belt_mall_logistics")

    def test_buffered_belts_do_not_hide_incomplete_gear_belt_connection(self):
        obs = _powered_mall_observation()
        obs["entities"].append(
            {
                "name": "wooden-chest",
                "unit_number": 31,
                "position": {"x": 20, "y": 20},
                "inventories": {"1": {"transport-belt": 12}},
            }
        )

        readiness = build_factory_readiness(obs)

        self.assertTrue(readiness.belt_line_buildable)
        self.assertFalse(readiness.gear_belt_logistics_connection_ready)
        self.assertEqual(readiness.failure_root, "gear_belt_logistics_incomplete")
        self.assertEqual(readiness.repair_skill, "build_gear_belt_mall_logistics")

    def test_sufficient_buffered_belts_allow_next_step_before_connection_cleanup(self):
        obs = _powered_mall_observation()
        obs["entities"].append(
            {
                "name": "wooden-chest",
                "unit_number": 31,
                "position": {"x": 20, "y": 20},
                "inventories": {"1": {"transport-belt": 34}},
            }
        )

        readiness = build_factory_readiness(obs)

        self.assertTrue(readiness.belt_line_buildable)
        self.assertFalse(readiness.gear_belt_logistics_connection_ready)
        self.assertIsNone(readiness.failure_root)
        self.assertIsNone(readiness.repair_skill)

    def test_direct_transfer_inserter_satisfies_gear_belt_connection(self):
        obs = _powered_mall_observation()
        obs["entities"].append(
            {
                "name": "wooden-chest",
                "unit_number": 31,
                "position": {"x": 20, "y": 20},
                "inventories": {"1": {"transport-belt": 12}},
            }
        )
        obs["entities"].append(
            {
                "name": "inserter",
                "unit_number": 30,
                "position": {"x": 2, "y": 0},
                "direction": 4,
                "electric_network_connected": True,
            }
        )

        readiness = build_factory_readiness(obs)

        self.assertTrue(readiness.gear_belt_logistics_connection_ready)
        self.assertIsNone(readiness.failure_root)

    def test_missing_mall_maps_to_bootstrap_repair(self):
        obs = _powered_mall_observation()
        obs["entities"] = [entity for entity in obs["entities"] if entity.get("recipe") != "iron-gear-wheel"]

        readiness = build_factory_readiness(obs)

        self.assertFalse(readiness.gear_mall_exists)
        self.assertEqual(readiness.failure_root, "gear_mall_missing")
        self.assertEqual(readiness.repair_skill, "bootstrap_build_item_mall")

    def test_non_logistics_belt_mall_pair_maps_belt_line_to_bootstrap_repair(self):
        obs = _powered_mall_observation()
        for entity in obs["entities"]:
            if entity.get("recipe") == "transport-belt":
                entity["position"] = {"x": 0, "y": -3}

        readiness = build_factory_readiness(obs)

        self.assertTrue(readiness.gear_mall_exists)
        self.assertTrue(readiness.belt_mall_exists)
        self.assertEqual(readiness.failure_root, "belt_line_unbuildable")
        self.assertEqual(readiness.repair_skill, "bootstrap_build_item_mall")
        self.assertIn("gear/belt mall logistics pair", readiness.blocked_by)
        self.assertFalse(readiness.details["gear_belt_logistics_pair_exists"])

    def test_missing_iron_source_maps_to_iron_repair(self):
        obs = _powered_mall_observation()
        obs["entities"] = [entity for entity in obs["entities"] if entity.get("name") != "stone-furnace"]

        readiness = build_factory_readiness(obs)

        self.assertFalse(readiness.iron_plate_source_ready)
        self.assertEqual(readiness.failure_root, "iron_plate_source_missing")
        self.assertEqual(readiness.repair_skill, "produce_iron_plate")

    def test_empty_unfueled_coal_supply_preempts_mall_bootstrap(self):
        obs = _powered_mall_observation()
        obs["inventory"] = {}
        obs["entities"].append(
            {
                "name": "burner-mining-drill",
                "unit_number": 40,
                "position": {"x": 8, "y": 8},
                "mining_target": "coal",
                "status_name": "no_fuel",
                "inventories": {},
            }
        )
        for entity in obs["entities"]:
            if entity.get("name") == "stone-furnace":
                entity["inventories"] = {"1": {"coal": 20}, "2": {"iron-plate": 8}}
        obs["entities"] = [entity for entity in obs["entities"] if entity.get("recipe") != "transport-belt"]

        readiness = build_factory_readiness(obs)

        self.assertFalse(readiness.details["coal_supply_ready"])
        self.assertTrue(readiness.details["starter_fuel_starved"])
        self.assertEqual(readiness.failure_root, "starter_fuel_supply_starved")
        self.assertEqual(readiness.repair_skill, "setup_coal_supply")

    def test_inventory_coal_stock_allows_mall_bootstrap_repair(self):
        obs = _powered_mall_observation()
        obs["inventory"] = {"coal": 12}
        obs["entities"].append(
            {
                "name": "burner-mining-drill",
                "unit_number": 40,
                "position": {"x": 8, "y": 8},
                "mining_target": "coal",
                "status_name": "no_fuel",
                "inventories": {},
            }
        )
        obs["entities"] = [entity for entity in obs["entities"] if entity.get("recipe") != "transport-belt"]

        readiness = build_factory_readiness(obs)

        self.assertTrue(readiness.details["coal_supply_ready"])
        self.assertFalse(readiness.details["starter_fuel_starved"])
        self.assertEqual(readiness.failure_root, "belt_mall_missing")
        self.assertEqual(readiness.repair_skill, "bootstrap_build_item_mall")


if __name__ == "__main__":
    unittest.main()
