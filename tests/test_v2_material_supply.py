from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_agent.material_supply import DedicatedMaterials
from factorio_agent.outposts import MiningOutposts
from factorio_agent.paired_mining import mining_pair
from factorio_agent.transport import xy
from factorio_agent.resource_policy import mining_exit_candidates, mining_reservations, validate_annotations


def bank(resource, x=30.5, y=20.5):
    return {'ok': True, 'ready': False, 'plan': mining_pair(x, y, 1, resource),
            'reserved_area': [[x-1.5, y-1.5], [x+2.5, y+5.5]], 'reserved_pair_slots': 1,
            'nominal_rate_per_minute': 60, 'wire_hops': 0, 'remaining_resource_survey': 10000}


class MaterialSupplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = SimpleNamespace(fingerprint='catalog')
        self.game = Mock()
        self.game.cfg = SimpleNamespace(runtime_dir=self.root)
        self.game.query.return_value = {'ok': True, 'offset': 0}
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        self.builder = SimpleNamespace(ensure_plan=Mock(return_value={'status': 'waiting'}),
                                       _occupied_by_plan=geometry._occupied_by_plan)
        self.outposts = MiningOutposts.__new__(MiningOutposts)
        self.outposts.path = self.root/'mining-outposts.json'
        old = bank('coal', -40.5, -40.5); old['ready'] = True
        old['consumer_connection'] = {'existing_boiler': True}
        self.outposts.state = {'world_id': 'w', 'banks': [old]}
        self.old_bank = deepcopy(old)
        (self.root/'pcb-board.json').write_text(json.dumps({'world_id': 'w', 'catalog_fingerprint': 'catalog',
            'blocks': {'coal_mining': dict(old['plan'], reserved_area=old['reserved_area'])}}))
        self.obs = {'world_id': 'w', 'tick': 100, 'technologies': {'electric-mining-drill': True}, 'entities': []}
        self.controller = DedicatedMaterials(self.game, self.builder, self.catalog, self.outposts)
        self.prepare_bank = patch('factorio_agent.material_supply.prepare_new_bank', side_effect=lambda game,builder,obs,resource,count,reservations:
                                  bank(resource, 30.5 if resource == 'coal' else 70.5)).start()
        self.preflight = patch('factorio_agent.material_supply.preflight', return_value={'ok': True, 'clear_obstacles': []}).start()
        self.addCleanup(patch.stopall)
        self.outposts.bank_proof = Mock(side_effect=self.native_proof)

    def native_proof(self, obs, prepared):
        item = prepared['plan']['resource']
        units = sorted(e['unit_number'] for e in obs['entities'] if e['name'] == 'electric-mining-drill'
                       and any(xy(e) == xy(w) for w in prepared['plan']['entities'] if w['name'] == 'electric-mining-drill'))
        return {'drill_units': units, 'native_producer_lanes': {'ok': True, 'left': {item: 1}, 'right': {item: 1}}}

    def build(self, resource):
        self.controller.step(self.obs, {'resource': resource})
        plan = self.controller.state['supplies'][resource]['bank']['plan']
        self.obs['entities'] = [dict(e, unit_number=i+100, energy=100, electric_network_connected=True)
                                for i,e in enumerate(plan['entities'])]
        self.builder.ensure_plan.return_value = {'status': 'succeeded'}
        return self.controller.step(self.obs, {'resource': resource})

    def test_first_prepare_is_readonly_and_never_reuses_active_coal_bank(self):
        with patch('factorio_agent.material_supply._atomic_json') as write:
            prepared = self.controller.prepare(self.obs, {'resource': 'coal'})
            write.assert_not_called()
        self.assertTrue(prepared['ok'])
        self.assertEqual(self.prepare_bank.call_args.args[4], 2)
        self.assertNotEqual(prepared['bank']['plan']['ports'], self.old_bank['plan']['ports'])
        self.assertEqual(self.outposts.state['banks'], [self.old_bank])
        self.assertFalse(self.controller.path.exists())
        self.assertFalse(self.controller.sources_path.exists())

    def test_defense_action_or_wait_precedes_any_new_mining_construction(self):
        perimeter = Mock()
        self.controller.perimeter = perimeter
        action = {'type': 'build', 'name': 'gun-turret'}
        perimeter.ensure_coverage.return_value = action
        self.assertEqual(self.controller.step(self.obs, {'resource': 'stone'}), action)
        self.builder.ensure_plan.assert_not_called()
        entry = self.controller.state['supplies']['stone']
        perimeter.ensure_coverage.assert_called_once_with(
            self.obs, entry['material_supply_id'], [entry['bank']['reserved_area']])
        self.assertFalse(self.controller.sources_path.exists())
        perimeter.ensure_coverage.return_value = {'status': 'waiting', 'reason': 'arming new post'}
        self.assertEqual(self.controller.step(self.obs, {'resource': 'stone'})['status'], 'waiting')
        self.builder.ensure_plan.assert_not_called()
        perimeter.ensure_coverage.return_value = None
        self.controller.step(self.obs, {'resource': 'stone'})
        self.builder.ensure_plan.assert_called_once()

    def test_paid_coal_distribution_preserves_original_port_and_has_real_nonoverlapping_footprints(self):
        prepared = self.controller.prepare(self.obs, {'resource': 'coal'})
        new = prepared['bank']
        original = bank('coal')['plan']['ports'][0]
        self.assertEqual(new['plan']['ports'][0], original)
        x,y = xy(original)
        outputs = new['material_distribution']['ports']
        self.assertEqual((xy(outputs['coal']),xy(outputs['brick-coal'])), ((x+2,y),(x+2,y+1)))
        self.assertEqual(new['plan']['required_items']['splitter'], 1)
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        boxes = [geometry._occupied_by_plan([e]) for e in new['plan']['entities']]
        self.assertEqual(sum(map(len,boxes)),len(set().union(*boxes)))
        self.assertEqual(new['nominal_rate_per_minute'],60)

    def test_fresh_stone_prepare_has_reservations_without_any_saved_stone_bank(self):
        self.assertFalse(any(b['plan']['resource']=='stone' for b in self.outposts.state['banks']))
        with patch('factorio_agent.material_supply._atomic_json') as write:
            prepared = self.controller.prepare(self.obs, {'resource':'stone'})
            write.assert_not_called()
        self.assertTrue(prepared['ok'])
        self.assertEqual(self.prepare_bank.call_args.args[3:5], ('stone',2))
        self.assertTrue(self.prepare_bank.call_args.args[5])
        self.assertFalse(self.controller.path.exists())

    def test_coal_retries_a_different_bank_after_rich_central_site_has_no_bounded_hub(self):
        candidates = [bank('coal',30.5), bank('coal',60.5)]
        self.prepare_bank.side_effect = deepcopy(candidates)
        self.game.query.side_effect = [{'ok':False,'reason':'no_ore_free_coal_hub_within_short_east_escape'},
                                       {'ok':True,'offset':0,'last_resource_step':0}]
        original = deepcopy(self.outposts.state)
        with patch('factorio_agent.material_supply._atomic_json') as write:
            prepared = self.controller.prepare(self.obs,{'resource':'coal'})
            write.assert_not_called()
        self.assertTrue(prepared['ok'])
        self.assertEqual(xy(prepared['bank']['plan']['ports'][0]),xy(candidates[1]['plan']['ports'][0]))
        self.assertIn(candidates[0]['reserved_area'],self.prepare_bank.call_args_list[1].args[5])
        self.assertEqual(self.outposts.state,original)
        self.assertEqual(self.game.query.call_count,2)

    def test_coal_site_retries_are_bounded_and_never_change_existing_reservations(self):
        self.prepare_bank.side_effect = [bank('coal',30.5+20*i) for i in range(6)]
        self.game.query.return_value = {'ok':False,'reason':'no_bounded_hub'}
        board_before = (self.root/'pcb-board.json').read_text()
        result = self.controller.prepare(self.obs,{'resource':'coal'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['attempted_banks'],6)
        self.assertEqual(self.prepare_bank.call_count,6)
        self.assertEqual((self.root/'pcb-board.json').read_text(),board_before)
        self.assertFalse(self.controller.path.exists())

    def test_bank_registered_once_ready_only_after_native_proof_and_normal_reservation_names(self):
        self.controller.step(self.obs, {'resource': 'coal'})
        registered = json.loads(self.outposts.path.read_text())
        self.assertEqual(len(registered['banks']), 2)
        self.assertFalse(registered['banks'][1]['ready'])
        self.assertEqual(registered['banks'][0], self.old_bank)
        self.assertFalse(self.controller.sources_path.exists())
        self.controller = DedicatedMaterials(self.game, self.builder, self.catalog, self.outposts)
        self.controller.step(self.obs, {'resource': 'coal'})
        self.assertEqual(len(json.loads(self.outposts.path.read_text())['banks']),2)
        self.assertEqual(self.prepare_bank.call_count,1)
        board = json.loads((self.root/'pcb-board.json').read_text())
        self.assertEqual(set(board['blocks']), {'coal_mining','coal_mining_1'})

    def test_coal_publication_binds_two_distinct_actual_ends_and_does_not_double_supply(self):
        result = self.build('coal')
        self.assertEqual(result['status'],'succeeded')
        saved = json.loads(self.controller.sources_path.read_text())['sources']
        self.assertEqual(set(saved), {'coal','brick-coal'})
        self.assertNotEqual(saved['coal']['unit_number'],saved['brick-coal']['unit_number'])
        self.assertEqual(saved['coal']['material_supply_id'],saved['brick-coal']['material_supply_id'])
        for source in saved.values():
            self.assertEqual((source['item'],source['facing'],source['direction']),('coal',4,'output'))
            self.assertEqual(source['nominal_shared_rate_per_minute'],60)
        self.assertTrue(json.loads(self.outposts.path.read_text())['banks'][1]['ready'])

    def test_stone_publishes_original_typed_output_without_a_distribution_alias(self):
        result = self.build('stone')
        self.assertEqual(result['status'],'succeeded')
        sources = json.loads(self.controller.sources_path.read_text())['sources']
        self.assertEqual(set(sources),{'stone'})
        self.assertEqual(sources['stone']['item'],'stone')
        board = json.loads((self.root/'pcb-board.json').read_text())
        self.assertIn('stone_mining',board['blocks'])

    def test_changed_output_identity_is_rejected_and_registry_not_retargeted(self):
        self.build('coal')
        before = json.loads(self.controller.sources_path.read_text())
        old = before['sources']['coal']['unit_number']
        next(e for e in self.obs['entities'] if e['unit_number']==old)['unit_number'] += 1000
        result = self.controller.step(self.obs,{'resource':'coal'})
        self.assertEqual(result['reason'],'dedicated_material_entity_identity_changed')
        self.assertEqual(json.loads(self.controller.sources_path.read_text()),before)

    def test_latest_outpost_file_is_refreshed_before_append(self):
        extra = bank('stone', -90.5, 50.5)
        latest = deepcopy(self.outposts.state);latest['banks'].append(extra)
        self.outposts.path.write_text(json.dumps(latest))
        self.controller.step(self.obs,{'resource':'coal'})
        result = json.loads(self.outposts.path.read_text())
        self.assertEqual(result['banks'][:2],latest['banks'])
        self.assertEqual(len(result['banks']),3)

    def test_collector_lanes_need_both_witnesses_but_not_a_packet_on_each_output_tile(self):
        self.build('coal')
        self.outposts.bank_proof.side_effect = None
        units = self.controller.state['supplies']['coal']['lane_witness']['drill_units']
        self.outposts.bank_proof.return_value = {'drill_units':units,'native_producer_lanes':{'ok':True,'left':{},'right':{}}}
        self.obs['tick'] += 60
        self.assertIsNotNone(self.controller.proof(self.obs,{'resource':'coal'}))
        self.obs['tick'] += 601
        self.assertIsNone(self.controller.proof(self.obs,{'resource':'coal'}))

    def test_resource_approval_is_local_and_coal_hub_has_no_blanket_ore_exception(self):
        self.game.query.return_value = {'ok':True,'offset':3,'last_resource_step':2}
        prepared = self.controller.prepare(self.obs,{'resource':'coal'})
        plan = prepared['bank']['plan']
        self.assertIn('mining_footprint',plan)
        self.assertIn('resource_escape_area',plan)
        splitter = prepared['bank']['material_distribution']['splitter']
        self.assertNotIn('mining_resource',splitter)
        ends = prepared['bank']['material_distribution']['ports']
        for spec in plan['entities']:
            if any(xy(spec)==xy(p) for p in ends.values()):
                self.assertNotIn('mining_resource',spec)
        approved = [e for e in plan['entities'] if e.get('mining_resource')]
        self.assertTrue(approved)
        self.assertTrue(all(e['name'] in ('electric-mining-drill','transport-belt','small-electric-pole') for e in approved))
        self.assertTrue(all(e['mining_resource']=='coal' for e in approved))
        exits = [e for e in plan['entities'] if e.get('mining_exit')]
        self.assertEqual([e['mining_exit']['step'] for e in exits],[1,2])
        self.assertTrue(all(e['mining_exit']['length']==2 for e in exits))
        self.assertTrue(all(e.get('mining_footprint') for e in approved if e.get('resource_role')=='mining_core'))
        for spec in approved:
            if spec.get('resource_role')!='mining_core':
                continue
            area = spec['mining_footprint']
            for px,py in self.builder._occupied_by_plan([spec]):
                self.assertTrue(area[0][0]<=px-.5 and px+.5<=area[1][0])
                self.assertTrue(area[0][1]<=py-.5 and py+.5<=area[1][1])
        self.assertNotIn('--',self.game.query.call_args.args[0])

    def test_coal_south_exit_keeps_original_port_and_places_shared_splitter_off_patch(self):
        original = bank('coal')['plan']['ports'][0]
        path = mining_exit_candidates(original['position'])[2][:7]
        self.game.query.return_value = {'ok': True, 'exit_prefix': path, 'exit_length': 5, 'clear_position': path[-1]}
        prepared = self.controller.prepare(self.obs, {'resource': 'coal'})
        self.assertTrue(prepared['ok'], prepared)
        new = prepared['bank']; plan = new['plan']
        self.assertEqual(plan['ports'][0], original)
        validate_annotations(plan['entities'], self.builder)
        hub_x, hub_y = path[-1]['x'], path[-1]['y']
        self.assertEqual(xy(new['material_distribution']['splitter']), (hub_x+1, hub_y+.5))
        self.assertEqual([xy(p) for p in new['material_distribution']['ports'].values()],
                         [(hub_x+2, hub_y), (hub_x+2, hub_y+1)])
        by_position = {xy(e): e for e in plan['entities']}
        self.assertEqual([by_position[p['x'], p['y']]['direction'] for p in path], [4]+[8]*5+[4])
        self.assertNotIn('mining_resource', by_position[hub_x, hub_y])
        self.assertNotIn('mining_resource', new['material_distribution']['splitter'])
        self.assertEqual(new['nominal_rate_per_minute'], 60)
        self.assertEqual(new['reserved_area'][1][1], hub_y+1.5)
        self.assertEqual(plan['mining_reserved_area'], bank('coal')['reserved_area'])
        self.controller.step(self.obs, {'resource': 'coal'})
        zones = mining_reservations(self.game, 'w')
        self.assertIn({'resource': 'coal', 'area': plan['mining_reserved_area']}, zones)
        for spec in [by_position[hub_x, hub_y], new['material_distribution']['splitter']]:
            footprint = self.builder._occupied_by_plan([spec])
            self.assertTrue(all(not any(z['area'][0][0] <= px < z['area'][1][0]
                and z['area'][0][1] <= py < z['area'][1][1] for z in zones) for px,py in footprint))

    def test_coal_extra_off_patch_hub_connector_is_untagged_and_reserves_both_east_aprons(self):
        original = bank('coal')['plan']['ports'][0]
        path = mining_exit_candidates(original['position'])[2][:7]
        clear = path[-1]; anchor = {'x': clear['x'], 'y': clear['y']+1}
        self.game.query.return_value = {'ok': True, 'exit_prefix': path, 'exit_length': 5,
            'clear_position': clear, 'hub_connector': [anchor], 'hub_position': anchor}
        prepared = self.controller.prepare(self.obs, {'resource': 'coal'})
        self.assertTrue(prepared['ok'], prepared)
        new = prepared['bank']; plan = new['plan']
        validate_annotations(plan['entities'], self.builder)
        by_position = {xy(e): e for e in plan['entities']}
        self.assertNotIn('mining_resource', by_position[clear['x'], clear['y']])
        self.assertNotIn('mining_resource', by_position[anchor['x'], anchor['y']])
        self.assertEqual(by_position[clear['x'], clear['y']]['direction'], 8)
        self.assertEqual(by_position[anchor['x'], anchor['y']]['direction'], 4)
        exits = [e for e in plan['entities'] if e.get('mining_exit')]
        self.assertTrue(exits)
        self.assertTrue(all(e['mining_exit']['path']==path and e['mining_exit']['length']==5 for e in exits))
        self.assertEqual([xy(p) for p in new['material_distribution']['ports'].values()],
                         [(anchor['x']+2, anchor['y']), (anchor['x']+2, anchor['y']+1)])
        self.assertEqual(new['reserved_area'][1][0], anchor['x']+3.5)
        self.assertEqual(new['nominal_rate_per_minute'], 60)


if __name__=='__main__':
    unittest.main()
