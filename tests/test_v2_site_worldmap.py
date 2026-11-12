import unittest

from factorio_agent.board import entity
from factorio_agent.site.fakeworld import make_world
from factorio_agent.site.geometry import Rect, chunk_aligned, plan_rect, rect_from_box, split_rect
from factorio_agent.site.rle import as_rows, decode_runs, encode_runs
from factorio_agent.site.worldmap import WATER, Feature, UnsurveyedError, Well, WorldMap


def blank(rect=Rect(-32, -32, 32, 32)):
    world = WorldMap()
    world.mark_surveyed(rect)
    return world


class GeometryTests(unittest.TestCase):
    def test_box_to_tiles_ignores_grazing_edges(self):
        self.assertEqual(rect_from_box(-0.7, -0.7, 1.7, 1.7), Rect(-1, -1, 2, 2))
        self.assertEqual(rect_from_box(2.1, 3.1, 2.9, 3.9), Rect(2, 3, 3, 4))
        self.assertEqual(rect_from_box(0.0, 0.0, 2.0, 1.0), Rect(0, 0, 2, 1))

    def test_plan_rect_uses_footprint_and_rotation(self):
        self.assertEqual(plan_rect(entity("assembling-machine-1", 0.5, 0.5)), Rect(-1, -1, 2, 2))
        self.assertEqual(plan_rect(entity("boiler", 1.5, 1.0)), Rect(0, 0, 3, 2))
        self.assertEqual(plan_rect(entity("boiler", 1.0, 1.5, 4)), Rect(0, 0, 2, 3))
        self.assertEqual(plan_rect(entity("transport-belt", -3.5, 7.5)), Rect(-4, 7, -3, 8))

    def test_chunk_alignment_handles_negative_tiles(self):
        self.assertEqual(chunk_aligned(Rect(-33, -1, 1, 64)), Rect(-64, -32, 32, 64))

    def test_split_covers_rect_without_overlap(self):
        parts = list(split_rect(Rect(-64, -64, 96, 32), 96))
        tiles = [t for p in parts for t in p.tiles()]
        self.assertEqual(len(tiles), len(set(tiles)))
        self.assertEqual(len(tiles), 160 * 96)


class RleTests(unittest.TestCase):
    def test_round_trip_splits_runs_on_value_row_and_gap(self):
        cells = {(0, 0): 1, (1, 0): 1, (2, 0): 2, (4, 0): 2, (0, 1): 1, (-1, 1): 1}
        runs = encode_runs(cells)
        self.assertEqual(runs, [[0, 0, 2, 1], [0, 2, 1, 2], [0, 4, 1, 2], [1, -1, 2, 1]])
        self.assertEqual({(x, y): v for x, y, v in decode_runs(runs)}, cells)

    def test_empty_lua_table_decodes_as_empty_list(self):
        self.assertEqual(as_rows({}), [])
        self.assertEqual(as_rows(None), [])
        with self.assertRaises(ValueError):
            as_rows({"x": 1})


class GridQueryTests(unittest.TestCase):
    def test_buildable_by_terrain_and_obstacle(self):
        world = blank()
        world.set_terrain(1, 1, WATER)
        world.add_feature(Feature("tree", "tree-01", Rect(2, 2, 3, 3)))
        world.add_feature(Feature("cliff", "cliff", Rect(4, 4, 8, 6)))
        world.add_feature(Feature("entity", "stone-furnace", Rect(10, 10, 12, 12)))
        self.assertTrue(world.is_buildable(0, 0))
        self.assertFalse(world.is_buildable(1, 1))
        self.assertTrue(world.is_buildable(2, 2))
        self.assertFalse(world.is_buildable(2, 2, allow_clearable=False))
        self.assertFalse(world.is_buildable(7, 5))
        self.assertFalse(world.is_buildable(11, 11))
        self.assertEqual(world.occupied_by(11, 11), "stone-furnace")
        self.assertIsNone(world.occupied_by(2, 2))

    def test_free_rect_reports_reasons_clearables_and_resources(self):
        world = blank()
        world.set_terrain(0, 0, WATER)
        rock = world.add_feature(Feature("rock", "huge-rock", Rect(2, 0, 4, 2)))
        world.set_ore(3, 3, "iron-ore", 500)
        report = world.free_rect(0, 0, 5, 5)
        self.assertFalse(report.free)
        self.assertEqual(report.blocked, {"water": [(0, 0)]})
        self.assertEqual(report.clearable, [rock])
        self.assertEqual(report.resources, {(3, 3): "iron-ore"})
        strict = world.free_rect(1, 0, 5, 5, allow_clearable=False)
        self.assertEqual(sorted(strict.blocked["rock"]), [(2, 0), (2, 1), (3, 0), (3, 1)])
        self.assertTrue(world.free_rect(5, 5, 9, 9).free)

    def test_unsurveyed_tiles_raise_instead_of_reading_free(self):
        world = blank(Rect(0, 0, 32, 32))
        self.assertTrue(world.is_buildable(31, 31))
        with self.assertRaises(UnsurveyedError):
            world.is_buildable(32, 0)
        with self.assertRaises(UnsurveyedError):
            world.free_rect(-1, 0, 4, 4)
        with self.assertRaises(UnsurveyedError):
            world.resource_at(0, -1)
        with self.assertRaises(ValueError):
            world.mark_surveyed(Rect(0, 0, 10, 10))

    def test_well_footprint_reports_resource_on_every_tile(self):
        world = blank()
        world.add_well(Well("crude-oil", (5, 5), 300000))
        self.assertEqual(world.resource_at(4, 6), ("crude-oil", 300000))
        self.assertIsNone(world.resource_at(7, 5))


