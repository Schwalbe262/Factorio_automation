from copy import deepcopy
import json
import re
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_ai.deterministic_builder import FactoryBuilder,_plan
from factorio_ai.deterministic_state import _atomic_json
from factorio_agent.board import entity
from factorio_agent.defense_coverage import coverage_cells
from factorio_agent.defense_perimeter import DefensePerimeter
from factorio_agent.defense_relocation import (DefenseRelocation,_hash,_key,validate_relocation_action,
    execute_build_replacement,execute_retire_post,execute_reconcile_retirement,_perimeter_matches,
    BUILD_REPLACEMENT_LUA,RETIRE_POST_LUA,RECONCILE_RETIREMENT_LUA)
from factorio_agent.engine_production import engine_core,SCOPE_ID
from factorio_agent.smelting import merge_entities
from test_v2_engine_production import catalog

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


class FakePerimeter(DefensePerimeter):
    def inspect(self,obs):
        rows=[]
        for spec in self.state['posts']:
            live=self.live.get(tuple(spec['position'].values()))
            row=dict(name=spec['name'],position=spec['position'],expected_unit=spec.get('unit_number'),
                missing=not bool(live),ammo=0,ammo_rounds=0,full_magazines=0)
            if live:row.update(live,identity_changed=live['unit_number']!=spec.get('unit_number'))
            rows.append(row)
        return dict(ok=True,tick=obs.get('tick',1),posts=rows,actor_full_magazines=self.actor_full,
            actor_partial_magazines=0,missing=[p['name']for p in rows if p['missing']],
            changed=[p['name']for p in rows if p.get('identity_changed')],unarmed=[p['name']for p in rows if p['ammo']==0],
            refill=[p['name']for p in rows if p['ammo']<5])


class RelocationFixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.catalog=catalog();self.catalog.items={'firearm-magazine':{'stack_size':100}}
        self.game=SimpleNamespace(expected_world='w',backend='assisted',cfg=SimpleNamespace(runtime_dir=self.root),query=Mock())
        self.bootstrap=SimpleNamespace(ensure_item=Mock(return_value={'type':'craft','recipe':'gun-turret','count':1}))
        self.builder=SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan)
        core=engine_core(self.catalog,118.5,-114.5)
        self.iron=dict(kind='item',item='iron-plate',direction='output',facing=4,position={'x':12.5,'y':-111.5},unit_number=7150)
        self.steel=dict(kind='item',item='steel-plate',direction='output',facing=4,position={'x':108.5,'y':-37.5},unit_number=6213,world_id='w')
        es=merge_entities(core['entities'],[entity('transport-belt',134.5,-95.5,4),entity('transport-belt',144.5,-108.5,12),
            entity('transport-belt',145.5,-108.5,12)],[entity('transport-belt',*s['position'].values(),s['facing'])for s in(self.iron,self.steel)])
        plan=_plan(es,core['ports'],reserved_area=core['reserved_area'],underground_pairs=core['underground_pairs'])
        state={'world_id':'w','catalog_fingerprint':'c','phase':'build','plan':plan,'core':core,'parameters':{},
            'iron_source':self.iron,'steel_source':self.steel,'steel_plan_revision':'s','steel_furnace_units':[1,2],
            'bindings':{_key(dict(s,name='transport-belt')):s['unit_number']for s in(self.iron,self.steel)}}
        self.engines=SimpleNamespace(state=state,path=self.root/'engine-production.json')
        self.engines.path.write_text(json.dumps(state))
        posts=[dict(name='coverage_engine-production_'+str(i),position={'x':p[0],'y':p[1]},unit_number=7150+i,
            coverage_scope=SCOPE_ID,commissioned=True,local_walk_access=True)for i,p in enumerate(((127,-116),(135,-95),(113,-100),(146,-109)),1)]
        names=[p['name']for p in posts];scope=dict(areas=[core['reserved_area']],cells=coverage_cells([core['reserved_area']]),
            effective_range=16,covering_posts=names,new_posts=names,commissioned=True)
        perimeter={'world_id':'w','catalog_fingerprint':'c','phase':'armed','range':18,'posts':posts,
            'coverage_scopes':{SCOPE_ID:scope,'also-shared':deepcopy(scope)}}
        (self.root/'defense-perimeter.json').write_text(json.dumps(perimeter))
        self.perimeter=FakePerimeter(self.game,self.bootstrap,self.catalog);self.perimeter.actor_full=20
        self.perimeter.live={tuple(p['position'].values()):dict(unit_number=p['unit_number'],health=400,max_health=400,
            ammo=10,full_magazines=10,ammo_rounds=100)for p in posts}
        (self.root/'pcb-board.json').write_text(json.dumps({'world_id':'w','catalog_fingerprint':'c','blocks':{'engine_production':plan}}))
        self.obs={'ok':True,'world_id':'w','tick':1,'actor_unit_number':100,'inventory':{},'entities':
            [dict(entity('transport-belt',*s['position'].values(),s['facing']),unit_number=s['unit_number'])for s in(self.iron,self.steel)]+
            [dict(entity('gun-turret',*p['position'].values()),unit_number=p['unit_number'])for p in posts]}
        self.c=DefenseRelocation(self.game,self.bootstrap,self.builder,self.catalog,self.perimeter,self.engines)
        self.sites=patch('factorio_agent.defense_relocation.native_candidate_survey',return_value={'ok':True,'range':18,'magazine_size':10,
            'candidates':[{'position':{'x':135,'y':-93}},{'position':{'x':146,'y':-111}}]}).start()
        self.check=patch('factorio_agent.defense_relocation.preflight',return_value={'ok':True,'clear_obstacles':[]}).start()
        patch('factorio_agent.defense_relocation.surface_pipe_proof',return_value={'ok':True}).start()
        self.addCleanup(patch.stopall)

    def install(self):
        prepared=self.c.prepare(self.obs);self.assertTrue(prepared['ok'],prepared);self.c.state=prepared;self.c._save()
        return self.c.state['entries'][0]

    def add_new(self,entry,unit=9000,ammo=10):
        entry['new']['unit_number']=unit
        self.perimeter.live[tuple(entry['new']['position'].values())]=dict(unit_number=unit,health=400,max_health=400,
            ammo=ammo,full_magazines=ammo,ammo_rounds=ammo*10)

    def retire_action(self,entry):
        return dict(type='retire_defense_post',transaction_id=self.c.state['id'],post_name=entry['name'],expected_old_unit=entry['old']['unit_number'],
            expected_new_unit=entry['new']['unit_number'],expected_actor_unit=100,expected_world_id='w')


