from pathlib import Path
from copy import deepcopy
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_array_production import ArrayProduction, phase_lab_count, owned_production, construction_order, foundation_stages
from factorio_ai.deterministic_layout_policy import resolve_layout_policy, save_layout_policy


class ArrayExecutionTests(unittest.TestCase):
    def test_completed_array_advances_item_oil_and_fluid_triggers_through_paid_producers(self):
        from factorio_ai.deterministic_factory import DeterministicFactory
        for trigger, expected in (({"type": "craft-item", "item": "widget"}, "assembler"),
                                  ({"type": "mine-entity", "entities": ["crude-oil"]}, "pumpjack"),
                                  ({"type": "craft-fluid", "fluid": "petroleum-gas"}, "pumpjack")):
            with self.subTest(trigger=trigger):
                plan = {"owner": "owner", "completed": True, "phase": "science", "world_id": "world",
                        "surface": "nauvis", "catalog_fingerprint": "catalog", "entities": [],
                        "source_links": {}, "power_plan": {"entities": []}, "lab_inputs": ["red"],
                        "demand": {"external_rates": {}}}
                factory = SimpleNamespace(state={"array_plans": {"owner": plan}},
                    catalog=SimpleNamespace(fingerprint="catalog", technologies={"trigger": {"research_trigger": trigger}}),
                    graph=SimpleNamespace(science_rate_per_minute=30,
                        next_research=Mock(return_value={"technology": "trigger", "kind": "trigger"})),
                    builder=SimpleNamespace(ensure_plan=Mock(return_value={"status": "succeeded"})), _save=Mock(),
                    ensure_product=Mock(return_value={"type": "build", "name": "assembler"}),
                    fluids=SimpleNamespace(ensure_source=Mock(return_value={"type": "build", "name": "pumpjack"})))
                factory.ensure_research_trigger = lambda obs, name: DeterministicFactory.ensure_research_trigger(factory, obs, name)
                arrays = ArrayProduction(factory)
                arrays._geometry = Mock(return_value={"underground_distance": 5, "pole_wire": 7.5, "pole_supply": 2.5})
                arrays._targets = Mock(return_value=({"red": 30}, ["red"], "science"))
                arrays._sources = Mock(return_value=(None, {}))
                obs = {"world_id": "world", "surface": "nauvis", "tick": 100,
                       "enabled_recipes": {"underground-belt": True}}
                with patch("factorio_ai.deterministic_array_production.plan_digest", return_value="owner"), \
                     patch("factorio_ai.deterministic_array_production.phase_lab_count", return_value=1), \
                     patch("factorio_ai.deterministic_array_production.foundation_stages", return_value=[]), \
                     patch("factorio_ai.deterministic_array_production.owned_production", return_value=None):
                    result = arrays.next_action(obs)
                self.assertEqual(result, {"type": "build", "name": expected})
                if expected == "assembler":
                    factory.ensure_product.assert_called_once_with(obs, "widget")
                    factory.fluids.ensure_source.assert_not_called()
                else:
                    factory.fluids.ensure_source.assert_called_once_with(obs,
                        "crude-oil" if trigger["type"] == "mine-entity" else "petroleum-gas")
                    factory.ensure_product.assert_not_called()

    def test_underground_supply_clearance_revalidates_footprint_and_preserves_owned_source(self):
        source = {"position": {"x": .5, "y": .5}, "facing": 4, "item": "ore"}
        outlet = {"name": "underground-belt", "position": {"x": 4.5, "y": .5},
                  "direction": 4, "belt_to_ground_type": "output"}
        source_belt = {"name": "transport-belt", "position": source["position"], "direction": 4}
        factory = SimpleNamespace(state={"production_layout": {"anchor": {"x": 10, "y": 0}}},
            catalog=SimpleNamespace(entities={}), _reserved=lambda: [], _save=Mock(),
            _material_route=Mock(return_value={"ok": False, "reason": "no route within bounds"}))
        factory.builder = SimpleNamespace(_occupied_by_plan=lambda rows: set(),
            can_place=Mock(return_value={"ok": True}), clear_route_obstacle=Mock(return_value={"ok": False}))
        local = {"entities": [{"name": "small-electric-pole", "position": {"x": .5, "y": 3.5}}],
                 "ports": [{"kind": "item", "item": "ore", "direction": "input", "facing": 4,
                            "position": {"x": .5, "y": .5}}]}
        arrays = ArrayProduction(factory)
        arrays._clearance = Mock(return_value={"type": "mine", "name": "tree-01", "count": 1,
                                               "position": outlet["position"]})
        with patch("factorio_ai.deterministic_array_production._survey", return_value={"ok": True, "clear": [1]}) as survey, \
             patch("factorio_ai.deterministic_array_production.reserved_aisles", return_value=set()), \
             patch("factorio_ai.deterministic_underground_routes.plan_underground_route", return_value={
                 "ok": False, "clearance_plan": {"segments": [source_belt, outlet]}}) as underground:
            result = arrays._reserve({"world_id": "world", "surface": "nauvis"}, local, "proof", {"ore": source})
            resumed = arrays._reserve({"world_id": "world", "surface": "nauvis"}, local, "proof", {"ore": source})
            self.assertEqual(resumed, result)
            self.assertEqual(survey.call_count, 1)
            self.assertEqual(underground.call_count, 1)
            arrays._reserve({"world_id": "changed", "surface": "nauvis"}, local, "proof", {"ore": source})
            self.assertEqual(survey.call_count, 2)
            self.assertEqual(underground.call_count, 2)
        self.assertEqual(result["type"], "mine")
        self.assertEqual(arrays._clearance.call_count, 3)
        self.assertTrue(all(call.args == ([outlet],) for call in arrays._clearance.call_args_list))
        self.assertTrue(underground.call_args.kwargs["clear_natural"])

    def test_reserved_supply_link_retains_underground_geometry_for_later_inspection(self):
        from factorio_ai.deterministic_builder import FactoryBuilder
        from factorio_ai.deterministic_input_links import _geometry
        def belt(name, x, **extra):
            return {"name": name, "position": {"x": x, "y": .5}, "direction": 4, **extra}
        inlet = belt("underground-belt", .5, belt_to_ground_type="input")
        outlet = belt("underground-belt", 4.5, belt_to_ground_type="output")
        route = {"ok": True, "segments": [inlet, outlet] + [belt("transport-belt", x+.5) for x in range(5, 11)],
                 "underground_pairs": [{"input": inlet, "output": outlet, "max_distance": 5}]}
        factory = SimpleNamespace(state={"blocks": {}, "links": {}, "power_links": {},
                                         "production_layout": {"anchor": {"x": 10, "y": 0}}},
            catalog=SimpleNamespace(entities={}, fingerprint="catalog"), _reserved=lambda: [], _save=Mock(),
            _material_route=Mock(return_value=route), _power_grid=Mock(return_value={"ok": True, "live": [{"x": 8.5, "y": 3.5}]}),
            _power_route=Mock(return_value={"ok": True, "path": [{"x": 10.5, "y": 3.5}]}))
        factory.builder = SimpleNamespace(_occupied_by_plan=lambda rows: FactoryBuilder._occupied_by_plan(None, rows),
                                          can_place=lambda rows: {"ok": True})
        local = {"entities": [{"name": "small-electric-pole", "position": {"x": .5, "y": 3.5}}],
                 "ports": [{"kind": "item", "item": "ore", "direction": "input", "facing": 4,
                            "position": {"x": .5, "y": .5}}]}
        source = {"kind": "item", "item": "ore", "direction": "output", "facing": 4,
                  "position": {"x": .5, "y": .5}}
        with patch("factorio_ai.deterministic_array_production._survey", return_value={"ok": True, "clear": [1]}), \
             patch("factorio_ai.deterministic_array_production.reserved_aisles", return_value=set()):
            result = ArrayProduction(factory)._reserve({"world_id": "world", "surface": "nauvis"}, local, "proof", {"ore": source})
        self.assertEqual(result["status"], "waiting")
        saved = factory.state["links"]["arrays:proof:ore"]
        _, edges, _ = _geometry(saved)
        self.assertTrue(any(point == (4.5, .5) for point, _ in edges[(.5, .5)]))

    def test_cached_compilation_still_rechecks_sources_and_placement_before_actions(self):
        factory = SimpleNamespace(state={}, catalog=SimpleNamespace(fingerprint="catalog", technologies={}),
                                  graph=SimpleNamespace(science_rate_per_minute=30, next_research=Mock(return_value=None)))
        manager = ArrayProduction(factory)
        manager._geometry = Mock(return_value={})
        manager._targets = Mock(return_value=({"iron-plate": 30}, [], "construction-foundation"))
        manager._sources = Mock(return_value=(None, {"iron-ore": {"position": {"x": 0, "y": 0}}}))
        manager._reserve = Mock(return_value={"type": "mine", "name": "tree"})
        obs = {"world_id": "world", "surface": "nauvis", "enabled_recipes": {"underground-belt": True}}
        with patch("factorio_ai.deterministic_array_production.optimize_array",
                   return_value={"entities": [], "demand": {"external_rates": {"iron-ore": 30}}}) as compile_plan:
            for _ in range(2):
                self.assertEqual(manager.next_action(obs)["type"], "mine")
            compile_plan.assert_called_once()
            self.assertEqual(manager._sources.call_count, 2)
            self.assertEqual(manager._reserve.call_count, 2)
            blocked = {"status": "blocked", "reason": "source changed"}
            manager._sources.return_value = (blocked, None)
            self.assertEqual(manager.next_action(obs), blocked)
            self.assertEqual(manager._reserve.call_count, 2)

    def test_local_blueprint_cache_recompiles_for_changed_inputs_and_returns_independent_plans(self):
        factory = SimpleNamespace(catalog=SimpleNamespace(fingerprint="catalog"))
        manager = ArrayProduction(factory)
        obs = {"world_id": "world", "surface": "nauvis", "enabled_recipes": {"widget": True}, "tick": 100}
        args = [obs, {"widget": 30}, {"belt_speed": .03125}, ["widget"], 15, "science"]
        with patch("factorio_ai.deterministic_array_production.optimize_array", return_value={"entities": []}) as compile_plan:
            first = manager._local_blueprint(*args)
            first["entities"].append({"name": "changed"})
            obs["tick"] = 200
            self.assertEqual(manager._local_blueprint(*args)["entities"], [])
            compile_plan.assert_called_once()
            for field, value in (("world_id", "other"), ("surface", "other"),
                                 ("enabled_recipes", {"widget": True, "fast-belt": True})):
                obs[field] = value
                manager._local_blueprint(*args)
            factory.catalog.fingerprint = "changed"
            manager._local_blueprint(*args)
            for index, value in ((1, {"widget": 40}), (2, {"belt_speed": .0625}),
                                 (3, ["widget", "green"]), (4, 16), (5, "next-phase")):
                args[index] = value
                manager._local_blueprint(*args)
            self.assertEqual(compile_plan.call_count, 10)

    def test_intermediate_producers_start_in_dependency_order_before_science_exports(self):
        from test_deterministic_arrays import array_catalog, GEOMETRY
        from factorio_ai.deterministic_arrays import optimize_array, PRODUCERS
        catalog = array_catalog()
        obs = {"enabled_recipes": dict.fromkeys(catalog.recipes, True)}
        plan = optimize_array(catalog, obs, {"widget": 30, "transport-belt": 30}, GEOMETRY,
                              labs=["widget"], lab_count=3)
        before = deepcopy(plan)
        stages = foundation_stages(plan, catalog, 5, include_intermediates=True)
        nodes = {n["item"]: n for n in plan["demand"]["nodes"]}
        available = set(plan["demand"]["external_rates"])
        for stage in stages:
            self.assertTrue(set(nodes[stage["item"]]["inputs"]).issubset(available))
            available.add(stage["item"])
            self.assertEqual(sum(e["name"] in PRODUCERS for e in stage["plan"]["entities"]), 1)
            self.assertFalse(any(e["name"] == "lab" for e in stage["plan"]["entities"]))
        order = [s["item"] for s in stages]
        self.assertLess(order.index("gear"), order.index("transport-belt"))
        self.assertLess(order.index("transport-belt"), order.index("widget"))
        self.assertEqual(next(s for s in stages if s["item"] == "transport-belt")["sources"], ["iron-plate"])
        self.assertEqual(len(stages), sum(e["name"] in PRODUCERS for e in plan["entities"]))
        self.assertLess(len(stages[0]["plan"]["entities"]), len(plan["entities"]))
        self.assertEqual(plan, before)

    def test_foundation_first_iron_cell_has_paths_without_copper_construction(self):
        rows, ports = [], []
        def row(name, x, y, direction=0, **fields):
            return {"name": name, "position": {"x": x, "y": y}, "direction": direction, **fields}
        for x, item, ore in ((0, "iron-plate", "iron-ore"), (10, "copper-plate", "copper-ore")):
            rows += [row("stone-furnace", x, 0, _array_recipe=item),
                     row("transport-belt", x-2.5, .5, 4, _item=ore),
                     row("inserter", x-1.5, .5, 12, _item=ore, _role="input"),
                     row("inserter", x+.5, -1.5, 0, _item="coal", _role="input"),
                     row("inserter", x+.5, 1.5, 0, _item=item, _role="output"),
                     row("transport-belt", x+.5, 2.5, 4, _item=item),
                     row("transport-belt", x+1.5, 2.5, 4, _item=item)]
            ports += [{"item": ore, "direction": "input", "position": {"x": x-2.5, "y": .5}},
                      {"item": item, "direction": "output", "position": {"x": x+1.5, "y": 2.5}}]
        rows += [row("transport-belt", x+.5, -2.5, 4, _item="coal") for x in range(11)]
        rows += [row("small-electric-pole", x, y) for x, y in
                 ((-.5, -1.5), (1.5, 1.5), (5.5, -1.5), (9.5, -1.5), (11.5, 1.5))]
        ports.append({"item": "coal", "direction": "input", "position": {"x": .5, "y": -2.5}})
        plan = {"entities": rows, "ports": ports,
                "demand": {"nodes": [{"item": item, "recipe": item} for item in ("iron-plate", "copper-plate")]}}
        stages = foundation_stages(plan, SimpleNamespace(entities={}), 5)
        self.assertEqual([s["item"] for s in stages], ["iron-plate", "copper-plate"])
        first = stages[0]
        self.assertEqual(first["sources"], ["coal", "iron-ore"])
        self.assertFalse(any(e.get("_item") == "copper-ore" for e in first["plan"]["entities"]))
        self.assertEqual(sum(e["name"] == "stone-furnace" for e in first["plan"]["entities"]), 1)
        self.assertLess(len(first["plan"]["entities"]), len(rows))
        self.assertEqual(sum(e["name"] == "small-electric-pole" for e in first["plan"]["entities"]), 2)
    def test_failed_external_routes_do_not_publish_a_district(self):
        factory = SimpleNamespace(state={}, catalog=SimpleNamespace(entities={}),
                                  _reserved=lambda: [], _save=Mock(),
                                  _material_route=Mock(return_value={"ok": False}))
        factory.builder = SimpleNamespace(_occupied_by_plan=lambda rows: set(),
                                          clear_route_obstacle=lambda *args, **kwargs: {"ok": False},
                                          can_place=lambda rows: {"ok": True})
        local = {"entities": [{"name": "transport-belt", "position": {"x": .5, "y": .5}}],
                 "ports": [{"item": "ore", "direction": "input", "facing": 8,
                            "position": {"x": .5, "y": .5}}]}
        with patch("factorio_ai.deterministic_array_production._survey", return_value={"ok": True, "clear": [1]}), \
             patch("factorio_ai.deterministic_array_production.reserved_aisles", return_value=set()):
            result = ArrayProduction(factory)._reserve({}, local, "owner", {"ore": {"position": {"x": 10.5, "y": .5}}})
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(factory._material_route.call_count, 3)
        self.assertEqual(factory.state, {})
        factory._save.assert_not_called()

    def test_receivers_and_output_arms_precede_activating_inputs(self):
        entities = [{"name": "inserter", "_role": "input"},
                    {"name": "stone-furnace"}, {"name": "inserter", "_role": "tap"},
                    {"name": "inserter", "_role": "output"},
                    {"name": "underground-belt"}, {"name": "transport-belt"}]
        plan = {"entities": entities, "owner": "test"}
        ordered = construction_order(plan)
        self.assertEqual([entities.index(e) for e in ordered["entities"]], [4, 5, 1, 3, 2, 0])
        self.assertIs(plan["entities"], entities)
        self.assertEqual(ordered["owner"], "test")

    def test_construction_capacity_precedes_science_and_preserves_requested_rates(self):
        factory = SimpleNamespace(state={"array_demands": {"iron-plate": 90}},
                                  graph=SimpleNamespace(science_rate_per_minute=30))
        manager = ArrayProduction(factory)
        obs = {"enabled_recipes": {"automation-science-pack": True}}
        targets, packs, phase = manager._targets(obs)
        self.assertEqual(targets, {"iron-plate": 90, "copper-plate": 30})
        self.assertEqual(packs, [])
        self.assertEqual(phase, "construction-foundation")
        factory.state["array_plans"] = {"first": {"phase": phase, "completed": False}}
        self.assertEqual(manager._targets(obs)[2], phase)
        factory.state["array_plans"]["first"]["completed"] = True
        targets, packs, phase = manager._targets(obs)
        self.assertEqual(phase, "science")
        self.assertEqual(targets["automation-science-pack"], 30)
        self.assertEqual(targets["iron-plate"], 90)

    def test_phase_labs_use_slowest_supported_research(self):
        catalog = SimpleNamespace(technologies={
            "quick": {"unit_energy": 600, "ingredients": [{"name": "red"}]},
            "slow": {"unit_energy": 1800, "ingredients": [{"name": "red"}]},
            "future": {"unit_energy": 7200, "ingredients": [{"name": "blue"}]}})
        geometry = {"lab_speed": 1, "lab_bonus": 0, "lab_drain": 100}
        self.assertEqual(phase_lab_count(catalog, ["red"], geometry, 30), 15)
        self.assertEqual(phase_lab_count(catalog, ["red", "blue"], geometry, 30), 60)

    def test_owned_counters_exclude_other_factories_and_track_rebuilds(self):
        machine = {"name": "assembling-machine-1", "recipe": "red", "position": {"x": .5, "y": .5}}
        plan = {"lab_inputs": ["red"], "entities": [machine],
                "demand": {"nodes": [{"item": "red", "recipe": "red", "output_amount": 2}]}}
        obs = {"entities": [{**machine, "unit_number": 7, "products_finished": 20},
                            {**machine, "position": {"x": 9.5, "y": .5}, "unit_number": 8,
                             "products_finished": 10000}],
               "production": {"red": {"produced": 20040, "consumed": 10}}}
        self.assertEqual(owned_production(plan, obs)["red"],
                         {"produced": 40, "consumed": 10, "units": [7]})
        obs["entities"][0]["unit_number"] = 9
        self.assertEqual(owned_production(plan, obs)["red"]["units"], [9])
        obs["entities"][0].pop("products_finished")
        self.assertIsNone(owned_production(plan, obs))

    def test_saved_policy_preserves_legacy_and_refuses_migration(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(resolve_layout_policy(root, None, new_world=False), "legacy")
            with self.assertRaises(ValueError):
                resolve_layout_policy(root, "arrays-v2", new_world=False)
            self.assertEqual(resolve_layout_policy(root, None, new_world=True), "arrays-v2")
            save_layout_policy(root, "arrays-v2")
            self.assertEqual(resolve_layout_policy(root, None, new_world=False), "arrays-v2")
            with self.assertRaises(ValueError):
                save_layout_policy(root, "legacy")


if __name__ == "__main__":
    unittest.main()
