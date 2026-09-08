"""Opt-in Lua inventory shadows on an existing RCON server; no world writes."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from factorio_ai import deterministic_game
from factorio_ai.deterministic_game import DeterministicGame, run_config


INERT_HELPERS = r'''
local function success(t) t=t or {};t.ok=true;return t end
local function failure(reason) return {ok=false,reason=reason} end
'''

INVENTORY_SHADOW = r'''
local game=nil;local storage=nil;local s={};local f={}
local options=helpers.json_to_table(OPTIONS_JSON)
local d={world_id=options.world}
local defines={inventory={character_ammo=42}}
local item={type=options.item_type,stack_size=options.stack_size,magazine_size=10}
local prototypes={item={}}
if options.prototype_exists then prototypes.item['firearm-magazine']=item end
local states={
 {valid_for_read=options.readable,name=options.item_name,count=options.source_count,
  ammo=options.ammo,quality={name=options.quality},prototype=item},
 {valid_for_read=true,name='firearm-magazine',count=4,ammo=2,
  quality={name='normal'},prototype=item},
 {valid_for_read=true,name='firearm-magazine',count=3,ammo=10,
  quality={name='rare'},prototype=item}}
local function snapshot()
 local out={}
 for i,state in ipairs(states) do
  out[i]={valid_for_read=state.valid_for_read,name=state.name,count=state.count,
   ammo=state.ammo,quality=state.quality.name}
 end
 return out
end
local before=snapshot()
local equipped={valid=options.inventory_valid}
for i,state in ipairs(states) do
 equipped[i]=setmetatable({}, {
  __index=state,
  __newindex=function(_,key,value)
   assert(key=='count','only exact selected stack count may change')
   assert(value>=0 and value<=state.count,'source count must only decrease')
   state.count=value
   if value==0 then state.valid_for_read=false end
  end})
end
equipped.remove=function() error('name-based ammunition removal is forbidden') end
local inserted=0;local insert_calls=0;local offered=0;local inserted_name='';local inserted_quality=''
local main={valid=true,
 get_insertable_count=function() return options.capacity end,
 insert=function(spec)
  insert_calls=insert_calls+1;offered=spec.count
  inserted_name=spec.name;inserted_quality=spec.quality or 'normal'
  local count=math.min(spec.count,options.capacity,options.insert_limit)
  inserted=inserted+count;return count
 end,
 remove=function() error('main inventory removal is forbidden') end}
local a={valid=options.actor_valid,unit_number=options.actor_unit,
 force=options.owned and f or {},
 get_main_inventory=function() return main end,
 get_inventory=function(index)
  assert(index==defines.inventory.character_ammo,'wrong source inventory')
  if options.inventory_exists then return equipped end
 end,
 insert=function() error('actor insert may choose equipped slots; use main inventory') end}
local function attempt()
ACTION_BODY
end
local result=attempt()
return {result=result,before=before,after=snapshot(),inserted=inserted,
 insert_calls=insert_calls,offered=offered,inserted_name=inserted_name,inserted_quality=inserted_quality}
'''


@unittest.skipUnless(os.environ.get("FACTORIO_EQUIPPED_AMMO_LIVE_TEST") == "1",
                     "requires explicit opt-in and an existing RCON server")
class EquippedAmmoLuaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = DeterministicGame(run_config(
            runtime=Path(os.environ.get("FACTORIO_EQUIPPED_AMMO_RUNTIME",
                                        "runtime/deterministic/adapter-smoke")),
            server_port=int(os.environ.get("FACTORIO_EQUIPPED_AMMO_SERVER_PORT", "34210")),
            rcon_port=int(os.environ.get("FACTORIO_EQUIPPED_AMMO_RCON_PORT", "27025"))))

    def recover(self, *, action_changes=None, backend="assisted", **changes):
        action = {"type": "recover_equipped_ammo", "item": "firearm-magazine", "slot": 1,
                  "count": 20, "expected_actor_unit_number": 58,
                  "expected_actor_world_id": "test-world"}
        action.update(action_changes or {})
        options = {"world": "test-world", "actor_unit": 58, "actor_valid": True,
                   "owned": True, "item_type": "ammo", "stack_size": 100,
                   "prototype_exists": True, "readable": True, "source_count": 70,
                   "item_name": "firearm-magazine", "ammo": 10, "quality": "normal",
                   "inventory_exists": True, "inventory_valid": True,
                   "capacity": 100, "insert_limit": 100}
        options.update(changes)
        adapter = DeterministicGame(self.live.cfg, backend=backend)
        with patch.object(adapter, "query", return_value={"ok": True}) as query, \
                patch.object(adapter, "_record_action", side_effect=lambda _, result: result):
            adapter.act(action)
        body = query.call_args.args[0]
        self.assertIn(deterministic_game.RECOVER_EQUIPPED_AMMO_LUA, body)
        fixture = INVENTORY_SHADOW.replace("OPTIONS_JSON", json.dumps(json.dumps(options)))
        fixture = fixture.replace("ACTION_BODY", body)
        # The production helper can restore storage.deterministic_player.actor.
        # Replace it before live.query so this test cannot write shared storage.
        with patch.object(deterministic_game, "_HELPERS", INERT_HELPERS):
            result = self.live.query(fixture)
        self.assertIn("result", result, result)
        self.assertEqual(result["after"][1:], result["before"][1:], result)
        self.assertEqual(result["after"][0]["count"] + result["inserted"],
                         result["before"][0]["count"], result)
        return result

    def assert_unchanged_failure(self, **changes):
        observed = self.recover(**changes)
        self.assertFalse(observed["result"]["ok"], observed)
        self.assertEqual(observed["before"], observed["after"], observed)
        self.assertEqual((observed["inserted"], observed["insert_calls"]), (0, 0), observed)

    def test_normal_full_magazines_reach_explicit_main_inventory(self):
        for backend in ("assisted", "character"):
            with self.subTest(backend=backend):
                observed = self.recover(backend=backend)
                self.assertTrue(observed["result"]["ok"], observed)
                self.assertEqual(observed["result"]["moved"], 20)
                self.assertEqual((observed["after"][0]["count"], observed["inserted"]), (50, 20))
                self.assertEqual(observed["inserted_name"], "firearm-magazine")
                self.assertEqual(observed["inserted_quality"], "normal")

    def test_requested_count_is_capped_by_actual_main_space(self):
        observed = self.recover(capacity=7)
        self.assertTrue(observed["result"]["ok"], observed)
        self.assertEqual(observed["result"]["moved"], 7)
        self.assertEqual((observed["after"][0]["count"], observed["inserted"], observed["offered"]),
                         (63, 7, 7))

    def test_source_decrement_uses_actual_insert_result(self):
        observed = self.recover(insert_limit=3)
        self.assertTrue(observed["result"]["ok"], observed)
        self.assertEqual(observed["result"]["moved"], 3)
        self.assertEqual((observed["after"][0]["count"], observed["inserted"], observed["offered"]),
                         (67, 3, 20))

    def test_full_main_or_rejected_insert_preserves_every_stack(self):
        for changes in ({"capacity": 0}, {"insert_limit": 0}):
            with self.subTest(changes=changes):
                observed = self.recover(**changes)
                self.assertEqual(observed["inserted"], 0)
                self.assertEqual(observed["before"], observed["after"])

    def test_exhausting_exact_source_slot_clears_that_stack(self):
        observed = self.recover(source_count=7)
        self.assertTrue(observed["result"]["ok"], observed)
        self.assertEqual(observed["result"]["moved"], 7)
        self.assertEqual(observed["after"][0]["count"], 0)
        self.assertFalse(observed["after"][0]["valid_for_read"])

    def test_stale_actor_world_or_force_cannot_transfer_ammunition(self):
        for changes in ({"actor_unit": 59}, {"world": "other-world"},
                        {"owned": False}, {"actor_valid": False}):
            with self.subTest(changes=changes):
                self.assert_unchanged_failure(**changes)

    def test_missing_changed_or_unreadable_source_cannot_transfer(self):
        for changes in ({"inventory_exists": False}, {"inventory_valid": False},
                        {"readable": False}, {"item_name": "piercing-rounds-magazine"}):
            with self.subTest(changes=changes):
                self.assert_unchanged_failure(**changes)

    def test_partial_magazines_and_non_normal_quality_are_preserved(self):
        for changes in ({"ammo": 2}, {"quality": "rare"}):
            with self.subTest(changes=changes):
                self.assert_unchanged_failure(**changes)

    def test_prototype_must_be_ammunition_with_sufficient_stack_size(self):
        for changes in ({"prototype_exists": False}, {"item_type": "item"}, {"stack_size": 10}):
            with self.subTest(changes=changes):
                self.assert_unchanged_failure(**changes)

    def test_slot_outside_existing_ammo_inventory_is_rejected(self):
        self.assert_unchanged_failure(action_changes={"slot": 4})
