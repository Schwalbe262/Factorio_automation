import unittest
from unittest.mock import Mock, patch

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

    def test_raw_template_entities_can_be_planned_without_blueprint_encoding(self):
        plan = cell_apply.design_build_plan({"entities": [{"name": "lab", "position": {"x": .5, "y": .5}}]})
        self.assertEqual(plan["required_items"], {"lab": 1})

    def test_failed_build_stops_before_recipe_and_preserves_lua_reason(self):
        plan = cell_apply.design_build_plan(self._design())
        plan["ok"] = True
        controller = Mock()
        controller.act.return_value = {"ok": False, "reason": "missing item: assembling-machine-1"}
        with patch.object(cell_apply, "load_design_plan", return_value=plan):
            result = cell_apply.apply_design(controller, None, "key", 0, 0, execute=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_action_index"], 0)
        self.assertEqual(result["reason"], "missing item: assembling-machine-1")
        self.assertEqual(result["results"][0]["detail"], result["reason"])
        self.assertEqual(controller.act.call_count, 1)
        self.assertEqual(result["placed"], 0)

    def test_success_counts_builds_separately_from_recipe_actions(self):
        plan = cell_apply.design_build_plan(self._design())
        plan["ok"] = True
        controller = Mock()
        controller.act.return_value = {"ok": True}
        with patch.object(cell_apply, "load_design_plan", return_value=plan):
            result = cell_apply.apply_design(controller, None, "key", 0, 0, execute=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["placed"], 4)
        self.assertEqual(result["applied"], 5)
        self.assertEqual(result["remaining"], 0)

    def test_transport_error_reports_failed_action_after_partial_construction(self):
        plan = cell_apply.design_build_plan(self._design())
        plan["ok"] = True
        controller = Mock()
        controller.act.side_effect = [{"ok": True}, TimeoutError("RCON unavailable")]
        with patch.object(cell_apply, "load_design_plan", return_value=plan):
            result = cell_apply.apply_design(controller, None, "key", 0, 0, execute=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["placed"], 1)
        self.assertEqual(result["failed_action_index"], 1)
        self.assertIn("RCON unavailable", result["reason"])

    def test_empty_design_is_not_a_successful_build_plan(self):
        with patch.object(cell_apply.cell_library, "get_design", return_value={"entities": []}):
            result = cell_apply.load_design_plan(".", "empty")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
