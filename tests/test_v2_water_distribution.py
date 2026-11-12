from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.water_gathering import water_core
from factorio_agent.water_distribution import WaterDistribution,header_core,_edges,_key,HEADER_PROOF_LUA,HEADER_GUARD_LUA,SURVEY_LUA
from test_v2_water_gathering import catalog

try:
    from lupa import LuaRuntime
except ImportError:LuaRuntime=None


class WaterHeaderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.cat=catalog()
        self.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root),query=Mock(return_value={'ok':True,'maximum_span':10,
            'soft':[{'x':89.5,'y':y}for y in(-48.5,-51.5,-52.5,-53.5)],'pipes':[],'assets':[]}))
        self.builder=SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,ensure_plan=Mock(return_value={'type':'build','name':'pipe'}))
        self.perimeter=SimpleNamespace(prepare_coverage=Mock(return_value={'ok':True}),ensure_coverage=Mock(return_value=None),coverage_proof=Mock(return_value={'complete':True}))
        old=water_core(self.cat,{'x':87.5,'y':-25.5},12);producer={'world_id':'w','catalog_fingerprint':'c','phase':'verified',
            'core':old,'plan':old,'bindings':{_key(q):i+1 for i,q in enumerate(old['entities'])},'defense_scope_id':'old-water'}
        source=dict(old['ports'][0],world_id='w',unit_number=producer['bindings'][_key(old['ports'][0])],defense_scope_id='old-water')
        self.water=SimpleNamespace(path=self.root/'water-gathering.json',state=producer,proof=Mock(return_value={'source':source}))
        self.water.path.write_text(json.dumps(producer));(self.root/'pcb-board.json').write_text(json.dumps({'world_id':'w','catalog_fingerprint':'c','blocks':{'water_gathering':old}}))
        self.registry={'world_id':'w','catalog_fingerprint':'c','sources':{'water':source},'handoffs':{}}
        (self.root/'fluid-sources.json').write_text(json.dumps(self.registry));self.c=WaterDistribution(self.game,self.builder,self.cat,self.perimeter,water=self.water)
        self.obs={'ok':True,'world_id':'w','tick':10,'entities':[dict(q,unit_number=producer['bindings'][_key(q)])for q in old['entities']]}
        self.check=patch('factorio_agent.water_distribution.preflight',return_value={'ok':True,'clear_obstacles':[]}).start()
        patch('factorio_agent.water_distribution.mining_reservations',return_value=[]).start()
        self.chemical=patch('factorio_agent.water_distribution.ChemicalProduction',return_value=SimpleNamespace(prepare=Mock(return_value={'ok':True,'prospective_only':True}))).start()
        self.addCleanup(patch.stopall)

    def test_prepare_is_readonly_and_complete_sulfur_pair_is_mandatory(self):
        before={p.name:p.read_bytes()for p in self.root.glob('*.json')};ready=self.c.prepare(self.obs)
        self.assertTrue(ready['ok'],ready);self.assertEqual(ready['core']['ports'][0]['position'],{'x':89.5,'y':-57.5})
        self.assertEqual(ready['core']['underground_pairs'][0]['span'],8)
        self.assertEqual({p.name:p.read_bytes()for p in self.root.glob('*.json')},before);self.assertEqual(self.c.state,{})
        self.assertNotIn('sulfur-water',json.loads(self.c.registry_path.read_text())['sources'])
        self.chemical.return_value.prepare.return_value={'ok':False,'reason':'no_clean_trunk_corridor'}
        self.assertEqual(self.c.prepare(self.obs)['reason'],'water_header_complete_sulfur_preview_required')
        self.assertFalse(self.c.path.exists());self.builder.ensure_plan.assert_not_called()

    def test_normal_paid_mouths_no_resources_exemptions_and_three_clear_output_cells(self):
        ready=self.c.prepare(self.obs);core=ready['core'];pair=core['underground_pairs'][0]
        self.assertEqual(pair['first'],[89.5,-46.5]);self.assertEqual(pair['last'],[89.5,-54.5])
        mouths={tuple(q['position'].values()):q for q in core['entities']if q['name']=='pipe-to-ground'}
        self.assertEqual((mouths[tuple(pair['first'])]['direction'],mouths[tuple(pair['last'])]['direction']),(8,0))
        self.assertTrue(all('mining_resource'not in q for q in core['entities']))
        self.assertEqual(len(core['output_access']),3)
        self.assertFalse(self.builder._occupied_by_plan(core['entities'])&self.builder._occupied_by_plan(core['output_access']))
        self.assertEqual(core['ports'][0]['facing'],0)

    def test_original_port_pcb_revision_resource_and_future_growth_fail_closed(self):
        for reason in('water_header_resource_obstruction','water_header_mining_reservation_obstruction','water_header_foreign_asset_obstruction'):
            self.game.query.return_value={'ok':False,'reason':reason}
            self.assertEqual(self.c.prepare(self.obs)['reason'],reason)
        self.builder.ensure_plan.assert_not_called()
        board=json.loads((self.root/'pcb-board.json').read_text());board['blocks']['water_gathering']['ports'][0]['facing']=0
        (self.root/'pcb-board.json').write_text(json.dumps(board));self.assertFalse(self.c.prepare(self.obs)['ok'])

    def test_defense_and_exact_original_unit_guard_precede_first_header_build(self):
        ready=self.c.prepare(self.obs);self.c.state=ready;self.c._save()
        self.perimeter.ensure_coverage.return_value={'type':'craft','recipe':'firearm-magazine','count':10}
        self.assertEqual(self.c.step(self.obs)['type'],'craft');self.builder.ensure_plan.assert_not_called()
        self.perimeter.ensure_coverage.return_value=None;self.obs['entities'][0]['unit_number']=999
        self.assertEqual(self.c.step(self.obs)['reason'],'water_header_preserved_asset_changed');self.builder.ensure_plan.assert_not_called()

    def test_changed_crossed_pipe_witness_or_missing_native_flow_never_publishes_alias(self):
        self.c.state=self.c.prepare(self.obs);self.c._save()
        with patch('factorio_agent.water_distribution.surface_pipe_proof',return_value={'ok':False,'reason':'preserved_surface_pipe_peer_identity_changed'}):
            self.assertEqual(self.c.step(self.obs)['reason'],'preserved_surface_pipe_peer_identity_changed')
        self.builder.ensure_plan.assert_not_called()
        self.builder.ensure_plan.return_value={'status':'succeeded'};self.c.proof=Mock(return_value=None)
        self.assertEqual(self.c.step(self.obs)['status'],'waiting')
        self.assertEqual(json.loads(self.c.registry_path.read_text()),self.registry)

    def test_owned_producer_apron_coordinates_are_independent_of_json_key_order(self):
        self.c.state=self.c.prepare(self.obs)
        self.c.state['core']['source_apron']=[{'y':p['y'],'x':p['x']}for p in self.c.state['core']['source_apron']]
        self.assertTrue(self.c._guard(self.obs)['ok'])


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class NativeWaterHeaderTests(WaterHeaderTests):
    def setUp(self):
        super().setUp();self.entry=self.c.prepare(self.obs);self.lua=LuaRuntime(unpack_returned_tuples=True)
        def table(v):
            if isinstance(v,dict):return self.lua.table_from({k:table(q)for k,q in v.items()})
            if isinstance(v,list):return self.lua.table_from([table(q)for q in v])
            return v
        self.table=table
        original=self.entry['original'];new=[dict(q,unit_number=100+i)for i,q in enumerate(self.entry['plan']['entities'])]
        self.nodes=original+new;self.output=dict(self.entry['core']['ports'][0],unit_number=new[-1]['unit_number'])
        self.edges=_edges(self.nodes,self.entry['core']['underground_pairs'])
        self.lua.execute('''d={world_id="w"};game={tick=100};f={name="player"};s={};world={};prototypes={utility_constants={default_pipeline_extent=320}}
function key(q)return q.name..":"..q.position.x..":"..q.position.y end
function add(q)local e={name=q.name,type=q.name,position=q.position,unit_number=q.unit_number,direction=q.direction or 0,
 force=f,surface=s,valid=true,quality={name="normal"},peers={},segment=1,fluid="water",prototype={collision_mask={colliding_with_tiles_only=false},fluidbox_prototypes={{}}}}
 e.get_fluid_contents=function()return{[e.fluid]=100}end;e.has_fluid_segment=function()return true end;e.get_fluid_segment_id=function()return e.segment end
 e.get_fluid_segment_extent_bounding_box=function()return{left_top={x=86,y=-61},right_bottom={x=91,y=-24}}end
 e.get_fluid_box_neighbours=function()return e.peers end;world[key(q)]=e;return e end
function target(p,name)return world[name..":"..p.x..":"..p.y]end
function connect(a,b)a.peers[#a.peers+1]={entity=b,index=1};b.peers[#b.peers+1]={entity=a,index=1}end
''')
        for q in self.nodes:self.lua.globals().add(table(q))
        for edge in self.edges:self.lua.globals().connect(self.lua.globals().target(table(edge['a']['position']),edge['a']['name']),self.lua.globals().target(table(edge['b']['position']),edge['b']['name']))
        self.args=dict(world_id='w',nodes=self.nodes,edges=self.edges,source=self.entry['tap'],outlet=self.output,foreign_pipes=[],downstream=[])

    def run_proof(self):
        self.lua.globals().args=self.table(self.args)
        return self.lua.execute('return(function()'+' '.join(HEADER_PROOF_LUA.splitlines())+'end)()')

    def test_native_exact_units_reciprocal_pairs_and_actual_water_are_required(self):
        self.assertTrue(self.run_proof()['ok'])
        out=self.lua.globals().target(self.table(self.output['position']),'pipe');out.unit_number=999
        self.assertEqual(self.run_proof()['reason'],'water_header_native_unit_changed');out.unit_number=self.output['unit_number']
        out.fluid='crude-oil';self.assertEqual(self.run_proof()['reason'],'water_header_mixed_fluid');out.fluid='water'
        out.peers=self.lua.table_from([]);self.assertEqual(self.run_proof()['reason'],'water_header_reciprocal_edge_missing')

    def test_foreign_network_isolation_and_extent_are_native_not_stock_claims(self):
        q=dict(name='pipe',position={'x':89.5,'y':-52.5},unit_number=999,direction=0)
        foreign=self.lua.globals().add(self.table(q));foreign.fluid='crude-oil';foreign.segment=2;self.args['foreign_pipes']=[q]
        self.assertTrue(self.run_proof()['ok']);foreign.segment=1
        self.assertEqual(self.run_proof()['reason'],'water_header_foreign_network_merged')
        foreign.segment=2;self.lua.execute('prototypes.utility_constants.default_pipeline_extent=10')
        self.assertEqual(self.run_proof()['reason'],'water_header_native_extent_changed')

    def test_native_scripts_flatten_without_comments_or_material_fluid_writes(self):
        for text in(SURVEY_LUA,HEADER_PROOF_LUA,HEADER_GUARD_LUA):
            self.assertNotIn('--',text)
            for word in('create_entity','mine_entity','insert_fluid','set_fluid','destroy('):self.assertNotIn(word,text)


if __name__=='__main__':unittest.main()
