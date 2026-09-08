"""Opt-in local Lua collision mocks; the fixture world is never mutated."""
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_game import DeterministicGame, run_config


@unittest.skipUnless(os.environ.get("FACTORIO_CHARACTER_INPUT_LIVE_TEST") == "1", "requires isolated QA RCON")
class RouteClearanceLuaTests(unittest.TestCase):
    def result(self, entities, *, terrain=True):
        game = DeterministicGame(run_config(runtime=Path("runtime/deterministic/character-navigation-qa"),
                                           server_port=34216, rcon_port=27031))
        fake = SimpleNamespace(cfg=game.cfg, query=Mock(), backend="assisted")
        # Shadow surface/prototypes only inside this RCON command, never storage.
        fixture = '''
local prototypes={entity={['transport-belt']={collision_box={left_top={x=-.4,y=-.4},right_bottom={x=.4,y=.4}}}}}
local s={can_place_entity=function(spec)
 if spec.position.y~=.5 then return false end
 if spec.position.x~=1.5 then return true end
 return spec.forced and ''' + ("true" if terrain else "false") + ''' or false
end,find_entities_filtered=function(spec)
 if spec.force then return {} end
 return ''' + entities + '''
end}
'''
        fake.query.side_effect = lambda body: game.query(fixture + body)
        builder = FactoryBuilder(fake, Mock(), SimpleNamespace(entities={}))
        return builder.clear_route_obstacle({"x": .5, "y": .5}, {"x": 2.5, "y": .5}, [], margin=1)

    def test_neutral_mineable_rock_and_tree_can_open_a_proven_route(self):
        for name, kind in (("big-rock", "simple-entity"), ("tree-01", "tree")):
            entity = "{{name='%s',type='%s',force={name='neutral'},minable=true,position={x=1.3,y=.5}},{type='resource'}}" % (name, kind)
            with self.subTest(name=name):
                result = self.result(entity)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["action"]["name"], name)

    def test_wreckage_structures_characters_and_unmineable_entities_remain_obstacles(self):
        for name, kind, force, minable in (
                ("crash-site-spaceship-wreck-small-1", "simple-entity", "neutral", "true"),
                ("transport-belt", "transport-belt", "player", "true"),
                ("character", "character", "player", "true"),
                ("big-rock", "simple-entity", "enemy", "true"),
                ("big-rock", "simple-entity", "neutral", "false")):
            entity = "{{name='%s',type='%s',force={name='%s'},minable=%s,position={x=1.3,y=.5}}}" % (name, kind, force, minable)
            with self.subTest(name=name, force=force, minable=minable):
                result = self.result(entity)
                self.assertFalse(result["ok"], result)
                self.assertNotIn("action", result)

    def test_water_under_a_clearable_rock_still_closes_the_route(self):
        result = self.result("{{name='big-rock',type='simple-entity',force={name='neutral'},minable=true,position={x=1.3,y=.5}}}", terrain=False)
        self.assertFalse(result["ok"], result)
