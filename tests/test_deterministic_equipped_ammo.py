from copy import deepcopy
from unittest.mock import Mock, patch
import unittest

from factorio_ai.deterministic_bootstrap import DeterministicBootstrap
from factorio_ai.deterministic_defense import DeterministicDefense
from factorio_ai.deterministic_game import DeterministicGame, run_config
from factorio_ai.deterministic_navigation import CharacterNavigator
import test_deterministic_bootstrap as bootstrap_tests


class EquippedAmmoTests(unittest.TestCase):
    def setUp(self):
        self.game = Mock()
        self.bootstrap = DeterministicBootstrap(self.game)
        self.obs = bootstrap_tests.observation(actor_unit_number=58,
            inventory={"iron-plate": 80}, equipped_ammo={"firearm-magazine": 70},
            recoverable_equipped_ammo=[{"slot": 1, "item": "firearm-magazine", "count": 70}],
            enabled_recipes={"firearm-magazine": True, "gun-turret": True},
            entities=[bootstrap_tests.entity("lab")])
        self.bootstrap._recipes["firearm-magazine"] = bootstrap_tests.recipe("firearm-magazine", {"iron-plate": 4})

    def recover(self, count=20):
        return self.bootstrap.ensure_item(self.obs, "firearm-magazine", count)

    def test_autoequipped_stock_is_recovered_before_spending_iron_for_another_batch(self):
        result = self.recover()
        self.assertEqual(result, {"type": "recover_equipped_ammo", "item": "firearm-magazine", "slot": 1,
            "count": 20, "expected_actor_world_id": "test-world", "expected_actor_unit_number": 58,
            "reason": "recover normally crafted equipped ammunition for production defense"})
        self.game.query.assert_not_called()

    def test_main_inventory_deficit_and_available_slot_both_bound_recovery(self):
        self.obs["inventory"]["firearm-magazine"] = 17
        self.assertEqual(self.recover()["count"], 3)
        self.obs["recoverable_equipped_ammo"][0]["count"] = 2
        self.assertEqual(self.recover()["count"], 2)
        self.obs["inventory"]["firearm-magazine"] = 20
        self.assertEqual(self.recover()["status"], "succeeded")

    def test_unsupported_equipped_metadata_falls_back_to_normal_crafting(self):
        self.obs["recoverable_equipped_ammo"] = []
        result = self.recover()
        self.assertEqual((result["type"], result["recipe"], result["count"]), ("craft", "firearm-magazine", 20))
        self.assertEqual(self.obs["equipped_ammo"]["firearm-magazine"], 70)

    def test_recovery_needs_explicit_observed_actor_identity(self):
        for field in ("actor_unit_number", "world_id"):
            original = self.obs.pop(field)
            self.assertEqual(self.recover()["status"], "blocked")
            self.obs[field] = original
        self.game.query.assert_not_called()

    def test_defense_recovers_then_builds_with_its_original_twenty_magazine_target(self):
        defense = DeterministicDefense(self.game, self.bootstrap)
        defense._turret_range = Mock(return_value=18)
        defense._threats = Mock(return_value={"ok": True, "enemies": []})
        defense._find_turret_site = Mock(return_value={"ok": True, "position": {"x": 10, "y": 10}})
        self.assertEqual(defense.next_action(self.obs)["type"], "recover_equipped_ammo")
        self.obs["inventory"].update({"firearm-magazine": 20, "gun-turret": 1})
        self.obs["recoverable_equipped_ammo"][0]["count"] = 50
        result = defense.next_action(self.obs)
        self.assertEqual((result["type"], result["name"]), ("build", "gun-turret"))
        self.assertEqual(defense.ammo_target, 20)

    def test_existing_empty_turret_uses_recovery_then_ordinary_main_inventory_insertion(self):
        defense = DeterministicDefense(self.game, self.bootstrap)
        defense._turret_range = Mock(return_value=18)
        defense._threats = Mock(return_value={"ok": True, "enemies": []})
        self.obs["entities"].append(bootstrap_tests.entity("gun-turret", position={"x": 0, "y": 0}))
        self.assertEqual(defense.next_action(self.obs)["type"], "recover_equipped_ammo")
        self.obs["inventory"]["firearm-magazine"] = 20
        result = defense.next_action(self.obs)
        self.assertEqual((result["type"], result["inventory"], result["count"]), ("insert", "turret_ammo", 20))

    def test_actor_slot_item_and_count_guards_reject_before_rcon(self):
        action = self.recover()
        game = DeterministicGame(run_config())
        with patch.object(game, "query") as query, patch.object(game, "_record_action") as log:
            for changes in ({"count": 0}, {"count": True}, {"count": 101}, {"slot": 0}, {"slot": 1.5},
                            {"item": "uranium-rounds-magazine"}, {"expected_actor_world_id": ""},
                            {"expected_actor_unit_number": False}, {"position": {"x": 1, "y": 1}}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    game.act({**action, **changes})
            for field in ("slot", "count", "expected_actor_unit_number", "expected_actor_world_id"):
                invalid = deepcopy(action);invalid.pop(field)
                with self.assertRaises(ValueError):
                    game.act(invalid)
            query.assert_not_called();log.assert_not_called()

    def test_assisted_and_character_adapters_use_identical_guarded_transfer(self):
        commands = []
        for backend in ("assisted", "character"):
            game = DeterministicGame(run_config(), backend=backend)
            with patch.object(game, "query", return_value={"ok": True, "moved": 20}) as query, \
                    patch.object(game, "_record_action", side_effect=lambda action, result: result):
                self.assertEqual(game.act(self.recover())["moved"], 20)
                commands.append(query.call_args.args[0])
        self.assertEqual(commands[0], commands[1])

    def test_character_navigation_needs_no_target_movement_for_own_inventory_transfer(self):
        self.game.backend = "character"
        self.game.query.return_value = {"ok": True}
        self.game.act.return_value = {"ok": True, "moved": 20}
        navigator = CharacterNavigator(self.game)
        navigator.pending_action = Mock(return_value=None)
        navigator._input = Mock()
        action = self.recover()
        self.assertEqual(navigator.execute(action, self.obs)["moved"], 20)
        navigator._input.assert_not_called()
        self.game.act.assert_called_once_with(action)
        self.assertNotIn("position", action)


if __name__ == "__main__":
    unittest.main()
