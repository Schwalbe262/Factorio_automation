from copy import deepcopy
import unittest

from factorio_ai.deterministic_arrays import production_demand, compile_array, optimize_array, validate_array, translate_array
from factorio_ai.world_catalog import WorldCatalog


GEOMETRY = {"belt_speed": .03125, "underground_distance": 5, "pole_wire": 7.5, "pole_supply": 2.5}


def array_catalog():
    def recipe(inputs, amount=1, energy=1):
        return {"enabled": True, "categories": ["crafting"], "energy": energy,
                "ingredients": [{"type": "item", "name": k, "amount": n} for k, n in inputs.items()],
                "products": [], "_amount": amount}
    recipes = {"gear": recipe({"iron-plate": 2}, energy=.5),
               "cable": recipe({"copper-plate": 1}, amount=2, energy=.5),
               "circuit": recipe({"iron-plate": 1, "cable": 3}, energy=.5),
               "widget": recipe({"gear": 1, "circuit": 1}, energy=5),
               "transport-belt": recipe({"iron-plate": 1, "gear": 1}, amount=2),
               "underground-belt": recipe({"iron-plate": 10, "transport-belt": 5}, amount=2),
               "inserter": recipe({"iron-plate": 1, "gear": 1, "circuit": 1}),
               "small-electric-pole": recipe({"wood": 1, "copper-plate": 2}, amount=2),
               "assembling-machine-1": recipe({"iron-plate": 9, "gear": 5, "circuit": 3}),
               "lab": recipe({"iron-plate": 10, "gear": 10, "circuit": 10})}
    for name, row in recipes.items():
        row["products"] = [{"type": "item", "name": name, "amount": row.pop("_amount")}]
    return WorldCatalog.from_dict({"recipes": recipes,
        "raw_sources": [{"type": "item", "name": name} for name in ["iron-plate", "copper-plate", "wood"]],
        "entities": {"assembling-machine-1": {"type": "assembling-machine", "crafting_speed": .5,
            "crafting_categories": ["crafting"], "items_to_place_this": [{"name": "assembling-machine-1"}]}}})


