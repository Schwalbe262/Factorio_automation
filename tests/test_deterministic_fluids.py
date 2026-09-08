from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_fluids import FluidProduction, exterior_connections, recipe_fluid_geometry


def box(index, role, x, y, direction, **extra):
    return {"index": index, "production_type": role, "pipe_connections": [
        {"connection_type": "normal", "direction": direction,
         "positions": [{"x": x, "y": y}, {"x": -y, "y": x}, {"x": -x, "y": -y}, {"x": y, "y": -x}]}], **extra}


def fluid(name, amount=1, **extra):
    return {"name": name, "amount": amount, "type": "fluid", **extra}


def entity(size, categories, boxes):
    return {"selection_box": {"left_top": {"x": -size/2, "y": -size/2},
                               "right_bottom": {"x": size/2, "y": size/2}},
            "crafting_categories": categories, "crafting_speed": 1, "fluidbox_prototypes": boxes}


def catalog():
    refinery = entity(5, ["oil-processing"], [box(1, "input", -1, 2, 8), box(2, "input", 1, 2, 8),
                      box(3, "output", -2, -2, 0), box(4, "output", 0, -2, 0), box(5, "output", 2, -2, 0)])
    chemical = entity(3, ["chemistry"], [box(1, "input", -1, -1, 0), box(2, "input", 1, -1, 0),
                       box(3, "output", -1, 1, 8), box(4, "output", 1, 1, 8)])
    recipes = {
        "basic-oil-processing": {"categories": ["oil-processing"], "energy": 5,
             "ingredients": [fluid("crude-oil", 100, fluidbox_index=2)],
             "products": [fluid("petroleum-gas", 45, fluidbox_index=3)]},
        "advanced-oil-processing": {"categories": ["oil-processing"], "energy": 5,
             "ingredients": [fluid("water", 50), fluid("crude-oil", 100)],
             "products": [fluid("heavy-oil", 25), fluid("light-oil", 45), fluid("petroleum-gas", 55)]},
        "sulfuric-acid": {"categories": ["chemistry"], "energy": 1,
             "ingredients": [{"type": "item", "name": "iron-plate", "amount": 1},
                             {"type": "item", "name": "sulfur", "amount": 5}, fluid("water", 100)],
             "products": [fluid("sulfuric-acid", 50)]},
    }
    tank = entity(3, [], [box(1, "none", -1, -1, 0, volume=25000)])
    return SimpleNamespace(recipes=recipes, entities={"oil-refinery": refinery, "chemical-plant": chemical,
                                                      "storage-tank": tank})


class GeometryTests(unittest.TestCase):
    def test_internal_coordinates_move_one_tile_outward(self):
        self.assertEqual(exterior_connections(box(1, "input", -1, -1, 0))[0],
                         {"position": {"x": -1, "y": -2}, "facing": 0})
        self.assertEqual(exterior_connections(box(1, "output", 1, 1, 4))[0]["position"], {"x": 2, "y": 1})

    def test_basic_petroleum_third_output_is_absolute_refinery_box_five(self):
        data = catalog()
        geometry = recipe_fluid_geometry(data.recipes["basic-oil-processing"], data.entities["oil-refinery"])
        self.assertEqual([(p["item"], p["fluidbox_index"], p["position"]) for p in geometry["fluid_ports"]],
                         [("crude-oil", 2, {"x": 1, "y": 3}), ("petroleum-gas", 5, {"x": 2, "y": -3})])

    def test_advanced_recipe_binds_each_coproduct_in_role_order(self):
        data = catalog()
        geometry = recipe_fluid_geometry(data.recipes["advanced-oil-processing"], data.entities["oil-refinery"])
        self.assertEqual([p["fluidbox_index"] for p in geometry["fluid_ports"]], [1, 2, 3, 4, 5])

    def test_explicit_slot_reservation_precedes_default_assignment(self):
        data = catalog()
        recipe = deepcopy(data.recipes["advanced-oil-processing"])
        recipe["ingredients"][1]["fluidbox_index"] = 1
        geometry = recipe_fluid_geometry(recipe, data.entities["oil-refinery"])
        self.assertEqual([p["fluidbox_index"] for p in geometry["fluid_ports"][:2]], [2, 1])

    def test_duplicate_slot_filter_and_missing_geometry_fail_closed(self):
        data = catalog()
        for invalid in ("duplicate", "filter", "missing"):
            recipe = deepcopy(data.recipes["advanced-oil-processing"])
            machine = deepcopy(data.entities["oil-refinery"])
            if invalid == "duplicate":
                for ingredient in recipe["ingredients"]: ingredient["fluidbox_index"] = 1
            elif invalid == "filter": machine["fluidbox_prototypes"][0]["filter"] = "steam"
            else: machine["fluidbox_prototypes"][0]["pipe_connections"] = []
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                recipe_fluid_geometry(recipe, machine)


class FluidProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)), query=Mock(return_value=None))
        self.builder = Mock()
        self.catalog = catalog()
        self.fluids = FluidProduction(self.game, self.builder, self.catalog)
        self.factory = Mock()
        self.factory.reserve_site.side_effect = lambda plan, *args, **kwargs: plan
        self.factory.register_plan.side_effect = lambda key, plan, obs: plan
        self.factory._reserved.return_value = []
        self.factory.request_recipe_unlock.return_value = {"status": "waiting", "reason": "research queued"}
        self.builder._occupied_by_plan.return_value = set()
        self.fluids.factory = self.factory
        self.obs = {"world_id": "fluid-test", "tick": 100, "entities": [],
                    "enabled_recipes": {"oil-refinery": True, "chemical-plant": True, "basic-oil-processing": True,
                                        "advanced-oil-processing": True, "sulfuric-acid": True}}

    def test_rotated_plan_moves_bound_petroleum_pipe_with_machine(self):
        north = self.fluids.plan("basic-oil-processing", "oil-refinery", {"x": .5, "y": .5})
        east = self.fluids.plan("basic-oil-processing", "oil-refinery", {"x": 20.5, "y": 10.5}, 4)
        self.assertTrue(north["ok"])
        self.assertTrue(east["ok"])
        port = next(p for p in east["ports"] if p["item"] == "petroleum-gas")
        self.assertEqual(port["position"], {"x": 25.5, "y": 12.5})
        self.assertEqual(port["fluidbox_index"], 5)
        self.assertEqual(east["required_items"]["pipe"], 6)

    def test_acid_exposes_two_real_item_inputs_and_does_not_overlap_fluids(self):
        plan = self.fluids.plan("sulfuric-acid", "chemical-plant", {"x": .5, "y": .5})
        self.assertTrue(plan["ok"], plan.get("reason"))
        self.assertEqual({p["item"] for p in plan["ports"] if p["kind"] == "item"}, {"iron-plate", "sulfur"})
        self.assertEqual(plan["input_rates"]["water"], 6000)

    def test_wrong_machine_or_missing_fluid_geometry_has_no_entities(self):
        for recipe, machine in (("basic-oil-processing", "chemical-plant"), ("missing", "oil-refinery")):
            result = self.fluids.plan(recipe, machine, {"x": .5, "y": .5})
            self.assertFalse(result["ok"])
            self.assertEqual(result["entities"], [])

    def test_offshore_selection_envelope_does_not_shift_its_land_side_outlet(self):
        self.catalog.entities["offshore-pump"] = entity(2, [], [box(1, "output", 0, 0, 8)])
        plan = self.fluids._utility_plan("offshore-pump", {"x": .5, "y": .5}, 4, "water")
        self.assertEqual(plan["ports"][0]["position"], {"x": -.5, "y": .5})
        self.assertEqual(plan["ports"][0]["facing"], 12)

    def test_build_yield_exposes_input_output_ports_and_never_claims_flow(self):
        self.builder.ensure_plan.return_value = {"type": "build", "name": "oil-refinery"}
        result = self.fluids.ensure_source(self.obs, "petroleum-gas")
        self.assertEqual(result["type"], "build")
        self.assertEqual(result["evidence"]["ports"][0]["item"], "petroleum-gas")
        self.assertEqual(result["evidence"]["input_ports"][0]["item"], "crude-oil")
        self.assertFalse(result["evidence"]["flow_verified"])

    def test_assembled_and_connected_but_zero_fluid_is_waiting(self):
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.factory.ensure_power_connection.return_value = {"status": "succeeded"}
        self.fluids._ensure_raw_source = Mock(return_value={"status": "succeeded", "evidence": {"ports": [
            {"kind": "fluid", "item": "crude-oil", "direction": "output", "position": {"x": 20.5, "y": 20.5}}]}})
        self.fluids._connect_pipe = Mock(return_value={"status": "succeeded"})
        result = self.fluids.ensure_source(self.obs, "petroleum-gas")
        self.assertEqual(result["status"], "waiting")
        self.assertFalse(result["evidence"]["flow_verified"])
        self.assertEqual(result["evidence"]["available"], 0)
        self.game.query.return_value = {"ok": True, "available": 10}
        self.assertEqual(self.fluids.ensure_source(self.obs, "petroleum-gas")["status"], "succeeded")

    def test_aggregate_machine_inventory_is_never_output_proof(self):
        e = {"name": "chemical-plant", "position": {"x": .5, "y": .5}}
        self.obs["entities"] = [{**e, "inventory": {"sulfur": 100}}]
        result = self.fluids._source_evidence(self.obs, {"entities": [e]}, "sulfur")
        self.assertEqual(result["available"], 0)

    def test_advanced_refining_reserves_all_coproduct_capacity_before_input_feed(self):
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.factory.ensure_power_connection.return_value = {"status": "succeeded"}
        self.fluids._ensure_buffer = Mock(side_effect=[{"status": "succeeded"}, {"type": "build", "name": "storage-tank"}])
        self.fluids._ensure_raw_source = Mock()
        result = self.fluids.ensure_source(self.obs, "light-oil")
        self.assertEqual(result["type"], "build")
        self.assertEqual([call.args[1] for call in self.fluids._ensure_buffer.call_args_list], ["heavy-oil", "light-oil"])
        self.fluids._ensure_raw_source.assert_not_called()

    def test_existing_plan_reused_on_resume_but_other_world_clears_it(self):
        self.builder.ensure_plan.return_value = {"type": "build"}
        self.fluids.ensure_source(self.obs, "petroleum-gas")
        resumed = FluidProduction(self.game, self.builder, self.catalog)
        resumed.factory = self.factory
        resumed.ensure_source(self.obs, "petroleum-gas")
        self.assertEqual(self.factory.reserve_site.call_count, 1)
        resumed.ensure_source({**self.obs, "world_id": "another-world"}, "petroleum-gas")
        self.assertEqual(self.factory.reserve_site.call_count, 2)

    def test_invalid_rate_and_locked_recipe_do_not_mutate_world(self):
        for rate in (0, -1, float("nan")):
            self.assertEqual(self.fluids.ensure_source(self.obs, "petroleum-gas", rate_per_minute=rate)["status"], "blocked")
        self.obs["enabled_recipes"] = {}
        self.assertEqual(self.fluids.ensure_source(self.obs, "petroleum-gas")["status"], "waiting")
        self.factory.request_recipe_unlock.assert_called_once_with(self.obs, "basic-oil-processing")
        self.builder.ensure_plan.assert_not_called()

    def test_capacity_requirement_never_silently_reuses_smaller_block(self):
        self.builder.ensure_plan.return_value = {"type": "build"}
        self.fluids.ensure_source(self.obs, "petroleum-gas")
        result = self.fluids.ensure_source(self.obs, "petroleum-gas", rate_per_minute=1000)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("capacity", result["reason"])

    def underground_geometry(self):
        self.catalog.entities["pipe-to-ground"] = {"fluidbox_prototypes": [{"pipe_connections": [
            {"connection_type": "normal", "direction": 0, "positions": [{"x": 0, "y": 0}]},
            {"connection_type": "underground", "direction": 8, "positions": [{"x": 0, "y": 0}], "max_underground_distance": 10}]}]}
        self.obs["enabled_recipes"]["pipe-to-ground"] = True
        self.builder.can_place.return_value = {"ok": True}
        self.builder.route.return_value = {"ok": True, "path": [{"x": .5, "y": -3.5}, {"x": .5, "y": -8.5}]}
        return ({"kind": "fluid", "item": "petroleum-gas", "direction": "output", "facing": 0,
                 "position": {"x": .5, "y": .5}},
                {"kind": "fluid", "item": "petroleum-gas", "direction": "input", "position": {"x": .5, "y": -8.5}})

    def test_enclosed_outlet_tunnels_with_opposed_mouths_within_live_range(self):
        source, destination = self.underground_geometry()
        result = self.fluids._underground_escape(self.obs, source, destination, [])
        self.assertTrue(result["ok"])
        pair = [e for e in result["entities"] if e["name"] == "pipe-to-ground"]
        self.assertEqual([(e["position"], e["direction"]) for e in pair],
                         [({"x": .5, "y": -.5}, 8), ({"x": .5, "y": -2.5}, 0)])

    def test_tunnel_cannot_steal_existing_underground_connection(self):
        source, destination = self.underground_geometry()
        self.obs["entities"] = [{"name": "pipe-to-ground", "position": {"x": .5, "y": -1.5}, "direction": 0}]
        result = self.fluids._underground_escape(self.obs, source, destination, [])
        self.assertFalse(result["ok"])
        self.builder.route.assert_not_called()

    def test_unsupported_tunnel_profile_or_locked_recipe_fails_closed(self):
        source, destination = self.underground_geometry()
        self.catalog.entities["pipe-to-ground"]["fluidbox_prototypes"][0]["pipe_connections"][0]["direction"] = 4
        self.assertFalse(self.fluids._underground_escape(self.obs, source, destination, [])["ok"])
        self.obs["enabled_recipes"]["pipe-to-ground"] = False
        self.assertEqual(self.fluids._underground_escape(self.obs, source, destination, [])["needs_recipe"], "pipe-to-ground")

    def test_pipe_link_checkpoint_reuses_built_route_and_shared_reservation(self):
        source, destination = self.underground_geometry()
        self.fluids._sync(self.obs)
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        first = self.fluids._connect_pipe(self.obs, source, destination, "test-link", {"entities": []})
        second = self.fluids._connect_pipe(self.obs, source, destination, "test-link", {"entities": []})
        self.assertEqual(first, second)
        self.builder.route.assert_called_once()
        self.factory.register_plan.assert_called_once()
        self.assertEqual(self.factory.register_plan.call_args.args[0], "fluid-link:test-link")

    def test_every_machine_coproduct_outlet_is_connected_to_storage(self):
        self.fluids._sync(self.obs)
        producer = self.fluids.plan("advanced-oil-processing", "oil-refinery", {"x": .5, "y": .5}, count=2)
        self.fluids.state["buffers"]["heavy-oil"] = {"ok": True, "entities": [], "ports": [
            {"kind": "fluid", "item": "heavy-oil", "direction": "input", "position": {"x": 10.5, "y": 10.5}}]}
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.fluids._connect_pipe = Mock(return_value={"status": "succeeded"})
        self.assertEqual(self.fluids._ensure_buffer(self.obs, "heavy-oil", producer)["status"], "succeeded")
        self.assertEqual(self.fluids._connect_pipe.call_count, 2)
        self.assertEqual([call.args[1]["machine_index"] for call in self.fluids._connect_pipe.call_args_list], [0, 1])

    def test_cracking_job_finishes_output_route_after_tank_drops_below_trigger(self):
        self.fluids._sync(self.obs)
        self.catalog.entities["storage-tank"]["fluidbox_prototypes"][0]["volume"] = 1000
        self.fluids.state["buffers"]["heavy-oil"] = {"entities": []}
        self.fluids.state["sources"]["heavy-oil-cracking"] = {"entities": []}
        self.obs["enabled_recipes"]["heavy-oil-cracking"] = True
        self.fluids._source_evidence = Mock(return_value={"available": 850})
        self.fluids._ensure_recipe = Mock(return_value={"status": "waiting"})
        self.assertEqual(self.fluids.maintain_coproducts(self.obs)["status"], "waiting")
        self.fluids._source_evidence.return_value = {"available": 750}
        self.fluids._ensure_recipe.return_value = {"status": "succeeded"}
        self.fluids._ensure_buffer = Mock(return_value={"type": "build", "name": "pipe"})
        self.assertEqual(self.fluids.maintain_coproducts(self.obs)["type"], "build")
        self.fluids._ensure_buffer.return_value = {"status": "succeeded"}
        self.assertIsNone(self.fluids.maintain_coproducts(self.obs))
        self.assertEqual(self.fluids.state["coproduct_jobs"], {})
        self.assertEqual(self.fluids._ensure_recipe.call_count, 3)


if __name__ == "__main__":
    unittest.main()
