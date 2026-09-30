from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_array_production import ArrayProduction, phase_lab_count, owned_production, construction_order, foundation_stages
from factorio_ai.deterministic_layout_policy import resolve_layout_policy, save_layout_policy


class ArrayExecutionTests(unittest.TestCase):
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
