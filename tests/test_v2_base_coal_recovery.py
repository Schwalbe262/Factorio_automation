from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_agent.board import entity
from factorio_agent.base_coal_recovery import (BaseCoalRecovery,coal_budget,preserved_pipe_specs,power_forks,
    COAL_HEALTH_LUA,DEPLETION_LUA,_key,_hash,active_coal_plan)
from factorio_ai.deterministic_builder import _plan

try:
    from lupa import LuaRuntime
except ImportError:LuaRuntime=None


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.cat=SimpleNamespace(fingerprint='fp',entities={'boiler':{'energy_usage':30000}},items={'coal':{'fuel_value':4000000}})
        self.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root),query=Mock())
        self.builder=SimpleNamespace(bootstrap=Mock(),ensure_plan=Mock(),state={})
        self.p=BaseCoalRecovery(self.game,self.builder,self.cat,Mock())
        self.pred=dict(entity('transport-belt',50.5,-7.5,4),unit_number=101)
        self.old=dict(entity('transport-belt',16.5,-3.5,0),unit_number=100)
        self.split=dict(entity('splitter',16,-3.5,0),_width=2,_height=1,splitter_input_priority='right',splitter_output_priority='none')
        self.outlet=entity('transport-belt',15.5,-4.5,0)
        self.plan=_plan([self.split,self.outlet],[],clear_obstacles=[],clearance_world_id='w')
        self.p.state=dict(world_id='w',catalog_fingerprint='fp',phase='cutover',old=self.old,dead_predecessor=self.pred,
            recoveries=[self.pred,self.old],recovery_index=0,protected=[],new_entities=[self.split,self.outlet],bindings={},
            plan=self.plan,shared_capacity={'boiler_max_coal_per_minute':54},samples=[])
        self.obs=dict(world_id='w',tick=100,entities=[self.old,self.pred],inventory={'splitter':1})
        self.p._docs=Mock(return_value={'board':{'blocks':{'base_coal_recovery':self.plan}},'steam':{'bank':{}}})
        self.p._capacity=Mock(return_value={'ok':True,'active_collector_ceiling_per_minute':66})

    def test_one_shared_source_budget_is_66_not_two_times_66(self):
        b=coal_budget(self.cat,[{'name':'boiler'},{'name':'boiler'}],66)
        self.assertEqual((b['boiler_max_coal_per_minute'],b['spare_potential_per_minute'],b['source_count']),(54,12,1))
        self.assertIsNone(b['measured_per_minute'])

    def test_rotated_north_fork_pays_two_exact_recoveries_before_any_build(self):
        with patch('factorio_agent.base_coal_recovery.preflight',return_value={'ok':True}):
            first=self.p.step(self.obs);self.assertEqual(first['expected_entity_unit'],101)
            second=self.p.step(dict(self.obs,entities=[self.old]));self.assertEqual(second['expected_entity_unit'],100)
            self.assertEqual(second['position'],self.old['position'])
            self.builder.ensure_plan.return_value=dict(type='build',name='splitter',direction=0)
            third=self.p.step(dict(self.obs,entities=[]));self.assertEqual(third['direction'],0)
            self.assertEqual(self.p.state['recovery_index'],2);self.assertEqual(self.p.state['started_recoveries'],[101,100])

    def test_foreign_replacement_nonempty_dead_predecessor_or_unstarted_loss_is_not_recovered(self):
        with patch('factorio_agent.base_coal_recovery.preflight',return_value={'ok':True}):
            for change,reason in [({'unit_number':999},'base_coal_cutover_unit_changed'),
                    ({'belt_inventory':{'coal':1}},'base_coal_dead_predecessor_no_longer_empty')]:
                self.assertEqual(self.p.step(dict(self.obs,entities=[self.old,dict(self.pred,**change)]))['reason'],reason)
            self.assertEqual(self.p.step(dict(self.obs,entities=[self.old]))['reason'],'base_coal_unstarted_corner_missing')

    def test_only_preserved_normal_pipe_survey_orientation_is_canonicalized(self):
        specs=[entity('pipe',1.5,.5,8),entity('steam-engine',5.5,.5,8)];before=deepcopy(specs)
        actual={('pipe',1.5,.5):dict(specs[0],direction=0,unit_number=12)}
        fixed=preserved_pipe_specs(specs,actual)
        self.assertEqual([q['direction']for q in fixed],[0,8]);self.assertEqual(specs,before)
        with self.assertRaises(ValueError):preserved_pipe_specs(specs,{})

    def test_interrupted_five_document_publication_resumes_only_old_or_new_hashes(self):
        names=['factory-builder.json','steam-expansion.json','mining-outposts.json','pcb-board.json','steam-coal-reroute.json']
        intent={}
        for name in names:
            old={'phase':'old','name':name};new={'phase':'new','name':name}
            (self.root/name).write_text(json.dumps(old));intent[name]=dict(old_hash=_hash(old),new_hash=_hash(new),value=new)
        self.p.state['publication_intent']=intent
        from factorio_agent import base_coal_recovery as module
        actual=module._atomic_json;calls=[]
        def interrupted(path,value):
            if Path(path).name in names:
                calls.append(Path(path).name)
                if len(calls)==3:raise OSError('interrupted after known prefix')
            return actual(path,value)
        with patch.object(module,'_atomic_json',side_effect=interrupted),self.assertRaises(OSError):self.p._finish_publication()
        self.assertEqual(json.loads((self.root/names[0]).read_text())['phase'],'new')
        self.assertIsNone(self.p._finish_publication());self.assertTrue(self.p.state['published'])
        self.assertTrue(all(json.loads((self.root/name).read_text())['phase']=='new'for name in names))
        (self.root/names[-1]).write_text(json.dumps({'foreign':'change'}))
        self.assertEqual(self.p._finish_publication()['reason'],'base_coal_publication_compare_and_swap_failed')

    def test_native_fork_generation_preserves_right_through_for_left_north_branch(self):
        rows=[entity('transport-belt',16.5,y,0)for y in(1.5,.5,-.5,-1.5,-2.5,-3.5,-4.5)]
        source=dict(position={'x':16.5,'y':1.5},facing=0,item='coal');dest=dict(position={'x':16.5,'y':-4.5},facing=0,item='coal')
        candidates=power_forks(_plan(rows,[]),source,dest,entity('transport-belt',51.5,-7.5,8))
        chosen=next(q for q in candidates if q['outlet']['position']=={'x':15.5,'y':-4.5})
        self.assertEqual((chosen['splitter']['direction'],chosen['splitter']['splitter_input_priority']),(0,'right'))

    def test_adopted_active_source_never_rebuilds_dead_predecessor_or_calls_exhausted_drill_productive(self):
        drill=entity('electric-mining-drill',-53.5,12.5,8);old_drill=entity('electric-mining-drill',-48.5,14.5,4)
        fixed_source=[dict(item='coal',position={'x':-51.5,'y':14.5},facing=4)]
        plan=_plan([drill,self.split,self.outlet],fixed_source,producer_rows={'left':[drill],'right':[]})
        state=dict(self.p.state,final_bank_plan=plan,old_bank_plan=plan,downstream=[entity('transport-belt',51.5,-7.5,8)],
            old_steam={'bank':{'plan':plan}},depleted_source={'unit_number':956,'depleted':True},
            old_builder={'coal_plan':{'drill':old_drill,'ports':[{'position':{'x':-45.5,'y':16.5}}]}})
        result=active_coal_plan(state)
        self.assertEqual(result['drill'],drill);self.assertEqual(result['ports'],fixed_source)
        self.assertNotIn(old_drill,result['entities']);self.assertNotIn({k:v for k,v in self.pred.items()if k!='unit_number'},result['entities'])
        self.assertTrue(result['retired_source']['diagnostic']['depleted'])
        self.assertEqual(result['retired_source']['diagnostic']['unit_number'],956)


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class NativeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.lua=LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute('''d={world_id="w"};f={};s={};game={tick=100};defines={inventory={}};world={}
function belt(unit,dir)local e={unit_number=unit,direction=dir,force=f,surface=s};e.get_transport_line=function()return{get_contents=function()return{{name="coal",quality="normal",count=4}}end}end;return e end
target=function(p,name)return world[name..":"..p.x..":"..p.y]end
''')
        def table(v):
            if isinstance(v,dict):return self.lua.table_from({k:table(q)for k,q in v.items()})
            if isinstance(v,list):return self.lua.table_from([table(q)for q in v])
            return v
        self.table=table

    def test_actual_lua_north_priority_coal_and_all_four_engines_are_required(self):
        split=dict(name='splitter',position={'x':16,'y':-3.5},unit_number=100,direction=0,splitter_input_priority='right')
        rows=[dict(name='transport-belt',position={'x':i+.5,'y':-.5},unit_number=200+i,direction=0)for i in range(3)]
        machines=[]
        for i,name in enumerate(['boiler','boiler']+['steam-engine']*4):machines.append(dict(name=name,position={'x':i+.5,'y':10.5},unit_number=300+i))
        self.lua.globals().x=self.table(dict(world_id='w',splitter=split,rows=rows,machines=machines))
        self.lua.execute('''world["splitter:16:-3.5"]={unit_number=100,direction=0,force=f,surface=s,splitter_input_priority="right",splitter_output_priority="none"}
for _,q in ipairs(x.rows)do world[q.name..":"..q.position.x..":"..q.position.y]=belt(q.unit_number,q.direction)end
for _,q in ipairs(x.machines)do local e={name=q.name,unit_number=q.unit_number,force=f,surface=s,energy_generated_last_tick=500,burner={remaining_burning_fuel=100}}
 e.get_fluid_contents=function()return{water=200,steam=200}end;e.get_fuel_inventory=function()return{get_item_count=function()return 5 end}end;e.is_connected_to_electric_network=function()return true end
 world[q.name..":"..q.position.x..":"..q.position.y]=e end''')
        result=self.lua.execute(COAL_HEALTH_LUA);self.assertTrue(result['ok'])
        self.lua.execute('x.splitter.splitter_input_priority="left";world["splitter:16:-3.5"].splitter_input_priority="left"')
        self.assertTrue(self.lua.execute(COAL_HEALTH_LUA)['ok'])
        self.lua.execute('world["splitter:16:-3.5"].direction=4')
        self.assertEqual(self.lua.execute(COAL_HEALTH_LUA)['reason'],'base_coal_native_priority_changed')

    def test_actual_depletion_is_not_relabelled_as_productive_coal(self):
        self.lua.globals().x=self.table(dict(world_id='w',drill=dict(name='electric-mining-drill',position={'x':-48.5,'y':14.5},unit_number=956)))
        self.lua.execute('''s.find_entities_filtered=function()return{}end;local e={unit_number=956,force=f,surface=s,position={x=-48.5,y=14.5},prototype={mining_drill_radius=2.49},energy=1600,status=99}
e.is_connected_to_electric_network=function()return true end;world["electric-mining-drill:-48.5:14.5"]=e''')
        result=self.lua.execute(DEPLETION_LUA);self.assertTrue(result['depleted']);self.assertTrue(result['powered']);self.assertEqual(result['remaining_coal'],0)


if __name__=='__main__':unittest.main()
