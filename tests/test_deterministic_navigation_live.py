"""Opt-in Lua contract checks on the isolated character QA server.

Set FACTORIO_CHARACTER_INPUT_LIVE_TEST=1 with character-navigation-qa running.
The Lua objects are local mocks; these tests do not move or modify world entities.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_game import DeterministicGame, run_config
from factorio_ai.deterministic_navigation import CharacterNavigator, CHARACTER_INPUT_LUA, SCENARIO_INPUT_LUA


@unittest.skipUnless(os.environ.get("FACTORIO_CHARACTER_INPUT_LIVE_TEST") == "1",
                     "requires the isolated character QA server")
class CharacterInputLuaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.game = DeterministicGame(run_config(
            runtime=Path("runtime/deterministic/character-navigation-qa"),
            server_port=34216, rcon_port=27031), backend="character")

    def build_navigator(self, actor, *, build_distance=10, existing=False, reachable=False):
        """Execute navigation Lua with local actors and mocked mutations only."""
        def query(body):
            fixture = '''
local d={};local f={}
local a={position=helpers.json_to_table(''' + json.dumps(json.dumps(actor)) + '''),
 build_distance=''' + str(build_distance) + ''',can_reach_entity=function() return ''' + str(reachable).lower() + ''' end}
a.bounding_box={left_top={x=a.position.x-.2,y=a.position.y-.2},
 right_bottom={x=a.position.x+.2,y=a.position.y+.2}}
local function target() return ''' + ('{type="mining-drill"}' if existing else 'nil') + ''' end
local s={can_place_entity=function(spec)
 return spec.name=="character" or math.abs(a.position.x)>2 or math.abs(a.position.y)>2
end}
'''
            return self.game.query(fixture + body)

        game = SimpleNamespace(backend="character", query=query,
                               act=Mock(return_value={"ok": True, "status": "succeeded"}))
        navigator = CharacterNavigator(game)
        navigator.pending_action = Mock(return_value=None)
        navigator._input = Mock(return_value={"ok": True, "status": "running"})
        return navigator, game

    def test_new_build_uses_actual_reach_including_shorter_than_four_tiles(self):
        for distance, reach, allowed in ((6, 10, True), (10, 10, True), (10.1, 10, False), (3, 2, False)):
            with self.subTest(distance=distance, reach=reach):
                navigator, game = self.build_navigator({"x": distance, "y": 0}, build_distance=reach)
                action = {"type": "build", "name": "electric-mining-drill", "position": {"x": 0, "y": 0}}
                navigator.execute(action, {})
                if allowed:
                    game.act.assert_called_once_with(action)
                    navigator._input.assert_not_called()
                else:
                    game.act.assert_not_called()
                    navigator._input.assert_called_once_with({"type": "move", "position": action["position"]})

    def test_footprint_escape_then_belt_drift_does_not_approach_build_center_again(self):
        actor = {"x": 0, "y": 0}
        navigator, game = self.build_navigator(actor)
        action = {"type": "build", "name": "electric-mining-drill", "position": {"x": 0, "y": 0}}
        navigator.execute(action, {})
        game.act.assert_not_called()
        escape = navigator._input.call_args.args[0]
        self.assertEqual(escape["type"], "move")
        self.assertGreater(escape["position"]["x"], 2)
        # Belts can displace the stopped actor before the next observation.
        actor["x"] = 6.173
        navigator._input.reset_mock()
        navigator.execute(action, {})
        navigator._input.assert_not_called()
        game.act.assert_called_once_with(action)

    def test_actual_build_reach_does_not_relax_existing_entity_interactions(self):
        for kind in ("build", "take", "insert", "mine", "recipe"):
            with self.subTest(kind=kind):
                navigator, game = self.build_navigator({"x": 6, "y": 0}, existing=True, reachable=False)
                action = {"type": kind, "name": "electric-mining-drill", "position": {"x": 0, "y": 0}}
                navigator.execute(action, {})
                game.act.assert_not_called()
                navigator._input.assert_called_once_with({"type": "move", "position": action["position"]})

    def test_actual_build_reach_does_not_relax_absent_nonbuild_targets(self):
        for kind in ("take", "insert", "mine", "recipe"):
            with self.subTest(kind=kind):
                navigator, game = self.build_navigator({"x": 6, "y": 0})
                action = {"type": kind, "name": "electric-mining-drill", "position": {"x": 0, "y": 0}}
                navigator.execute(action, {})
                game.act.assert_not_called()
                navigator._input.assert_called_once_with({"type": "move", "position": action["position"]})

    def test_ground_pickup_walks_inside_pickup_radius_even_when_entity_is_reachable(self):
        for distance in (2.0, 1.0):
            with self.subTest(distance=distance):
                fixture = '''
local d={};local a={position={x=0,y=0},item_pickup_distance=1,can_reach_entity=function() return true end}
local entity={type="item-entity",position={x=''' + str(distance) + ''',y=0}}
local function target() return entity end
'''
                game = SimpleNamespace(backend="character", act=Mock(return_value={"ok": True, "status": "succeeded"}))
                game.query = lambda body: self.game.query(fixture + body)
                navigator = CharacterNavigator(game)
                navigator.pending_action = Mock(return_value=None)
                navigator._input = Mock(return_value={"ok": True, "status": "running"})
                action = {"type": "take", "name": "item-on-ground", "position": {"x": distance, "y": 0},
                          "item": "coal", "quality": "normal", "count": 8}
                result = navigator.execute(action, {})
                if distance > 1:
                    self.assertEqual(result["status"], "running")
                    navigator._input.assert_called_once_with({"type": "move", "position": action["position"]})
                    game.act.assert_not_called()
                else:
                    self.assertEqual(result["status"], "succeeded")
                    navigator._input.assert_not_called()
                    game.act.assert_called_once_with(action)

    def scenario(self, reason, kind="move", prior=0):
        import json
        body = '''
local x={type=''' + json.dumps(kind) + ''',position={x=20,y=20}}
local d={motion={key=helpers.table_to_json(x),kind=x.type,status="blocked",
 reason=''' + json.dumps(reason) + ''',replan_count=''' + str(prior) + '''}}
local a={position={x=1,y=1},prototype={collision_box={},collision_mask={}}}
local f={};local game={tick=100};local calls=0;local flags=nil
local s={find_non_colliding_position=function(_,p) return p end,
 request_path=function(spec) calls=calls+1;flags=spec.pathfind_flags;return calls end}
local function advance()
''' + CHARACTER_INPUT_LUA + '''
end
local first=advance();local count=d.motion.replan_count
if first.ok then d.motion.status="blocked";d.motion.reason="character_path_stalled" end
local second=advance()
if second.ok then d.motion.status="blocked";d.motion.reason="character_path_stalled" end
local third=advance()
return success{first=first,second=second,third=third,requests=calls,flags=flags,
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
        self.assertEqual(result["flags"], {"cache": False, "allow_destroy_friendly_entities": False,
                                         "allow_paths_through_own_entities": False})

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

    def test_source_upgrade_guard_rejects_stale_world_and_missing_target_before_mining(self):
        import json
        for world, reason in (("different-world", "source_upgrade_world_changed"),
                              ("expected-world", "source_upgrade_target_changed")):
            with self.subTest(world=world):
                body = '''
local x={type="mine",name="burner-mining-drill",position={x=1,y=1},count=1,
 expected_world_id="expected-world",expected_unit_number=77,required_replacement_item="electric-mining-drill",
 exhausted_source_receiver={name="wooden-chest",position={x=1,y=-1}}}
local d={world_id=''' + json.dumps(world) + '''}
local a={valid=true,get_main_inventory=function() return {} end}
local function target() return nil end
local function attempt()
''' + CHARACTER_INPUT_LUA + '''
end
local result=attempt();result.mining=a.mining_state.mining;return result
'''
                result = self.game.query(body)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["reason"], reason)
                self.assertFalse(result["mining"])

    def test_belt_oscillation_does_not_count_as_path_progress(self):
        result = self.path_progress("tick%2+1")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "character_path_stalled")
        self.assertFalse(result["walking"])

    def test_steady_approach_to_waypoint_preserves_active_path(self):
        result = self.path_progress("tick*7/400")
        self.assertEqual(result["status"], "running")
        self.assertGreater(result["last_progress"], 390)
        self.assertTrue(result["walking"])

    def test_vertical_path_corrects_belt_drift_while_walking_past_obstacle(self):
        for actor_x, direction in ((2.62890625, 14), (2.37109375, 2)):
            with self.subTest(actor_x=actor_x):
                result = self.path_direction({"x": actor_x, "y": 0.40234375},
                                             {"x": 2.5, "y": 0.5}, {"x": 2.5, "y": -0.5})
                self.assertEqual(result["direction"], direction)
                self.assertTrue(result["walking"])

    def test_horizontal_path_corrects_belt_drift_on_either_side(self):
        for actor_y, direction in ((2.62890625, 14), (2.37109375, 10)):
            with self.subTest(actor_y=actor_y):
                result = self.path_direction({"x": 0.40234375, "y": actor_y},
                                             {"x": 0.5, "y": 2.5}, {"x": -0.5, "y": 2.5})
                self.assertEqual(result["direction"], direction)

    def test_centered_straight_and_diagonal_paths_keep_their_heading(self):
        for previous, destination, direction in (({"x": 0, "y": 1}, {"x": 0, "y": -1}, 0),
                                                  ({"x": -1, "y": 1}, {"x": 1, "y": -1}, 2)):
            with self.subTest(destination=destination):
                result = self.path_direction({"x": 0, "y": 0}, previous, destination)
                self.assertEqual(result["direction"], direction)

    def test_lateral_step_overshoot_keeps_forward_progress(self):
        body = '''
local actor={valid=true,position={x=12.57421875,y=-41.5}}
local motion={kind="move",status="running",expires_tick=1000,last_progress_tick=0,
 path={{position={x=12.5,y=-41.5}},{position={x=12.5,y=-42.5}}},next_waypoint=2}
local storage={deterministic_player={actor=actor,motion=motion}}
local handlers={};local _G={};local game={tick=0}
local script={get_event_handler=function() return nil end,on_event=function(id,fn) handlers[id]=fn end}
''' + SCENARIO_INPUT_LUA + '''
for tick=1,20 do
 game.tick=tick;handlers[defines.events.on_tick]{tick=tick}
 if actor.walking_state.walking then
  local angle=(actor.walking_state.direction-4)*math.pi/8
  actor.position.x=actor.position.x+math.cos(angle)*0.1484375
  actor.position.y=actor.position.y+math.sin(angle)*0.1484375
 end
end
return success{status=motion.status,position=actor.position}
'''
        result = self.game.query(body)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["status"], "succeeded")
        self.assertLess(result["position"]["y"], -42.1)

    def path_direction(self, position, previous, destination):
        import json
        payload = json.dumps(json.dumps({"position": position, "previous": previous, "destination": destination}))
        body = '''
local fixture=helpers.json_to_table(''' + payload + ''')
local actor={valid=true,position=fixture.position}
local motion={kind="move",status="running",expires_tick=1000,last_progress_tick=0,
 path={{position=fixture.previous},{position=fixture.destination}},next_waypoint=2}
local storage={deterministic_player={actor=actor,motion=motion}}
local handlers={};local _G={};local game={tick=1}
local script={get_event_handler=function() return nil end,on_event=function(id,fn) handlers[id]=fn end}
''' + SCENARIO_INPUT_LUA + '''
handlers[defines.events.on_tick]{tick=1}
return success{status=motion.status,walking=actor.walking_state.walking,direction=actor.walking_state.direction}
'''
        result = self.game.query(body)
        self.assertTrue(result.get("ok"), result)
        return result

    def path_progress(self, position_expression):
        body = '''
local actor={valid=true,position={x=0,y=0}}
local motion={kind="move",status="running",expires_tick=1000,last_progress_tick=0,
 path={{position={x=10,y=0}}},next_waypoint=1}
local storage={deterministic_player={actor=actor,motion=motion}}
local handlers={};local _G={};local game={tick=0}
local script={get_event_handler=function() return nil end,on_event=function(id,fn) handlers[id]=fn end}
''' + SCENARIO_INPUT_LUA + '''
for tick=1,400 do
 game.tick=tick;actor.position.x=''' + position_expression + '''
 handlers[defines.events.on_tick]{tick=tick}
end
return success{status=motion.status,reason=motion.reason,last_progress=motion.last_progress_tick,
 walking=actor.walking_state.walking}
'''
        return self.game.query(body)
