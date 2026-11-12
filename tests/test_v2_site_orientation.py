import unittest
from dataclasses import replace

from site_transforms import map_point, map_rect, map_side, map_tile, transform_world

from factorio_agent.site.fakeworld import make_world
from factorio_agent.site.geometry import Rect
from factorio_agent.site.masterplan import plan_site
from factorio_agent.site.orientation import facing_side, sides_facing
from factorio_agent.site.powersite import DIRECTION_COMPASS
from factorio_agent.site.worldmap import WATER, WorldMap

TRANSFORMS = ("flip_x", "flip_y", "rotate")
MIRROR_SEEDS = (2, 7)


def blank(half=256):
    world = WorldMap()
    world.mark_surveyed(Rect(-half, -half, half, half))
    return world


def north_ore_world():
    """Land only in a north-south band; iron and copper north of the start."""
    world = blank()
    land = Rect(-80, -256, 80, 256)
    for x, y in world.bounds().tiles():
        if not land.contains(x, y):
            world.set_terrain(x, y, WATER)
    for x, y in Rect(-30, -140, -10, -120).tiles():
        world.set_ore(x, y, "iron-ore", 1500)
    for x, y in Rect(10, -140, 30, -120).tiles():
        world.set_ore(x, y, "copper-ore", 1500)
    return world


def assert_mirrored(test, plan, other, kind):
    """``other`` must be ``plan`` transformed by ``kind`` (bus, zones, sides, corridors)."""
    test.assertEqual(other.bus.direction, map_side(kind, plan.bus.direction))
    test.assertEqual(other.bus.origin, map_point(kind, plan.bus.origin))
    test.assertEqual(other.bus.length, plan.bus.length)
    twins = {zone.name: zone for zone in other.zones}
    test.assertEqual(set(twins), {zone.name for zone in plan.zones})
    for zone in plan.zones:
        twin = twins[zone.name]
        test.assertEqual(twin.rect, map_rect(kind, zone.rect), zone.name)
        test.assertEqual(twin.input_side, map_side(kind, zone.input_side), zone.name)
        test.assertEqual(twin.output_side, map_side(kind, zone.output_side), zone.name)
        ports = sorted((p.material, p.role, p.side) for p in twin.ports)
        expected = sorted((p.material, p.role, map_side(kind, p.side)) for p in zone.ports)
        test.assertEqual(ports, expected, zone.name)
    corridors = {c.name: c for c in other.corridors}
    test.assertEqual(set(corridors), {c.name for c in plan.corridors})
    for corridor in plan.corridors:
        twin = corridors[corridor.name]
        test.assertEqual(twin.from_side, map_side(kind, corridor.from_side), corridor.name)
        test.assertEqual(twin.to_side, map_side(kind, corridor.to_side), corridor.name)
        directions = [map_side(kind, s.direction) for s in corridor.segments]
        test.assertEqual([s.direction for s in twin.segments], directions, corridor.name)


class MirrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {}
        for seed in MIRROR_SEEDS:
            world = make_world(seed, size=512)
            plan = plan_site(make_world(seed, size=512))
            for kind in TRANSFORMS:
                twin_world = transform_world(world, kind)
                twin = plan_site(twin_world, start=map_tile(kind, (0, 0)))
                cls.cases[(seed, kind)] = (plan, twin, twin_world)

    def test_plans_follow_mirrors_and_rotation(self):
        for (seed, kind), (plan, twin, _world) in self.cases.items():
            with self.subTest(seed=seed, kind=kind):
                assert_mirrored(self, plan, twin, kind)

    def test_mirrored_plans_validate(self):
        for (seed, kind), (_plan, twin, world) in self.cases.items():
            with self.subTest(seed=seed, kind=kind):
                self.assertEqual(twin.validate(world), [])


