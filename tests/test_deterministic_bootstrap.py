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

    def test_operating_electric_source_precedes_isolated_burner_and_incomplete_cells(self):
        burner = {"drill": entity("burner-mining-drill", direction=0),
                  "receiver": entity("wooden-chest", position={"x": 1.5, "y": 2.5})}
        electric = {"drill": entity("electric-mining-drill"), "electric": True, "operating": True,
                    "receiver": entity("wooden-chest", position={"x": 8.5, "y": 9.5})}
        partial = {"drill": entity("burner-mining-drill", direction=0)}
        self.game.query.return_value = {"ok": True, "cells": [partial, burner, electric]}
        result = self.driver.discover_cell("coal", "wooden-chest")
        self.assertEqual(result["receiver"], electric["receiver"])
        result = self.driver.discover_cell("coal", "wooden-chest", preferred_receiver=burner["receiver"])
        self.assertEqual(result["receiver"], burner["receiver"])

    def test_electric_coal_drill_never_requests_a_burner_emergency_hand_seed(self):
        self.game.query.return_value = {"ok": True, "cells": [{"fuel": 0, "burning": False, "electric": True}]}
        result = self.driver.ensure_item(observation(), "coal", 8)
        self.assertEqual(result["status"], "waiting")
        self.assertNotIn("type", result)

    def test_construction_collects_only_the_missing_batch_from_live_belt(self):
        obs = observation(inventory={"iron-plate": 2}, entities=[
            entity("stone-furnace", {}), entity("transport-belt", belt_inventory={"iron-plate": 40})])
        action = self.driver.ensure_item(obs, "iron-plate", 7)
        self.assertEqual(action["type"], "take")
        self.assertEqual(action["name"], "transport-belt")
        self.assertEqual(action["count"], 5)
        self.game.query.assert_not_called()

    def test_bootstrap_does_not_steal_power_coal_from_automatic_belts(self):
        obs = observation(entities=[entity("transport-belt", belt_inventory={"coal": 40})])
        self.assertIsNone(self.driver._take_output(obs, "coal", 8))

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


