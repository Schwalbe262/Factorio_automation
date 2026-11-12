import unittest

from factorio_agent.site.fakeworld import make_world
from factorio_agent.site.geometry import Rect
from factorio_agent.site.survey import SURVEY_LUA, SurveyError, merge_reply, survey_area, survey_body, update
from factorio_agent.site.worldmap import UNBUILDABLE, WATER, Feature, WorldMap

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None

SLICE = Rect(0, 0, 32, 32)


def reply(**fields):
    base = {"ok": True, "names": {}, "terrain": {}, "ore": {}, "amounts": {}, "wells": {}, "features": {}}
    base.update(fields)
    return base


class EmptyGame:
    def __init__(self):
        self.bodies = []

    def query(self, body):
        self.bodies.append(body)
        return reply()


class MergeReplyTests(unittest.TestCase):
    def test_runs_names_and_features_decode_into_the_map(self):
        world = WorldMap()
        merge_reply(
            world,
            SLICE,
            reply(
                names=["iron-ore", "tree-01", "crude-oil"],
                terrain=[[2, 3, 4, 1], [5, 0, 2, 2]],
                ore=[[10, 10, 3, 1]],
                amounts=[100, 200, 300],
                wells=[[20, 20, 3, 300000, 3]],
                features=[[1, 2, 7, 7, 1, 1]],
            ),
        )
        self.assertEqual(world.terrain(6, 2), WATER)
        self.assertEqual(world.terrain(7, 2), "land")
        self.assertEqual(world.terrain(1, 5), UNBUILDABLE)
        self.assertEqual(world.resource_at(11, 10), ("iron-ore", 200))
        self.assertEqual(world.resource_at(19, 21), ("crude-oil", 300000))
        self.assertEqual(world.features_at(7, 7)[0].name, "tree-01")
        self.assertTrue(world.is_surveyed(31, 31))

    def test_bad_replies_raise(self):
        with self.assertRaises(SurveyError):
            merge_reply(WorldMap(), SLICE, {"ok": False, "reason": "boom"})
        with self.assertRaises(SurveyError):
            merge_reply(WorldMap(), SLICE, reply(names=["coal"], ore=[[0, 0, 2, 1]], amounts=[5]))
        with self.assertRaises(SurveyError):
            merge_reply(WorldMap(), SLICE, reply(terrain=[[40, 0, 1, 1]]))
        with self.assertRaises(SurveyError):
            merge_reply(WorldMap(), SLICE, reply(names=["tree-01"], features=[[1, 2, 0, 0, 1, 1]]))

    def test_adjacent_slices_merge_without_duplicates_and_resurvey_replaces(self):
        world = WorldMap()
        cliff = [4, 1, 29, 4, 4, 2]
        merge_reply(world, Rect(0, 0, 32, 32), reply(names=["cliff"], features=[cliff]))
        merge_reply(world, Rect(32, 0, 64, 32), reply(names=["cliff"], features=[[4, 1, 33, 9, 2, 2]]))
        self.assertEqual(len(world.features("cliff")), 2)
        self.assertEqual(world.features_at(32, 4)[0].rect, Rect(29, 4, 33, 6))
        world.mark_occupied(Rect(1, 1, 3, 3), "planned-block")
        merge_reply(world, Rect(0, 0, 32, 32), reply(names=["cliff"], features=[cliff]))
        self.assertEqual(len(world.features("cliff")), 2)
        merge_reply(world, Rect(32, 0, 64, 32), reply())
        self.assertEqual([f.rect for f in world.features("cliff")], [Rect(29, 4, 33, 6)])
        self.assertEqual(world.occupied_by(1, 1), "planned-block")

    def test_resurvey_drops_removed_ore_and_water(self):
        world = WorldMap()
        merge_reply(world, SLICE, reply(names=["coal"], ore=[[0, 0, 1, 1]], amounts=[9], terrain=[[1, 1, 1, 1]]))
        merge_reply(world, SLICE, reply())
        self.assertIsNone(world.resource_at(0, 0))
        self.assertEqual(world.terrain(1, 1), "land")


