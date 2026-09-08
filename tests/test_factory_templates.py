import unittest
from collections import Counter

from factorio_ai.factory_templates import build_template, route_orthogonal


class FactoryTemplateTests(unittest.TestCase):
    def test_steam_bank_connects_real_boiler_engine_geometry_and_exposes_supplies(self):
        result = build_template("steam_bank", anchor={"x": .5, "y": .5})
        self.assertTrue(result["ok"], result["reason"])
        self.assertEqual(result["required_items"]["boiler"], 1)
        self.assertEqual(result["required_items"]["steam-engine"], 2)
        boiler = next(e for e in result["entities"] if e["name"] == "boiler")
        engines = [e for e in result["entities"] if e["name"] == "steam-engine"]
        # North boiler steam connection y=-.5 and engine south connection y=+2:
        # the two neighbouring internal connection tiles must be one tile apart.
        self.assertEqual(boiler["position"]["y"] - .5 - (engines[0]["position"]["y"] + 2), 1)
        self.assertEqual(engines[0]["position"]["y"] - engines[1]["position"]["y"], 5)
        self.assertEqual({(p["kind"], p["item"], p["direction"]) for p in result["ports"]},
                         {("fluid", "water", "input"), ("item", "coal", "input"),
                          ("power", "electricity", "output")})
        self.assertIn("burner-inserter", result["required_items"])
        self.assertIn("sustained_flow", result["validation_required"])

    def test_furnace_has_separate_ore_coal_inputs_and_aligned_even_center(self):
        result = build_template("furnace_row", count=3, inputs=["iron-ore", "coal"], output="iron-plate")
        self.assertTrue(result["ok"], result["reason"])
        furnaces = [e for e in result["entities"] if e["name"] == "stone-furnace"]
        self.assertEqual(len(furnaces), 3)
        self.assertTrue(all(e["position"]["x"].is_integer() and e["position"]["y"].is_integer() for e in furnaces))
        item_ports = [p for p in result["ports"] if p["kind"] == "item"]
        self.assertEqual(Counter(p["item"] for p in item_ports), {"iron-ore": 3, "coal": 3, "iron-plate": 3})
        self.assertEqual(len({tuple(p["position"].values()) for p in item_ports}), 9)

    def test_assembler_ports_have_distinct_belt_paths_and_correct_inserter_sides(self):
        result = build_template("assembler_row", recipe="advanced-circuit", count=2,
                                inputs=["copper-cable", "electronic-circuit", "plastic-bar"], output="advanced-circuit")
        self.assertTrue(result["ok"], result["reason"])
        first = [e for e in result["entities"] if e["name"] == "inserter"][:4]
        self.assertEqual([e["direction"] for e in first], [0, 12, 8, 12])
        self.assertEqual(result["required_items"]["assembling-machine-1"], 2)
        self.assertEqual(Counter(e["name"] for e in result["entities"]), result["required_items"])

    def test_lab_row_exposes_six_science_inputs_without_recipe_or_output(self):
        packs = [f"pack-{i}" for i in range(6)]
        result = build_template("labs_row", count=2, inputs=packs)
        self.assertTrue(result["ok"], result["reason"])
        self.assertEqual(result["required_items"]["lab"], 2)
        self.assertEqual(len([p for p in result["ports"] if p["kind"] == "item"]), 12)
        self.assertFalse(any("recipe" in e for e in result["entities"]))

    def test_rotation_transforms_ports_entities_directions_and_bounds(self):
        options = dict(recipe="iron-gear-wheel", inputs=["iron-plate"], output="iron-gear-wheel",
                       anchor={"x": 10.5, "y": -4.5})
        original = build_template("assembler_row", **options)
        rotated = build_template("assembler_row", rotation=4, **options)
        self.assertTrue(rotated["ok"], rotated["reason"])
        for a, b in zip(original["entities"], rotated["entities"]):
            self.assertEqual(b["position"]["x"], 10.5 - (a["position"]["y"] + 4.5))
            self.assertEqual(b["position"]["y"], -4.5 + (a["position"]["x"] - 10.5))
            self.assertEqual(b["direction"], (a["direction"] + 4) % 16)
        for a, b in zip(original["ports"], rotated["ports"]):
            if "facing" in a:
                self.assertEqual(b["facing"], (a["facing"] + 4) % 16)
        self.assertEqual(original["bounds"]["width"], rotated["bounds"]["height"])

    def test_chemical_inputs_require_real_geometry_and_have_exposed_pipes(self):
        options = dict(recipe="plastic-bar", inputs=["coal", "petroleum-gas"], output="plastic-bar")
        self.assertFalse(build_template("chemical_row", **options)["ok"])
        geometry = {"width": 3, "height": 3, "fluid_ports": [
            {"item": "petroleum-gas", "direction": "input", "position": {"x": 0, "y": -2}}]}
        result = build_template("chemical_row", prototype_geometry=geometry, **options)
        self.assertTrue(result["ok"], result["reason"])
        self.assertEqual(result["required_items"]["pipe"], 3)
        self.assertIn(("fluid", "petroleum-gas"), {(p["kind"], p["item"]) for p in result["ports"]})
        self.assertEqual(len(geometry["fluid_ports"]), 1)

    def test_refinery_routes_multiple_fluids_without_solid_output_inserter(self):
        geometry = {"width": 5, "height": 5, "fluid_ports": [
            {"item": "crude-oil", "direction": "input", "position": {"x": 1, "y": 3}},
            {"item": "petroleum-gas", "direction": "output", "position": {"x": 2, "y": -3}}]}
        result = build_template("refinery_row", recipe="basic-oil-processing", inputs=["crude-oil"],
                                output="petroleum-gas", prototype_geometry=geometry)
        self.assertTrue(result["ok"], result["reason"])
        self.assertEqual(result["required_items"]["oil-refinery"], 1)
        self.assertEqual(result["required_items"]["pipe"], 6)
        self.assertNotIn("inserter", result["required_items"])

    def test_fluid_template_rejects_missing_ports_wrong_coordinates_and_fluid_mixing(self):
        options = dict(recipe="sulfuric-acid", inputs=["water", "sulfur", "iron-plate"], output="sulfuric-acid")
        invalid_geometries = [
            {"width": 3, "height": 3, "fluid_ports": []},
            {"width": 3, "height": 3, "fluid_ports": [
                {"item": "water", "direction": "input", "position": {"x": 0, "y": 0}}]},
            {"width": 3, "height": 3, "fluid_ports": [
                {"item": "water", "direction": "input", "position": {"x": 0, "y": -2}},
                {"item": "sulfuric-acid", "direction": "output", "position": {"x": 1, "y": -2}}]},
        ]
        for geometry in invalid_geometries:
            with self.subTest(geometry=geometry):
                result = build_template("chemical_row", prototype_geometry=geometry, **options)
                self.assertFalse(result["ok"])
                self.assertEqual(result["entities"], [])
        self.assertIn("different fluid", build_template("chemical_row", prototype_geometry=invalid_geometries[-1], **options)["reason"])

    def test_unsupported_machine_fluid_in_solid_template_and_invalid_limits_fail_closed(self):
        for options in ({"count": 0}, {"rotation": 1}, {"count": 100000}, {"anchor": {"x": float("nan"), "y": 0}}):
            self.assertFalse(build_template("steam_bank", **options)["ok"])
        self.assertFalse(build_template("assembler_row", recipe="processing-unit", inputs=["sulfuric-acid"], output="processing-unit")["ok"])
        self.assertFalse(build_template("unknown")["ok"])


