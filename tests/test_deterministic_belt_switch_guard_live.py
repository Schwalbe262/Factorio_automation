"""Opt-in emitted mining Lua tests using local tables only, never engine entities."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from factorio_ai import deterministic_game
from factorio_ai.deterministic_game import DeterministicGame, run_config
from tests.test_deterministic_belt_switch_guard import switch_action


SHADOW = r'''
local storage=nil;local game=nil;local o=helpers.json_to_table(OPTIONS)
local request=helpers.json_to_table(ACTION);local q=request.belt_route_replacement
local d={world_id=o.world or "switch-world"};local f={};local s={};local rows={};local by_unit={}
local counts={mined=0,started=0,slot_checks=0,removed=0,inserted=0}
local inv={valid=not o.invalid_inventory,
 get_item_count=function(spec) assert(spec.name=="transport-belt" and spec.quality=="normal");return o.replacement_stock or 1 end,
 count_empty_stacks=function(filtered,barred) assert(filtered==false and barred==false);counts.slot_checks=counts.slot_checks+1;return o.empty_slots or 5 end,
 remove=function() error("guard must never remove items") end,insert=function() error("guard must never insert items") end}
local a=setmetatable({valid=true,unit_number=o.actor_unit or 58,force=o.actor_foreign and {} or f,
 surface=o.actor_surface and {} or s,get_main_inventory=function() return inv end,can_reach_entity=function() return not o.distant end,
 mine_entity=function(e) assert(e.unit_number==11);counts.mined=counts.mined+1;return not o.mine_fails end},
 {__newindex=function(t,k,v) if k=="mining_state" then counts.started=counts.started+1 else error("unexpected actor write") end end})
local prototypes={item={["copper-plate"]={stack_size=100},["transport-belt"]={stack_size=100}}}
local function make(spec)
 local e={valid=true,name=spec.name,type=spec.name,unit_number=spec.unit_number,position={x=spec.position.x,y=spec.position.y},
  lookup=spec.position,direction=spec.direction,force=f,surface=s,health=150,max_health=150,minable=true,
  quality={name="normal"},belt_to_ground_type=spec.belt_to_ground_type,prototype={max_underground_distance=5},lines={}}
 e.get_max_transport_line_index=function() return 4 end
 e.get_transport_line=function(index) return {get_contents=function() return e.lines[index] or {} end} end
 rows[#rows+1]=e;by_unit[e.unit_number]=e;return e
end
local old=make{name="transport-belt",unit_number=11,position=request.position,direction=q.entry_direction}
old.lines[1]={{name="copper-plate",quality="normal",count=2}};old.lines[2]={{name="copper-plate",quality="normal",count=2}}
local exit=make(q.exit)
for _,spec in ipairs(q.entities) do make(spec) end
for _,pair in ipairs(q.pairs) do local input=by_unit[pair.input.unit_number];local output=by_unit[pair.output.unit_number]
 input.underground_belt_neighbour=output;output.underground_belt_neighbour=input end
if o.entry_unit then old.unit_number=o.entry_unit end
if o.entry_direction then old.direction=o.entry_direction end
if o.entry_offset then old.position.x=old.position.x+.1 end
if o.entry_foreign then old.force={} end
if o.entry_surface then old.surface={} end
if o.entry_damaged then old.health=149 end
if o.entry_unminable then old.minable=false end
if o.entry_contaminated then old.lines[2][1].name="iron-plate" end
if o.entry_quality then old.lines[2][1].quality=o.entry_quality end
if o.hardware_quality then old.quality.name=o.hardware_quality end
if o.exit_unit then exit.unit_number=o.exit_unit end
if o.exit_damaged then exit.health=149 end
if o.exit_contaminated then exit.lines[1]={{name="iron-plate",quality="normal",count=1}} end
if o.new_missing then by_unit[21].valid=false end
if o.new_unit then by_unit[21].unit_number=99 end
if o.new_direction then by_unit[21].direction=8 end
if o.new_offset then by_unit[21].position.x=2.5 end
if o.new_foreign then by_unit[21].force={} end
if o.new_surface then by_unit[21].surface={} end
if o.new_damaged then by_unit[21].health=149 end
if o.new_role then by_unit[22].belt_to_ground_type="output" end
if o.hidden_contamination then by_unit[22].lines[3]={{name="iron-plate",quality="normal",count=1}} end
if o.hidden_quality then by_unit[23].lines[4]={{name="copper-plate",quality="rare",count=1}} end
if o.forward_pair then by_unit[22].underground_belt_neighbour={} end
if o.backward_pair then by_unit[23].underground_belt_neighbour={} end
if o.current_limit then by_unit[22].prototype.max_underground_distance=o.current_limit end
local function target(position,name)
 for _,e in ipairs(rows) do if e.name==name and e.lookup.x==position.x and e.lookup.y==position.y then return e end end
end
local function success(row) row.ok=true;return row end
local function failure(reason) return {ok=false,reason=reason} end
'''


@unittest.skipUnless(os.environ.get("FACTORIO_BELT_SWITCH_LIVE_TEST") == "1", "requires opt-in RCON for inert Lua tables")
class BeltSwitchLuaTests(unittest.TestCase):
    def execute(self, options, backend="assisted"):
        action = switch_action()
        with TemporaryDirectory() as temporary:
            game = DeterministicGame(run_config(runtime=Path(temporary),
                rcon_port=int(os.environ.get("FACTORIO_BELT_SWITCH_RCON_PORT", "27025"))), backend=backend)
            with patch.object(game, "query", return_value={"ok": True}) as query, patch.object(game, "_record_action"):
                game.act(action)
                body = query.call_args.args[0]
            shadow = SHADOW.replace("OPTIONS", json.dumps(json.dumps(options))).replace("ACTION", json.dumps(json.dumps(action)))
            body = "local result=(function() " + body + " end)();result.counts=counts;return result"
            with patch.object(deterministic_game, "_HELPERS", ""):
                return game.query(shadow + body)

    def test_success_uses_one_normal_mine_without_manual_item_transfers(self):
        result = self.execute({})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["counts"]["mined"], 1)
        self.assertEqual(result["counts"]["started"], 0)
        self.assertEqual(result["counts"]["slot_checks"], 1)
        result = self.execute({"mine_fails": True})
        self.assertFalse(result["ok"])
        self.assertEqual((result["reason"], result["counts"]["mined"]), ("mining_failed", 1))

    def test_every_changed_guard_rejects_before_normal_mining_or_item_mutations(self):
        cases = [{key: True} for key in ("actor_foreign", "actor_surface", "entry_offset", "entry_foreign", "entry_surface",
            "entry_damaged", "entry_unminable", "entry_contaminated", "exit_damaged", "exit_contaminated",
            "new_missing", "new_unit", "new_direction", "new_offset", "new_foreign", "new_surface", "new_damaged",
            "new_role", "hidden_contamination", "hidden_quality", "forward_pair", "backward_pair", "invalid_inventory")]
        cases += [{"world": "other"}, {"actor_unit": 59}, {"entry_unit": 13}, {"entry_direction": 4},
                  {"entry_quality": "rare"}, {"hardware_quality": "rare"}, {"exit_unit": 13},
                  {"current_limit": 3}, {"replacement_stock": 0}, {"empty_slots": 4}]
        for backend in ("assisted",):
            for options in cases:
                with self.subTest(backend=backend, options=options):
                    result = self.execute(options, backend)
                    self.assertFalse(result["ok"], result)
                    self.assertIn("reason", result)
                    self.assertEqual(result["counts"]["mined"], 0)
                    self.assertEqual(result["counts"]["started"], 0)
                    self.assertEqual((result["counts"]["removed"], result["counts"]["inserted"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
