from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_bootstrap import DeterministicBootstrap


def observation(**fields):
    result = {"ok": True, "world_id": "test-world", "tick": 100, "inventory": {}, "entities": [],
              "resources": {"coal": {"position": {"x": 5.5, "y": 8.5}},
                            "stone": {"position": {"x": 10.5, "y": 12.5}}},
              "enabled_recipes": {}, "technologies": {}}
    result.update(fields)
    return result


def entity(name, inventory=None, position=None, **fields):
    return {"name": name, "position": position or {"x": 4, "y": 5}, "inventory": inventory or {}, **fields}


def recipe(name, ingredients, *, output=1, handcraftable=True):
    return {"ok": True, "name": name, "handcraftable": handcraftable,
            "ingredients": [{"name": item, "type": "item", "amount": count} for item, count in ingredients.items()],
            "products": [{"name": name, "type": "item", "amount": output}]}


class DeterministicBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.game = Mock()
        self.driver = DeterministicBootstrap(self.game)

    def test_iron_and_copper_are_collected_from_real_furnace_output(self):
        for item in ["iron-plate", "copper-plate"]:
            with self.subTest(item=item):
                obs = observation(inventory={item: 2}, entities=[entity("stone-furnace", {item: 5})])
                action = self.driver.ensure_item(obs, item, 6)
                self.assertEqual((action["type"], action["item"], action["count"]), ("take", item, 4))
        self.game.query.assert_not_called()

    def test_missing_plates_wait_and_raw_ore_never_hand_mines(self):
        for item in ["iron-plate", "copper-plate", "iron-ore", "copper-ore"]:
            with self.subTest(item=item):
                result = self.driver.ensure_item(observation(), item, 10)
                self.assertNotIn("type", result)
                self.assertEqual(result["status"], "waiting" if "plate" in item else "blocked")

    def test_initial_coal_seed_consumes_only_observed_resource(self):
        self.game.query.return_value = {"ok": True, "cells": []}
        action = self.driver.ensure_item(observation(), "coal", 80)
        self.assertEqual(action["type"], "mine")
        self.assertEqual(action["count"], 50)
        self.assertEqual(action["position"], {"x": 5.5, "y": 8.5})

    def test_operating_coal_supply_waits_for_chest_instead_of_hand_mining(self):
        self.game.query.return_value = {"ok": True, "cells": [{"fuel": 0, "burning": True}]}
        result = self.driver.ensure_item(observation(), "coal", 8)
        self.assertEqual(result["status"], "waiting")
        self.assertNotIn("type", result)

    def test_stone_supply_ends_hand_mining_when_drill_exists(self):
        self.game.query.return_value = {"ok": True, "cells": [{"fuel": 2}]}
        result = self.driver.ensure_item(observation(), "stone", 5)
        self.assertEqual(result["status"], "waiting")

    def test_raw_supply_query_error_is_not_permission_to_hand_mine(self):
        self.game.query.return_value = {"ok": False, "reason": "API error"}
        result = self.driver.ensure_item(observation(), "coal", 8)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["evidence"]["query_error"], "API error")

    def test_fuel_collected_from_chest_not_operating_miner(self):
        obs = observation(entities=[entity("burner-mining-drill", {"coal": 8}),
                                    entity("wooden-chest", {"coal": 3})])
        action = self.driver.ensure_item(obs, "coal", 8)
        self.assertEqual((action["type"], action["name"], action["count"]), ("take", "wooden-chest", 3))

    def test_live_multiyield_recipe_uses_engine_crafting_count(self):
        self.game.query.return_value = recipe("transport-belt", {"iron-plate": 1, "iron-gear-wheel": 1}, output=2)
        obs = observation(inventory={"iron-plate": 4, "iron-gear-wheel": 4, "transport-belt": 1},
                          enabled_recipes={"transport-belt": True})
        action = self.driver.ensure_item(obs, "transport-belt", 4)
        self.assertEqual((action["type"], action["recipe"], action["count"]), ("craft", "transport-belt", 2))

    def test_recursive_handcraft_uses_live_ingredient_amounts(self):
        self.driver._recipes = {"lab": recipe("lab", {"iron-gear-wheel": 7}),
                                "iron-gear-wheel": recipe("iron-gear-wheel", {"iron-plate": 2})}
        obs = observation(inventory={"iron-plate": 20, "iron-gear-wheel": 3},
                          enabled_recipes={"lab": True, "iron-gear-wheel": True})
        action = self.driver.ensure_item(obs, "lab", 1)
        self.assertEqual((action["recipe"], action["count"]), ("iron-gear-wheel", 4))

    def test_locked_or_machine_only_recipe_never_reaches_crafting(self):
        self.game.query.return_value = recipe("boiler", {"iron-plate": 5})
        result = self.driver.ensure_item(observation(inventory={"iron-plate": 10}), "boiler", 1)
        self.assertEqual(result["status"], "blocked")
        self.driver._recipes["boiler"]["handcraftable"] = False
        result = self.driver.ensure_item(observation(inventory={"iron-plate": 10}, enabled_recipes={"boiler": True}), "boiler", 1)
        self.assertEqual(result["reason"], "required item needs a production machine")

    def test_busy_engine_queue_does_not_duplicate_consumed_ingredients(self):
        obs = observation(crafting_queue=[{"recipe": "burner-mining-drill", "count": 1}])
        result = self.driver.ensure_item(obs, "burner-mining-drill", 1)
        self.assertEqual(result["status"], "waiting")
        self.game.query.assert_not_called()

    def test_assembler_input_stock_is_not_misreported_as_output(self):
        obs = observation(entities=[entity("assembling-machine-1", {"iron-plate": 10}, recipe="iron-gear-wheel")])
        self.game.query.return_value = {"count": 0}
        result = self.driver.ensure_item(obs, "iron-plate", 5)
        self.assertEqual(result["status"], "waiting")

    def test_partial_cell_resume_reuses_receiver_and_dynamic_position(self):
        receiver = entity("wooden-chest", position={"x": 123.5, "y": -9.5})
        cell = {"ok": True, "complete": False, "receiver": receiver,
                "drill": {"name": "burner-mining-drill", "position": {"x": 124, "y": -8}, "direction": 0}}
        obs = observation(inventory={"burner-mining-drill": 1}, entities=[receiver])
        with patch.object(self.driver, "discover_cell", return_value=cell):
            action = self.driver._ensure_cell(obs, "coal", "wooden-chest")
        self.assertEqual((action["type"], action["name"]), ("build", "burner-mining-drill"))
        self.assertEqual(action["position"], {"x": 124, "y": -8})

    def test_depleted_drill_is_recovered_before_fueling(self):
        obs = observation(entities=[entity("burner-mining-drill", status_name="no_minable_resources")])
        action = self.driver.next_action(obs)
        self.assertEqual((action["type"], action["name"]), ("mine", "burner-mining-drill"))
        self.game.query.assert_not_called()

    def test_lab_inventory_alone_does_not_fake_natural_trigger(self):
        obs = observation(inventory={"lab": 1}, technologies={"steam-power": True, "electronics": True})
        self.game.query.return_value = {"ok": True, "associated": False}
        with patch.object(self.driver, "_ensure_cell", return_value=None):
            result = self.driver.next_action(obs)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["evidence"]["dependency"], "crafting_player")
        obs["technologies"]["automation-science-pack"] = True
        with patch.object(self.driver, "_ensure_cell", return_value=None):
            self.assertEqual(self.driver.next_action(obs)["status"], "succeeded")

    def test_associated_player_resume_crafts_lab_for_missing_natural_event(self):
        obs = observation(inventory={"lab": 1}, technologies={"steam-power": True, "electronics": True})
        self.game.query.return_value = {"ok": True, "associated": True}
        with patch.object(self.driver, "_ensure_cell", return_value=None), patch.object(
            self.driver, "ensure_item", return_value={"status": "waiting"}
        ) as ensure:
            self.driver.next_action(obs)
        ensure.assert_called_once_with(obs, "lab", 2)

    def test_bootstrap_does_not_mutate_observation(self):
        obs = observation(inventory={"coal": 8}, entities=[entity("stone-furnace")])
        before = deepcopy(obs)
        action = self.driver.maintain_fuel(obs)
        self.assertEqual((action["type"], action["inventory"]), ("insert", "fuel"))
        self.assertEqual(obs, before)


if __name__ == "__main__":
    unittest.main()
