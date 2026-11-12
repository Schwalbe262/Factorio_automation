import unittest

from factorio_agent.blocks.params import RowLimits, lane_capacity
from factorio_agent.blocks.sizing import BeltLanes, plan_lanes, size_row
from v2_blocks_fixture import FLUID_RECIPES, ROW_RECIPES, fake_catalog, real_catalog

AM1 = "assembling-machine-1"


class FakeCatalogSizing(unittest.TestCase):
    def setUp(self):
        self.catalog = fake_catalog()

    def test_gear_count_and_flows(self):
        sizing = size_row(self.catalog, "iron-gear-wheel", 150, machine=AM1)
        self.assertEqual(sizing.count, 3)
        self.assertEqual(sizing.product.per_machine, 60)
        self.assertEqual(sizing.flow("iron-plate").total, 360)
        self.assertEqual(sizing.rated_per_minute, 180)
        self.assertEqual(sizing.input_belts, (BeltLanes("iron-plate", "iron-plate"),))
        self.assertEqual(sizing.lane_capacity_per_minute, 450)
        self.assertEqual(sizing.max_count, 7)

    def test_exact_rate_does_not_round_up(self):
        self.assertEqual(size_row(self.catalog, "automation-science-pack", 18, machine=AM1).count, 3)

    def test_product_amount_counts(self):
        sizing = size_row(self.catalog, "copper-cable", 120, machine=AM1)
        self.assertEqual((sizing.count, sizing.product.per_machine), (1, 120))
        self.assertEqual(sizing.flow("copper-plate").per_machine, 60)

    def test_no_unlocked_machine(self):
        with self.assertRaisesRegex(ValueError, "no unlocked row-compatible machine"):
            size_row(self.catalog, "iron-gear-wheel", 60)

    def test_best_unlocked_machine(self):
        self.assertEqual(size_row(self.catalog, "iron-gear-wheel", 60, researched=["automation"]).machine, AM1)
        both = size_row(self.catalog, "iron-gear-wheel", 60, researched=["automation", "automation-2"])
        self.assertEqual(both.machine, "assembling-machine-2")
        by_recipe = size_row(self.catalog, "iron-gear-wheel", 60, enabled_recipes=["assembling-machine-2"])
        self.assertEqual(by_recipe.machine, "assembling-machine-2")

    def test_faster_machine_with_unsupported_footprint_is_skipped(self):
        sizing = size_row(self.catalog, "iron-gear-wheel", 60, researched=["automation", "wide"])
        self.assertEqual(sizing.machine, AM1)
        with self.assertRaisesRegex(ValueError, "4x4 footprint"):
            size_row(self.catalog, "iron-gear-wheel", 60, machine="wide-plant")

    def test_category_mismatch(self):
        with self.assertRaisesRegex(ValueError, "crafting categories"):
            size_row(self.catalog, "iron-gear-wheel", 60, machine="chemical-plant")

    def test_recipe_lock_is_reported_not_enforced(self):
        self.assertTrue(size_row(self.catalog, "iron-gear-wheel", 60, machine=AM1).recipe_unlocked)
        self.assertFalse(size_row(self.catalog, "copper-cable", 60, machine=AM1).recipe_unlocked)

    def test_fluid_recipes_rejected_with_reason(self):
        for recipe in FLUID_RECIPES:
            with self.subTest(recipe=recipe):
                with self.assertRaisesRegex(ValueError, "fluid .* pipe"):
                    size_row(self.catalog, recipe, 1, machine="assembling-machine-2")

    def test_other_rejections(self):
        cases = {
            "five-part": "at most 4",
            "two-out": "2 products",
            "missing": "unknown recipe",
        }
        for recipe, reason in cases.items():
            with self.subTest(recipe=recipe):
                with self.assertRaisesRegex(ValueError, reason):
                    size_row(self.catalog, recipe, 1, machine=AM1)
        with self.assertRaisesRegex(ValueError, "split into several rows"):
            size_row(self.catalog, "iron-gear-wheel", 1200, machine=AM1)
        with self.assertRaisesRegex(ValueError, "positive"):
            size_row(self.catalog, "iron-gear-wheel", 0, machine=AM1)

    def test_lane_plans(self):
        two = size_row(self.catalog, "logistic-science-pack", 5, machine=AM1).input_belts
        self.assertEqual(two, (BeltLanes("inserter", "transport-belt"),))
        three = size_row(self.catalog, "inserter", 60, machine=AM1).input_belts
        self.assertEqual(len(three), 2)
        self.assertEqual(sorted(len(b.items) for b in three), [1, 2])
        lds = size_row(self.catalog, "low-density-structure", 2, machine=AM1).input_belts
        self.assertEqual(lds[0], BeltLanes("copper-plate", "copper-plate"))
        four = plan_lanes({"a": 4, "b": 3, "c": 2, "d": 1})
        self.assertEqual([len(b.items) for b in four], [2, 2])

    def test_belt_lane_capacity_override(self):
        limits = RowLimits(lane_items_per_minute={"transport-belt": 100})
        self.assertEqual(lane_capacity(self.catalog, "transport-belt", limits), 100)
        with self.assertRaisesRegex(ValueError, "not exported"):
            lane_capacity(self.catalog, "mystery-belt")


@unittest.skipIf(real_catalog() is None, "real eval-780299489 catalog not available")
class RealCatalogSizing(unittest.TestCase):
    def setUp(self):
        self.catalog = real_catalog()

    def test_row_recipes_size(self):
        for recipe in ROW_RECIPES:
            with self.subTest(recipe=recipe):
                sizing = size_row(self.catalog, recipe, 1, machine=AM1)
                self.assertEqual((sizing.count, sizing.machine_size), (1, 3))
                self.assertLessEqual(len(sizing.input_belts), 2)

    def test_all_researched_prefers_fastest_row_machine(self):
        techs = list(self.catalog.technologies)
        sizing = size_row(self.catalog, "copper-cable", 60, researched=techs)
        self.assertEqual(sizing.machine, "assembling-machine-3")

    def test_fresh_map_has_no_machine(self):
        with self.assertRaisesRegex(ValueError, "no unlocked"):
            size_row(self.catalog, "iron-gear-wheel", 60)

    def test_fluid_recipes_rejected(self):
        for recipe in FLUID_RECIPES:
            with self.subTest(recipe=recipe):
                with self.assertRaisesRegex(ValueError, "fluid"):
                    size_row(self.catalog, recipe, 1, machine="assembling-machine-2")

    def test_belt_speed_falls_back_to_documented_default(self):
        self.assertEqual(lane_capacity(self.catalog, "transport-belt"), 450)


if __name__ == "__main__":
    unittest.main()
