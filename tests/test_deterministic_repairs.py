from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_bootstrap import DeterministicBootstrap
from factorio_ai.deterministic_repairs import NativeRepairs


def entity(**changes):
    return {"name": "lab", "type": "lab", "unit_number": 42, "position": {"x": 2, "y": 0},
            "health": 80, "max_health": 150, "inventory": {}, **changes}


class NativeRepairTests(unittest.TestCase):
    def setUp(self):
        self.game = SimpleNamespace(backend="character", query=Mock(return_value={"ok": True, "quiet": True, "tick": 100}))
        self.bootstrap = Mock()
        self.bootstrap.ensure_item.return_value = {"status": "succeeded"}
        self.factory = SimpleNamespace(priority_research=["military"],
            request_recipe_unlock=Mock(return_value={"status": "waiting", "evidence": {"technology": "repair-unlock"}}),
            next_action=Mock(return_value={"type": "build", "name": "transport-belt", "position": {"x": 10, "y": 0}}))
        self.catalog = SimpleNamespace(technologies={
            "repair-unlock": {"unlocks": ["repair-pack"], "unit_count": 25,
                              "ingredients": [{"name": "automation-science-pack", "amount": 1}]}})
        self.driver = NativeRepairs(self.game, self.bootstrap, self.factory, self.catalog)
        self.defense = SimpleNamespace(_threats=Mock(return_value={"ok": True, "enemies": []}))
        self.obs = {"ok": True, "world_id": "world-one", "actor_unit_number": 7, "tick": 100,
                    "position": {"x": 0, "y": 0}, "entities": [entity()], "inventory": {"repair-pack": 1},
                    "enabled_recipes": {"repair-pack": True}, "technologies": {}, "research": None}

    def plan(self):
        return self.driver.next_action(self.obs, self.defense)

    def test_healthy_entities_do_not_query_or_acquire_items(self):
        self.obs["entities"][0]["health"] = 150
        self.assertIsNone(self.plan())
        self.defense._threats.assert_not_called()
        self.bootstrap.ensure_item.assert_not_called()
        self.factory.request_recipe_unlock.assert_not_called()

    def test_invalid_and_foreign_entities_never_become_repair_targets(self):
        changes = [{"type": kind} for kind in ("character", "resource", "corpse", "character-corpse", "rail-remnants",
                                               "entity-ghost", "tile-ghost", "item-entity", "")]
        changes += [{"health": value} for value in (0, -1, True, float("nan"), float("inf"), None)]
        changes += [{"max_health": value} for value in (0, -1, True, float("nan"), float("inf"), None)]
        changes += [{"unit_number": value} for value in (0, -1, True, None)]
        changes += [{"force": "enemy"}, {"force": "neutral"}, {"position": {"x": float("nan"), "y": 0}}]
        for change in changes:
            with self.subTest(change=change):
                self.obs["entities"] = [entity(**change)]
                self.assertIsNone(self.plan())
        self.defense._threats.assert_not_called()
        self.bootstrap.ensure_item.assert_not_called()

    def test_missing_actor_identity_cannot_emit_repair(self):
        for change in ({"actor_unit_number": True}, {"actor_unit_number": 0}, {"world_id": ""},
                       {"position": None}, {"ok": False}):
            with self.subTest(change=change):
                self.assertIsNone(self.driver.next_action({**self.obs, **change}, self.defense))
        self.defense._threats.assert_not_called()

    def test_repair_binds_exact_observed_world_actor_and_entity(self):
        before = deepcopy(self.obs)
        action = self.plan()
        self.assertEqual(action["type"], "repair")
        self.assertEqual((action["expected_world_id"], action["expected_actor_unit"], action["expected_entity_unit"]),
                         ("world-one", 7, 42))
        self.assertEqual((action["name"], action["position"]), ("lab", {"x": 2, "y": 0}))
        self.assertEqual(self.obs, before)
        self.bootstrap.ensure_item.assert_called_once_with(self.obs, "repair-pack", 1)

    def test_target_order_is_stable_and_reobserves_health(self):
        first, second = entity(unit_number=9, health=40), entity(unit_number=8, health=40)
        self.obs["entities"] = [first, second]
        self.assertEqual(self.plan()["expected_entity_unit"], 8)
        self.obs["entities"].reverse()
        self.assertEqual(self.plan()["expected_entity_unit"], 8)
        second["health"] = second["max_health"]
        self.assertEqual(self.plan()["expected_entity_unit"], 9)

    def test_multiple_carried_packs_block_without_acquisition_or_stock_changes(self):
        for enabled in (True, False):
            for count in (2, 100):
                with self.subTest(enabled=enabled, count=count):
                    self.obs["enabled_recipes"]["repair-pack"] = enabled
                    self.obs["inventory"]["repair-pack"] = count
                    before = deepcopy(self.obs)
                    action = self.plan()
                    self.assertEqual(action["status"], "blocked")
                    self.assertEqual(action["reason"], "exactly one carried pack required")
                    self.assertEqual(action["evidence"], {"item": "repair-pack", "count": count, "required_count": 1})
                    self.assertNotIn("type", action)
                    self.assertEqual(self.obs, before)
        self.bootstrap.ensure_item.assert_not_called()
        self.factory.request_recipe_unlock.assert_not_called()
        self.factory.next_action.assert_not_called()

    def test_strict_repair_defers_reach_to_navigator_and_assisted_moves_first(self):
        self.obs["entities"][0]["position"] = {"x": 30, "y": 20}
        self.assertEqual(self.plan()["type"], "repair")
        self.game.backend = "assisted"
        self.assertEqual(self.plan()["type"], "move")
        self.obs["position"] = {"x": 27, "y": 20}
        self.assertEqual(self.plan()["type"], "repair")

    def test_material_actions_and_waits_are_not_skipped(self):
        for action in ({"type": "take", "item": "repair-pack", "count": 1},
                       {"type": "craft", "recipe": "repair-pack", "count": 1},
                       {"type": "mine", "name": "coal", "count": 24},
                       {"status": "waiting", "reason": "engine crafting queue is busy"},
                       {"status": "blocked", "reason": "missing input"}):
            with self.subTest(action=action):
                self.bootstrap.ensure_item.return_value = action
                self.assertIs(self.plan(), action)

    def test_enemy_or_unknown_survey_yields_to_defense_before_any_repair_dependency(self):
        for enabled in (True, False):
            self.obs["enabled_recipes"]["repair-pack"] = enabled
            for survey in ({"ok": True, "enemies": [{"type": "unit"}]}, {"ok": False},
                           {"ok": True}, {"ok": True, "enemies": None}, None,
                           *({"ok": True, "enemies": enemies} for enemies in
                             (False, True, 0, "", "[]", (), {"1": {"type": "unit"}}, {"unexpected": False}))):
                with self.subTest(enabled=enabled, survey=survey):
                    self.defense._threats.return_value = survey
                    self.assertIsNone(self.plan())
        self.bootstrap.ensure_item.assert_not_called()
        self.factory.request_recipe_unlock.assert_not_called()
        self.factory.next_action.assert_not_called()

    def test_empty_lua_enemy_tables_allow_repair_and_locked_research(self):
        for enabled in (True, False):
            self.obs["enabled_recipes"]["repair-pack"] = enabled
            for enemies in ({}, []):
                with self.subTest(enabled=enabled, enemies=enemies):
                    self.defense._threats.return_value = {"ok": True, "enemies": enemies}
                    action = self.plan()
                    self.assertEqual(action["type"], "repair" if enabled else "build")

    def test_clear_defense_survey_cannot_bypass_live_identity_or_threat_guard(self):
        self.defense._threats.return_value = {"ok": True, "enemies": {}}
        for enabled in (True, False):
            self.obs["enabled_recipes"]["repair-pack"] = enabled
            for survey in ({"ok": False, "reason": "repair_actor_changed"}, {"ok": True, "quiet": False}, None):
                with self.subTest(enabled=enabled, survey=survey):
                    self.game.query.return_value = survey
                    self.assertIsNone(self.plan())
        self.bootstrap.ensure_item.assert_not_called()
        self.factory.request_recipe_unlock.assert_not_called()
        self.factory.next_action.assert_not_called()

    def test_malformed_observation_tick_never_starts_a_repair_dependency(self):
        for enabled in (True, False):
            self.obs["enabled_recipes"]["repair-pack"] = enabled
            for tick in (-1, True, False, 100.0, "100", None):
                with self.subTest(enabled=enabled, tick=tick):
                    self.obs["tick"] = tick
                    self.assertIsNone(self.plan())
            self.obs.pop("tick")
            self.assertIsNone(self.plan())
        self.defense._threats.assert_not_called()
        self.game.query.assert_not_called()
        self.bootstrap.ensure_item.assert_not_called()
        self.factory.request_recipe_unlock.assert_not_called()
        self.factory.next_action.assert_not_called()

    def test_rollback_or_malformed_live_tick_cannot_authorize_repair_or_research(self):
        for enabled in (True, False):
            self.obs["enabled_recipes"]["repair-pack"] = enabled
            for tick in (99, -1, True, 100.0, "100", None):
                with self.subTest(enabled=enabled, tick=tick):
                    self.game.query.return_value = {"ok": True, "quiet": True, "tick": tick}
                    self.assertIsNone(self.plan())
            self.game.query.return_value = {"ok": True, "quiet": True}
            self.assertIsNone(self.plan())
        self.bootstrap.ensure_item.assert_not_called()
        self.factory.request_recipe_unlock.assert_not_called()
        self.factory.next_action.assert_not_called()

    def test_equal_or_newer_integer_live_tick_allows_normal_work(self):
        for enabled in (True, False):
            self.obs["enabled_recipes"]["repair-pack"] = enabled
            for observed, current in ((0, 0), (100, 100), (100, 101)):
                with self.subTest(enabled=enabled, observed=observed, current=current):
                    self.obs["tick"] = observed
                    self.game.query.return_value = {"ok": True, "quiet": True, "tick": current}
                    self.assertEqual(self.plan()["type"], "repair" if enabled else "build")

    def test_survey_covers_actor_target_and_remote_production_and_turrets(self):
        self.obs["entities"] += [entity(name="boiler", health=150, position={"x": 100, "y": 0}),
                                  entity(name="gun-turret", health=150, position={"x": 0, "y": 100})]
        self.plan()
        positions = [row["position"] for row in self.defense._threats.call_args.args[0]]
        for position in ({"x": 0, "y": 0}, {"x": 2, "y": 0}, {"x": 100, "y": 0}, {"x": 0, "y": 100}):
            self.assertIn(position, positions)

    def test_optional_defense_uses_readonly_threat_query_and_fails_closed(self):
        self.assertEqual(self.driver.next_action(self.obs)["type"], "repair")
        self.assertIn('radius=48,force="enemy"', self.game.query.call_args.args[0])
        for survey in ({"ok": True, "quiet": False}, {"ok": False}, None):
            with self.subTest(survey=survey):
                self.game.query.return_value = survey
                self.assertIsNone(self.driver.next_action(self.obs))

    def test_locked_recipe_services_factory_action_in_same_turn(self):
        self.obs["enabled_recipes"] = {}
        original = self.factory.priority_research
        action = self.factory.next_action.return_value
        self.assertIs(self.plan(), action)
        self.factory.request_recipe_unlock.assert_called_once_with(self.obs, "repair-pack")
        self.factory.next_action.assert_called_once_with(self.obs)
        self.assertIs(self.factory.priority_research, original)
        self.bootstrap.ensure_item.assert_not_called()

    def test_active_research_precedes_catalog_repair_unlock_and_restores_priorities(self):
        self.obs["enabled_recipes"] = {}
        self.obs["research"] = "logistics"
        original = self.factory.priority_research
        def advance(obs):
            self.assertIs(obs, self.obs)
            self.assertEqual(self.factory.priority_research, ["logistics", "repair-unlock", "military"])
            return {"status": "waiting", "reason": "automatic science consumption running"}
        self.factory.next_action.side_effect = advance
        self.assertEqual(self.plan()["status"], "waiting")
        self.assertIs(self.factory.priority_research, original)

    def test_priority_restoration_survives_factory_error(self):
        self.obs["enabled_recipes"] = {}
        original = self.factory.priority_research
        self.factory.next_action.side_effect = RuntimeError("survey failed")
        with self.assertRaisesRegex(RuntimeError, "survey failed"):
            self.plan()
        self.assertIs(self.factory.priority_research, original)

    def test_bootstrap_research_switch_is_deferred_while_current_research_is_active(self):
        self.obs["enabled_recipes"] = {}
        self.obs["research"] = "logistics"
        original = self.factory.priority_research
        self.factory.next_action.return_value = {"type": "research", "technology": "electric-mining-drill"}
        result = self.plan()
        self.assertNotIn("type", result)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["evidence"], {"technology": "logistics", "deferred_technology": "electric-mining-drill"})
        self.assertIs(self.factory.priority_research, original)

    def test_active_research_keeps_all_nonresearch_factory_actions(self):
        self.obs["enabled_recipes"] = {}
        self.obs["research"] = "logistics"
        for action in ({"type": "build", "name": "transport-belt"}, {"type": "take", "item": "iron-plate", "count": 2},
                       {"type": "craft", "recipe": "transport-belt", "count": 5},
                       {"type": "research", "technology": "logistics"}):
            with self.subTest(action=action):
                self.factory.next_action.return_value = action
                self.assertIs(self.plan(), action)

    def test_locked_dependency_never_skips_an_emitted_action_or_hides_failure(self):
        self.obs["enabled_recipes"] = {}
        for action in ({"type": "research", "technology": "repair-unlock"},
                       {"status": "blocked", "reason": "no catalog unlock"},
                       {"status": "failed", "reason": "catalog unavailable"}):
            with self.subTest(action=action):
                self.factory.request_recipe_unlock.return_value = action
                self.assertIs(self.plan(), action)
        self.factory.next_action.assert_not_called()

    def use_normal_bootstrap(self):
        self.bootstrap = DeterministicBootstrap(self.game, self.catalog)
        self.driver.bootstrap = self.bootstrap
        self.bootstrap._recipes["repair-pack"] = {"ok": True, "name": "repair-pack", "handcraftable": True,
            "ingredients": [{"name": "iron-gear-wheel", "amount": 2}, {"name": "electronic-circuit", "amount": 2}],
            "products": [{"name": "repair-pack", "amount": 1}]}
        self.bootstrap._recipes["iron-gear-wheel"] = {"ok": True, "name": "iron-gear-wheel", "handcraftable": True,
            "ingredients": [{"name": "iron-plate", "amount": 2}], "products": [{"name": "iron-gear-wheel", "amount": 1}]}
        self.obs["enabled_recipes"]["iron-gear-wheel"] = True

    def test_normal_recipe_pays_live_ingredients_and_queues_one_pack(self):
        self.use_normal_bootstrap()
        self.obs["inventory"] = {"iron-gear-wheel": 2, "electronic-circuit": 2}
        before = deepcopy(self.obs)
        action = self.plan()
        self.assertEqual((action["type"], action["recipe"], action["count"]), ("craft", "repair-pack", 1))
        self.assertEqual(self.obs, before)

    def test_missing_recipe_ingredient_waits_for_paid_production(self):
        self.use_normal_bootstrap()
        self.obs["inventory"] = {"iron-gear-wheel": 1, "electronic-circuit": 2}
        action = self.plan()
        self.assertEqual(action["status"], "waiting")
        self.assertEqual(action["evidence"]["item"], "iron-plate")
        self.assertNotIn("type", action)

    def test_existing_normal_pack_is_collected_before_another_craft(self):
        self.use_normal_bootstrap()
        self.obs["inventory"] = {}
        self.obs["entities"].append(entity(name="wooden-chest", type="container", unit_number=43,
                                          health=150, inventory={"repair-pack": 3}))
        action = self.plan()
        self.assertEqual((action["type"], action["item"], action["count"]), ("take", "repair-pack", 1))

    def test_existing_engine_queue_finishes_before_repair_pack_craft(self):
        self.use_normal_bootstrap()
        self.obs["inventory"] = {}
        self.obs["crafting_queue"] = [{"recipe": "transport-belt", "count": 10}]
        self.assertEqual(self.plan()["reason"], "engine crafting queue is busy")


if __name__ == "__main__":
    unittest.main()
