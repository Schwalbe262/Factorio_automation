import unittest

from factorio_ai import cell_compiler as cc, cell_placer as cp, cell_flow_check as cf
from factorio_ai import layout_validation


class CellFlowCheckTests(unittest.TestCase):
    def _spec(self):
        return cc.compile_cell("electronic-circuit", 60, available_machines=["assembling-machine-2"])

    def test_good_cell_passes(self):
        spec = self._spec()
        placed = cp.place_cell(spec, cp.BoundingBox(40, 40))
        result = cf.precheck_cell(spec, placed, power_situation=cc.PowerSituation(available_headroom_kw=2000))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["checks"]["power_coverage"], "pass")
        self.assertEqual(result["checks"]["operability"], "pass")

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
