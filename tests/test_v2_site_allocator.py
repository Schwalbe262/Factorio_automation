import unittest

from factorio_agent.site.fakeworld import make_world
from factorio_agent.site.geometry import Rect
from factorio_agent.site.masterplan import MasterPlan, plan_site
from factorio_agent.site.planmodel import SiteError
from factorio_agent.site.worldmap import Feature, WorldMap


def blank(half=256):
    world = WorldMap()
    world.mark_surveyed(Rect(-half, -half, half, half))
    return world


def gap_between(a, b):
    """Along-axis distance between two allocations on the same side."""
    if a.along1 <= b.along0:
        return b.along0 - a.along1
    return a.along0 - b.along1


class AllocationOrderTests(unittest.TestCase):
    def setUp(self):
        self.world = blank()
        self.plan = plan_site(self.world)
        self.config = self.plan.config

    def test_alternates_sides_nearest_head_first(self):
        blocks = [self.plan.allocate(self.world, "smelter", 40, 30, name=f"b{i}") for i in range(6)]
        self.assertEqual([b.side for b in blocks], ["right", "left", "right", "left", "right", "left"])
        step = 40 + self.config.district_gap
        self.assertEqual([b.along0 for b in blocks], [0, 0, step, step, 2 * step, 2 * step])
        self.assertEqual(self.plan.validate(self.world), [])

    def test_gap_setback_and_geometry(self):
        a = self.plan.allocate(self.world, "x", 40, 30, name="a")
        self.plan.allocate(self.world, "x", 40, 30, name="b")
        c = self.plan.allocate(self.world, "x", 40, 30, name="c")
        self.assertEqual(a.side, c.side)
        self.assertEqual(gap_between(a, c), self.config.district_gap)
        bus = self.plan.bus
        self.assertEqual(a.rect, bus.side_rect(a.side, 0, 40, 3, 33))
        self.assertEqual(a.tap_rect, bus.side_rect(a.side, 0, 40, 0, 3))
        self.assertFalse(a.rect.intersects(bus.rect()))
        self.assertEqual(a.flow, bus.direction)
        self.assertEqual(a.rect.width * a.rect.height, 40 * 30)

    def test_bus_edge_faces_the_bus(self):
        a = self.plan.allocate(self.world, "x", 40, 30, name="a")
        b = self.plan.allocate(self.world, "x", 40, 30, name="b")
        bus = self.plan.bus.rect()
        for block in (a, b):
            r = block.rect
            gap = {"W": r.x0 - bus.x1, "E": bus.x0 - r.x1, "N": r.y0 - bus.y1, "S": bus.y0 - r.y1}[block.bus_edge]
            self.assertEqual(gap, self.config.district_setback, block)

    def test_reserved_on_the_world(self):
        a = self.plan.allocate(self.world, "x", 40, 30, name="a")
        self.assertEqual(self.world.reserved_by(*a.rect.anchor()), "block:a")
        self.assertEqual(self.world.reserved_by(*a.tap_rect.anchor()), "districts:" + a.side)
        with self.assertRaises(ValueError):
            self.world.reserve(a.rect, "intruder")

    def test_exhaustion_raises(self):
        with self.assertRaises(SiteError) as caught:
            for index in range(100):
                self.plan.allocate(self.world, "x", 60, 40, name=f"b{index}")
        self.assertIn("exhausted", str(caught.exception))
        count = len(self.plan.allocations)
        slots = (self.plan.bus.length + self.config.district_gap) // (60 + self.config.district_gap)
        self.assertEqual(count, 2 * slots)
        self.assertEqual(self.plan.validate(self.world), [])

    def test_rejects_bad_requests(self):
        self.plan.allocate(self.world, "x", 40, 30, name="a")
        with self.assertRaises(SiteError):
            self.plan.allocate(self.world, "x", 40, 30, name="a")
        with self.assertRaises(SiteError):
            self.plan.allocate(self.world, "x", 40, self.plan.district_depth + 1, name="deep")
        with self.assertRaises(SiteError):
            self.plan.allocate(self.world, "x", 0, 10, name="flat")


