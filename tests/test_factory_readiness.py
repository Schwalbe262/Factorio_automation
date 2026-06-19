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
        self.assertEqual(readiness.repair_skill, "build_gear_belt_mall_logistics")

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

    def test_missing_mall_maps_to_bootstrap_repair(self):
        obs = _powered_mall_observation()
        obs["entities"] = [entity for entity in obs["entities"] if entity.get("recipe") != "iron-gear-wheel"]

        readiness = build_factory_readiness(obs)

        self.assertFalse(readiness.gear_mall_exists)
        self.assertEqual(readiness.failure_root, "gear_mall_missing")
        self.assertEqual(readiness.repair_skill, "bootstrap_build_item_mall")

    def test_missing_iron_source_maps_to_iron_repair(self):
        obs = _powered_mall_observation()
        obs["entities"] = [entity for entity in obs["entities"] if entity.get("name") != "stone-furnace"]

        readiness = build_factory_readiness(obs)

        self.assertFalse(readiness.iron_plate_source_ready)
        self.assertEqual(readiness.failure_root, "iron_plate_source_missing")
        self.assertEqual(readiness.repair_skill, "produce_iron_plate")


if __name__ == "__main__":
    unittest.main()
