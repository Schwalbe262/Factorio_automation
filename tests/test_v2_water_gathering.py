from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_agent.water_gathering import WaterGathering,water_core,_key,WATER_PROOF_LUA

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


def catalog():return SimpleNamespace(fingerprint='c',entities={'offshore-pump':{
    'type':'offshore-pump','electric':False,'burner':False,'energy_usage':1000,
    'selection_box':{'left_top':{'x':-.6,'y':-1.49},'right_bottom':{'x':.6,'y':.49}},
    'fluidbox_prototypes':[{'index':1,'production_type':'output','pipe_connections':[{
        'connection_type':'normal','direction':8,'positions':[{'x':0,'y':0}for _ in range(4)]}]}]}})


class WaterGatheringTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.cat=catalog();self.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root),query=Mock())
        self.old={'name':'offshore-pump','position':{'x':54.5,'y':.5},'direction':4,'unit_number':166,'peers':[{'unit_number':397,'index':1}]}
        self.sites={'ok':True,'sites':[{'position':{'x':87.5,'y':-25.5},'direction':12}],'preserved_pumps':[self.old]}
        self.game.query.side_effect=lambda script:deepcopy(self.sites)if'local pumps=s.find_entities_filtered'in script else{'ok':True}
        self.builder=SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,
            ensure_plan=Mock(return_value={'type':'craft','recipe':'offshore-pump','count':1}))
        self.perimeter=SimpleNamespace(prepare_coverage=Mock(return_value={'ok':True,'new_posts':[]}),ensure_coverage=Mock(return_value=None),
            coverage_proof=Mock(return_value={'complete':True}))
        (self.root/'pcb-board.json').write_text(json.dumps({'world_id':'w','catalog_fingerprint':'c','blocks':{}}))
        self.registry={'world_id':'w','catalog_fingerprint':'c','sources':{'crude-oil':{'unit_number':6506},'petroleum-gas':{'unit_number':7000}}}
        (self.root/'fluid-sources.json').write_text(json.dumps(self.registry))
        self.c=WaterGathering(self.game,self.builder,self.cat,self.perimeter)
        self.obs={'ok':True,'world_id':'w','tick':10,'entities':[],'enabled_recipes':{'offshore-pump':True,'pipe':True}}
        self.preflight=patch('factorio_agent.water_gathering.preflight',return_value={'ok':True,'clear_obstacles':[]}).start()
        self.addCleanup(patch.stopall)

    def install(self):
        self.c.state=self.c.prepare(self.obs);self.assertTrue(self.c.state['ok'],self.c.state)
        self.obs['entities']=[dict(e,unit_number=i+100)for i,e in enumerate(self.c.state['plan']['entities'])]
        self.assertIsNone(self.c._bind(self.obs))

    def test_catalog_rotation_native_pipe_chain_and_full_asymmetric_body_area(self):
        expected={0:(.5,3.5),4:(-2.5,.5),8:(.5,-2.5),12:(3.5,.5)}
        geometry=FactoryBuilder.__new__(FactoryBuilder)
        for d,end in expected.items():
            p=water_core(self.cat,{'x':.5,'y':.5},d)
            self.assertEqual(tuple(p['ports'][0]['position'].values()),end)
            self.assertEqual(p['ports'][0]['facing'],(8+d)%16)
            self.assertEqual(p['required_items'],{'pipe':3,'offshore-pump':1})
            boxes=[geometry._occupied_by_plan([e])for e in p['entities']]
            self.assertEqual(sum(map(len,boxes)),len(set().union(*boxes)))
            area=p['pump_survey_area'];self.assertGreater(area[1][0]-area[0][0],1)
            self.assertTrue(all('mining_resource'not in e for e in p['entities']))
        east=water_core(self.cat,{'x':.5,'y':.5},12)['pump_survey_area']
        self.assertAlmostEqual(east[0][0],-.99);self.assertAlmostEqual(east[1][0],.99)

    def test_prepare_readonly_requires_native_unlock_and_preserves_old_pump_snapshot(self):
        before={p.name:p.read_bytes()for p in self.root.glob('*.json')}
        with patch('factorio_agent.water_gathering._atomic_json')as write:
            result=self.c.prepare(self.obs);write.assert_not_called()
        self.assertTrue(result['ok'],result);self.assertEqual(result['preserved_pumps'],[self.old])
        self.assertEqual(result['plan']['defense_scope_id'],result['defense_scope_id'])
        self.assertEqual({p.name:p.read_bytes()for p in self.root.glob('*.json')},before)
        self.assertEqual(self.c.state,{});self.builder.ensure_plan.assert_not_called()
        self.assertEqual(self.c.prepare({**self.obs,'enabled_recipes':{}})['reason'],'native_water_pump_and_pipe_recipes_required')

    def test_external_energy_or_missing_port_metadata_fails_closed(self):
        self.cat.entities['offshore-pump']['electric']=True
        self.assertEqual(self.c.prepare(self.obs)['reason'],'unsupported_native_water_intake')
        self.cat.entities['offshore-pump']['electric']=False
        self.cat.entities['offshore-pump']['fluidbox_prototypes'][0]['pipe_connections'][0]['positions']=[]
        self.assertEqual(self.c.prepare(self.obs)['reason'],'unsupported_native_water_intake')
        self.builder.ensure_plan.assert_not_called()

    def test_foreign_fluid_resource_or_continuation_failure_never_latches_plan(self):
        for reason in('water_intake_near_existing_fluid_network','water_pump_body_over_resource_or_mining_growth',
                      'water_output_has_no_clear_resource_free_continuation'):
            self.game.query.side_effect=lambda script,r=reason:deepcopy(self.sites)if'local pumps=s.find_entities_filtered'in script else{'ok':False,'reason':r}
            result=self.c.prepare(self.obs);self.assertFalse(result['ok'])
            self.assertEqual(result['evidence']['candidate_failures'][0]['reason'],reason)
            self.assertFalse(self.c.path.exists());self.builder.ensure_plan.assert_not_called()

    def test_defense_and_fresh_coast_whole_checks_precede_first_build(self):
        self.perimeter.ensure_coverage.return_value={'type':'craft','recipe':'firearm-magazine','count':10}
        self.assertEqual(self.c.step(self.obs)['recipe'],'firearm-magazine');self.builder.ensure_plan.assert_not_called()
        self.perimeter.ensure_coverage.return_value=None
        self.game.query.side_effect=None;self.game.query.return_value={'ok':False,'reason':'coast_changed'}
        self.assertEqual(self.c.step(self.obs)['reason'],'water_coast_changed_after_defense')
        self.builder.ensure_plan.assert_not_called()

    def test_missing_or_replaced_owned_asset_blocks_reconstruction(self):
        self.install();self.obs['entities'][0]['unit_number']=999
        self.assertEqual(self.c.step(self.obs)['reason'],'water_asset_missing_or_replaced');self.builder.ensure_plan.assert_not_called()

    def test_publication_requires_flow_and_preserves_all_other_fluid_sources(self):
        self.install();self.builder.ensure_plan.return_value={'status':'succeeded'};self.c.state['construction_authorized']=True
        self.game.query.side_effect=None;self.game.query.return_value={'ok':False,'reason':'native_independent_water_required'}
        self.assertEqual(self.c.step(self.obs)['status'],'waiting')
        self.assertEqual(json.loads(self.c.sources_path.read_text()),self.registry)
        end=self.c.state['core']['pipes'][-1];unit=self.c.state['bindings'][_key(end)]
        self.game.query.return_value={'ok':True,'tick':20,'outlet_unit':unit}
        self.assertEqual(self.c.step(self.obs)['status'],'succeeded')
        saved=json.loads(self.c.sources_path.read_text())['sources']
        self.assertEqual(saved['crude-oil'],self.registry['sources']['crude-oil'])
        self.assertEqual(saved['petroleum-gas'],self.registry['sources']['petroleum-gas'])
        self.assertEqual(saved['water']['unit_number'],unit)

    def test_existing_registered_water_is_not_silently_rebound(self):
        self.registry['sources']['water']={'unit_number':999};self.c.sources_path.write_text(json.dumps(self.registry))
        self.assertEqual(self.c.prepare(self.obs)['reason'],'water_source_exists_without_intake_checkpoint')
        self.game.query.assert_not_called()

    def test_verified_legacy_scope_publication_recovers_both_metadata_write_orders(self):
        self.install();self.c.state['phase']='verified';self.c.state['plan'].pop('defense_scope_id')
        legacy=deepcopy(self.c.state['plan']);desired=dict(legacy,defense_scope_id=self.c.state['defense_scope_id'])
        board={'world_id':'w','catalog_fingerprint':'c','blocks':{'water_gathering':legacy}}
        path=self.root/'pcb-board.json';path.write_text(json.dumps(board))
        with patch.object(self.c,'proof',return_value=None):
            self.assertEqual(self.c._reserve(self.obs)['reason'],'water_scope_upgrade_native_proof_required')
        self.assertEqual(json.loads(path.read_text())['blocks']['water_gathering'],legacy)
        with patch.object(self.c,'proof',return_value={'native':True}):self.assertIsNone(self.c._reserve(self.obs))
        self.assertEqual(self.c.state['plan'],desired);self.assertEqual(json.loads(path.read_text())['blocks']['water_gathering'],desired)
        self.c.state['plan']=legacy
        with patch.object(self.c,'proof',return_value={'native':True}):self.assertIsNone(self.c._reserve(self.obs))
        self.assertEqual(self.c.state['plan'],desired);self.builder.ensure_plan.assert_not_called()

    def test_scope_upgrade_never_adopts_changed_board_geometry(self):
        self.install();self.c.state['phase']='verified';self.c.state['plan'].pop('defense_scope_id')
        changed=dict(self.c.state['plan'],reserved_area=[[100,100],[110,110]])
        path=self.root/'pcb-board.json';path.write_text(json.dumps({'world_id':'w','catalog_fingerprint':'c','blocks':{'water_gathering':changed}}))
        with patch.object(self.c,'proof',return_value={'native':True})as proof:
            self.assertEqual(self.c._reserve(self.obs)['reason'],'water_scope_upgrade_reservation_changed');proof.assert_not_called()
        self.assertEqual(json.loads(path.read_text())['blocks']['water_gathering'],changed)

    @unittest.skipUnless(LuaRuntime,'optional Lua runtime: pip install lupa')
    def test_actual_lua_reciprocal_flow_identity_steam_preservation_and_independent_segments(self):
        self.install();lua=LuaRuntime(unpack_returned_tuples=True)
        def table(v):
            if isinstance(v,dict):return lua.table_from({k:table(a)for k,a in v.items()})
            if isinstance(v,list):return lua.table_from([table(a)for a in v])
            return v
        def decode(v):return{k:decode(a)for k,a in v.items()}if hasattr(v,'items')else v
        chain=[self.c.state['core']['pump']]+self.c.state['core']['pipes']
        lua.globals().chain=table([dict(e,unit_number=self.c.state['bindings'][_key(e)])for e in chain])
        lua.globals().old=table(self.old);lua.globals().helpers=lua.table_from({'json_to_table':lambda raw:table(json.loads(raw))})
        lua.execute('''f={};s={};game={tick=20};prototypes={utility_constants={default_pipeline_extent=320}};one_way=false;contaminated=false;empty=false;merged=false;oversize=false;old_peer_changed=false
function pos(p)return{x=p.x,y=p.y}end
old.force=f;old.surface=s
old.get_fluid_box_neighbours=function()return{{entity={unit_number=old_peer_changed and 999 or 397,has_fluid_segment=function()return true end,get_fluid_segment_id=function()return 3 end},index=1}}end
function setup(i,e)
 e.force=f;e.surface=s;e.get_fluid_contents=function()if empty then return{}end;if contaminated then return{steam=1}end;return{water=10}end
 e.has_fluid_segment=function()return true end;e.get_fluid_segment_id=function()return merged and 3 or 7 end
 e.get_fluid_segment_extent_bounding_box=function()return{left_top={x=0,y=0},right_bottom={x=oversize and 321 or 4,y=1}}end
 e.get_fluid_box_neighbours=function()local peers={};if i>1 and not one_way then peers[#peers+1]={entity=chain[i-1],index=1}end;if i<#chain then peers[#peers+1]={entity=chain[i+1],index=1}end;return peers end
end
for i,e in ipairs(chain)do setup(i,e)end
function target(p,name)if name==old.name and p.x==old.position.x and p.y==old.position.y then return old end;for _,e in ipairs(chain)do if e.name==name and e.position.x==p.x and e.position.y==p.y then return e end end end''')
        self.game.query.side_effect=lambda body:decode(lua.execute('return(function() '+' '.join(body.splitlines())+' end)()'))
        self.assertIsNotNone(self.c.proof(self.obs),self.c.last_proof_failure)
        for change,restore in(('one_way=true','one_way=false'),('contaminated=true','contaminated=false'),
            ('empty=true','empty=false'),('merged=true','merged=false'),('oversize=true','oversize=false'),
            ('old_peer_changed=true','old_peer_changed=false'),('old.direction=0','old.direction=4'),
            ('chain[2].unit_number=999','chain[2].unit_number='+str(self.c.state['bindings'][_key(chain[1])]))):
            lua.execute(change);self.assertIsNone(self.c.proof(self.obs),change);lua.execute(restore)
        for forbidden in('insert_fluid','set_fluid','create_entity','.researched=','set_recipe'):
            self.assertNotIn(forbidden,WATER_PROOF_LUA)


if __name__=='__main__':unittest.main()
