"""Local Lua mocks on the isolated fixture server, never world mutations."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from factorio_ai.deterministic_game import DeterministicGame, run_config


@unittest.skipUnless(os.environ.get("FACTORIO_CHARACTER_INPUT_LIVE_TEST") == "1", "requires isolated fixture RCON")
class SourceMineGuardLuaTests(unittest.TestCase):
    def run_guard(self, *, world="guard-world", unit=77, stock=1, ore=0, drop_y=.5, owned=True, receiver=True, identity_only=False):
        live = DeterministicGame(run_config(runtime=Path("runtime/deterministic/factory-fixture"), server_port=34214, rcon_port=27029))
        fixture = '''
local mines=0;local f={};local d={world_id="''' + world + '''"}
local inv={get_item_count=function(name) return ''' + str(stock) + ''' end}
local a={valid=true,get_main_inventory=function() return inv end,
 mine_entity=function(e) mines=mines+1;return true end}
local e={valid=true,name="burner-mining-drill",type="mining-drill",unit_number=''' + str(unit) + ''',
 position={x=.5,y=2.5},drop_position={x=.5,y=''' + str(drop_y) + '''},
 force=''' + ("f" if owned else "{}") + ''',prototype={mining_drill_radius=.99}}
local receiver={force=f,type="container",bounding_box={left_top={x=.15,y=.15},right_bottom={x=.85,y=.85}}}
local s={find_entities_filtered=function(spec) return {{amount=''' + str(ore) + '''}} end}
local function target(p,name) if name=="burner-mining-drill" then return e end;return ''' + ("receiver" if receiver else "nil") + ''' end
'''
        action = {"type": "mine", "name": "burner-mining-drill", "position": {"x": .5, "y": 2.5}, "count": 1,
            "expected_world_id": "guard-world", "expected_unit_number": 77,
            "exhausted_source_receiver": {"name": "wooden-chest", "position": {"x": .5, "y": .5}},
            "required_replacement_item": "electric-mining-drill"}
        if identity_only:
            action = {key: value for key, value in action.items() if key in {"type", "name", "position", "count"}}
            action.update(expected_entity_unit=77, expected_entity_world_id="guard-world")
        with TemporaryDirectory() as temp:
            game = DeterministicGame(run_config(runtime=Path(temp)))
            def query(body):
                return live.query(fixture + "local function attempt() " + body + " end;local result=attempt();result.mock_mines=mines;return result")
            with patch.object(game, "query", side_effect=query):
                return game.act(action)

    def test_guarded_depleted_drill_uses_the_normal_mining_operation_once(self):
        result = self.run_guard()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["mock_mines"], 1)

    def test_owned_entity_guard_rechecks_exact_identity_world_and_force(self):
        self.assertEqual(self.run_guard(identity_only=True)["mock_mines"], 1)
        for change, reason in (({"world": "different"}, "owned_mine_world_changed"),
                               ({"unit": 78}, "owned_mine_target_changed"), ({"owned": False}, "owned_mine_target_changed")):
            with self.subTest(change=change):
                result = self.run_guard(identity_only=True, **change)
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["mock_mines"], 0)

    def test_changed_world_target_receiver_resource_or_inventory_never_mines(self):
        for changes, reason in (({"world": "different"}, "world_changed"), ({"unit": 78}, "target_changed"),
                ({"owned": False}, "target_changed"), ({"receiver": False}, "receiver_changed"),
                ({"drop_y": 1.5}, "receiver_changed"), ({"ore": 1}, "drill_not_exhausted"),
                ({"stock": 0}, "replacement_missing")):
            with self.subTest(changes=changes):
                result = self.run_guard(**changes)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["reason"], "source_upgrade_" + reason)
                self.assertEqual(result["mock_mines"], 0)


@unittest.skipUnless(os.environ.get("FACTORIO_CHARACTER_INPUT_LIVE_TEST") == "1", "requires isolated fixture RCON")
class GroundPickupLuaTests(unittest.TestCase):
    def pickup(self, capacity, *, count=5, backend="assisted", distance=0, expected_quality="rare", item_type="item"):
        live = DeterministicGame(run_config(runtime=Path("runtime/deterministic/factory-fixture"), server_port=34214, rcon_port=27029))
        fixture = '''
local inserted=0;local quality=nil;local destroyed=false
local stack={valid_for_read=true,name="coal",quality={name="rare"},prototype={type="''' + item_type + '''"},count=5}
local inv={insert=function(spec) inserted=math.min(spec.count,''' + str(capacity) + ''');quality=spec.quality;return inserted end}
local a={valid=true,position={x=0,y=0},item_pickup_distance=1,
 get_main_inventory=function() return inv end,can_reach_entity=function() return true end}
local e={name="item-on-ground",type="item-entity",position={x=''' + str(distance) + ''',y=0},stack=stack,
 destroy=function() destroyed=true;stack.count=0 end}
local function target() return e end
'''
        with TemporaryDirectory() as temp:
            game = DeterministicGame(run_config(runtime=Path(temp)), backend=backend)
            def query(body):
                return live.query(fixture + "local function attempt() " + body +
                    " end;local result=attempt();result.inserted=inserted;result.remaining=stack.count;result.destroyed=destroyed;result.inserted_quality=quality;return result")
            with patch.object(game, "query", side_effect=query):
                return game.act({"type": "take", "name": "item-on-ground", "position": {"x": distance, "y": 0},
                                 "item": "coal", "quality": expected_quality, "count": count})

    def test_pickup_conserves_partial_full_and_requested_stack_counts_and_quality(self):
        for capacity, count, expected in ((0, 5, 0), (2, 5, 2), (10, 5, 5), (10, 1, 1)):
            with self.subTest(capacity=capacity, count=count):
                result = self.pickup(capacity, count=count)
                self.assertTrue(result["ok"], result)
                self.assertEqual((result["inserted"], result["remaining"]), (expected, 5 - expected))
                self.assertEqual(result["destroyed"], expected == 5)
                self.assertEqual(result["inserted_quality"], "rare")

    def test_strict_pickup_requires_actual_pickup_distance_and_matching_quality(self):
        result = self.pickup(10, backend="character", distance=2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "out_of_pickup_reach")
        self.assertEqual((result["inserted"], result["remaining"]), (0, 5))
        self.assertTrue(self.pickup(10, backend="character", distance=.5)["ok"])
        result = self.pickup(10, expected_quality="normal")
        self.assertEqual(result["reason"], "ground_item_quality_changed")
        self.assertEqual((result["inserted"], result["remaining"]), (0, 5))
        result = self.pickup(10, item_type="ammo")
        self.assertEqual(result["reason"], "ground_stack_metadata_unsupported")
        self.assertEqual((result["inserted"], result["remaining"]), (0, 5))
