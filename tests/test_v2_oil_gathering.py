from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.oil_gathering import OilGathering,oil_core,_key
from factorio_agent.resource_policy import mining_reservations

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


def catalog():
    return SimpleNamespace(fingerprint='catalog',technologies={'oil-processing':{'research_trigger':{
        'type':'mine-entity','entities':['crude-oil']}}},entities={'pumpjack':{
        'selection_box':{'left_top':{'x':-1.5,'y':-1.5},'right_bottom':{'x':1.5,'y':1.5}},
        'fluidbox_prototypes':[{'index':1,'production_type':'output','volume':1000,'pipe_connections':[{
            'connection_type':'normal','direction':0,'flow_direction':'output','positions':[
                {'x':1,'y':-1},{'x':1,'y':1},{'x':-1,'y':1},{'x':-1,'y':-1}]}]}]}})


class OilGatheringTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.catalog=catalog()
        self.board={'world_id':'w','catalog_fingerprint':'catalog','blocks':{}}
        (self.root/'pcb-board.json').write_text(json.dumps(self.board))
        self.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root),query=Mock(side_effect=lambda body:
            {'ok':True,'wells':[{'position':{'x':10.5,'y':-20.5},'amount':10000}]}
            if 'local wells={}'in body else {'ok':True}))
        geometry=FactoryBuilder.__new__(FactoryBuilder)
        self.builder=SimpleNamespace(_occupied_by_plan=geometry._occupied_by_plan,
            ensure_plan=Mock(return_value={'type':'craft','recipe':'pumpjack','count':1}))
        self.perimeter=SimpleNamespace(prepare_coverage=Mock(return_value={'ok':True,'new_posts':[],
            'requirements':{'gun-turret':1,'firearm-magazine':10}}),
            ensure_coverage=Mock(return_value=None),coverage_proof=Mock(return_value={'geometry':{'complete':True}}))
        self.c=OilGathering(self.game,self.builder,self.catalog,self.perimeter)
        self.obs={'ok':True,'world_id':'w','tick':10,'entities':[],
                  'technologies':{'oil-gathering':True},'enabled_recipes':{'pumpjack':True}}
        self.preflight=patch('factorio_agent.oil_gathering.preflight',return_value={'ok':True,'clear_obstacles':[]}).start()
        self.power=patch('factorio_agent.oil_gathering.power_connection',return_value={'ok':True,'entities':[],'wire_hops':1}).start()
        self.addCleanup(patch.stopall)

    def install(self):
        self.c.state=self.c.prepare(self.obs)
        self.assertTrue(self.c.state['ok'],self.c.state)
        self.obs['entities']=[dict(e,unit_number=i+10)for i,e in enumerate(self.c.state['plan']['entities'])]
        self.assertIsNone(self.c._bind(self.obs))

    def test_geometry_uses_all_four_rotated_native_external_ports_and_paid_items(self):
        expected={0:(1,-2),4:(2,1),8:(-1,2),12:(-2,-1)}
        geometry=FactoryBuilder.__new__(FactoryBuilder)
        for direction,(x,y)in expected.items():
            p=oil_core(self.catalog,{'x':.5,'y':.5},direction,
                       power_offset=(0,2)if direction!=8 else(0,-2))
            self.assertEqual(p['pipes'][0]['position'],{'x':x+.5,'y':y+.5})
            boxes=[geometry._occupied_by_plan([e])for e in p['entities']]
            self.assertEqual(sum(map(len,boxes)),len(set().union(*boxes)))
            self.assertEqual(p['required_items'],{'pipe':3,'small-electric-pole':1,'pumpjack':1})
            self.assertEqual(p['ports'][0]['kind'],'fluid')
            self.assertTrue(all('mining_resource'not in e for e in p['entities']if e['name']!='pumpjack'))

    def test_unsupported_connection_catalog_blocks_instead_of_guessing(self):
        self.catalog.entities['pumpjack']['fluidbox_prototypes'][0]['pipe_connections'][0]['positions']=[]
        self.assertEqual(self.c.prepare(self.obs)['reason'],'unsupported_native_pumpjack_geometry')
        self.power.assert_not_called()

    def test_prepare_requires_natural_unlock_and_trigger_before_native_io(self):
        self.assertEqual(self.c.prepare({**self.obs,'technologies':{}})['reason'],
                         'natural_oil_gathering_and_enabled_pumpjack_required')
        self.catalog.technologies['oil-processing']['research_trigger']['type']='nothing'
        self.assertEqual(self.c.prepare(self.obs)['reason'],'installed_oil_processing_crude_extraction_trigger_required')
        self.game.query.assert_not_called()

    def test_prepare_is_readonly_and_needs_whole_plan_and_defense_preflight(self):
        before=(self.root/'pcb-board.json').read_bytes()
        with patch('factorio_agent.oil_gathering._atomic_json')as write:
            result=self.c.prepare(self.obs)
            write.assert_not_called()
        self.assertTrue(result['ok'],result)
        self.assertEqual(self.c.state,{})
        self.assertEqual((self.root/'pcb-board.json').read_bytes(),before)
        self.assertFalse(self.c.path.exists())
        self.assertGreaterEqual(self.preflight.call_count,2)
        self.perimeter.prepare_coverage.assert_called_once()

    def test_existing_fluid_network_is_not_reused_or_contaminated(self):
        self.game.query.side_effect=lambda body:({'ok':True,'wells':[{'position':{'x':10.5,'y':-20.5},'amount':10000}]}
            if 'local wells={}'in body else {'ok':False,'reason':'oil_outlet_near_existing_fluid_network'})
        result=self.c.prepare(self.obs)
        self.assertFalse(result['ok'])
        self.assertIn('oil_outlet_near_existing_fluid_network',result['candidate_failures'])
        self.power.assert_not_called()

    def test_defense_action_blocks_all_pumpjack_construction(self):
        self.perimeter.ensure_coverage.return_value={'type':'craft','recipe':'firearm-magazine','count':10}
        result=self.c.step(self.obs)
        self.assertEqual(result['recipe'],'firearm-magazine')
        self.builder.ensure_plan.assert_not_called()
        block=json.loads((self.root/'pcb-board.json').read_text())['blocks']['oil_gathering']
        self.assertEqual(block['mining_reserved_area'],self.c.state['core']['mining_footprint'])
        self.assertEqual(mining_reservations(self.game,'w'),[{'resource':'crude-oil','area':block['mining_reserved_area']}])

    def test_ready_coverage_delegates_only_ordinary_paid_construction(self):
        action=self.c.step(self.obs)
        self.assertEqual(action,{'type':'craft','recipe':'pumpjack','count':1})
        self.assertFalse(self.c.sources_path.exists())

    def test_changed_registered_oil_layout_is_not_overwritten(self):
        self.c.step(self.obs)
        board=json.loads((self.root/'pcb-board.json').read_text())
        board['blocks']['oil_gathering']['ports'][0]['position']['x']+=1
        (self.root/'pcb-board.json').write_text(json.dumps(board));before=(self.root/'pcb-board.json').read_bytes()
        self.builder.ensure_plan.reset_mock()
        self.assertEqual(self.c.step(self.obs)['reason'],'oil_gathering_reservation_changed')
        self.assertEqual((self.root/'pcb-board.json').read_bytes(),before)
        self.builder.ensure_plan.assert_not_called()

    def test_crude_reservation_cannot_expand_exemption_over_pipes_or_service_area(self):
        self.c.step(self.obs)
        board=json.loads((self.root/'pcb-board.json').read_text())
        board['blocks']['oil_gathering']['mining_reserved_area']=self.c.state['core']['reserved_area']
        (self.root/'pcb-board.json').write_text(json.dumps(board))
        with self.assertRaises(ValueError):mining_reservations(self.game,'w')

    def test_saved_units_never_adopt_replacements_or_missing_assets(self):
        self.install();old=self.obs['entities'][0]['unit_number']
        self.obs['entities'][0]['unit_number']+=100
        self.assertEqual(self.c.step(self.obs)['reason'],'owned_oil_asset_missing_or_replaced')
        self.assertEqual(self.c.state['bindings'][_key(self.obs['entities'][0])],old)
        self.builder.ensure_plan.assert_not_called()

    def test_changed_world_blocks_before_build_or_query(self):
        self.install();self.game.query.reset_mock()
        self.assertEqual(self.c.step({**self.obs,'world_id':'other'})['reason'],'oil_gathering_world_or_catalog_changed')
        self.game.query.assert_not_called()
        self.builder.ensure_plan.assert_not_called()

    def test_native_crude_peer_or_natural_research_failure_cannot_publish_source(self):
        self.install();self.builder.ensure_plan.return_value={'status':'succeeded'}
        self.game.query.side_effect=None
        for reason in ('native_crude_pipe_peer_missing','pumpjack_not_normally_powered',
                       'native_crude_and_natural_research_required','native_oil_asset_identity_changed'):
            self.game.query.return_value={'ok':False,'reason':reason}
            self.assertEqual(self.c.step(self.obs)['status'],'waiting')
            self.assertFalse(self.c.sources_path.exists())
        script=self.game.query.call_args.args[0]
        self.assertIn('previous.get_fluid_box_neighbours(1)',script)
        self.assertIn('e.get_fluid_box_neighbours(1)',script)
        self.assertIn('not f.technologies["oil-processing"].researched',script)
        self.assertNotIn('--',' '.join(script.splitlines()))
        for forbidden in ('set_research','.researched=true','insert_fluid','set_fluid','create_entity','mine_entity'):
            self.assertNotIn(forbidden,script)

    def native_success(self):
        chain=[self.c.state['core']['pumpjack']]+self.c.state['core']['pipes']
        return {'ok':True,'tick':55,'oil_processing_researched':True,
                'chain':[dict(e,unit_number=self.c.state['bindings'][_key(e)],crude=10)for e in chain]}

    def test_proven_source_publishes_exact_output_unit_and_preserves_other_sources(self):
        self.install();self.builder.ensure_plan.return_value={'status':'succeeded'}
        self.game.query.side_effect=None;self.game.query.return_value=self.native_success()
        other={'world_id':'w','catalog_fingerprint':'catalog','sources':{'water':{'unit_number':99}}}
        self.c.sources_path.write_text(json.dumps(other))
        result=self.c.step(self.obs)
        self.assertEqual(result['status'],'succeeded')
        saved=json.loads(self.c.sources_path.read_text())
        self.assertEqual(saved['sources']['water'],{'unit_number':99})
        source=saved['sources']['crude-oil']
        self.assertEqual(source['unit_number'],self.native_success()['chain'][-1]['unit_number'])
        self.assertEqual((source['kind'],source['item'],source['direction']),('fluid','crude-oil','output'))

    def test_other_registered_crude_identity_is_never_overwritten(self):
        self.install();self.builder.ensure_plan.return_value={'status':'succeeded'}
        self.game.query.side_effect=None;self.game.query.return_value=self.native_success()
        other={'world_id':'w','catalog_fingerprint':'catalog','sources':{'crude-oil':{'unit_number':999}}}
        self.c.sources_path.write_text(json.dumps(other));before=self.c.sources_path.read_bytes()
        self.assertEqual(self.c.step(self.obs)['reason'],'published_crude_output_identity_changed')
        self.assertEqual(self.c.sources_path.read_bytes(),before)

    @unittest.skipUnless(LuaRuntime,'optional Lua runtime: pip install lupa')
    def test_flattened_native_proof_executes_real_peer_power_fluid_and_trigger_guards(self):
        self.install();lua=LuaRuntime(unpack_returned_tuples=True)
        def table(v):
            if isinstance(v,dict):return lua.table_from({k:table(a)for k,a in v.items()})
            if isinstance(v,list):return lua.table_from([table(a)for a in v])
            return v
        def decode(v):
            if not hasattr(v,'items'):return v
            keys=list(v.keys())
            if keys and all(type(k)is int for k in keys)and sorted(keys)==list(range(1,len(keys)+1)):
                return [decode(v[i])for i in range(1,len(keys)+1)]
            return {k:decode(a)for k,a in v.items()}
        chain=[self.c.state['core']['pumpjack']]+self.c.state['core']['pipes']
        lua.globals().specs=table([dict(e,unit_number=self.c.state['bindings'][_key(e)])for e in chain])
        lua.globals().well_position=table(self.c.state['well']['position'])
        lua.globals().helpers=lua.table_from({'json_to_table':lambda raw:table(json.loads(raw))})
        lua.execute('''f={technologies={["oil-processing"]={researched=true}}};s={};game={tick=100};powered=true;one_way=0;foreign_fluid=false
function pos(p)return {x=p.x,y=p.y}end
local function setup(i,e)
 e.force=f;e.surface=s;e.energy=100;e.is_connected_to_electric_network=function()return powered end
 e.get_fluid_contents=function()if foreign_fluid then return {water=10}else return {["crude-oil"]=10}end end
 e.get_fluid_box_neighbours=function()local peers={}
  if i>1 and one_way~=i then peers[#peers+1]={entity=specs[i-1],index=1}end
  if i<#specs then peers[#peers+1]={entity=specs[i+1],index=1}end
  return peers end
end
for i,e in ipairs(specs)do setup(i,e)end
function target(p,name)
 if name=="crude-oil"then return {name=name,amount=10000,position=well_position}end
 for _,e in ipairs(specs)do if e.name==name and e.position.x==p.x and e.position.y==p.y then return e end end end''')
        self.game.query=lambda body:decode(lua.execute('return (function() '+' '.join(body.splitlines())+' end)()'))
        self.assertIsNotNone(self.c.proof(self.obs))
        for change,restore in (('one_way=3','one_way=0'),('powered=false','powered=true'),
                ('f.technologies["oil-processing"].researched=false','f.technologies["oil-processing"].researched=true'),
                ('foreign_fluid=true','foreign_fluid=false'),('specs[2].unit_number=999','specs[2].unit_number='+str(self.c.state['bindings'][_key(chain[1])]))):
            with self.subTest(change=change):
                lua.execute(change)
                self.assertIsNone(self.c.proof(self.obs))
                lua.execute(restore)


if __name__=='__main__':unittest.main()