class OrthogonalRouterTests(unittest.TestCase):
    def test_route_avoids_wall_and_uses_adjacent_steps_on_half_tile_grid(self):
        blocked = {(2.5, y + .5) for y in (-1, 0, 1)}
        result = route_orthogonal((.5, .5), (4.5, .5), occupied=blocked)
        self.assertTrue(result["ok"], result["reason"])
        path = [tuple(p.values()) for p in result["path"]]
        self.assertEqual(len(path), 9)
        self.assertFalse(set(path) & blocked)
        self.assertTrue(all(abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1 for a, b in zip(path, path[1:])))

    def test_start_and_end_directions_preserve_belt_handoffs(self):
        result = route_orthogonal((0, 0), (4, 0), start_direction=0, end_direction=8)
        self.assertTrue(result["ok"], result["reason"])
        self.assertEqual(result["segments"][0]["direction"], 0)
        self.assertEqual(result["path"][-2], {"x": 4.0, "y": -1.0})
        self.assertEqual(result["segments"][-1]["direction"], 8)

    def test_unreachable_occupied_and_budget_limited_paths_report_failure(self):
        self.assertFalse(route_orthogonal((0, 0), (2, 0), occupied={(0, 0)})["ok"])
        self.assertFalse(route_orthogonal((0, 0), (.5, 0))["ok"])
        result = route_orthogonal((0, 0), (2, 0), occupied={(1, 0)},
                                  bounds={"min_x": 0, "max_x": 2, "min_y": 0, "max_y": 0})
        self.assertFalse(result["ok"])
        self.assertIn("no route", result["reason"])
        result = route_orthogonal((0, 0), (20, 20), max_nodes=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["visited"], 1)
        self.assertIn("budget", result["reason"])


if __name__ == "__main__":
    unittest.main()
