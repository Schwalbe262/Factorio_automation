import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.corpse_recovery import (inspect_corpse_recovery, execute_corpse_recovery,
    CORPSE_RECOVERY_INSPECT_LUA, CORPSE_RECOVERY_EXECUTE_LUA)

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


class CorpseRecoveryInterfaceTests(unittest.TestCase):
    def test_only_exact_world_actor_and_death_are_accepted_without_coordinates(self):
        game=SimpleNamespace(expected_world='w',query=Mock(return_value={'ok':True}))
        inspect_corpse_recovery(game,'w',10915,90);self.assertEqual(game.query.call_count,1)
        game.query.reset_mock()
        for args in (('other',10915,90),('w',True,90),('w',10915,0),('',10915,90)):
            with self.assertRaises(ValueError):execute_corpse_recovery(game,*args)
        game.query.assert_not_called()


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class NativeCorpseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.lua=LuaRuntime(unpack_returned_tuples=True)
        def table(value):
            if isinstance(value,dict):return self.lua.table_from({k:table(v)for k,v in value.items()})
            if isinstance(value,list):return self.lua.table_from([table(v)for v in value])
            return value
        self.lua.globals().helpers=self.lua.table_from({'json_to_table':lambda raw:table(json.loads(raw))})
        self.lua.execute('''f={index=1};s={index=1};game={tick=100};calls=0;interrupt_once=false;corrupt=false;duplicate=false;corpse_force_filter_seen=false
owner={valid=true,index=1,name="FactoryAutomaton",connected=true,force=f}
actor={valid=true,name="character",unit_number=10915,force=f,surface=s,player=owner,position={x=0,y=0},health=250,crafting_queue_size=0,cursor_stack={valid_for_read=false}}
owner.character=actor;d={world_id="w",actor=actor,crafting_actor_unit_number=10915,crafting_player_index=1,prior_receipt={actor_unit=11,items=37}}
defines={inventory={character_main=1,character_guns=2,character_ammo=3,character_armor=4,character_trash=5,character_corpse=1}}
local function stack(name,count,type_,extra)
 local proto={type=type_,magazine_size=10,get_durability=function(quality)return 60 end}
 local value={valid_for_read=true,name=name,count=count,quality={name="normal"},prototype=proto,native_stack=true}
 for k,v in pairs(extra or{})do value[k]=v end;return value
end
corpse_inv={valid=true,stack("iron-plate",10,"item"),stack("firearm-magazine",2,"ammo",{ammo=3}),stack("repair-pack",2,"repair-tool",{durability=7,tags={original="retained"}})}
main={valid=true,stack("iron-plate",5,"item"),{valid_for_read=false},{valid_for_read=false},{valid_for_read=false}}
other_ammo={valid=true,stack("firearm-magazine",10,"ammo",{ammo=10})};empty_inv={valid=true}
actor.get_inventory=function(index)if index==1 then return main elseif index==3 then return other_ammo else return empty_inv end end
actor.get_main_inventory=function()return main end
main.get_item_count=function(name)local n=0;for i=1,#main do if main[i].valid_for_read and main[i].name==name then n=n+main[i].count end end;return n end
corpse={valid=true,position={x=153,y=-60},surface=s,character_corpse_player_index=1,character_corpse_tick_of_death=90,get_inventory=function(index)return corpse_inv end}
s.find_entities_filtered=function(q)
 if q.force~=nil then corpse_force_filter_seen=true end
 if not corpse.valid then return{}end
 if duplicate then return{corpse,corpse}end
 return{corpse}
end
f.get_spawn_position=function(surface)return{x=0,y=0}end
game.get_player=function(key)if key==1 or key=="FactoryAutomaton"then return owner end end
function pos(p)return{x=p.x,y=p.y}end
local function transfer(source)
 assert(source.native_stack,"must transfer the real source stack")
 local moved=0
 if source.prototype.type=="item"then for i=1,#main do local target=main[i]
  if target.valid_for_read and target.name==source.name and target.quality.name==source.quality.name and target.count<100 then
   local amount=math.min(100-target.count,source.count);target.count=target.count+amount;source.count=source.count-amount;moved=moved+amount
   if source.count==0 then source.valid_for_read=false;return moved end
  end
 end end
 for i=1,#main do if not main[i].valid_for_read then
  local target={};for k,v in pairs(source)do target[k]=v end
  main[i]=target;moved=moved+source.count;source.count=0;source.valid_for_read=false
  if corrupt and target.prototype.type=="ammo"then target.ammo=10 end
  return moved
 end end
 return moved
end
main.transfer_from_stack=function(source)
 calls=calls+1;local moved=transfer(source)
 if interrupt_once then interrupt_once=false;error("inert interrupted native return")end
 return moved
end''')
        self.game=SimpleNamespace(expected_world='w',query=self.query)

    def query(self,script):
        flat=' '.join(script.splitlines())
        result=self.lua.execute('local ok,result=pcall(function() '+flat+' end);if not ok then return{ok=false,reason=tostring(result)}end;return result')
        return dict(result.items())

    def inspect(self):return inspect_corpse_recovery(self.game,'w',10915,90)
    def execute(self):return execute_corpse_recovery(self.game,'w',10915,90)

    def test_inspect_selects_exact_player_death_without_force_filter_or_changes(self):
        report=self.inspect();self.assertTrue(report['ok']);self.assertEqual(report['corpse']['goods']['item_count'],14)
        self.assertEqual(report['corpse']['goods']['rounds']['firearm-magazine@normal'],13)
        self.assertEqual(report['corpse']['goods']['durability']['repair-pack@normal'],67)
        self.assertFalse(self.lua.globals().corpse_force_filter_seen)
        self.assertTrue(self.lua.eval('calls==0 and d.corpse_recoveries==nil and actor.position.x==0 and main[1].count==5'))

    def test_actual_stack_transfer_conserves_counts_partial_rounds_and_tool_metadata(self):
        result=self.execute();self.assertTrue(result['ok'],result);self.assertEqual(result['status'],'succeeded')
        self.assertEqual(result['transferred_items'],14);self.assertEqual(result['remaining_items'],0)
        before=result['conservation']['before'];after=result['conservation']['after']
        self.assertEqual(before['item_count'],after['item_count']);self.assertEqual(after['item_count'],29)
        self.assertEqual(after['rounds']['firearm-magazine@normal'],113)
        self.assertEqual(after['durability']['repair-pack@normal'],67)
        self.assertTrue(self.lua.eval('main[2].ammo==3 and main[3].durability==7 and main[3].tags.original=="retained"'))
        self.assertTrue(self.lua.eval('actor.position.x==0 and actor.position.y==0 and actor.health==250 and d.prior_receipt.actor_unit==11'))

    def test_complete_retry_does_not_transfer_again_even_after_native_corpse_disappears(self):
        self.assertTrue(self.execute()['ok']);calls=self.lua.globals().calls
        self.lua.execute('corpse.valid=false;game.tick=200')
        result=self.execute();self.assertTrue(result['ok']);self.assertTrue(result['already_recovered'])
        self.assertEqual(result['transferred_items'],0);self.assertEqual(result['total_transferred_items'],14)
        self.assertEqual(result['tick'],200);self.assertEqual(self.lua.globals().calls,calls)

    def test_full_main_returns_waiting_then_resumes_only_actual_remaining_stacks(self):
        self.lua.execute('main[1].count=100;main[3]=nil;main[4]=nil')
        first=self.execute();self.assertTrue(first['ok']);self.assertEqual(first['status'],'waiting')
        self.assertEqual(first['transferred_items'],10);self.assertEqual(first['remaining_items'],4)
        self.lua.execute('main[1]={valid_for_read=false};main[2]={valid_for_read=false}')
        second=self.execute();self.assertTrue(second['ok']);self.assertEqual(second['status'],'succeeded')
        self.assertEqual(second['transferred_items'],4);self.assertEqual(second['total_transferred_items'],14)
        calls=self.lua.globals().calls;self.assertTrue(self.execute()['already_recovered']);self.assertEqual(self.lua.globals().calls,calls)

    def test_pending_prefix_reconciles_after_interrupted_transfer_without_replaying_items(self):
        self.lua.execute('interrupt_once=true')
        first=self.execute();self.assertEqual(first['reason'],'corpse_recovery_native_transfer_interrupted')
        self.assertTrue(self.lua.eval('main[1].count==15 and not corpse_inv[1].valid_for_read'))
        result=self.execute();self.assertTrue(result['ok'],result);self.assertEqual(result['total_transferred_items'],14)
        self.assertTrue(self.lua.eval('main[1].count==15'))

    def test_pending_complete_prefix_can_be_verified_after_native_corpse_removal(self):
        self.lua.execute('corpse_inv[2]=nil;corpse_inv[3]=nil;interrupt_once=true')
        self.assertEqual(self.execute()['reason'],'corpse_recovery_native_transfer_interrupted')
        calls=self.lua.globals().calls;self.lua.execute('corpse.valid=false')
        result=self.execute();self.assertTrue(result['ok'],result)
        self.assertEqual(result['reason'],'corpse_recovery_pending_prefix_verified')
        self.assertEqual(result['transferred_items'],0);self.assertEqual(result['total_transferred_items'],10)
        self.assertEqual(self.lua.globals().calls,calls)

    def test_broken_native_metadata_conservation_freezes_receipt_without_recreating_items(self):
        self.lua.execute('corrupt=true')
        result=self.execute();self.assertEqual(result['reason'],'corpse_recovery_goods_not_conserved')
        calls=self.lua.globals().calls
        self.assertEqual(self.execute()['reason'],'corpse_recovery_prior_conservation_failure_requires_review')
        self.assertEqual(self.lua.globals().calls,calls)

    def test_wrong_owner_death_world_actor_duplicate_and_pending_work_fail_before_transfer(self):
        changes=[('owner.name="Other"','owner.name="FactoryAutomaton"'),
            ('corpse.character_corpse_player_index=2','corpse.character_corpse_player_index=1'),
            ('corpse.character_corpse_tick_of_death=91','corpse.character_corpse_tick_of_death=90'),
            ('d.world_id="other"','d.world_id="w"'),('actor.unit_number=10916','actor.unit_number=10915'),
            ('duplicate=true','duplicate=false'),('d.native_repair={actor_unit=10915}','d.native_repair=nil'),
            ('actor.crafting_queue_size=1','actor.crafting_queue_size=0')]
        for change,restore in changes:
            self.lua.execute(change);self.assertFalse(self.execute()['ok'],change)
            self.assertEqual(self.lua.globals().calls,0);self.lua.execute(restore)

    def test_more_than_512_corpse_slots_and_changed_pinned_position_fail_closed(self):
        self.lua.execute('for i=4,513 do corpse_inv[i]={valid_for_read=false}end')
        self.assertFalse(self.execute()['ok']);self.assertEqual(self.lua.globals().calls,0)
        self.lua.execute('for i=4,513 do corpse_inv[i]=nil end;main[1].count=100;main[2]=nil;main[3]=nil;main[4]=nil')
        self.assertEqual(self.execute()['status'],'waiting')
        self.lua.execute('corpse.position.x=154')
        self.assertEqual(self.execute()['reason'],'corpse_recovery_marker_identity_changed')
        self.lua.execute('corpse.position.x=153;actor.position.x=1;main[2]={valid_for_read=false};main[3]={valid_for_read=false};main[4]={valid_for_read=false}')
        result=self.execute();self.assertTrue(result['ok']);self.assertEqual(result['actor_position']['x'],1)

    def test_raid_recovery_defers_spare_tools_without_losing_goods_or_blocking_native_repair_limit(self):
        self.lua.execute('main[4]={valid_for_read=true,name="repair-pack",count=1,durability=30,quality={name="normal"},prototype=corpse_inv[3].prototype,native_stack=true}')
        result=execute_corpse_recovery(self.game,'w',10915,90,keep_single_repair_tool=True)
        self.assertTrue(result['ok']);self.assertEqual(result['status'],'waiting')
        self.assertEqual(result['transferred_items'],12);self.assertEqual(result['deferred_repair_tools'],2)
        self.assertEqual(self.lua.eval('main.get_item_count("repair-pack")'),1)
        self.assertEqual(self.lua.eval('corpse_inv[3].count'),2)

    def test_product_script_has_only_native_stack_transfer_and_no_recreation_or_movement(self):
        self.assertIn('main.transfer_from_stack(stack)',CORPSE_RECOVERY_EXECUTE_LUA)
        for script in(CORPSE_RECOVERY_INSPECT_LUA,CORPSE_RECOVERY_EXECUTE_LUA):
            self.assertNotIn('--',script)
            for forbidden in('create_entity','destroy(','.health=','.position=','teleport(','.insert(','.set_stack(',
                             'remove_item','set_controller','game.speed=','craft{','respawn('):
                self.assertNotIn(forbidden,script)


if __name__=='__main__':unittest.main()
