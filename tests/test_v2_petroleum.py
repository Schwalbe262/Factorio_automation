from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_ai.deterministic_builder import FactoryBuilder,_plan
from factorio_agent.board import entity
from factorio_agent.petroleum import (BasicPetroleum,PETROLEUM_PROOF_LUA,_fluid_edges,_key,
    basic_petroleum_rates,compile_pipe_crossings,fluid_plan_isolation,pipeline_extent,
    plan_crude_connection,recipe_fluid_port,refinery_core)
from factorio_agent.routing import expand,metrics,VECTORS

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


def catalog():
    boxes=[]
    for index,(x,y)in enumerate(((-1,2),(1,2),(-2,-2),(0,-2),(2,-2)),1):
        positions=[]
        for rotation in range(4):positions.append({'x':x,'y':y});x,y=-y,x
        boxes.append({'index':index,'production_type':'input'if index<3 else'output',
            'pipe_connections':[{'connection_type':'normal','direction':8 if index<3 else 0,'positions':positions}]})
    return SimpleNamespace(fingerprint='c',recipes={'basic-oil-processing':{
        'energy':5,'category':'oil-processing','ingredients':[{'name':'crude-oil','type':'fluid','amount':100,'fluidbox_index':2}],
        'products':[{'name':'petroleum-gas','type':'fluid','amount':45,'fluidbox_index':3}]}},
        entities={'oil-refinery':{'crafting_speed':1,'energy_usage':7000,'crafting_categories':['oil-processing'],
            'selection_box':{'left_top':{'x':-2.5,'y':-2.5},'right_bottom':{'x':2.5,'y':2.5}},'fluidbox_prototypes':boxes}})


def route(vertices):
    path=expand(vertices);segments=[]
    for i,p in enumerate(path):
        a,b=(path[i],path[i+1])if i+1<len(path)else(path[i-1],path[i])
        d=next(d for d,v in VECTORS.items()if v==(b[0]-a[0],b[1]-a[1]))
        segments.append({'position':{'x':p[0],'y':p[1]},'direction':d})
    return {'ok':True,'segments':segments,'path':[{'x':x,'y':y}for x,y in path],'metrics':metrics(path)}


