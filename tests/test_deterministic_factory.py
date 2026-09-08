from copy import deepcopy
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.factory_templates import build_template


def ready(**evidence):
    return {"status": "succeeded", "reason": "observed", "evidence": evidence}


def port(item, x=0.5, y=0.5, direction="output", facing=4):
    return {"kind": "item", "item": item, "direction": direction,
            "position": {"x": x, "y": y}, "facing": facing}


class FactoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)),
                                    query=Mock(return_value={"ok": True, "covered": 0, "blocked": []}))
        self.bootstrap = Mock()
        self.catalog = SimpleNamespace(fingerprint="catalog-a", recipes={}, entities={}, technologies={
            "automation": {"name": "automation", "ingredients": [{"name": "automation-science-pack", "amount": 1}],
                           "unit_count": 10, "prerequisites": []},
            "logistics": {"name": "logistics", "ingredients": [{"name": "automation-science-pack", "amount": 1}],
                          "unit_count": 20, "prerequisites": ["automation"]},
            "military": {"name": "military", "ingredients": [{"name": "automation-science-pack", "amount": 1}],
                         "unit_count": 10, "prerequisites": ["automation"]},
            "gun-turret": {"name": "gun-turret", "ingredients": [{"name": "automation-science-pack", "amount": 1}],
                          "unit_count": 10, "prerequisites": ["military"]},
        })
        self.catalog.recipe_for_product = lambda name: self.catalog.recipes.get(name)
        self.catalog.technology_order = lambda names, **kwargs: ["automation", "military", "gun-turret"]
        self.builder = FactoryBuilder(self.game, self.bootstrap, self.catalog)
        self.builder.ensure_plan = Mock(return_value=ready())
        self.builder.can_place = Mock(return_value={"ok": True})
        self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        self.factory.ensure_power_connection = Mock(return_value=ready())
        self.factory.graph = Mock()
        self.factory.graph.for_first_rocket.return_value = {"bom": {"science_packs": {"automation-science-pack": 10, "logistic-science-pack": 10}}}
        self.factory.graph.next_research.return_value = {"technology": "logistics", "kind": "research"}
        self.factory.graph.machines_for_recipe.return_value = [{"name": "assembling-machine-1", "crafting_speed": .5}]
        self.obs = {"world_id": "one", "tick": 100, "inventory": {}, "entities": [], "position": {"x": 10, "y": 10},
                    "enabled_recipes": {}, "technologies": {}, "production": {}, "research": "automation", "research_progress": 0}
        self.factory._sync(self.obs)

    def seed_lab(self):
        lab = build_template("labs_row", inputs=["automation-science-pack", "logistic-science-pack"])
        self.factory.state["blocks"]["research:labs"] = lab
        self.obs["entities"] = [{"name": "lab", "position": {"x": .5, "y": .5}, "inventory": {}}]
        self.factory._ensure_lab = Mock(return_value=ready())

    def test_initial_science_batch_is_bounded_even_when_one_pack_is_in_flight(self):
        self.seed_lab()
        self.bootstrap.ensure_item.return_value = {"type": "craft", "recipe": "automation-science-pack", "count": 10}
        result = self.factory.next_action(self.obs)
        self.assertEqual(result["count"], 10)
        self.bootstrap.ensure_item.assert_called_once_with(self.obs, "automation-science-pack", 10)
        self.obs["production"] = {"automation-science-pack": {"produced": 10, "consumed": 1}}
        self.obs["entities"][0]["inventory"] = {"automation-science-pack": 9}
        self.obs["research_progress"] = .01
        self.assertEqual(self.factory.next_action(self.obs)["status"], "waiting")
        self.bootstrap.ensure_item.assert_called_once()

    def test_existing_science_stock_reduces_seed_batch_and_uses_lab_input(self):
        self.seed_lab()
        self.obs["inventory"] = {"automation-science-pack": 3}
        self.obs["entities"][0]["inventory"] = {"automation-science-pack": 2}
        result = self.factory.next_action(self.obs)
        self.assertEqual((result["type"], result["count"], result["inventory"]), ("insert", 3, "lab_input"))
        self.obs["inventory"] = {}
        self.factory.next_action(self.obs)
        self.bootstrap.ensure_item.assert_called_once_with(self.obs, "automation-science-pack", 5)

    def automatic_sources(self):
        self.seed_lab()
        self.obs["technologies"] = {"automation": True}
        self.factory.ensure_product = Mock(side_effect=lambda obs, item: ready(ports=[port(item)]))
        self.factory.connect_input = Mock(return_value=ready())

    def test_research_starts_only_after_automatic_production_and_lab_links(self):
        self.automatic_sources()
        result = self.factory.next_action(self.obs)
        self.assertEqual((result["type"], result["technology"]), ("research", "logistics"))
        self.factory.connect_input.assert_called_once()
        self.bootstrap.ensure_item.assert_not_called()
        self.factory.connect_input.return_value = {"type": "build", "name": "transport-belt"}
        self.assertEqual(self.factory.next_action(self.obs)["name"], "transport-belt")

    def test_almost_complete_research_and_large_stocks_do_not_claim_completion(self):
        self.automatic_sources()
        self.obs.update(research="logistics", research_progress=.999)
        self.obs["inventory"] = {"automation-science-pack": 1000}
        result = self.factory.next_action(self.obs)
        self.assertEqual(result["status"], "waiting")
        self.assertFalse(result["evidence"]["input_handcarry"])

    def test_defense_priority_research_walks_prerequisites_after_automation(self):
        self.automatic_sources()
        self.factory.priority_research = ["gun-turret"]
        self.assertEqual(self.factory.next_action(self.obs)["technology"], "military")
        self.obs["technologies"]["military"] = True
        self.assertEqual(self.factory.next_action(self.obs)["technology"], "gun-turret")

    def test_trigger_waits_for_real_technology_credit(self):
        self.automatic_sources()
        self.obs["technologies"]["logistics"] = True
        self.catalog.technologies["trigger"] = {"research_trigger": {"type": "craft-item", "item": "electronic-circuit"}, "prerequisites": []}
        self.factory.graph.next_research.return_value = {"technology": "trigger", "kind": "trigger"}
        self.assertEqual(self.factory.next_action(self.obs)["status"], "waiting")
        self.factory.ensure_product.assert_called_with(self.obs, "electronic-circuit")

    def test_flow_proof_excludes_bootstrap_science_and_requires_both_deltas(self):
        self.automatic_sources()
        self.obs["production"] = {"automation-science-pack": {"produced": 10, "consumed": 10}}
        self.factory.next_action(self.obs)
        self.obs["production"]["automation-science-pack"]["produced"] = 12
        self.assertFalse(self.factory.flow_evidence(self.obs)["automation-science-pack"]["production_and_consumption_verified"])
        self.obs["production"]["automation-science-pack"]["consumed"] = 11
        proof = self.factory.flow_evidence(self.obs)["automation-science-pack"]
        self.assertEqual(proof["produced_since_automation"], 2)
        self.assertTrue(proof["production_and_consumption_verified"])
        self.obs["tick"] = 10
        self.factory._sync(self.obs)
        self.assertEqual(self.factory.flow_evidence(self.obs), {})

    def test_world_and_catalog_changes_invalidate_saved_plans(self):
        self.factory.state["blocks"]["old"] = {"ok": True, "entities": []}
        self.factory._save()
        recovered = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        self.assertIn("old", recovered.state["blocks"])
        self.obs["world_id"] = "two"
        recovered._sync(self.obs)
        self.assertEqual(recovered.state["blocks"], {})
        recovered.state["blocks"]["other"] = {}
        recovered._fingerprint = "catalog-b"
        recovered._sync(self.obs)
        self.assertEqual(recovered.state["blocks"], {})

    def test_fixed_reservations_allow_only_identical_shared_endpoints(self):
        pipe = {"name": "pipe", "position": {"x": .5, "y": .5}, "_fluid": "water"}
        original = {"ok": True, "entities": [pipe]}
        self.assertTrue(self.factory.register_plan("pipe-a", original, self.obs)["ok"])
        self.assertTrue(self.factory.register_plan("pipe-b", original, self.obs)["ok"])
        wrong = deepcopy(original)
        wrong["entities"][0]["_fluid"] = "petroleum-gas"
        self.assertFalse(self.factory.register_plan("pipe-c", wrong, self.obs)["ok"])
        self.assertNotIn("pipe-c", self.factory.state["blocks"])

    def test_dynamic_sites_preserve_grid_and_future_plan_footprints(self):
        origin = build_template("assembler_row", recipe="gear", inputs=["iron-plate"], output="gear")
        first = self.factory.reserve_site(origin, "a", self.obs)
        second = self.factory.reserve_site(origin, "b", self.obs)
        self.assertTrue(first["ok"] and second["ok"])
        self.assertFalse(self.builder._occupied_by_plan(first["entities"]) & self.builder._occupied_by_plan(second["entities"]))
        self.assertTrue(all(e["position"]["x"] % 1 == .5 for e in first["entities"]))

    def test_incompatible_materials_fail_without_route(self):
        self.builder.route = Mock()
        result = self.factory.connect_input(self.obs, port("iron-plate"), port("copper-plate", 10.5, direction="input"), "bad")
        self.assertEqual(result["status"], "blocked")
        self.builder.route.assert_not_called()

    def test_second_consumer_uses_side_tap_without_rewriting_first_belt(self):
        source = port("iron-plate")
        destination = port("iron-plate", 10.5, direction="input")
        first = self.factory.connect_input(self.obs, source, destination, "first")
        self.assertEqual(first["status"], "succeeded", first)
        before = deepcopy(self.factory.state["links"]["first"])
        second = self.factory.connect_input(self.obs, source, port("iron-plate", 10.5, 8.5, "input", 0), "second")
        self.assertEqual(second["status"], "succeeded", second)
        self.assertEqual(self.factory.state["links"]["first"], before)
        self.assertTrue(any(e["name"] == "inserter" for e in self.factory.state["links"]["second"]["entities"]))
        self.assertFalse(second["evidence"]["flow_verified"])

    def test_saved_connection_is_reobserved_for_reconstruction(self):
        self.factory.connect_input(self.obs, port("iron-plate"), port("iron-plate", 10.5, direction="input"), "link")
        self.builder.ensure_plan.return_value = {"type": "build", "name": "transport-belt"}
        self.assertEqual(self.factory.connect_input(self.obs, port("iron-plate"), port("iron-plate", 10.5, direction="input"), "link")["type"], "build")

    def test_raw_source_port_uses_half_tile_geometry_and_never_claims_flow(self):
        self.bootstrap.discover_cell.side_effect = lambda resource, receiver: {"ok": True, "complete": True,
            "receiver": {"name": receiver, "position": {"x": 10 if resource == "iron-ore" else 30.5, "y": 20 if resource == "iron-ore" else 30.5}}}
        self.factory._fuel_burner = Mock(return_value=ready())
        result = self.factory.ensure_product(self.obs, "iron-plate")
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(result["evidence"]["flow_verified"])
        plan = self.factory.state["blocks"]["source:iron-plate"]
        self.assertTrue(all(e["position"]["x"] % 1 == .5 and e["position"]["y"] % 1 == .5 for e in plan["entities"]))

    def test_locked_and_cyclic_recipes_do_not_fall_back_to_handcraft(self):
        recipe = {"name": "gear", "ingredients": [{"name": "gear"}], "products": [{"name": "gear"}]}
        self.catalog.recipes["gear"] = recipe
        self.assertIn("locked", self.factory.ensure_product(self.obs, "gear")["reason"])
        self.obs["enabled_recipes"]["gear"] = True
        self.assertIn("cycle", self.factory.ensure_product(self.obs, "gear")["reason"])
        self.bootstrap.ensure_item.assert_not_called()

    def test_fluid_recipes_are_delegated_to_machine_only_producer(self):
        self.catalog.recipes["plastic-bar"] = {"name": "plastic-bar", "ingredients": [{"name": "petroleum-gas", "type": "fluid"}], "products": [{"name": "plastic-bar"}]}
        self.obs["enabled_recipes"]["plastic-bar"] = True
        self.factory.fluids = Mock()
        self.factory.fluids.ensure_source.return_value = {"type": "build", "name": "chemical-plant"}
        self.assertEqual(self.factory.ensure_product(self.obs, "plastic-bar")["name"], "chemical-plant")
        self.factory.fluids.ensure_source.assert_called_once_with(self.obs, "plastic-bar")

    def test_electric_wire_route_jumps_impassable_conveyor_wall(self):
        # A complete tile wall blocks walk/pipe routing, but a legal wire hop
        # crosses it without placing a pole on the wall.
        self.game.query.return_value = {"ok": True, "blocked": [{"x": 3.5, "y": y + .5} for y in range(-100, 101)]}
        route = self.factory._power_route({"x": .5, "y": .5}, {"x": 12.5, "y": .5})
        self.assertTrue(route["ok"], route)
        self.assertTrue(all(position["x"] != 3.5 for position in route["path"]))
        self.assertTrue(all(math.dist((a["x"], a["y"]), (b["x"], b["y"])) <= 7
                            for a, b in zip(route["path"], route["path"][1:])))

    def test_natural_oil_and_fluid_triggers_wait_for_actual_research(self):
        self.automatic_sources()
        self.obs["technologies"]["logistics"] = True
        self.factory.fluids = Mock()
        self.factory.fluids.ensure_source.return_value = ready(ports=[])
        for trigger, product in (({"type": "mine-entity", "entities": ["crude-oil"]}, "crude-oil"),
                                 ({"type": "craft-fluid", "fluid": "sulfuric-acid", "count": 1}, "sulfuric-acid")):
            self.catalog.technologies["trigger"] = {"research_trigger": trigger, "prerequisites": []}
            self.factory.graph.next_research.return_value = {"technology": "trigger", "kind": "trigger"}
            self.assertEqual(self.factory.next_action(self.obs)["status"], "waiting")
            self.factory.fluids.ensure_source.assert_called_with(self.obs, product)

    def test_machine_capability_request_is_persisted_for_scheduler(self):
        self.catalog.technologies["military"]["unlocks"] = ["tank"]
        self.assertEqual(self.factory.request_recipe_unlock(self.obs, "tank")["status"], "waiting")
        self.assertEqual(self.factory.state["capability_research"], ["military"])
        self.automatic_sources()
        self.assertEqual(self.factory.next_action(self.obs)["technology"], "military")

    def test_furnace_fuel_is_owned_only_after_real_belt_construction(self):
        burner = {"name": "stone-furnace", "position": {"x": 10, "y": 20}}
        self.factory.connect_input = Mock(return_value={"type": "build", "name": "transport-belt"})
        result = self.factory._fuel_burner(self.obs, burner, port("coal"))
        self.assertEqual(result["type"], "build")
        self.assertFalse(self.factory.owns_automated_burner(burner))
        self.factory.connect_input.return_value = ready()
        self.assertEqual(self.factory._fuel_burner(self.obs, burner, port("coal"))["status"], "succeeded")
        self.assertTrue(self.factory.owns_automated_burner(burner))
        self.bootstrap.ensure_item.assert_not_called()

    def test_recipe_capacity_adds_real_machines_and_merges_output_belts(self):
        self.catalog.recipes["gear"] = {"name": "gear", "energy": .5, "ingredients": [{"name": "iron-plate", "amount": 2}],
                                        "products": [{"name": "gear", "amount": 1}]}
        self.obs["enabled_recipes"]["gear"] = True
        self.factory._source_endpoint = Mock(return_value=ready(ports=[port("iron-plate")]))
        self.factory.connect_input = Mock(return_value=ready())
        self.factory._merge_output = Mock(return_value=ready())
        result = self.factory.ensure_product(self.obs, "gear", rate_per_minute=120)
        self.assertEqual(result["evidence"]["machines_constructed"], 2)
        machines = [e for block in self.factory.state["blocks"].values() for e in block["entities"] if e["name"] == "assembling-machine-1"]
        self.assertEqual(len(machines), 2)
        self.factory._merge_output.assert_called_once()
        self.assertFalse(result["evidence"]["flow_verified"])

    def test_raw_capacity_uses_mining_and_smelting_limits_and_automatic_fuel(self):
        self.catalog.entities.update({"iron-ore": {"mining_time": 1}, "electric-mining-drill": {"mining_speed": .5},
                                      "burner-mining-drill": {"mining_speed": .25}, "stone-furnace": {"crafting_speed": 1}})
        self.catalog.recipes["iron-plate"] = {"name": "iron-plate", "energy": 3.2}
        self.obs["enabled_recipes"]["electric-mining-drill"] = True
        self.factory.ensure_product = Mock(return_value=ready(ports=[port("coal")]))
        self.factory._raw_capacity_site = Mock(side_effect=lambda obs, item, key: self.factory._electric_source_plan(item, int(key.rsplit(":", 1)[1]) * 12, 20))
        self.factory.connect_input = Mock(return_value=ready())
        self.factory._merge_output = Mock(return_value=ready())
        result = self.factory._expand_raw_source(self.obs, "iron-plate", 60, port("iron-plate"))
        self.assertEqual(result["evidence"]["additional_cells"], 3)
        self.assertEqual(result["evidence"]["nominal_capacity_per_minute"], 71.25)
        self.assertEqual(self.factory.connect_input.call_count, 3)
        self.assertEqual(self.factory._merge_output.call_count, 3)

    def test_shared_pipe_endpoint_accepts_normalized_direction_and_missing_fluid_label(self):
        original = {"ok": True, "entities": [{"name": "pipe", "position": {"x": .5, "y": .5}, "direction": 4}]}
        self.factory.register_plan("block", original, self.obs)
        routed = deepcopy(original)
        routed["entities"][0].update(direction=0, _fluid="water")
        self.assertTrue(self.factory.register_plan("fluid-link", routed, self.obs)["ok"])

    def test_source_pole_search_handles_chest_between_two_mining_drills(self):
        self.bootstrap.discover_cell.return_value = {"ok": True, "complete": True,
            "receiver": {"name": "wooden-chest", "position": {"x": -51.5, "y": -.5}},
            "drill": {"name": "burner-mining-drill", "position": {"x": -51, "y": 1}}}
        nearby = [{"name": "burner-mining-drill", "position": {"x": -51, "y": 1}},
                  {"name": "burner-mining-drill", "position": {"x": -51, "y": -2}}]
        occupied = self.builder._occupied_by_plan(nearby)
        self.builder.can_place.side_effect = lambda entities: {"ok": not bool(self.builder._occupied_by_plan(entities) & occupied)}
        self.factory._fuel_burner = Mock(return_value=ready())
        result = self.factory.ensure_product(self.obs, "coal")
        self.assertEqual(result["status"], "succeeded")
        plan = self.factory.state["blocks"]["source:coal"]
        pole = next(e for e in plan["entities"] if e["name"] == "small-electric-pole")
        self.assertNotIn((pole["position"]["x"], pole["position"]["y"]), occupied)

    def test_future_producer_exit_stays_clear_of_other_material_routes(self):
        self.factory.register_plan("producer", {"ok": True, "entities": [
            {"name": "transport-belt", "position": {"x": .5, "y": .5}, "direction": 4}],
            "ports": [port("copper-plate")]}, self.obs)
        route = self.factory._material_route({"x": -2.5, "y": .5}, {"x": 4.5, "y": .5}, self.factory._reserved(),
                                             start_direction=4, end_direction=4)
        self.assertTrue(route["ok"], route)
        self.assertNotIn({"x": 1.5, "y": .5}, route["path"])
        own = self.factory._material_route({"x": .5, "y": .5}, {"x": 4.5, "y": .5}, self.factory._reserved(),
                                           start_direction=4, end_direction=4)
        self.assertTrue(own["ok"], own)
        self.assertIn({"x": 1.5, "y": .5}, own["path"])

    def test_rollback_invalidates_science_allowance_and_burner_ownership(self):
        self.seed_lab()
        self.factory.state["bootstrap_science"] = {"automation-science-pack": {"produced": 10, "allowance": 0}}
        self.factory.state["automated_burners"] = ["stone-furnace:0,0"]
        self.obs["tick"] = 1
        self.factory.next_action(self.obs)
        self.assertEqual(self.factory.state["automated_burners"], [])
        self.bootstrap.ensure_item.assert_called_once_with(self.obs, "automation-science-pack", 10)

    def test_long_inserter_bridge_crosses_a_continuous_foreign_belt_without_mixing(self):
        foreign = [{"name": "transport-belt", "position": {"x": .5, "y": y + .5}, "direction": 8} for y in range(-100, 101)]
        def survey(body):
            if "long inserter recipe is locked" in body:
                return {"ok": True, "belts": foreign}
            return {"ok": True, "blocked": [e["position"] for e in foreign]}
        self.game.query.side_effect = survey
        route = self.factory._material_route({"x": -8.5, "y": .5}, {"x": 8.5, "y": .5}, [], start_direction=4, end_direction=4)
        self.assertTrue(route["ok"], route)
        arm = next(e for e in route["segments"] if e.get("name") == "long-handed-inserter")
        self.assertEqual(arm["direction"], 12)
        self.assertEqual(arm["position"], {"x": -.5, "y": .5})
        self.assertFalse(any(e.get("name", "transport-belt") == "transport-belt" and e["position"]["x"] == .5 for e in route["segments"]))
        self.assertTrue(any(e.get("name") == "small-electric-pole" for e in route["segments"]))

    def test_capacity_merge_never_faces_existing_output_belt_head_on(self):
        source = port("iron-plate", 4.5, .5, facing=12)
        destination = port("iron-plate", .5, .5, facing=4)
        self.assertEqual(self.factory._merge_output(self.obs, source, destination, "merge")["status"], "succeeded")
        belts = [e for e in self.factory.state["links"]["merge"]["entities"] if e["name"] == "transport-belt"]
        self.assertEqual(belts[-1]["direction"], 4)
        self.assertNotEqual(belts[-2]["direction"], 12)

    def test_capacity_bridge_requires_observed_power_before_connection_success(self):
        source, destination = port("iron-plate", -8.5), port("iron-plate", 8.5)
        self.factory.state["links"]["merge"] = {"ok": True, "entities": [
            {"name": "long-handed-inserter", "position": {"x": -.5, "y": .5}, "direction": 12},
            {"name": "small-electric-pole", "position": {"x": -.5, "y": 2.5}}]}
        blocked = {"status": "blocked", "reason": "bridge pole disconnected"}
        self.factory.ensure_power_connection.return_value = blocked
        self.assertEqual(self.factory._merge_output(self.obs, source, destination, "merge"), blocked)
        self.factory.ensure_power_connection.assert_called_once_with(
            self.obs, "merge:merge", self.factory.state["links"]["merge"])
        self.factory.ensure_power_connection.return_value = ready()
        self.assertEqual(self.factory._merge_output(self.obs, source, destination, "merge")["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
