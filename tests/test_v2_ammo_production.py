from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_agent.ammo_production import AmmoProduction,ammo_core,ammo_rates,_key
from factorio_agent.ammo_transfer import validate_ammo_action
from factorio_agent.main_bus import BusMaterial,ConsumerTap,plan_main_bus


def catalog():return SimpleNamespace(fingerprint='catalog',items={'firearm-magazine':{'stack_size':100}},
    recipes={'firearm-magazine':{'energy':1,'ingredients':[{'name':'iron-plate','amount':4,'type':'item'}],
                                 'products':[{'name':'firearm-magazine','amount':1,'type':'item'}]}},
    entities={'assembling-machine-1':{'crafting_speed':.5}})


class AmmoProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.cat=catalog();self.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root),query=Mock())
        geometry=FactoryBuilder.__new__(FactoryBuilder)
        self.builder=SimpleNamespace(_occupied_by_plan=geometry._occupied_by_plan,
            ensure_plan=Mock(return_value={'status':'waiting','reason':'normally construct ammo core'}),
            bootstrap=SimpleNamespace(ensure_item=Mock()))
        self.defense=SimpleNamespace(prepare_coverage=Mock(return_value={'ok':True}),ensure_coverage=Mock(return_value=None),
            coverage_proof=Mock(return_value={'geometry':{'complete':True}}))
        bus=plan_main_bus([BusMaterial('iron-plate',-11.5,(375,),2)],
            [ConsumerTap('old-green','iron-plate',-80.5,'normal',0,100)],north_y=-122.5,south_y=-10.5,port_x=12.5)
        self.board={'world_id':'w','catalog_fingerprint':'catalog','blocks':{},'shared_main_bus':bus}
        (self.root/'pcb-board.json').write_text(json.dumps(self.board))
        docs={'board':self.board,'factory':{'world_id':'w','catalog_fingerprint':'catalog','bus':bus}}
        self.source={'kind':'item','item':'iron-plate','direction':'output','facing':4,'priority':'critical',
            'position':{'x':12.5,'y':-42.5},'name':'ammo-iron','unit_number':90,
            'priority_splitter':{'name':'splitter','position':{'x':-11.5,'y':-41.5},'unit_number':91,'direction':0}}
        self.tap=SimpleNamespace(state={},_documents=Mock(return_value=docs),_save=Mock(),
            step=Mock(return_value={'status':'succeeded','evidence':{'port':self.source}}))
        preview={'ok':True,'new_bus':{'ports':[self.source]},'added_entities':[],
                 'remove':{'unit_number':99},'phase':'stock','parameters':{}}
        self.preview=patch('factorio_agent.ammo_production.BusTap',return_value=SimpleNamespace(prepare=Mock(return_value=preview))).start()
        self.land=patch('factorio_agent.ammo_production.survey_slots',return_value={'ok':True,'legal':[1]}).start()
        self.site=patch('factorio_agent.ammo_production.industrial_site_anchors',return_value=((40,-40),)).start()
        self.preflight=patch('factorio_agent.ammo_production.preflight',return_value={'ok':True,'clear_obstacles':[]}).start()
        self.route=patch('factorio_agent.ammo_production.plan_connection',return_value={
            'ok':True,'entities':[],'underground_pairs':[],'route_metrics':{'length':5,'turns':0}}).start()
        self.power=patch('factorio_agent.ammo_production.connect_power_groups',return_value={'ok':True,'entities':[]}).start()
        self.addCleanup(patch.stopall)
        self.c=AmmoProduction(self.game,self.builder,self.cat,self.defense,tap=self.tap)
        self.obs={'ok':True,'world_id':'w','tick':10,'actor_unit_number':11,'entities':[],
            'enabled_recipes':{name:True for name in('firearm-magazine','assembling-machine-1','fast-inserter','inserter','wooden-chest')}}
        self.buffer={'ok':True,'tick':10,'unit_number':100,'actor_unit':11,'full_magazines':2,
                     'source_slots':[1],'actor_full_magazines':0,'insertable':100,'bar_slots':1}
        self.game.query.side_effect=lambda script:deepcopy(self.buffer)if script.startswith('local spec=')else{
            'ok':True,'tick':10,'assembler_unit':109,'products_finished':1}

    def installed(self):
        self.c.state=self.c.prepare(self.obs);self.assertTrue(self.c.state['ok'],self.c.state)
        self.c.state.update(phase='build',iron_source=self.source)
        self.obs['entities']=[dict(e,unit_number=i+100,energy=100,electric_network_connected=True)
                              for i,e in enumerate(self.c.state['plan']['entities'])]
        self.assertIsNone(self.c._bind(self.obs))
        self.buffer['unit_number']=self.c.state['bindings'][_key(self.c.state['core']['chest'])]

    def test_nominal_rates_real_recipe_and_compact_native_arm_geometry(self):
        core=ammo_core(self.cat,40,-40);geometry=FactoryBuilder.__new__(FactoryBuilder)
        boxes=[geometry._occupied_by_plan([e])for e in core['entities']]
        self.assertEqual(sum(map(len,boxes)),len(set().union(*boxes)))
        self.assertEqual(core['rated']['magazines_per_minute'],30)
        self.assertEqual(core['rated']['iron_per_minute'],120)
        self.assertEqual(core['rated']['chest_magazine_limit'],100)
        self.assertEqual(core['input_arm']['name'],'fast-inserter')
        self.assertEqual(core['input_arm']['position']['y']-1,core['ports'][0]['position']['y'])
        self.assertEqual(core['output_arm']['position']['y']+1,core['chest']['position']['y'])
        self.assertFalse(geometry._occupied_by_plan(core['output_access'])&geometry._occupied_by_plan(core['entities']))
        self.cat.recipes['firearm-magazine']['products'][0]['probability']=.5
        with self.assertRaises(ValueError):ammo_rates(self.cat)

    def test_prepare_is_readonly_full_prospective_critical_tap_and_output_access(self):
        original=(self.root/'pcb-board.json').read_bytes()
        with patch('factorio_agent.ammo_production._atomic_json')as write:
            result=self.c.prepare(self.obs);write.assert_not_called()
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['parameters']['priority'],'critical')
        self.assertEqual(result['parameters']['demand_per_minute'],120)
        self.assertEqual(self.c.state,{})
        self.assertEqual((self.root/'pcb-board.json').read_bytes(),original)
        self.assertEqual(self.preflight.call_args.kwargs['ignored_units'],[99])
        self.assertTrue(all(e in self.power.call_args.args[4]for e in result['core']['output_access']))

    def test_recipes_or_another_unfinished_tap_block_before_cost(self):
        self.assertFalse(self.c.prepare({**self.obs,'enabled_recipes':{}})['ok'])
        self.tap.state={'phase':'recover','parameters':{'name':'steel-iron'}}
        self.assertEqual(self.c.prepare(self.obs)['reason'],'another_bus_tap_cutover_unfinished')
        self.builder.ensure_plan.assert_not_called()

    def test_defense_gate_precedes_any_ammo_producer_build(self):
        self.defense.ensure_coverage.return_value={'type':'craft','recipe':'firearm-magazine','count':10}
        result=self.c.step(self.obs)
        self.assertEqual(result['recipe'],'firearm-magazine')
        self.builder.ensure_plan.assert_not_called()
        self.assertFalse(self.c.commissioned)

    def test_exact_single_slot_chest_bar_is_configured_before_machine_construction(self):
        self.installed();self.buffer['bar_slots']=16
        action=self.c.step(self.obs)
        self.assertEqual(action['type'],'configure_ammo_buffer')
        validate_ammo_action(action,kind='configure_ammo_buffer',expected_world='w')
        self.builder.ensure_plan.assert_not_called()

    def test_real_output_is_required_then_source_commissions(self):
        self.installed();self.builder.ensure_plan.return_value={'status':'succeeded'}
        self.buffer['full_magazines']=0
        self.assertEqual(self.c.step(self.obs)['status'],'waiting')
        self.assertFalse(self.c.commissioned)
        self.buffer['full_magazines']=1
        result=self.c.step(self.obs)
        self.assertEqual(result['status'],'succeeded')
        self.assertTrue(self.c.commissioned)
        self.assertEqual(result['evidence']['output']['unit_number'],self.buffer['unit_number'])

    def test_destroyed_or_replaced_known_assets_block_and_are_not_retargeted(self):
        self.installed();old=self.obs['entities'][0]['unit_number'];self.obs['entities'][0]['unit_number']+=500
        self.assertEqual(self.c.step(self.obs)['reason'],'owned_ammo_asset_missing_or_replaced')
        self.assertEqual(self.c.state['bindings'][_key(self.obs['entities'][0])],old)
        self.assertIsNone(self.c.proof(self.obs))

    def test_after_commission_empty_buffer_waits_instead_of_handcrafting(self):
        self.installed();self.c.state['phase']='verified';self.buffer['full_magazines']=0
        result=self.c.ensure_full_magazines(self.obs,10)
        self.assertEqual(result['status'],'waiting')
        self.builder.bootstrap.ensure_item.assert_not_called()

    def test_refill_can_restore_global_coverage_without_recursive_defense_proof(self):
        self.installed();self.c.state['phase']='verified';self.defense.coverage_proof.return_value=None
        self.defense.coverage_proof.reset_mock()
        action=self.c.ensure_full_magazines(self.obs,10)
        self.assertEqual((action['type'],action['count']),('take_full_magazines',2))
        validate_ammo_action(action,kind='take_full_magazines',expected_world='w')
        self.defense.coverage_proof.assert_not_called()

    def test_refill_requests_are_bounded_and_only_full_actor_stock_counts(self):
        self.installed();self.c.state['phase']='verified'
        self.game.query.reset_mock()
        for count in (True,0,101):
            with self.assertRaises(ValueError):self.c.ensure_full_magazines(self.obs,count)
        self.game.query.assert_not_called()
        self.buffer.update(actor_full_magazines=9,full_magazines=80)
        self.assertEqual(self.c.ensure_full_magazines(self.obs,10)['count'],1)
        self.buffer['actor_full_magazines']=10
        self.assertIsNone(self.c.ensure_full_magazines(self.obs,10))
        self.buffer.update(actor_full_magazines=0,insertable=0)
        self.assertEqual(self.c.ensure_full_magazines(self.obs,10)['reason'],'actor_inventory_has_no_ammo_capacity')

    def test_sixty_magazine_target_uses_fifty_then_ten_without_raising_transfer_cap(self):
        self.installed();self.c.state['phase']='verified'
        self.buffer.update(actor_full_magazines=0,full_magazines=100,insertable=100)
        first=self.c.ensure_full_magazines(self.obs,60);self.assertEqual(first['count'],50)
        validate_ammo_action(first,kind='take_full_magazines',expected_world='w')
        self.buffer.update(actor_full_magazines=50,full_magazines=50)
        second=self.c.ensure_full_magazines(self.obs,60);self.assertEqual(second['count'],10)
        validate_ammo_action(second,kind='take_full_magazines',expected_world='w')
        self.buffer['actor_full_magazines']=60
        self.assertIsNone(self.c.ensure_full_magazines(self.obs,60))
        self.buffer['actor_full_magazines']=0
        self.assertEqual(self.c.ensure_full_magazines(self.obs,100)['count'],50)

    def test_real_lua_factory_proof_checks_current_production_power_drop_and_critical_priority(self):
        from tests.test_v2_ammo_transfer import NativeAmmoTransferTests,LuaRuntime
        if not LuaRuntime:self.skipTest('optional Lua runtime: pip install lupa')
        self.installed();fixture=NativeAmmoTransferTests();fixture.setUp();lua=fixture.lua
        def table(v):
            if isinstance(v,dict):return lua.table_from({k:table(a)for k,a in v.items()})
            if isinstance(v,list):return lua.table_from([table(a)for a in v])
            return v
        records=[dict(e,unit_number=self.c.state['bindings'][_key(e)])for e in self.c.state['plan']['entities']
                 if e['name']!='wooden-chest']
        records += [dict(self.source,name='transport-belt',direction=4),dict(self.source['priority_splitter'],
            splitter_input_priority='left',splitter_output_priority='right')]
        lua.globals().entities=table(records)
        lua.globals().chest_position=table(self.c.state['core']['chest']['position'])
        lua.globals().chest_unit=self.buffer['unit_number']
        lua.execute('''game={tick=150};bar=2;chest.position=chest_position;chest.unit_number=chest_unit
entities[#entities+1]=chest;powered=true;produced=1;wrong_output=false;foreign_iron=false
function target(p,name)for _,e in ipairs(entities)do if e.valid~=false and e.name==name and e.position.x==p.x and e.position.y==p.y then return e end end end
for _,e in ipairs(entities)do e.force=f;e.surface=s;e.energy=100;e.is_connected_to_electric_network=function()return powered end
 if e.name=="assembling-machine-1"then assembler=e;e.products_finished=1;e.get_recipe=function()return {name="firearm-magazine"}end end
 if e.name=="inserter"or e.name=="fast-inserter"then
  e.pickup_position={x=e.position.x,y=e.position.y-1};e.drop_position={x=e.position.x,y=e.position.y+1}
  if e.name=="inserter"then output_arm=e end
 end
 if e.name=="transport-belt"then e.get_transport_line=function()return {get_contents=function()return {{name=foreign_iron and "copper-plate"or "iron-plate",count=4}}end}end end
 if e.name=="splitter"then priority=e end
end''')
        self.c.game=fixture.game
        self.assertIsNotNone(self.c.proof(self.obs))
        cases=(('assembler.products_finished=0','assembler.products_finished=1'),('powered=false','powered=true'),
            ('output_arm.drop_position.y=999','output_arm.drop_position.y=chest.position.y'),
            ('priority.splitter_output_priority="left"','priority.splitter_output_priority="right"'),
            ('foreign_iron=true','foreign_iron=false'),('chest.valid=false','chest.valid=true'))
        for change,restore in cases:
            with self.subTest(change=change):
                lua.execute(change);self.assertIsNone(self.c.proof(self.obs));lua.execute(restore)


if __name__=='__main__':unittest.main()
