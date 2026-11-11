from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.board import entity
from factorio_agent.bus_tap import BusTap, extend_bus, reconstruct_bus, replace_bus, signature, validate_parameters
from factorio_agent.main_bus import BusMaterial, ConsumerTap, plan_main_bus


def parameters():
    return {'name': 'steel-iron', 'item': 'iron-plate', 'splitter_y': -51.5, 'priority': 'surplus', 'demand_per_minute': 37.5}


def bus():
    return plan_main_bus((BusMaterial('iron-plate', -11.5, (225,), 2), BusMaterial('copper-plate', -3.5, (112.5,), 2)),
                         (ConsumerTap('green-copper', 'copper-plate', -79.5, 'critical', demand_per_minute=52.5),
                          ConsumerTap('green-iron', 'iron-plate', -76.5, 'critical', demand_per_minute=192.5),
                          ConsumerTap('red-iron', 'iron-plate', -87.5, 'critical', demand_per_minute=72),
                          ConsumerTap('red-copper', 'copper-plate', -102.5, 'critical', demand_per_minute=36)),
                         north_y=-118.5, south_y=-12.5, port_x=12.5,
                         foreign_entities=[entity('transport-belt', -11.5, -47.5, 4)])


def observation(plan, previous=None):
    previous = {signature(e): e for e in (previous or {}).get('entities', [])}
    rows = []
    for i, spec in enumerate(plan['entities']):
        live = dict(spec, unit_number=previous.get(signature(spec), {}).get('unit_number', i + 1000), belt_inventory={})
        if spec['name'] == 'underground-belt': live['underground_pair_verified'] = True
        rows.append(live)
    by_position = {(e['name'], e['position']['x'], e['position']['y']): e for e in rows}
    for pair in plan['underground_pairs']:
        a = by_position[('underground-belt', *pair['input'])]; b = by_position[('underground-belt', *pair['output'])]
        a['underground_neighbour'] = {'unit_number': b['unit_number']}; b['underground_neighbour'] = {'unit_number': a['unit_number']}
    return {'world_id': 'w', 'tick': 100, 'entities': rows, 'inventory': {}}


class BusTapGeometryTests(unittest.TestCase):
    def test_metadata_reconstruction_retains_supply_and_parallel_reservations(self):
        materials, taps, merges, north, south, east = reconstruct_bus(bus())
        self.assertEqual([(m.item, m.source_rates, m.reserved_lines) for m in materials],
                         [('iron-plate', (225,), 2), ('copper-plate', (112.5,), 2)])
        self.assertEqual((len(taps), len(merges), north, south, east), (4, 0, -118.5, -12.5, 12.5))
        self.assertEqual(sum(t.demand_per_minute for t in taps if t.item == 'iron-plate'), 264.5)

    def test_only_one_owned_straight_trunk_tile_changes(self):
        old = bus(); result = extend_bus(old, parameters(), observation(old))
        self.assertTrue(result['ok'], result)
        new = result['bus']; removed = {signature(e) for e in old['entities']} - {signature(e) for e in new['entities']}
        self.assertEqual(removed, {signature(result['remove_spec'])})
        self.assertEqual(result['remove_spec']['position'], {'x': -11.5, 'y': -51.5})
        self.assertEqual(result['tap_port']['position'], {'x': 12.5, 'y': -52.5})
        self.assertTrue(all(p in new['ports'] for p in old['ports']))
        self.assertTrue(all(p in new['underground_pairs'] for p in old['underground_pairs']))
        self.assertEqual(new['capacity']['materials']['iron-plate']['lines'][0]['declared_source_per_minute'], 225)
        self.assertEqual(new['capacity']['materials']['iron-plate']['lines'][0]['declared_demand_per_minute'], 302)

    def test_old_tunnels_remain_even_if_the_crossed_foreign_belt_was_retired(self):
        old = bus(); result = extend_bus(old, parameters(), observation(old))
        self.assertTrue(result['ok'])
        self.assertTrue(all(p in result['bus']['underground_pairs'] for p in old['underground_pairs']))

    def test_native_splitter_uses_surplus_priority_and_no_new_producer(self):
        old = bus(); result = extend_bus(old, parameters(), observation(old))
        station = next(s for s in result['bus']['stations'] if s['name'] == 'steel-iron')
        self.assertEqual((station['splitter']['splitter_input_priority'], station['splitter']['splitter_output_priority']), ('left', 'left'))
        self.assertEqual({e['name'] for e in result['added_entities']}, {'transport-belt', 'underground-belt', 'splitter'})

    def test_parameter_and_metadata_incompatibilities_fail_closed(self):
        for change in ({'demand_per_minute': float('nan')}, {'line': True}, {'splitter_y': -51}, {'priority': 'magic'}):
            with self.assertRaises(ValueError): validate_parameters({**parameters(), **change})
        old = bus(); old['flow_direction'] = 4
        with self.assertRaises(ValueError): reconstruct_bus(old)
        old = bus()
        self.assertFalse(extend_bus(old, {**parameters(), 'line': 1}, observation(old))['ok'])

    def test_plan_publication_never_adds_machines_or_changes_production_ports(self):
        old = bus(); new = extend_bus(old, parameters(), observation(old))['bus']
        machines = [entity('assembling-machine-1', 50.5 + i * 4, -60.5, recipe='logistic-science-pack') for i in range(12)]
        ports = [{'item': 'logistic-science-pack', 'position': {'x': 69.5, 'y': -59.5}, 'direction': 'output'}]
        plan = _plan(old['entities'] + machines, ports, underground_pairs=old['underground_pairs'])
        changed = replace_bus(plan, old, new)
        self.assertEqual([e for e in changed['entities'] if e['name'] == 'assembling-machine-1'], machines)
        self.assertEqual(changed['ports'], ports)
        self.assertEqual(changed, replace_bus(changed, old, new))


