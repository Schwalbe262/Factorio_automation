"""Opt-in execution of emitted Lua against local tables, never engine entities."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factorio_ai import deterministic_game
from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_game import DeterministicGame, run_config
from factorio_ai.deterministic_underground import OBSERVE_UNDERGROUND_LUA


BUILD_SHADOW = r'''
local storage=nil;local game=nil;local o=helpers.json_to_table(OPTIONS)
local counts={remaining=o.stock or 2,removed=0,refunded=0,created=0,placements=0}
local f={};local s={};local inv={}
inv.get_item_count=function() return counts.remaining end
inv.remove=function(x) counts.remaining=counts.remaining-x.count;counts.removed=counts.removed+x.count;return x.count end
inv.insert=function(x) counts.remaining=counts.remaining+x.count;counts.refunded=counts.refunded+x.count;return x.count end
local a={valid=true,position={x=o.distant and 100 or 0,y=0},build_distance=10,get_main_inventory=function() return inv end}
local old={name="underground-belt",unit_number=81,position={x=o.offset and 0.6 or 0.5,y=0.5},direction=o.direction or 4,
 force=o.foreign and {} or f,surface=o.other_surface and {} or s,belt_to_ground_type=o.role or "input"}
local function target() if o.existing then return old end end
local prototypes={entity={["underground-belt"]={items_to_place_this={{name="underground-belt"}}}}}
s.can_place_entity=function(x) counts.placements=counts.placements+1;counts.placement_type=x.type;return not o.blocked end
s.create_entity=function(x) counts.created=counts.created+1;counts.create_type=x.type;if not o.create_fails then return {unit_number=82} end end
local function success(t) t.ok=true;return t end
local function failure(reason) return {ok=false,reason=reason} end
'''

PAIR_SHADOW = r'''
local storage=nil;local game=nil;local o=helpers.json_to_table(OPTIONS)
local function pos(p) return {x=p.x,y=p.y} end
local f={name="fixture"};local s={name="fixture"}
local e={valid=true,name="underground-belt",unit_number=11,position={x=0.5,y=0.5},direction=4,
 force=f,surface=s,belt_to_ground_type="input",prototype={max_underground_distance=5}}
local n={valid=true,name="underground-belt",unit_number=12,position={x=o.finish_x or 4.5,y=o.finish_y or 0.5},direction=o.direction or 4,
 force=o.foreign and {name="foreign"} or f,surface=o.other_surface and {name="other"} or s,belt_to_ground_type=o.role or "output"}
if not o.unpaired then e.underground_belt_neighbour=n end
if not o.not_reciprocal then n.underground_belt_neighbour=e end
if o.other_name then n.name="fast-underground-belt" end
e.get_max_transport_line_index=function() return 4 end
e.get_transport_line=function(index) return {get_contents=function()
 if index==3 then return {{name="copper-plate",count=5,quality="normal"}}
 elseif index==4 then return {{name="iron-plate",count=2,quality="uncommon"}}
 else return {} end
end} end
'''


@unittest.skipUnless(os.environ.get("FACTORIO_UNDERGROUND_LIVE_TEST") == "1", "requires opt-in RCON for inert Lua tables")
class UndergroundLuaTests(unittest.TestCase):
    def query(self, shadow, body, options):
        with TemporaryDirectory() as temporary:
            game = DeterministicGame(run_config(runtime=Path(temporary),
                rcon_port=int(os.environ.get("FACTORIO_UNDERGROUND_RCON_PORT", "27041"))))
            with patch.object(deterministic_game, "_HELPERS", ""):
                return game.query(shadow.replace("OPTIONS", json.dumps(json.dumps(options))) + body)

    def build(self, options, *, backend="assisted", placement=False, omit_direction=False):
        with TemporaryDirectory() as temporary:
            game = DeterministicGame(run_config(runtime=Path(temporary)), backend=backend)
            spec = {"name": "underground-belt", "position": {"x": .5, "y": .5}, "direction": 4,
                    "belt_to_ground_type": "input"}
            if omit_direction:
                spec.pop("direction")
            with patch.object(game, "query", return_value={"ok": True}) as query, patch.object(game, "_record_action"):
                if placement:
                    FactoryBuilder(game, None, SimpleNamespace(entities={})).can_place([spec])
                else:
                    game.act({"type": "build", **spec})
                body = query.call_args.args[0]
        body = "local result=(function() " + body + " end)();result.counts=counts;return result"
        return self.query(BUILD_SHADOW, body, options)

    def test_wrong_endpoint_cannot_be_reused_or_accepted_for_placement(self):
        for backend in ("assisted", "character"):
            for options in ({"role": "output"}, {"foreign": True}, {"other_surface": True},
                            {"offset": True}, {"direction": 8}):
                for placement in (False, True):
                    with self.subTest(backend=backend, placement=placement, options=options):
                        result = self.build({"existing": True, **options}, backend=backend, placement=placement)
                        self.assertFalse(result["ok"], result)
                        self.assertEqual(result["counts"]["remaining"], 2)
                        self.assertEqual(result["counts"]["removed"], 0)
                        self.assertEqual(result["counts"]["placements"], 0)
        result = self.build({"existing": True}, placement=True, omit_direction=True)
        self.assertFalse(result["ok"], result)  # Default direction0 cannot adopt a direction4 mouth.

    def test_paid_build_reuse_and_failed_creation_conserve_inventory(self):
        cases = [({}, True, 1, 1, 0), ({"existing": True}, True, 2, 0, 0),
                 ({"blocked": True}, False, 2, 0, 0), ({"stock": 0}, False, 0, 0, 0),
                 ({"create_fails": True}, False, 2, 1, 1)]
        for backend in ("assisted", "character"):
            for options, ok, remaining, removed, refunded in cases:
                with self.subTest(backend=backend, options=options):
                    result = self.build(options, backend=backend)
                    self.assertEqual(result["ok"], ok, result)
                    counts = result["counts"]
                    self.assertEqual((counts["remaining"], counts["removed"], counts["refunded"]),
                                     (remaining, removed, refunded))
                    if ok:
                        self.assertEqual(result["unit_number"], 81 if options.get("existing") else 82)
                    if counts["created"]:
                        self.assertEqual((counts["placement_type"], counts["create_type"]), ("input", "input"))
        result = self.build({"distant": True}, backend="character")
        self.assertEqual(result["reason"], "out_of_reach")
        self.assertEqual(result["counts"]["remaining"], 2)
        self.assertEqual(result["counts"]["placements"], 0)

    def test_pair_identity_geometry_and_all_four_transport_lines_are_observed(self):
        body = OBSERVE_UNDERGROUND_LUA + "return observe_underground(e)"
        result = self.query(PAIR_SHADOW, body, {})
        self.assertTrue(result["underground_pair_verified"], result)
        self.assertEqual(result["max_underground_distance"], 5)
        self.assertEqual(result["underground_span"], 4)
        self.assertEqual(result["underground_neighbour"]["unit_number"], 12)
        self.assertEqual(result["belt_inventory"], {"copper-plate": 5, "iron-plate": 2})
        self.assertEqual(len(result["transport_lines"]), 4)
        self.assertEqual(result["transport_lines"][3]["contents"][0]["quality"], "uncommon")
        for options in ({"unpaired": True}, {"not_reciprocal": True}, {"foreign": True},
                        {"other_surface": True}, {"role": "input"}, {"direction": 8},
                        {"finish_x": 6.5}, {"finish_x": -3.5}, {"finish_y": 1.5}, {"other_name": True}):
            with self.subTest(options=options):
                result = self.query(PAIR_SHADOW, body, options)
                self.assertFalse(result["underground_pair_verified"], result)


if __name__ == "__main__":
    unittest.main()
