from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.furnace_upgrade import FurnaceUpgrade
from factorio_agent.paired_smelting import PairedSmelting, paired_bank
from factorio_agent.smelting import SmeltingDistrict, FURNACE_NAMES, bank_capacities, merge_entities
from factorio_agent.transport import xy


def catalog():
    return SimpleNamespace(fingerprint='catalog',
        entities={name: {'type': 'furnace', 'crafting_speed': speed} for name, speed in
                  (('stone-furnace', 1), ('steel-furnace', 2), ('electric-furnace', 2))},
        recipes={name: {'energy': 3.2, 'products': [{'name': name, 'amount': 1}]}
                 for name in ('iron-plate', 'copper-plate')})


class FurnaceUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = catalog()
        self.banks = [paired_bank(-40, -32, 1, 'iron-ore'), paired_bank(-40, -16, 1, 'copper-ore')]
        self.plan = _plan(merge_entities(*(b['entities'] for b in self.banks)), [b['ports'][-1] for b in self.banks],
                          underground_pairs=[], reserved_area=[[-48, -40], [-10, 0]])
        self.source = SmeltingDistrict.__new__(SmeltingDistrict)
        self.source.path = self.root/'smelting-district.json'
        self.source.catalog = self.catalog
        self.source.state = {'world_id': 'w', 'catalog_fingerprint': 'catalog', 'paired': True,
                             'plan': deepcopy(self.plan), 'banks': deepcopy(self.banks),
                             'automatic_feed_baseline': {'tick': 0}}
        self.source.proof = Mock(return_value={'source': 'native'})
        self.paired = PairedSmelting.__new__(PairedSmelting)
        self.paired.path = self.root/'paired-smelting.json'
        self.paired.catalog = self.catalog
        self.paired.smelting = self.source
        self.paired.state = dict(deepcopy(self.source.state), phase='verified', baseline_products={})
        self.paired.proof = Mock(return_value={'lanes': 'native'})
        self.game = Mock()
        self.game.query.return_value = {'count': 1}
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        self.builder = SimpleNamespace(_occupied_by_plan=geometry._occupied_by_plan,
            can_place=Mock(return_value={'ok': True}), ensure_plan=Mock(return_value={'status': 'succeeded'}),
            bootstrap=SimpleNamespace(ensure_item=Mock(return_value={'type': 'craft', 'recipe': 'steel-furnace', 'count': 1})))
        self.source.builder = self.builder
        self.actual = [dict(e, unit_number=i+1, products_finished=1, energy=100,
                            electric_network_connected=True, electric_network_id=1,
                            inventory={'coal': 2}, belt_inventory={}) for i, e in enumerate(self.plan['entities'])]
        self.obs = {'world_id': 'w', 'tick': 100, 'technologies': {'advanced-material-processing': True},
                    'enabled_recipes': {'steel-furnace': True}, 'inventory': {'steel-furnace': 1}, 'entities': self.actual}
        self.request = {'resource': 'iron-ore'}
        (self.root/'pcb-board.json').write_text(json.dumps({'world_id': 'w', 'catalog_fingerprint': 'catalog',
                                                         'blocks': {'smelting_district': self.plan}}))
        (self.root/'mining-outposts.json').write_text(json.dumps({'world_id': 'w', 'banks': [
            {'ready': True, 'nominal_rate_per_minute': 600, 'plan': {'resource': resource}}
            for resource in ('iron-ore', 'copper-ore')]}))
        self.controller = self.make_controller()
        self.preflight = patch('factorio_agent.furnace_upgrade.preflight', return_value={'ok': True, 'clear_obstacles': []}).start()
        self.addCleanup(patch.stopall)

    def make_controller(self):
        controller = FurnaceUpgrade.__new__(FurnaceUpgrade)
        controller.game, controller.builder, controller.catalog = self.game, self.builder, self.catalog
        controller.smelting, controller.paired = self.source, self.paired
        controller.path = self.root/'furnace-upgrade.json'
        controller.state = json.loads(controller.path.read_text()) if controller.path.exists() else {}
        return controller

    def test_prepare_is_readonly_and_requires_natural_research_and_connected_ore_capacity(self):
        obs = dict(self.obs, technologies={})
        self.assertEqual(self.controller.prepare(obs, self.request)['reason'], 'native_advanced_material_processing_required')
        (self.root/'mining-outposts.json').write_text(json.dumps({'world_id': 'w', 'banks': []}))
        result = self.controller.prepare(self.obs, self.request)
        self.assertEqual(result['reason'], 'expand_connected_mining_before_furnace_upgrade')
        self.assertEqual(result['required_nominal_ore_rate'], 75)
        self.assertFalse(self.controller.path.exists())
        self.game.query.assert_not_called()

    def test_prepare_preflights_whole_preserved_plan_ignoring_only_exact_old_units(self):
        before = deepcopy(self.source.state)
        prepared = self.controller.prepare(self.obs, self.request)
        self.assertTrue(prepared['ok'])
        self.assertEqual(len(prepared['targets']), 2)
        wanted = {e['unit_number'] for e in self.actual if e['name'] == 'stone-furnace' and e['position']['y'] in (-32, -27)}
        self.assertEqual(set(self.preflight.call_args.kwargs['ignored_units']), wanted)
        prospective = self.preflight.call_args.args[3]
        self.assertEqual(len(prospective), len(self.plan['entities']))
        self.assertEqual(self.source.state, before)
        self.assertFalse(self.controller.path.exists())

    def test_payment_precedes_any_recovery_and_rare_aggregate_stock_is_not_accepted(self):
        self.game.query.return_value = {'count': 0}
        result = self.controller.step(self.obs, self.request)
        self.assertEqual(result['type'], 'craft')
        self.assertNotIn('pending', self.controller.state)
        self.assertEqual(self.builder.bootstrap.ensure_item.call_args.args[0]['inventory']['steel-furnace'], 0)
        self.assertTrue(all(e['name'] != 'steel-furnace' for e in self.source.state['plan']['entities']))

    def test_mine_is_exactly_guarded_and_fresh_gap_build_survives_controller_reload(self):
        mine = self.controller.step(self.obs, self.request)
        self.assertEqual(mine['type'], 'mine')
        self.assertEqual(mine['expected_entity_world_id'], 'w')
        old_unit = mine['expected_entity_unit']
        self.obs['entities'] = [e for e in self.obs['entities'] if e['unit_number'] != old_unit]
        resumed = self.make_controller()
        build = resumed.step(self.obs, self.request)
        self.assertEqual((build['type'], build['name'], build['position']), ('build', 'steel-furnace', mine['position']))
        self.assertEqual(resumed.state['pending']['unit_number'], old_unit)

    def test_pending_recovery_never_retargets_a_new_stone_unit_and_rechecks_payment(self):
        mine = self.controller.step(self.obs, self.request)
        self.game.query.return_value = {'count': 0}
        self.assertEqual(self.controller.step(self.obs, self.request)['type'], 'craft')
        self.game.query.return_value = {'count': 1}
        next(e for e in self.obs['entities'] if e['unit_number'] == mine['expected_entity_unit'])['unit_number'] += 1000
        result = self.controller.step(self.obs, self.request)
        self.assertEqual(result['reason'], 'old_furnace_identity_changed_never_retarget_replacement')

    def test_upgrade_transition_furnace_stays_excluded_from_actor_starter_fueling(self):
        mine = self.controller.step(self.obs, self.request)
        old = next(e for e in self.obs['entities'] if e['unit_number'] == mine['expected_entity_unit'])
        self.assertTrue(self.source.owns_furnace(old, self.obs))
        self.assertTrue(self.paired.owns_furnace(old, self.obs))

    def test_partial_target_300_upgrades_four_balanced_furnaces_and_preserves_eight_stone(self):
        banks = [paired_bank(-40, -32, 6, 'iron-ore'), paired_bank(-40, -16, 1, 'copper-ore')]
        plan = _plan(merge_entities(*(b['entities'] for b in banks)), [b['ports'][-1] for b in banks],
                     underground_pairs=[], reserved_area=[[-48, -40], [-10, 0]])
        self.source.state.update(plan=deepcopy(plan), banks=deepcopy(banks))
        self.paired.state.update(plan=deepcopy(plan), banks=deepcopy(banks))
        (self.root/'pcb-board.json').write_text(json.dumps({'world_id': 'w', 'catalog_fingerprint': 'catalog',
                                                         'blocks': {'smelting_district': deepcopy(plan)}}))
        self.obs['entities'] = [dict(e, unit_number=i+1, products_finished=1, energy=100,
            electric_network_connected=True, electric_network_id=1, inventory={'coal': 2}, belt_inventory={})
            for i, e in enumerate(plan['entities'])]
        mining = {'world_id': 'w', 'banks': [{'ready': True, 'nominal_rate_per_minute': 300,
                  'plan': {'resource': 'iron-ore'}}]}
        (self.root/'mining-outposts.json').write_text(json.dumps(mining))
        self.request = {'resource': 'iron-ore', 'rate_per_minute': 300}
        prepared = self.controller.prepare(self.obs, self.request)
        self.assertTrue(prepared['ok'])
        self.assertEqual(prepared['target_nominal_rate'], 300)
        self.assertEqual(len(prepared['targets']), 4)
        self.assertEqual([e['position']['y'] for e in prepared['targets']].count(-32), 2)
        self.assertEqual([e['position']['y'] for e in prepared['targets']].count(-27), 2)
        self.assertEqual(len(self.preflight.call_args.kwargs['ignored_units']), 4)
        result = self.complete_upgrade()
        self.assertEqual(result['evidence']['requested_rate_per_minute'], 300)
        iron = next(b for b in self.source.state['banks'] if b['resource'] == 'iron-ore')
        names = [e['name'] for e in iron['entities'] if e['name'] in FURNACE_NAMES]
        self.assertEqual(names.count('steel-furnace'), 4)
        self.assertEqual(names.count('stone-furnace'), 8)
        self.assertEqual(self.source.state['plan']['ports'], plan['ports'])
        mining['banks'][0]['nominal_rate_per_minute'] = 360
        (self.root/'mining-outposts.json').write_text(json.dumps(mining))
        next_target = self.controller.prepare(self.obs, {'resource': 'iron-ore', 'rate_per_minute': 360})
        self.assertTrue(next_target['ok'])
        self.assertEqual(len(next_target['targets']), 4)
        self.assertEqual(next_target['target_nominal_rate'], 375)
        self.assertFalse({xy(e) for e in prepared['targets']} & {xy(e) for e in next_target['targets']})

    def test_invalid_optional_rates_fail_before_planning_or_native_io(self):
        for rate in (None, True, 0, -1, float('inf'), float('nan')):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                self.controller.step(self.obs, {'resource': 'iron-ore', 'rate_per_minute': rate})
        self.game.query.assert_not_called()

    def complete_upgrade(self):
        for index in range(12):
            result = self.controller.step(self.obs, self.request)
            if result.get('status') == 'succeeded':
                return result
            if result.get('type') == 'mine':
                self.obs['entities'] = [e for e in self.obs['entities'] if e['unit_number'] != result['expected_entity_unit']]
            elif result.get('type') == 'build':
                self.obs['entities'].append(dict(result, unit_number=1000+index, products_finished=1,
                    inventory={'coal': 2}, energy=100, electric_network_connected=True))
            else:
                self.fail(result)
        self.fail('upgrade did not finish')

    def test_completed_upgrade_synchronizes_all_authorities_and_preserves_ports_and_nonfurnace_assets(self):
        result = self.complete_upgrade()
        self.assertEqual(result['evidence']['nominal_capacity_per_minute'], 75)
        source = json.loads(self.source.path.read_text())
        paired = json.loads(self.paired.path.read_text())
        board = json.loads((self.root/'pcb-board.json').read_text())
        self.assertEqual(source['plan']['ports'], self.plan['ports'])
        before = [e for e in self.plan['entities'] if e['name'] not in FURNACE_NAMES]
        self.assertEqual([e for e in source['plan']['entities'] if e['name'] not in FURNACE_NAMES], before)
        self.assertEqual(paired['plan'], source['plan'])
        self.assertEqual(paired['banks'], source['banks'])
        self.assertEqual(board['blocks']['smelting_district'], source['plan'])
        iron = next(b for b in source['banks'] if b['resource'] == 'iron-ore')
        self.assertTrue(all(e['name'] == 'steel-furnace' for row in iron['producer_rows'].values() for e in row))

    def test_later_capacity_request_adds_steel_and_never_reconstructs_stone(self):
        self.complete_upgrade()
        result = self.source.capacity_step(self.obs, {'resource': 'iron-ore', 'rate_per_minute': 450})
        self.assertEqual(result['status'], 'succeeded')
        iron = next(b for b in self.source.state['banks'] if b['resource'] == 'iron-ore')
        names = [e['name'] for e in iron['entities'] if e['name'] in FURNACE_NAMES]
        self.assertEqual(names, ['steel-furnace']*12)
        self.assertEqual(bank_capacities(self.catalog, [iron]), {'iron-ore': 450})

    def test_ports_changed_by_another_job_are_not_overwritten_from_a_stale_upgrade(self):
        self.controller.step(self.obs, self.request)
        changed = json.loads(self.source.path.read_text())
        changed['plan']['ports'][0]['position']['x'] += 1
        self.source.path.write_text(json.dumps(changed))
        result = self.controller.step(self.obs, self.request)
        self.assertEqual(result['reason'], 'smelting_ports_or_infrastructure_changed_during_upgrade')
        self.assertEqual(json.loads(self.source.path.read_text())['plan']['ports'], changed['plan']['ports'])

    def test_generalized_smelting_proof_requires_each_steel_furnace_production_and_real_fuel(self):
        self.complete_upgrade()
        self.source.game = Mock()
        self.source.game.query.return_value = {'ok': True, 'banks': [
            {'resource': b['resource'], 'lanes': [{b['resource']: 1}, {'coal': 1}]} for b in self.source.state['banks']]}
        for live in self.obs['entities']:
            live['electric_network_id'] = 1
            if live['name'] == 'transport-belt':
                live['belt_inventory'] = {'iron-plate': 1, 'copper-plate': 1}
        self.assertIsNotNone(SmeltingDistrict.proof(self.source, self.obs))
        steel = next(e for e in self.obs['entities'] if e['name'] == 'steel-furnace')
        steel['products_finished'] = 0
        self.assertIsNone(SmeltingDistrict.proof(self.source, self.obs))
        steel['products_finished'] = 1; steel['inventory'] = {}
        self.assertIsNone(SmeltingDistrict.proof(self.source, self.obs))


    def consumer_documents(self):
        documents = {}
        for filename in ('bus-factory.json', 'logistic-science.json'):
            doc = {'world_id': 'w', 'catalog_fingerprint': 'catalog', 'phase': 'verified',
                   'source_plan': dict(deepcopy(self.plan), consumer_note=filename),
                   'plan': {'entities': [{'name': 'transport-belt', 'position': {'x': 900, 'y': 900}, 'direction': 4}]},
                   'route_metrics': {'tiles': 24}, 'proof': {'tick': 50}, 'unrelated': [1, 2, 3]}
            (self.root/filename).write_text(json.dumps(doc))
            documents[filename] = doc
        return documents

    def test_consumer_source_copies_update_without_changing_factory_routes_or_metadata(self):
        before = self.consumer_documents()
        self.complete_upgrade()
        source = json.loads(self.source.path.read_text())
        for filename, original in before.items():
            with self.subTest(filename=filename):
                after = json.loads((self.root/filename).read_text())
                self.assertEqual({k: v for k, v in after.items() if k != 'source_plan'},
                                 {k: v for k, v in original.items() if k != 'source_plan'})
                self.assertEqual(after['source_plan']['entities'], source['plan']['entities'])
                self.assertEqual(after['source_plan']['ports'], original['source_plan']['ports'])
                self.assertEqual(after['source_plan']['underground_pairs'], original['source_plan']['underground_pairs'])
                self.assertEqual(after['source_plan']['consumer_note'], filename)

    def test_changed_consumer_identity_blocks_before_any_authority_write_or_mine(self):
        docs = self.consumer_documents()
        docs['bus-factory.json']['world_id'] = 'replacement-world'
        (self.root/'bus-factory.json').write_text(json.dumps(docs['bus-factory.json']))
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        result = self.controller.step(self.obs, self.request)
        self.assertEqual(result['reason'], 'furnace_upgrade_publication_document_identity_changed')
        for filename, content in before.items():
            self.assertEqual((self.root/filename).read_bytes(), content)
        self.assertFalse(self.source.path.exists())
        self.assertFalse(self.paired.path.exists())

    def test_changed_consumer_route_revision_blocks_instead_of_overwriting(self):
        docs = self.consumer_documents()
        docs['logistic-science.json']['source_plan']['entities'][0]['position']['x'] += 1
        path = self.root/'logistic-science.json'
        path.write_text(json.dumps(docs['logistic-science.json']))
        original = path.read_bytes()
        result = self.controller.step(self.obs, self.request)
        self.assertEqual(result['reason'], 'furnace_upgrade_publication_revision_changed')
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(self.source.path.exists())

    def test_partial_publication_recovers_each_old_or_new_copy_after_restart(self):
        from factorio_ai.deterministic_state import _atomic_json as write
        self.consumer_documents()
        failures = [True]

        def interrupt(path, value):
            if Path(path).name == 'logistic-science.json' and failures:
                failures.pop()
                raise OSError('simulated process interruption before final atomic file write')
            return write(path, value)

        with patch('factorio_agent.furnace_upgrade._atomic_json', side_effect=interrupt):
            with self.assertRaises(OSError):
                self.controller.step(self.obs, self.request)
        saved = json.loads(self.controller.path.read_text())
        self.assertIn('publication', saved)
        self.assertTrue(any(e['name'] == 'steel-furnace' for e in json.loads(self.source.path.read_text())['plan']['entities']))
        self.assertFalse(any(e['name'] == 'steel-furnace' for e in json.loads((self.root/'logistic-science.json').read_text())['source_plan']['entities']))
        resumed = self.make_controller()
        action = resumed.step(self.obs, self.request)
        self.assertEqual(action['type'], 'mine')
        self.assertNotIn('publication', resumed.state)
        source = json.loads(self.source.path.read_text())
        for filename in ('bus-factory.json', 'logistic-science.json'):
            self.assertEqual(json.loads((self.root/filename).read_text())['source_plan']['entities'], source['plan']['entities'])
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with patch('factorio_agent.furnace_upgrade._atomic_json') as atomic:
            self.assertIsNone(resumed._publish(xy(resumed.state['pending'])))
            atomic.assert_not_called()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_absent_consumer_files_are_not_manufactured(self):
        self.complete_upgrade()
        self.assertFalse((self.root/'bus-factory.json').exists())
        self.assertFalse((self.root/'logistic-science.json').exists())


if __name__ == '__main__':
    unittest.main()
