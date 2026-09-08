"""Opt-in actual emitted survey Lua over local tables; no world entities or writes."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from factorio_ai import deterministic_game
from factorio_ai.deterministic_game import DeterministicGame, run_config
from factorio_ai.deterministic_fluids import FluidProduction


SHADOW = r'''
local game=nil;local storage=nil;local o=helpers.json_to_table(OPTIONS)
local f={};local foreign={};local s={}
local function pos(p) return {x=p.x,y=p.y} end
local function pipe(x,id,fluid,force)
 return {position={x=x,y=0},force=force or f,
  get_fluid_segment_id=function() return id end,
  get_fluid_contents=function() return fluid and {[fluid]=100} or {} end}
end
local origin=pipe(0,1,o.source_fluid or "water",o.foreign_source and foreign)
local receiver=pipe(10,o.connected and 1 or 2,o.receiver_fluid,o.foreign_receiver and foreign)
local member=pipe(20,o.connected and 1 or 2,o.member_fluid,o.foreign_member and foreign)
local rows={origin,receiver,member}
local function target(p,name)
 assert(name=="pipe")
 for _,e in ipairs(rows) do if e.position.x==p.x and e.position.y==p.y then return e end end
end
s.find_entities_filtered=function(args) assert(args.type=="pipe" and args.force==nil);return rows end
'''


@unittest.skipUnless(os.environ.get("FACTORIO_FLUID_RECEIVER_LIVE_TEST") == "1", "requires opt-in RCON for inert tables")
class ReceiverSurveyLuaTests(unittest.TestCase):
    def execute(self, options):
        with TemporaryDirectory() as temporary:
            game = DeterministicGame(run_config(runtime=Path(temporary),
                rcon_port=int(os.environ.get("FACTORIO_FLUID_RECEIVER_RCON_PORT", "27025"))))
            fluids = FluidProduction(game, Mock(), Mock())
            source = {"position": {"x": 0, "y": 0}, "item": "water"}
            destination = {"position": {"x": 10, "y": 0}, "item": "water"}
            with patch.object(game, "query", return_value={"ok": True}) as query:
                fluids._network_taps(source, destination)
                body = query.call_args.args[0]
            with patch.object(deterministic_game, "_HELPERS", ""):
                result = game.query(SHADOW.replace("OPTIONS", json.dumps(json.dumps(options))) + body)
            with patch.object(game, "query", return_value=result):
                return result, fluids._network_taps(source, destination)

    def test_empty_and_pure_intended_receiver_members_are_valid(self):
        for options in ({}, {"receiver_fluid": "water", "member_fluid": "water"}, {"connected": True}):
            with self.subTest(options=options):
                result, network = self.execute(options)
                self.assertTrue(result["ok"], result)
                self.assertEqual(len(network["destination_taps"]), 3 if options.get("connected") else 2)
                self.assertEqual(network["connected"], bool(options.get("connected")))

    def test_contaminated_or_foreign_endpoint_or_member_blocks_whole_segment(self):
        for options in ({"receiver_fluid": "steam"}, {"member_fluid": "crude-oil"},
                        {"source_fluid": "petroleum-gas"}, {"foreign_source": True},
                        {"foreign_receiver": True}, {"foreign_member": True},
                        {"connected": True, "member_fluid": "steam"}):
            with self.subTest(options=options):
                result, network = self.execute(options)
                self.assertFalse(result["ok"], result)
                self.assertTrue(result["unsafe"])
                self.assertEqual(network["taps"], [])
                self.assertEqual(network["blocked"], result["reason"])


if __name__ == "__main__":
    unittest.main()
