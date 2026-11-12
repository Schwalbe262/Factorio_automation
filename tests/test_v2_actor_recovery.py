import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.actor_recovery import (inspect_actor_recovery, execute_actor_recovery,
    ACTOR_RECOVERY_INSPECT_LUA, ACTOR_RECOVERY_EXECUTE_LUA)

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


class ActorRecoveryInterfaceTests(unittest.TestCase):
    def test_status_uses_query_without_normal_observe_or_actions(self):
        game=SimpleNamespace(expected_world='w',query=Mock(return_value={'ok':True}),
            observe=Mock(side_effect=AssertionError('agent_dead')),act=Mock(side_effect=AssertionError('no gameplay')))
        self.assertTrue(inspect_actor_recovery(game)['ok'])
        game.observe.assert_not_called();game.act.assert_not_called()

    def test_exact_parameters_fail_before_any_query(self):
        game=SimpleNamespace(expected_world='w',query=Mock())
        for args in (('',11,12),('other',11,12),('w',11,11),('w',True,12),('w',11,0)):
            with self.assertRaises(ValueError):execute_actor_recovery(game,*args)
        game.query.assert_not_called()


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class NativeActorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.lua=LuaRuntime(unpack_returned_tuples=True)
        def table(value):
            if isinstance(value,dict):return self.lua.table_from({k:table(v)for k,v in value.items()})
            if isinstance(value,list):return self.lua.table_from([table(v)for v in value])
            return value
        self.lua.globals().helpers=self.lua.table_from({'json_to_table':lambda raw:table(json.loads(raw))})
        self.lua.execute('''f={index=1};s={index=1};owner={index=1,name="FactoryAutomaton",valid=true,connected=true,controller_type=7,force=f}
old={valid=false};new={valid=true,name="character",unit_number=10915,position={x=0,y=0},surface=s,force=f,player=owner,health=250,max_health=250}
owner.character=new;d={world_id="w",actor=old,crafting_player_index=1,crafting_actor_unit_number=11,prior_receipt={actor_unit=11,items=37}}
defines={inventory={character_main=1,character_guns=2,character_ammo=3,character_armor=4,character_trash=5}}
main={{valid_for_read=true,name="iron-plate",quality={name="normal"},count=12,prototype={type="item"}}}
ammo={{valid_for_read=true,name="firearm-magazine",quality={name="normal"},count=10,ammo=10,prototype={type="ammo",magazine_size=10}}}
new.get_inventory=function(index)if index==1 then return main elseif index==3 then return ammo else return{}end end
corpse={valid=true,position={x=153,y=-60},surface=s,character_corpse_player_index=1,character_corpse_tick_of_death=90}
corpses={corpse};characters={new};corpse_force_filter_seen=false
s.find_entities_filtered=function(q)
 if q.type=="character-corpse"then if q.force~=nil then corpse_force_filter_seen=true end;return corpses end
 if q.type=="character"then return characters end;error("unexpected query")
end
f.get_spawn_position=function(surface)return{x=0,y=0}end
function pos(p)return{x=p.x,y=p.y}end
game={tick=100,surfaces={s},get_player=function(key)if key==1 or key=="FactoryAutomaton"then return owner end end}''')
        self.game=SimpleNamespace(expected_world='w',query=self.query)

    def query(self, script):
        return dict(self.lua.execute('return(function() '+' '.join(script.splitlines())+' end)()').items())

    def recover(self):return execute_actor_recovery(self.game,'w',11,10915)

    def test_inspection_finds_neutral_owner_corpse_without_mutating_storage(self):
        result=inspect_actor_recovery(self.game)
        self.assertTrue(result['ok']);self.assertTrue(result['recovery_ready'])
        self.assertEqual(result['classification'],'owner_death_evidence_with_successor')
        self.assertEqual(len(result['matching_owner_corpses']),1)
        self.assertFalse(self.lua.globals().corpse_force_filter_seen)
        self.assertEqual(result['successor_inventory']['ammo']['totals']['firearm-magazine'],10)
        self.assertTrue(self.lua.eval('d.actor==old and d.crafting_actor_unit_number==11 and d.actor_successions==nil'))

    def test_absent_corpse_reports_uncertainty_instead_of_asserting_no_death(self):
        self.lua.execute('corpses={}')
        result=inspect_actor_recovery(self.game)
        self.assertEqual(result['classification'],'replacement_or_death_without_retained_corpse')
        self.assertTrue(result['recovery_ready'])

    def test_recovery_changes_only_framework_binding_and_new_succession_record(self):
        result=self.recover();self.assertTrue(result['ok']);self.assertFalse(result['already_recovered'])
        self.assertEqual(result['tick'],100)
        self.assertTrue(self.lua.eval('d.actor==new and d.crafting_actor_unit_number==10915 and d.crafting_player_index==1'))
        self.assertTrue(self.lua.eval('d.prior_receipt.actor_unit==11 and d.prior_receipt.items==37'))
        self.assertTrue(self.lua.eval('new.health==250 and main[1].count==12 and ammo[1].count==10 and ammo[1].ammo==10 and owner.controller_type==7'))
        self.assertEqual(result['succession']['old_unit'],11);self.assertEqual(result['succession']['new_unit'],10915)
        self.assertEqual(result['succession']['owner_index'],1)

    def test_exact_marker_retry_is_idempotent_and_does_not_overwrite_history(self):
        first=self.recover();self.lua.execute('game.tick=200')
        second=self.recover();self.assertTrue(second['ok']);self.assertTrue(second['already_recovered'])
        self.assertEqual(second['tick'],200)
        self.assertEqual(second['succession']['tick'],first['succession']['tick'])
        self.lua.execute('new.unit_number=10916')
        self.assertFalse(self.recover()['ok'])

    def test_live_pointer_live_old_unit_and_changed_owner_are_rejected(self):
        changes=[('old.valid=true;old.unit_number=11','old.valid=false'),
            ('characters={new,{valid=true,unit_number=11}}','characters={new}'),
            ('owner.name="Other"','owner.name="FactoryAutomaton"'),
            ('d.crafting_player_index=2','d.crafting_player_index=1'),
            ('owner.connected=false','owner.connected=true'),
            ('new.player={index=2}','new.player=owner'),
            ('new.force={index=2}','new.force=f'),('new.surface={index=2}','new.surface=s'),
            ('d.world_id="other"','d.world_id="w"'),('d.crafting_actor_unit_number=12','d.crafting_actor_unit_number=11')]
        for change,restore in changes:
            with self.subTest(change=change):
                self.lua.execute(change);result=self.recover();self.assertFalse(result['ok'])
                self.assertTrue(self.lua.eval('d.actor==old and d.actor_successions==nil'))
                self.lua.execute(restore)

    def test_pending_native_repair_blocks_without_clearing_or_retargeting_it(self):
        self.lua.execute('d.native_repair={actor_unit=11,world_id="w",request="old-request",phase="cleanup"}')
        report=inspect_actor_recovery(self.game)
        self.assertTrue(report['native_repair_pending']);self.assertFalse(report['recovery_ready'])
        self.assertEqual(report['pending_native_repair']['actor_unit'],11)
        self.assertEqual(self.recover()['reason'],'actor_recovery_pending_native_repair_requires_separate_review')
        self.assertTrue(self.lua.eval('d.actor==old and d.native_repair.actor_unit==11 and d.native_repair.request=="old-request"'))

    def test_conflicting_audit_and_changed_retry_marker_fail_closed(self):
        self.lua.execute('d.actor_successions={other={world_id="w",old_unit=11,new_unit=999}}')
        self.assertEqual(self.recover()['reason'],'actor_recovery_conflicting_succession')
        self.lua.execute('d.actor_successions=nil');self.assertTrue(self.recover()['ok'])
        self.lua.execute('for _,record in pairs(d.actor_successions)do record.owner_index=2 end')
        self.assertEqual(self.recover()['reason'],'actor_recovery_retry_marker_or_current_binding_changed')

    def test_scripts_are_flattened_and_exclude_gameplay_mutations(self):
        for script in(ACTOR_RECOVERY_INSPECT_LUA,ACTOR_RECOVERY_EXECUTE_LUA):
            self.assertNotIn('--',script)
            for forbidden in('create_entity','destroy(','.die(','.health=','insert{','remove{','set_controller',
                             '.researched=','respawn(','game.speed=','d.native_repair=nil'):
                self.assertNotIn(forbidden,script)


if __name__=='__main__':unittest.main()
