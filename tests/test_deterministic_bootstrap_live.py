"""Opt-in, local Lua mocks on the isolated character QA server; no world writes."""
import os
from pathlib import Path
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_bootstrap import DeterministicBootstrap
from factorio_ai.deterministic_game import DeterministicGame, run_config


@unittest.skipUnless(os.environ.get("FACTORIO_CHARACTER_INPUT_LIVE_TEST") == "1",
                     "requires the isolated character QA server")
class BootstrapSiteLuaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.game = DeterministicGame(run_config(
            runtime=Path("runtime/deterministic/character-navigation-qa"),
            server_port=34216, rcon_port=27031), backend="character")

    def survey(self, blockers, *, terrain_clear=True, backend="character"):
        fake = Mock(backend=backend)
        fake.query.side_effect = [{"ok": True, "cells": []}, {}]
        DeterministicBootstrap(fake).discover_cell("coal", "wooden-chest")
        body = fake.query.call_args.args[0]
        fixture = '''
local a={type="character"};local f={}
local ore={name="coal",type="resource",position={x=.5,y=.5},amount=100}
local boxes={left_top={x=-.9,y=-.9},right_bottom={x=.9,y=.9}}
local prototypes={entity={['burner-mining-drill']={collision_box=boxes},['wooden-chest']={collision_box=boxes}}}
local function target() return nil end
local s={find_entities_filtered=function(spec)
 if spec.type=="resource" or spec.name=="coal" then return {ore} end
 return ''' + blockers + '''
end,can_place_entity=function(spec)
 if spec.build_check_type then return ''' + ("true" if terrain_clear else "false") + ''' end
 return false
end}
'''
        return self.game.query(fixture + body)

    def test_only_automation_actor_can_be_ignored_during_planning(self):
        self.assertTrue(self.survey("{a,ore}")["ok"])

    def test_other_characters_and_wreckage_remain_obstructions(self):
        for blockers in ("{a,{type='character'}}", "{a,{type='simple-entity-with-owner',name='crash-site-spaceship'}}"):
            with self.subTest(blockers=blockers):
                self.assertFalse(self.survey(blockers)["ok"])

    def test_terrain_and_assisted_collision_rules_are_preserved(self):
        self.assertFalse(self.survey("{a}", terrain_clear=False)["ok"])
        self.assertFalse(self.survey("{a}", backend="assisted")["ok"])
