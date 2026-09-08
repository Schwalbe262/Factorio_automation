"""Opt-in inert Lua tables exercise the actual repair guards, without world writes."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from factorio_ai import deterministic_game
from factorio_ai import deterministic_repair_control as repair
from factorio_ai.deterministic_game import DeterministicGame, run_config


FIXTURE_LUA = r'''
local o=helpers.json_to_table(OPTIONS)
local game={tick=o.tick or 20};local storage=nil;local f={};local s={}
local d={world_id="fixture",crafting_player_index=1,crafting_actor_unit_number=15}
local x={expected_world_id=o.world or "fixture",expected_actor_unit=o.actor or 15,
 expected_entity_unit=o.unit or 44,name="steel-chest",position={x=3.5,y=.5},request="request"}
local function success(t) t.ok=true;return t end
local function failure(reason) return {ok=false,reason=reason} end
local function pos(p) return {x=p.x,y=p.y} end
local prototype={get_durability=function() return 300 end}
local function stack(count,durability,name)
 return {valid_for_read=count>0,count=count,durability=durability,name=name or "repair-pack",
  quality={name="normal"},is_repair_tool=not name or name=="repair-pack",prototype=prototype}
end
local inv={stack(o.multiple_packs and 2 or 1,300)};local cursor=stack(0,0)
inv.find_item_stack=function() return inv[1] end
cursor.transfer_stack=function(source,count)
 cursor.valid_for_read=true;cursor.count=count;cursor.durability=source.durability
 source.count=source.count-count;source.valid_for_read=source.count>0;return true
end
inv.transfer_from_stack=function(source)
 if o.full_inventory then return 0 end
 inv[1]=stack(source.count,source.durability);source.count=0;source.valid_for_read=false;return inv[1].count
end
local previous={valid=true,surface=s,unit_number=99};local unrelated={valid=true,surface=s,unit_number=100}
local e={valid=true,force=f,surface=s,name="steel-chest",type="container",unit_number=44,
 position=x.position,health=o.health or 200,max_health=350}
if o.foreign then e.force={} end
if o.surface then e.surface={} end
local function target() if o.missing then return nil end;return e end
local a={valid=true,unit_number=15,force=f,surface=s,cursor_stack=cursor,crafting_queue_size=0,
 selected=previous,can_reach_entity=function() return not o.distant end,get_main_inventory=function() return inv end}
a.player={connected=not o.disconnected,name="FactoryAutomaton",character=a,index=1}
a.clear_selected_entity=function() a.selected=nil end
a.update_selected_entity=function() a.selected=e end
if o.cursor_busy then cursor.name="wood";cursor.valid_for_read=true;cursor.count=1;cursor.is_repair_tool=false end
if o.pending then
 inv[1]=stack(0,0);cursor.valid_for_read=true;cursor.count=1;cursor.durability=o.durability or 295
 d.native_repair={request="request",world_id="fixture",actor_unit=15,entity_unit=44,name=e.name,
  position=e.position,started_tick=10,deadline=130,previous_selected=previous,last_selected=e,
  quality="normal",health=200,count=1,durability=300,phase="repair"}
 a.selected=o.unrelated_selection and unrelated or e
 if o.request_changed then d.native_repair.request="another-request" end
end
local function perform()
BODY
end
local result=perform()
return {ok=true,result=result,pending=d.native_repair~=nil,cursor_count=cursor.count,
 cursor_name=cursor.name,inventory_count=inv[1].count,inventory_durability=inv[1].durability,
 selection=a.selected and a.selected.unit_number,repairing=a.repair_state and a.repair_state.repairing or false}
'''


@unittest.skipUnless(os.environ.get("FACTORIO_REPAIR_INERT_TEST") == "1", "requires inert RCON fixture")
class RepairControlLuaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.game = DeterministicGame(run_config(runtime=Path(os.environ.get("FACTORIO_REPAIR_RUNTIME",
            "runtime/native-repair-fixture")), server_port=34244,
            rcon_port=int(os.environ.get("FACTORIO_REPAIR_RCON_PORT", "27044"))))

    def evaluate(self, body, **options):
        script = FIXTURE_LUA.replace("OPTIONS", json.dumps(json.dumps(options))).replace("BODY", body)
        with patch.object(deterministic_game, "_HELPERS", ""):
            result = self.game.query(script)
        self.assertTrue(result.get("ok"), result)
        return result

    def test_begin_rejects_changed_world_actor_target_force_surface_and_unrelated_cursor(self):
        for change in ({"world": "different"}, {"actor": 16}, {"unit": 45}, {"foreign": True},
                       {"surface": True}, {"disconnected": True}, {"missing": True}, {"cursor_busy": True}):
            with self.subTest(change=change):
                result = self.evaluate(repair.BEGIN_REPAIR_LUA, **change)
                self.assertFalse(result["result"]["ok"])
                self.assertFalse(result["pending"])
                self.assertFalse(result["repairing"])
                self.assertEqual(result["inventory_count"], 1)
                self.assertEqual(result["selection"], 99)
                self.assertEqual(result["cursor_count"], 1 if change.get("cursor_busy") else 0)

    def test_out_of_reach_uses_no_pack_and_prepare_claims_one_without_health_input(self):
        result = self.evaluate(repair.BEGIN_REPAIR_LUA, distant=True)
        self.assertEqual(result["result"]["reason"], "native_repair_out_of_reach")
        self.assertEqual((result["inventory_count"], result["cursor_count"]), (1, 0))
        result = self.evaluate(repair.BEGIN_REPAIR_LUA)
        self.assertEqual(result["result"]["status"], "running")
        self.assertEqual((result["inventory_count"], result["cursor_count"]), (0, 1))
        self.assertFalse(result["repairing"])

    def test_spare_packs_are_rejected_before_any_claim_to_prevent_native_auto_refill(self):
        result = self.evaluate(repair.BEGIN_REPAIR_LUA, multiple_packs=True)
        self.assertEqual(result["result"]["reason"], "native_repair_single_carried_pack_required")
        self.assertFalse(result["pending"])
        self.assertFalse(result["repairing"])
        self.assertEqual((result["inventory_count"], result["cursor_count"]), (2, 0))

    def test_pulse_revalidates_identity_tick_deadline_selection_and_real_reach(self):
        for change, reason in (({"tick": 5}, "native_repair_tick_rollback"),
                               ({"tick": 131}, "native_repair_tick_budget_reached"),
                               ({"missing": True}, "native_repair_target_changed"),
                               ({"distant": True}, "native_repair_out_of_reach"),
                               ({"unrelated_selection": True}, "native_repair_selection_changed")):
            with self.subTest(change=change):
                result = self.evaluate(repair.PULSE_REPAIR_LUA, pending=True, **change)
                self.assertEqual(result["result"]["reason"], reason)
                self.assertFalse(result["repairing"])
        result = self.evaluate(repair.PULSE_REPAIR_LUA, pending=True)
        self.assertTrue(result["repairing"])
        self.assertEqual(result["selection"], 44)

    def test_cleanup_returns_exact_partial_durability_and_preserves_unrelated_selection(self):
        for unrelated in (False, True):
            result = self.evaluate(repair.FINISH_REPAIR_LUA, pending=True, health=210, unrelated_selection=unrelated)
            self.assertEqual(result["result"]["durability_used"], 5)
            self.assertEqual(result["result"]["repaired"], 10)
            self.assertEqual((result["inventory_count"], result["inventory_durability"]), (1, 295))
            self.assertEqual(result["cursor_count"], 0)
            self.assertFalse(result["pending"])
            self.assertEqual(result["selection"], 100 if unrelated else 99)

    def test_inventory_full_retains_real_pack_for_later_cleanup_and_request_mismatch_cannot_release(self):
        result = self.evaluate(repair.FINISH_REPAIR_LUA, pending=True, full_inventory=True)
        self.assertEqual(result["result"]["reason"], "native_repair_cursor_return_waiting")
        self.assertTrue(result["pending"])
        self.assertEqual((result["inventory_count"], result["cursor_count"]), (0, 1))
        result = self.evaluate(repair.FINISH_REPAIR_LUA, pending=True, request_changed=True)
        self.assertEqual(result["result"]["reason"], "native_repair_request_changed")
        self.assertTrue(result["pending"])
        self.assertEqual(result["cursor_count"], 1)

    def test_external_health_gain_without_tool_cost_and_tick_rollback_cannot_claim_success(self):
        for change, reason in (({"health": 210, "durability": 300}, "native_repair_receipt_uncorrelated"),
                               ({"tick": 5}, "native_repair_receipt_invalidated")):
            result = self.evaluate(repair.FINISH_REPAIR_LUA, pending=True, **change)
            self.assertFalse(result["result"]["ok"])
            self.assertEqual(result["result"]["reason"], reason)
            self.assertFalse(result["pending"])
            self.assertEqual(result["cursor_count"], 0)

    def test_actual_take_and_insert_queries_transfer_partial_quality_tools_without_recreating_them(self):
        shadow = r'''
local o=helpers.json_to_table(OPTIONS)
local game=nil;local storage=nil;local f={};local s={}
local function success(t) t.ok=true;return t end
local function failure(reason) return {ok=false,reason=reason} end
local source={{valid_for_read=true,name="repair-pack",count=2,durability=37,
 quality={name="rare"},is_repair_tool=true}}
local destination={{valid_for_read=o.full_inventory,count=o.full_inventory and 1 or 0}}
destination[1].transfer_stack=function(stack,count)
 if o.full_inventory then return false end
 assert(count==1,"only the requested count may move")
 local dest=destination[1];dest.valid_for_read=true;dest.count=1;dest.name=stack.name
 dest.quality=stack.quality;dest.durability=stack.durability
 stack.count=stack.count-1;stack.durability=300;return true
end
local inv=o.kind=="take" and destination or source
local other=o.kind=="take" and source or destination
local a={valid=true,get_main_inventory=function() return inv end,can_reach_entity=function() return true end}
local e={force=o.foreign and {} or f,surface=s,type="container",
 get_output_inventory=function() return other end,get_inventory=function() return other end}
local function target() return e end
local function perform()
BODY
end
local result=perform()
return {ok=true,result=result,source_count=source[1].count,source_durability=source[1].durability,
 dest_count=destination[1].count,dest_durability=destination[1].durability,
 quality=destination[1].quality and destination[1].quality.name}
'''
        for backend in ("assisted", "character"):
            for kind in ("take", "insert"):
                for change in ({}, {"full_inventory": True}, {"foreign": True}):
                    with self.subTest(backend=backend, kind=kind, change=change):
                        adapter = DeterministicGame(self.game.cfg, backend=backend)
                        action = {"type": kind, "name": "steel-chest", "position": {"x": 0, "y": 0},
                                  "item": "repair-pack", "count": 1}
                        with patch.object(adapter, "query", return_value={"ok": True}) as query, \
                                patch.object(adapter, "_record_action", side_effect=lambda action, result: result):
                            adapter.act(action)
                        body = query.call_args.args[0]
                        script = shadow.replace("OPTIONS", json.dumps(json.dumps({"kind": kind, **change}))).replace("BODY", body)
                        with patch.object(deterministic_game, "_HELPERS", ""):
                            result = self.game.query(script)
                        self.assertTrue(result.get("ok"), result)
                        if not change:
                            self.assertEqual(result["result"].get("moved"), 1)
                            self.assertEqual((result["source_count"], result["source_durability"]), (1, 300))
                            self.assertEqual((result["dest_count"], result["dest_durability"], result["quality"]), (1, 37, "rare"))
                        else:
                            self.assertEqual((result["source_count"], result["source_durability"]), (2, 37))
                            self.assertNotEqual(result["result"].get("status"), "succeeded")


if __name__ == "__main__":
    unittest.main()
