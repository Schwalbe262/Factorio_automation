from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_agent.board import entity
from factorio_agent.radar_network import (RadarNetwork, radar_core, radar_candidates, planned_power_watts,
    RADAR_POWER_LUA, RADAR_SITE_LUA, RADAR_PROOF_LUA, _payload, _key)

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


def catalog():
    return SimpleNamespace(fingerprint='c', entities={
        'radar':{'type':'radar', 'electric':True, 'burner':False, 'energy_usage':5000,
                 'collision_box':{'left_top':{'x':-1.2, 'y':-1.2}, 'right_bottom':{'x':1.2, 'y':1.2}}},
        'small-electric-pole':{'type':'electric-pole', 'electric':False},
        'transport-belt':{'electric':False}, 'wooden-chest':{'electric':False},
        'chemical-plant':{'electric':True, 'energy_usage':3500},
        'steam-engine':{'electric':True, 'energy_usage':0}})


class RadarNetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root = Path(self.temp.name)
        self.catalog = catalog();self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root), query=Mock(side_effect=self.query))
        self.geometry = FactoryBuilder.__new__(FactoryBuilder)
        self.builder = SimpleNamespace(_occupied_by_plan=self.geometry._occupied_by_plan,
                                       ensure_plan=Mock(return_value={'type':'craft', 'recipe':'radar', 'count':1}))
        self.posts = [{'name':'post_'+str(i), 'position':{'x':x, 'y':.5}, 'unit_number':i} for x,i in ((-8.5,20), (8.5,21))]
        self.perimeter = SimpleNamespace(state={'posts':self.posts,'range':18}, prepare_coverage=Mock(return_value={'ok':True}),
            ensure_coverage=Mock(return_value=None), coverage_proof=Mock(return_value={'complete':True}))
        self.perimeter.inspect = Mock(return_value={'ok':True, 'posts':[dict(p, full_magazines=10) for p in self.posts]})
        self.source = dict(entity('small-electric-pole', -3.5, -.5), unit_number=1, electric_network_id=2)
        self.obs = {'ok':True, 'world_id':'w', 'tick':10, 'technologies':{'radar':True},
            'enabled_recipes':{'radar':True, 'small-electric-pole':True},
            'entities':[self.source, dict(entity('steam-engine', 4.5, 8.5), unit_number=50, energy=100, electric_network_id=2)]
                        +[dict(entity('gun-turret', p['position']['x'], p['position']['y']), unit_number=p['unit_number'],health=400,max_health=400) for p in self.posts]}
        self.board = {'world_id':'w', 'catalog_fingerprint':'c', 'blocks':{}}
        (self.root/'pcb-board.json').write_text(json.dumps(self.board))
        self.c = RadarNetwork(self.game, self.builder, self.catalog, self.perimeter)
        self.candidates = patch('factorio_agent.radar_network.radar_candidates', side_effect=lambda *args, oil=False, **kwargs:[] if oil else [(.5,.5)]).start()
        self.preflight = patch('factorio_agent.radar_network.preflight', return_value={'ok':True, 'clear_obstacles':[]}).start()
        self.power = patch('factorio_agent.radar_network.power_connection', return_value={
            'ok':True, 'entities':[entity('small-electric-pole', 2.5, 1.5)],
            'source_unit_number':1, 'source_position':self.source['position']}).start()
        self.addCleanup(patch.stopall)

    def query(self, script):
        if 'local flow=net.flow_last_tick' in script:return {'ok':True, 'tick':20, 'force_index':1, 'headroom_watts':2000000}
        if 'local armed={}' in script:return {'ok':True, 'tick':20,
            'covering_posts':[dict(p,full_magazines=10)for p in self.posts], 'charted_sample_chunks':9}
        if 'local radar=nil' in script:return {'ok':True, 'tick':20, 'scan_progress':.25, 'charted_sample_chunks':9}
        raise AssertionError('unexpected native query')

    def install(self):
        self.c.state = self.c.prepare(self.obs);self.assertTrue(self.c.state['ok'], self.c.state)
        self.obs['entities'] += [dict(e, unit_number=i+100) for i,e in enumerate(self.c.state['plan']['entities'])]
        self.assertIsNone(self.c._bind(self.obs))
        self.assertIsNone(self.c._reserve(self.obs,self.c._board(self.obs)))
        return self.c.state

    def test_full_three_by_three_body_separate_service_and_catalog_watts(self):
        core = radar_core(self.catalog, .5, .5)
        self.assertEqual(len(self.geometry._occupied_by_plan([core['radar']])), 9)
        self.assertEqual(core['watts'], 300000)
        self.assertEqual(core['required_items'], {'radar':1, 'small-electric-pole':1})
        boxes = [self.geometry._occupied_by_plan([e]) for e in core['entities']+core['service_keepouts']]
        self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))
        self.assertEqual(core['reserved_area'], [[-3,-3], [4,4]])
        with self.assertRaises(ValueError):radar_core(self.catalog, 0, .5)

    def test_candidate_cap_cannot_be_consumed_by_unstocked_or_replaced_pairs(self):
        self.perimeter.inspect.return_value = {'ok':True, 'posts':[
            dict(self.posts[0], full_magazines=4), dict(self.posts[1], full_magazines=10)]}
        self.c.prepare(self.obs)
        self.assertEqual(self.candidates.call_args.args[2], self.posts[1:])
        self.perimeter.inspect.return_value['posts'][0].update(full_magazines=10, identity_changed=True)
        self.c.prepare(self.obs)
        self.assertEqual(self.candidates.call_args.args[2], self.posts[1:])
        self.perimeter.inspect.return_value = {'ok':False}
        self.candidates.reset_mock()
        self.assertEqual(self.c.prepare(self.obs)['reason'], 'radar_current_defense_inspection_required')
        self.candidates.assert_not_called()

    def test_walk_service_cells_are_not_preflighted_as_belts_to_build(self):
        self.assertTrue(self.c.prepare(self.obs)['ok'])
        specs = self.preflight.call_args.args[3]
        self.assertEqual({p['name'] for p in specs}, {'radar','small-electric-pole'})
        self.assertGreater(len(self.c.prepare(self.obs)['core']['service_keepouts']),0)

    def test_prepare_is_readonly_and_natural_research_blocks_before_queries(self):
        before = {p.name:p.read_bytes() for p in self.root.glob('*.json')}
        with patch('factorio_agent.radar_network._atomic_json') as write:
            ready = self.c.prepare(self.obs);write.assert_not_called()
        self.assertTrue(ready['ok'], ready);self.assertFalse(ready['future_oil']['reserved'])
        self.assertEqual({p.name:p.read_bytes() for p in self.root.glob('*.json')}, before)
        self.assertEqual(self.c.state, {});self.builder.ensure_plan.assert_not_called()
        self.game.query.reset_mock()
        self.assertEqual(self.c.prepare({**self.obs, 'technologies':{}})['reason'], 'native_radar_research_and_recipes_required')
        self.game.query.assert_not_called()

    def test_public_empty_parameters_cannot_request_extra_radars(self):
        self.assertTrue(self.c.prepare(self.obs,{})['ok'])
        self.game.query.reset_mock()
        for method in (self.c.prepare,self.c.step,self.c.proof):
            for parameters in ({'count':2},[],False):
                with self.assertRaises(ValueError):method(self.obs,parameters)
        self.game.query.assert_not_called();self.builder.ensure_plan.assert_not_called()

    def test_future_load_counts_only_missing_electric_equipment_and_deduplicates(self):
        plant = entity('chemical-plant', 50.5, 50.5)
        board = {'blocks':{'one':{'entities':[plant, entity('wooden-chest', 1.5, 1.5)]}, 'two':{'entities':[plant]}}}
        self.assertEqual(planned_power_watts(self.catalog, self.obs, board), 210000)
        self.assertEqual(planned_power_watts(self.catalog, {'entities':[plant]}, board), 0)
        board['blocks']['one']['entities'].append(entity('unknown-machine', 70.5, 70.5))
        with self.assertRaises(ValueError):planned_power_watts(self.catalog, self.obs, board)

    def test_future_transport_ports_and_shared_reservations_cannot_be_erased(self):
        cases = [
            {'blocks':{'future':{'reserved_area':[[20,20],[30,30]], 'entities':[entity('transport-belt', .5, .5)]}}},
            {'blocks':{'port':{'reserved_area':[[20,20],[30,30]], 'entities':[], 'ports':[
                {'position':{'x':-5.5,'y':.5}, 'facing':4, 'direction':'output'}]}}},
            {'blocks':{}, 'shared_main_bus':{'reservations':[{'area':[[-1,-1],[2,2]]}]}}]
        for extra in cases:
            (self.root/'pcb-board.json').write_text(json.dumps({**self.board, **extra}))
            self.assertFalse(self.c.prepare(self.obs)['ok']);self.builder.ensure_plan.assert_not_called()
        self.assertFalse(self.c.path.exists())

    def test_headroom_failure_blocks_before_radar_cost_and_never_latches(self):
        original = self.game.query.side_effect
        self.game.query.side_effect = lambda script:({'ok':False, 'reason':'radar_measured_power_headroom_insufficient'}
            if 'local flow=net.flow_last_tick' in script else original(script))
        result = self.c.prepare(self.obs)
        self.assertFalse(result['ok']);self.assertIn('radar_measured_power_headroom_insufficient', result['evidence']['failures'])
        self.assertEqual(self.c.state, {});self.builder.ensure_plan.assert_not_called()

    def test_fresh_two_post_security_gate_precedes_first_builder_action(self):
        self.c.state = self.c.prepare(self.obs)
        with patch.object(self.c, '_site_guard', return_value={'ok':False, 'reason':'radar_requires_two_healthy_armed_covering_posts'}):
            self.assertEqual(self.c.step(self.obs)['reason'], 'radar_security_or_service_changed')
        self.builder.ensure_plan.assert_not_called()
        self.assertEqual(self.c.step(self.obs)['type'],'craft')
        self.perimeter.prepare_coverage.assert_not_called();self.perimeter.ensure_coverage.assert_not_called()

    def test_changed_power_source_or_fresh_resource_preflight_blocks_resume(self):
        self.c.state = self.c.prepare(self.obs)
        with patch.object(self.c, '_power_guard', return_value={'ok':False, 'reason':'radar_power_source_changed'}):
            self.assertEqual(self.c.step(self.obs)['reason'], 'radar_native_power_margin_changed')
        self.preflight.return_value = {'ok':False, 'reason':'new_nonmining_entity_on_resource'}
        self.assertEqual(self.c.step(self.obs)['reason'], 'radar_whole_preflight_changed')
        self.builder.ensure_plan.assert_not_called()

    def test_exact_bound_unit_replacement_never_adopts_or_rebuilds(self):
        entry = self.install();radar = entry['core']['radar']
        live = next(e for e in self.obs['entities'] if e['name'] == 'radar')
        live['unit_number'] += 1
        self.assertEqual(self.c.step(self.obs)['reason'], 'radar_owned_asset_missing_or_replaced')
        self.assertIsNone(self.c.proof(self.obs));self.builder.ensure_plan.assert_not_called()
        self.assertNotEqual(entry['bindings'][_key(radar)], live['unit_number'])

    def test_proof_requires_native_working_scan_and_local_current_defense(self):
        self.install();self.assertIsNotNone(self.c.proof(self.obs))
        self.perimeter.coverage_proof.return_value = None
        self.assertIsNotNone(self.c.proof(self.obs));self.perimeter.coverage_proof.assert_not_called()
        with patch.object(self.c,'_site_guard',return_value={'ok':False,'reason':'radar_requires_two_healthy_armed_covering_posts'}):
            self.assertIsNone(self.c.proof(self.obs))
            self.assertEqual(self.c.last_proof_failure['reason'],'radar_requires_two_healthy_armed_covering_posts')
        original = self.game.query.side_effect
        self.game.query.side_effect = lambda script:({'ok':False, 'reason':'radar_native_scan_progress_required'}
            if 'local radar=nil' in script else original(script))
        self.assertIsNone(self.c.proof(self.obs));self.assertEqual(self.c.last_proof_failure['reason'], 'radar_native_scan_progress_required')

    def test_second_oil_slot_requires_its_own_power_security_and_defense_before_reservation(self):
        self.candidates.side_effect = lambda *args, oil=False, **kwargs:[(40.5,.5)] if oil else [(.5,.5)]
        self.power.side_effect = lambda game,builder,obs,destination,reserved:{'ok':True,
            'entities':[entity('small-electric-pole',destination['x'],destination['y'])],
            'source_unit_number':1,'source_position':self.source['position']}
        result = self.c.prepare(self.obs)
        self.assertTrue(result['ok'], result);self.assertTrue(result['future_oil']['reserved'])
        self.assertTrue(result['future_oil']['plan']['reserved_only'])
        self.assertEqual(sum(e['name'] == 'radar' for e in result['plan']['entities']), 1)
        self.assertEqual(len(result['defense_areas']), 1)
        self.assertEqual(result['future_oil']['coverage_status'],'preview_only_not_live_proof')
        power_scripts = [call.args[0] for call in self.game.query.call_args_list if 'local flow=net.flow_last_tick' in call.args[0]]
        raw = power_scripts[-1].split('json_to_table(',1)[1].split(')\n',1)[0]
        self.assertEqual(json.loads(json.loads(raw))['additional_watts'], 600000)

    def test_unrelated_missing_refill_or_changed_post_does_not_block_local_radar(self):
        unrelated={'name':'far_front','position':{'x':200,'y':200},'unit_number':500}
        self.perimeter.state['posts'].append(unrelated)
        self.perimeter.inspect.return_value={'ok':True,'missing':['far_front'],'refill':['far_front'],'changed':['far_front'],
            'posts':[dict(p,full_magazines=10)for p in self.posts[:2]]+[dict(unrelated,missing=True,full_magazines=0,identity_changed=True)]}
        self.perimeter.prepare_coverage.return_value={'ok':False,'reason':'existing_defense_perimeter_not_healthy'}
        self.perimeter.ensure_coverage.return_value={'status':'blocked','reason':'existing_defense_post_missing_requires_explicit_repair'}
        self.perimeter.coverage_proof.return_value=None
        original=self.game.query.side_effect
        self.game.query.side_effect=lambda script:({'ok':True,'tick':20,'covering_posts':[dict(p,full_magazines=5)for p in self.posts[:2]]}
            if 'local armed={}'in script else original(script))
        ready=self.c.prepare(self.obs);self.assertTrue(ready['ok'],ready)
        self.assertEqual(self.c.step(self.obs)['type'],'craft')
        self.perimeter.prepare_coverage.assert_not_called();self.perimeter.ensure_coverage.assert_not_called()
        self.perimeter.coverage_proof.assert_not_called()

    def test_duplicate_unregistered_or_low_stock_local_receipts_never_build(self):
        self.c.state=self.c.prepare(self.obs)
        rows=[dict(p,full_magazines=5)for p in self.posts]
        for changed in([rows[0]], [rows[0],rows[0]], [rows[0],dict(rows[1],unit_number=999)],
                       [rows[0],dict(rows[1],full_magazines=4)]):
            with patch.object(self.c,'_site_guard',return_value={'ok':True,'tick':30,'covering_posts':changed}):
                self.assertEqual(self.c.step(self.obs)['reason'],'radar_security_or_service_changed')
            self.builder.ensure_plan.assert_not_called()

    def test_reserved_future_oil_power_is_a_preview_and_not_a_base_build_prerequisite(self):
        self.candidates.side_effect=lambda *args,oil=False,**kwargs:[(40.5,.5)]if oil else[(.5,.5)]
        self.power.side_effect=lambda game,builder,obs,destination,reserved:{'ok':True,
            'entities':[entity('small-electric-pole',destination['x'],destination['y'])],
            'source_unit_number':1,'source_position':self.source['position']}
        self.c.state=self.c.prepare(self.obs);self.assertTrue(self.c.state['future_oil']['reserved'])
        original=self.game.query.side_effect
        def query(script):
            if 'local flow=net.flow_last_tick'in script:
                raw=script.split('json_to_table(',1)[1].split(')\n',1)[0]
                if json.loads(json.loads(raw))['additional_watts']>300000:return {'ok':False,'reason':'future_power_no_longer_available'}
            return original(script)
        self.game.query.side_effect=query
        self.assertEqual(self.c.step(self.obs)['type'],'craft')

    def test_unproven_second_oil_power_is_deferred_without_spending_or_blocking_base(self):
        self.candidates.side_effect = lambda *args, oil=False, **kwargs:[(40.5,.5)] if oil else [(.5,.5)]
        self.power.side_effect = lambda game,builder,obs,destination,reserved:{'ok':True,
            'entities':[entity('small-electric-pole',destination['x'],destination['y'])],
            'source_unit_number':1,'source_position':self.source['position']}
        original = self.game.query.side_effect
        def query(script):
            if 'local flow=net.flow_last_tick' in script:
                raw=script.split('json_to_table(',1)[1].split(')\n',1)[0]
                if json.loads(json.loads(raw))['additional_watts']==600000:
                    return {'ok':False,'reason':'radar_measured_power_headroom_insufficient'}
            return original(script)
        self.game.query.side_effect=query
        result=self.c.prepare(self.obs)
        self.assertTrue(result['ok'],result);self.assertFalse(result['future_oil']['reserved'])
        self.assertEqual(len(result['defense_areas']),1);self.builder.ensure_plan.assert_not_called()

    def test_existing_unmanaged_radar_is_never_adopted_or_duplicated(self):
        self.obs['entities'].append(dict(entity('radar',40.5,40.5),unit_number=999))
        self.assertEqual(self.c.prepare(self.obs)['reason'],'existing_radar_requires_explicit_ownership_reconciliation')
        self.game.query.assert_not_called();self.builder.ensure_plan.assert_not_called()

    def test_future_oil_reservation_cannot_cover_the_unbuilt_base_power_route(self):
        self.candidates.side_effect=lambda *args,oil=False,**kwargs:[(-7.5,.5)]if oil else[(.5,.5)]
        self.power.return_value['entities'].insert(0,entity('small-electric-pole',-5.5,.5))
        result=self.c.prepare(self.obs)
        self.assertTrue(result['ok'],result);self.assertFalse(result['future_oil']['reserved'])
        self.assertIn('radar_reserved_area_conflict',result['future_oil']['evidence']['failures'])

    def test_changed_source_registry_blocks_reservation_before_metadata_or_radar_cost(self):
        self.c.state=self.c.prepare(self.obs)
        before=(self.root/'pcb-board.json').read_bytes()
        (self.root/'fluid-sources.json').write_text(json.dumps({'world_id':'other','catalog_fingerprint':'c','sources':{}}))
        self.assertEqual(self.c.step(self.obs)['reason'],'radar_reservation_context_changed')
        self.assertEqual((self.root/'pcb-board.json').read_bytes(),before);self.builder.ensure_plan.assert_not_called()

    def test_working_proof_does_not_ignore_missing_registered_ownership(self):
        self.install();board=json.loads((self.root/'pcb-board.json').read_text())
        board['blocks'].pop('radar_base');(self.root/'pcb-board.json').write_text(json.dumps(board))
        self.assertIsNone(self.c.proof(self.obs));self.assertEqual(self.c.last_proof_failure['reason'],'radar_registered_plan_changed')


