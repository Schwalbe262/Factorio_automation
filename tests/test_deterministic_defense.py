from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_defense import DeterministicDefense


def entity(name, x=0, y=0, **kwargs):
    return {"name": name, "position": {"x": x, "y": y}, "health": 200, "inventory": {}, **kwargs}


def observation(**kwargs):
    return {"ok": True, "tick": 100, "world_id": "world", "inventory": {},
            "enabled_recipes": {"gun-turret": True}, "technologies": {},
            "entities": [entity("lab")], **kwargs}


class DeterministicDefenseTests(unittest.TestCase):
    def setUp(self):
        self.game = Mock()
        self.bootstrap = Mock()
        self.bootstrap.ensure_item.side_effect = lambda obs, item, count: {"type": "craft", "recipe": item, "count": count}
        self.enemies = []
        self.site = {"ok": True, "position": {"x": -6, "y": 0}}
        def query(body):
            if "attack_parameters.range" in body:
                return {"range": 18}
            if "local anchors=" in body:
                return {"ok": True, "enemies": self.enemies}
            if "local args=" in body:
                return self.site
            raise AssertionError("unexpected query")
        self.game.query.side_effect = query
        self.catalog = SimpleNamespace(technologies={"turret-unlock": {"unlocks": ["gun-turret"]}})
        self.driver = DeterministicDefense(self.game, self.bootstrap, self.catalog)

    def test_empty_world_needs_no_defense_queries_or_construction(self):
        result = self.driver.next_action(observation(entities=[]))
        self.assertEqual(result["status"], "succeeded")
        self.game.query.assert_not_called()
        self.bootstrap.ensure_item.assert_not_called()

    def test_ammunition_is_acquired_before_building_exposed_turret(self):
        obs = observation(inventory={"gun-turret": 1})
        result = self.driver.next_action(obs)
        self.assertEqual(result, {"type": "craft", "recipe": "firearm-magazine", "count": 20})
        self.bootstrap.ensure_item.assert_called_once_with(obs, "firearm-magazine", 20)
        self.game.act.assert_not_called()

    def test_missing_turret_uses_normal_material_acquisition(self):
        obs = observation(inventory={"firearm-magazine": 20})
        result = self.driver.next_action(obs)
        self.assertEqual((result["recipe"], result["count"]), ("gun-turret", 1))
        self.bootstrap.ensure_item.assert_called_once_with(obs, "gun-turret", 1)

    def test_build_uses_live_collision_checked_geometry(self):
        self.site = {"ok": True, "position": {"x": 123, "y": -40}}
        result = self.driver.next_action(observation(inventory={"gun-turret": 1, "firearm-magazine": 20}))
        self.assertEqual(result["type"], "build")
        self.assertEqual(result["position"], self.site["position"])
        self.assertEqual(result["name"], "gun-turret")

    def test_empty_turret_is_armed_before_more_perimeter_expansion(self):
        turret = entity("gun-turret", -6, 0)
        result = self.driver.next_action(observation(inventory={"firearm-magazine": 3},
            entities=[entity("lab"), entity("burner-mining-drill", 100, 100), turret]))
        self.assertEqual((result["type"], result["inventory"], result["count"]), ("insert", "turret_ammo", 3))
        self.assertEqual(result["position"], turret["position"])

    def test_low_ammo_refill_produces_only_missing_target(self):
        obs = observation(entities=[entity("lab"), entity("gun-turret", -6, inventory={"firearm-magazine": 4})])
        result = self.driver.next_action(obs)
        self.assertEqual(result["recipe"], "firearm-magazine")
        self.bootstrap.ensure_item.assert_called_once_with(obs, "firearm-magazine", 16)

    def test_built_but_unarmed_turret_is_not_success(self):
        obs = observation(entities=[entity("lab"), entity("gun-turret", -6)])
        self.assertNotEqual(self.driver.next_action(obs).get("status"), "succeeded")

    def test_managed_turret_keeps_coverage_without_perpetual_hand_refill(self):
        self.driver.automatic_ammo = lambda turret: True
        obs = observation(entities=[entity("lab"), entity("gun-turret", -6, inventory={"firearm-magazine": 3})])
        result = self.driver.next_action(obs)
        self.assertEqual(result["status"], "succeeded")
        self.bootstrap.ensure_item.assert_not_called()

    def test_empty_managed_turret_waits_for_supply_without_claiming_armed_coverage(self):
        self.driver.automatic_ammo = lambda turret: True
        result = self.driver.next_action(observation(entities=[entity("lab"), entity("gun-turret", -6)]))
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["evidence"]["empty_turrets"], 1)
        self.bootstrap.ensure_item.assert_not_called()

    def test_armed_covering_turret_proves_defended_supply(self):
        result = self.driver.next_action(observation(entities=[entity("stone-furnace"),
            entity("burner-mining-drill", 0, 2), entity("gun-turret", -6, inventory={"firearm-magazine": 20})]))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["evidence"]["turret_count"], 1)
        self.bootstrap.ensure_item.assert_not_called()

    def test_distant_turret_does_not_claim_coverage(self):
        obs = observation(entities=[entity("lab"), entity("gun-turret", 100, inventory={"firearm-magazine": 20})])
        self.assertNotEqual(self.driver.next_action(obs).get("status"), "succeeded")

    def test_locked_turrets_yield_actual_research_dependency(self):
        result = self.driver.next_action(observation(enabled_recipes={}))
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["evidence"]["requirements"]["research"], ["turret-unlock"])
        self.bootstrap.ensure_item.assert_not_called()
        self.game.act.assert_not_called()

    def test_near_enemy_without_unlocked_defense_is_explicit_urgent_blocker(self):
        self.enemies = [{"name": "small-biter", "type": "unit", "position": {"x": 20, "y": 0}}]
        result = self.driver.next_action(observation(enabled_recipes={}))
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["evidence"]["urgent"])
        self.game.act.assert_not_called()

    def test_health_decrease_is_observed_but_not_silently_healed(self):
        self.driver.next_action(observation(enabled_recipes={}, entities=[entity("lab", unit_number=1)]))
        result = self.driver.next_action(observation(tick=200, enabled_recipes={}, entities=[entity("lab", health=180, unit_number=1)]))
        self.assertEqual(result["evidence"]["damage_observed"], ["1"])
        self.assertTrue(result["evidence"]["urgent"])
        self.game.act.assert_not_called()

    def test_rollback_does_not_reuse_previous_world_damage(self):
        self.driver.next_action(observation(enabled_recipes={}, entities=[entity("lab", unit_number=1)]))
        result = self.driver.next_action(observation(tick=50, enabled_recipes={}, entities=[entity("lab", health=180, unit_number=1)]))
        self.assertEqual(result["evidence"]["damage_observed"], [])
        self.assertFalse(result["evidence"]["urgent"])

    def test_scattered_producers_create_additional_ammo_production_targets(self):
        requirements = self.driver.requirements(observation(entities=[entity("lab"), entity("burner-mining-drill", 100)]))
        self.assertEqual(requirements["items"]["gun-turret"], 2)
        self.assertEqual(requirements["items"]["firearm-magazine"], 40)
        self.assertGreater(requirements["production_per_minute"]["firearm-magazine"], 0)

    def test_blocked_placement_does_not_spend_construction_materials(self):
        self.site = {"ok": False, "reason": "placement_blocked"}
        result = self.driver.next_action(observation())
        self.assertEqual(result["status"], "blocked")
        self.bootstrap.ensure_item.assert_not_called()


if __name__ == "__main__":
    unittest.main()