class SurveyDriverTests(unittest.TestCase):
    def test_area_is_chunk_aligned_and_cut_into_bounded_slices(self):
        game = EmptyGame()
        result = survey_area(game, (-100, -100, 100, 100))
        self.assertEqual(result.queries, 9)
        self.assertEqual(len(game.bodies), 9)
        self.assertTrue(all(r.width <= 96 and r.height <= 96 for r in result.slices))
        self.assertTrue(result.world.is_surveyed(-128, 127))
        self.assertFalse(result.world.is_surveyed(128, 0))
        self.assertGreaterEqual(result.seconds, 0.0)
        self.assertGreater(result.max_reply_bytes, 0)
        self.assertIn("local x0,y0,x1,y1=-128,-128,-32,-32", game.bodies[0])

    def test_rejects_slice_sizes_off_the_chunk_grid(self):
        with self.assertRaises(ValueError):
            update(WorldMap(), EmptyGame(), (0, 0, 10, 10), chunk_tiles=50)

    def test_lua_is_read_only_and_one_line_safe(self):
        self.assertNotIn("--", SURVEY_LUA)
        for word in ("destroy", "create_entity", "set_tiles", "order_deconstruction", "mine("):
            self.assertNotIn(word, SURVEY_LUA)
        self.assertNotIn("__AREA__", survey_body(SLICE))


@unittest.skipUnless(LuaRuntime, "Lua runtime required")
class LuaSurveyTests(unittest.TestCase):
    def game(self, world):
        from factorio_agent.site.fakegame import FakeSurveyGame

        return FakeSurveyGame(world)

    def test_fake_world_survives_a_full_lua_survey(self):
        world = make_world(11, size=192)
        world.add_feature(Feature("entity", "assembling-machine-1", Rect(94, 10, 97, 13)))
        game = self.game(world)
        result = survey_area(game, world.bounds(), chunk_tiles=64)
        self.assertEqual(result.queries, 9)
        self.assertEqual(game.queries, 9)
        self.assertEqual(result.world.to_json(), world.to_json())
        self.assertLess(result.max_reply_bytes, 200_000)

    def test_hand_built_entities_are_classified_and_owned_by_one_slice(self):
        game = self.game(WorldMap())
        add_tile = game.lua.globals().add_tile
        add_entity = game.lua.globals().add_entity
        add_tile("water", 3, 4)
        add_tile("out-of-map", 5, 4)
        add_tile("grass-1", 6, 4)
        add_entity("tree-01", "tree", True, 31.9, 2.5, 31.6, 2.2, 32.3, 2.8, None, True)
        add_entity("huge-rock", "simple-entity", True, 10, 10, 9, 9, 11, 11, None, True)
        add_entity("rock-wall", "simple-entity", True, 14.5, 10.5, 14.1, 10.1, 14.9, 10.9, None, False)
        add_entity("crude-oil", "resource", True, 40.5, 8.5, 39.1, 7.1, 41.9, 9.9, 450000, True)
        add_entity("iron-ore", "resource", True, 2.5, 2.5, 2.4, 2.4, 2.6, 2.6, 777, True)
        add_entity("entity-ghost", "entity-ghost", False, 20.5, 20.5, 20.1, 20.1, 20.9, 20.9, None, True)
        add_entity("transport-belt", "transport-belt", False, 21.5, 20.5, 21.1, 20.1, 21.9, 20.9, None, True)
        result = survey_area(game, (0, 0, 64, 32), chunk_tiles=32)
        world = result.world
        self.assertEqual(result.queries, 2)
        self.assertEqual(world.terrain(3, 4), WATER)
        self.assertEqual(world.terrain(5, 4), UNBUILDABLE)
        self.assertEqual(world.terrain(6, 4), "land")
        self.assertEqual([f.rect for f in world.features("tree")], [Rect(31, 2, 33, 3)])
        self.assertEqual(world.features("rock")[0].rect, Rect(9, 9, 11, 11))
        self.assertEqual(world.features("fixed")[0].name, "rock-wall")
        self.assertEqual([(w.centre, w.size, w.amount) for w in world.wells()], [((40, 8), 3, 450000)])
        self.assertEqual(world.resource_at(2, 2), ("iron-ore", 777))
        self.assertEqual([f.name for f in world.features("entity")], ["transport-belt"])
        self.assertIsNone(world.occupied_by(0, 0))


if __name__ == "__main__":
    unittest.main()