class RadarCandidateTests(unittest.TestCase):
    def test_native_failures_at_one_pair_leave_candidates_at_another_pair(self):
        posts = [{'unit_number':i, 'position':{'x':x,'y':.5}}
                 for i,x in ((1,0),(2,12),(3,200),(4,212))]
        obs = {'entities':[dict(name='gun-turret',health=400,max_health=400,**p) for p in posts]
                         +[entity('lab',5.5,-5.5)]}
        sites = radar_candidates(obs,{},posts,effective_range=16)
        self.assertTrue(any(x < 20 for x,y in sites))
        self.assertTrue(any(x > 190 for x,y in sites))
        self.assertLessEqual(len(sites),16)

    def test_reserved_midpoints_do_not_hide_valid_exterior_service_sites_before_cap(self):
        posts=[{'unit_number':i,'position':{'x':x,'y':.5}}for i,x in((1,-7.5),(2,7.5))]
        obs={'entities':[dict(name='gun-turret',health=400,max_health=400,**p)for p in posts]+[entity('lab',.5,.5)]}
        reserved=[[-4,-3],[4,4]]
        board={'blocks':{'producer':{'reserved_area':reserved}}}
        sites=radar_candidates(obs,board,posts,effective_range=16)
        self.assertTrue(sites);self.assertLessEqual(len(sites),16)
        for x,y in sites:
            area=[[x-3.5,y-3.5],[x+3.5,y+3.5]]
            self.assertFalse(area[0][0]<reserved[1][0]and area[1][0]>reserved[0][0]
                and area[0][1]<reserved[1][1]and area[1][1]>reserved[0][1])
            for p in posts:
                self.assertTrue(all((cx-p['position']['x'])**2+(cy-p['position']['y'])**2<=16**2
                    for cx in(area[0][0],area[1][0])for cy in(area[0][1],area[1][1])))
        hard={sites[0]}
        filtered=radar_candidates(obs,board,posts,effective_range=16,keepout_cells=hard)
        self.assertTrue(filtered)
        self.assertTrue(all(not(abs(x-hx)<=3 and abs(y-hy)<=3)for x,y in filtered for hx,hy in hard))

    def test_candidates_are_finite_native_post_pairs_not_hotspot_coordinates(self):
        posts = [{'unit_number':i, 'position':{'x':x, 'y':y}} for i,x,y in ((1,0,0),(2,12,0),(3,24,0),(4,36,0),(5,48,0))]
        obs = {'entities':[dict(name='gun-turret',health=400,max_health=400,**p) for p in posts]+[entity('lab', 5.5, -10.5), entity('oil-refinery', 40.5, -.5)]}
        base = radar_candidates(obs, {}, posts);oil = radar_candidates(obs, {}, posts, oil=True)
        self.assertLessEqual(len(base),16);self.assertLessEqual(len(oil),8)
        self.assertTrue(base);self.assertTrue(oil);self.assertNotEqual(base[0],oil[0])
        self.assertTrue(all(x % 1 == .5 and y % 1 == .5 for x,y in base+oil))
        self.assertEqual(radar_candidates(obs, {}, posts[:1]), [])

    def test_damaged_nearest_canonical_pair_does_not_crowd_out_healthy_pairs(self):
        posts=[{'unit_number':i,'position':{'x':x,'y':.5}}for i,x in((1,0),(2,12),(3,40),(4,52))]
        obs={'entities':[dict(name='gun-turret',health=399 if p['unit_number']<=2 else 400,
                             max_health=400,**p)for p in posts]+[entity('lab',5.5,-5.5)]}
        sites=radar_candidates(obs,{},posts,effective_range=16)
        self.assertTrue(sites);self.assertTrue(all(x>30 for x,y in sites))


