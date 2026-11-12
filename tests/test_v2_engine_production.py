from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_ai.deterministic_builder import FactoryBuilder,_plan
from factorio_agent.board import entity
from factorio_agent.engine_production import (ENGINE_PROOF_LUA,EngineProduction,_hash,_key,
    audit_engine_core,engine_core,engine_rates,_clear_planned_power_group)
from factorio_agent.main_bus import BusMaterial,ConsumerTap,plan_main_bus
from factorio_agent.transport import xy

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


def catalog():
    recipes={}
    for name,seconds,ingredients in(('engine-unit',10,{'steel-plate':1,'iron-gear-wheel':1,'pipe':2}),
        ('iron-gear-wheel',.5,{'iron-plate':2}),('pipe',.5,{'iron-plate':1})):
        recipes[name]={'energy':seconds,'category':'advanced-crafting'if name=='engine-unit'else'crafting',
            'ingredients':[{'name':n,'type':'item','amount':v}for n,v in ingredients.items()],
            'products':[{'name':name,'type':'item','amount':1}]}
    return SimpleNamespace(fingerprint='c',recipes=recipes,entities={'assembling-machine-1':{
        'crafting_speed':.5,'crafting_categories':['crafting','advanced-crafting']}})


class EngineCoreTests(unittest.TestCase):
    def test_actual_recipe_balances_and_conservative_normal_inserter_capacity(self):
        c=catalog();r=engine_rates(c)
        self.assertEqual((r['engines_per_minute'],r['iron_per_minute'],r['steel_per_minute']),(6,24,6))
        self.assertEqual((r['gear_per_minute'],r['pipe_per_minute']),(6,12))
        self.assertEqual(r['component_nominal_iron_per_minute'],180)
        self.assertEqual(engine_rates(c,4)['engines_per_minute'],12)
        c.recipes['engine-unit']['ingredients'][2]['amount']=10
        with self.assertRaises(ValueError):engine_rates(c)

    def test_all_ingredients_and_exact_mixed_lanes_reach_all_current_and_future_machines(self):
        for count in(2,3,4):
            core=engine_core(catalog(),.5,.5,count);audit=audit_engine_core(core,catalog())
            self.assertTrue(audit['ok'],audit['errors']);self.assertEqual(audit['machine_count'],count+2)
            self.assertEqual(len(core['underground_pairs']),4)
            self.assertEqual(len([a for a in audit['arm_targets']if a['name']=='long-handed-inserter']),count)
            self.assertEqual(core['reserved_area'],[[-4,-0.0],[27,19]])
            for a in audit['arm_targets']:self.assertTrue(a['allowed_items'])

    def test_expansion_keeps_all_existing_assets_ports_and_reservation_exact(self):
        small=engine_core(catalog(),.5,.5);large=engine_core(catalog(),.5,.5,4)
        self.assertEqual(small['ports'],large['ports']);self.assertEqual(small['reserved_area'],large['reserved_area'])
        self.assertTrue(all(e in large['entities']for e in small['entities']))
        self.assertEqual(small['required_items'],{'transport-belt':103,'assembling-machine-1':4,
            'inserter':8,'underground-belt':8,'long-handed-inserter':2,'small-electric-pole':8})
        self.assertFalse(any('drop_position'in e or 'pickup_position'in e for e in small['entities']))

    def test_empty_inline_tail_separates_pipe_corner_from_gear_lane(self):
        c=catalog();p=engine_core(c,.5,.5)
        tail=entity('transport-belt',-1.5,7.5,4)
        self.assertIn(tail,p['entities']);self.assertTrue(audit_engine_core(p,c)['ok'])
        p['entities'].remove(tail)
        errors=audit_engine_core(p,c)['errors']
        self.assertTrue(any(e.startswith('engine_component_lane_mismatch:')for e in errors),errors)

    def test_bad_native_long_arm_or_lane_path_is_rejected_by_static_audit(self):
        c=catalog();p=engine_core(c,.5,.5)
        next(e for e in p['entities']if e['name']=='long-handed-inserter')['name']='inserter'
        self.assertFalse(audit_engine_core(p,c)['ok'])
        p=engine_core(c,.5,.5);p['entities']=[e for e in p['entities']if xy(e)!=(.5,6.5)]
        self.assertFalse(audit_engine_core(p,c)['ok'])
        p=engine_core(c,.5,.5);p['entities'].append(entity('small-electric-pole',15.5,10.5))
        self.assertIn('future_engine_slot_occupied',audit_engine_core(p,c)['errors'])
        p=engine_core(c,.5,.5);p['entities'].append(dict(entity('underground-belt',9.5,8.5,12),belt_to_ground_type='output'))
        self.assertIn('underground_pair_can_be_stolen',audit_engine_core(p,c)['errors'])


class EngineControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.cat=catalog();self.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root),query=Mock(return_value={'ok':True,'pole_index':1}))
        self.builder=SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,
            ensure_plan=Mock(return_value={'type':'craft','recipe':'assembling-machine-1','count':1}))
        self.defense=SimpleNamespace(prepare_coverage=Mock(return_value={'ok':True}),ensure_coverage=Mock(return_value=None),
                                    coverage_proof=Mock(return_value={'complete':True}))
        bus=plan_main_bus([BusMaterial('iron-plate',-11.5,(375,),2)],
            [ConsumerTap('science','iron-plate',-80.5,'critical',demand_per_minute=100)],north_y=-122.5,south_y=-10.5,port_x=12.5)
        self.board={'world_id':'w','catalog_fingerprint':'c','blocks':{},'shared_main_bus':bus}
        (self.root/'pcb-board.json').write_text(json.dumps(self.board))
        docs={'board':self.board,'factory':{'world_id':'w','catalog_fingerprint':'c','bus':bus}}
        self.iron={'kind':'item','item':'iron-plate','direction':'output','facing':4,'priority':'surplus',
            'position':{'x':12.5,'y':-92.5},'name':'engine-iron','unit_number':90,
            'priority_splitter':{'name':'splitter','position':{'x':-11,'y':-91.5},'unit_number':91,'direction':0}}
        self.tap=SimpleNamespace(state={},_documents=Mock(return_value=docs),_save=Mock(),
            step=Mock(return_value={'status':'succeeded','evidence':{'port':self.iron}}))
        self.steel_source={'kind':'item','item':'steel-plate','direction':'output','facing':4,'position':{'x':108.5,'y':-37.5}}
        self.steel=SimpleNamespace(state={'world_id':'w','catalog_fingerprint':'c','phase':'verified',
            'plan':{'entities':[entity('transport-belt',108.5,-37.5,4)]},
            'core':{'ports':[self.steel_source],'reserved_area':[[91.5,-45.5],[110,-31.5]]}},
            proof=Mock(return_value={'furnace_units':[6172,6176],'rated':{'steel_per_minute':7.5}}))
        preview={'ok':True,'new_bus':{'ports':[self.iron]},'added_entities':[],
            'remove':{'unit_number':99},'phase':'stock','parameters':{}}
        self.preview=patch('factorio_agent.engine_production.BusTap',return_value=SimpleNamespace(prepare=Mock(return_value=preview))).start()
        self.land=patch('factorio_agent.engine_production.survey_slots',return_value={'ok':True,'legal':[1]}).start()
        self.preflight=patch('factorio_agent.engine_production.preflight',return_value={'ok':True,'clear_obstacles':[]}).start()
        self.route=patch('factorio_agent.engine_production.plan_connection',return_value=_plan([],[],underground_pairs=[],route_metrics={'length':10,'turns':1})).start()
        self.power=patch('factorio_agent.engine_production.connect_power_groups',return_value={'ok':True,'entities':[]}).start()
        self.addCleanup(patch.stopall)
        self.c=EngineProduction(self.game,self.builder,self.cat,self.defense,tap=self.tap,steel=self.steel)
        self.obs={'ok':True,'world_id':'w','tick':10,'technologies':{'engine':True},
            'enabled_recipes':{r:True for r in('engine-unit','iron-gear-wheel','pipe','assembling-machine-1','inserter','long-handed-inserter')},
            'entities':[dict(entity('transport-belt',108.5,-37.5,4),unit_number=6213,belt_inventory={'steel-plate':8})]}

    def install(self):
        self.c.state=self.c.prepare(self.obs);self.assertTrue(self.c.state['ok'],self.c.state)
        self.c.state.update(phase='build',iron_source=self.iron)
        self.obs['entities'] += [dict(e,unit_number=i+100)for i,e in enumerate(self.c.state['plan']['entities'])]
        self.assertIsNone(self.c._bind(self.obs))
        mouths={xy(e):e for e in self.obs['entities']if e['name']=='underground-belt'}
        for p in self.c.state['plan']['underground_pairs']:
            a,b=mouths[tuple(p['input'])],mouths[tuple(p['output'])]
            for one,two in((a,b),(b,a)):one.update(underground_pair_verified=True,underground_neighbour={'unit_number':two['unit_number']})

    def test_prepare_is_readonly_whole_tap_two_routes_future_power_and_defense(self):
        original=(self.root/'pcb-board.json').read_bytes()
        with patch('factorio_agent.engine_production._atomic_json')as write:
            result=self.c.prepare(self.obs);write.assert_not_called()
        self.assertTrue(result['ok'],result);self.assertFalse(self.c.path.exists());self.assertEqual(self.c.state,{})
        self.assertEqual((self.root/'pcb-board.json').read_bytes(),original)
        self.assertEqual((result['parameters']['priority'],result['parameters']['demand_per_minute']),('surplus',24))
        self.assertEqual(result['steel_source']['unit_number'],6213)
        self.assertEqual(result['steel_furnace_units'],[6172,6176]);self.assertEqual(result['shared_steel_nominal_per_minute'],7.5)
        self.assertEqual(self.route.call_count,2);self.assertEqual(self.preflight.call_count,2)
        self.assertEqual(self.preflight.call_args.kwargs['ignored_units'],[99]);self.builder.ensure_plan.assert_not_called()

    def test_natural_research_current_steel_and_unfinished_cutover_gate_all_cost(self):
        self.assertEqual(self.c.prepare({**self.obs,'technologies':{}})['reason'],'native_engine_technology_and_component_machine_recipes_required')
        self.steel.proof.return_value=None
        self.assertEqual(self.c.prepare(self.obs)['reason'],'current_native_steel_production_proof_required')
        self.steel.proof.return_value={'furnace_units':[6172,6176],'rated':{'steel_per_minute':7.5}}
        self.tap.state={'phase':'recover'}
        self.assertEqual(self.c.prepare(self.obs)['reason'],'another_bus_tap_cutover_unfinished')
        self.builder.ensure_plan.assert_not_called();self.assertFalse(self.c.path.exists())

    def test_steel_route_failure_is_precise_and_does_not_cut_bus_or_persist(self):
        self.route.return_value={'ok':False,'reason':'no_clean_trunk_corridor','visited':123}
        result=self.c.prepare(self.obs)
        self.assertEqual(result['evidence']['failures'][0]['stage'],'steel_route')
        self.assertEqual(result['evidence']['failures'][0]['detail']['visited'],123)
        self.assertFalse(self.c.path.exists());self.tap.step.assert_not_called()

    def test_round_robin_considers_third_core_before_second_station_at_first_core(self):
        self.land.return_value={'ok':True,'legal':[1,2,3]};calls=[]
        def route(game,builder,obs,source,destination,reserved):
            calls.append((source['item'],dict(destination['position'])))
            if source['item']=='iron-plate'and destination['position']['y']==-92.5:
                return {'ok':False,'reason':'no_clean_trunk_corridor','visited':0}
            return _plan([],[],underground_pairs=[],route_metrics={'length':10,'turns':1})
        self.route.side_effect=route
        result=self.c.prepare(self.obs);self.assertTrue(result['ok'],result)
        self.assertEqual(result['core']['anchor'],{'x':118.5,'y':-114.5})
        self.assertEqual([item for item,_ in calls],['steel-plate','iron-plate']*3)
        self.assertEqual(self.preview.return_value.prepare.call_count,3);self.assertEqual(result['route_attempts'],6)
        self.assertEqual(self.c.state,{})

    def test_second_station_pass_reuses_only_current_prepare_steel_routes(self):
        self.land.return_value={'ok':True,'legal':[1,2]};counts={'iron':0,'steel':0}
        def route(game,builder,obs,source,destination,reserved):
            if source['item']=='steel-plate':counts['steel']+=1
            else:
                counts['iron']+=1
                if counts['iron']<=2:return {'ok':False,'reason':'no_clean_trunk_corridor'}
            return _plan([],[],underground_pairs=[],route_metrics={'length':10,'turns':1})
        self.route.side_effect=route
        result=self.c.prepare(self.obs);self.assertTrue(result['ok'],result)
        self.assertEqual(counts,{'iron':3,'steel':2});self.assertEqual(result['route_attempts'],5)
        self.assertEqual(self.c.state,{})

    def test_power_group_chooses_only_an_existing_planned_pole_without_changing_plan(self):
        core=engine_core(self.cat,118.5,-114.5);before=deepcopy(core)
        poles=[e for e in core['entities']if e['name']=='small-electric-pole']
        for index in(1,2):
            self.game.query.return_value={'ok':True,'pole_index':index}
            result=_clear_planned_power_group(self.game,core);self.assertTrue(result['ok'])
            selected=next(e for e in result['group']['entities']if e['name']=='small-electric-pole')
            self.assertEqual(selected,poles[index-1]);self.assertEqual(core,before)
            self.assertCountEqual(result['group']['entities'],core['entities'])
        self.game.query.return_value={'ok':True,'pole_index':99}
        self.assertFalse(_clear_planned_power_group(self.game,core)['ok'])

    @unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
    def test_native_power_group_skips_obstructed_first_pole_and_preserves_owned_first(self):
        lua=LuaRuntime(unpack_returned_tuples=True);core=engine_core(self.cat,118.5,-114.5)
        poles=[e for e in core['entities']if e['name']=='small-electric-pole'];before=deepcopy(core)
        def table(v):
            if isinstance(v,dict):return lua.table_from({k:table(a)for k,a in v.items()})
            if isinstance(v,list):return lua.table_from([table(a)for a in v])
            return v
        lua.globals().helpers=lua.table_from({'json_to_table':lambda raw:table(json.loads(raw))})
        lua.globals().first=table(poles[0]['position']);lua.globals().second=table(poles[1]['position'])
        lua.execute('''f={};s={};owned=false;blocked_all=false
s.can_place_entity=function(q)return not blocked_all and q.position.x==second.x and q.position.y==second.y end
function target(p,name)if owned and p.x==first.x and p.y==first.y then return{force=f,surface=s,position=p}end end''')
        game=SimpleNamespace(query=lambda body:dict(lua.execute('return(function() '+' '.join(body.splitlines())+' end)()').items()))
        result=_clear_planned_power_group(game,core);self.assertTrue(result['ok']);self.assertEqual(result['group']['entities'][0],poles[1])
        lua.execute('owned=true');result=_clear_planned_power_group(game,core)
        self.assertEqual(result['group']['entities'][0],poles[0]);self.assertEqual(core,before)
        lua.execute('owned=false;blocked_all=true');self.assertFalse(_clear_planned_power_group(game,core)['ok'])

    def test_round_robin_keeps_one_twelve_route_budget(self):
        self.land.return_value={'ok':True,'legal':[1,2,3,4]}
        def route(game,builder,obs,source,destination,reserved):
            if source['item']=='iron-plate':return {'ok':False,'reason':'no_clean_trunk_corridor'}
            return _plan([],[],underground_pairs=[],route_metrics={'length':10,'turns':1})
        self.route.side_effect=route
        result=self.c.prepare(self.obs);self.assertFalse(result['ok']);self.assertEqual(result['route_attempts'],12)
        self.assertEqual(self.route.call_count,12);self.assertEqual(self.c.state,{})

    def test_native_vectors_or_total_planning_budget_fail_before_cutover(self):
        self.game.query.return_value={'ok':False,'reason':'unsupported_native_engine_inserter_vectors'}
        self.assertEqual(self.c.prepare(self.obs)['reason'],'unsupported_native_engine_inserter_vectors')
        self.tap.step.assert_not_called();self.assertFalse(self.c.path.exists())
        self.game.query.return_value={'ok':True}
        with patch('factorio_agent.engine_production.PlanningBudget')as budget_type:
            budget_type.return_value=SimpleNamespace(game=self.game,expired=lambda:True,
                failure=lambda **extra:dict(ok=False,reason='industrial_planning_budget_exhausted',**extra))
            result=self.c.prepare(self.obs)
        self.assertEqual(result['reason'],'industrial_planning_budget_exhausted')
        self.assertFalse(self.c.path.exists());self.assertEqual(self.c.state,{})

    def test_defense_and_fresh_preflight_precede_any_producer_build(self):
        self.defense.ensure_coverage.return_value={'type':'craft','recipe':'firearm-magazine','count':10}
        self.assertEqual(self.c.step(self.obs)['recipe'],'firearm-magazine');self.builder.ensure_plan.assert_not_called()
        self.defense.ensure_coverage.return_value=None;self.preflight.return_value={'ok':False,'reason':'new_turret_on_route'}
        self.assertEqual(self.c.step(self.obs)['reason'],'engine_site_changed_after_defense');self.builder.ensure_plan.assert_not_called()

    def test_ordinary_construction_has_no_actor_feed_and_known_asset_loss_blocks(self):
        self.install();self.assertEqual(self.c.step(self.obs)['recipe'],'assembling-machine-1')
        self.obs['entities'].pop();self.assertEqual(self.c.step(self.obs)['reason'],'owned_engine_asset_missing_or_replaced')
        self.assertNotIn('insert',str(self.builder.ensure_plan.call_args.kwargs))

    def test_preserved_pipe_peer_change_blocks_before_builder_and_invalidates_proof(self):
        self.install();self.builder.ensure_plan.reset_mock()
        with patch('factorio_agent.engine_production.surface_pipe_proof',return_value={'ok':False,'reason':'preserved_surface_pipe_peer_identity_changed'}):
            self.assertEqual(self.c.step(self.obs)['reason'],'engine_preserved_pipe_network_changed')
            self.builder.ensure_plan.assert_not_called();self.assertIsNone(self.c.proof(self.obs))
            self.assertEqual(self.c.last_proof_failure['reason'],'preserved_surface_pipe_peer_identity_changed')

    def test_active_defense_relocation_gates_engine_builder_and_reobserves_after_completion(self):
        self.install();self.builder.ensure_plan.reset_mock()
        self.c.relocation=SimpleNamespace(active=True,step=Mock(return_value={'status':'waiting','reason':'paid replacement first'}))
        self.assertEqual(self.c.step(self.obs)['reason'],'paid replacement first');self.builder.ensure_plan.assert_not_called()
        self.c.relocation.step.return_value={'status':'succeeded'}
        self.assertEqual(self.c.step(self.obs)['reason'],'defense_relocation_complete_reobserve_engine_site')
        self.builder.ensure_plan.assert_not_called()

    def test_bound_steel_plan_change_or_unpaired_mouth_fails_closed(self):
        self.install();self.steel.state['plan']['entities'].append(entity('transport-belt',109.5,-37.5,4))
        self.assertEqual(self.c.step(self.obs)['reason'],'engine_bound_steel_plan_changed')
        self.steel.state['plan']['entities'].pop()
        self.steel.proof.return_value['furnace_units']=[999,6176]
        self.assertIsNone(self.c.proof(self.obs));self.assertEqual(self.c.last_proof_failure['reason'],'engine_bound_steel_furnace_units_changed')
        self.steel.proof.return_value['furnace_units']=[6172,6176]
        next(e for e in self.obs['entities']if e['name']=='underground-belt')['underground_pair_verified']=False
        self.assertIsNone(self.c.proof(self.obs));self.assertEqual(self.c.last_proof_failure['reason'],'engine_native_underground_pairs_unverified')

    @unittest.skipUnless(LuaRuntime,'optional Lua runtime: pip install lupa')
    def test_actual_proof_lua_normal_inputs_vectors_priority_and_real_output(self):
        self.install();lua=LuaRuntime(unpack_returned_tuples=True)
        def table(v):
            if isinstance(v,dict):return lua.table_from({k:table(a)for k,a in v.items()})
            if isinstance(v,list):return lua.table_from([table(a)for a in v])
            return v
        def decode(v):return{k:decode(a)for k,a in v.items()}if hasattr(v,'items')else v
        lua.globals().helpers=lua.table_from({'json_to_table':lambda raw:table(json.loads(raw))})
        lua.execute('''f={};s={};game={tick=20};defines={inventory={crafter_input=1}};entities={};powered=true;wrong_recipe=false;wrong_quality=false;foreign_item=false;bad_arm=false;empty_output=false;wrong_lane=false
function k(q)return q.name..":"..q.position.x..":"..q.position.y end
function target(p,name)return entities[name..":"..p.x..":"..p.y]end
function belt(q,items)
 q.force=f;q.surface=s;q.get_max_transport_line_index=function()return 2 end
 q.get_transport_line=function(index)return{get_contents=function()
  local result={};for _,name in ipairs(items[index]or{})do result[#result+1]={name=foreign_item and"copper-plate"or name,quality=wrong_quality and"uncommon"or"normal",count=1}end;return result end}end
 entities[k(q)]=q;return q end
function machine(q)
 q.force=f;q.surface=s;q.energy=100;q.products_finished=1;q.is_connected_to_electric_network=function()return powered end
 q.get_recipe=function()return{name=wrong_recipe and"firearm-magazine"or q.recipe}end
 q.get_inventory=function()return{{valid_for_read=true,name=q.recipe=="engine-unit"and"steel-plate"or"iron-plate",quality={name=wrong_quality and"uncommon"or"normal"}}}end
 q.get_output_inventory=function()return{{valid_for_read=true,name=foreign_item and"copper-plate"or q.recipe,quality={name="normal"}}}end
 entities[k(q)]=q;return q end
function arm(q)
 q.force=f;q.surface=s;q.energy=100;q.is_connected_to_electric_network=function()return powered end
 q.pickup_position=q.pickup_position;q.drop_position={x=q.drop_position.x,y=q.drop_position.y+.2};q.held_stack={valid_for_read=false}
 entities[k(q)]=q;return q end''')
        bindings=self.c.state['bindings'];core=self.c.state['core'];audit=self.c.state['audit']
        for spec in core['machines']:lua.globals().machine(table(dict(spec,unit_number=bindings[_key(spec)])))
        for spec in audit['arm_targets']:lua.globals().arm(table(dict(spec,unit_number=bindings[_key(spec)])))
        for spec in audit['belt_contracts']:
            items=spec['allowed_items'];lua.globals().belt(table(dict(spec,unit_number=bindings[_key(spec)])),table([items,items]))
        for p in[{'x':xy(m)[0],'y':xy(m)[1]-3}for m in core['engine_machines']]:
            spec=next(e for e in audit['belt_contracts']if e['position']==p)
            lua.globals().belt(table(dict(spec,unit_number=bindings[_key(spec)])),table([['iron-gear-wheel'],['pipe']]))
        for source in(self.c.state['steel_source'],dict(self.iron,name='transport-belt')):
            lua.globals().belt(table(dict(source,direction=source['facing'])),table([[source['item']],[source['item']]]))
        split=dict(self.iron['priority_splitter'],force=lua.globals().f,surface=lua.globals().s,
                   splitter_input_priority='left',splitter_output_priority='left')
        lua.globals().entities[_key(split)]=table(split)
        self.game.query.side_effect=lambda body:decode(lua.execute('return(function() '+' '.join(body.splitlines())+' end)()'))
        self.assertIsNotNone(self.c.proof(self.obs),self.c.last_proof_failure)
        for change,restore in(('powered=false','powered=true'),('wrong_recipe=true','wrong_recipe=false'),
            ('wrong_quality=true','wrong_quality=false'),('foreign_item=true','foreign_item=false')):
            lua.execute(change);self.assertIsNone(self.c.proof(self.obs),change);lua.execute(restore)
        first=lua.globals().entities[_key(core['machines'][0])];first.products_finished=0
        self.assertIsNone(self.c.proof(self.obs));first.products_finished=1
        split_live=lua.globals().entities[_key(split)];split_live.splitter_output_priority='right'
        self.assertIsNone(self.c.proof(self.obs));split_live.splitter_output_priority='left'
        arm=lua.globals().entities[_key(audit['arm_targets'][0])];arm.pickup_position.y+=1
        self.assertIsNone(self.c.proof(self.obs));arm.pickup_position.y-=1
        arm.held_stack=table({'valid_for_read':True,'name':'copper-plate','quality':{'name':'normal'}})
        self.assertIsNone(self.c.proof(self.obs));arm.held_stack=table({'valid_for_read':False})
        outlet=dict(core['ports'][-1],name='transport-belt');old=lua.globals().entities[_key(outlet)]
        lua.globals().belt(table(dict(outlet,direction=4,unit_number=bindings[_key(outlet)])),table([[],[]]))
        self.assertIsNone(self.c.proof(self.obs));lua.globals().entities[_key(outlet)]=old
        pickup={'position':{'x':xy(core['engine_machines'][0])[0],'y':xy(core['engine_machines'][0])[1]-3},'name':'transport-belt'}
        old=lua.globals().entities[_key(pickup)]
        lua.globals().belt(table(dict(old.items())),table([['pipe'],['iron-gear-wheel']]))
        self.assertIsNone(self.c.proof(self.obs));lua.globals().entities[_key(pickup)]=old
        source=lua.globals().entities[_key(dict(self.iron,name='transport-belt'))];source.unit_number=999
        self.assertIsNone(self.c.proof(self.obs));source.unit_number=90
        for forbidden in('create_entity','set_recipe','insert_fluid','set_research','.insert(','.remove('):
            self.assertNotIn(forbidden,ENGINE_PROOF_LUA)


if __name__=='__main__':unittest.main()
