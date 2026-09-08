from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_armaments import Armaments
from factorio_ai.deterministic_bootstrap import DeterministicBootstrap
from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_construction_buffer import BUFFER_KEY, ensure_construction_buffer
from factorio_ai.deterministic_factory import DeterministicFactory


def entity(name, x, y, direction=0, **extra):
    return {"name": name, "position": {"x": x, "y": y}, "direction": direction, **extra}


class ConstructionBufferTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(backend="assisted", cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)),
                                    query=Mock(return_value={"ok": True, "slots": 1}))
        self.catalog = SimpleNamespace(fingerprint="catalog", entities={})
        self.bootstrap = DeterministicBootstrap(self.game)
        self.builder = FactoryBuilder(self.game, self.bootstrap, self.catalog)
        self.builder.can_place = Mock(return_value={"ok": True})
        self.builder.ensure_plan = Mock(return_value={"type": "build", "name": "wooden-chest"})
        self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        self.factory.ensure_power_connection = Mock(return_value={"status": "succeeded"})
        self.owner = {"ok": True, "entities": [
            entity("assembling-machine-1", -6.5, -34.5, recipe="transport-belt"),
            entity("transport-belt", -3.5, -34.5, 4), entity("transport-belt", -2.5, -34.5, 4),
            entity("small-electric-pole", -4.5, -36.5)],
            "ports": [{"kind": "item", "item": "transport-belt", "direction": "output", "facing": 4,
                       "position": {"x": -2.5, "y": -34.5}}]}
        self.obs = {"world_id": "world", "tick": 100, "inventory": {}, "position": {"x": 0, "y": 0},
                    "entities": [{**deepcopy(e), "unit_number": i + 1} for i, e in enumerate(self.owner["entities"])]}
        self.factory._sync(self.obs)
        self.factory.state["blocks"]["recipe:transport-belt"] = deepcopy(self.owner)

    def reserve(self):
        ensure_construction_buffer(self.factory, self.obs)
        return self.factory.state["blocks"][BUFFER_KEY]

    def complete(self, *, slots=1, stock=0):
        plan = self.reserve()
        self.obs["entities"].extend([{**deepcopy(plan["entities"][0]), "unit_number": 20,
                                      "inventory": {"transport-belt": stock}},
                                     {**deepcopy(plan["entities"][2]), "unit_number": 21}])
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.game.query.return_value = {"ok": True, "slots": slots}
        return plan

    def test_persisted_mall_gets_side_buffer_without_changing_any_output_or_clearance(self):
        plan = self.reserve()
        chest, pole, arm = plan["entities"]
        self.assertEqual(arm, entity("inserter", -3.5, -35.5, 8))
        self.assertEqual(chest, entity("wooden-chest", -3.5, -36.5))
        self.assertEqual(pole, self.owner["entities"][3])
        self.assertEqual(self.factory.state["blocks"]["recipe:transport-belt"], self.owner)
        self.assertEqual(plan["source"], self.owner["entities"][1])
        self.assertFalse(self.builder._occupied_by_plan(plan["entities"]) & self.factory._port_clearances())
        self.builder.ensure_plan.assert_called_once_with(self.obs, {"ok": True, "entities": [chest]})
        self.factory.ensure_power_connection.assert_not_called()

    def test_reserved_or_live_collision_tries_other_side(self):
        self.factory.state["blocks"]["other"] = {"entities": [entity("wooden-chest", -3.5, -36.5)]}
        plan = self.reserve()
        self.assertEqual(plan["entities"][2], entity("inserter", -3.5, -33.5, 0))
        self.assertEqual(plan["source_port"], self.owner["ports"][0])
        del self.factory.state["blocks"][BUFFER_KEY]
        del self.factory.state["blocks"]["other"]
        self.builder.can_place.side_effect = lambda rows: {"ok": rows[0]["position"]["y"] > -34.5}
        self.assertEqual(self.reserve()["entities"][2]["position"]["y"], -33.5)

    def test_unreserved_player_chest_and_inserter_are_never_adopted(self):
        for existing in (entity("wooden-chest", -3.5, -36.5), entity("inserter", -3.5, -35.5, 8)):
            with self.subTest(name=existing["name"]):
                self.factory.state["blocks"].pop(BUFFER_KEY, None)
                self.obs["entities"] = self.obs["entities"][:4] + [{**existing, "unit_number": 100}]
                plan = self.reserve()
                self.assertEqual(plan["entities"][2]["position"]["y"], -33.5)

    def test_one_slot_bar_is_observed_before_building_feeder_and_rechecked_on_resume(self):
        plan = self.reserve()
        self.obs["entities"].append({**plan["entities"][0], "unit_number": 20})
        self.game.query.return_value = {"ok": True, "slots": 16}
        self.builder.ensure_plan.reset_mock()
        action = ensure_construction_buffer(self.factory, self.obs)
        self.assertEqual((action["type"], action["slots"]), ("bar", 1))
        self.builder.ensure_plan.assert_not_called()
        self.factory.ensure_power_connection.assert_not_called()
        self.assertIn("source.unit_number~=x.source_unit", self.game.query.call_args.args[0])
        self.assertIn("chest.unit_number~=x.chest_unit", self.game.query.call_args.args[0])

    def test_destroyed_chest_recovers_exact_owned_feeder_before_rebuilding(self):
        plan = self.complete()
        self.obs["entities"] = [e for e in self.obs["entities"] if e["unit_number"] != 20]
        self.builder.ensure_plan.reset_mock()
        action = ensure_construction_buffer(self.factory, self.obs)
        self.assertEqual((action["type"], action["count"], action["expected_entity_unit"]), ("mine", 1, 21))
        self.assertEqual(action["expected_entity_world_id"], "world")
        self.assertEqual(action["position"], plan["entities"][2]["position"])
        self.builder.ensure_plan.assert_not_called()

    def test_missing_feeder_or_power_is_repaired_before_enabling_buffer_collection(self):
        plan = self.complete(stock=64)
        self.obs["entities"] = [e for e in self.obs["entities"] if e["unit_number"] != 21]
        self.builder.ensure_plan.return_value = {"type": "build", **plan["entities"][2]}
        self.assertEqual(ensure_construction_buffer(self.factory, self.obs)["name"], "inserter")
        self.assertEqual(self.bootstrap.construction_buffers, {})
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.factory.ensure_power_connection.return_value = {"status": "waiting"}
        self.assertEqual(ensure_construction_buffer(self.factory, self.obs)["status"], "waiting")
        self.assertEqual(self.bootstrap.construction_buffers, {})

    def test_owned_buffer_collects_32_before_full_eight_item_assembler_without_waiting(self):
        self.complete(stock=64)
        self.obs["entities"][0]["inventory"] = {"transport-belt": 8}
        self.assertIsNone(ensure_construction_buffer(self.factory, self.obs))
        action = self.bootstrap.ensure_item(self.obs, "transport-belt", 32)
        self.assertEqual((action["name"], action["count"]), ("wooden-chest", 32))
        self.assertEqual(action["reason"], "collect buffered construction transport-belt")
        self.obs["entities"][-2]["inventory"] = {"transport-belt": 3}
        self.assertEqual(self.bootstrap.ensure_item(self.obs, "transport-belt", 32)["count"], 3)
        self.obs["entities"][-2]["inventory"] = {}
        self.game.query.return_value = {"count": 8}
        fallback = self.bootstrap.ensure_item(self.obs, "transport-belt", 32)
        self.assertEqual((fallback["name"], fallback["count"]), ("assembling-machine-1", 8))

    def routine(self):
        self.complete(stock=100)
        self.obs["entities"][0]["inventory"] = {"transport-belt": 8}
        self.obs["entities"].append(entity("gun-turret", 20, 20, unit_number=30, inventory={"firearm-magazine": 20}))
        self.obs["technologies"] = {"automation": True}
        self.obs["enabled_recipes"] = {"electric-mining-drill": True}
        self.catalog.recipe_for_product = lambda item: {"ingredients": [{"name": "iron-plate", "amount": 4}],
                                                       "products": [{"name": "firearm-magazine", "amount": 1}]}
        self.game.query.side_effect = lambda body: {"ok": True, "slots": 1} if "buffer_identity_changed" in body else {"count": 8}
        self.factory.ensure_product = Mock(side_effect=lambda obs, item, **kwargs:
                                           self.bootstrap.ensure_item(obs, "transport-belt", 32))
        return Armaments(self.game, self.bootstrap, self.builder, self.factory, self.catalog)

    def test_cold_routine_revalidates_owned_full_buffer_before_collecting_small_assembler_output(self):
        routine = self.routine()
        self.assertEqual(self.bootstrap.construction_buffers, {})
        original = deepcopy(self.factory.state["blocks"])
        for held, needed in ((0, 32), (5, 27)):
            with self.subTest(held=held):
                self.bootstrap.construction_buffers.clear()
                self.obs["inventory"] = {"transport-belt": held}
                with patch.object(self.bootstrap, "_recipe") as recipe:
                    action = routine.next_action(self.obs)
                    recipe.assert_not_called()
                self.assertEqual((action["type"], action["name"], action["count"]), ("take", "wooden-chest", needed))
                self.assertEqual(action["position"], original[BUFFER_KEY]["entities"][0]["position"])
                self.assertEqual(self.bootstrap.construction_buffers["transport-belt"]["unit_number"], 20)
                self.assertEqual(self.factory.state["blocks"], original)
                self.assertEqual(self.obs["entities"][-3]["inventory"], {"transport-belt": 100})

    def test_routine_does_not_create_unsaved_buffer_or_reuse_another_world_reservation(self):
        for mode in ("missing", "world_changed"):
            with self.subTest(mode=mode):
                routine = self.routine()
                if mode == "missing":
                    del self.factory.state["blocks"][BUFFER_KEY]
                else:
                    self.obs["world_id"] = "new-world"
                with patch("factorio_ai.deterministic_construction_buffer.ensure_construction_buffer") as buffer:
                    action = routine.next_action(self.obs)
                    buffer.assert_not_called()
                self.assertNotIn(BUFFER_KEY, self.factory.state["blocks"])
                self.assertEqual((action["type"], action["name"], action["count"]), ("take", "assembling-machine-1", 8))

    def test_routine_buffer_repair_wait_and_identity_failure_precede_material_collection(self):
        routine = self.routine()
        for result in ({"type": "build", "name": "inserter"}, {"status": "waiting"},
                       {"status": "blocked", "reason": "buffer_identity_changed"}):
            with self.subTest(result=result):
                self.factory.ensure_product.reset_mock()
                with patch("factorio_ai.deterministic_construction_buffer.ensure_construction_buffer", return_value=result):
                    self.assertEqual(routine.next_action(self.obs), result)
                self.factory.ensure_product.assert_not_called()
        self.game.query.side_effect = None
        self.game.query.return_value = {"ok": False, "reason": "buffer_identity_changed"}
        self.bootstrap.construction_buffers["transport-belt"] = {"stale": True}
        result = routine.next_action(self.obs)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["evidence"]["reason"], "buffer_identity_changed")
        self.assertEqual(self.bootstrap.construction_buffers, {})
        self.factory.ensure_product.assert_not_called()

    def test_routine_seeding_and_locked_mining_still_precede_buffer_binding(self):
        routine = self.routine()
        self.obs["entities"][-1]["inventory"] = {}
        self.obs["inventory"] = {"firearm-magazine": 5}
        with patch("factorio_ai.deterministic_construction_buffer.ensure_construction_buffer") as buffer:
            action = routine.next_action(self.obs)
            self.assertEqual((action["type"], action["item"]), ("insert", "firearm-magazine"))
            buffer.assert_not_called()
        self.obs["entities"][-1]["inventory"] = {"firearm-magazine": 20}
        self.obs["enabled_recipes"] = {}
        self.factory.request_recipe_unlock = Mock(return_value={"status": "waiting"})
        with patch("factorio_ai.deterministic_construction_buffer.ensure_construction_buffer") as buffer:
            self.assertEqual(routine.next_action(self.obs), {"status": "waiting"})
            buffer.assert_not_called()

    def test_stale_buffer_world_or_unit_does_not_override_ordinary_collection(self):
        self.complete(stock=64)
        self.obs["entities"][0]["inventory"] = {"transport-belt": 8}
        ensure_construction_buffer(self.factory, self.obs)
        self.game.query.return_value = {"count": 8}
        for change in ({"world_id": "different"}, {"unit_number": 999}):
            with self.subTest(change=change):
                saved = deepcopy(self.bootstrap.construction_buffers)
                self.bootstrap.construction_buffers["transport-belt"].update(change)
                self.assertEqual(self.bootstrap.ensure_item(self.obs, "transport-belt", 32)["name"], "assembling-machine-1")
                self.bootstrap.construction_buffers = saved

    def test_owned_source_direction_recovers_but_changed_port_fails_closed(self):
        self.complete()
        self.obs["entities"][1]["direction"] = 0
        result = ensure_construction_buffer(self.factory, self.obs)
        self.assertEqual((result["type"], result["count"], result["expected_entity_unit"]), ("mine", 1, 2))
        self.assertEqual(result["expected_entity_world_id"], "world")
        self.assertEqual(self.bootstrap.construction_buffers, {})
        self.obs["entities"][1]["direction"] = 4
        self.factory.state["blocks"]["recipe:transport-belt"]["ports"][0]["position"]["x"] += 1
        self.assertEqual(ensure_construction_buffer(self.factory, self.obs)["status"], "blocked")

    def test_missing_owned_source_is_rebuilt_with_normal_materials(self):
        plan = self.complete()
        self.obs["entities"] = [e for e in self.obs["entities"] if e["unit_number"] != 2]
        self.builder.ensure_plan.return_value = {"type": "craft", "recipe": "transport-belt", "count": 1}
        action = ensure_construction_buffer(self.factory, self.obs)
        self.assertEqual(action["type"], "craft")
        self.builder.ensure_plan.assert_called_with(self.obs, {"ok": True, "entities": [plan["source"]]})
        self.assertEqual(self.bootstrap.construction_buffers, {})

    def test_contaminated_source_or_chest_is_not_barred_or_used_for_collection(self):
        self.complete(stock=64)
        for reason in ("buffer_chest_contaminated", "buffer_source_contaminated", "buffer_identity_changed"):
            with self.subTest(reason=reason):
                self.game.query.return_value = {"ok": False, "reason": reason}
                self.builder.ensure_plan.reset_mock()
                result = ensure_construction_buffer(self.factory, self.obs)
                self.assertEqual((result["status"], result["evidence"]["reason"]), ("blocked", reason))
                self.builder.ensure_plan.assert_not_called()
                self.assertEqual(self.bootstrap.construction_buffers, {})
        lua = self.game.query.call_args.args[0]
        self.assertIn("for lane=1,2", lua)
        self.assertIn("buffer_chest_contaminated", lua)
        self.assertIn("buffer_source_contaminated", lua)

    def test_unavailable_buffer_site_or_unbuilt_mall_does_not_block_production(self):
        self.builder.can_place.return_value = {"ok": False}
        self.assertIsNone(ensure_construction_buffer(self.factory, self.obs))
        self.assertNotIn(BUFFER_KEY, self.factory.state["blocks"])
        self.obs["entities"][0]["recipe"] = "iron-gear-wheel"
        self.builder.can_place.reset_mock()
        self.assertIsNone(ensure_construction_buffer(self.factory, self.obs))
        self.builder.can_place.assert_not_called()

    def test_character_must_move_to_chest_before_ordinary_bar_action(self):
        self.complete(slots=16)
        self.game.backend = "character"
        self.assertEqual(ensure_construction_buffer(self.factory, self.obs)["type"], "move")
        self.obs["position"] = self.factory.state["blocks"][BUFFER_KEY]["entities"][0]["position"]
        self.assertEqual(ensure_construction_buffer(self.factory, self.obs)["type"], "bar")


if __name__ == "__main__":
    unittest.main()