@unittest.skipUnless(LuaRuntime, 'optional executable Lua runtime')
class NativeRadarTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        def table(value):
            if isinstance(value, dict):return self.lua.table_from({k:table(v) for k,v in value.items()})
            if isinstance(value, list):return self.lua.table_from([table(v) for v in value])
            return value
        self.table = table
        self.lua.globals().helpers = self.lua.table_from({'json_to_table':lambda raw:table(json.loads(raw))})
        self.lua.execute('''f={index=1,technologies={radar={researched=true}},recipes={radar={enabled=true}}};s={};d={world_id="w"};game={tick=30}
defines={inventory={turret_ammo=1},flow_precision_index={five_seconds=1},entity_status={working=1}};enemy=false;resource=false;walk_blocked=false
flow={primary_output=0,secondary_output=60000,solar_output=0,maximum_consumption=6000,consumption_satisfaction=1}
stats={input_counts={a=1},output_counts={b=1},get_flow_count=function(q)return q.category=="input"and 8000 or 7000 end}
net={valid=true,flow_last_tick=flow,statistics=stats};sub={parent_network=net}
proto={tile_width=3,tile_height=3,get_max_energy_usage=function(q)return 5000 end}
prototypes={entity={radar=proto,["gun-turret"]={attack_parameters={range=18}}}}
source={valid=true,name="small-electric-pole",unit_number=1,position={x=-3.5,y=-.5},direction=0,force=f,surface=s,electric_network_id=2,electric_network=sub}
radar={valid=true,name="radar",unit_number=100,position={x=.5,y=.5},direction=0,force=f,surface=s,electric_network_id=2,energy=1000,status=1,radar_scan_progress=.25,prototype=proto,is_connected_to_electric_network=function()return true end}
pole={valid=true,name="small-electric-pole",unit_number=101,position={x=2.5,y=1.5},direction=0,force=f,surface=s,electric_network_id=2}
stack={valid_for_read=true,name="firearm-magazine",quality={name="normal"},count=10,ammo=10,prototype={magazine_size=10}}
posts={};for i,x in ipairs{-8.5,8.5}do posts[i]={valid=true,name="gun-turret",unit_number=19+i,position={x=x,y=.5},direction=0,force=f,surface=s,health=400,max_health=400,get_inventory=function()return{stack}end}end
function pos(p)return{x=p.x,y=p.y}end
function target(p,name)for _,e in ipairs{source,radar,pole,posts[1],posts[2]}do if e.name==name and e.position.x==p.x and e.position.y==p.y then return e end end end
s.count_entities_filtered=function(q)if q.type=="resource"then return resource and 1 or 0 end;return enemy and 1 or 0 end
s.find_entities_filtered=function(q)return{}end
s.can_place_entity=function(q)return not walk_blocked end
f.is_chunk_charted=function(surface,chunk)return true end''')
        self.source = dict(entity('small-electric-pole', -3.5,-.5), unit_number=1, electric_network_id=2, world_id='w', force_index=1)
        self.core = radar_core(catalog(), .5, .5)

    def execute(self, script, args):
        return dict(self.lua.execute('return(function() '+' '.join(script.replace('PAYLOAD', _payload(args)).splitlines())+' end)()').items())

    def power(self):return self.execute(RADAR_POWER_LUA, {'world_id':'w', 'source':self.source, 'preserved_poles':[self.source],
        'scheduled_watts':500000, 'additional_watts':300000})

    def site(self, **extra):return self.execute(RADAR_SITE_LUA, {'world_id':'w', 'core':self.core,
        'posts':[{'position':{'x':x,'y':.5}, 'unit_number':u} for x,u in ((-8.5,20),(8.5,21))],
        'zones':[], 'foreign_areas':[], 'enemy_radius':32, 'minimum_full_magazines':5, **extra})

    def proof(self):return self.execute(RADAR_PROOF_LUA, {'world_id':'w', 'source':self.source, 'radar_watts':300000,
        'specs':[dict(e, unit_number=i+100) for i,e in enumerate(self.core['entities'])]})

    def test_distant_roving_enemy_does_not_block_rear_radar_but_close_enemy_does(self):
        self.lua.execute('''enemy_distance=60
s.count_entities_filtered=function(q)if q.force=="enemy"then return enemy_distance<=q.radius and 1 or 0 end;return 0 end''')
        self.assertTrue(self.site()['ok'])
        self.lua.execute('enemy_distance=24')
        self.assertEqual(self.site()['reason'],'radar_nearby_enemies_require_security_first')

    def test_actual_worm_range_protects_the_whole_service_area(self):
        self.lua.execute('''worm={type="turret",attack_parameters={range=60}};prototypes.entity.worm=worm
foe={position={x=45,y=.5},prototype=worm};s.find_entities_filtered=function(q)return{foe}end''')
        self.assertEqual(self.site()['reason'],'radar_service_inside_hostile_turret_range')
        self.lua.execute('worm.attack_parameters.range=25')
        self.assertTrue(self.site()['ok'])

    def test_native_offered_headroom_measured_flow_reserve_and_exact_network(self):
        result = self.power();self.assertTrue(result['ok']);self.assertEqual(result['measured_5s_watts'],480000)
        self.assertEqual(result['reserve_watts'],720000);self.assertEqual(result['headroom_watts'],2320000)
        for change, restore in (('flow.secondary_output=15000','flow.secondary_output=60000'),
                ('flow.consumption_satisfaction=.9','flow.consumption_satisfaction=1'),
                ('source.unit_number=99','source.unit_number=1'),('source.electric_network_id=36','source.electric_network_id=2'),
                ('d.world_id="other"','d.world_id="w"')):
            self.lua.execute(change);self.assertFalse(self.power()['ok'],change);self.lua.execute(restore)

    def test_native_two_post_security_resource_and_walk_service_fail_closed(self):
        self.assertTrue(self.site()['ok'])
        for change, restore in (('enemy=true','enemy=false'),('resource=true','resource=false'),
                ('posts[1].health=399','posts[1].health=400'),('stack.count=4','stack.count=10'),
                ('stack.quality.name="uncommon"','stack.quality.name="normal"'),
                ('walk_blocked=true','walk_blocked=false'),('f.technologies.radar.researched=false','f.technologies.radar.researched=true')):
            self.lua.execute(change);self.assertFalse(self.site()['ok'],change);self.lua.execute(restore)
        self.assertFalse(self.site(zones=[{'area':[[-1,-1],[2,2]]}])['ok'])

    def test_native_working_scan_proof_requires_units_force_direction_energy_and_real_progress(self):
        self.assertTrue(self.proof()['ok'])
        for change, restore in (('radar.unit_number=999','radar.unit_number=100'),('radar.direction=4','radar.direction=0'),
                ('radar.energy=0','radar.energy=1000'),('radar.status=2','radar.status=1'),
                ('radar.radar_scan_progress=0','radar.radar_scan_progress=.25'),
                ('radar.force={index=2}','radar.force=f'),('pole.electric_network_id=36','pole.electric_network_id=2')):
            self.lua.execute(change);self.assertFalse(self.proof()['ok'],change);self.lua.execute(restore)

    def test_scripts_are_flattenable_readonly_and_make_no_chart_or_combat_mutations(self):
        for script in (RADAR_POWER_LUA, RADAR_SITE_LUA, RADAR_PROOF_LUA):
            self.assertNotIn('--',script)
            for forbidden in ('create_entity', 'destroy(', 'die(', '.health=', '.researched=', '.chart(', 'insert{', 'set_fluid'):
                self.assertNotIn(forbidden, script)


if __name__ == '__main__':unittest.main()
