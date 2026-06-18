import unittest

from factorio_ai import blueprints, cell_apply


class CellApplyTests(unittest.TestCase):
    def _design(self):
        entities = [
            {"name": "assembling-machine-1", "position": {"x": 0, "y": 0}, "recipe": "iron-gear-wheel"},
            {"name": "transport-belt", "position": {"x": 1, "y": -3}, "direction": 4},
            {"name": "inserter", "position": {"x": 0, "y": -2}, "direction": 0},
            {"name": "stone-furnace", "position": {"x": 5, "y": 0}, "recipe": "iron-plate"},
        ]
        return {"blueprint": {"exchange_string": blueprints.encode_blueprint_entities("t", entities)}}

    def test_required_items_count_every_entity(self):
        plan = cell_apply.design_build_plan(self._design(), 0, 0)
        self.assertEqual(
            plan["required_items"],
            {"assembling-machine-1": 1, "inserter": 1, "stone-furnace": 1, "transport-belt": 1},
        )
        self.assertEqual(plan["entity_count"], 4)

    def test_build_actions_offset_to_anchor_and_preserve_direction(self):
        plan = cell_apply.design_build_plan(self._design(), 10, 20)
        builds = {a["name"]: a for a in plan["actions"] if a["type"] == "build"}
        self.assertEqual(builds["assembling-machine-1"]["position"], {"x": 10.0, "y": 20.0})
        self.assertEqual(builds["transport-belt"]["position"], {"x": 11.0, "y": 17.0})
        self.assertEqual(builds["transport-belt"]["direction"], 4)

    def test_set_recipe_only_for_non_furnace_machines(self):
        plan = cell_apply.design_build_plan(self._design(), 0, 0)
        recipes = [a for a in plan["actions"] if a["type"] == "set_recipe"]
        self.assertEqual(len(recipes), 1)  # the assembler, NOT the auto-smelting stone-furnace
        self.assertEqual(recipes[0]["recipe"], "iron-gear-wheel")
        self.assertEqual(recipes[0]["name"], "assembling-machine-1")

    def test_dry_run_returns_plan_without_executing(self):
        # apply_design with execute=False must not touch the controller (pass None to prove it).
        import tempfile, json, os
        from pathlib import Path
        from factorio_ai import cell_library, cell_compiler, cell_placer
        with tempfile.TemporaryDirectory() as d:
            spec = cell_compiler.compile_cell("iron-gear-wheel", 60, available_machines=["assembling-machine-1"])
            placed = cell_placer.place_cell(spec, cell_placer.BoundingBox(80, 80))
            bp = blueprints.encode_blueprint_entities("g", placed.entities)
            rec = cell_library.save_design(Path(d), spec, blueprint_string=bp, sandbox_status="t", placed=placed)
            out = cell_apply.apply_design(None, Path(d), rec["key"], 0, 0, execute=False)
            self.assertTrue(out["ok"])
            self.assertFalse(out["executed"])
            self.assertGreater(out["build_count"], 0)

    def test_unknown_key_returns_not_ok(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            out = cell_apply.apply_design(None, Path(d), "nope", 0, 0, execute=False)
            self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
