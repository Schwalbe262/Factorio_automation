"""Trees and rocks never reject or displace a planned site (site.nature)."""

import json
import re
from types import SimpleNamespace
import unittest

from factorio_agent.board import entity
from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.planning import preflight
from factorio_agent.power_layout import V2FactoryBuilder
from factorio_agent.site import nature
from factorio_agent.site.candidates import free_sites
from factorio_agent.site.geometry import Rect
from factorio_agent.site.masterplan import plan_site
from factorio_agent.site.worldmap import WATER, Feature, WorldMap


def blank(half=256):
    world = WorldMap()
    world.mark_surveyed(Rect(-half, -half, half, half))
    return world


def plant(world, rect: Rect, every: int = 1):
    """A dense forest: a tree on every ``every``-th tile of ``rect``."""
    b = world.bounds()
    rect = Rect(max(rect.x0, b.x0), max(rect.y0, b.y0), min(rect.x1, b.x1), min(rect.y1, b.y1))
    for x, y in rect.tiles():
        if (x + y) % every == 0 and not world.features_at(x, y):
            world.add_feature(Feature("tree", "tree-01", Rect(x, y, x + 1, y + 1)))


def corridor_world():
    """Land strip with ore patches at the start, so the bus has one obvious run."""
    world = blank()
    land = Rect(-24, -72, 256, 72)
    for x, y in world.bounds().tiles():
        if not land.contains(x, y):
            world.set_terrain(x, y, WATER)
    for x, y in Rect(-20, 40, -12, 50).tiles():
        world.set_ore(x, y, "iron-ore", 1000)
    for x, y in Rect(-20, -50, -12, -40).tiles():
        world.set_ore(x, y, "copper-ore", 1000)
    return world


class RankingRuleTests(unittest.TestCase):
    def test_position_first_and_trees_only_break_ties(self):
        rows = [(10.0, 0, "far-bare"), (9.0, 500, "near-forest"), (9.0, 3, "near-sparse")]
        self.assertEqual(nature.ranked(rows), ["near-sparse", "near-forest", "far-bare"])

    def test_batches_cover_every_obstacle_once(self):
        obstacles = list(range(250))
        groups = nature.batches(obstacles)
        self.assertEqual([len(g) for g in groups], [96, 96, 58])
        self.assertEqual(sum(groups, []), obstacles)
        with self.assertRaises(ValueError):
            nature.batches(obstacles, 0)


class FreeSiteTests(unittest.TestCase):
    def test_dense_forest_keeps_the_nearest_site(self):
        bare = blank(64)
        forest = blank(64)
        plant(forest, Rect(-64, -64, 64, 64))
        point = [(0.0, 0.0)]
        expected = free_sites(bare, point, 16, 16, limit=4)
        self.assertEqual(free_sites(forest, point, 16, 16, limit=4), expected)

    def test_forest_beyond_any_former_cap_is_not_rejected(self):
        forest = blank(64)
        plant(forest, Rect(-64, -64, 64, 64))
        first = free_sites(forest, [(0.0, 0.0)], 16, 16, limit=1)[0]
        self.assertGreater(len(forest.free_rect(first.x0, first.y0, first.x1, first.y1).clearable), 200)

    def test_water_and_cliff_still_reject(self):
        world = blank(64)
        point = [(0.0, 0.0)]
        nearest = free_sites(world, point, 16, 16, limit=1)[0]
        water = Rect(nearest.x0 + 3, nearest.y0 + 3, nearest.x0 + 4, nearest.y0 + 4)
        world.set_terrain(water.x0, water.y0, WATER)
        moved = free_sites(world, point, 16, 16, limit=1)[0]
        self.assertNotEqual(moved, nearest)
        self.assertFalse(moved.intersects(water))
        cliff = Rect(moved.x0 + 4, moved.y0 + 4, moved.x0 + 6, moved.y0 + 6)
        world.add_feature(Feature("cliff", "cliff", cliff))
        third = free_sites(world, point, 16, 16, limit=1)[0]
        self.assertFalse(third.intersects(water) or third.intersects(cliff))


class MasterPlanTests(unittest.TestCase):
    def test_forest_beside_the_bus_keeps_bus_and_block_slots(self):
        bare = plan_site(corridor_world())
        world = corridor_world()
        bus = bare.bus.rect()
        # Forest over the bus, the bus-side strips and the smelting zone.
        plant(world, Rect(bus.x0 - 40, bus.y0 - 40, bus.x1 + 4, bus.y1 + 40), every=2)
        plant(world, bus)
        forest = plan_site(world)
        self.assertEqual(forest.bus.rect(), bus)
        self.assertEqual(forest.bus.direction, bare.bus.direction)
        self.assertEqual([s.rect for s in forest.strips], [s.rect for s in bare.strips])
        self.assertEqual(forest.validate(world), [])


def tree(i):
    return {"name": "tree-01", "position": {"x": i + 0.5, "y": 0.5}, "unit_number": i + 1}


class ClearingPhaseTests(unittest.TestCase):
    def test_whole_block_check_has_no_tree_cap(self):
        trees = [tree(i) for i in range(300)]
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        game = SimpleNamespace(query=lambda _: {"ok": True, "clear_obstacles": trees})
        result = preflight(game, geometry, {"world_id": "w"}, [entity("transport-belt", 0.5, 0.5, 4)])
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["clear_obstacles"]), 300)

    def test_large_forest_is_cleared_in_paid_batches_then_done(self):
        standing = {i: tree(i) for i in range(250)}
        sizes = []

        def query(lua):
            approved = json.loads(json.loads(re.search(r"json_to_table\((\".*?\")\)", lua, re.S).group(1)))
            sizes.append(len(approved))
            for wanted in approved:
                if wanted["unit_number"] - 1 in standing:
                    return {"ok": True, "obstacle": {"name": wanted["name"], "position": wanted["position"]}}
            return {"ok": True}

        builder = V2FactoryBuilder.__new__(V2FactoryBuilder)
        builder.game = SimpleNamespace(query=query)
        plan = _plan([], [], clear_obstacles=list(standing.values()), clearance_world_id="w")
        mines = 0
        while True:
            step = builder.ensure_plan({"world_id": "w", "tick": 1}, plan)
            if step.get("type") != "mine":
                break
            self.assertEqual(step["count"], 1)
            self.assertIn("clearing batch", step["reason"])
            mines += 1
            standing.pop(int(step["position"]["x"]))
        self.assertEqual(step["status"], "succeeded")
        self.assertEqual(mines, 250)
        self.assertLessEqual(max(sizes), nature.CLEARANCE_BATCH)
        # Every batch is now remembered as cleared: no further clearance query.
        calls = len(sizes)
        self.assertEqual(builder.ensure_plan({"world_id": "w", "tick": 1}, plan)["status"], "succeeded")
        self.assertEqual(len(sizes), calls)


if __name__ == "__main__":
    unittest.main()
