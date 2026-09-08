from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_bootstrap import DeterministicBootstrap
from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_construction_materials import ConstructionMaterials
from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.deterministic_supervisor import DeterministicSupervisor
from factorio_ai.factory_templates import build_template


READY = {"status": "succeeded", "reason": "observed", "evidence": {}}


def recipe(name, ingredients, *, handcraftable=True):
    return {"ok": True, "name": name, "enabled": True, "handcraftable": handcraftable,
            "categories": ["crafting" if handcraftable else "smelting"], "energy": 16,
            "ingredients": [{"type": "item", "name": item, "amount": count} for item, count in ingredients.items()],
            "products": [{"type": "item", "name": name, "amount": 1}]}


class ConstructionMaterialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)), backend="assisted", query=Mock())
        self.recipes = {"pumpjack": recipe("pumpjack", {"steel-plate": 5, "iron-gear-wheel": 10,
                         "electronic-circuit": 5, "pipe": 10}),
                        "steel-plate": recipe("steel-plate", {"iron-plate": 5}, handcraftable=False)}
        self.catalog = SimpleNamespace(fingerprint="catalog", recipes=self.recipes, entities={}, technologies={})
        self.catalog.recipe_for_product = lambda item: self.recipes.get(item)
        self.bootstrap = DeterministicBootstrap(self.game, self.catalog)
        self.bootstrap._recipes = deepcopy(self.recipes)
        self.builder = FactoryBuilder(self.game, self.bootstrap, self.catalog)
        self.builder.can_place = Mock(return_value={"ok": True})
        self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        self.factory.graph = Mock()
        self.factory.graph.machines_for_recipe.return_value = [{"name": "stone-furnace", "crafting_speed": 1}]
        self.factory.ensure_power_connection = Mock(return_value=READY)
        self.factory.connect_input = Mock(return_value=READY)
        self.factory._source_endpoint = Mock(side_effect=lambda obs, item: dict(READY, evidence={"ports": [
            {"kind": "item", "item": item, "direction": "output", "position": {"x": .5, "y": .5}, "facing": 4}]}))
        self.factory.reserve_site = self.reserve
        self.materials = ConstructionMaterials(self.factory)
        self.obs = {"world_id": "world", "tick": 1, "inventory": {"iron-gear-wheel": 10, "electronic-circuit": 5,
                    "pipe": 10, "stone-furnace": 1, "transport-belt": 20, "inserter": 8, "small-electric-pole": 8},
                    "entities": [], "enabled_recipes": {name: True for name in self.recipes}}
        self.plan = {"ok": True, "entities": [{"name": "pumpjack", "position": {"x": 50, "y": 50}, "direction": 0}]}
        self.blocked = {"status": "blocked", "reason": "required item needs a production machine",
                        "evidence": {"item": "steel-plate", "recipe": "steel-plate", "have": 0, "need": 5}}

    def reserve(self, origin, key, obs, reference=None):
        if key not in self.factory.state["blocks"]:
            self.factory.state["blocks"][key] = build_template("furnace_row", recipe="steel-plate",
                machine="stone-furnace", inputs=["iron-plate", "coal"], output="steel-plate", anchor={"x": 20, "y": 20})
            self.factory._save()
        return self.factory.state["blocks"][key]

    def test_pumpjack_steel_builds_real_producer_then_collects_bounded_output_before_engine_craft(self):
        # The original bootstrap still fails closed outside the production builder.
        self.assertEqual(self.builder.ensure_plan(self.obs, self.plan), self.blocked)
        self.builder.construction_materials = self.materials
        actions = []
        for _ in range(24):
            choice = self.builder.ensure_plan(self.obs, self.plan)
            if choice.get("status") == "waiting":
                break
            for child in choice["actions"] if choice["type"] == "build_many" else [choice]:
                self.assertEqual(child["type"], "build", child)
                self.assertNotEqual(child["name"], "pumpjack")
                item = child["item"]
                self.assertGreater(self.obs["inventory"].get(item, 0), 0)
                self.obs["inventory"][item] -= 1  # Fixture mirrors the ordinary build cost.
                self.obs["entities"].append({"name": child["name"], "position": child["position"],
                                             "direction": child["direction"], "inventory": {}})
                actions.append(child)
            self.obs["tick"] += 1
        else:
            self.fail("fixture did not finish its finite producer construction")
        self.assertEqual(actions[0]["name"], "stone-furnace")
        self.assertEqual(choice["reason"], "waiting for machine-made construction ingredient output")
        self.assertEqual(choice["evidence"]["need"], 5)
        self.assertIn("recipe:steel-plate", self.factory.state["blocks"])
        self.assertGreater(self.factory.connect_input.call_count, 0)
        # Construction alone creates no steel and never claims the pumpjack made.
        self.assertEqual(self.obs["inventory"].get("steel-plate", 0), 0)
        furnace = next(e for e in self.obs["entities"] if e["name"] == "stone-furnace")
        furnace["inventory"]["steel-plate"] = 8  # Later observed machine output.
        pickup = self.builder.ensure_plan(self.obs, self.plan)
        self.assertEqual((pickup["type"], pickup["item"], pickup["count"]), ("take", "steel-plate", 5))
        furnace["inventory"]["steel-plate"] -= pickup["count"]
        self.obs["inventory"]["steel-plate"] = pickup["count"]
        crafted = self.builder.ensure_plan(self.obs, self.plan)
        self.assertEqual((crafted["type"], crafted["recipe"], crafted["count"]), ("craft", "pumpjack", 1))
        self.assertEqual(self.obs["inventory"].get("pumpjack", 0), 0)

    def test_ordinary_actions_and_locked_recipes_do_not_start_production(self):
        self.factory.ensure_product = Mock()
        for result in (READY, {"type": "craft", "recipe": "pumpjack", "count": 1},
                       {"status": "blocked", "reason": "required recipe is locked", "evidence": {"item": "steel-plate"}}):
            self.assertIs(self.materials.ensure(self.obs, result), result)
        self.factory.ensure_product.assert_not_called()

    def test_supervisor_attaches_bridge_only_when_shared_production_is_prepared(self):
        self.assertIsNone(self.builder.construction_materials)
        supervisor = DeterministicSupervisor(self.game)
        supervisor.bootstrap, supervisor.builder, supervisor.catalog = self.bootstrap, self.builder, self.catalog
        supervisor.prepare_production()
        material_source = self.builder.construction_materials
        self.assertIsInstance(material_source, ConstructionMaterials)
        self.assertIs(material_source.factory, supervisor.factory)
        self.assertIs(supervisor.factory.fluids, supervisor.fluids)
        supervisor.prepare_production()
        self.assertIs(self.builder.construction_materials, material_source)
        self.game.query.assert_not_called()

    def test_nested_construction_dependency_cycle_is_bounded_and_unwinds(self):
        self.builder.construction_materials = self.materials
        self.factory.ensure_product = Mock(side_effect=lambda *args: self.builder.ensure_plan(self.obs, self.plan))
        result = self.builder.ensure_plan(self.obs, self.plan)
        self.assertEqual(result["reason"], "construction material production dependency cycle")
        self.assertEqual(result["evidence"]["stack"], ["steel-plate"])
        self.factory.ensure_product.assert_called_once()
        self.assertEqual(self.materials._stack, ())
        self.factory.ensure_product.side_effect = RuntimeError("lost observation")
        with self.assertRaises(RuntimeError):
            self.materials.ensure(self.obs, self.blocked)
        self.assertEqual(self.materials._stack, ())

    def test_blocked_or_incomplete_producer_never_becomes_a_material_or_craft(self):
        for result in ({"type": "build", "name": "chemical-plant"},
                       {"status": "waiting", "reason": "research unlock needed"},
                       {"status": "blocked", "reason": "fluid geometry unsupported"}):
            self.factory.ensure_product = Mock(return_value=result)
            self.assertIs(self.materials.ensure(self.obs, self.blocked), result)
            self.assertEqual(self.materials._stack, ())

    def test_output_pickup_uses_remaining_demand_and_cannot_claim_success_for_empty_port(self):
        self.factory.ensure_product = Mock(return_value=READY)
        self.obs["inventory"]["steel-plate"] = 2
        self.obs["entities"] = [{"name": "transport-belt", "position": {"x": 9.5, "y": 10.5},
                                 "belt_inventory": {"steel-plate": 40}}]
        result = self.materials.ensure(self.obs, self.blocked)
        self.assertEqual((result["type"], result["count"]), ("take", 3))
        self.obs["entities"] = []
        self.assertEqual(self.materials.ensure(self.obs, self.blocked)["status"], "waiting")

    def test_missing_or_invalid_bounded_leaf_demand_never_requests_a_producer(self):
        self.factory.ensure_product = Mock()
        for value in (None, True, 0, -1, 1.5):
            blocked = deepcopy(self.blocked)
            blocked["evidence"]["need"] = value
            self.assertEqual(self.materials.ensure(self.obs, blocked)["status"], "blocked")
        self.factory.ensure_product.assert_not_called()

    def test_restart_reuses_producer_reservation_and_reobserves_missing_construction(self):
        self.builder.construction_materials = self.materials
        first = self.builder.ensure_plan(self.obs, self.plan)
        old_plan = deepcopy(self.factory.state["blocks"]["recipe:steel-plate"])
        # Losing the process-local cycle stack cannot mint materials or readiness.
        self.builder.construction_materials = ConstructionMaterials(self.factory)
        self.obs["tick"] = 0
        second = self.builder.ensure_plan(self.obs, self.plan)
        self.assertEqual(first, second)
        self.assertEqual(self.factory.state["blocks"]["recipe:steel-plate"], old_plan)
        self.assertEqual(second["type"], "build")


if __name__ == "__main__":
    unittest.main()