class PatchTests(unittest.TestCase):
    def test_separate_patches_of_same_ore_stay_separate(self):
        world = blank()
        for x, y in Rect(0, 0, 3, 3).tiles():
            world.set_ore(x, y, "iron-ore", 100)
        for x, y in Rect(10, 0, 12, 2).tiles():
            world.set_ore(x, y, "iron-ore", 50)
        patches = world.resource_patches("iron-ore")
        self.assertEqual([p.tile_count for p in patches], [9, 4])
        self.assertEqual(patches[0].amount, 900)
        self.assertEqual(patches[0].bbox, Rect(0, 0, 3, 3))
        self.assertEqual(patches[0].centroid, (1.5, 1.5))
        self.assertTrue(patches[1].contains(11, 1))

    def test_diagonal_tiles_connect_but_other_ores_do_not(self):
        world = blank()
        world.set_ore(0, 0, "coal", 10)
        world.set_ore(1, 1, "coal", 10)
        world.set_ore(2, 2, "stone", 10)
        self.assertEqual(len(world.resource_patches("coal")), 1)
        self.assertEqual([p.name for p in world.resource_patches()], ["coal", "stone"])

    def test_wells_join_into_fields_by_distance(self):
        world = blank(Rect(-64, -64, 64, 64))
        world.add_well(Well("crude-oil", (0, 0), 100))
        world.add_well(Well("crude-oil", (6, 4), 200))
        world.add_well(Well("crude-oil", (50, 50), 300))
        patches = world.resource_patches("crude-oil")
        self.assertEqual([p.wells for p in patches], [((0, 0), (6, 4)), ((50, 50),)])
        self.assertEqual(patches[0].tile_count, 18)


class ShoreTests(unittest.TestCase):
    def test_water_near_is_sorted_and_shore_points_at_water(self):
        world = blank()
        for x, y in Rect(5, -2, 10, 3).tiles():
            world.set_terrain(x, y, WATER)
        near = world.water_tiles_near(0, 0, 6)
        self.assertEqual(near[0], (5, 0))
        self.assertTrue(all(abs(x) <= 6 for x, _ in near))
        shore = world.nearest_shore(0, 0)
        self.assertEqual((shore.water, shore.land, shore.direction), ((5, 0), (4, 0), 4))
        world.add_feature(Feature("cliff", "cliff", Rect(4, -3, 5, 4)))
        self.assertEqual(world.nearest_shore(0, 0).land, (5, -3))

    def test_no_water_means_no_shore(self):
        self.assertIsNone(blank().nearest_shore(0, 0, 10))


class PlanAndZoneTests(unittest.TestCase):
    def test_apply_plan_marks_footprints_and_can_be_cleared(self):
        world = blank()
        plan = [entity("assembling-machine-1", 0.5, 0.5), entity("inserter", 2.5, 0.5, 4)]
        world.apply_plan(plan, owner="red-science")
        self.assertEqual(world.occupied_by(-1, 1), "assembling-machine-1")
        self.assertEqual(world.free_rect(2, 0, 3, 1).blocked, {"plan": [(2, 0)]})
        self.assertEqual(world.clear_plans("other"), 0)
        self.assertEqual(world.clear_plans("red-science"), 2)
        self.assertTrue(world.is_buildable(0, 0))

    def test_reservations_block_other_owners_only(self):
        world = blank()
        world.reserve(Rect(0, 0, 4, 4), "smelting")
        world.reserve(Rect(2, 2, 6, 6), "smelting")
        self.assertEqual(world.reserved_by(5, 5), "smelting")
        self.assertIsNone(world.reserved_by(6, 6))
        with self.assertRaises(ValueError):
            world.reserve(Rect(3, 3, 8, 8), "bus")
        self.assertTrue(world.free_rect(0, 0, 4, 4, owner="smelting").free)
        self.assertEqual(len(world.free_rect(0, 0, 2, 1, owner="bus").blocked["reserved"]), 2)
        world.release("smelting")
        self.assertEqual(world.zones(), [])
        world.reserve(Rect(3, 3, 8, 8), "bus")


class JsonTests(unittest.TestCase):
    def test_round_trip_keeps_every_layer(self):
        world = make_world(7, size=128)
        world.apply_plan([entity("lab", 0.5, 0.5)], owner="research")
        world.reserve(Rect(4, 4, 10, 10), "mall")
        text = world.to_json()
        again = WorldMap.from_json(text)
        self.assertEqual(again.to_json(), text)
        self.assertEqual(again.ore_tiles(), world.ore_tiles())
        self.assertEqual(again.terrain_tiles(), world.terrain_tiles())
        self.assertEqual(
            sorted(map(Feature.key, again.features()), key=repr), sorted(map(Feature.key, world.features()), key=repr)
        )
        self.assertEqual(again.reserved_by(5, 5), "mall")
        self.assertEqual(again.surveyed_chunks(), world.surveyed_chunks())

    def test_large_map_serialises_well_under_five_megabytes(self):
        world = make_world(3, size=640, patches=6, lakes=10, forests=30, forest_density=0.6, rocks=80)
        self.assertLess(len(world.to_json().encode()), 5_000_000)

    def test_rejects_foreign_json(self):
        with self.assertRaises(ValueError):
            WorldMap.from_json('{"format": "other"}')


if __name__ == "__main__":
    unittest.main()
