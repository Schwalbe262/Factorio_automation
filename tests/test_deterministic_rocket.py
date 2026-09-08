from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_rocket import DeterministicRocket


def catalog():
    recipes = {
        "rocket-silo": {"name": "rocket-silo", "categories": ["crafting"], "ingredients": [
            {"name": "steel-plate", "amount": 1000}, {"name": "concrete", "amount": 1000},
            {"name": "pipe", "amount": 100}, {"name": "processing-unit", "amount": 200},
            {"name": "electric-engine-unit", "amount": 200}]},
        "rocket-part": {"name": "rocket-part", "categories": ["rocket-building"], "ingredients": [
            {"name": "processing-unit", "amount": 1}, {"name": "low-density-structure", "amount": 1},
            {"name": "rocket-fuel", "amount": 1}]}}
    return SimpleNamespace(
        recipe_for_product=lambda item: recipes.get(item), recipes=recipes,
        entities={"rocket-silo": {"rocket_parts_required": 50, "collision_box": [[-4.4, -4.4], [4.4, 4.4]]},
                  "character": {"crafting_categories": ["crafting"]}},
        items={name: {"stack_size": stack} for name, stack in [
            ("steel-plate", 100), ("concrete", 100), ("pipe", 100), ("processing-unit", 100),
            ("electric-engine-unit", 50), ("space-platform-starter-pack", 1)]})


def observation(**changes):
    return {"world_id": "fixture", "tick": 10, "position": {"x": 0, "y": 0},
            "inventory": {}, "entities": [], "technologies": {"rocket-silo": True},
            "enabled_recipes": {"rocket-silo": True, "rocket-part": True}, **changes}


def ready(**evidence):
    return {"status": "succeeded", "evidence": evidence}


