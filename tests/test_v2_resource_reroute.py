from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.board import entity
from factorio_agent.outposts import MiningOutposts
from factorio_agent.paired_mining import mining_pair
from factorio_agent.resource_reroute import SteamCoalReroute, partition_routes
from factorio_agent.resource_policy import annotate_mining_exit, validate_annotations
from factorio_agent.smelting import merge_entities, port
from factorio_agent.transport import xy


def routes():
    old = [entity('transport-belt',2.5,2.5,4), entity('transport-belt',3.5,2.5,8),
           entity('transport-belt',3.5,3.5,8)]
    old += [entity('transport-belt',x+.5,4.5,4 if x<8 else 0) for x in range(3,9)]
    old += [entity('transport-belt',8.5,y+.5,0 if y>1 else 4) for y in (3,2,1)]
    old += [entity('transport-belt',9.5,1.5,4),entity('transport-belt',10.5,1.5,8),entity('transport-belt',10.5,2.5,8)]
    new = [entity('transport-belt',x+.5,2.5,4 if x<6 else 0) for x in range(2,7)]
    new += [entity('transport-belt',x+.5,1.5,4 if x<10 else 8) for x in range(6,11)]
    new += [entity('transport-belt',10.5,2.5,8)]
    return old,new


class SteamCoalRerouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = SimpleNamespace(fingerprint='catalog')
        self.old, self.new = routes()
        mining = mining_pair(.5,.5,1,'coal')
        destination = port('coal',10.5,2.5,8)
        core = _plan([self.old[-1],entity('boiler',10.5,5,8),entity('steam-engine',10.5,8.5,8),
                      entity('steam-engine',10.5,13.5,8),entity('small-electric-pole',12.5,5.5)],
                     [destination],reserved_area=[[8,0],[14,17]])
        plan = _plan(merge_entities(mining['entities'],self.old,core['entities']),[destination],underground_pairs=[])
        bank_plan = _plan(merge_entities(mining['entities'],self.old),mining['ports'],
                         **{k:v for k,v in mining.items()if k not in ('ok','reason','entities','ports','required_items')},underground_pairs=[])
        bank = {'plan':bank_plan,'ready':True,'power_only':True,'reserved_area':[[-1,-1],[3,6]],
                'nominal_rate_per_minute':60}
        self.steam = SimpleNamespace(path=self.root/'steam-expansion.json',state={
            'world_id':'w','catalog_fingerprint':'catalog','core':core,'plan':plan,'bank':deepcopy(bank),
            'preserved_engine_units':[5000],'samples':[]})
        self.outposts = MiningOutposts.__new__(MiningOutposts)
        self.outposts.path = self.root/'mining-outposts.json'
        self.outposts.state = {'world_id':'w','banks':[deepcopy(bank)]}
        self.obs = {'world_id':'w','tick':100,'inventory':{},'entities':[
            dict(e,unit_number=i+100,energy=100,electric_network_connected=True)for i,e in enumerate(plan['entities'])]}
        self.obs['entities'].append(dict(entity('steam-engine',-30.5,-30.5,8),unit_number=5000))
        self.next_unit = 6000
        self.carry = True;self.coal = True;self.power = True
        self.game = Mock();self.game.cfg=SimpleNamespace(runtime_dir=self.root)
        self.game.query.side_effect = self.query
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        self.builder = SimpleNamespace(_occupied_by_plan=geometry._occupied_by_plan,
            ensure_plan=Mock(side_effect=self.ensure),
            bootstrap=SimpleNamespace(ensure_item=Mock(return_value={'type':'craft','recipe':'transport-belt','count':1})))
        self.steam.sample = Mock(side_effect=self.sample)
        self.controller = SteamCoalReroute(self.game,self.builder,self.catalog,self.steam,self.outposts)
        board = {'world_id':'w','catalog_fingerprint':'catalog','blocks':{
            'steam_expansion':dict(plan,reserved_area=core['reserved_area']),
            'coal_mining':dict(bank_plan,reserved_area=bank['reserved_area'])}}
        (self.root/'pcb-board.json').write_text(json.dumps(board))
        self.route = _plan(self.new,[],underground_pairs=[],clear_obstacles=[],
            route_metrics={'length':len(self.new),'turns':3,'manhattan_distance':8})
        self.plan_route = patch('factorio_agent.resource_reroute.plan_connection',return_value=self.route).start()
        self.native_preflight = patch('factorio_agent.resource_reroute.preflight',return_value={'ok':True,'clear_obstacles':[]}).start()
        self.addCleanup(patch.stopall)

    def query(self,body):
        if 'return {stock=out}' in body:
            return {'stock':{'transport-belt':50 if self.carry else 0,'underground-belt':50 if self.carry else 0}}
        return {'ok':True,'tick':self.obs['tick']+1,'count':1 if self.coal else 0}

    def sample(self,obs):
        if not self.power:
            return {'ok':False,'reason':'engine_waiting_for_steam_or_load'}
        units = [e['unit_number']for e in obs['entities']if e['name']=='steam-engine'and e['unit_number']!=5000]
        return {'ok':True,'tick':obs['tick']+2,'engines':[{'unit_number':u,'kw':100,'steam':100}for u in units]}

    def ensure(self,obs,plan):
        for spec in plan['entities']:
            live=next((e for e in obs['entities']if e['name']==spec['name']and xy(e)==xy(spec)),None)
            if not live:
                return dict(spec,type='build',item=spec['name'])
            if live['direction']!=spec['direction']:
                return {'status':'blocked','reason':'test_wrong_direction'}
        return {'status':'succeeded'}

    def apply(self,action):
        if action.get('type')=='build':
            self.obs['entities'].append(dict(action,unit_number=self.next_unit));self.next_unit+=1
        elif action.get('type')in ('mine','mine_many'):
            rows=action['actions']if action['type']=='mine_many'else[action]
            units={e['expected_entity_unit']for e in rows}
            self.obs['entities']=[e for e in self.obs['entities']if e['unit_number']not in units]
        self.obs['tick']+=30

    def reach_sample(self):
        for _ in range(30):
            result=self.controller.step(self.obs)
            if self.controller.state.get('phase')=='sample':
                return result
            self.assertIn(result.get('type'),('build','mine'),result)
            self.apply(result)
        self.fail('did not reach fresh route sampling')

    def mature(self):
        for _ in range(4):
            self.obs['tick']+=600
            result=self.controller.step(self.obs)
            if result.get('type')=='mine_many':return result
        self.fail('did not acquire a thirty-second native sample window')

    def test_prepare_is_readonly_keeps_ports_and_ignores_only_exact_saved_route_units(self):
        before=deepcopy(self.steam.state)
        with patch('factorio_agent.resource_reroute._atomic_json')as write:
            prepared=self.controller.prepare(self.obs);write.assert_not_called()
        self.assertTrue(prepared['ok'],prepared)
        ignored=set(self.plan_route.call_args.kwargs['ignored_units'])
        expected={e['unit_number']for e in self.obs['entities']if any(e['name']==s['name']and xy(e)==xy(s)for s in self.old)}
        source_unit = next(e['unit_number'] for e in self.obs['entities']
                           if e['name'] == 'transport-belt' and xy(e) == xy(prepared['source_port']))
        self.assertEqual(ignored,expected - {source_unit})
        self.assertIn(source_unit, {e['unit_number'] for e in prepared['protected']})
        routed_obs = self.plan_route.call_args.args[2]
        self.assertIn(source_unit, {e['unit_number'] for e in routed_obs['entities']})
        self.assertEqual(prepared['final_plan']['ports'],self.steam.state['plan']['ports'])
        self.assertEqual(prepared['final_bank_plan']['ports'],self.steam.state['bank']['plan']['ports'])
        self.assertEqual(self.steam.state,before)
        self.assertFalse(self.controller.path.exists())

    def test_prebuild_finishes_before_paid_guarded_corner_cutover(self):
        saw_build=False
        for _ in range(20):
            result=self.controller.step(self.obs)
            if result.get('type')=='mine':
                self.assertTrue(saw_build)
                self.assertEqual(result['expected_entity_world_id'],'w')
                for spec in self.controller.state['prebuild']:
                    self.assertTrue(any(xy(e)==xy(spec)and e['direction']==spec['direction']for e in self.obs['entities']))
                return
            self.assertEqual(result.get('type'),'build');saw_build=True;self.apply(result)
        self.fail('no guarded cutover')

    def test_scoped_exit_is_deferred_and_built_with_complete_retained_context(self):
        path = [{'x': x + .5, 'y': 2.5} for x in range(2, 6)]
        annotate_mining_exit(self.new, 'coal', path[0], 2, path=path)
        self.reach_sample()
        self.assertFalse(any(e.get('mining_exit') for e in self.controller.state['prebuild']))
        self.assertIn((4.5, 2.5), {xy(e) for e in self.controller.state['deferred']})
        built = self.builder.ensure_plan.call_args.args[1]
        self.assertTrue(all((p['x'], p['y']) in {xy(e) for e in built['entities']} for p in path))
        validate_annotations(built['entities'], self.builder)

    def test_all_changed_replacement_stock_is_required_before_any_corner_mine(self):
        self.carry=False
        for _ in range(20):
            result=self.controller.step(self.obs)
            if result.get('type')=='craft':
                self.assertFalse(self.controller.state.get('pending_recovery'))
                self.assertTrue(all(self.controller._exact(self.obs,e)for e in self.controller.state['conflicts']))
                return
            self.assertEqual(result.get('type'),'build');self.apply(result)
        self.fail('did not request prepaid materials')

    def test_no_old_buffer_retirement_without_added_path_coal_even_with_healthy_old_tail(self):
        self.coal=False
        self.reach_sample()
        obsolete=deepcopy(self.controller.state['obsolete'])
        for _ in range(4):
            self.obs['tick']+=600
            result=self.controller.step(self.obs)
            self.assertEqual(result['status'],'waiting')
            self.assertTrue(all(self.controller._exact(self.obs,e)for e in obsolete))
        self.assertFalse(self.controller.state['samples'])

    def test_thirty_second_window_accumulates_then_retires_only_old_units_in_eight_bound(self):
        self.reach_sample()
        retirement=self.mature()
        original_protected={e['unit_number']for e in self.controller.state['protected']}
        for _ in range(5):
            self.assertEqual(retirement['type'],'mine_many')
            self.assertLessEqual(len(retirement['actions']),8)
            self.assertFalse(original_protected&{e['expected_entity_unit']for e in retirement['actions']})
            self.apply(retirement)
            retirement=self.controller.step(self.obs)
            if retirement.get('status')=='succeeded':break
        self.assertEqual(retirement.get('status'),'succeeded')
        self.obs['tick']+=30
        self.assertIsNotNone(self.controller.proof(self.obs))
        self.assertTrue(original_protected<={e['unit_number']for e in self.obs['entities']})
        saved=json.loads(self.outposts.path.read_text())
        self.assertEqual(saved['banks'][0]['plan'],self.steam.state['bank']['plan'])
        self.assertEqual(set(json.loads((self.root/'pcb-board.json').read_text())['blocks']),{'coal_mining','steam_expansion'})

    def test_retirement_pauses_on_fresh_engine_failure_and_preserves_remaining_old_assets(self):
        self.reach_sample();self.mature()
        remaining=deepcopy(self.controller.state['obsolete'])
        self.power=False;self.obs['tick']+=30
        result=self.controller.step(self.obs)
        self.assertEqual(result['status'],'waiting')
        self.assertTrue(all(self.controller._exact(self.obs,e)for e in remaining))
        self.assertFalse(self.steam.state['samples'])

    def test_unknown_new_unit_at_cutover_position_is_never_retargeted(self):
        for _ in range(20):
            result=self.controller.step(self.obs)
            if result.get('type')=='mine':break
            self.apply(result)
        old=next(e for e in self.obs['entities']if e['unit_number']==result['expected_entity_unit'])
        old['unit_number']+=10000
        blocked=self.controller.step(self.obs)
        self.assertEqual(blocked['reason'],'coal_recovery_will_not_retarget_new_unit')

    def test_resume_prunes_mined_corner_before_building_replacement(self):
        for _ in range(20):
            result=self.controller.step(self.obs)
            if result.get('type')=='mine':break
            self.apply(result)
        self.apply(result)
        self.controller=SteamCoalReroute(self.game,self.builder,self.catalog,self.steam,self.outposts)
        result=self.controller.step(self.obs)
        self.assertEqual(result.get('type'),'build')
        self.assertFalse(self.controller.state['conflicts'])
        self.apply(result)
        self.controller.step(self.obs)
        self.assertEqual(self.controller.state['phase'],'sample')

    def test_missing_old_asset_blocks_before_hypothetical_route_planning(self):
        missing=next(e for e in self.obs['entities']if xy(e)==(3.5,3.5))
        self.obs['entities'].remove(missing)
        result=self.controller.prepare(self.obs)
        self.assertEqual(result['reason'],'old_steam_asset_missing_or_changed')
        self.plan_route.assert_not_called()

    def test_more_than_eight_old_cutover_conflicts_fails_closed(self):
        with patch('factorio_agent.resource_reroute.partition_routes',return_value={'conflicts':[{}]*9}):
            result=self.controller.prepare(self.obs)
        self.assertEqual(result['reason'],'coal_reroute_changes_more_than_eight_owned_conflicts')

    def test_declared_old_underground_pair_requires_native_neighbour_verification(self):
        pair={'input':[4.5,4.5],'output':[6.5,4.5],'direction':4,'span':2}
        for plan in (self.steam.state['plan'],self.steam.state['bank']['plan']):
            plan['entities']=[e for e in plan['entities']if xy(e)!=(5.5,4.5)]
            for e in plan['entities']:
                if xy(e)in((4.5,4.5),(6.5,4.5)):
                    e['name']='underground-belt';e['belt_to_ground_type']='input'if xy(e)==(4.5,4.5)else'output'
            plan['underground_pairs']=[pair]
        self.outposts.state['banks'][0]['plan']=deepcopy(self.steam.state['bank']['plan'])
        self.obs['entities']=[e for e in self.obs['entities']if xy(e)!=(5.5,4.5)]
        for e in self.obs['entities']:
            if xy(e)in((4.5,4.5),(6.5,4.5)):
                e['name']='underground-belt';e['belt_to_ground_type']='input'if xy(e)==(4.5,4.5)else'output'
        result=self.controller.prepare(self.obs)
        self.assertEqual(result['reason'],'old_steam_coal_underground_pair_not_verified')

    def test_foreign_existing_belt_on_alternative_is_not_claimed_as_owned(self):
        self.obs['entities'].append(dict(self.new[2],unit_number=99999))
        result=self.controller.prepare(self.obs)
        self.assertEqual(result['reason'],'alternative_coal_route_would_claim_foreign_existing_asset')

    def test_existing_quality_gate_failure_is_reported_without_snake_fallback(self):
        self.plan_route.return_value={'ok':False,'reason':'no_bounded_trunk_route'}
        result=self.controller.prepare(self.obs)
        self.assertEqual((result['reason'],result['stage']),('no_bounded_trunk_route','bounded_resource_avoiding_coal_route'))

    def test_partial_old_underground_pairs_are_coupled_instead_of_retained_by_position(self):
        old=[dict(entity('underground-belt',.5,.5,4),belt_to_ground_type='input'),
             dict(entity('underground-belt',3.5,.5,4),belt_to_ground_type='output')]
        new=[dict(old[0]),dict(entity('underground-belt',4.5,.5,4),belt_to_ground_type='output')]
        parts=partition_routes({'entities':old,'underground_pairs':[{'input':[.5,.5],'output':[3.5,.5],'direction':4}]},
            {'entities':new,'underground_pairs':[{'input':[.5,.5],'output':[4.5,.5],'direction':4}]},self.builder)
        self.assertFalse(parts['retained'])
        self.assertEqual(len(parts['conflicts']),2)
        self.assertEqual(len(parts['deferred']),2)


if __name__=='__main__':unittest.main()
