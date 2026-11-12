import unittest

from factorio_agent.blocks.audit import audit_row
from factorio_agent.blocks.derate import edge_capacity
from factorio_agent.blocks.row import extend_row, row_block
from factorio_agent.blocks.sizing import BeltLanes, size_row
from v2_blocks_fixture import FLOWS, fake_catalog, real_catalog

AM1 = "assembling-machine-1"
INSERTERS = ("inserter", "fast-inserter")


class EdgeCapacityTests(unittest.TestCase):
    def test_one_belt_uses_the_whole_edge(self):
        cap = edge_capacity(3, (2.0,), 1.0, (30, 40, 30))
        self.assertEqual((cap.crafts, cap.near, cap.far), (45.0, 3, 0))

    def test_two_belts_take_the_best_split(self):
        cap = edge_capacity(3, (3.0, 1.0), 1.0, (30, 40, 30))
        self.assertEqual((cap.near, cap.far), (2, 1))
        self.assertAlmostEqual(cap.crafts, 20.0)

    def test_output_edge_caps_the_rate(self):
        self.assertAlmostEqual(edge_capacity(3, (1.0,), 2.0, (120, 40, 30)).crafts, 45.0)

    def test_rejects_impossible_edges(self):
        with self.assertRaises(ValueError):
            edge_capacity(1, (1.0, 1.0), 1.0, (30, 40, 30))
        with self.assertRaises(ValueError):
            edge_capacity(3, (), 1.0, (30, 40, 30))


class FakeDerateTests(unittest.TestCase):
    def setUp(self):
        self.catalog = fake_catalog()

    def test_legacy_sizing_is_unchanged_without_an_inserter(self):
        sizing = size_row(self.catalog, "iron-gear-wheel", 60, machine=AM1)
        self.assertEqual((sizing.count, sizing.crafts_per_machine, sizing.inserter), (1, 60, None))
        self.assertFalse(sizing.derated)
        with self.assertRaisesRegex(ValueError, "faster inserter"):
            row_block(self.catalog, sizing)

    def test_derated_gear_row_uses_more_machines_and_audits_clean(self):
        sizing = size_row(self.catalog, "iron-gear-wheel", 120, machine=AM1, inserter="inserter")
        self.assertTrue(sizing.derated)
        self.assertEqual((sizing.count, sizing.crafts_per_machine, sizing.nameplate_crafts), (3, 45, 60))
        for flow in FLOWS:
            with self.subTest(flow=flow):
                plan = row_block(self.catalog, sizing, origin=(4.5, 9.5), flow=flow)
                self.assertEqual(audit_row(self.catalog, plan), [])
                self.assertEqual(plan["crafts_per_machine"], 45)
                self.assertEqual(plan["sizing_inserter"], "inserter")

    def test_audit_catches_a_declared_rate_the_inserters_cannot_carry(self):
        sizing = size_row(self.catalog, "iron-gear-wheel", 120, machine=AM1, inserter="inserter")
        plan = row_block(self.catalog, sizing, origin=(4.5, 9.5))
        plan["crafts_per_machine"] = 60
        self.assertIn("inserter_throughput_insufficient", {e.split(":")[0] for e in audit_row(self.catalog, plan)})
        plan["crafts_per_machine"] = 90
        self.assertIn("declared_rate_above_nameplate", {e.split(":")[0] for e in audit_row(self.catalog, plan)})

    def test_fewest_machines_wins_and_ties_prefer_the_slower_machine(self):
        both = ["automation", "automation-2"]
        sizing = size_row(self.catalog, "iron-gear-wheel", 60, researched=both, inserter="inserter")
        # Both machines are capped at 45 crafts/min by three plain inserters: take the cheaper one.
        self.assertEqual((sizing.machine, sizing.count), (AM1, 2))
        fast = size_row(self.catalog, "iron-gear-wheel", 80, researched=both, inserter="fast-inserter")
        self.assertEqual((fast.machine, fast.count), ("assembling-machine-2", 1))

    def test_explicit_one_item_per_belt_lanes(self):
        sizing = size_row(
            self.catalog,
            "electronic-circuit",
            30,
            machine=AM1,
            inserter="inserter",
            lanes=[("copper-cable",), ("iron-plate",)],
        )
        self.assertEqual(
            sizing.input_belts, (BeltLanes("copper-cable", "copper-cable"), BeltLanes("iron-plate", "iron-plate"))
        )
        self.assertAlmostEqual(sizing.crafts_per_machine, 20.0)
        self.assertEqual(audit_row(self.catalog, row_block(self.catalog, sizing, origin=(4.5, 9.5))), [])
        with self.assertRaisesRegex(ValueError, "exactly once"):
            size_row(self.catalog, "electronic-circuit", 30, machine=AM1, lanes=[("iron-plate",)])

    def test_extension_keeps_the_derated_rate(self):
        sizing = size_row(self.catalog, "iron-gear-wheel", 90, machine=AM1, inserter="inserter")
        plan = row_block(self.catalog, sizing, origin=(4.5, 9.5))
        extended = extend_row(plan, self.catalog, 1)
        self.assertEqual((extended["count"], extended["crafts_per_machine"]), (3, 45))
        self.assertEqual(audit_row(self.catalog, extended), [])

    def test_no_machine_fits_is_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "no unlocked machine fits one row"):
            size_row(self.catalog, "iron-gear-wheel", 5000, researched=["automation"], inserter="inserter")


@unittest.skipIf(real_catalog() is None, "real eval-780299489 catalog not available")
class RealDerateTests(unittest.TestCase):
    CASES = (("electronic-circuit", 60), ("iron-gear-wheel", 120), ("transport-belt", 60))

    def setUp(self):
        self.catalog = real_catalog()
        self.all_research = list(self.catalog.technologies)

    def test_named_rates_size_and_audit_clean_with_both_inserters(self):
        for recipe, rate in self.CASES:
            for inserter in INSERTERS:
                for researched in (self.all_research, ["automation"]):
                    with self.subTest(recipe=recipe, inserter=inserter, techs=len(researched)):
                        sizing = size_row(self.catalog, recipe, rate, researched=researched, inserter=inserter)
                        self.assertGreaterEqual(sizing.rated_per_minute, rate - 1e-9)
                        for flow in FLOWS:
                            plan = row_block(self.catalog, sizing, inserter=inserter, origin=(0.5, 0.5), flow=flow)
                            self.assertEqual(audit_row(self.catalog, plan), [])

    def test_circuit_at_sixty_no_longer_rejected_with_all_research(self):
        sizing = size_row(self.catalog, "electronic-circuit", 60, researched=self.all_research, inserter="inserter")
        self.assertTrue(sizing.derated)
        self.assertEqual(sizing.count, 3)
        self.assertEqual(sizing.machine, AM1)


if __name__ == "__main__":
    unittest.main()
