from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_agent.core.deterministic_builder import FactoryBuilder,_plan
from factorio_agent.board import entity
from factorio_agent.outposts import MiningOutposts,mining_bank
from factorio_agent.paired_mining import mining_pair


class OutpostExpansionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.c=MiningOutposts.__new__(MiningOutposts);self.c.path=self.root/'mining-outposts.json'
        self.c.game=SimpleNamespace(query=Mock(return_value={'ok':True,'indices':[1,2]}))
        geometry=FactoryBuilder.__new__(FactoryBuilder)
        self.c.builder=SimpleNamespace(_occupied_by_plan=geometry._occupied_by_plan,
            ensure_plan=Mock(return_value={'type':'build','name':'electric-mining-drill'}))
        self.c.perimeter=SimpleNamespace(ensure_coverage=Mock(return_value={'type':'craft','recipe':'firearm-magazine'}),
                                         coverage_proof=Mock(return_value={'geometry':{'complete':True}}))
        self.old={'plan':mining_bank(.5,.5,1,'iron-ore'),'ready':True,'nominal_rate_per_minute':30}
        self.new={'ok':True,'plan':mining_pair(10.5,10.5,1,'iron-ore'),'ready':False,'nominal_rate_per_minute':60,
                  'reserved_area':[[9,9],[13,16]],'reserved_pair_slots':1}
        self.c.state={'world_id':'w','banks':[self.old,self.new]}
        self.c.publish_reservations=Mock(return_value=None)
        self.c.bank_proof=Mock(return_value={'nominal_rate_per_minute':30})
        self.obs={'world_id':'w','tick':1,'technologies':{'electric-mining-drill':True},
                  'entities':[dict(e,unit_number=i+1)for i,e in enumerate(self.old['plan']['entities'])]}
        self.request={'resource':'iron-ore','rate_per_minute':60}

    def test_pending_new_bank_waits_for_paid_coverage_and_reuses_durable_scope(self):
        old=deepcopy(self.old)
        action=self.c.step(self.obs,self.request)
        self.assertEqual(action['recipe'],'firearm-magazine')
        self.c.builder.ensure_plan.assert_not_called()
        scope=self.new['defense_scope_id']
        self.c.perimeter.ensure_coverage.assert_called_with(self.obs,scope,[self.new['reserved_area']])
        self.assertEqual(self.old,old)
        self.c.perimeter.ensure_coverage.return_value=None
        self.assertEqual(self.c.step(self.obs,self.request)['type'],'build')
        self.assertEqual(self.new['defense_scope_id'],scope)
        self.assertEqual(len(self.c.state['banks']),2)

    def test_all_new_solid_banks_use_the_policy_and_preserve_every_reservation(self):
        self.c.state['banks']=[dict(self.old,reserved_area=[[1,1],[4,4]])]
        board={'world_id':'w','blocks':{'factory':{'reserved_area':[[20,20],[30,30]]}},
               'shared_main_bus':{'reservations':[{'area':[[-12,-120],[8,-10]]}]}}
        (self.root/'pcb-board.json').write_text(json.dumps(board));before=deepcopy(self.c.state)
        for resource in('iron-ore','copper-ore','coal','stone'):
            with patch('factorio_agent.paired_mining.prepare_new_bank',return_value={'ok':True})as prepare:
                self.assertTrue(self.c.prepare(self.obs,resource,2,extra_reservations=[[[40,40],[44,44]]])['ok'])
                self.assertEqual(prepare.call_args.args[3:5],(resource,2))
                self.assertEqual(prepare.call_args.args[5],[board['blocks']['factory']['reserved_area'],
                    board['shared_main_bus']['reservations'][0]['area'],[[1,1],[4,4]],[[40,40],[44,44]]])
        self.assertEqual(self.c.state,before);self.assertFalse(self.c.path.exists())
        board['world_id']='foreign';(self.root/'pcb-board.json').write_text(json.dumps(board))
        with patch('factorio_agent.paired_mining.prepare_new_bank')as prepare:
            self.assertEqual(self.c.prepare(self.obs,'coal',2)['reason'],'mining_planning_board_world_changed')
            prepare.assert_not_called()

    def test_pending_extension_also_gates_before_first_new_producer(self):
        self.c.state['banks']=[self.old]
        extension=deepcopy(self.new)
        self.old['pending_extension']=extension
        self.assertEqual(self.c.step(self.obs,self.request)['type'],'craft')
        self.c.builder.ensure_plan.assert_not_called()
        self.assertIn('defense_scope_id',extension)

    def test_changed_retained_unit_blocks_before_even_new_defense_construction(self):
        self.c.state['banks']=[self.old];extension=deepcopy(self.new)
        extension['retained_units']=[{k:self.obs['entities'][0][k]for k in('name','position','unit_number','direction')}]
        self.old['pending_extension']=extension;self.obs['entities'][0]['unit_number']+=1000
        result=self.c.step(self.obs,self.request)
        self.assertEqual(result['reason'],'mining_retained_native_identity_changed')
        self.c.builder.ensure_plan.assert_not_called();self.c.perimeter.ensure_coverage.assert_not_called()

    def test_published_outer_bank_does_not_turn_clear_exit_into_an_ore_reservation(self):
        self.new['mining_reserved_area']=[[9,9],[13,16]];self.new['reserved_area']=[[9,9],[23,16]]
        self.new['layout_contract']={'fixed_output':'exact'}
        (self.root/'pcb-board.json').write_text(json.dumps({'world_id':'w','blocks':{}}))
        self.assertIsNone(MiningOutposts.publish_reservations(self.c,self.obs))
        block=json.loads((self.root/'pcb-board.json').read_text())['blocks']['iron-ore_mining']
        self.assertEqual(block['reserved_area'],self.new['reserved_area'])
        self.assertEqual(block['mining_reserved_area'],[[9,9],[13,16]])
        self.assertEqual(block['layout_contract'],self.new['layout_contract'])

    def test_declared_coverage_geometry_change_blocks_without_retargeting(self):
        self.c.step(self.obs,self.request)
        scope=self.new['defense_scope_id']
        self.new['reserved_area'][1][0]+=1
        self.assertEqual(self.c.step(self.obs,self.request)['reason'],'mining_defense_scope_geometry_changed')
        self.assertEqual(self.new['defense_scope_id'],scope)
        self.c.builder.ensure_plan.assert_not_called()

    def test_only_explicitly_scoped_new_bank_requires_current_coverage_in_proof(self):
        c=MiningOutposts.__new__(MiningOutposts);c.perimeter=self.c.perimeter
        bank={'plan':self.old['plan'],'nominal_rate_per_minute':30,'defense_scope_id':'mining-test'}
        obs={**self.obs,'entities':[dict(e,unit_number=i+1,energy=100,electric_network_connected=True,
            belt_inventory={'iron-ore':3})for i,e in enumerate(bank['plan']['entities'])]}
        with patch('factorio_agent.outposts.underground_proof',return_value=True):
            self.assertIsNotNone(c.bank_proof(obs,bank)['defense_coverage'])
            c.perimeter.coverage_proof.return_value=None
            self.assertIsNone(c.bank_proof(obs,bank))
            bank.pop('defense_scope_id')
            self.assertIsNotNone(c.bank_proof(obs,bank))

    def connection_fixture(self):
        primary={'plan':mining_bank(.5,.5,1,'iron-ore')}
        source=primary['plan']['ports'][0]
        belts=[entity('transport-belt',source['position']['x']+i,source['position']['y'],4)for i in range(10)]
        self.obs['entities']=[dict(e,unit_number=200+i)for i,e in enumerate(belts)]
        plan=_plan(belts,[source])
        (self.root/'smelting-district.json').write_text(json.dumps({'world_id':'w','plan':plan}))
        self.c.prepare=Mock(side_effect=[deepcopy(self.new),dict(deepcopy(self.new),reserved_area=[[19,9],[23,16]])])
        return primary

    def test_bounded_alternate_site_reservations_are_local_and_prepare_is_readonly(self):
        primary=self.connection_fixture();before=deepcopy(self.c.state)
        route={'ok':True,'entities':[],'underground_pairs':[],'route_metrics':{'length':1,'turns':0}}
        with patch('factorio_agent.outposts.belt_connection',side_effect=[{'ok':False,'reason':'no_safe_bounded_mining_exit'},route]),\
             patch('factorio_agent.outposts.preflight',return_value={'ok':True,'clear_obstacles':[]}):
            result=self.c.prepare_connected_bank(self.obs,primary,1)
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['planning_attempts'],{'sites':2,'routes':2})
        self.assertEqual(self.c.prepare.call_args.kwargs['extra_reservations'],[self.new['reserved_area']])
        self.assertEqual(self.c.state,before)
        self.assertFalse(self.c.path.exists())

    def test_no_fit_is_bounded_to_six_sites_and_eighteen_routes(self):
        primary=self.connection_fixture()
        self.c.prepare.side_effect=lambda *a,**kw:deepcopy(self.new)
        with patch('factorio_agent.outposts.belt_connection',return_value={'ok':False,'reason':'no_clean_trunk_corridor'})as route:
            result=self.c.prepare_connected_bank(self.obs,primary,1)
        self.assertFalse(result['ok'])
        self.assertEqual(self.c.prepare.call_count,6)
        self.assertLessEqual(route.call_count,18)
        self.assertFalse(self.c.path.exists())

    def test_final_whole_plan_preflight_failure_never_publishes_connection(self):
        primary=self.connection_fixture();self.c.prepare.side_effect=lambda *a,**kw:deepcopy(self.new)
        route={'ok':True,'entities':[],'underground_pairs':[],'route_metrics':{}}
        with patch('factorio_agent.outposts.belt_connection',return_value=route),\
             patch('factorio_agent.outposts.preflight',return_value={'ok':False,'reason':'whole_block_prospective_overlap'}):
            result=self.c.prepare_connected_bank(self.obs,primary,1)
        self.assertFalse(result['ok'])
        self.assertIn('whole_block_prospective_overlap',result['candidates'])
        self.assertNotIn('consumer_connection',self.new)

    def traced_fixture(self):
        primary=self.connection_fixture();source=primary['plan']['ports'][0]
        y=source['position']['y'];x=source['position']['x']
        specs=[entity('transport-belt',x+i,y,4)for i in range(41)if i not in (11,12,13)]
        for spec in specs:
            if spec['position']['x'] in (x+10,x+14):
                spec.update(name='underground-belt',belt_to_ground_type='input'if spec['position']['x']==x+10 else'output')
        pair={'input':[x+10,y],'output':[x+14,y],'direction':4,'span':4}
        plan=_plan(specs,[source],underground_pairs=[pair])
        destination={'item':'iron-ore','direction':'input','facing':4,'position':{'x':x+40,'y':y}}
        (self.root/'smelting-district.json').write_text(json.dumps({'world_id':'w','plan':plan,
            'banks':[{'resource':'iron-ore','ports':[destination]}]}))
        self.obs['entities']=[dict(e,unit_number=300+i)for i,e in enumerate(specs)]
        mouths=[e for e in self.obs['entities']if e['name']=='underground-belt']
        for mouth,other in zip(mouths,reversed(mouths)):
            mouth.update(underground_pair_verified=True,underground_neighbour={'unit_number':other['unit_number']})
        def legal(script):
            raw=script.split('helpers.json_to_table(',1)[1].split(');',1)[0]
            payload=json.loads(json.loads(raw))
            return {'ok':True,'indices':list(range(1,len(payload['targets'])+1))}
        self.c.game.query.side_effect=legal
        return primary

    def test_saved_corridor_traces_underground_and_reaches_joins_beyond_first_thirty_tiles(self):
        primary=self.traced_fixture()
        self.c.prepare.side_effect=lambda *a,**kw:deepcopy(self.new)
        def route(game,builder,obs,source,destination,reserved):
            if destination['position']['x']<35:return {'ok':False,'reason':'no_clean_trunk_corridor'}
            return {'ok':True,'entities':[],'underground_pairs':[],'route_metrics':{'length':1}}
        with patch('factorio_agent.outposts.belt_connection',side_effect=route),\
             patch('factorio_agent.outposts.preflight',return_value={'ok':True,'clear_obstacles':[]}):
            result=self.c.prepare_connected_bank(self.obs,primary,1)
        self.assertTrue(result['ok'],result)
        self.assertGreater(result['consumer_connection']['target']['position']['x'],35)
        self.assertEqual(result['planning_attempts']['sites'],1)

    def test_changed_existing_underground_partner_blocks_before_choosing_new_bank(self):
        primary=self.traced_fixture()
        next(e for e in self.obs['entities']if e['name']=='underground-belt')['underground_neighbour']['unit_number']=999
        result=self.c.prepare_connected_bank(self.obs,primary,1)
        self.assertEqual(result['reason'],'owned_ore_trunk_underground_pair_changed')
        self.c.prepare.assert_not_called()

    def test_changed_existing_trunk_direction_blocks_before_choosing_new_bank(self):
        primary=self.traced_fixture()
        self.obs['entities'][3]['direction']=0
        result=self.c.prepare_connected_bank(self.obs,primary,1)
        self.assertEqual(result['reason'],'owned_ore_trunk_changed_before_expansion')
        self.c.prepare.assert_not_called()


if __name__=='__main__':unittest.main()
