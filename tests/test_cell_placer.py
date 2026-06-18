import unittest

from factorio_ai import blueprints, cell_placer as cp, planner
from factorio_ai import knowledge
from factorio_ai.cell_compiler import compile_cell


class CellPlacerTests(unittest.TestCase):
    def _ec_spec(self):
        return compile_cell("electronic-circuit", 60, available_machines=["assembling-machine-2"])

    def test_ec_cell_passes_static_operability(self):
        placed = cp.place_cell(self._ec_spec(), cp.BoundingBox(40, 40))
        self.assertTrue(placed.fits)
        report = planner._blueprint_operability_report(placed.entities)
        self.assertEqual(report["status"], "pass")  # every recipe assembler has its inserters
        self.assertEqual(report["errors"], [])

    def test_ec_cell_round_trips_through_blueprint(self):
        placed = cp.place_cell(self._ec_spec(), cp.BoundingBox(40, 40))
        encoded = blueprints.encode_blueprint_entities("ec", placed.entities)
        self.assertTrue(encoded)
        self.assertTrue(blueprints.decode_blueprint_string(encoded))

    def test_power_coverage_and_connectivity(self):
        placed = cp.place_cell(self._ec_spec(), cp.BoundingBox(40, 40))
        self.assertTrue(placed.power_coverage_ok)
        self.assertTrue(any("pole" in e["name"] for e in placed.entities))

    def test_box_too_small_reports_required(self):
        placed = cp.place_cell(self._ec_spec(), cp.BoundingBox(5, 5))
        self.assertFalse(placed.fits)
        self.assertGreater(placed.required_box["width"], 5)

    def test_furnace_has_no_recipe_field(self):
        spec = compile_cell("iron-plate", 120, available_machines=["stone-furnace", "steel-furnace"])
        placed = cp.place_cell(spec, cp.BoundingBox(40, 40))
        furnaces = [e for e in placed.entities if "furnace" in e["name"]]
        self.assertTrue(furnaces)
        for f in furnaces:
            self.assertNotIn("recipe", f)  # furnaces auto-smelt

    def test_coverage_detects_uncovered_machine(self):
        prof = knowledge.pole_profile("small-electric-pole")
        machines = [(0, 0, 3, 3), (100, 100, 3, 3)]  # second machine far away
        poles = [(0, 0)]
        self.assertFalse(cp._coverage_ok(machines, poles, prof))
        self.assertTrue(cp._coverage_ok([(0, 0, 3, 3)], poles, prof))

    def test_connectivity_detects_disconnected_poles(self):
        # two poles 50 tiles apart exceed small-pole wire reach (7.5)
        self.assertFalse(cp._connectivity_ok([(0, 0), (50, 0)], 7.5))
        self.assertTrue(cp._connectivity_ok([(0, 0), (5, 0)], 7.5))

    def test_substages_are_placed(self):
        placed = cp.place_cell(self._ec_spec(), cp.BoundingBox(40, 40))
        # EC cell co-locates copper-cable -> both assembling machines present
        ec = [e for e in placed.entities if e.get("recipe") == "electronic-circuit"]
        cable = [e for e in placed.entities if e.get("recipe") == "copper-cable"]
        self.assertTrue(ec)
        self.assertTrue(cable)

    def _collisions(self, placed):
        from collections import defaultdict
        occ = defaultdict(list)
        for e in placed.entities:
            for t in cp._entity_tiles(e["name"], e["position"]["x"], e["position"]["y"]):
                occ[t].append(e["name"])
        return {t: v for t, v in occ.items() if len(v) > 1}

    def test_no_entity_collisions(self):
        # the sandbox rejects overlapping entities; the placer must never produce any.
        for item, rate, machines in [
            ("electronic-circuit", 60, ["assembling-machine-1"]),
            ("electronic-circuit", 30, ["assembling-machine-2"]),
            ("iron-gear-wheel", 60, ["assembling-machine-1"]),
            ("copper-cable", 90, ["assembling-machine-1"]),
        ]:
            spec = compile_cell(item, rate, available_machines=machines, belt_tiers_available=["transport-belt"])
            placed = cp.place_cell(spec, cp.BoundingBox(80, 80))
            self.assertEqual(self._collisions(placed), {}, f"{item}@{rate} has overlapping entities")

    def test_boundary_sources_and_destination(self):
        # every ingredient enters at a west-boundary source belt; the product exits at the single
        # east-boundary destination (the paper's source/destination model + user's I/O requirement).
        spec = compile_cell("electronic-circuit", 60, available_machines=["assembling-machine-1"],
                            belt_tiers_available=["transport-belt"])
        placed = cp.place_cell(spec, cp.BoundingBox(80, 80))
        src_items = {s["item"] for s in placed.sources}
        self.assertEqual(src_items, {"copper-plate", "iron-plate"})  # raw inputs, not copper-cable
        self.assertIsNotNone(placed.destination)
        self.assertEqual(placed.destination["item"], "electronic-circuit")
        # destination is east of all sources.
        self.assertGreater(placed.destination["x"], max(s["x"] for s in placed.sources))

    def test_every_machine_is_wired(self):
        spec = compile_cell("electronic-circuit", 60, available_machines=["assembling-machine-1"],
                            belt_tiers_available=["transport-belt"])
        placed = cp.place_cell(spec, cp.BoundingBox(80, 80), long_inserter_available=True)
        self.assertTrue(placed.connectivity_ok)

    def test_continuous_input_lane_reaches_boundary(self):
        # the copper-plate lane must be a continuous run of belts from the boundary into the cell
        # (no isolated single-tile stubs).
        spec = compile_cell("electronic-circuit", 60, available_machines=["assembling-machine-1"],
                            belt_tiers_available=["transport-belt"])
        placed = cp.place_cell(spec, cp.BoundingBox(80, 80))
        src = next(s for s in placed.sources if s["item"] == "copper-plate")
        lane_xs = sorted(e["position"]["x"] for e in placed.entities
                         if e["name"] == "transport-belt" and e["position"]["y"] == src["y"])
        self.assertGreaterEqual(len(lane_xs), 8)  # a real lane, not a stub
        # contiguous: no gap larger than 1 tile.
        for a, b in zip(lane_xs, lane_xs[1:]):
            self.assertLessEqual(b - a, 1.0)


if __name__ == "__main__":
    unittest.main()