class BusTapControllerTests(unittest.TestCase):
    def setup_controller(self, root):
        old = bus(); obs = observation(old)
        production = _plan(old['entities'] + [entity('assembling-machine-1', 55.5 + i * 4, -60.5,
                            recipe='logistic-science-pack') for i in range(12)], [], underground_pairs=old['underground_pairs'])
        shared = {'world_id': 'w', 'catalog_fingerprint': 'catalog'}
        documents = {'bus-factory.json': {**shared, 'phase': 'verified', 'remove': [], 'cleanup': [], 'bus': old,
                                         'plan': production, 'red_plan': production},
                     'automation-science.json': {**shared, 'plan': production},
                     'logistic-science.json': {**shared, 'plan': production},
                     'pcb-board.json': {**shared, 'shared_main_bus': old, 'blocks': {'logistic_science': production}}}
        for filename, document in documents.items(): Path(root, filename).write_text(json.dumps(document))
        builder = SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,
                                  ensure_plan=Mock(return_value={'status': 'succeeded'}),
                                  bootstrap=SimpleNamespace(ensure_item=Mock(return_value={'type': 'craft', 'recipe': 'splitter', 'count': 1})))
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root), query=Mock(return_value={'ok': True, 'max_span': 5}))
        return BusTap(game, builder, SimpleNamespace(fingerprint='catalog')), obs

    def test_prepare_is_read_only_and_ignores_only_the_exact_owned_target_unit(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs = self.setup_controller(root)
            with patch('factorio_agent.bus_tap.preflight', return_value={'ok': True, 'clear_obstacles': []}) as native, \
                 patch('factorio_agent.bus_tap._atomic_json') as write:
                ready = c.prepare(obs, parameters())
                self.assertTrue(ready['ok'], ready)
                self.assertEqual(native.call_args.kwargs['ignored_units'], (ready['remove']['unit_number'],))
                self.assertEqual(native.call_args.args[3], ready['added_entities'])
                self.assertEqual(len(ready['retained_units']), len(c._documents()['factory']['bus']['entities']) - 1)
                write.assert_not_called(); c.builder.ensure_plan.assert_not_called()
                self.assertFalse(c.path.exists())

    def test_ore_under_new_underground_span_blocks_before_any_cost_or_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs = self.setup_controller(root)
            c.game.query.side_effect = [{'ok': True, 'max_span': 5},
                                       {'ok': False, 'reason': 'tap_new_corridor_crosses_resource_patch',
                                        'position': {'x': -7.5, 'y': -52.5}}]
            with patch('factorio_agent.bus_tap.preflight') as native:
                result = c.prepare(obs, parameters())
                self.assertEqual(result['reason'], 'tap_new_corridor_crosses_resource_patch')
                native.assert_not_called(); c.builder.ensure_plan.assert_not_called()
                resource_script = c.game.query.call_args.args[0]
                encoded = resource_script.split('helpers.json_to_table(', 1)[1].split(')\nfor', 1)[0]
                tiles = json.loads(json.loads(encoded))
                self.assertIn({'x': -7.5, 'y': -52.5}, tiles)
                self.assertIn({'x': -3.5, 'y': -52.5}, tiles)

    def test_build_cannot_repair_a_lost_grandfathered_old_bus_unit(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs = self.setup_controller(root)
            with patch('factorio_agent.bus_tap.preflight', return_value={'ok': True, 'clear_obstacles': []}):
                c.state = c.prepare(obs, parameters())
            c.state['phase'] = 'build'; lost = c.state['retained_units'][0]['unit_number']
            obs['entities'] = [e for e in obs['entities'] if e['unit_number'] != lost]
            self.assertEqual(c.step(obs, parameters())['reason'], 'tap_retained_native_unit_changed_during_build')
            c.builder.ensure_plan.assert_not_called()

    def test_missing_metadata_is_an_explicit_block_without_native_actions(self):
        with tempfile.TemporaryDirectory() as root:
            game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root), query=Mock())
            c = BusTap(game, None, SimpleNamespace(fingerprint='catalog'))
            self.assertEqual(c.prepare({'world_id': 'w'}, parameters())['reason'], 'tap_saved_bus_missing_or_incompatible')
            game.query.assert_not_called()

    def test_all_paid_assets_are_stocked_before_the_one_guarded_mine(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs = self.setup_controller(root)
            with patch('factorio_agent.bus_tap.preflight', return_value={'ok': True, 'clear_obstacles': []}):
                first = c.step(obs, parameters())
                self.assertEqual(first['type'], 'craft')
                obs['inventory'] = c.state['required_stock'].copy()
                result = c.step(obs, parameters())
                self.assertEqual(result['type'], 'mine')
                self.assertEqual(result['expected_entity_unit'], c.state['remove']['unit_number'])
                self.assertEqual(result['expected_entity_world_id'], 'w')

    def test_recovery_rechecks_stock_and_remaining_native_identities(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs = self.setup_controller(root)
            with patch('factorio_agent.bus_tap.preflight', return_value={'ok': True, 'clear_obstacles': []}):
                c.state = c.prepare(obs, parameters()); c.state['phase'] = 'recover'
                self.assertEqual(c.step(obs, parameters())['type'], 'craft')
                obs['inventory'] = c.state['required_stock'].copy(); obs['entities'][0]['unit_number'] += 999
                self.assertEqual(c.step(obs, parameters())['reason'], 'tap_retained_native_unit_changed_before_cutover')

    def test_completed_recovery_is_popped_before_building_same_coordinates(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs = self.setup_controller(root)
            with patch('factorio_agent.bus_tap.preflight', return_value={'ok': True, 'clear_obstacles': []}):
                c.state = c.prepare(obs, parameters())
            c.state['phase'] = 'recover'; target = c.state['remove']['unit_number']
            obs['entities'] = [e for e in obs['entities'] if e['unit_number'] != target]
            def build(*_):
                self.assertIsNone(c.state['remove']); self.assertEqual(c.state['phase'], 'build')
                return {'type': 'build', 'name': 'splitter'}
            c.builder.ensure_plan.side_effect = build
            self.assertEqual(c.step(obs, parameters())['type'], 'build')

    def test_publication_updates_all_copies_and_is_restart_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs = self.setup_controller(root)
            with patch('factorio_agent.bus_tap.preflight', return_value={'ok': True, 'clear_obstacles': []}):
                c.state = c.prepare(obs, parameters())
            self.assertIsNone(c._publish()); self.assertIsNone(c._publish())
            docs = c._documents()
            self.assertEqual(docs['factory']['bus'], c.state['new_bus'])
            self.assertEqual(docs['board']['shared_main_bus'], c.state['new_bus'])
            for plan in (docs['factory']['plan'], docs['factory']['red_plan'], docs['red']['plan'], docs['green']['plan'],
                         docs['board']['blocks']['logistic_science']):
                self.assertEqual(sum(e['name'] == 'assembling-machine-1' for e in plan['entities']), 12)
                self.assertTrue(all(e in plan['entities'] for e in c.state['new_bus']['entities']))

    def test_proof_requires_native_priorities_unchanged_units_and_typed_output(self):
        with tempfile.TemporaryDirectory() as root:
            c, old_obs = self.setup_controller(root)
            with patch('factorio_agent.bus_tap.preflight', return_value={'ok': True, 'clear_obstacles': []}):
                c.state = c.prepare(old_obs, parameters())
            obs = observation(c.state['new_bus'], old_obs)
            port = next(p for p in c.state['new_bus']['ports'] if p['name'] == 'steel-iron')
            output = next(e for e in obs['entities'] if e['name'] == 'transport-belt' and e['position'] == port['position'])
            self.assertIsNone(c.proof(obs)); output['belt_inventory'] = {'iron-plate': 1}
            proof = c.proof(obs)
            self.assertIsNotNone(proof)
            self.assertEqual(proof['port']['priority'], 'surplus')
            self.assertIn('unit_number', proof['port']['priority_splitter'])
            output['belt_inventory'] = {'copper-plate': 1}; self.assertIsNone(c.proof(obs))


if __name__ == '__main__':
    unittest.main()
