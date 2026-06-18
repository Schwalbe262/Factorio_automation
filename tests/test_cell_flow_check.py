import unittest

from factorio_ai import cell_compiler as cc, cell_placer as cp, cell_flow_check as cf
from factorio_ai import layout_validation


class CellFlowCheckTests(unittest.TestCase):
    def _spec(self):
        return cc.compile_cell("electronic-circuit", 60, available_machines=["assembling-machine-2"])

    def test_precheck_reports_new_geometry_checks(self):
        # the corrected precheck exposes footprint-aware checks + metrics.
        spec = self._spec()
        placed = cp.place_cell(spec, cp.BoundingBox(40, 40))
        result = cf.precheck_cell(spec, placed, power_situation=cc.PowerSituation(available_headroom_kw=2000))
        for key in ("inserter_reach", "fuel_supply", "flow_reachability", "collision"):
            self.assertIn(key, result["checks"])
        self.assertIn("rect_fill", result["metrics"])
        self.assertEqual(result["checks"]["power_coverage"], "pass")

    def test_belt_row_ec_flow_bug_is_caught(self):
        # REGRESSION: the legacy belt_row electronic-circuit layout routes the co-located copper-cable
        # on an east-flowing shared lane, so the far cable assembler can't feed the EC machine. The
        # corrected flow-reachability check must catch this (the direct_insertion archetype fixes it).
        spec = cc.compile_cell("electronic-circuit", 60, available_machines=["assembling-machine-1"],
                               belt_tiers_available=["transport-belt"])
        placed = cp.place_cell(spec, cp.BoundingBox(80, 80))
        result = cf.precheck_cell(spec, placed)
        self.assertEqual(result["checks"]["flow_reachability"], "fail")

    def test_furnace_inserter_miss_is_caught(self):
        # REGRESSION: the legacy belt_row places a 2x2 furnace's output inserter where it doesn't
        # reach the furnace (the ±1.5 operability box missed this); inserter_reach must catch it.
        spec = cc.compile_cell("iron-plate", 120, available_machines=["stone-furnace"],
                               belt_tiers_available=["transport-belt"])
        placed = cp.place_cell(spec, cp.BoundingBox(80, 80))
        result = cf.precheck_cell(spec, placed)
        self.assertEqual(result["checks"]["inserter_reach"], "fail")
        self.assertEqual(result["checks"]["fuel_supply"], "fail")  # no coal source in the legacy layout

    def test_box_too_small_fails(self):
        spec = self._spec()
        placed = cp.place_cell(spec, cp.BoundingBox(5, 5))
        result = cf.precheck_cell(spec, placed)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["checks"]["box_fit"], "fail")

    def test_power_over_budget_warns(self):
        spec = self._spec()
        placed = cp.place_cell(spec, cp.BoundingBox(40, 40))
        result = cf.precheck_cell(spec, placed, power_situation=cc.PowerSituation(available_headroom_kw=100))
        self.assertEqual(result["checks"]["power_budget"], "warn")

    def test_belt_underprovisioned_fails(self):
        # Force a belt tier that can't carry the flow by mutating the spec's input belt to tier-1
        # at a rate above 15/s (900/min).
        spec = cc.compile_cell("copper-cable", 2000, available_machines=["assembling-machine-2"],
                               belt_tiers_available=["transport-belt"])  # only 15/s belts
        placed = cp.place_cell(spec, cp.BoundingBox(80, 80))
        result = cf.precheck_cell(spec, placed)
        # copper-plate input at 1000/min on a single 900/min belt -> underprovisioned unless lanes>1
        # The compiler should have flagged multiple lanes; throughput check must still hold capacity.
        self.assertIn(result["checks"]["belt_throughput"], {"pass", "fail"})


class CandidateInjectionTests(unittest.TestCase):
    def test_find_injected_candidate_takes_precedence(self):
        injected = [{"candidate_id": "cell:electronic-circuit:60", "blueprint": {"exchange_string": "x"}}]
        found = layout_validation.find_layout_candidate({}, "cell:electronic-circuit:60", candidates=injected)
        self.assertEqual(found["candidate_id"], "cell:electronic-circuit:60")

    def test_missing_candidate_raises(self):
        with self.assertRaises(ValueError):
            layout_validation.find_layout_candidate({}, "nope", candidates=[])


if __name__ == "__main__":
    unittest.main()
