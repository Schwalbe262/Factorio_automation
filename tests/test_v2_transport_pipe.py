from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_agent.board import entity
from factorio_agent.routing import route_trunks
from factorio_agent.transport import compile_crossings,plan_connection,surface_pipe_proof,PIPE_WITNESS_LUA,_production_keepouts

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


def witness(x=5.5,y=.5):
    return dict(name='pipe',position={'x':x,'y':y},unit_number=10,world_id='w',force_index=1,
        peers=[dict(name='pipe',position={'x':x,'y':y-1},unit_number=11,fluidbox_index=1)])


class PipeCrossingTests(unittest.TestCase):
    def setUp(self):
        self.route=route_trunks((.5,.5),(10.5,.5),start_direction=4,end_direction=4)
        self.pipe=witness()

    def test_verified_existing_pipe_is_preserved_above_straight_native_belt_tunnel(self):
        plan=compile_crossings(self.route,[self.pipe],verified_pipes=[self.pipe])
        self.assertTrue(plan['ok'],plan);self.assertNotIn(self.pipe['position'],[e['position']for e in plan['entities']])
        self.assertEqual(plan['required_items'],{'transport-belt':8,'underground-belt':2})
        self.assertEqual(plan['underground_pairs'][0],{'input':[4.5,.5],'output':[6.5,.5],'direction':4,'span':2,'preserved_pipes':[self.pipe]})
        self.assertTrue(all(e['name']!='pipe'for e in plan['entities']))
        self.assertEqual(plan,json.loads(json.dumps(plan)))

    def test_unverified_future_wrong_unit_and_nonordinary_pipe_cannot_be_softened(self):
        for raw in(entity('pipe',5.5,.5),dict(self.pipe,unit_number=99)):
            self.assertEqual(compile_crossings(self.route,[raw])['reason'],'surface_pipe_crossing_requires_verified_identity')
        wrong=dict(self.pipe,unit_number=99)
        self.assertFalse(compile_crossings(self.route,[wrong],verified_pipes=[self.pipe])['ok'])
        for malformed in(dict(self.pipe,name='pipe-to-ground'),dict(self.pipe,unit_number=True),
                          dict(self.pipe,position={'x':float('nan'),'y':.5}),dict(self.pipe,peers={}),dict(self.pipe,world_id='')):
            self.assertFalse(compile_crossings(self.route,[],verified_pipes=[malformed])['ok'])

    def test_mouth_endpoint_turn_span_and_existing_underground_rules_still_apply(self):
        for x in(.5,1.5,9.5,10.5):
            q=witness(x)
            self.assertFalse(compile_crossings(self.route,[q],verified_pipes=[q])['ok'])
        self.assertFalse(compile_crossings(self.route,[self.pipe],verified_pipes=[self.pipe],maximum_span=1)['ok'])
        mouth=dict(entity('underground-belt',2.5,.5,4),belt_to_ground_type='input')
        self.assertFalse(compile_crossings(self.route,[self.pipe,mouth],verified_pipes=[self.pipe])['ok'])
        corner={'ok':True,'segments':[dict(e)for e in self.route['segments']],'metrics':self.route['metrics']}
        corner['segments'][5]['direction']=0
        self.assertFalse(compile_crossings(corner,[self.pipe],verified_pipes=[self.pipe])['ok'])

    def test_mixed_close_surface_layers_share_one_bounded_tunnel_without_belt_merges(self):
        belt=entity('transport-belt',3.5,.5,0)
        plan=compile_crossings(self.route,[belt,self.pipe],verified_pipes=[self.pipe])
        self.assertTrue(plan['ok'],plan);self.assertEqual(plan['underground_pairs'][0]['span'],4)
        self.assertEqual(plan['underground_pairs'][0]['preserved_pipes'],[self.pipe])
        self.assertFalse(compile_crossings(self.route,[dict(belt,direction=4),self.pipe],verified_pipes=[self.pipe])['ok'])

    def test_planner_consumes_only_native_witness_and_keeps_prospective_footprints_hard(self):
        game=SimpleNamespace(query=Mock(return_value={'ok':True,'blocked':[],'max_span':5,'crossable_pipes':[self.pipe]}))
        builder=SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,can_place=Mock(return_value={'ok':True}))
        obs={'world_id':'w','entities':[self.pipe]};a={'item':'iron-plate','position':{'x':.5,'y':.5},'facing':4}
        b={'item':'iron-plate','position':{'x':10.5,'y':.5},'facing':4}
        result=plan_connection(game,builder,obs,a,b)
        self.assertTrue(result['ok'],result);self.assertEqual(result['underground_pairs'][0]['preserved_pipes'],[self.pipe])
        future=entity('chemical-plant',5.5,.5)
        result=plan_connection(game,builder,obs,a,b,[future])
        self.assertTrue(result['ok'],result)
        self.assertFalse(any(pair.get('preserved_pipes')for pair in result['underground_pairs']))
        self.assertTrue(all(abs(e['position']['x']-5.5)>1 or abs(e['position']['y']-.5)>1 for e in result['entities']))

    def test_no_pipe_witness_needs_no_native_proof_and_wrong_identity_fails_before_io(self):
        game=SimpleNamespace(query=Mock())
        self.assertEqual(surface_pipe_proof(game,{},{}),{'ok':True,'preserved_pipes':0})
        plan={'underground_pairs':[{'preserved_pipes':[self.pipe]}]}
        self.assertFalse(surface_pipe_proof(game,{'world_id':'other','entities':[]},plan)['ok'])
        self.assertFalse(surface_pipe_proof(game,{'world_id':'w','entities':[dict(self.pipe,unit_number=99)]},plan)['ok'])
        game.query.assert_not_called()


class ProductionReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root),query=Mock(return_value={'ok':True,'blocked':[],'max_span':5}))
        self.builder=SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,can_place=Mock(return_value={'ok':True}))
        self.source=dict(kind='item',item='steel-plate',direction='output',facing=4,position={'x':1.5,'y':.5},unit_number=1,world_id='w')
        self.destination=dict(kind='item',item='steel-plate',direction='input',facing=4,position={'x':10.5,'y':.5})
        self.obs={'world_id':'w','entities':[dict(entity('transport-belt',1.5,.5,4),unit_number=1)]}
        self.bounds={'min_x':-10.5,'max_x':25.5,'min_y':-10.5,'max_y':10.5}
        self.owner={'reserved_area':[[0,-1],[3,2]],'ports':[self.source], 'entities':[entity('transport-belt',1.5,.5,4)]}
        self.board={'world_id':'w','blocks':{'producer':self.owner}}
        self.preflight=patch('factorio_agent.planning.preflight',return_value={'ok':True}).start();self.addCleanup(patch.stopall)

    def keepouts(self,reserved=(),**kwargs):
        (self.root/'pcb-board.json').write_text(json.dumps(self.board))
        return _production_keepouts(self.game,self.builder,self.obs,self.source,self.destination,reserved,self.bounds,**kwargs)

    def test_registered_source_opens_only_empty_straight_apron_and_keeps_future_area_hard(self):
        p=self.keepouts();self.assertTrue(p['ok'],p)
        self.assertEqual(p['source_prefix'],[{'x':x,'y':.5}for x in(1.5,2.5,3.5)])
        self.assertNotIn((2.5,.5),p['hard']);self.assertIn((2.5,1.5),p['hard'])
        self.assertEqual(p['aprons'][0]['units'],{'1':1})
        self.preflight.assert_not_called()

    def test_other_registered_fluid_port_first_outside_tile_keeps_future_access(self):
        self.owner['ports'].append(dict(kind='fluid',item='petroleum-gas',direction='output',facing=4,
            position={'x':2.5,'y':-.5},name='pipe',fluidbox_index=1))
        p=self.keepouts();self.assertTrue(p['ok']);self.assertIn((3.5,-.5),p['hard'])
        self.assertNotIn((3.5,.5),p['hard'])

    def test_wrong_port_unit_world_foreign_owner_and_nonempty_apron_are_not_exempted(self):
        self.source['item']='iron-plate'
        self.owner['ports']=[dict(self.source,item='steel-plate')]
        self.assertEqual(self.keepouts()['reason'],'transport_port_inside_unrelated_production_reservation')
        self.source['item']='steel-plate';self.source['unit_number']=99
        self.assertEqual(self.keepouts()['reason'],'production_source_apron_requires_exact_owned_port')
        self.source['unit_number']=1;self.source['world_id']='other'
        self.assertEqual(self.keepouts()['reason'],'production_source_apron_requires_exact_owned_port')
        self.source['world_id']='w';self.obs['entities'].append(dict(entity('transport-belt',2.5,.5,4),unit_number=2))
        self.assertEqual(self.keepouts()['reason'],'production_source_apron_not_empty')
        self.obs['entities'].pop();self.board['blocks']['foreign']={'reserved_area':[[2,0],[3,1]],'entities':[]}
        self.assertEqual(self.keepouts()['reason'],'production_port_apron_hits_other_reservation')

    def test_source_apron_reserved_obstacles_and_four_step_bound_fail_closed(self):
        self.owner['entities'].append(entity('small-electric-pole',2.5,.5))
        self.assertEqual(self.keepouts()['reason'],'production_port_apron_hits_producer_asset')
        self.owner['entities'].pop();self.owner['reserved_area'][1][0]=8
        self.assertEqual(self.keepouts()['reason'],'production_port_apron_exceeds_four_outward_cells')

    def test_full_future_production_and_bus_reservations_block_buried_path_too(self):
        self.board['blocks']={'future_refinery':{'reserved_area':[[4,-2],[7,2]],'entities':[],'ports':[]}}
        self.board['shared_main_bus']={'reservations':[{'area':[[8,-1],[9,2]],'item':'copper-plate'}]}
        p=self.keepouts();self.assertIn((5.5,.5),p['hard']);self.assertIn((8.5,.5),p['hard'])
        pipe=witness();self.game.query.return_value={'ok':True,'blocked':[],'max_span':5,'crossable_pipes':[pipe]}
        result=plan_connection(self.game,self.builder,self.obs,self.source,self.destination)
        self.assertTrue(result['ok'],result)
        self.assertTrue(all(not(4<e['position']['x']<7 and-2<e['position']['y']<2)for e in result['entities']))
        self.assertFalse(any(p.get('preserved_pipes')for p in result['underground_pairs']))

    def test_exact_declared_destination_approach_is_bounded_not_whole_block(self):
        self.board['blocks']={'consumer':{'reserved_area':[[9,-1],[13,3]],'ports':[self.destination],'entities':[]}}
        self.assertEqual(self.keepouts()['reason'],'production_destination_apron_requires_owned_or_declared_input')
        p=self.keepouts([entity('transport-belt',10.5,.5,4)])
        self.assertTrue(p['ok'],p);self.assertEqual(p['destination_suffix'],[{'x':x,'y':.5}for x in(8.5,9.5,10.5)])
        self.assertIn((11.5,1.5),p['hard']);self.assertNotIn((9.5,.5),p['hard'])

    def test_solid_mining_outer_area_uses_existing_separate_mining_policy(self):
        self.owner.update(resource='coal',entities=[entity('electric-mining-drill',.5,.5)],reserved_area=[[0,-1],[8,2]])
        p=self.keepouts();self.assertTrue(p['ok']);self.assertEqual(p['hard'],set());self.assertEqual(p['source_prefix'],[])

    def test_unbuilt_external_feed_and_power_footprints_are_hard_without_fake_native_assets(self):
        self.owner['entities'] += [entity('transport-belt',6.5,-3.5,4),entity('small-electric-pole',7.5,-3.5)]
        before=deepcopy(self.obs);p=self.keepouts()
        self.assertTrue(p['ok'],p);self.assertIn((6.5,-3.5),p['hard']);self.assertIn((7.5,-3.5),p['hard'])
        self.assertEqual(self.obs,before)
        self.obs['entities'].append(dict(entity('transport-belt',6.5,-3.5,4),unit_number=10))
        self.assertNotIn((6.5,-3.5),self.keepouts()['hard'])

    def test_unbuilt_underground_reserves_the_entire_external_tunnel(self):
        self.owner['entities'] += [dict(entity('underground-belt',6.5,-3.5,4),belt_to_ground_type='input'),
                                   dict(entity('underground-belt',10.5,-3.5,4),belt_to_ground_type='output')]
        self.owner['underground_pairs']=[{'input':[6.5,-3.5],'output':[10.5,-3.5],'direction':4,'span':4}]
        p=self.keepouts();self.assertTrue(p['ok'],p)
        self.assertTrue({(x,-3.5)for x in(6.5,7.5,8.5,9.5,10.5)}<=p['hard'])

    def test_separately_declared_future_fluid_tunnel_also_reserves_buried_cells(self):
        self.owner['entities'] += [entity('pipe-to-ground',6.5,-3.5,12),entity('pipe-to-ground',10.5,-3.5,4)]
        self.owner['fluid_underground_pairs']=[{'first':[6.5,-3.5],'last':[10.5,-3.5],'direction':4,'span':4}]
        p=self.keepouts();self.assertTrue(p['ok'],p);self.assertIn((8.5,-3.5),p['hard'])

    def test_exact_receiver_revision_releases_only_its_unbuilt_external_footprints(self):
        receiver={'reserved_area':[[9,-1],[13,3]],'ports':[self.destination],
                  'entities':[entity('transport-belt',5.5,-3.5,4)]}
        self.board['blocks']['receiver']=receiver
        self.owner['entities'].append(entity('transport-belt',7.5,-3.5,4))
        release={'block':'receiver','plan_revision':hashlib.sha256(json.dumps(receiver,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
        p=self.keepouts([entity('transport-belt',10.5,.5,4)],released_unbuilt_receiver=release)
        self.assertTrue(p['ok'],p);self.assertNotIn((5.5,-3.5),p['hard'])
        self.assertIn((7.5,-3.5),p['hard']);self.assertIn((11.5,1.5),p['hard'])
        release['plan_revision']='stale'
        self.assertEqual(self.keepouts([entity('transport-belt',10.5,.5,4)],released_unbuilt_receiver=release)['reason'],
                         'transport_unbuilt_receiver_release_identity_changed')

    def test_release_requires_the_exact_registered_destination_block_and_board(self):
        release={'block':'producer','plan_revision':hashlib.sha256(json.dumps(self.owner,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
        self.assertEqual(self.keepouts(released_unbuilt_receiver=release)['reason'],'transport_unbuilt_receiver_release_identity_changed')
        game=SimpleNamespace(query=Mock())
        result=plan_connection(game,self.builder,self.obs,self.source,self.destination,released_unbuilt_receiver=release)
        self.assertEqual(result['reason'],'transport_unbuilt_receiver_release_identity_changed');game.query.assert_not_called()

    def test_future_surface_transport_keeps_neighbor_cells_clear_of_incidental_feeds(self):
        self.owner['entities'] += [entity('transport-belt',6.5,-3.5,4),entity('pipe',9.5,-3.5)]
        p=self.keepouts();self.assertTrue(p['ok'],p)
        for point in((6.5,-2.5),(5.5,-3.5),(9.5,-2.5),(10.5,-3.5)):
            self.assertIn(point,p['hard'])

    def test_new_destination_cannot_erase_a_foreign_future_transport_keepout(self):
        self.owner['entities'].append(entity('pipe',10.5,1.5))
        self.assertEqual(self.keepouts()['reason'],'transport_endpoint_conflicts_with_unbuilt_external_plan')


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class NativePipeCrossingTests(unittest.TestCase):
    def setUp(self):
        self.lua=LuaRuntime(unpack_returned_tuples=True)
        def table(v):
            if isinstance(v,dict):return self.lua.table_from({k:table(a)for k,a in v.items()})
            if isinstance(v,list):return self.lua.table_from([table(a)for a in v])
            return v
        def decode(v):
            if not hasattr(v,'items'):return v
            rows=dict(v.items())
            if rows and set(rows)==set(range(1,len(rows)+1)):return [decode(rows[i])for i in range(1,len(rows)+1)]
            return {k:decode(a)for k,a in rows.items()}
        self.lua.globals().helpers=self.lua.table_from({'json_to_table':lambda raw:table(json.loads(raw))})
        self.lua.execute('''d={world_id="w"};f={index=1,name="player"};enemy={index=2,name="enemy"};s={};a={};one_way=false;resource=false;water=false;extra=false;no_peers=false;blocking=false
defines={build_check_type={manual_ghost=1}}
prototypes={entity={["transport-belt"]={collision_mask={layers={ground=true}}},["underground-belt"]={max_underground_distance=5}}}
function pos(p)return{x=p.x,y=p.y}end
pipe={valid=true,name="pipe",type="pipe",position={x=5.5,y=.5},unit_number=10,force=f,surface=s,prototype={collision_mask={colliding_with_tiles_only=false}},fluids={['crude-oil']=100}}
peer={valid=true,name="pipe",type="pipe",position={x=5.5,y=-.5},unit_number=11,force=f,surface=s,prototype=pipe.prototype}
third={valid=true,name="pipe",type="pipe",position={x=6.5,y=.5},unit_number=12,force=f,surface=s,prototype=pipe.prototype}
pipe.get_fluid_box_neighbours=function()local rows=no_peers and{}or{{entity=peer,index=1}};if extra then rows[#rows+1]={entity=third,index=1}end;return rows end
peer.get_fluid_box_neighbours=function()return one_way and{}or{{entity=pipe,index=1}}end
third.get_fluid_box_neighbours=function()return{{entity=pipe,index=1}}end
function target(p,name)if name=="pipe"and p.x==5.5 and p.y==.5 then return pipe end end
s.get_tile=function(p)return{collides_with=function()return water or(apron_water and p.x==1.5 and p.y==.5)end}end
s.can_place_entity=function(q)return not(q.position.x==5.5 and q.position.y==.5)end
s.find_entities_filtered=function(q)
 local rows={};local area=q.area
 if not area then return rows end
 local apron_hit=area[1][1]<1.5 and area[2][1]>1.5 and area[1][2]<.5 and area[2][2]>.5
 if q.type=="resource"then
  if apron_resource and apron_hit then return{{name="coal",type="resource",position={x=1.5,y=.5},amount=100}}end
  if resource and area[1][1]<5.5 and area[2][1]>5.5 and area[1][2]<.5 and area[2][2]>.5 then return{{name="coal",type="resource",position={x=5.5,y=.5},amount=100}}end;return rows end
 for _,e in ipairs({pipe,peer})do if area[1][1]<e.position.x and area[2][1]>e.position.x and area[1][2]<e.position.y and area[2][2]>e.position.y then rows[#rows+1]=e end end
 if resource and area[1][1]<5.5 and area[2][1]>5.5 and area[1][2]<.5 and area[2][2]>.5 then rows[#rows+1]={name="coal",type="resource"}end
 if apron_resource and apron_hit then rows[#rows+1]={name="coal",type="resource"}end
 if apron_blocking and apron_hit then rows[#rows+1]={name="iron-chest",type="container",force=f,prototype=pipe.prototype}end
 if blocking and area[1][1]<5.5 and area[2][1]>5.5 and area[1][2]<.5 and area[2][2]>.5 then rows[#rows+1]={name="iron-chest",type="container",force=f,prototype=pipe.prototype}end
 return rows end
''')
        self.surveys=[]
        def query(body):
            result=decode(self.lua.execute('return(function() '+' '.join(body.splitlines())+' end)()'))
            self.surveys.append(result);return result
        self.game=SimpleNamespace(query=Mock(side_effect=query))
        self.pipe=witness();self.obs={'world_id':'w','entities':[self.pipe]}
        self.plan={'underground_pairs':[{'preserved_pipes':[self.pipe]}]}

    def survey(self):
        builder=SimpleNamespace(_occupied_by_plan=lambda _:set())
        a={'item':'iron-plate','position':{'x':.5,'y':.5},'facing':4};b={'item':'iron-plate','position':{'x':10.5,'y':.5},'facing':4}
        with patch('factorio_agent.transport.route_trunks',return_value={'ok':False,'reason':'fixture_stop'}):
            plan_connection(self.game,builder,self.obs,a,b)
        return self.surveys[-1]

    def test_real_flattened_survey_binds_only_owned_exact_pipe_and_native_reciprocal_peers(self):
        result=self.survey();self.assertEqual(result['crossable_pipes'],[self.pipe])
        for change,restore in(('pipe.unit_number=99','pipe.unit_number=10'),('pipe.force=enemy','pipe.force=f'),
            ('one_way=true','one_way=false'),('resource=true','resource=false'),('water=true','water=false'),('blocking=true','blocking=false')):
            self.lua.execute(change);result=self.survey()
            self.assertIn({'x':5.5,'y':.5},result['blocked'],change)
            self.assertFalse(result['crossable_pipes'],change);self.lua.execute(restore)

    def test_native_proof_preserves_fluid_and_exact_peer_graph_before_and_after(self):
        for _ in range(2):self.assertTrue(surface_pipe_proof(self.game,self.obs,self.plan)['ok'])
        self.assertEqual(self.lua.globals().pipe.fluids['crude-oil'],100)
        for change,restore in(('pipe.unit_number=99','pipe.unit_number=10'),('peer.unit_number=99','peer.unit_number=11'),
            ('peer.force=enemy','peer.force=f'),('one_way=true','one_way=false'),('extra=true','extra=false'),('no_peers=true','no_peers=false')):
            self.lua.execute(change);self.assertFalse(surface_pipe_proof(self.game,self.obs,self.plan)['ok'],change);self.lua.execute(restore)
        self.lua.execute('d.world_id="other"')
        self.assertFalse(surface_pipe_proof(self.game,self.obs,self.plan)['ok'])

    def test_native_helper_has_no_line_comments_or_mutations(self):
        self.assertNotIn('--',PIPE_WITNESS_LUA)
        for forbidden in('create_entity','destroy(','insert_fluid','set_fluid','rotate('):self.assertNotIn(forbidden,PIPE_WITNESS_LUA)

    def test_native_producer_apron_checks_fresh_unit_purity_and_unexpected_belts(self):
        with tempfile.TemporaryDirectory()as root:
            self.game.cfg=SimpleNamespace(runtime_dir=root)
            source=dict(kind='item',item='iron-plate',direction='output',facing=4,position={'x':.5,'y':.5},unit_number=20,world_id='w')
            self.obs['entities'].append(dict(entity('transport-belt',.5,.5,4),unit_number=20))
            Path(root,'pcb-board.json').write_text(json.dumps({'world_id':'w','blocks':{'producer':{
                'reserved_area':[[0,-1],[2,2]],'ports':[source],'entities':[entity('transport-belt',.5,.5,4)]}}}))
            self.lua.execute('''mixed=false;unexpected=false
source_belt={name="transport-belt",valid=true,force=f,surface=s,unit_number=20,direction=4,position={x=.5,y=.5}}
source_belt.get_transport_line=function()return{get_contents=function()return{{name=mixed and"copper-plate"or"iron-plate",count=1}}end}end
local original_target=target
function target(p,name)
 if name=="transport-belt"and p.y==.5 then
  if p.x==.5 then return source_belt end
  if unexpected and p.x==1.5 then return source_belt end
 end;return original_target(p,name)end''')
            builder=SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,can_place=Mock(return_value={'ok':True}))
            destination=dict(kind='item',item='iron-plate',direction='input',facing=4,position={'x':10.5,'y':.5})
            def run():
                return plan_connection(self.game,builder,self.obs,source,destination)
            self.assertTrue(run()['ok'])
            for change,restore,reason in(('source_belt.unit_number=21','source_belt.unit_number=20','production_port_apron_belt_identity_changed'),
                ('mixed=true','mixed=false','production_port_apron_belt_contaminated'),
                ('unexpected=true','unexpected=false','production_port_apron_unexpected_belt')):
                self.lua.execute(change);self.assertEqual(run()['reason'],reason);self.lua.execute(restore)
            for change,restore in(('apron_resource=true','apron_resource=false'),('apron_water=true','apron_water=false'),
                                  ('apron_blocking=true','apron_blocking=false')):
                self.lua.execute(change);self.assertFalse(run()['ok'],change);self.lua.execute(restore)
            builder.can_place.return_value={'ok':False,'blocked':[dict(entity('transport-belt',1.5,.5),reason='terrain_or_entity_collision')]}
            self.assertEqual(run()['reason'],'native_corridor_preflight_failed')
            checked={tuple(e['position'].values())for e in builder.can_place.call_args.args[0]}
            self.assertTrue({(.5,.5),(1.5,.5),(2.5,.5)}<=checked)


if __name__=='__main__':unittest.main()