class GeometryDrivenSideTests(unittest.TestCase):
    def test_ore_north_runs_bus_south_and_feeds_smelting_from_north(self):
        world = north_ore_world()
        plan = plan_site(world)
        self.assertEqual(plan.validate(world), [])
        self.assertEqual(plan.bus.direction, "S")
        smelting = plan.zone("smelting")
        self.assertEqual(smelting.input_side, "N")
        self.assertEqual(smelting.output_side, "S")
        for name in ("iron-ore", "copper-ore"):
            self.assertEqual(plan.zone("mining:" + name).output_side, "S")
            corridor = plan.corridor(f"corridor:{name}->smelting")
            self.assertEqual((corridor.from_side, corridor.to_side), ("S", "N"))
        entries = [p.material for p in smelting.ports if p.role == "in"]
        self.assertEqual(entries, ["iron-ore", "copper-ore"])

    def test_mirrored_north_ore_map_does_the_opposite(self):
        world = transform_world(north_ore_world(), "flip_y")
        plan = plan_site(world, start=map_tile("flip_y", (0, 0)))
        self.assertEqual(plan.validate(world), [])
        self.assertEqual(plan.bus.direction, "N")
        self.assertEqual(plan.zone("smelting").input_side, "S")
        self.assertEqual(plan.zone("smelting").output_side, "N")

    def test_entry_points_follow_lateral_order_of_patches(self):
        world = transform_world(north_ore_world(), "flip_x")
        plan = plan_site(world, start=map_tile("flip_x", (0, 0)))
        entries = [p.material for p in plan.zone("smelting").ports if p.role == "in"]
        self.assertEqual(entries, ["copper-ore", "iron-ore"])

    def test_bus_head_and_tail(self):
        plan = plan_site(north_ore_world())
        bus = plan.bus
        self.assertEqual(bus.head, bus.origin)
        self.assertEqual(bus.tail, (bus.origin[0], bus.origin[1] + bus.length))
        self.assertEqual(plan.zone("bus").input_side, "N")

    def test_power_sides_differ_and_face_the_shore(self):
        world = blank()
        for x, y in Rect(-90, -20, -60, 20).tiles():
            world.set_terrain(x, y, WATER)
        plan = plan_site(world)
        power = plan.zone("power")
        water = power.port("water", "in")
        self.assertEqual(water.side, DIRECTION_COMPASS[plan.water.direction])
        self.assertTrue(power.rect.contains(*plan.water.land))
        self.assertFalse(power.rect.contains(*plan.water.water))
        self.assertNotEqual(power.output_side, water.side)
        self.assertEqual(power.port("electricity", "out").side, power.output_side)


class AllocationSideTests(unittest.TestCase):
    def setUp(self):
        self.world = blank()
        self.plan = plan_site(self.world)

    def test_returning_block_leaves_on_the_bus_edge_downstream(self):
        block = self.plan.allocate(self.world, "plates", 40, 30, name="a")
        self.assertTrue(block.returns_to_bus)
        self.assertEqual(block.input_side, block.bus_edge)
        self.assertEqual(block.output_side, block.bus_edge)
        self.assertEqual(block.flow, self.plan.bus.direction)
        along, _lateral = self.plan.bus.local_tile(int(block.output_point[0]), int(block.output_point[1]))
        self.assertEqual(along, block.along1 - 1)

    def test_terminal_block_leaves_on_the_far_edge(self):
        block = self.plan.allocate(self.world, "science", 40, 30, name="labs", returns_to_bus=False)
        self.assertEqual(block.input_side, block.bus_edge)
        self.assertFalse(block.returns_to_bus)
        opposite = {"N": "S", "S": "N", "E": "W", "W": "E"}[block.bus_edge]
        self.assertEqual(block.output_side, opposite)

    def test_all_blocks_flow_downstream_on_both_sides(self):
        for index in range(4):
            self.plan.allocate(self.world, "x", 30, 20, name=f"b{index}", returns_to_bus=index % 2 == 0)
        flows = {(a.side, a.flow) for a in self.plan.allocations}
        self.assertEqual(flows, {("left", self.plan.bus.direction), ("right", self.plan.bus.direction)})
        self.assertEqual(self.plan.validate(self.world), [])

    def test_extend_keeps_the_output_rule(self):
        self.plan.allocate(self.world, "x", 30, 20, name="a", returns_to_bus=False)
        grown = self.plan.extend(self.world, "a", 10)
        self.assertFalse(grown.returns_to_bus)
        self.assertEqual(grown.along1, 40)


class ValidateSideTests(unittest.TestCase):
    def test_validate_catches_wrong_sides(self):
        world = north_ore_world()
        plan = plan_site(world)
        smelting = plan.zone("smelting")
        bad = replace(smelting, input_side=smelting.output_side)
        plan.zones = tuple(bad if z.name == "smelting" else z for z in plan.zones)
        problems = plan.validate(world)
        self.assertTrue(any("same side" in p for p in problems), problems)
        self.assertTrue(any("not its input side" in p for p in problems), problems)

    def test_validate_catches_mixed_flows(self):
        world = blank()
        plan = plan_site(world)
        plan.allocate(world, "x", 30, 20, name="a")
        plan.allocate(world, "x", 30, 20, name="b")
        plan.allocate(world, "x", 30, 20, name="c")
        turned = {"N": "S", "S": "N", "E": "W", "W": "E"}[plan.bus.direction]
        plan.allocations[2] = replace(plan.allocations[2], flow=turned)
        problems = plan.validate(world)
        self.assertTrue(any("flow in different directions" in p for p in problems), problems)


class FacingTests(unittest.TestCase):
    def test_sides_facing_ranks_by_geometry(self):
        rect = Rect(0, 0, 10, 10)
        self.assertEqual(sides_facing(rect, (5, -100))[0], "N")
        self.assertEqual(sides_facing(rect, (100, 6))[0], "E")
        self.assertEqual(sides_facing(rect, (100, 6))[-1], "W")
        self.assertEqual(facing_side(rect, (5, -100), exclude=("N",)), sides_facing(rect, (5, -100))[1])


if __name__ == "__main__":
    unittest.main()
