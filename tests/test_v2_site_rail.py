"""Rail reservations of the master plan (unload station, trunk, loading stations)."""

import unittest

from factorio_agent.site.fakeworld import make_world
from factorio_agent.site.geometry import Rect
from factorio_agent.site.masterplan import MasterPlan, plan_site
from factorio_agent.site.planmodel import SiteConfig, SiteError
from factorio_agent.site.worldmap import WorldMap

SEEDS = (2, 3, 7, 11)


def blank(half=256):
    world = WorldMap()
    world.mark_surveyed(Rect(-half, -half, half, half))
    return world


def paint(world, rect: Rect, name: str):
    for x, y in rect.tiles():
        world.set_ore(x, y, name, 1500)


def west_outpost_world():
    """Primary patches round the start; the only other patches lie far west."""
    world = blank()
    paint(world, Rect(30, -60, 50, -40), "iron-ore")
    paint(world, Rect(-50, -60, -30, -40), "copper-ore")
    paint(world, Rect(30, 40, 50, 60), "coal")
    paint(world, Rect(-50, 40, -30, 60), "stone")
    paint(world, Rect(-224, -12, -200, 12), "iron-ore")
    paint(world, Rect(-224, 70, -200, 94), "copper-ore")
    return world


class RailPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {}
        for seed in SEEDS:
            world = make_world(seed, size=512)
            cls.cases[seed] = (world, plan_site(world))

    def test_every_seed_has_an_unload_station_or_says_why(self):
        for seed, (world, plan) in self.cases.items():
            with self.subTest(seed=seed):
                station = plan.zone("rail:unload")
                if station is None:
                    self.assertTrue(any(w.startswith("rail:unload") for w in plan.warnings), plan.warnings)
                    continue
                self.assertEqual(plan.validate(world), [])
                smelting = plan.zone("smelting")
                ore = smelting.port("ore", "in")
                self.assertEqual(ore.side, station.growth)
                self.assertNotEqual(ore.side, smelting.output_side)
                feeder_sides = {p.side for p in smelting.ports if p.role == "in" and p.material != "ore"}
                if ore.side in feeder_sides:
                    self.assertTrue(any("shares the" in w for w in plan.warnings), plan.warnings)
                    self.assertTrue({"N", "E", "S", "W"} - {smelting.output_side} <= feeder_sides)
                config = plan.config
                self.assertEqual(
                    sorted((station.rect.width, station.rect.height)),
                    [config.rail_unload_width, config.rail_unload_length],
                )
                for zone in plan.zones:
                    if zone.name != "rail:unload":
                        self.assertFalse(zone.rect.intersects(station.rect), zone.name)

    def test_trunk_reaches_the_boundary_or_says_why(self):
        for seed, (_world, plan) in self.cases.items():
            with self.subTest(seed=seed):
                if plan.zone("rail:unload") is None:
                    continue
                trunk = plan.corridor("rail:trunk")
                if trunk is None:
                    self.assertTrue(any(w.startswith("rail:trunk") for w in plan.warnings), plan.warnings)
                    continue
                self.assertLessEqual(len(trunk.segments), 2)
                self.assertTrue(all(min(s.rect.width, s.rect.height) == 8 for s in trunk.segments))

    def test_district_allocation_never_takes_rail_land(self):
        for seed, (world, plan) in self.cases.items():
            with self.subTest(seed=seed):
                rail = [z.rect for z in plan.zones if z.kind in ("rail", "outpost")]
                rail += list(plan.corridor("rail:trunk").rects) if plan.corridor("rail:trunk") else []
                for index in range(40):
                    try:
                        block = plan.allocate(world, "x", 24, plan.district_depth, name=f"b{index}")
                    except SiteError:
                        break
                    self.assertFalse(any(block.rect.intersects(r) for r in rail), block)
                self.assertEqual(plan.validate(world), [])

    def test_rail_data_round_trips(self):
        _world, plan = self.cases[7]
        again = MasterPlan.from_json(plan.to_json())
        self.assertEqual(again.to_dict(), plan.to_dict())
        self.assertIn("rail:unload", plan.summary())


class RailDirectionTests(unittest.TestCase):
    def test_trunk_leaves_towards_the_western_outposts(self):
        world = west_outpost_world()
        plan = plan_site(world)
        self.assertEqual(plan.validate(world), [])
        trunk = plan.corridor("rail:trunk")
        self.assertIsNotNone(trunk, plan.warnings)
        self.assertEqual(trunk.segments[-1].direction, "W")
        self.assertEqual(trunk.segments[-1].rect.x0, world.bounds().x0)
        names = {z.name for z in plan.zones}
        self.assertIn("mining:iron-ore:1", names)
        self.assertIn("rail:load:iron-ore:1", names)
        station = plan.zone("rail:load:iron-ore:1").rect
        self.assertEqual(sorted((station.width, station.height)), [16, 56])
        self.assertFalse(any(world.resource_at(x, y) for x, y in station.tiles()))

    def test_small_patches_get_no_outpost(self):
        world = west_outpost_world()
        plan = plan_site(world, config=SiteConfig(rail_outpost_min_tiles=10_000))
        self.assertFalse(any(z.kind == "outpost" for z in plan.zones))


if __name__ == "__main__":
    unittest.main()
