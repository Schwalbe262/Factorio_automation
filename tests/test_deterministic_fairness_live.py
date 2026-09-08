"""Opt-in execution of the safety Lua using only local tables on an existing server."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from factorio_ai import deterministic_game
from factorio_ai.deterministic_game import DeterministicGame, run_config
from factorio_ai.deterministic_routine_fairness import SAFETY_LUA


SHADOW = '''
local storage=nil;local game={tick=100};local o=helpers.json_to_table(OPTIONS)
local f={};local foreign={};local s={};local d={world_id=o.world};local defines={inventory={turret_ammo=1}}
local a={valid=true,unit_number=o.actor,force=o.foreign_actor and foreign or f,surface=s,
 position={x=o.actor_x,y=0},health=o.actor_health,max_health=250}
if o.foreign_surface then a.surface={} end
local lab={valid=true,name="lab",unit_number=1,position={x=260,y=0},force=f,health=o.asset_health,max_health=200}
local turret={valid=true,name="gun-turret",unit_number=o.turret_unit,position={x=260,y=5},force=f,
 direction=o.turret_direction,health=o.turret_health,max_health=400,
 get_inventory=function() return {get_item_count=function() return o.ammo end} end}
local arm={valid=true,type="inserter",name="inserter",unit_number=o.arm_unit,position={x=260,y=4},
 force=f,direction=o.arm_direction,health=150,max_health=150,energy=o.arm_energy,
 is_connected_to_electric_network=function() return o.arm_connected end}
local belt={valid=true,type="transport-belt",name="transport-belt",unit_number=4,position={x=260,y=3},
 force=f,direction=o.belt_direction,health=150,max_health=150,
 get_transport_line=function() return {get_contents=function() return {{name=o.cargo,count=1}} end} end}
if o.foreign_asset then lab.force=foreign end
local all={lab,turret,arm,belt};local surveyed={}
local function target(p,name)
 for _,e in ipairs(all) do if e.name==name and e.position.x==p.x and e.position.y==p.y
  and not (o.arm_missing and e==arm) and not (o.turret_missing and e==turret) then return e end end
end
s.find_entities_filtered=function() return o.extra_asset and {lab,turret,lab} or {lab,turret} end
s.count_entities_filtered=function(args)
 assert(args.radius==48 and args.force=="enemy")
 assert(args.type[1]=="unit" and args.type[2]=="unit-spawner" and args.type[3]=="turret")
 surveyed[#surveyed+1]={x=args.position.x,y=args.position.y}
 return o.enemy_x and (args.position.x-o.enemy_x)^2+args.position.y^2<=48^2 and 1 or 0
end
local function guard()
BODY
end
return {result=guard(),surveyed=surveyed}
'''


@unittest.skipUnless(os.environ.get("FACTORIO_FAIRNESS_LIVE_TEST") == "1", "requires explicit opt-in and existing RCON server")
class FairnessLuaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = DeterministicGame(run_config(runtime=Path("runtime/deterministic/factory-fixture"),
            server_port=34214, rcon_port=int(os.environ.get("FACTORIO_FAIRNESS_RCON_PORT", "27029"))))

    def guard(self, **changes):
        options = dict(world="world", actor=58, actor_x=180, actor_health=250, asset_health=200,
                       ammo=10, arm_unit=3, arm_direction=0, arm_energy=268, arm_connected=True,
                       cargo="firearm-magazine", turret_unit=2, turret_direction=0, turret_health=400, belt_direction=0)
        options.update(changes)
        payload = {"world_id": "world", "actor": 58, "names": ["lab", "gun-turret"],
                   "assets": [{"name": "lab", "unit_number": 1, "position": {"x": 260, "y": 0}},
                              {"name": "gun-turret", "unit_number": 2, "position": {"x": 260, "y": 5}}],
                   "routes": [{"name": "gun-turret", "unit_number": 2, "direction": 0, "position": {"x": 260, "y": 5}},
                              {"name": "inserter", "unit_number": 3, "direction": 0, "position": {"x": 260, "y": 4}},
                              {"name": "transport-belt", "unit_number": 4, "direction": 0,
                               "ammunition": True, "position": {"x": 260, "y": 3}}]}
        body = SAFETY_LUA.replace("PAYLOAD", json.dumps(json.dumps(payload)))
        script = SHADOW.replace("OPTIONS", json.dumps(json.dumps(options))).replace("BODY", body)
        with patch.object(deterministic_game, "_HELPERS", ""):
            result = self.live.query(script)
        self.assertIn("result", result, result)
        return result

    def test_current_actor_and_remote_assets_are_surveyed_without_origin_assumption(self):
        observed = self.guard()
        self.assertTrue(observed["result"]["routes_ready"])
        self.assertEqual(observed["surveyed"], [{"x": 180, "y": 0}, {"x": 260, "y": 0}, {"x": 260, "y": 5}])
        self.assertTrue(self.guard(actor_x=170)["result"]["quiet"])
        for enemy in (170, 260, 308):
            with self.subTest(enemy=enemy):
                result = self.guard(enemy_x=enemy)["result"]
                self.assertEqual(result["reason"], "nearby_enemy")
                self.assertFalse(result["quiet"])

    def test_exact_actor_and_asset_identity_changes_fail_closed(self):
        for changes in ({"world": "other"}, {"actor": 59}, {"foreign_actor": True},
                        {"foreign_surface": True}, {"foreign_asset": True}, {"extra_asset": True}):
            with self.subTest(changes=changes):
                self.assertFalse(self.guard(**changes)["result"]["ok"])

    def test_damage_and_low_ammunition_do_not_grant_quiet_slot(self):
        for changes in ({"actor_health": 249}, {"asset_health": 199}, {"ammo": 9}, {"ammo": 0}):
            with self.subTest(changes=changes):
                self.assertFalse(self.guard(**changes)["result"]["quiet"])

    def test_missing_changed_unpowered_or_impure_route_retains_repair_priority(self):
        for changes in ({"arm_missing": True}, {"arm_unit": 55}, {"arm_direction": 8},
                        {"arm_energy": 0}, {"arm_connected": False}, {"cargo": "iron-plate"}):
            with self.subTest(changes=changes):
                result = self.guard(**changes)["result"]
                self.assertTrue(result["quiet"])
                self.assertFalse(result["routes_ready"])

    def test_healthy_turret_facing_change_keeps_intake_ready_but_other_guards_remain(self):
        for facing in (0, 4, 8, 12):
            with self.subTest(turret_facing=facing):
                result = self.guard(turret_direction=facing)["result"]
                self.assertTrue(result["quiet"])
                self.assertTrue(result["routes_ready"])
        for changes in ({"arm_direction": 12}, {"belt_direction": 12}):
            with self.subTest(changes=changes):
                result = self.guard(turret_direction=12, **changes)["result"]
                self.assertFalse(result["routes_ready"])
        for changes in ({"turret_unit": 55}, {"turret_unit": None}, {"turret_missing": True}):
            with self.subTest(changes=changes):
                self.assertFalse(self.guard(turret_direction=12, **changes)["result"]["ok"])
        for changes in ({"ammo": 9}, {"turret_health": 399}):
            with self.subTest(changes=changes):
                self.assertFalse(self.guard(turret_direction=12, **changes)["result"]["quiet"])


if __name__ == "__main__":
    unittest.main()