class RelocationControllerTests(RelocationFixture):
    def unrelated_repair(self):
        old=dict(name='plastic',position={'x':173,'y':-38},unit_number=7236,coverage_scope='plastic',
                 commissioned=True,local_walk_access=True)
        self.c.state['initial_posts'].append(old)
        self.perimeter.state['posts'].append(dict(old,unit_number=8426,lost_unit_number=7236,lost_unit_numbers=[7236]))
        self.perimeter.state['coverage_scopes']['plastic']=dict(commissioned=True,new_posts=['plastic'],covering_posts=['plastic'])
        return old,self.perimeter.state['posts'][-1]

    def test_unrelated_normal_repair_is_allowed_but_protected_posts_and_scope_revisions_stay_exact(self):
        self.install();_,repaired=self.unrelated_repair()
        self.assertTrue(_perimeter_matches(self.c.state,self.perimeter.state))
        saved=deepcopy(repaired)
        for change in(dict(position={'x':174,'y':-38}),dict(coverage_scope='different'),dict(local_walk_access=False),
                      dict(lost_unit_number=None,lost_unit_numbers=[]),dict(commissioned=False)):
            repaired.update(change);self.assertFalse(_perimeter_matches(self.c.state,self.perimeter.state))
            repaired.clear();repaired.update(deepcopy(saved))
        protected=self.perimeter.state['posts'][0];old=deepcopy(protected)
        protected.update(unit_number=9999,lost_unit_number=old['unit_number'],lost_unit_numbers=[old['unit_number']])
        self.assertFalse(_perimeter_matches(self.c.state,self.perimeter.state));protected.clear();protected.update(old)
        self.perimeter.state['coverage_scopes'][SCOPE_ID]['effective_range']=17
        self.assertFalse(_perimeter_matches(self.c.state,self.perimeter.state))

    def test_only_commissioned_additions_in_new_unrelated_registered_scopes_are_allowed(self):
        self.install();post=dict(name='bastion1',position={'x':180,'y':-30},unit_number=8500,
            commissioned=True,coverage_scope='plastic-hardening')
        self.perimeter.state['posts'].append(post)
        self.assertFalse(_perimeter_matches(self.c.state,self.perimeter.state))
        self.perimeter.state['coverage_scopes']['plastic-hardening']=dict(commissioned=True,new_posts=['bastion1'],covering_posts=['bastion1'])
        self.assertTrue(_perimeter_matches(self.c.state,self.perimeter.state))
        post['coverage_scope']=SCOPE_ID
        self.assertFalse(_perimeter_matches(self.c.state,self.perimeter.state))

    def test_loss_origin_remains_proven_after_recent_history_rolls_over(self):
        self.install();old,repaired=self.unrelated_repair()
        repaired.update(unit_number=9999,lost_unit_number=9998,lost_unit_numbers=list(range(9983,9999)),lost_unit_origin=7236)
        self.assertTrue(_perimeter_matches(self.c.state,self.perimeter.state))
        repaired['lost_unit_origin']=5555
        self.assertFalse(_perimeter_matches(self.c.state,self.perimeter.state))
        old['lost_unit_origin']=5555
        self.assertTrue(_perimeter_matches(self.c.state,self.perimeter.state))
        repaired['lost_unit_origin']=7236
        self.assertFalse(_perimeter_matches(self.c.state,self.perimeter.state))

    def test_readonly_prepare_pays_two_replacements_and_checks_all_scope_references(self):
        before=self.engines.path.read_bytes();perimeter=self.perimeter.path.read_bytes()
        with patch('factorio_agent.defense_relocation._atomic_json')as write:
            plan=self.c.prepare(self.obs);write.assert_not_called()
        self.assertTrue(plan['ok'],plan);self.assertEqual([e['old']['unit_number']for e in plan['entries']],[7152,7154])
        self.assertEqual(plan['requirements'],{'gun-turret':2,'firearm-magazine':20})
        self.assertTrue(all(set(e['scope_revisions'])=={SCOPE_ID,'also-shared'}for e in plan['entries']))
        self.assertEqual(self.check.call_args.kwargs['ignored_units'],[7152,7154])
        self.assertFalse(self.c.path.exists());self.assertEqual(self.engines.path.read_bytes(),before);self.assertEqual(self.perimeter.path.read_bytes(),perimeter)

    def test_core_build_source_change_and_foreign_post_never_trigger_relocation(self):
        self.engines.state['bindings']['unexpected']=999
        self.assertEqual(self.c.prepare(self.obs)['reason'],'defense_relocation_consumer_bindings_already_present')
        self.engines.state['bindings'].pop('unexpected');self.obs['entities'].append(dict(self.engines.state['core']['machines'][0],unit_number=999))
        self.assertEqual(self.c.prepare(self.obs)['reason'],'defense_relocation_consumer_asset_already_built');self.obs['entities'].pop()
        self.obs['entities'][0]['unit_number']=999
        self.assertEqual(self.c.prepare(self.obs)['reason'],'defense_relocation_fixed_engine_source_changed');self.obs['entities'][0]['unit_number']=7150
        self.obs['entities'].append(dict(entity('gun-turret',134,-95),unit_number=999))
        self.assertEqual(self.c.prepare(self.obs)['reason'],'conflicting_post_is_not_owned_new_engine_scope')

    def test_no_clear_covered_site_or_future_preflight_never_spends(self):
        self.sites.return_value['candidates']=[]
        self.assertEqual(self.c.prepare(self.obs)['reason'],'no_protected_free_replacement_preserves_all_referenced_scopes')
        self.sites.return_value['candidates']=[{'position':{'x':135,'y':-93}},{'position':{'x':146,'y':-111}}]
        self.check.return_value={'ok':False,'reason':'foreign entity'}
        self.assertEqual(self.c.prepare(self.obs)['reason'],'replacement_post_and_engine_whole_preflight_failed')
        self.bootstrap.ensure_item.assert_not_called()

    def test_replacement_must_preserve_an_additional_scope_that_uses_only_that_post(self):
        name=self.perimeter.state['posts'][1]['name'];area=[[134,-110],[136,-109]]
        self.perimeter.state['coverage_scopes']['also-shared']=dict(areas=[area],cells=coverage_cells([area]),
            effective_range=16,covering_posts=[name],new_posts=[],commissioned=True)
        self.assertEqual(self.c.prepare(self.obs)['reason'],'no_protected_free_replacement_preserves_all_referenced_scopes')
        self.bootstrap.ensure_item.assert_not_called()

    def test_both_replacements_require_final_original_engine_and_future_preflight(self):
        self.install();before=self.engines.path.read_bytes()
        for index,entry in enumerate(self.c.state['entries']):
            self.add_new(entry,9000+index);entry['phase']='done'
            self.perimeter.live.pop(tuple(entry['old']['position'].values()))
            self.perimeter.state['posts']=[deepcopy(entry['new'])if p['name']==entry['name']else p for p in self.perimeter.state['posts']]
        def probe(obs,entry):return dict(ok=True,new_unit=entry['new']['unit_number'],old_present=False,retired=True)
        with patch.object(self.c,'_probe',side_effect=probe):
            proof=self.c.proof(self.obs);self.assertEqual(len(proof['relocated']),2)
            self.assertTrue(proof['engine_plan_unchanged']);self.assertEqual(self.engines.path.read_bytes(),before)
            self.check.return_value={'ok':False,'reason':'new future obstruction'}
            self.assertIsNone(self.c.proof(self.obs))

    def test_paid_build_and_ten_full_magazines_precede_publication_and_old_mine(self):
        entry=self.install();probe={'ok':True,'normal_turrets':1,'new_unit':None,'old_present':True,'retired':False,'old_magazines':10,'empty_slots':2}
        with patch.object(self.c,'_probe',side_effect=lambda *args:deepcopy(probe)):
            self.assertEqual(self.c.step(self.obs)['type'],'build_defense_replacement')
            self.add_new(entry,ammo=0);probe['new_unit']=9000
            entry['new'].pop('unit_number');self.c._save()
            self.assertEqual(self.c.step(self.obs)['reason'],'paid_replacement_unit_bound_from_atomic_receipt')
            self.assertEqual(self.c.step(self.obs)['type'],'insert')
            self.assertEqual(next(p for p in self.perimeter.state['posts']if p['name']==entry['name'])['unit_number'],7152)
            self.perimeter.live[tuple(entry['new']['position'].values())].update(ammo=10,full_magazines=10,ammo_rounds=100)
            self.assertEqual(self.c.step(self.obs)['reason'],'paid_armed_replacement_published_before_old_post_recovery')
            self.assertEqual(next(p for p in self.perimeter.state['posts']if p['name']==entry['name'])['unit_number'],9000)
            self.assertIn(tuple(entry['old']['position'].values()),self.perimeter.live)
            self.assertEqual(self.c.step(self.obs)['type'],'retire_defense_post')
            probe.update(retired=True,old_present=False);self.perimeter.live.pop(tuple(entry['old']['position'].values()))
            self.assertEqual(self.c.step(self.obs)['reason'],'old_post_recovered_from_exact_atomic_receipt')

    def test_publication_restart_recovers_old_or_new_metadata_without_retarget(self):
        entry=self.install();self.add_new(entry);entry['phase']='publish'
        entry['publication']={'old_revision':_hash(entry['old']),'new_revision':_hash(entry['new'])};self.c._save()
        original_engine=self.engines.path.read_bytes()
        def interrupted(path,data):
            if Path(path)==self.c.path:raise OSError('interrupted after perimeter publication')
            return _atomic_json(path,data)
        with patch('factorio_agent.defense_relocation._atomic_json',side_effect=interrupted):
            with self.assertRaises(OSError):self.c._publish(self.obs,entry)
        saved=json.loads(self.c.path.read_text());self.assertEqual(saved['entries'][0]['phase'],'publish')
        self.perimeter.state=json.loads(self.perimeter.path.read_text())
        resumed=DefenseRelocation(self.game,self.bootstrap,self.builder,self.catalog,self.perimeter,self.engines)
        entry=resumed.state['entries'][0]
        self.assertEqual(resumed._publish(self.obs,entry)['reason'],'paid_armed_replacement_published_before_old_post_recovery')
        self.assertEqual(entry['phase'],'retire');self.assertEqual(self.engines.path.read_bytes(),original_engine)
        self.assertEqual(len(self.perimeter.state['posts']),4)

    def test_publication_requires_intent_and_blocks_changed_unit_or_scope(self):
        entry=self.install();self.add_new(entry);entry['phase']='publish';self.c._save()
        self.assertEqual(self.c._publish(self.obs,entry)['reason'],'persisted_defense_publication_intent_required')
        entry['publication']={'old_revision':_hash(entry['old']),'new_revision':_hash(entry['new'])};self.c._save()
        disk=json.loads(self.perimeter.path.read_text());disk['posts'][1]['unit_number']=999;self.perimeter.path.write_text(json.dumps(disk));before=self.perimeter.path.read_bytes()
        self.assertEqual(self.c._publish(self.obs,entry)['reason'],'known_defense_publication_identity_changed')
        self.assertEqual(self.perimeter.path.read_bytes(),before)

    def test_action_envelopes_reject_before_io(self):
        valid=dict(type='build_defense_replacement',transaction_id='t',post_name='p',expected_old_unit=1,expected_actor_unit=2,expected_world_id='w',position={'x':1,'y':2})
        for change in({'expected_old_unit':True},{'position':{'x':float('nan'),'y':2}},{'expected_world_id':'other'},{'extra':1}):
            with self.assertRaises(ValueError):execute_build_replacement(self.game,{**valid,**change})
        self.game.query.assert_not_called()


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class NativeRelocationTests(RelocationFixture):
    def setUp(self):
        super().setUp();self.entry=self.install();self.lua=LuaRuntime(unpack_returned_tuples=True)
        def table(v):
            if isinstance(v,dict):return self.lua.table_from({k:table(a)for k,a in v.items()})
            if isinstance(v,list):return self.lua.table_from([table(a)for a in v])
            return v
        self.table=table
        self.lua.globals().helpers=self.lua.table_from({'json_to_table':lambda raw:table(json.loads(raw))})
        self.lua.execute('''game={tick=10};d={world_id="w"};f={name="player"};enemy={name="enemy"};s={};a={valid=true,unit_number=100,force=f,surface=s};defines={inventory={turret_ammo=1,character_ammo=2,character_main=3,character_corpse=3}}
prototypes={item={["firearm-magazine"]={type="ammo",magazine_size=10,stack_size=100},["gun-turret"]={type="item",stack_size=50}},entity={["gun-turret"]={tile_width=2,tile_height=2,attack_parameters={range=18}}}}
function empty()return{valid_for_read=false}end
function stack(name,count,ammo)
 return{valid_for_read=true,name=name,count=count,ammo=ammo or 10,quality={name="normal"},health=1,spoil_tick=0,
  is_item_with_tags=false,is_item_with_inventory=false,is_item_with_entity_data=false,prototype=prototypes.item[name]or{type="item",stack_size=100}}end
main={stack("gun-turret",2),stack("firearm-magazine",30),empty(),empty(),valid=true};filters={}
main.supports_filters=function()return true end;main.get_filter=function(i)return filters[i]end
main.get_item_count=function(q)local n=0;for i=1,#main do local st=main[i];if st.valid_for_read and st.name==q.name and st.quality.name==(q.quality or"normal")then n=n+st.count end end;return n end
main.get_insertable_count=function(q)local n=0;local proto=prototypes.item[q.name]
 for i=1,#main do local st=main[i]
  if st.valid_for_read and st.name==q.name and st.quality.name=="normal"then n=n+proto.stack_size-st.count
  elseif not st.valid_for_read and(not filters[i]or filters[i]==q.name)then n=n+proto.stack_size end
 end;return n end
main.insert=function(q)local left=q.count
 for i=1,#main do local st=main[i];if st.valid_for_read and st.name==q.name and st.quality.name=="normal"then
  local n=math.min(left,prototypes.item[q.name].stack_size-st.count);st.count=st.count+n;left=left-n end end
 for i=1,#main do if left>0 and not main[i].valid_for_read and(not filters[i]or filters[i]==q.name)then
  local n=math.min(left,prototypes.item[q.name].stack_size);main[i]=stack(q.name,n);left=left-n end end
 return q.count-left end
a.get_main_inventory=function()return main end
equipped={empty(),empty(),empty(),valid=true};a.get_inventory=function(index)if index==2 then return equipped elseif index==3 then return main else error("unexpected actor inventory")end end
function key(name,p)return name..":"..p.x..":"..p.y end
world={};next_unit=9000;mined_calls=0;built_calls=0;ore=false;failed_build=false
function turret(p,unit,count)
 local inv={count>0 and stack("firearm-magazine",count)or empty(),valid=true}
 local e={valid=true,name="gun-turret",type="ammo-turret",position=p,unit_number=unit,force=f,surface=s,quality={name="normal"},health=400,max_health=400,ammo_inventory=inv}
 e.get_inventory=function()return inv end;world[key(e.name,p)]=e;return e end
function target(p,name)return world[key(name,p)]end
function belt(q)
 local e={valid=true,name="transport-belt",position=q.position,unit_number=q.unit_number,direction=q.facing,force=f,surface=s}
 world[key(e.name,e.position)]=e end
s.count_entities_filtered=function()return ore and 1 or 0 end
s.can_place_entity=function(q)return not target(q.position,q.name)end
s.create_entity=function(q)if failed_build then return nil end;built_calls=built_calls+1;local e=turret(q.position,next_unit,0);next_unit=next_unit+1;return e end
a.mine_entity=function(e)
 mined_calls=mined_calls+1;for i=1,#e.ammo_inventory do local st=e.ammo_inventory[i];if st.valid_for_read then
  if autoequip then equipped[1]=stack(st.name,st.count,st.ammo)else main.insert{name=st.name,count=st.count,quality="normal"}end end end
 main.insert{name="gun-turret",count=1,quality="normal"};world[key(e.name,e.position)]=nil;e.valid=false;return true end
function load_new(unit,count)
 for _,e in pairs(world)do if e.unit_number==unit then main[2].count=main[2].count-count;e.ammo_inventory[1]=stack("firearm-magazine",count);return end end end
''')
        for post in self.perimeter.state['posts']:self.lua.globals().turret(table(post['position']),post['unit_number'],10)
        for source in(self.iron,self.steel):self.lua.globals().belt(table(source))
        def python(v):
            if not hasattr(v,'items'):return v
            rows=dict(v.items())
            if rows and set(rows)==set(range(1,len(rows)+1)):return [python(rows[i])for i in range(1,len(rows)+1)]
            return {k:python(a)for k,a in rows.items()}
        self.game.query=Mock(side_effect=lambda body:python(self.lua.execute('return(function() '+' '.join(body.splitlines())+' end)()')))
        self.action=dict(type='build_defense_replacement',transaction_id=self.c.state['id'],post_name=self.entry['name'],
            expected_old_unit=self.entry['old']['unit_number'],expected_actor_unit=100,expected_world_id='w',position=self.entry['new']['position'])

    def publish_paid(self):
        built=execute_build_replacement(self.game,self.action);self.assertTrue(built['ok'],built)
        self.entry['new']['unit_number']=built['unit_number'];self.lua.globals().load_new(built['unit_number'],10)
        self.entry['phase']='retire';self.entry['publication']={'old_revision':_hash(self.entry['old']),'new_revision':_hash(self.entry['new'])}
        self.c._save();self.perimeter.state['posts']=[deepcopy(self.entry['new'])if p['name']==self.entry['name']else p for p in self.perimeter.state['posts']]
        self.perimeter._save();return self.retire_action(self.entry)

    def test_native_paid_build_receipt_survives_retry_without_free_turret_or_adoption(self):
        result=execute_build_replacement(self.game,self.action);self.assertTrue(result['ok'],result)
        self.assertEqual(result['paid_turrets'],1);self.assertEqual(self.lua.globals().main[1].count,1)
        replay=execute_build_replacement(self.game,self.action);self.assertTrue(replay['reused_receipt'])
        self.assertEqual(self.lua.globals().built_calls,1);self.assertEqual(self.lua.globals().main[1].count,1)
        marker=self.lua.globals().d.defense_relocations[self.c.state['id']+':'+self.entry['name']]
        self.lua.globals().world['gun-turret:135:-93'].unit_number=999
        self.assertEqual(execute_build_replacement(self.game,self.action)['reason'],'paid_replacement_missing_or_replaced')
        self.assertEqual(marker.unit_number,9000)

    def test_unrelated_repaired_post_requires_exact_native_owned_unit(self):
        old=dict(name='plastic',position={'x':173,'y':-38},unit_number=7236,coverage_scope='plastic',commissioned=True)
        self.c.state['initial_posts'].append(old)
        repaired=dict(old,unit_number=8426,lost_unit_number=7236,lost_unit_numbers=[7236])
        self.perimeter.state['posts'].append(repaired)
        self.perimeter.state['coverage_scopes']['plastic']=dict(commissioned=True,new_posts=['plastic'],covering_posts=['plastic'])
        self.c._save();self.perimeter._save()
        self.lua.globals().turret(self.table(repaired['position']),8426,10)
        self.assertTrue(execute_build_replacement(self.game,self.action)['ok'])
        self.lua.globals().world['gun-turret:173:-38'].force=self.lua.globals().enemy
        self.assertEqual(execute_build_replacement(self.game,self.action)['reason'],'unrelated_repaired_defense_post_native_identity_changed')

    def test_native_no_paid_normal_stock_foreign_unit_actor_world_or_resource_build(self):
        cases=(('main[1].valid_for_read=false','main[1].valid_for_read=true','paid_normal_turret_missing'),
            ('a.unit_number=999','a.unit_number=100','defense_relocation_actor_changed'),
            ('d.world_id="other"','d.world_id="w"','defense_relocation_world_changed'),
            ('ore=true','ore=false','replacement_site_changed'))
        for change,restore,reason in cases:
            self.lua.execute(change);self.assertEqual(execute_build_replacement(self.game,self.action)['reason'],reason);self.lua.execute(restore)
        self.lua.globals().turret(self.table(self.entry['new']['position']),999,10)
        self.assertEqual(execute_build_replacement(self.game,self.action)['reason'],'unexpected_replacement_post_before_paid_build')
        self.assertEqual(self.lua.globals().built_calls,0)

    def test_native_retirement_uses_ordinary_mining_and_conserves_magazines_and_rounds(self):
        action=self.publish_paid();self.lua.execute('main[2].ammo=5')
        result=execute_retire_post(self.game,action);self.assertTrue(result['ok'],result)
        self.assertEqual((result['recovered_turrets'],result['recovered_magazines'],result['recovered_rounds']),(1,10,100))
        self.assertEqual((self.lua.globals().main[1].count,self.lua.globals().main[2].count,self.lua.globals().main[2].ammo),(2,30,5))
        replay=execute_retire_post(self.game,action);self.assertTrue(replay['reused_receipt']);self.assertEqual(self.lua.globals().mined_calls,1)

    def test_native_autoequipped_ammo_is_counted_once_with_actor_partial_rounds_preserved(self):
        action=self.publish_paid();self.lua.execute('autoequip=true;main[2].ammo=5;equipped[2]=stack("firearm-magazine",2,4)')
        result=execute_retire_post(self.game,action);self.assertTrue(result['ok'],result)
        marker=self.lua.globals().d.defense_relocations[self.c.state['id']+':'+self.entry['name']]
        self.assertEqual(marker.retirement_intent.before.magazines,22)
        self.assertEqual(marker.retirement_receipt.after.magazines,32)
        self.assertEqual(marker.retirement_receipt.after.rounds-marker.retirement_intent.before.rounds,100)
        self.assertEqual(marker.retirement_receipt.after.gun_turrets,2)
        self.assertEqual(self.lua.globals().main[2].ammo,5);self.assertEqual(self.lua.globals().equipped[2].ammo,4)
        self.assertEqual(self.lua.globals().mined_calls,1)

    def test_native_inventory_sort_and_stack_merge_preserve_unrelated_materials(self):
        action=self.publish_paid()
        self.lua.execute('''
main[3]=stack("iron-plate",4);main[4]=stack("iron-plate",6);main[5]=empty();main[6]=empty()
local mine=a.mine_entity
a.mine_entity=function(e)local ok=mine(e);main[3]=empty();main[4]=empty();main[6]=stack("iron-plate",10);return ok end
''')
        result=execute_retire_post(self.game,action);self.assertTrue(result['ok'],result)
        self.assertEqual(self.lua.globals().main[6].count,10)

    def test_native_persisted_intent_resumes_missing_old_without_mining_twice(self):
        action=self.publish_paid();self.lua.execute('autoequip=true')
        self.assertTrue(execute_retire_post(self.game,action)['ok'])
        marker=self.lua.globals().d.defense_relocations[self.c.state['id']+':'+self.entry['name']]
        marker.retired=None;marker.retirement_receipt=None
        resumed=execute_retire_post(self.game,action);self.assertTrue(resumed['ok'],resumed)
        self.assertTrue(resumed['recovered_from_intent']);self.assertEqual(self.lua.globals().mined_calls,1)
        marker.retired=None;marker.retirement_receipt=None;self.lua.globals().main[1].count+=1
        self.assertEqual(execute_retire_post(self.game,action)['reason'],'retirement_intent_conservation_changed')
        self.assertEqual(self.lua.globals().mined_calls,1)

    def test_native_intent_precedes_mine_and_changed_before_inventory_blocks_retry(self):
        action=self.publish_paid();marker=self.lua.globals().d.defense_relocations[self.c.state['id']+':'+self.entry['name']]
        self.lua.execute('a.mine_entity=function(old)return false end')
        self.assertEqual(execute_retire_post(self.game,action)['reason'],'ordinary_defense_post_mining_failed')
        self.assertEqual(marker.retirement_intent.old_unit,self.entry['old']['unit_number'])
        self.assertEqual(marker.retirement_intent.before.gun_turrets,1)
        self.lua.execute('main[3]=stack("stone",1)')
        self.assertEqual(execute_retire_post(self.game,action)['reason'],'retirement_intent_before_inventory_or_stock_changed')
        self.assertEqual(self.lua.globals().mined_calls,0)

    def test_unknown_missing_old_is_never_adopted_without_native_intent(self):
        action=self.publish_paid();self.lua.execute('world["gun-turret:135:-95"]=nil')
        self.assertEqual(execute_retire_post(self.game,action)['reason'],'old_post_missing_without_retirement_intent')
        self.assertEqual(self.c._probe(self.obs,self.entry)['reason'],'old_post_missing_without_retirement_receipt')
        self.assertEqual(self.lua.globals().mined_calls,0)

    def legacy_case(self):
        action=self.publish_paid();action['reason']='normally recover exact old post after paid armed replacement coverage'
        self.lua.execute('autoequip=true');self.assertTrue(execute_retire_post(self.game,action)['ok'])
        marker=self.lua.globals().d.defense_relocations[self.c.state['id']+':'+self.entry['name']]
        for k in('retired','retirement_intent','retirement_receipt','recovered_magazines','recovered_rounds'):marker[k]=None
        events=[dict(key='engine-job',event='action_intent',tick=10,action=action),
            dict(key='engine-job',event='action_result',tick=10,result=dict(ok=False,status='blocked',reason='post_recovery_count_or_round_conservation_failed'))]
        self.events=self.root/'job-events.jsonl';self.events.write_text(''.join(json.dumps(e)+'\n'for e in events))
        self.add_new(self.entry);return action,marker

    def test_legacy_readonly_proposal_requires_explicit_review_and_preserves_honest_receipt(self):
        _,marker=self.legacy_case();before=self.c.path.read_bytes()
        proposal=self.c.prepare_legacy_retirement_reconciliation(self.obs,'engine-job')
        self.assertTrue(proposal['ok'],proposal);self.assertEqual(self.c.path.read_bytes(),before)
        self.assertFalse(proposal['baseline_available']);self.assertFalse(proposal['recovered_counts_available'])
        self.assertFalse(marker.retired);self.assertEqual(self.c.step(self.obs)['reason'],'old_post_missing_without_retirement_receipt')
        self.entry['legacy_retirement_reconciliation']=proposal['intent'];self.c._save()
        restarted=DefenseRelocation(self.game,self.bootstrap,self.builder,self.catalog,self.perimeter,self.engines)
        action=restarted.step(self.obs);self.assertEqual(action['type'],'reconcile_defense_post_retirement')
        result=execute_reconcile_retirement(self.game,action);self.assertTrue(result['ok'],result)
        self.assertTrue(marker.retired);self.assertFalse(marker.retirement_receipt.baseline_available)
        self.assertIsNone(marker.recovered_magazines);self.assertEqual(self.lua.globals().mined_calls,1)
        self.assertTrue(execute_reconcile_retirement(self.game,action)['reused_receipt'])

    def test_legacy_duplicate_pair_changed_actor_or_evidence_blocks_without_receipt(self):
        _,marker=self.legacy_case();proposal=self.c.prepare_legacy_retirement_reconciliation(self.obs,'engine-job')
        self.assertTrue(proposal['ok'],proposal)
        self.events.write_text(self.events.read_text()*2)
        self.assertFalse(self.c.prepare_legacy_retirement_reconciliation(self.obs,'engine-job')['ok'])
        self.events.write_text('\n'.join(self.events.read_text().splitlines()[:2])+'\n')
        self.entry['legacy_retirement_reconciliation']=proposal['intent'];self.c._save();action=self.c.step(self.obs)
        self.lua.execute('main[3]=stack("stone",1)')
        self.assertEqual(execute_reconcile_retirement(self.game,action)['reason'],'legacy_actor_inventory_changed_after_review')
        self.assertFalse(marker.retired);self.assertEqual(self.lua.globals().mined_calls,1)
        self.entry['legacy_retirement_reconciliation']['baseline_available']=True;self.c._save()
        self.game.query.reset_mock()
        with self.assertRaises(ValueError):execute_reconcile_retirement(self.game,action)
        self.game.query.assert_not_called()

    def test_native_never_mines_before_ten_full_replacement_and_all_scope_coverage(self):
        action=self.publish_paid();new=self.lua.globals().world['gun-turret:135:-93']
        new.ammo_inventory[1].count=9
        self.assertEqual(execute_retire_post(self.game,action)['reason'],'replacement_requires_ten_full_normal_magazines');new.ammo_inventory[1].count=10
        other=self.lua.globals().world['gun-turret:127:-116'];other.ammo_inventory[1].count=4
        self.assertEqual(execute_retire_post(self.game,action)['reason'],'referencing_scope_post_unhealthy');other.ammo_inventory[1].count=10
        new.position.x=140
        self.assertFalse(execute_retire_post(self.game,action)['ok']);new.position.x=135
        self.assertEqual(self.lua.globals().mined_calls,0)

    def test_native_partial_old_magazine_metadata_and_aggregate_capacity_fail_before_mine(self):
        action=self.publish_paid();old=self.lua.globals().world['gun-turret:135:-95'];stack=old.ammo_inventory[1]
        for field,value,restore in(('ammo',5,10),('is_item_with_tags',True,False)):
            stack[field]=value;self.assertEqual(execute_retire_post(self.game,action)['reason'],'old_post_partial_or_nonplain_ammo_requires_explicit_handling');stack[field]=restore
        self.lua.execute('main[1]=stack("stone",100);main[2]=stack("stone",100);main[3]=stack("stone",100)')
        self.assertEqual(execute_retire_post(self.game,action)['reason'],'aggregate_post_recovery_inventory_capacity_missing')
        self.assertEqual(self.lua.globals().mined_calls,0)

    def test_native_scripts_are_flattenable_and_never_set_ammo_health_or_research(self):
        for script in(BUILD_REPLACEMENT_LUA,RETIRE_POST_LUA,RECONCILE_RETIREMENT_LUA):
            self.assertNotIn('--',script)
            self.assertIsNone(re.search(r'\.(?:ammo|health)\s*=(?!=)',script),'script must not set ammo or health')
            for forbidden in('set_research','insert_fluid','set_fluid'):
                self.assertNotIn(forbidden,script)


if __name__=='__main__':unittest.main()