class RocketConstructionTests(unittest.TestCase):
    def setUp(self):
        self.game = SimpleNamespace(backend="assisted", query=Mock(return_value={"ok": True, "count": 0, "bar": 11, "size": 32}))
        self.bootstrap = SimpleNamespace(ensure_item=Mock())
        self.builder = SimpleNamespace(ensure_plan=Mock(return_value=ready()))
        self.factory = SimpleNamespace(state={"blocks": {}}, _sync=Mock(),
            ensure_power_connection=Mock(return_value=ready()), connect_input=Mock(return_value=ready()))
        self.factory.ensure_product = Mock(side_effect=lambda obs, item: ready(ports=[
            {"kind": "item", "item": item, "direction": "output", "position": {"x": -10.5, "y": .5}, "facing": 4}]))

        def reserve(plan, key, obs, reference=None):
            self.factory.state["blocks"].setdefault(key, deepcopy(plan))
            return self.factory.state["blocks"][key]

        self.factory.reserve_site = Mock(side_effect=reserve)
        self.catalog = catalog()
        self.rocket = DeterministicRocket(self.game, self.bootstrap, self.builder, self.catalog, self.factory)

    def silo_observation(self, **changes):
        silo = deepcopy(self.rocket.silo_plan()["entities"][0])
        silo.update({"unit_number": 50, "rocket_parts": 0, "inventory": {}})
        silo.update(changes)
        return observation(entities=[silo])

    def buffer_observation(self, item="steel-plate", **changes):
        plan = self.rocket.buffer_plan(item)
        self.factory.state["blocks"]["rocket-buffer:" + item] = plan
        chest = deepcopy(plan["entities"][0])
        chest["inventory"] = {}
        return observation(entities=[chest], **changes)

    def test_silo_plan_uses_catalog_geometry_and_all_three_live_ingredients(self):
        plan = self.rocket.silo_plan()
        silo = plan["entities"][0]
        self.assertEqual((silo["_width"], silo["_height"]), (9, 9))
        self.assertEqual(silo["recipe"], "rocket-part")
        self.assertEqual({p["item"] for p in plan["ports"]}, {"processing-unit", "low-density-structure", "rocket-fuel"})
        self.assertEqual(len([e for e in plan["entities"] if e["name"] == "inserter"]), 3)
        for port in plan["ports"]:
            self.assertEqual(port["direction"], "input")
            self.assertEqual(port["kind"], "item")
            self.assertTrue(abs(port["position"]["x"] - .5) > 4.5 or abs(port["position"]["y"] - .5) > 4.5)

    def test_locked_research_never_constructs_or_forces_unlock(self):
        result = self.rocket.next_action(observation(technologies={}))
        self.assertEqual(result["status"], "waiting")
        self.builder.ensure_plan.assert_not_called()
        self.factory.ensure_product.assert_not_called()
        self.game.query.assert_not_called()

    def test_silo_is_handcrafted_only_after_live_ingredient_quantities_are_present(self):
        inventory = {i["name"]: i["amount"] for i in self.catalog.recipes["rocket-silo"]["ingredients"]}
        inventory["steel-plate"] -= 1
        self.rocket.collect_product = Mock(side_effect=lambda obs, item, count:
            ready() if obs["inventory"].get(item, 0) >= count else {"type": "take", "item": item, "count": 1})
        missing = self.rocket.next_action(observation(inventory=inventory))
        self.assertEqual(missing, {"type": "take", "item": "steel-plate", "count": 1})
        inventory["steel-plate"] += 1
        crafted = self.rocket.next_action(observation(inventory=inventory))
        self.assertEqual((crafted["type"], crafted["recipe"], crafted["count"]), ("craft", "rocket-silo", 1))
        self.assertTrue(any(call.args[1:] == ("electric-engine-unit", 200)
                            for call in self.rocket.collect_product.call_args_list))
        self.factory.ensure_product.assert_not_called()

    def test_silo_handcraft_waits_for_existing_engine_queue(self):
        self.assertEqual(self.rocket.next_action(observation(crafting_queue=[{"recipe": "rocket-silo"}]))["status"], "waiting")
        self.factory.ensure_product.assert_not_called()

    def test_existing_silo_is_fed_by_three_automatic_links_before_pack_collection(self):
        self.rocket.collect_product = Mock(return_value={"status": "waiting", "reason": "starter pack output"})
        result = self.rocket.next_action(self.silo_observation())
        self.assertEqual(result["reason"], "starter pack output")
        self.assertEqual(self.factory.connect_input.call_count, 3)
        self.assertEqual({call.args[1]["item"] for call in self.factory.connect_input.call_args_list},
                         {"processing-unit", "low-density-structure", "rocket-fuel"})
        self.rocket.collect_product.assert_called_once_with(self.silo_observation(), "space-platform-starter-pack", 1)

    def test_empty_buffer_is_capped_before_power_and_material_connection(self):
        obs = self.buffer_observation()
        self.game.query.return_value.update(bar=33)
        result = self.rocket.collect_product(obs, "steel-plate", 1000)
        self.assertEqual((result["type"], result["slots"]), ("bar", 10))
        self.factory.ensure_power_connection.assert_not_called()
        self.factory.connect_input.assert_not_called()

    def test_collection_takes_only_actual_remaining_batch_and_keeps_source_automatic(self):
        obs = self.buffer_observation(inventory={"steel-plate": 950})
        self.game.query.return_value.update(count=300)
        result = self.rocket.collect_product(obs, "steel-plate", 1000)
        self.assertEqual((result["type"], result["count"], result["inventory"]), ("take", 50, "chest"))
        self.factory.ensure_product.assert_not_called()

    def test_empty_capped_buffer_waits_for_real_output_and_routes_production(self):
        result = self.rocket.collect_product(self.buffer_observation(), "steel-plate", 1000)
        self.assertEqual(result["status"], "waiting")
        self.factory.ensure_product.assert_called_once()
        self.factory.connect_input.assert_called_once()
        self.assertNotIn("type", result)

    def test_character_collection_walks_into_reach_first(self):
        self.game.backend = "character"
        self.game.query.return_value.update(count=300)
        result = self.rocket.collect_product(self.buffer_observation(position={"x": 100, "y": 100}), "steel-plate", 1000)
        self.assertEqual(result["type"], "move")

    def test_launch_uses_reserved_silo_and_observed_real_starter_pack(self):
        obs = self.silo_observation(rocket_parts=0, rocket_silo_status="rocket_ready")
        obs["inventory"] = {"space-platform-starter-pack": 1}
        obs["entities"].insert(0, {"name": "rocket-silo", "unit_number": 1, "position": {"x": 200, "y": 200}, "rocket_parts": 0})
        result = self.rocket.next_action(obs)
        self.assertEqual(result["type"], "launch")
        self.assertEqual(result["position"], {"x": .5, "y": .5})

    def test_launch_animation_cannot_be_mistaken_for_platform_delivery(self):
        result = self.rocket.next_action(observation(launch={"ordered": True, "platform_valid": True,
            "platform_hub_valid": False, "baseline": 4, "rockets_launched": 5}))
        self.assertEqual(result["status"], "running")
        self.factory.ensure_product.assert_not_called()
        complete = self.rocket.next_action(observation(launch={"ordered": True, "platform_valid": True,
            "platform_hub_valid": True, "baseline": 4, "rockets_launched": 5}))
        self.assertEqual(complete["status"], "succeeded")

    def test_non_positive_and_fractional_collection_requests_are_rejected(self):
        for count in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                self.rocket.collect_product(observation(), "steel-plate", count)


if __name__ == "__main__":
    unittest.main()