class PetroleumGeometryTests(unittest.TestCase):
    def test_actual_recipe_output_ordinal_maps_to_physical_fifth_box(self):
        c=catalog()
        self.assertEqual(recipe_fluid_port(c,'basic-oil-processing','crude-oil')['fluidbox_index'],2)
        port=recipe_fluid_port(c,'basic-oil-processing','petroleum-gas')
        self.assertEqual((port['recipe_ordinal'],port['fluidbox_index'],port['offset']),(3,5,{'x':2,'y':-3}))
        self.assertEqual(recipe_fluid_port(c,'basic-oil-processing','petroleum-gas',4)['offset'],{'x':3,'y':2})

    def test_capacity_is_catalog_nominal_and_missing_water_is_not_invented(self):
        c=catalog();r=basic_petroleum_rates(c)
        self.assertEqual((r['crude_per_minute'],r['petroleum_per_minute'],r['electric_kw']),(1200,540,420))
        c.recipes['basic-oil-processing']['ingredients'].append({'type':'fluid','name':'water','amount':1})
        with self.assertRaises(ValueError):refinery_core(c,.5,.5)

    def test_expansion_preserves_fixed_ports_headers_and_all_old_assets_without_overlap(self):
        c=catalog();g=FactoryBuilder.__new__(FactoryBuilder);small=refinery_core(c,.5,.5)
        for count in(1,2,3):
            p=refinery_core(c,.5,.5,count);boxes=[g._occupied_by_plan([e])for e in p['entities']]
            self.assertEqual(sum(map(len,boxes)),len(set().union(*boxes)))
            self.assertEqual(p['ports'],small['ports']);self.assertEqual(p['reserved_area'],small['reserved_area'])
            self.assertTrue(all(e in p['entities']for e in small['entities']))
        self.assertEqual(small['required_items'],{'pipe':46,'small-electric-pole':2,'oil-refinery':1})

    def test_underground_mouths_face_the_surface_on_opposite_sides_of_straight_crossing(self):
        r=route([(.5,.5),(8.5,.5)]);p=compile_pipe_crossings(r,{(4.5,.5)},10)
        self.assertTrue(p['ok'],p)
        mouths=[e for e in p['entities']if e['name']=='pipe-to-ground']
        self.assertEqual([e['direction']for e in mouths],[12,4])
        self.assertEqual(p['underground_pairs'][0]['span'],2)
        self.assertNotIn((4.5,.5),{(e['position']['x'],e['position']['y'])for e in p['entities']})
        self.assertEqual(compile_pipe_crossings(r,{(4.5,.5)},1)['reason'],'pipe_crossing_requires_clear_bounded_mouths')

    def test_buried_corner_or_endpoint_crossing_is_rejected(self):
        corner=route([(.5,.5),(4.5,.5),(4.5,4.5)])
        self.assertFalse(compile_pipe_crossings(corner,{(4.5,.5)},10)['ok'])
        self.assertFalse(compile_pipe_crossings(corner,{(1.5,.5)},10)['ok'])

    def test_extent_uses_existing_native_segment_bbox_and_not_total_path_length(self):
        box={'left_top':{'x':0,'y':-304},'right_bottom':{'x':71,'y':-303}}
        self.assertTrue(pipeline_extent(box,[entity('pipe',104.5,-40.5)],320)['ok'])
        self.assertEqual(pipeline_extent(box,[entity('pipe',104.5,20.5)],320)['reason'],'pipeline_requires_explicit_pump_segment')
        self.assertFalse(pipeline_extent(None,[entity('pipe',1,1)],320)['ok'])

    def test_prospective_gas_crude_contact_is_rejected_before_native_io(self):
        core=refinery_core(catalog(),.5,.5);game=SimpleNamespace(query=Mock())
        bad=_plan([entity('pipe',2.5,-4.5)],[])
        self.assertEqual(fluid_plan_isolation(game,{'position':{'x':-10.5,'y':5.5}},core,bad)['reason'],
                         'prospective_crude_petroleum_networks_touch')
        game.query.assert_not_called()

    def test_real_planner_compiles_soft_crossing_and_never_buries_a_resource_cell(self):
        with tempfile.TemporaryDirectory()as root:
            Path(root,'pcb-board.json').write_text(json.dumps({'world_id':'w','blocks':{}}))
            game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root),query=Mock(side_effect=[
                {'ok':True,'spans':[10]},{'ok':True,'hard':[],'soft':[{'x':10.5,'y':.5}]}]))
            builder=SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan)
            source={'position':{'x':.5,'y':.5},'unit_number':1,'facing':4}
            target={'position':{'x':20.5,'y':.5},'facing':4}
            core={'entities':[entity('pipe',20.5,.5)]}
            result=plan_crude_connection(game,builder,{'world_id':'w'},source,target,core)
            self.assertTrue(result['ok'],result);self.assertEqual(len(result['underground_pairs']),1)
            self.assertNotIn({'x':.5,'y':.5},[e['position']for e in result['entities']])
            game.query.side_effect=[{'ok':True,'spans':[10]},{'ok':True,'hard':[{'x':10.5,'y':.5}],'soft':[]}]
            result=plan_crude_connection(game,builder,{'world_id':'w'},source,target,core)
            self.assertTrue(result['ok'],result)
            self.assertNotIn({'x':10.5,'y':.5},result['full_path'])
            self.assertLessEqual(result['route_metrics']['turns'],6)
            self.assertLessEqual(result['route_metrics']['length'],1.35*20+4)

    def test_real_planner_keeps_future_refinery_slots_empty_except_typed_entry(self):
        with tempfile.TemporaryDirectory()as root:
            Path(root,'pcb-board.json').write_text(json.dumps({'world_id':'w','blocks':{}}))
            game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root),query=Mock(side_effect=[
                {'ok':True,'spans':[10]},{'ok':True,'hard':[],'soft':[]}]))
            builder=SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan)
            core=refinery_core(catalog(),.5,.5);source={'position':{'x':-10.5,'y':-10.5},'unit_number':1,'facing':4}
            result=plan_crude_connection(game,builder,{'world_id':'w'},source,core['ports'][0],core)
            self.assertTrue(result['ok'],result)
            area=core['reserved_area'];port=core['ports'][0]['position']
            for p in result['full_path']:
                if area[0][0]<p['x']<area[1][0]and area[0][1]<p['y']<area[1][1]:
                    self.assertEqual(p['y'],port['y']);self.assertLessEqual(p['x'],port['x'])


class PetroleumControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.cata=catalog();self.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root),query=Mock(return_value={'ok':True}))
        self.builder=SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,
            ensure_plan=Mock(return_value={'type':'craft','recipe':'oil-refinery','count':1}))
        self.perimeter=SimpleNamespace(prepare_coverage=Mock(return_value={'ok':True}),ensure_coverage=Mock(return_value=None),
                                      coverage_proof=Mock(return_value={'complete':True}))
        self.c=BasicPetroleum(self.game,self.builder,self.cata,self.perimeter)
        self.obs={'ok':True,'world_id':'w','tick':10,'entities':[],'technologies':{'oil-processing':True},
                  'enabled_recipes':{'oil-refinery':True,'basic-oil-processing':True}}
        self.source={'kind':'fluid','item':'crude-oil','name':'pipe','position':{'x':.5,'y':-30.5},
            'direction':'output','facing':4,'fluidbox_index':1,'world_id':'w','unit_number':1,'defense_scope_id':'oil-well'}
        self.registry={'world_id':'w','catalog_fingerprint':'c','sources':{'crude-oil':self.source}}
        (self.root/'fluid-sources.json').write_text(json.dumps(self.registry))
        (self.root/'pcb-board.json').write_text(json.dumps({'world_id':'w','catalog_fingerprint':'c','blocks':{}}))
        self.source_result={'ok':True,'source':self.source,'extent':{'left_top':{'x':0,'y':-31},'right_bottom':{'x':1,'y':-30}},'limit':320}
        self.source_patch=patch.object(self.c,'_source',return_value=deepcopy(self.source_result)).start()
        self.survey=patch('factorio_agent.petroleum.survey_slots',return_value={'ok':True,'legal':[1]}).start()
        self.preflight=patch('factorio_agent.petroleum.preflight',return_value={'ok':True,'clear_obstacles':[]}).start()
        self.power=patch('factorio_agent.petroleum.power_connection',return_value={'ok':True,'entities':[],'wire_hops':1}).start()
        self.isolation=patch('factorio_agent.petroleum.fluid_plan_isolation',return_value={'ok':True}).start()
        def connection(game,builder,obs,source,destination,core):
            # An isolated hypothetical route; destination is already the core's first crude header.
            return _plan([entity('pipe',*destination['position'].values())],[],underground_pairs=[],route_metrics={'length':100,'turns':1})
        self.route_patch=patch('factorio_agent.petroleum.plan_crude_connection',side_effect=connection).start()
        self.addCleanup(patch.stopall)

    def install(self):
        self.c.state=self.c.prepare(self.obs);self.assertTrue(self.c.state['ok'],self.c.state)
        self.obs['entities']=[dict(e,unit_number=i+10)for i,e in enumerate(self.c.state['plan']['entities'])]
        self.assertIsNone(self.c._bind(self.obs))

    def test_prepare_is_readonly_and_natural_research_and_proven_source_are_required(self):
        with patch('factorio_agent.petroleum._atomic_json')as write:
            prepared=self.c.prepare(self.obs);write.assert_not_called()
        self.assertTrue(prepared['ok'],prepared);self.assertEqual(self.c.state,{})
        self.assertFalse(self.c.path.exists());self.builder.ensure_plan.assert_not_called()
        self.assertEqual(self.c.prepare({**self.obs,'technologies':{}})['reason'],'naturally_researched_basic_oil_processing_required')
        self.source_patch.return_value={'ok':False,'reason':'proven_crude_source_not_published'}
        self.assertEqual(self.c.prepare(self.obs)['reason'],'proven_crude_source_not_published')

    def test_native_route_failure_names_stage_and_preserves_budget(self):
        self.route_patch.side_effect=None;self.route_patch.return_value={'ok':False,'reason':'no_clean_trunk_corridor','visited':1234}
        result=self.c.prepare(self.obs)
        self.assertFalse(result['ok']);self.assertEqual(result['evidence']['attempts'],6)
        self.assertEqual(result['evidence']['failures'][0]['stage'],'crude_route')
        self.assertEqual(result['evidence']['failures'][0]['detail']['visited'],1234)
        self.builder.ensure_plan.assert_not_called();self.assertFalse(self.c.path.exists())

    def test_total_planning_deadline_returns_known_failure_without_latching_plan(self):
        self.route_patch.side_effect=None;self.route_patch.return_value={'ok':False,'reason':'no_clean_trunk_corridor'}
        with patch('factorio_agent.petroleum.time.monotonic',side_effect=[0,0,21,21]):
            result=self.c.prepare(self.obs)
        self.assertFalse(result['ok']);self.assertTrue(result['evidence']['budget_exhausted'])
        self.assertEqual(result['evidence']['attempts'],1);self.assertEqual(self.route_patch.call_count,1)
        self.assertFalse(self.c.path.exists());self.assertEqual(self.c.state,{})

    def test_native_source_registry_guard_rejects_world_or_ambiguous_handoff_before_query(self):
        registry=deepcopy(self.registry);registry['world_id']='other'
        self.c.sources_path.write_text(json.dumps(registry))
        self.assertEqual(BasicPetroleum._source(self.c,self.obs)['reason'],'fluid_source_registry_identity_changed')
        registry=deepcopy(self.registry);registry['sources']['crude-oil']['unit_number']=None
        self.c.sources_path.write_text(json.dumps(registry))
        self.assertEqual(BasicPetroleum._source(self.c,self.obs)['reason'],'proven_exact_crude_pipe_source_required')
        self.game.query.assert_not_called()

    def test_defense_precedes_paid_machine_and_new_posts_trigger_whole_preflight(self):
        self.perimeter.ensure_coverage.return_value={'type':'craft','recipe':'firearm-magazine','count':10}
        self.assertEqual(self.c.step(self.obs)['recipe'],'firearm-magazine');self.builder.ensure_plan.assert_not_called()
        self.perimeter.ensure_coverage.return_value=None
        self.preflight.return_value={'ok':False,'reason':'post_occupies_route'}
        self.assertEqual(self.c.step(self.obs)['reason'],'petroleum_post_defense_whole_preflight_failed')
        self.builder.ensure_plan.assert_not_called()

    def test_paid_build_emits_no_actor_fluid_feed_and_preserves_exact_source(self):
        result=self.c.step(self.obs)
        self.assertEqual(result,{'type':'craft','recipe':'oil-refinery','count':1})
        self.source_patch.return_value['source']['unit_number']=999
        self.assertEqual(self.c.step(self.obs)['reason'],'prepared_crude_source_changed_or_empty')
        self.assertNotIn('petroleum-gas',json.loads(self.c.sources_path.read_text())['sources'])

    def test_known_owned_asset_loss_blocks_rebuilding(self):
        self.install();self.obs['entities'].pop()
        self.assertEqual(self.c.step(self.obs)['reason'],'petroleum_asset_missing_or_replaced')
        self.builder.ensure_plan.assert_not_called()

    def test_registry_publication_requires_native_production_and_preserves_crude(self):
        self.install();self.builder.ensure_plan.return_value={'status':'succeeded'}
        self.game.query.return_value={'ok':False,'reason':'petroleum_reciprocal_typed_peer_missing'}
        self.assertEqual(self.c.step(self.obs)['status'],'waiting')
        self.assertNotIn('petroleum-gas',json.loads(self.c.sources_path.read_text())['sources'])
        outlet=self.c.state['core']['ports'][1];unit=self.c.state['bindings'][_key(outlet)]
        self.game.query.return_value={'ok':True,'tick':20,'outlet_unit':unit,'petroleum':45}
        self.assertEqual(self.c.step(self.obs)['status'],'succeeded')
        saved=json.loads(self.c.sources_path.read_text())
        self.assertEqual(saved['sources']['crude-oil'],self.source)
        self.assertEqual(saved['sources']['petroleum-gas']['unit_number'],unit)

    def test_proof_includes_owned_pole_identity_and_blocks_unrelated_published_outlet(self):
        self.install();self.builder.ensure_plan.return_value={'status':'succeeded'}
        outlet=self.c.state['core']['ports'][1];unit=self.c.state['bindings'][_key(outlet)]
        self.game.query.return_value={'ok':True,'tick':20,'outlet_unit':unit,'petroleum':45}
        pole=next(e for e in self.obs['entities']if e['name']=='small-electric-pole');pole['unit_number']=999
        self.assertIsNone(self.c.proof(self.obs));self.assertEqual(self.c.last_proof_failure['reason'],'petroleum_asset_missing_or_replaced')
        pole['unit_number']=self.c.state['bindings'][_key(pole)]
        registry=deepcopy(self.registry);registry['sources']['petroleum-gas']={'unit_number':999}
        self.c.sources_path.write_text(json.dumps(registry))
        self.assertEqual(self.c.step(self.obs)['reason'],'published_petroleum_output_identity_changed')

    @unittest.skipUnless(LuaRuntime,'optional Lua runtime: pip install lupa')
    def test_real_native_proof_checks_recipe_products_peers_indices_purity_identity_and_extent(self):
        self.install();lua=LuaRuntime(unpack_returned_tuples=True)
        def table(v):
            if isinstance(v,dict):return lua.table_from({k:table(a)for k,a in v.items()})
            if isinstance(v,list):return lua.table_from([table(a)for a in v])
            return v
        def decode(v):return {k:decode(a)for k,a in v.items()}if hasattr(v,'items')else v
        bindings={**self.c.state['bindings'],_key(self.source):1}
        specs=[dict(e,unit_number=bindings[_key(e)])for e in self.c.state['fluid_nodes']+self.c.state['core']['machines']]
        lua.globals().specs=table(specs);lua.globals().edges=table(self.c.state['fluid_edges'])
        lua.globals().helpers=lua.table_from({'json_to_table':lambda raw:table(json.loads(raw))})
        lua.execute('''f={};s={};game={tick=20};prototypes={utility_constants={default_pipeline_extent=320}};powered=true;finished=1;wrong_recipe=false;contaminated=false;one_way=false;wrong_index=false;oversize=false;foreign_peer=false;wrong_box=false
local function k(q)return q.name..":"..q.position.x..":"..q.position.y end
entities={};for _,q in ipairs(specs)do entities[k(q)]=q end
local function setup(q)
 q.force=f;q.surface=s;q.energy=100;q.products_finished=1
 q.is_connected_to_electric_network=function()return powered end
 q.get_recipe=function()return{name=wrong_recipe and"advanced-oil-processing"or"basic-oil-processing"}end
 q.get_fluid_box_prototype=function(index)if index>2 then error("runtime storage out of bounds")end;return{index=index==1 and 2 or 5}end
 q.get_fluid=function(index)if index>2 then error("runtime storage out of bounds")end;if wrong_box then return{name="crude-oil",amount=10}end;if index==1 then return{name="crude-oil",amount=10}elseif index==2 then return{name="petroleum-gas",amount=45}end end
 q.get_fluid_contents=function()if contaminated then return{water=1}end;if q.name=="oil-refinery"then return{["crude-oil"]=10,["petroleum-gas"]=45}end;if q.position.y<-50 then return{["petroleum-gas"]=45}else return{["crude-oil"]=10}end end
 q.has_fluid_segment=function()return true end
 q.get_fluid_segment_extent_bounding_box=function()return{left_top={x=0,y=0},right_bottom={x=10,y=oversize and 321 or 10}}end
 q.get_fluid_box_neighbours=function(index)local result={}
  local function storage(spec)return spec.name=="oil-refinery"and(spec.fluidbox_index==2 and 1 or 2)or spec.fluidbox_index end
  for _,edge in ipairs(edges)do
   if k(edge.a)==k(q)and storage(edge.a)==index then result[#result+1]={entity=entities[k(edge.b)],index=wrong_index and 3 or storage(edge.b)}end
   if not one_way and k(edge.b)==k(q)and storage(edge.b)==index then result[#result+1]={entity=entities[k(edge.a)],index=storage(edge.a)}end
  end
  if foreign_peer then result[#result+1]={entity={unit_number=999},index=1}end
  return result end
end
for _,q in ipairs(specs)do setup(q)end
function target(p,name)return entities[name..":"..p.x..":"..p.y]end''')
        gas_positions={tuple(p['position'].values())for p in self.c.state['core']['gas_pipes']}
        for q in specs:
            live=lua.globals().entities[_key(q)]
            fluids={'petroleum-gas':45}if tuple(q['position'].values())in gas_positions else{'crude-oil':10}
            if q['name']=='oil-refinery':fluids={'crude-oil':10,'petroleum-gas':45}
            live.get_fluid_contents=lua.eval('function(expected)return function()if contaminated then return{water=1}end;return expected end end')(table(fluids))
        self.game.query.side_effect=lambda body:decode(lua.execute('return(function() '+' '.join(body.splitlines())+' end)()'))
        self.assertIsNotNone(self.c.proof(self.obs),self.c.last_proof_failure)
        outlet=deepcopy(self.c.state['core']['ports'][1]);outlet_key=_key(outlet)
        source=dict(outlet,world_id='w',unit_number=bindings[outlet_key],defense_scope_id=self.c.state['defense_scope_id'])
        neighbor=dict(name='pipe',position={'x':outlet['position']['x']+1,'y':outlet['position']['y']},
                      direction=0,fluidbox_index=1,unit_number=7000)
        lua.globals().outlet=lua.globals().entities[outlet_key];lua.globals().consumer=table(neighbor)
        lua.execute('''consumer.force=f;consumer.surface=s;consumer.get_fluid_contents=function()return {["petroleum-gas"]=1}end
consumer.get_fluid_box_neighbours=function()return {{entity=outlet,index=1}}end
original_peers=outlet.get_fluid_box_neighbours
outlet.get_fluid_box_neighbours=function(index)local rows=original_peers(index);rows[#rows+1]={entity=consumer,index=1};return rows end
entities["pipe:"..consumer.position.x..":"..consumer.position.y]=consumer''')
        self.assertIsNone(self.c.proof(self.obs))
        registry=deepcopy(self.registry)
        registry['handoffs']={'chemical:plastic-bar':{'petroleum-gas':dict(world_id='w',catalog_fingerprint=self.c.catalog.fingerprint,
            source=source,first_neighbor=neighbor,phase='bound')}}
        self.c.sources_path.write_text(json.dumps(registry))
        self.assertIsNotNone(self.c.proof(self.obs),self.c.last_proof_failure)
        lua.execute('consumer.unit_number=7001');self.assertIsNone(self.c.proof(self.obs))
        lua.execute('outlet.get_fluid_box_neighbours=original_peers')
        self.c.sources_path.write_text(json.dumps(self.registry))
        machine=_key(self.c.state['core']['machines'][0]);live=lua.globals().entities[machine]
        for change,restore in(('powered=false','powered=true'),('wrong_recipe=true','wrong_recipe=false'),
            ('contaminated=true','contaminated=false'),('one_way=true','one_way=false'),
            ('wrong_index=true','wrong_index=false'),('oversize=true','oversize=false'),('foreign_peer=true','foreign_peer=false'),
            ('wrong_box=true','wrong_box=false')):
            lua.execute(change);self.assertIsNone(self.c.proof(self.obs),change);lua.execute(restore)
        live.products_finished=0;self.assertIsNone(self.c.proof(self.obs));live.products_finished=1
        live.unit_number=999;self.assertIsNone(self.c.proof(self.obs))
        for forbidden in('insert_fluid','set_fluid','create_entity','set_research','.researched=true'):
            self.assertNotIn(forbidden,PETROLEUM_PROOF_LUA)


if __name__=='__main__':unittest.main()