class ArrayTests(unittest.TestCase):
    def setUp(self):
        self.catalog = array_catalog()
        self.obs = {"enabled_recipes": dict.fromkeys(self.catalog.recipes, True), "entities": []}
        self.demand = production_demand(self.catalog, self.obs, {"widget": 30})
        self.plan = compile_array(self.catalog, self.demand, GEOMETRY)
        self.assertTrue(self.plan["ok"], self.plan["validation"])

    def test_shared_dependency_rates_and_slow_science_machine_counts(self):
        d = production_demand(self.catalog, self.obs, {"widget": 30, "circuit": 10})
        self.assertEqual(d["rates"]["circuit"], 40)
        self.assertEqual(d["rates"]["cable"], 120)
        widget = next(n for n in d["nodes"] if n["item"] == "widget")
        self.assertEqual(widget["count"], 5)
        self.assertGreaterEqual(widget["per_machine_capacity"] * widget["count"], 30)

    def test_construction_array_needs_no_labs(self):
        plan = optimize_array(self.catalog, self.obs, {"widget": 30}, GEOMETRY,
                              labs=[], lab_count=0)
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["lab_count"], 0)
        self.assertFalse(any(e["name"] == "lab" for e in plan["entities"]))
        with self.assertRaises(ValueError):
            compile_array(self.catalog, self.demand, GEOMETRY, labs=["widget"], lab_count=0)

    def test_locked_and_unsupported_input_counts_fail_before_construction(self):
        self.obs["enabled_recipes"]["widget"] = False
        with self.assertRaisesRegex(ValueError, "locked"):
            production_demand(self.catalog, self.obs, {"widget": 30})

    def test_export_port_deducts_internal_demand(self):
        demand = production_demand(self.catalog, self.obs, {"widget": 30, "circuit": 10})
        plan = compile_array(self.catalog, demand, GEOMETRY)
        port = next(p for p in plan["ports"] if p["item"] == "circuit")
        self.assertEqual(port["gross_rate_per_minute"], 40)
        self.assertEqual(port["rate_per_minute"], 10)
        self.assertEqual(next(p for p in plan["ports"] if p["item"] == "gear")["rate_per_minute"], 0)

    def test_single_item_spine_capacity_is_a_gate(self):
        geometry = {**GEOMETRY, "belt_speed": .0001}
        with self.assertRaisesRegex(ValueError, "faster belt"):
            compile_array(self.catalog, self.demand, geometry)

    def test_broken_underground_pair_is_not_validated(self):
        p = deepcopy(self.plan)
        endpoint = next(e for e in p["entities"] if e.get("belt_to_ground_type") == "output")
        p["entities"].remove(endpoint)
        self.assertFalse(validate_array(p, self.catalog, GEOMETRY)["ok"])

    def test_wrong_belt_item_and_missing_recipe_arm_are_detected(self):
        p = deepcopy(self.plan)
        arm = next(e for e in p["entities"] if e.get("_role") == "input")
        p["entities"].remove(arm)
        result = validate_array(p, self.catalog, GEOMETRY)
        self.assertIn("recipe_input_missing", {e["rule"] for e in result["errors"]})
        p = deepcopy(self.plan)
        next(e for e in p["entities"] if e["name"] == "transport-belt")["_item"] = "foreign-item"
        self.assertFalse(validate_array(p, self.catalog, GEOMETRY)["ok"])

    def test_missing_machine_cannot_reuse_declared_capacity(self):
        p = deepcopy(self.plan)
        p["entities"].remove(next(e for e in p["entities"] if e.get("recipe") == "widget"))
        result = validate_array(p, self.catalog, GEOMETRY)
        self.assertIn("machine_count_mismatch", {e["rule"] for e in result["errors"]})

    def test_power_and_exact_observed_item_paths_remain_required(self):
        p = deepcopy(self.plan)
        p["entities"] = [e for e in p["entities"] if e["name"] != "small-electric-pole"]
        self.assertFalse(validate_array(p, self.catalog, GEOMETRY)["ok"])

    def test_optimization_is_deterministic_bounded_and_does_not_mutate_catalog(self):
        before = self.catalog.fingerprint
        a = optimize_array(self.catalog, self.obs, {"widget": 30}, GEOMETRY)
        b = optimize_array(self.catalog, self.obs, {"widget": 30}, GEOMETRY)
        self.assertEqual(a["digest"], b["digest"])
        self.assertLessEqual(a["optimization"]["candidates"], 1 + 6 * 32)
        self.assertEqual(self.catalog.fingerprint, before)
        self.assertFalse(a["validation"]["flow_verified"])

    def test_process_reordering_rejects_consumers_before_suppliers(self):
        order = [node["item"] for node in self.demand["nodes"]]
        with self.assertRaisesRegex(ValueError, "suppliers before consumers"):
            compile_array(self.catalog, self.demand, GEOMETRY, node_order=list(reversed(order)))
        with self.assertRaisesRegex(ValueError, "each production node once"):
            compile_array(self.catalog, self.demand, GEOMETRY, node_order=order[:-1])
        plan = compile_array(self.catalog, self.demand, GEOMETRY, node_order=order)
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["geometry"]["node_order"], order)

    def test_rotation_preserves_static_connections_and_power(self):
        for direction in (0, 4, 8, 12):
            translated = translate_array(self.plan, {"x": -20.5, "y": 40.5}, direction)
            self.assertTrue(validate_array(translated, self.catalog, GEOMETRY)["ok"])

    def test_array_labs_are_fed_by_declared_science_spines(self):
        p = compile_array(self.catalog, self.demand, GEOMETRY, labs=["widget"], lab_count=3)
        self.assertTrue(p["validation"]["ok"])
        self.assertEqual(sum(e["name"] == "lab" for e in p["entities"]), 3)

    def test_horizontal_lab_rack_has_separate_powered_inputs_in_all_rotations(self):
        demand = production_demand(self.catalog, self.obs, {"widget": 30, "circuit": 30, "gear": 30})
        for packs in (["widget"], ["widget", "circuit"], ["widget", "circuit", "gear"]):
            plan = compile_array(self.catalog, demand, GEOMETRY, labs=packs, lab_count=3, lab_layout="rack")
            self.assertTrue(plan["ok"], plan["validation"])
            labs = [e for e in plan["entities"] if e["name"] == "lab"]
            self.assertEqual(len(labs), 3)
            self.assertEqual(len({e["position"]["y"] for e in labs}), 1)
            for lab in labs:
                x, y = lab["position"]["x"], lab["position"]["y"]
                arm_positions = {(x, y - 2), (x, y + 2), (x - 2, y)}
                supplied = {e.get("_item") for e in plan["entities"] if e.get("_role") == "input"
                            and (e["position"]["x"], e["position"]["y"]) in arm_positions}
                self.assertEqual(supplied, set(packs))
            for direction in (0, 4, 8, 12):
                moved = translate_array(plan, {"x": 10.5, "y": -10.5}, direction)
                self.assertTrue(validate_array(moved, self.catalog, GEOMETRY)["ok"])

    def test_lab_rack_reduces_transport_with_identical_production_and_lab_capacity(self):
        from factorio_ai.deterministic_layout_metrics import layout_metrics
        column = compile_array(self.catalog, self.demand, GEOMETRY, labs=["widget"], lab_count=15)
        rack = compile_array(self.catalog, self.demand, GEOMETRY, labs=["widget"], lab_count=15, lab_layout="rack")
        self.assertTrue(rack["ok"])
        self.assertEqual(column["demand"], rack["demand"])
        self.assertEqual(column["lab_count"], rack["lab_count"])
        cost = lambda p: layout_metrics(p["entities"], self.catalog)["transport"]["raw_item_units"]
        self.assertLess(cost(rack), cost(column))
        too_fast = production_demand(self.catalog, self.obs, {"widget": 45})
        with self.assertRaisesRegex(ValueError, "tap capacity"):
            compile_array(self.catalog, too_fast, GEOMETRY, labs=["widget"], lab_count=15, lab_layout="rack")

    def test_two_tile_furnace_inputs_do_not_overlap_machine(self):
        data = self.catalog.to_dict()
        data["recipes"]["iron-plate"] = {"enabled": True, "categories": ["smelting"], "energy": 3.2,
            "ingredients": [{"name": "iron-ore", "type": "item", "amount": 1}],
            "products": [{"name": "iron-plate", "type": "item", "amount": 1}]}
        data["recipes"]["stone-furnace"] = {"enabled": True, "categories": ["crafting"], "energy": .5,
            "ingredients": [{"name": "stone", "type": "item", "amount": 5}],
            "products": [{"name": "stone-furnace", "type": "item", "amount": 1}]}
        data["entities"]["stone-furnace"] = {"type": "furnace", "crafting_speed": 1,
            "crafting_categories": ["smelting"], "burner": True, "energy_usage": 1500,
            "items_to_place_this": [{"name": "stone-furnace"}]}
        data["items"]["coal"] = {"fuel_value": 4000000}
        data["raw_sources"] = [{"type": "item", "name": name}
                               for name in ["iron-ore", "copper-plate", "wood", "coal", "stone"]]
        catalog = WorldCatalog.from_dict(data)
        demand = production_demand(catalog, {"enabled_recipes": dict.fromkeys(catalog.recipes, True)},
                                   {"widget": 30}, boundary={"iron-ore", "copper-plate", "wood", "coal", "stone"})
        plan = compile_array(catalog, demand, GEOMETRY)
        self.assertTrue(plan["ok"], plan["validation"])
        self.assertGreater(demand["external_rates"]["coal"], 0)
        iron = next(n for n in demand["nodes"] if n["item"] == "iron-plate")
        self.assertEqual(iron["rate"], 90)
        self.assertEqual(iron["count"], 5)
        self.assertGreaterEqual(iron["count"] * iron["per_machine_capacity"], iron["rate"])
        self.assertTrue(all("recipe" not in e for e in plan["entities"] if e["name"] == "stone-furnace"))
        for direction in (0, 4, 8, 12):
            self.assertTrue(validate_array(translate_array(plan, {"x": .5, "y": .5}, direction),
                                           catalog, GEOMETRY)["ok"])
