from copy import deepcopy
import unittest

from factorio_ai.deterministic_production import ProductionGraph
from factorio_ai.world_catalog import WorldCatalog
from tests.test_world_catalog import fixture, material, recipe, technology


def production_fixture():
    data = fixture()
    data["entities"]["character"] = {"type": "character", "crafting_categories": ["crafting"], "crafting_speed": 1}
    data["entities"]["assembler"]["items_to_place_this"] = [{"name": "assembler", "count": 1}]
    data["entities"]["assembler"].update({"electric": True, "energy_usage": 1000})
    data["entities"]["rocket-silo"] = {"type": "rocket-silo", "rocket_parts_required": 7,
                                          "crafting_categories": ["rocket-building"], "crafting_speed": 1,
                                          "items_to_place_this": [{"name": "rocket-silo", "count": 1}]}
    data["recipes"]["assembler"] = recipe("assembler", [material("iron-plate", 5)])
    data["recipes"]["lab"] = recipe("lab", [material("gear")])
    for name in ("rocket-silo", "space-platform-starter-pack"):
        data["recipes"][name] = recipe(name, [material("iron-plate", 5)], enabled=False)
    data["recipes"]["rocket-part"] = recipe("rocket-part", [material("iron-plate", 3)], enabled=False, categories=["rocket-building"])
    data["recipes"]["science"] = recipe("science", [material("gear")], [material("science", 2)], energy=10, enabled=False)
    data["technologies"] = {
        "starter": technology("starter", unlocks=["science"], research_trigger={"type": "craft-item", "item": {"name": "lab"}, "count": 1}),
        "silo-tech": technology("silo-tech", ["starter"], ["rocket-part", "rocket-silo", "space-platform-starter-pack"],
                                ingredients=[material("science")], unit_count=10),
    }
    return data


class ProductionGraphTests(unittest.TestCase):
    def test_graph_includes_natural_lab_bootstrap_without_forced_research(self):
        graph = ProductionGraph(WorldCatalog.from_dict(production_fixture()))
        plan = graph.for_first_rocket()
        self.assertEqual(plan["bom"]["recipe_batches"]["lab"], 1)
        self.assertEqual(plan["gaps"], [])
        trigger = graph.next_research({"technologies": {}})
        self.assertEqual(trigger["kind"], "trigger")
        self.assertEqual(trigger["trigger"]["item"], {"name": "lab"})
        self.assertIsNone(trigger["action"])

    def test_research_waits_for_trigger_and_respects_current_research(self):
        graph = ProductionGraph(WorldCatalog.from_dict(production_fixture()))
        result = graph.next_research({"technologies": {"starter": True}})
        self.assertEqual(result["action"], {"type": "research", "technology": "silo-tech"})
        result = graph.next_research({"technologies": {"starter": True}, "research": "silo-tech"})
        self.assertEqual(result["reason"], "research_running")
        self.assertIsNone(graph.next_research({"technologies": {"starter": True, "silo-tech": True}}))

    def test_live_recipe_availability_overrides_snapshot(self):
        graph = ProductionGraph(WorldCatalog.from_dict(production_fixture()))
        observation = {"enabled_recipes": {"science": True, "assembler": True}}
        demand = graph.recipe_demand(observation)
        self.assertEqual(demand["science"]["status"], "ready")
        self.assertEqual(demand["rocket-part"]["status"], "locked")
        self.assertEqual(demand["science"]["cycles_per_minute"], 15)
        self.assertEqual(demand["science"]["machines_for_rate"], 5)
        self.assertEqual(demand["science"]["ingredient_rates_per_minute"], [material("gear", 15)])
        self.assertEqual(demand["science"]["reserved_power_watts"], 375000)

    def test_machine_requires_category_and_placement_unlock(self):
        graph = ProductionGraph(WorldCatalog.from_dict(production_fixture()))
        self.assertEqual(graph.machines_for_recipe("rocket-part"), [])
        self.assertEqual([row["name"] for row in graph.machines_for_recipe("rocket-part", unlocked_only=False)], ["rocket-silo"])
        self.assertEqual([row["name"] for row in graph.machines_for_recipe("rocket-part", {"enabled_recipes": {"rocket-silo": True}})], ["rocket-silo"])

    def test_fluid_demand_retains_material_type_and_missing_machine_is_explicit(self):
        data = production_fixture()
        data["raw_sources"] = [material("water", kind="fluid")]
        data["recipes"]["science"]["ingredients"].append(material("water", 5, "fluid"))
        data["recipes"]["science"]["categories"] = ["fluid-only"]
        graph = ProductionGraph(WorldCatalog.from_dict(data))
        plan = graph.for_first_rocket()
        self.assertIn({"kind": "machine_category", "recipe": "science", "categories": ["fluid-only"]}, plan["gaps"])
        self.assertIn(material("water", 75, "fluid"), plan["raw_rates_per_minute"])

    def test_plan_results_are_isolated_and_invalid_reserves_rejected(self):
        catalog = WorldCatalog.from_dict(production_fixture())
        graph = ProductionGraph(catalog)
        plan = graph.for_first_rocket()
        original = deepcopy(plan)
        plan["nodes"].clear()
        self.assertEqual(graph.for_first_rocket(), original)
        with self.assertRaises(ValueError):
            ProductionGraph(catalog, power_reserve_fraction=1)
        with self.assertRaises(ValueError):
            ProductionGraph(catalog, science_rate_per_minute=0)


if __name__ == "__main__":
    unittest.main()
