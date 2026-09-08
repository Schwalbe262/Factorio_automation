"""Opt-in insertion/collection Lua shadows; the fixture world is never mutated."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from factorio_ai import deterministic_game
from factorio_ai.deterministic_game import DeterministicGame, run_config


INERT_HELPERS = """
local function success(t) t=t or {};t.ok=true;return t end
local function failure(reason) return {ok=false,reason=reason} end
"""

TRANSFER_SHADOW = r'''
local game=nil;local storage=nil;local d={};local s={};local f={}
local o=helpers.json_to_table(OPTIONS_JSON)
local main_count=o.main;local output_count=o.output;local destination_count=0
local lanes={o.first,o.second};local calls={}
local function called(name,spec)
 assert(type(spec.count)=="number" and spec.count>0,"zero-count inventory call: "..name)
 calls[name]=(calls[name] or 0)+1
end
local function snapshot() return {main=main_count,output=output_count,first=lanes[1],second=lanes[2],destination=destination_count} end
local before=snapshot()
local main={valid=true,
 get_item_count=function() return main_count end,
 get_insertable_count=function() return o.capacity end,
 insert=function(spec)
  called("main_insert",spec)
  local n=math.min(spec.count,o.capacity,o.insert_limit)
  main_count=main_count+n;return n
 end,
 remove=function(spec)
  called("main_remove",spec)
  local n=math.min(spec.count,main_count);main_count=main_count-n;return n
 end}
local output={valid=true,
 remove=function(spec)
  called("output_remove",spec)
  local n=math.min(spec.count,output_count);output_count=output_count-n;return n
 end,
 insert=function(spec)
  called("output_restore",spec);output_count=output_count+spec.count;return spec.count
 end}
local destination={valid=true,insert=function(spec)
 called("destination_insert",spec)
 local n=math.min(spec.count,o.destination_capacity);destination_count=destination_count+n;return n
end}
local lines={}
for i=1,2 do lines[i]={
 get_item_count=function() return i==1 and o.reported_first or o.reported_second end,
 remove_item=function(spec)
  called("line"..i.."_remove",spec)
  local n=math.min(spec.count,lanes[i]);lanes[i]=lanes[i]-n;return n
 end} end
local e={valid=true,type=o.target_type,
 get_transport_line=function(i) return lines[i] end,
 get_output_inventory=function() return output end,
 get_inventory=function() return o.kind=="insert" and destination or output end,
 insert=destination.insert}
local defines={inventory={chest=1,turret_ammo=2}}
local a={valid=true,get_main_inventory=function() return main end,can_reach_entity=function() return o.reachable end}
local function target() if o.target_present then return e end end
local function attempt()
ACTION_BODY
end
local result=attempt()
return {result=result,before=before,after=snapshot(),calls=calls}
'''


@unittest.skipUnless(os.environ.get("FACTORIO_TRANSFER_LIVE_TEST") == "1",
                     "requires explicit opt-in and an existing fixture RCON server")
class TransferLuaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = DeterministicGame(run_config(
            runtime=Path(os.environ.get("FACTORIO_TRANSFER_RUNTIME", "runtime/deterministic/factory-fixture")),
            server_port=int(os.environ.get("FACTORIO_TRANSFER_SERVER_PORT", "34214")),
            rcon_port=int(os.environ.get("FACTORIO_TRANSFER_RCON_PORT", "27029"))))

    def transfer(self, *, kind="take", backend="assisted", count=8, **changes):
        options = {"kind": kind, "main": 0, "output": 0, "first": 0, "second": 0,
                   "reported_first": None, "reported_second": None, "capacity": 100, "insert_limit": 100,
                   "destination_capacity": 100, "target_type": "transport-belt", "reachable": True, "target_present": True}
        options.update(changes)
        for key, source in (("reported_first", "first"), ("reported_second", "second")):
            if options[key] is None:
                options[key] = options[source]
        action = {"type": kind, "name": "transport-belt" if options["target_type"] == "transport-belt" else "wooden-chest",
                  "position": {"x": .5, "y": .5}, "item": "iron-plate", "count": count}
        if kind == "insert":
            action["inventory"] = "chest"
        adapter = DeterministicGame(self.live.cfg, backend=backend)
        with patch.object(adapter, "query", return_value={"ok": True}) as query, \
                patch.object(adapter, "_record_action", side_effect=lambda action, result: result):
            adapter.act(action)
        fixture = TRANSFER_SHADOW.replace("OPTIONS_JSON", json.dumps(json.dumps(options)))
        fixture = fixture.replace("ACTION_BODY", query.call_args.args[0])
        with patch.object(deterministic_game, "_HELPERS", INERT_HELPERS):
            observed = self.live.query(fixture)
        self.assertIn("result", observed, observed)
        self.assertEqual(sum(observed["before"].values()), sum(observed["after"].values()), observed)
        return observed

    def test_belt_empty_after_observation_waits_without_zero_count_insert(self):
        for backend in ("assisted", "character"):
            result = self.transfer(backend=backend)
            self.assertEqual((result["result"]["status"], result["result"]["moved"]), ("waiting", 0))
            self.assertEqual(result["calls"], {})

    def test_full_main_inventory_waits_before_belt_or_output_collection(self):
        for target_type in ("transport-belt", "container"):
            for backend in ("assisted", "character"):
                result = self.transfer(backend=backend, target_type=target_type, first=8, output=8, capacity=0)
                self.assertEqual(result["result"]["moved"], 0)
                self.assertEqual(result["calls"], {})

    def test_insert_missing_stock_or_full_destination_preserves_material(self):
        for backend in ("assisted", "character"):
            empty = self.transfer(kind="insert", backend=backend, target_type="container", main=0)
            self.assertEqual(empty["result"]["moved"], 0)
            self.assertEqual(empty["calls"], {})
            full = self.transfer(kind="insert", backend=backend, target_type="container", main=8, destination_capacity=0)
            self.assertEqual(full["result"]["moved"], 0)
            self.assertNotIn("main_remove", full["calls"])

    def test_empty_machine_output_never_inserts_zero_items(self):
        for backend in ("assisted", "character"):
            result = self.transfer(backend=backend, target_type="container", output=0)
            self.assertEqual(result["result"]["moved"], 0)
            self.assertNotIn("main_insert", result["calls"])

    def test_belt_collection_spans_both_lanes_with_exact_actual_count(self):
        for backend in ("assisted", "character"):
            result = self.transfer(backend=backend, first=2, second=7)
            self.assertEqual(result["result"]["moved"], 8)
            self.assertEqual((result["after"]["main"], result["after"]["first"], result["after"]["second"]), (8, 0, 1))

    def test_belt_partial_main_acceptance_and_zero_acceptance_are_safe(self):
        partial = self.transfer(first=2, second=7, insert_limit=3)
        self.assertEqual(partial["result"]["moved"], 3)
        self.assertEqual((partial["after"]["main"], partial["after"]["second"]), (3, 6))
        zero = self.transfer(first=2, second=7, insert_limit=0)
        self.assertEqual(zero["result"]["moved"], 0)
        self.assertNotIn("line1_remove", zero["calls"])

    def test_belt_short_removal_rolls_back_only_unbacked_main_items(self):
        for backend in ("assisted", "character"):
            result = self.transfer(backend=backend, first=2, second=1, reported_first=4, reported_second=6)
            self.assertEqual(result["result"]["moved"], 3)
            self.assertEqual((result["after"]["main"], result["after"]["first"], result["after"]["second"]), (3, 0, 0))
            self.assertEqual(result["calls"]["main_remove"], 1)

    def test_output_partial_or_zero_acceptance_restores_the_original_source(self):
        for backend in ("assisted", "character"):
            for limit in (0, 3):
                result = self.transfer(backend=backend, target_type="container", output=8, insert_limit=limit)
                self.assertEqual(result["result"]["moved"], limit)
                self.assertEqual((result["after"]["main"], result["after"]["output"]), (limit, 8-limit))
                self.assertEqual(result["calls"]["output_restore"], 1)

    def test_partial_destination_insert_removes_only_delivered_actor_stock(self):
        for backend in ("assisted", "character"):
            result = self.transfer(kind="insert", backend=backend, target_type="container", main=8, destination_capacity=3)
            self.assertEqual(result["result"]["moved"], 3)
            self.assertEqual((result["after"]["main"], result["after"]["destination"]), (5, 3))

    def test_missing_target_and_character_reach_guard_never_mutate_inventory(self):
        for changes in ({"target_present": False}, {"backend": "character", "reachable": False}):
            result = self.transfer(first=8, **changes)
            self.assertFalse(result["result"]["ok"])
            self.assertEqual(result["before"], result["after"])
            self.assertEqual(result["calls"], {})


if __name__ == "__main__":
    unittest.main()
