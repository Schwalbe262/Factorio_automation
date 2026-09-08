"""Opt-in Lua contract checks on the isolated character QA server.

Set FACTORIO_CHARACTER_INPUT_LIVE_TEST=1 with character-navigation-qa running.
The Lua objects are local mocks; these tests do not move or modify world entities.
"""

import os
from pathlib import Path
import unittest

from factorio_ai.deterministic_game import DeterministicGame, run_config
from factorio_ai.deterministic_navigation import CHARACTER_INPUT_LUA


@unittest.skipUnless(os.environ.get("FACTORIO_CHARACTER_INPUT_LIVE_TEST") == "1",
                     "requires the isolated character QA server")
class CharacterInputLuaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.game = DeterministicGame(run_config(
            runtime=Path("runtime/deterministic/character-navigation-qa"),
            server_port=34216, rcon_port=27031), backend="character")

    def scenario(self, reason, kind="move", prior=0):
        import json
        body = '''
local x={type=''' + json.dumps(kind) + ''',position={x=20,y=20}}
local d={motion={key=helpers.table_to_json(x),kind=x.type,status="blocked",
 reason=''' + json.dumps(reason) + ''',replan_count=''' + str(prior) + '''}}
local a={position={x=1,y=1},prototype={collision_box={},collision_mask={}}}
local f={};local game={tick=100};local calls=0
local s={find_non_colliding_position=function(_,p) return p end,
 request_path=function(spec) calls=calls+1;return calls end}
local function advance()
''' + CHARACTER_INPUT_LUA + '''
end
local first=advance();local count=d.motion.replan_count
if first.ok then d.motion.status="blocked";d.motion.reason="character_path_stalled" end
local second=advance()
if second.ok then d.motion.status="blocked";d.motion.reason="character_path_stalled" end
local third=advance()
return success{first=first,second=second,third=third,requests=calls,
 first_replan_count=count,final_replan_count=d.motion.replan_count,terminal_reason=d.motion.reason}
'''
        result = self.game.query(body)
        self.assertTrue(result.get("ok"), result)
        return result

    def test_walk_replans_from_current_position_only_twice(self):
        result = self.scenario("character_path_stalled")
        self.assertTrue(result["first"]["ok"])
        self.assertTrue(result["second"]["ok"])
        self.assertEqual(result["first_replan_count"], 1)
        self.assertEqual(result["final_replan_count"], 2)
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["third"]["reason"], "character_path_replan_exhausted")
        self.assertEqual(result["terminal_reason"], "character_path_replan_exhausted")

    def test_saved_exhausted_budget_does_not_restart_path_requests(self):
        result = self.scenario("character_path_stalled", prior=2)
        self.assertEqual(result["requests"], 0)
        self.assertEqual(result["third"]["reason"], "character_path_replan_exhausted")

    def test_unreachable_destination_is_not_retried(self):
        result = self.scenario("no_character_path")
        self.assertEqual(result["requests"], 0)
        self.assertEqual(result["third"]["reason"], "no_character_path")

    def test_stalled_mining_cannot_turn_into_a_walk(self):
        result = self.scenario("character_mining_stalled", kind="mine")
        self.assertEqual(result["requests"], 0)
        self.assertEqual(result["third"]["reason"], "character_mining_stalled")
