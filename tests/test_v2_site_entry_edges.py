"""Per-feeder smelting entry edges (site.orientation.choose_entry_sides)."""

import unittest

from factorio_agent.site.geometry import Rect
from factorio_agent.site.orientation import choose_entry_sides, route_into_edge
from factorio_agent.site.planmodel import SiteConfig
from factorio_agent.site.planzones import Zone
from factorio_agent.site.tilegrid import Occupancy, TileGrid
from factorio_agent.site.worldmap import WorldMap

# The live seed-2150100594 geometry: coal lies directly north of smelting.
SMELTING = Rect(-88, 52, -24, 100)
MINING = {
    "coal": Rect(-85, 2, -58, 29),
    "copper-ore": Rect(-58, 8, -17, 52),
    "iron-ore": Rect(-17, 33, 28, 81),
}


def live_case():
    world = WorldMap()
    world.mark_surveyed(Rect(-192, -128, 128, 192))
    for material, rect in MINING.items():
        inner = Rect(rect.x0 + 4, rect.y0 + 4, rect.x1 - 4, rect.y1 - 4)
        for x, y in inner.tiles():
            world.set_ore(x, y, material, 1000)
    occupancy = Occupancy(TileGrid(world))
    zones = {m: Zone("mining", "mining:" + m, r) for m, r in MINING.items()}
    occupancy.taken.extend(zone.rect for zone in zones.values())
    occupancy.taken.append(SMELTING)
    sources = [(m, zones[m]) for m in ("iron-ore", "copper-ore", "coal")]
    return occupancy, sources


class LiveGeometryTests(unittest.TestCase):
    def test_single_east_edge_cannot_route_coal(self):
        occupancy, sources = live_case()
        feeders, _total = route_into_edge(occupancy, sources, SMELTING, "E", SiteConfig())
        coal = next(f for f in feeders if f.material == "coal")
        self.assertIsNone(coal.route)

    def test_coal_enters_north_with_a_short_straight_corridor(self):
        occupancy, sources = live_case()
        input_side, feeders = choose_entry_sides(occupancy, SMELTING, "S", sources, SiteConfig())
        by_material = {f.material: f for f in feeders}
        self.assertTrue(all(f.route is not None for f in feeders), feeders)
        coal = by_material["coal"]
        self.assertEqual(coal.to_side, "N")
        self.assertEqual([s.direction for s in coal.route.segments], ["S"])
        self.assertLessEqual(coal.route.length(SiteConfig().corridor_width), 30)
        self.assertNotIn("S", {f.to_side for f in feeders})
        self.assertEqual(by_material["iron-ore"].to_side, "E")
        self.assertIn(input_side, {f.to_side for f in feeders})

    def test_shared_edge_feeders_get_disjoint_entry_bands(self):
        occupancy, sources = live_case()
        _side, feeders = choose_entry_sides(occupancy, SMELTING, "S", sources, SiteConfig())
        north = [f for f in feeders if f.to_side == "N" and f.route is not None]
        bands = sorted(f.route.entry_band for f in north)
        for (_a0, a1), (b0, _b1) in zip(bands, bands[1:]):
            self.assertLessEqual(a1, b0)


class FirstSideMirrorTests(unittest.TestCase):
    def test_first_block_lands_on_the_mirrored_spot(self):
        from site_transforms import map_rect, map_tile, transform_world

        from factorio_agent.site.fakeworld import make_world
        from factorio_agent.site.masterplan import plan_site

        world = make_world(7, size=512)
        twin_world = transform_world(world, "flip_x")
        plan = plan_site(world)
        twin = plan_site(twin_world, start=map_tile("flip_x", (0, 0)))
        block = plan.allocate(world, "x", 24, 16, name="a")
        mirrored = twin.allocate(twin_world, "x", 24, 16, name="a")
        self.assertEqual(mirrored.rect, map_rect("flip_x", block.rect))


if __name__ == "__main__":
    unittest.main()