class ReleaseExtendTests(unittest.TestCase):
    def setUp(self):
        self.world = blank()
        self.plan = plan_site(self.world)
        self.a = self.plan.allocate(self.world, "x", 40, 30, name="a")
        self.b = self.plan.allocate(self.world, "x", 40, 30, name="b")
        self.c = self.plan.allocate(self.world, "x", 40, 30, name="c")

    def test_extend_last_block_grows_away_from_head(self):
        grown = self.plan.extend(self.world, "c", 20)
        self.assertEqual((grown.along0, grown.along1), (self.c.along0, self.c.along1 + 20))
        self.assertEqual(self.world.reserved_by(*grown.rect.anchor()), "block:c")
        self.assertEqual(self.plan.validate(self.world), [])

    def test_extend_into_neighbour_gap_raises(self):
        with self.assertRaises(SiteError):
            self.plan.extend(self.world, "a", 1)

    def test_extend_onto_blocked_land_raises(self):
        far = self.plan.bus.side_rect("left", 41, 43, 10, 12)
        self.world.add_feature(Feature("cliff", "cliff", far))
        with self.assertRaises(SiteError):
            self.plan.extend(self.world, "b", 10)

    def test_release_frees_the_slot(self):
        released = self.plan.release(self.world, "b")
        self.assertEqual(released.name, "b")
        self.assertEqual(self.world.reserved_by(*self.b.rect.anchor()), "districts:left")
        again = self.plan.allocate(self.world, "x", 40, 30, name="d")
        self.assertEqual((again.side, again.along0), ("left", 0))
        self.assertEqual(self.plan.validate(self.world), [])
        with self.assertRaises(SiteError):
            self.plan.release(self.world, "missing")


class SkipAndReloadTests(unittest.TestCase):
    def test_skips_blocked_land_with_free_rect(self):
        world = blank()
        plan = plan_site(world)
        cliff = plan.bus.side_rect("right", 10, 12, 20, 22)
        world.add_feature(Feature("cliff", "cliff", cliff))
        first = plan.allocate(world, "x", 40, 30, name="a")
        second = plan.allocate(world, "x", 40, 30, name="b")
        self.assertEqual((first.side, first.along0), ("left", 0))
        self.assertEqual(second.side, "right")
        self.assertGreaterEqual(second.along0, 12)
        self.assertFalse(second.rect.intersects(cliff))

    def test_trees_do_not_block(self):
        world = blank()
        plan = plan_site(world)
        tree = plan.bus.side_rect("right", 5, 6, 10, 11)
        world.add_feature(Feature("tree", "tree-01", tree))
        first = plan.allocate(world, "x", 40, 30, name="a")
        self.assertEqual((first.side, first.along0), ("right", 0))

    def test_reload_then_allocate_matches_continuous(self):
        world = make_world(4, size=512)
        plan = plan_site(world)
        sizes = [(40, 30), (24, 48), (60, 20), (32, 32), (48, 40), (20, 20)]
        for index, (along, depth) in enumerate(sizes[:3]):
            plan.allocate(world, "x", along, depth, name=f"b{index}")
        world2 = WorldMap.from_json(world.to_json())
        plan2 = MasterPlan.from_json(plan.to_json())
        for index, (along, depth) in enumerate(sizes[3:], start=3):
            first = plan.allocate(world, "x", along, depth, name=f"b{index}")
            second = plan2.allocate(world2, "x", along, depth, name=f"b{index}")
            self.assertEqual(first, second)
        self.assertEqual(plan.to_json(), plan2.to_json())
        self.assertEqual(plan2.validate(world2), [])

    def test_seeded_maps_allocate_without_ore(self):
        for seed in (2, 7, 11):
            with self.subTest(seed=seed):
                world = make_world(seed, size=512)
                plan = plan_site(world)
                for index in range(8):
                    block = plan.allocate(world, "x", 40, 40, name=f"b{index}")
                    report = world.free_rect(*block.rect.to_list(), owner=block.owner)
                    self.assertTrue(report.free)
                    self.assertEqual(report.resources, {})
                self.assertEqual(plan.validate(world), [])


if __name__ == "__main__":
    unittest.main()
