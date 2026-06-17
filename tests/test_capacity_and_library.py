import json
import tempfile
import unittest
from pathlib import Path

from factorio_ai import capacity_planner, cell_compiler as cc, cell_library


class CapacityPlannerTests(unittest.TestCase):
    def test_new_when_no_existing_site(self):
        plan = capacity_planner.plan_capacity("electronic-circuit", 60, sites=[],
                                              available_machines=["assembling-machine-2"])
        self.assertEqual(plan["mode"], "new")
        self.assertGreater(plan["needed_area"], 0)
        self.assertTrue(plan["spec"]["ok"])

    def test_expand_when_headroom_fits(self):
        # a same-item site with a big reserved box and a small current footprint => headroom fits
        sites = [{
            "site_id": "ec-1",
            "target_item": "electronic-circuit",
            "status": "running",
            "bounds": {"min_x": 0, "min_y": 0, "max_x": 12, "max_y": 13},
            "reserved_box": {"min_x": 0, "min_y": 0, "max_x": 200, "max_y": 200},
        }]
        plan = capacity_planner.plan_capacity("electronic-circuit", 60, sites=sites,
                                              available_machines=["assembling-machine-2"])
        self.assertEqual(plan["mode"], "expand")
        self.assertEqual(plan["site_id"], "ec-1")

    def test_new_when_existing_site_full(self):
        sites = [{
            "site_id": "ec-1",
            "target_item": "electronic-circuit",
            "status": "running",
            "bounds": {"min_x": 0, "min_y": 0, "max_x": 100, "max_y": 100},
            "reserved_box": {"min_x": 0, "min_y": 0, "max_x": 101, "max_y": 101},  # ~no headroom
        }]
        plan = capacity_planner.plan_capacity("electronic-circuit", 600, sites=sites,
                                              available_machines=["assembling-machine-2"])
        self.assertEqual(plan["mode"], "new")


class CellLibraryTests(unittest.TestCase):
    def _spec(self):
        return cc.compile_cell("electronic-circuit", 60, available_machines=["assembling-machine-2"])

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            spec = self._spec()
            rec = cell_library.save_design(runtime, spec, blueprint_string="0eABC", sandbox_status="pass")
            self.assertTrue(rec["key"])
            self.assertIn("electronic-circuit", rec["description"])
            self.assertEqual(rec["blueprint"]["exchange_string"], "0eABC")

            designs = cell_library.load_designs(runtime)
            self.assertEqual(len(designs), 1)
            self.assertEqual(designs[0]["item"], "electronic-circuit")
            # index written
            self.assertTrue((runtime / cell_library.LIBRARY_DIRNAME / cell_library.INDEX_NAME).exists())

    def test_key_is_stable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            spec = self._spec()
            k1 = cell_library.design_key(spec)
            cell_library.save_design(runtime, spec, blueprint_string="a")
            cell_library.save_design(runtime, spec, blueprint_string="b")  # same key -> overwrite
            designs = cell_library.load_designs(runtime)
            self.assertEqual(len(designs), 1)
            self.assertEqual(designs[0]["key"], k1)
            self.assertEqual(designs[0]["blueprint"]["exchange_string"], "b")

    def test_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            cell_library.save_design(runtime, self._spec(), blueprint_string="x")
            summary = cell_library.library_summary(runtime)
            self.assertEqual(summary["count"], 1)


if __name__ == "__main__":
    unittest.main()
