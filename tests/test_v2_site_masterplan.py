import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from factorio_agent.site.busframe import lane_table
from factorio_agent.site.corridors import route_double, route_sided, subtract_rects
from factorio_agent.site.fakeworld import make_world
from factorio_agent.site.geometry import Rect
from factorio_agent.site.masterplan import MasterPlan, main, plan_site
from factorio_agent.site.orientation import entry_slots
from factorio_agent.site.planmodel import SiteConfig, SiteError
from factorio_agent.site.tilegrid import Occupancy, TileGrid
from factorio_agent.site.worldmap import WATER, Feature, WorldMap

SEEDS = tuple(range(1, 13))


def blank(half=256):
    world = WorldMap()
    world.mark_surveyed(Rect(-half, -half, half, half))
    return world


def flood_except(world, land: Rect):
    """Turn every surveyed tile outside ``land`` into water."""
    for x, y in world.bounds().tiles():
        if not land.contains(x, y):
            world.set_terrain(x, y, WATER)


def paint_ore(world, rect: Rect, name: str, amount: int = 1000):
    for x, y in rect.tiles():
        world.set_ore(x, y, name, amount)


def bus_ore_tiles(world, plan):
    return [t for t in plan.bus.rect().tiles() if world.resource_at(*t) is not None]


class SeededPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worlds = {}
        cls.plans = {}
        for seed in SEEDS:
            world = make_world(seed, size=512)
            cls.worlds[seed] = world
            cls.plans[seed] = plan_site(world)

    def test_every_seed_passes_validation(self):
        for seed in SEEDS:
            with self.subTest(seed=seed):
                self.assertEqual(self.plans[seed].validate(self.worlds[seed]), [])

    def test_bus_is_long_enough_and_off_ore(self):
        for seed in SEEDS:
            with self.subTest(seed=seed):
                plan = self.plans[seed]
                self.assertGreaterEqual(plan.bus.length, plan.config.bus_min_length)
                self.assertEqual(bus_ore_tiles(self.worlds[seed], plan), [])

    def test_bus_does_not_touch_any_patch(self):
        for seed in SEEDS:
            with self.subTest(seed=seed):
                rect = self.plans[seed].bus.rect()
                for patch in self.worlds[seed].resource_patches():
                    self.assertFalse(any(rect.contains(*t) for t in patch.tiles), patch.name)

    def test_primary_patches_and_mining_zones(self):
        for seed in SEEDS:
            with self.subTest(seed=seed):
                plan = self.plans[seed]
                for name in plan.config.required_solids:
                    resource = plan.resource(name)
                    self.assertIsNotNone(resource.primary)
                    zone = plan.zone("mining:" + name)
                    bbox = resource.primary.bbox
                    self.assertTrue(zone.rect.x0 <= bbox.x0 and zone.rect.x1 >= bbox.x1)
                    self.assertTrue(zone.rect.y0 <= bbox.y0 and zone.rect.y1 >= bbox.y1)
                fluids = plan.resource("crude-oil")
                self.assertIsNotNone(fluids)
                self.assertIsNone(fluids.primary)

    def test_smelting_sits_behind_the_bus_head(self):
        for seed in SEEDS:
            with self.subTest(seed=seed):
                plan = self.plans[seed]
                smelting = plan.zone("smelting")
                self.assertIsNotNone(smelting)
                along, _lateral = plan.bus.local_tile(*smelting.rect.anchor())
                self.assertLess(along, 0)

    def test_zones_are_reserved_on_the_world(self):
        plan = self.plans[SEEDS[0]]
        world = self.worlds[SEEDS[0]]
        owners = {owner for owner, _rect in world.zones()}
        for name in ("bus", "smelting", "power", "districts:left", "districts:right"):
            self.assertIn(name, owners)
        with self.assertRaises(ValueError):
            world.reserve(plan.bus.rect(), "someone-else")

    def test_planning_is_deterministic(self):
        for seed in SEEDS[:3]:
            with self.subTest(seed=seed):
                again = plan_site(make_world(seed, size=512))
                self.assertEqual(again.to_json(), self.plans[seed].to_json())

    def test_json_round_trip(self):
        for seed in SEEDS:
            with self.subTest(seed=seed):
                plan = self.plans[seed]
                self.assertEqual(MasterPlan.from_json(plan.to_json()), plan)

    def test_reloaded_world_and_plan_still_validate(self):
        seed = SEEDS[1]
        world = WorldMap.from_json(self.worlds[seed].to_json())
        plan = MasterPlan.from_json(self.plans[seed].to_json())
        self.assertEqual(plan.validate(world), [])

    def test_different_seeds_give_different_buses(self):
        layouts = {(self.plans[s].bus.origin, self.plans[s].bus.direction) for s in SEEDS}
        directions = {self.plans[s].bus.direction for s in SEEDS}
        self.assertGreater(len(layouts), len(SEEDS) // 2)
        self.assertGreaterEqual(len(directions), 2)

    def test_lanes_follow_the_configured_order(self):
        plan = self.plans[SEEDS[0]]
        materials = [lane.material for lane in plan.bus.lanes]
        self.assertEqual(materials[:8], list(plan.config.lane_order))
        self.assertTrue(all(m == "" for m in materials[8:]))

    def test_validate_reports_tampering(self):
        seed = SEEDS[2]
        world = WorldMap.from_json(self.worlds[seed].to_json())
        plan = self.plans[seed]
        bus = plan.bus.rect()
        world.set_ore(bus.x0, bus.y0, "iron-ore", 10)
        problems = plan.validate(world)
        self.assertTrue(any("bus covers 1 resource tiles" in p for p in problems), problems)


class LaneTableTests(unittest.TestCase):
    def test_groups_of_four_with_two_tile_gaps(self):
        lanes = lane_table(28, ("a", "b"), 4, 2)
        self.assertEqual(len(lanes), 20)
        self.assertEqual([lane.offset for lane in lanes[:6]], [-14, -13, -12, -11, -8, -7])
        self.assertEqual(lanes[-1].offset, 13)
        self.assertEqual([lane.material for lane in lanes[:3]], ["a", "b", ""])
        self.assertEqual({lane.group for lane in lanes}, {0, 1, 2, 3, 4})


class HandBuiltMapTests(unittest.TestCase):
    def test_bus_takes_the_only_long_free_run(self):
        world = blank()
        flood_except(world, Rect(-24, -72, 256, 72))
        paint_ore(world, Rect(-20, 40, -12, 50), "iron-ore")
        paint_ore(world, Rect(-20, -50, -12, -40), "copper-ore")
        plan = plan_site(world)
        self.assertEqual(plan.bus.direction, "E")
        self.assertEqual(plan.validate(world), [])
        self.assertTrue(any("coal" in w for w in plan.warnings))

    def test_ore_across_the_obvious_path_is_avoided(self):
        world = blank()
        paint_ore(world, Rect(-120, -20, -100, 20), "iron-ore")
        paint_ore(world, Rect(-120, 30, -100, 60), "copper-ore")
        paint_ore(world, Rect(60, -256, 64, 256), "uranium-ore")
        plan = plan_site(world)
        self.assertEqual(plan.validate(world), [])
        self.assertEqual(bus_ore_tiles(world, plan), [])
        stripe = Rect(60, -256, 64, 256)
        self.assertFalse(plan.bus.rect().intersects(stripe))
        for strip in plan.strips:
            self.assertFalse(strip.rect.intersects(stripe))
        uranium = plan.resource("uranium-ore")
        self.assertIsNone(uranium.primary)
        self.assertEqual(len(uranium.others), 1)

    def test_narrow_land_degrades_district_depth(self):
        world = blank()
        flood_except(world, Rect(-128, -30, 256, 30))
        plan = plan_site(world)
        self.assertEqual(plan.district_depth, 24)
        self.assertTrue(any("district depth reduced" in w for w in plan.warnings))
        self.assertEqual(plan.validate(world), [])

    def test_small_map_shortens_the_bus(self):
        world = make_world(3, size=256)
        plan = plan_site(world)
        self.assertLess(plan.bus.length, plan.config.bus_target_length)
        self.assertGreaterEqual(plan.bus.length, plan.config.bus_min_length)
        self.assertTrue(any("bus shortened" in w for w in plan.warnings))
        self.assertEqual(plan.validate(world), [])

    def test_too_small_map_raises(self):
        with self.assertRaises(SiteError):
            plan_site(make_world(3, size=128))

    def test_cliff_wall_blocks_but_trees_do_not(self):
        world = blank()
        flood_except(world, Rect(-24, -72, 256, 72))
        for x in range(-24, 256, 2):
            world.add_feature(Feature("tree", "tree-01", Rect(x, 0, x + 1, 1)))
        world.add_feature(Feature("cliff", "cliff", Rect(200, -72, 202, 72)))
        plan = plan_site(world)
        self.assertEqual(plan.bus.direction, "E")
        self.assertFalse(plan.bus.rect().intersects(Rect(200, -72, 202, 72)))
        self.assertEqual(plan.validate(world), [])

    def test_shore_and_power_zone(self):
        world = blank()
        for x, y in Rect(-90, -20, -60, 20).tiles():
            world.set_terrain(x, y, WATER)
        plan = plan_site(world)
        self.assertIsNotNone(plan.water)
        power = plan.zone("power")
        land = plan.water.land
        self.assertTrue(power.rect.contains(*land))
        self.assertEqual(plan.validate(world), [])


class CorridorTests(unittest.TestCase):
    def test_subtract_rects_covers_exactly_the_rest(self):
        rect = Rect(0, 0, 10, 10)
        holes = [Rect(2, 2, 5, 5), Rect(4, 4, 8, 12), Rect(-3, 0, 1, 1)]
        pieces = subtract_rects(rect, holes)
        tiles = [t for p in pieces for t in p.tiles()]
        self.assertEqual(len(tiles), len(set(tiles)))
        expected = {t for t in rect.tiles() if not any(h.contains(*t) for h in holes)}
        self.assertEqual(set(tiles), expected)

    def test_straight_and_elbow_respect_the_sides(self):
        occupancy = Occupancy(TileGrid(blank(64)))
        a = Rect(-40, -40, -20, -20)
        straight, _cost = route_sided(occupancy, a, "E", Rect(0, -40, 20, -20), "W", 4)
        self.assertEqual([s.direction for s in straight.segments], ["E"])
        self.assertEqual(straight.segments[0].rect.x0, a.x1)
        elbow, _cost = route_sided(occupancy, a, "E", Rect(10, 10, 30, 30), "N", 4)
        self.assertEqual([s.direction for s in elbow.segments], ["E", "S"])
        self.assertEqual(elbow.segments[-1].rect.y1, 10)
        self.assertIsNone(route_sided(occupancy, a, "E", Rect(10, 10, 30, 30), "E", 4))

    def test_entry_slot_limits_the_entry_band(self):
        occupancy = Occupancy(TileGrid(blank(64)))
        route, _cost = route_sided(
            occupancy, Rect(-40, -40, -20, -20), "E", Rect(-40, 0, 20, 20), "N", 4, entry=(8, 16)
        )
        self.assertTrue(8 <= route.entry_band[0] and route.entry_band[1] <= 16)

    def test_double_elbow_fallback(self):
        occupancy = Occupancy(TileGrid(blank(64)))
        a = Rect(-40, -40, -20, -20)
        b = Rect(0, 10, 20, 30)
        z_shape, _cost = route_double(occupancy, a, "E", b, "W", 4)
        self.assertEqual([s.direction for s in z_shape.segments], ["E", "S", "E"])
        u_shape, _cost = route_double(occupancy, a, "N", Rect(0, -40, 20, -30), "N", 4)
        self.assertEqual([s.direction for s in u_shape.segments], ["N", "E", "S"])

    def test_route_avoids_ore_and_reports_none(self):
        world = blank(64)
        paint_ore(world, Rect(-64, -10, 64, -6), "coal")
        occupancy = Occupancy(TileGrid(world))
        self.assertIsNone(route_sided(occupancy, Rect(-10, -40, 10, -30), "S", Rect(-10, 20, 10, 30), "N", 4))

    def test_entry_slots_mirror(self):
        self.assertEqual(entry_slots((0, 64), 3), [(0, 21), (21, 43), (43, 64)])
        self.assertEqual(entry_slots((-64, 0), 3), [(-64, -43), (-43, -21), (-21, 0)])


class CommandLineTests(unittest.TestCase):
    def test_main_writes_svg_and_summary(self):
        world = make_world(5, size=512)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "map.json"
            target = Path(folder) / "plan.svg"
            source.write_text(world.to_json(), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main([str(source), str(target)])
            self.assertEqual(code, 0)
            self.assertIn("bus head", output.getvalue())
            self.assertIn("smelting", target.read_text(encoding="utf-8"))


class ConfigTests(unittest.TestCase):
    def test_config_round_trip_and_checks(self):
        config = SiteConfig(bus_width=20, lane_order=("iron-plate",))
        self.assertEqual(SiteConfig.from_dict(config.to_dict()), config)
        with self.assertRaises(ValueError):
            SiteConfig(bus_width=27)


class TouchingPatchTests(unittest.TestCase):
    def test_overlapping_bounding_boxes_are_trimmed_not_dropped(self):
        """Live seed 397906970: copper sat diagonally against iron and got no zone at all."""
        from factorio_agent.site.geometry import Rect
        from factorio_agent.site.siteselect import _trim, mining_rect

        iron = Rect(-48, 14, -14, 45)
        copper = Rect(-61, 33, -39, 55)
        stone = Rect(-51, 57, -35, 71)
        from types import SimpleNamespace

        occupancy = SimpleNamespace(grid=SimpleNamespace(bounds=Rect(-384, -384, 384, 384)), taken=[])
        config = SimpleNamespace(mining_margin=6)
        iron_zone = mining_rect(SimpleNamespace(bbox=iron), occupancy, [], config, (copper, stone))
        self.assertFalse(iron_zone.intersects(copper))
        occupancy.taken.append(iron_zone)
        copper_zone = mining_rect(SimpleNamespace(bbox=copper), occupancy, [], config, (iron, stone))
        self.assertIsNotNone(copper_zone)
        self.assertFalse(copper_zone.intersects(iron_zone))
        self.assertFalse(copper_zone.intersects(stone))
        # The cut is made on the axis that keeps more of the patch's own box.
        self.assertEqual(_trim(Rect(-67, 27, -48, 61), stone, copper), Rect(-67, 27, -48, 57))

    def test_a_neighbour_spanning_the_zone_on_one_axis_leaves_only_the_other_cut(self):
        """Fleet auto-1113-1758 seed 2159617943: the y strip was built inverted and faulted the plan."""
        from factorio_agent.site.geometry import Rect
        from factorio_agent.site.siteselect import _trim

        zone = Rect(-14, 20, 21, 32)
        # Taller than the zone, centre above it, reaching below it; it covers x 15..30.
        other = Rect(15, 0, 30, 35)
        self.assertEqual(_trim(zone, other), Rect(-14, 20, 15, 32))
        # Spanning both axes: nothing is left.
        self.assertIsNone(_trim(zone, Rect(-20, 0, 30, 35)))


if __name__ == "__main__":
    unittest.main()
