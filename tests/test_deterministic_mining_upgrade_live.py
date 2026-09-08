"""Read-only Lua-local fixtures using live prototypes on the isolated QA server."""
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_bootstrap import DeterministicBootstrap
from factorio_ai.deterministic_game import DeterministicGame, run_config
from factorio_ai.deterministic_mining_upgrade import _fuel_intake_survey, _survey


@unittest.skipUnless(os.environ.get("FACTORIO_CHARACTER_INPUT_LIVE_TEST") == "1", "requires isolated QA RCON")
class MiningUpgradeLuaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.game = DeterministicGame(run_config(runtime=Path("runtime/deterministic/character-navigation-qa"),
                                               server_port=34216, rcon_port=27031))

    def survey(self, *, terrain=True, mixed=False, foreign=False, ground=False):
        fake = SimpleNamespace(query=Mock(return_value={}))
        record = {"old_drill": {"name": "burner-mining-drill", "position": {"x": 10, "y": 12}},
                  "receiver": {"name": "wooden-chest", "position": {"x": 10.5, "y": 10.5}}, "resource": "coal"}
        _survey(SimpleNamespace(game=fake), record, choose=True)
        body = fake.query.call_args.args[0]
        fixture = '''
local f={};local d={world_id="local-fixture"}
local receiver={name="wooden-chest",force=f,position={x=10.5,y=10.5},prototype=prototypes.entity["wooden-chest"]}
local old={name="burner-mining-drill",position={x=10,y=12},drop_position=receiver.position,
 prototype=prototypes.entity["burner-mining-drill"],force=f,burner={},minable=true,unit_number=123,
 status=defines.entity_status.no_minable_resources}
local ore={name="coal",position={x=12.5,y=14.5},amount=77,type="resource"}
local pole={name="small-electric-pole",position={x=12.5,y=13.5},prototype=prototypes.entity["small-electric-pole"],
 electric_network_id=7,quality="normal"}
local function target(p,name)
 if name==receiver.name and p.x==receiver.position.x and p.y==receiver.position.y then return receiver end
 if name==old.name and p.x==old.position.x and p.y==old.position.y then return old end
end
local s={find_entities_filtered=function(spec)
 if spec.type=="generator" then return {{electric_network_id=7}} end
 if spec.type=="electric-pole" then return {pole} end
 if spec.type=="resource" then return {ore''' + (', {name="copper-ore",position={x=11.5,y=14.5},amount=33}' if mixed else '') + '''} end
 return {old''' + (', {name="transport-belt",type="transport-belt"}' if foreign else '') + (
    ', {name="item-on-ground",type="item-entity",position={x=11.3,y=11.5},stack={valid_for_read=true,name="coal",count=7,quality={name="uncommon"}}}'
    if ground else '') + '''}
end, get_tile=function() return {collides_with=function() return ''' + ("false" if terrain else "true") + ''' end} end,
can_place_entity=function() return false end}
'''
        return self.game.query(fixture + body)

    def test_live_rotated_output_and_lattice_geometry_find_same_receiver(self):
        result = self.survey()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["old"]["remaining"], 0)
        candidates = result["candidates"]
        self.assertEqual(len(candidates), 4)
        row = next(row for row in candidates if row["drill"]["direction"] == 0)
        self.assertEqual(row["drill"]["position"], {"x": 10.5, "y": 12.5})
        self.assertEqual(row["remaining"], 77)
        self.assertTrue(row["terrain_clear"])
        self.assertFalse(row["blocked"])
        self.assertEqual(row["power"]["reach"], 2.5)
        self.assertFalse(row["can_place"])

    def test_protected_collisions_water_and_mixed_resources_remain_rejected(self):
        for kwargs, field, expected in (({"foreign": True}, "blocked", True),
                                        ({"terrain": False}, "terrain_clear", False),
                                        ({"mixed": True}, "mixed", True)):
            with self.subTest(kwargs=kwargs):
                result = self.survey(**kwargs)
                self.assertTrue(result["ok"], result)
                row = next(row for row in result["candidates"] if row["drill"]["direction"] == 0)
                self.assertEqual(row[field], expected)

    def test_ground_stack_metadata_is_pickable_without_hiding_structural_obstacles(self):
        for foreign in (False, True):
            result = self.survey(ground=True, foreign=foreign)
            self.assertTrue(result["ok"], result)
            row = next(row for row in result["candidates"] if row["drill"]["direction"] == 0)
            self.assertEqual(row["blocked"], foreign)
            self.assertEqual(row["ground_items"], [{"name": "item-on-ground", "position": {"x": 11.3, "y": 11.5},
                                                   "item": "coal", "quality": "uncommon", "count": 7}])
            self.assertFalse(row["can_place"])

    def test_fuel_intake_proof_uses_actual_drop_inside_old_live_prototype_footprint(self):
        fake = SimpleNamespace(query=Mock(return_value={}))
        record = {"old_drill": {"name": "burner-mining-drill", "position": {"x": 10, "y": 12}, "direction": 0}}
        planned = [{"name": "inserter", "position": {"x": 11.5, "y": 11.5}, "direction": 4}]
        _fuel_intake_survey(SimpleNamespace(game=fake), record, planned)
        body = fake.query.call_args.args[0]
        for drop, expected in (("{x=10.3,y=11.5}", True), ("{x=12.5,y=11.5}", False)):
            fixture = '''
local f={};local d={world_id="local-fixture"}
local function target()
 return {type="inserter",force=f,minable=true,unit_number=777,direction=4,energy=1,
  is_connected_to_electric_network=function() return true end,drop_position=''' + drop + '''}
end
'''
            result = self.game.query(fixture + body)
            self.assertTrue(result["ok"], result)
            row = result["inserters"][0]
            self.assertEqual(row["feeds_old_drill"], expected)
            self.assertEqual(row["unit_number"], 777)
            self.assertTrue(row["owned"])
            self.assertTrue(row["powered"])

    def test_idle_electric_discovery_sees_diagonal_ore_outside_old_radius(self):
        fake = SimpleNamespace(query=Mock(return_value={}))
        DeterministicBootstrap(fake)._existing_cells("coal")
        body = fake.query.call_args.args[0]
        fixture = '''
local f={}
local receiver={name="wooden-chest",type="container",position={x=10.5,y=10.5},prototype=prototypes.entity["wooden-chest"]}
local drill={name="electric-mining-drill",position={x=10.5,y=12.5},drop_position={x=10.5,y=10.65},direction=0,
 prototype=prototypes.entity["electric-mining-drill"],energy=1,get_fuel_inventory=function() return nil end,
 is_connected_to_electric_network=function() return true end}
local s={find_entities_filtered=function(spec)
 if spec.type=="mining-drill" then return {drill} end
 if spec.type=="resource" then return {{name="coal",position={x=12.5,y=14.5},amount=77}} end
 return {receiver}
end}
'''
        result = self.game.query(fixture + body)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["cells"]), 1)
        cell = result["cells"][0]
        self.assertTrue(cell["electric"])
        self.assertTrue(cell["operating"])
        self.assertEqual(cell["remaining"], 77)