class StrictHandcraftBatchTests(unittest.TestCase):
    def setUp(self):
        self.game = Mock(backend="character")
        self.driver = DeterministicBootstrap(self.game)
        self.driver._recipes = {
            "inserter": recipe("inserter", {"iron-gear-wheel": 1, "electronic-circuit": 1, "iron-plate": 1}),
            "iron-gear-wheel": recipe("iron-gear-wheel", {"iron-plate": 2}),
            "electronic-circuit": recipe("electronic-circuit", {"copper-cable": 3, "iron-plate": 1}),
            "copper-cable": recipe("copper-cable", {"copper-plate": 1}, output=2),
            "transport-belt": recipe("transport-belt", {"iron-gear-wheel": 1, "iron-plate": 1}, output=2),
        }

    def obs(self, **fields):
        fields.setdefault("enabled_recipes", {name: True for name in self.driver._recipes})
        return observation(**fields)

    def test_shared_iron_and_cable_batches_are_reserved_once(self):
        targets = self.driver._handcraft_collection_targets(self.obs(), "inserter", 2)
        self.assertEqual(targets, {"iron-plate": 8, "copper-plate": 3})

    def test_existing_intermediates_and_raw_inventory_are_not_double_counted(self):
        obs = self.obs(inventory={"iron-gear-wheel": 1, "electronic-circuit": 1,
                                  "copper-cable": 1, "iron-plate": 2})
        original = deepcopy(obs)
        self.assertEqual(self.driver._handcraft_collection_targets(obs, "inserter", 2),
                         {"iron-plate": 5, "copper-plate": 1})
        self.assertEqual(obs, original)

    def test_odd_target_respects_real_multiyield_batches_and_current_target_stock(self):
        self.assertEqual(self.driver._handcraft_collection_targets(
            self.obs(inventory={"transport-belt": 1}), "transport-belt", 4), {"iron-plate": 6})
        self.assertEqual(self.driver._handcraft_collection_targets(
            self.obs(inventory={"transport-belt": 4}), "transport-belt", 4), {})

    def test_collects_shared_plate_requirement_in_one_bounded_visit(self):
        obs = self.obs(inventory={"iron-plate": 2}, entities=[entity("stone-furnace", {"iron-plate": 100})])
        action = self.driver.ensure_item(obs, "inserter", 2)
        self.assertEqual((action["type"], action["item"], action["count"]), ("take", "iron-plate", 6))

    def test_partial_output_and_fifty_item_transfer_cap_preserve_remaining_deficit(self):
        for have, available, expected in [(0, 5, 5), (5, 100, 3)]:
            with self.subTest(have=have):
                obs = self.obs(inventory={"iron-plate": have}, entities=[entity("stone-furnace", {"iron-plate": available})])
                self.assertEqual(self.driver.ensure_item(obs, "inserter", 2)["count"], expected)
        for have, expected in [(20, 50), (70, 10)]:
            obs = self.obs(inventory={"iron-plate": have}, entities=[entity("stone-furnace", {"iron-plate": 200})])
            self.assertEqual(self.driver.ensure_item(obs, "inserter", 20)["count"], expected)

    def test_fully_stocked_batch_keeps_existing_intermediate_engine_crafting(self):
        action = self.driver.ensure_item(self.obs(inventory={"iron-plate": 8, "copper-plate": 3}), "inserter", 2)
        self.assertEqual((action["type"], action["recipe"], action["count"]), ("craft", "iron-gear-wheel", 2))

    def test_existing_produced_intermediates_are_collected_before_unneeded_raw_inputs(self):
        obs = self.obs(entities=[entity("wooden-chest", {"iron-gear-wheel": 2})])
        self.assertEqual(self.driver._handcraft_collection_targets(obs, "inserter", 2),
                         {"iron-gear-wheel": 2, "copper-plate": 3, "iron-plate": 4})
        action = self.driver.ensure_item(obs, "inserter", 2)
        self.assertEqual((action["type"], action["item"], action["count"]), ("take", "iron-gear-wheel", 2))

    def test_external_intermediate_stock_is_not_reserved_twice_across_branches(self):
        self.driver._recipes["electronic-circuit"] = recipe("electronic-circuit", {"iron-gear-wheel": 1, "copper-plate": 1})
        obs = self.obs(entities=[entity("wooden-chest", {"iron-gear-wheel": 2})])
        self.assertEqual(self.driver._handcraft_collection_targets(obs, "inserter", 2),
                         {"iron-gear-wheel": 2, "iron-plate": 6, "copper-plate": 2})

    def test_locked_descendant_declines_before_any_speculative_collection(self):
        obs = self.obs(enabled_recipes={"inserter": True, "iron-gear-wheel": True})
        with patch.object(self.driver, "_take_output", return_value=None):
            self.assertIsNone(self.driver._collect_handcraft_batch(obs, "inserter", 2))
        self.game.act.assert_not_called()

    def test_unsupported_trees_decline_without_new_recipe_or_mining_actions(self):
        original = deepcopy(self.driver._recipes)
        for case in ("cycle", "fluid", "stochastic", "machine", "ore", "fractional_output"):
            with self.subTest(case=case):
                self.driver._recipes = deepcopy(original)
                gear = self.driver._recipes["iron-gear-wheel"]
                if case == "cycle":
                    gear["ingredients"] = [{"name": "inserter", "type": "item", "amount": 1}]
                elif case == "fluid":
                    gear["ingredients"][0]["type"] = "fluid"
                elif case == "stochastic":
                    gear["products"][0]["probability"] = .5
                elif case == "machine":
                    gear["handcraftable"] = False
                elif case == "fractional_output":
                    gear["products"][0]["amount"] = .5
                else:
                    gear["ingredients"][0]["name"] = "iron-ore"
                self.assertIsNone(self.driver._handcraft_collection_targets(self.obs(), "inserter", 2))
        self.game.act.assert_not_called()

    def test_assisted_and_nested_requests_keep_original_ingredient_acquisition(self):
        obs = self.obs(entities=[entity("stone-furnace", {"iron-plate": 100})])
        self.game.backend = "assisted"
        self.assertEqual(self.driver.ensure_item(obs, "inserter", 2)["count"], 4)
        self.game.backend = "character"
        self.assertEqual(self.driver.ensure_item(obs, "inserter", 2, ("other-target",))["count"], 4)

    def test_busy_engine_queue_never_prefetches_consumed_materials(self):
        obs = self.obs(crafting_queue=[{"recipe": "inserter", "count": 2}])
        with patch.object(self.driver, "_collect_handcraft_batch") as collect:
            self.assertEqual(self.driver.ensure_item(obs, "inserter", 2)["status"], "waiting")
        collect.assert_not_called()

    def test_established_raw_cells_never_enable_new_manual_collection(self):
        for material in ("stone", "coal"):
            with self.subTest(material=material):
                self.driver._recipes["fixture"] = recipe("fixture", {material: 10})
                self.game.query.return_value = {"ok": True, "cells": [{"fuel": 0, "burning": False}]}
                with patch.object(self.driver, "_raw_material") as manual:
                    result = self.driver.ensure_item(self.obs(), "fixture", 2)
                self.assertEqual(result["status"], "waiting")
                manual.assert_not_called()

    def test_pre_cell_stone_gathering_and_whole_tree_mining_stay_bounded(self):
        self.driver._recipes["fixture"] = recipe("fixture", {"stone": 10})
        self.game.query.return_value = {"ok": True, "cells": []}
        action = self.driver.ensure_item(self.obs(), "fixture", 3)
        self.assertEqual((action["type"], action["name"], action["count"]), ("mine", "stone", 30))
        self.driver._recipes["fixture"] = recipe("fixture", {"wood": 3})
        self.game.query.return_value = {"ok": True, "name": "tree-01", "position": {"x": 9, "y": 2}}
        action = self.driver.ensure_item(self.obs(), "fixture", 2)
        self.assertEqual((action["type"], action["name"], action["count"]), ("mine", "tree-01", 1))


if __name__ == "__main__":
    unittest.main()
