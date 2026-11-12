from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.board import entity
from factorio_agent.chemical_production import source_owned_apron
from factorio_agent.gas_distribution import ALIASES,CONSUMER_ID,GAS_PROOF_LUA,GasDistribution,_key,gas_manifold_core

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


def catalog():return SimpleNamespace(fingerprint='c',recipes={'basic-oil-processing':{
    'category':'oil-processing','energy':5,'ingredients':[{'type':'fluid','name':'crude-oil','amount':100}],
    'products':[{'type':'fluid','name':'petroleum-gas','amount':45}]}},entities={'oil-refinery':{
    'crafting_speed':1,'energy_usage':7000,'crafting_categories':['oil-processing']}})


class GasDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.cat=catalog();self.game=SimpleNamespace(expected_world='w',cfg=SimpleNamespace(runtime_dir=self.root),query=Mock())
        self.game.query.return_value={'ok':True,'extent':{'left_top':{'x':129,'y':-62},'right_bottom':{'x':151,'y':-61}},'limit':320}
        self.builder=SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,
            ensure_plan=Mock(return_value={'type':'craft','recipe':'pipe','count':13}))
        self.perimeter=SimpleNamespace(prepare_coverage=Mock(return_value={'ok':True,'new_posts':[]}),
            ensure_coverage=Mock(return_value=None),coverage_proof=Mock(return_value={'complete':True}))
        self.source={'kind':'fluid','item':'petroleum-gas','name':'pipe','position':{'x':150.5,'y':-61.5},
            'direction':'output','facing':4,'fluidbox_index':1,'world_id':'w','unit_number':6963,'defense_scope_id':'petroleum'}
        self.producer={'entities':[entity('pipe',150.5,-61.5)],'ports':[deepcopy(self.source)],
            'reserved_area':[[123,-63],[153,-51]],'defense_scope_id':'petroleum'}
        self.petroleum=SimpleNamespace(state={'world_id':'w','catalog_fingerprint':'c','phase':'verified',
            'plan':self.producer,'core':{'machines':[entity('oil-refinery',128.5,-57.5)]}},last_proof_failure=None,
            proof=Mock(return_value={'native':{'refineries':[{'unit_number':6111}]}}))
        self.registry={'world_id':'w','catalog_fingerprint':'c','sources':{'petroleum-gas':self.source,'water':{'unit_number':9}}}
        (self.root/'fluid-sources.json').write_text(json.dumps(self.registry))
        (self.root/'pcb-board.json').write_text(json.dumps({'world_id':'w','catalog_fingerprint':'c','blocks':{'basic_petroleum':self.producer}}))
        self.c=GasDistribution(self.game,self.builder,self.cat,self.perimeter,petroleum=self.petroleum)
        self.snapshot=patch.object(self.c,'_source_snapshot',return_value=deepcopy(self.game.query.return_value)).start()
        self.obs={'ok':True,'world_id':'w','tick':10,'entities':[dict(entity('pipe',150.5,-61.5),unit_number=6963)]}
        self.survey=patch('factorio_agent.gas_distribution.survey_slots',return_value={'ok':True,'legal':[1]}).start()
        self.preflight=patch('factorio_agent.gas_distribution.preflight',return_value={'ok':True,'clear_obstacles':[]}).start()
        self.isolation=patch('factorio_agent.gas_distribution.GasDistribution._isolation',return_value={'ok':True}).start()
        def route(game,builder,obs,source,destination,core,prior):
            es=[entity('pipe',x,-61.5)for x in(151.5,152.5,153.5)]
            return {'ok':True,'entities':es,'first_neighbor':dict(es[0],fluidbox_index=1),
                'underground_pairs':[],'maximum_underground_span':10,'route_metrics':{'length':3,'turns':0}}
        self.route=patch('factorio_agent.gas_distribution.plan_typed_fluid_connection',side_effect=route).start()
        self.handoff=patch('factorio_agent.gas_distribution._handoff',return_value={'ok':True}).start()
        self.addCleanup(patch.stopall)

    def install(self):
        self.c.state=self.c.prepare(self.obs);self.assertTrue(self.c.state['ok'],self.c.state)
        self.obs['entities'] += [dict(e,unit_number=i+100)for i,e in enumerate(self.c.state['plan']['entities'])]
        self.assertIsNone(self.c._bind(self.obs))
        first=dict(self.c.state['first_neighbor'],unit_number=self.c.state['bindings'][_key(self.c.state['first_neighbor'])])
        registry=deepcopy(self.registry);registry['handoffs']={CONSUMER_ID:{'petroleum-gas':{
            'world_id':'w','catalog_fingerprint':'c','source':self.source,'first_neighbor':first,'phase':'bound'}}}
        self.c.sources_path.write_text(json.dumps(registry))

    def test_pure_core_has_two_fixed_distinct_normal_ports_and_three_clear_exit_cells(self):
        core=gas_manifold_core(153.5,-61.5);self.assertEqual(core['required_items'],{'pipe':11})
        self.assertEqual([p['facing']for p in core['ports']],[4,0])
        geometry=self.builder._occupied_by_plan(core['entities'])
        for name,source in zip(ALIASES,core['ports']):
            self.assertTrue(all((p['x'],p['y'])not in geometry for p in core['alias_access'][name]))
            source=dict(source,defense_scope_id='gas-distribution',unit_number=1,world_id='w')
            block=dict(core,defense_scope_id='gas-distribution')
            self.assertEqual(len(source_owned_apron(self.builder,source,[block])['path']),4)

    def test_prepare_is_readonly_keeps_original_port_and_one_total_capacity(self):
        before={p.name:p.read_bytes()for p in self.root.glob('*.json')}
        with patch('factorio_agent.gas_distribution._atomic_json')as write:
            result=self.c.prepare(self.obs);write.assert_not_called()
        self.assertTrue(result['ok'],result);self.assertEqual(result['plan']['required_items'],{'pipe':13})
        self.assertEqual(result['original_source'],self.source);self.assertEqual(result['shared_nominal_gas_per_minute'],540)
        self.assertEqual(result['first_neighbor']['position'],{'x':151.5,'y':-61.5})
        self.assertEqual({p.name:p.read_bytes()for p in self.root.glob('*.json')},before);self.assertEqual(self.c.state,{})
        self.builder.ensure_plan.assert_not_called();self.handoff.assert_not_called()

    def test_original_chemical_plan_or_owner_blocks_manifold_before_any_cost(self):
        self.registry['handoffs']={'chemical:plastic-bar':{'petroleum-gas':{'source':self.source}}}
        self.c.sources_path.write_text(json.dumps(self.registry))
        self.assertEqual(self.c.prepare(self.obs)['reason'],'original_gas_edge_already_owned')
        self.registry.pop('handoffs');self.c.sources_path.write_text(json.dumps(self.registry))
        (self.root/'chemical-production.json').write_text(json.dumps({'world_id':'w','catalog_fingerprint':'c',
            'products':{'plastic-bar':{'sources':{'petroleum-gas':self.source}}}}))
        self.assertEqual(self.c.prepare(self.obs)['reason'],'original_gas_already_owned_by_chemical_plan')
        self.game.query.assert_not_called();self.builder.ensure_plan.assert_not_called()

    def test_existing_alias_or_orphan_main_handoff_is_not_adopted(self):
        for key in ALIASES:
            registry=deepcopy(self.registry);registry['sources'][key]={'unit_number':999}
            self.c.sources_path.write_text(json.dumps(registry))
            self.assertEqual(self.c.prepare(self.obs)['reason'],'gas_alias_exists_without_manifold_checkpoint')
        registry=deepcopy(self.registry);registry['handoffs']={CONSUMER_ID:{'petroleum-gas':{'source':self.source}}}
        self.c.sources_path.write_text(json.dumps(registry))
        self.assertEqual(self.c.prepare(self.obs)['reason'],'gas_handoff_exists_without_manifold_checkpoint')

    def test_accepted_engine_unbuilt_assets_and_whole_core_are_hard_planning_inputs(self):
        planned=entity('transport-belt',154.5,-70.5,0)
        (self.root/'engine-production.json').write_text(json.dumps({'world_id':'w','catalog_fingerprint':'c',
            'plan':{'entities':[planned]},'core':{'reserved_area':[[114,-115],[145,-96]]}}))
        result=self.c.prepare(self.obs);self.assertTrue(result['ok'],result)
        hard=self.route.call_args.args[6];self.assertIn(planned,hard)
        self.assertTrue(any(e['name']=='pcb-reserved'for e in hard))

    def test_defense_handoff_and_fresh_whole_preflight_gate_every_paid_build(self):
        self.perimeter.ensure_coverage.return_value={'type':'craft','recipe':'firearm-magazine','count':10}
        self.assertEqual(self.c.step(self.obs)['recipe'],'firearm-magazine');self.handoff.assert_not_called();self.builder.ensure_plan.assert_not_called()
        self.perimeter.ensure_coverage.return_value=None;self.handoff.return_value={'ok':False,'reason':'edge_busy'}
        self.assertEqual(self.c.step(self.obs)['reason'],'gas_manifold_handoff_not_authorized');self.builder.ensure_plan.assert_not_called()
        self.handoff.return_value={'ok':True};self.preflight.return_value={'ok':False,'reason':'new_defense_clips_route'}
        self.assertEqual(self.c.step(self.obs)['reason'],'gas_manifold_site_changed_after_defense');self.builder.ensure_plan.assert_not_called()
        self.snapshot.return_value={'ok':False,'reason':'original_gas_source_identity_changed'}
        self.assertEqual(self.c.step(self.obs)['reason'],'gas_manifold_original_source_guard_failed');self.builder.ensure_plan.assert_not_called()

    def test_lost_or_replaced_known_pipe_blocks_reconstruction(self):
        self.install();self.obs['entities'][1]['unit_number']=999
        self.assertEqual(self.c.step(self.obs)['reason'],'gas_manifold_asset_missing_or_replaced')
        self.builder.ensure_plan.assert_not_called()

    def test_underground_landing_replaces_only_prospective_inlet_and_protects_crossed_belt(self):
        belt=entity('transport-belt',154.5,-66.5,0);self.obs['entities'].append(dict(belt,unit_number=7000))
        def crossing(game,builder,obs,source,destination,core,prior):
            if destination['position']!={'x':155.5,'y':-66.5}:return {'ok':False,'reason':'fixture_requires_north_alternative'}
            es=[entity('pipe',x,y)for x,y in((151.5,-61.5),(152.5,-61.5),(153.5,-61.5),(153.5,-62.5),
                (153.5,-63.5),(152.5,-63.5),(152.5,-64.5),(152.5,-65.5),(152.5,-66.5))]
            es += [entity('pipe-to-ground',153.5,-66.5,12),entity('pipe-to-ground',155.5,-66.5,4)]
            return {'ok':True,'entities':es,'first_neighbor':dict(es[0],fluidbox_index=1),'maximum_underground_span':10,
                'underground_pairs':[{'first':[153.5,-66.5],'last':[155.5,-66.5],'direction':4,'span':2}],
                'route_metrics':{'length':12,'turns':4}}
        self.route.side_effect=crossing;result=self.c.prepare(self.obs);self.assertTrue(result['ok'],result)
        landing=[e for e in result['plan']['entities']if e['position']=={'x':155.5,'y':-66.5}]
        self.assertEqual(len(landing),1);self.assertEqual(landing[0]['name'],'pipe-to-ground')
        self.assertEqual(result['plan']['required_items']['pipe-to-ground'],2)
        self.assertEqual(result['crossed_assets'],[dict(belt,unit_number=7000)])
        self.c.state=result;self.game.query.return_value={'ok':False,'reason':'gas_manifold_crossed_asset_changed'}
        self.assertEqual(self.c.step(self.obs)['reason'],'gas_manifold_crossed_asset_guard_failed')
        self.builder.ensure_plan.assert_not_called()

    def test_budget_and_missing_verified_upstream_count_fail_without_persisting(self):
        with patch('factorio_agent.gas_distribution.PlanningBudget')as budget_type:
            budget_type.return_value=SimpleNamespace(game=self.game,expired=lambda:True,
                failure=lambda **extra:dict(ok=False,reason='industrial_planning_budget_exhausted',**extra))
            result=self.c.prepare(self.obs)
        self.assertEqual(result['reason'],'industrial_planning_budget_exhausted');self.assertFalse(self.c.path.exists())
        self.petroleum.proof.return_value['native']['refineries']=[]
        self.assertEqual(self.c.prepare(self.obs)['reason'],'gas_upstream_verified_refinery_count_unavailable')
        self.petroleum.state['core']['machines'].append(entity('oil-refinery',136.5,-57.5))
        self.petroleum.proof.return_value['native']['refineries']=[{'unit_number':6111},{'unit_number':6111}]
        self.assertEqual(self.c.prepare(self.obs)['reason'],'gas_upstream_verified_refinery_count_unavailable')
        self.builder.ensure_plan.assert_not_called();self.assertFalse(self.c.path.exists())

    def test_publication_requires_both_gas_outputs_preserves_canonical_and_stable_alias_descriptors(self):
        self.install();self.c.state['construction_authorized']=True;self.builder.ensure_plan.return_value={'status':'succeeded'}
        self.game.query.return_value={'ok':False,'reason':'gas_manifold_alias_empty'}
        self.assertEqual(self.c.step(self.obs)['status'],'waiting')
        self.assertNotIn('plastic-gas',json.loads(self.c.sources_path.read_text())['sources'])
        self.game.query.return_value={'ok':True,'tick':20,'alias_amounts':{'plastic-gas':10,'sulfur-gas':10}}
        self.assertEqual(self.c.step(self.obs)['status'],'succeeded')
        saved=json.loads(self.c.sources_path.read_text())['sources'];self.assertEqual(saved['petroleum-gas'],self.source)
        self.assertEqual(saved['water'],self.registry['sources']['water'])
        self.assertNotEqual(saved['plastic-gas']['unit_number'],saved['sulfur-gas']['unit_number'])
        self.assertTrue(all('capacity'not in key and 'rate'not in key for name in ALIASES for key in saved[name]))
        aliases={name:deepcopy(saved[name])for name in ALIASES}
        self.petroleum.state['core']['machines'].append(entity('oil-refinery',136.5,-57.5))
        self.petroleum.proof.return_value['native']['refineries'].append({'unit_number':6112})
        result=self.c.step(self.obs);self.assertEqual(result['evidence']['shared_nominal_gas_per_minute'],1080)
        self.assertEqual({name:json.loads(self.c.sources_path.read_text())['sources'][name]for name in ALIASES},aliases)

    @unittest.skipUnless(LuaRuntime,'optional Lua runtime: pip install lupa')
    def test_native_lua_exact_unit_reciprocal_purity_extent_and_both_aliases(self):
        self.install();lua=LuaRuntime(unpack_returned_tuples=True)
        def table(v):
            if isinstance(v,dict):return lua.table_from({k:table(a)for k,a in v.items()})
            if isinstance(v,list):return lua.table_from([table(a)for a in v])
            return v
        def decode(v):return{k:decode(a)for k,a in v.items()}if hasattr(v,'items')else v
        bindings={**self.c.state['bindings'],_key(self.source):6963}
        lua.globals().nodes=table([dict(e,unit_number=bindings[_key(e)])for e in self.c.state['nodes']])
        lua.globals().edges=table(self.c.state['edges']);lua.globals().helpers=lua.table_from({'json_to_table':lambda raw:table(json.loads(raw))})
        lua.execute('''f={};s={};game={tick=20};prototypes={utility_constants={default_pipeline_extent=320}};entities={};one_way=false;contaminated=false;empty=false;oversize=false;foreign_peer=false
local function key(q)return q.name..":"..q.position.x..":"..q.position.y end
for _,e in ipairs(nodes)do entities[key(e)]=e end
function setup(e)
 e.force=f;e.surface=s;e.has_fluid_segment=function()return true end
 e.get_fluid_segment_extent_bounding_box=function()return{left_top={x=0,y=0},right_bottom={x=oversize and 321 or 20,y=4}}end
 e.get_fluid_contents=function()if contaminated then return{water=1}end;if empty then return{}end;return{["petroleum-gas"]=10}end
 e.get_fluid_box_neighbours=function()local peers={};for _,edge in ipairs(edges)do
  if key(edge.a)==key(e)then peers[#peers+1]={entity=entities[key(edge.b)],index=1}end
  if not one_way and key(edge.b)==key(e)then peers[#peers+1]={entity=entities[key(edge.a)],index=1}end end
  if foreign_peer then peers[#peers+1]={entity={unit_number=999},index=1}end;return peers end
end
for _,e in ipairs(nodes)do setup(e)end
function target(p,name)return entities[name..":"..p.x..":"..p.y]end''')
        self.game.query.side_effect=lambda body:decode(lua.execute('return(function() '+' '.join(body.splitlines())+' end)()'))
        self.assertIsNotNone(self.c.proof(self.obs),self.c.last_proof_failure)
        for change,restore in(('one_way=true','one_way=false'),('contaminated=true','contaminated=false'),
            ('empty=true','empty=false'),('oversize=true','oversize=false'),('foreign_peer=true','foreign_peer=false')):
            lua.execute(change);self.assertIsNone(self.c.proof(self.obs),change);lua.execute(restore)
        node=lua.globals().nodes[2];old=node.unit_number;node.unit_number=999
        self.assertIsNone(self.c.proof(self.obs));node.unit_number=old
        # A later consumer is accepted only after its exact alias edge is bound.
        before=self.c.proof(self.obs);alias=before['sources']['plastic-gas']
        neighbor=dict(entity('pipe',alias['position']['x']+1,alias['position']['y']),unit_number=9000,fluidbox_index=1)
        lua.globals().consumer=table(neighbor);lua.globals().alias_entity=lua.globals().entities[_key(alias)]
        lua.execute('''setup(consumer);entities[consumer.name..":"..consumer.position.x..":"..consumer.position.y]=consumer
local base=alias_entity.get_fluid_box_neighbours
alias_entity.get_fluid_box_neighbours=function()local rows=base();rows[#rows+1]={entity=consumer,index=1};return rows end
consumer.get_fluid_box_neighbours=function()return{{entity=alias_entity,index=1}}end''')
        self.assertIsNone(self.c.proof(self.obs))
        registry=json.loads(self.c.sources_path.read_text());registry['sources'].update(before['sources'])
        registry['handoffs']['chemical:plastic-bar']={'petroleum-gas':{'world_id':'w','catalog_fingerprint':'c',
            'phase':'bound','source':alias,'first_neighbor':neighbor}}
        self.c.sources_path.write_text(json.dumps(registry))
        self.assertIsNotNone(self.c.proof(self.obs),self.c.last_proof_failure)
        lua.globals().consumer.unit_number=9001;self.assertIsNone(self.c.proof(self.obs))
        for forbidden in('insert_fluid','set_fluid','create_entity','.researched='):
            self.assertNotIn(forbidden,GAS_PROOF_LUA)


if __name__=='__main__':unittest.main()
