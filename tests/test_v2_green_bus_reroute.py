from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.board import entity
from factorio_agent.bus_factory import identity
from factorio_agent.compact_green import compact_green_core
from factorio_agent.green_bus_reroute import (GreenBusReroute, move_green_taps, old_green_branches,
                                              partition_green_network, replace_network)
from factorio_agent.main_bus import BusMaterial, ConsumerTap, plan_main_bus
from factorio_agent.routing import expand, metrics, VECTORS


def old_bus():
    return plan_main_bus((BusMaterial('iron-plate', -11.5, (225,), 2), BusMaterial('copper-plate', -3.5, (112.5,), 2)),
                         (ConsumerTap('green-copper', 'copper-plate', -79.5, 'critical', demand_per_minute=52.5),
                          ConsumerTap('green-iron', 'iron-plate', -76.5, 'critical', demand_per_minute=192.5),
                          ConsumerTap('red-iron', 'iron-plate', -87.5, 'critical', demand_per_minute=72),
                          ConsumerTap('red-copper', 'copper-plate', -102.5, 'critical', demand_per_minute=36)),
                         north_y=-118.5, south_y=-12.5, port_x=12.5,
                         foreign_entities=[entity('transport-belt', -11.5, -47.5, 4)])


def route(vertices, ports):
    points = expand(vertices); es = []
    for i, p in enumerate(points):
        direction = ports[-1]['facing'] if i == len(points)-1 else next(d for d, v in VECTORS.items()
            if v == (points[i+1][0]-p[0], points[i+1][1]-p[1]))
        es.append(entity('transport-belt', *p, direction))
    return _plan(es, ports, route_metrics=metrics(points), underground_pairs=[], clear_obstacles=[])


def native_observation(plan, previous=None):
    old = {identity(e): e['unit_number'] for e in (previous or {}).get('entities', [])}
    es = [dict(e, unit_number=old.get(identity(e), i + 1000), energy=100, electric_network_connected=True,
               products_finished=1, belt_inventory={}) for i, e in enumerate(plan['entities'])]
    by_position = {(e['name'], e['position']['x'], e['position']['y']): e for e in es}
    for p in plan.get('underground_pairs', []):
        first, last = by_position[('underground-belt', *p['input'])], by_position[('underground-belt', *p['output'])]
        first.update(underground_pair_verified=True, underground_neighbour={'unit_number': last['unit_number']})
        last.update(underground_pair_verified=True, underground_neighbour={'unit_number': first['unit_number']})
    return {'world_id': 'w', 'tick': 100, 'entities': es, 'inventory': {}}


class GreenBusGeometryTests(unittest.TestCase):
    def test_moves_green_ports_without_changing_red_source_or_trunk_contracts(self):
        old = old_bus(); removed = {identity(e) for e in old_green_branches(old)}
        obs = {'entities': [e for e in old['entities'] if identity(e) not in removed]}
        result = move_green_taps(old, -107.5, -83.5, obs)
        self.assertTrue(result['ok'], result)
        new = result['bus']; ports = {p['name']: p for p in new['ports']}
        self.assertEqual(ports['green-copper']['position'], {'x': 12.5, 'y': -108.5})
        self.assertEqual(ports['green-iron']['position'], {'x': 12.5, 'y': -84.5})
        self.assertEqual(new['trunks'], old['trunks']); self.assertEqual(new['reservations'], old['reservations'])
        self.assertEqual(len(new['stations']), len(old['stations']))
        self.assertTrue(all(p in new['ports'] for p in old['ports'] if p['name'] not in ('green-iron', 'green-copper')))
        self.assertTrue(all(p in new['underground_pairs'] for p in old['underground_pairs'] if p['direction'] == 0))

    def test_same_geometry_with_changed_plate_type_is_cut_and_deferred(self):
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        a, common, b = entity('transport-belt', -.5, .5, 4), entity('transport-belt', .5, .5, 4), entity('transport-belt', 1.5, .5, 4)
        old = _plan([a, common], [], underground_pairs=[]); new = _plan([common, b], [], underground_pairs=[])
        result = partition_green_network(old, new, geometry, old_items={identity(common): 'copper-plate'}, new_items={identity(common): 'iron-plate'})
        self.assertTrue(result['ok'])
        self.assertNotIn(common, result['retained']); self.assertIn(common, result['conflicts'])
        self.assertIn(common, result['deferred']); self.assertIn(b, result['deferred'])
        self.assertEqual(result['prebuild'], [])

    def test_protected_bus_asset_cannot_be_recovered_for_convenience(self):
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        old = _plan([entity('transport-belt', .5, .5, 0)], [], underground_pairs=[])
        new = _plan([entity('transport-belt', .5, .5, 4)], [], underground_pairs=[])
        self.assertFalse(partition_green_network(old, new, geometry, old['entities'])['ok'])

    def test_publication_preserves_all_production_machines_poles_and_fixed_ports(self):
        old = _plan([entity('transport-belt', .5, .5, 4)], [], underground_pairs=[])
        new = _plan([entity('transport-belt', .5, 1.5, 4)], [], underground_pairs=[])
        fixed = [entity('assembling-machine-1', 10.5, 10.5, recipe='logistic-science-pack'), entity('small-electric-pole', 12.5, 10.5)]
        ports = [{'item': 'logistic-science-pack', 'direction': 'output', 'position': {'x': 14.5, 'y': 10.5}}]
        plan = _plan(old['entities'] + fixed, ports, underground_pairs=[])
        changed = replace_network(plan, old, new)
        self.assertEqual(changed['ports'], ports); self.assertTrue(all(e in changed['entities'] for e in fixed))
        self.assertEqual(changed, replace_network(changed, old, new))


class GreenBusControllerTests(unittest.TestCase):
    def setup_controller(self, root):
        bus = old_bus(); core = compact_green_core(43.5, -78.5); by_name = {p['name']: p for p in bus['ports']}
        adapters = [route([(12.5, -80.5), (40.5, -80.5)], [by_name['green-copper'], core['ports'][1]]),
                    route([(12.5, -77.5), (40.5, -77.5)], [by_name['green-iron'], core['ports'][0]])]
        full = _plan(bus['entities'] + core['entities'] + [e for r in adapters for e in r['entities']], core['ports'],
                     underground_pairs=bus['underground_pairs'] + core['underground_pairs'], reserved_area=core['reserved_area'])
        red = _plan(bus['entities'] + [entity('assembling-machine-1', 24.5, -97.5, recipe='automation-science-pack')], [],
                    underground_pairs=bus['underground_pairs'])
        shared = {'world_id': 'w', 'catalog_fingerprint': 'catalog'}
        route_metrics = [{'connection': 'green-copper-plate', 'length': 28}, {'connection': 'green-iron-plate', 'length': 28}]
        docs = {'bus-factory.json': {**shared, 'phase': 'verified', 'cleanup': [], 'remove': [], 'bus': bus,
                                    'plan': full, 'red_plan': red, 'core': core, 'route_metrics': route_metrics},
                'automation-science.json': {**shared, 'plan': red},
                'logistic-science.json': {**shared, 'plan': full, 'core': core, 'route_metrics': route_metrics},
                'pcb-board.json': {**shared, 'shared_main_bus': bus, 'blocks': {'logistic_science': full}}}
        for filename, document in docs.items(): Path(root, filename).write_text(json.dumps(document))
        builder = SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,
            ensure_plan=Mock(return_value={'status': 'succeeded'}), bootstrap=SimpleNamespace(ensure_item=Mock(return_value={'type': 'craft', 'recipe': 'splitter', 'count': 1})))
        components = [SimpleNamespace(state=deepcopy(docs[name])) for name in ('bus-factory.json', 'automation-science.json', 'logistic-science.json')]
        components[0].proof = Mock(return_value={'source_smelting': {'verified': True}, 'compact_machine_units': list(range(12))})
        c = GreenBusReroute(SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root), query=Mock(return_value={'ok': True, 'max_span': 5})),
                            builder, SimpleNamespace(fingerprint='catalog'), *components, SimpleNamespace())
        return c, native_observation(full), core

    def fake_connection(self, game, builder, obs, source, destination, *args, **kwargs):
        if source['item'] == 'copper-plate':
            vertices = [(12.5, -108.5), (50.5, -108.5), (50.5, -83.5), (39.5, -83.5), (39.5, -80.5), (40.5, -80.5)]
        else:
            vertices = [(12.5, -84.5), (13.5, -84.5), (13.5, -87.5), (18.5, -87.5), (18.5, -88.5), (37.5, -88.5), (37.5, -77.5), (40.5, -77.5)]
        return route(vertices, [source, destination])

    def prepare(self, c, obs):
        with patch('factorio_agent.green_bus_reroute.plan_connection', side_effect=self.fake_connection), \
             patch('factorio_agent.green_bus_reroute.preflight', return_value={'ok': True, 'clear_obstacles': []}):
            return c.prepare(obs, candidates=((-107.5, -83.5),))

    def test_prepare_is_read_only_and_stock_and_recoveries_are_concrete(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs, core = self.setup_controller(root)
            with patch('factorio_agent.green_bus_reroute._atomic_json') as write:
                ready = self.prepare(c, obs)
                self.assertTrue(ready['ok'], ready)
                self.assertEqual(ready['core_ports'], core['ports'])
                self.assertTrue(ready['pending_cutover']); self.assertTrue(ready['prebuild']['entities'])
                self.assertTrue(ready['pending_retirement']); self.assertEqual(ready['required_stock']['splitter'], 2)
                self.assertLessEqual(max(r['turns'] for r in ready['route_metrics'].values()), 6)
                write.assert_not_called(); c.builder.ensure_plan.assert_not_called(); c.bus_factory.proof.assert_not_called()
                self.assertFalse(c.path.exists())

    def test_failed_bounded_adapter_prevents_any_cutover_or_state_write(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs, _ = self.setup_controller(root)
            with patch('factorio_agent.green_bus_reroute.plan_connection', return_value={'ok': False, 'reason': 'no_clean_trunk_corridor'}), \
                 patch('factorio_agent.green_bus_reroute._atomic_json') as write:
                result = c.prepare(obs, candidates=((-107.5, -83.5),))
                self.assertFalse(result['ok']); write.assert_not_called(); c.builder.ensure_plan.assert_not_called()

    def test_prebuild_stock_is_collected_before_old_green_transport_is_mined(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs, _ = self.setup_controller(root); c.state = self.prepare(c, obs)
            self.assertEqual(c.step(obs)['type'], 'craft')
            c.builder.ensure_plan.assert_not_called()

    def test_recovery_is_bounded_and_does_not_target_rebuilt_coordinates(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs, _ = self.setup_controller(root); c.state = self.prepare(c, obs)
            old = c.state['pending_cutover'][0]
            obs['entities'] = [e for e in obs['entities'] if e['unit_number'] != old['unit_number']]
            c._recover(obs, 'pending_cutover')
            self.assertNotIn(old, c.state['pending_cutover'])
            obs['entities'].append(dict(old, unit_number=99999))
            action = c._recover(obs, 'pending_cutover')
            if action:
                self.assertLessEqual(len(action['actions']), 8)
                self.assertTrue(all(a['expected_entity_unit'] != 99999 and a['expected_entity_world_id'] == 'w' for a in action['actions']))

    def test_publication_is_idempotent_and_never_changes_the_twelve_machine_core(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs, core = self.setup_controller(root); c.state = self.prepare(c, obs)
            self.assertIsNone(c._publish()); self.assertIsNone(c._publish())
            docs = c._documents()
            self.assertEqual(docs['factory']['core'], core); self.assertEqual(docs['green']['core'], core)
            self.assertEqual(sum(e['name'] == 'assembling-machine-1' for e in docs['factory']['plan']['entities']), 12)
            self.assertEqual(sum(e['name'] == 'assembling-machine-1' for e in docs['green']['plan']['entities']), 12)
            self.assertEqual(sum(e['name'] == 'assembling-machine-1' for e in docs['red']['plan']['entities']), 1)
            self.assertEqual(docs['factory']['bus'], docs['board']['shared_main_bus'])
            for key in ('factory','green'):
                self.assertEqual([r['length'] for r in docs[key]['route_metrics']],[78,43])

    def test_cutover_preflight_keeps_obsolete_routes_physically_present(self):
        with tempfile.TemporaryDirectory() as root:
            c,obs,_=self.setup_controller(root)
            with patch('factorio_agent.green_bus_reroute.plan_connection',side_effect=self.fake_connection), \
                 patch('factorio_agent.green_bus_reroute.preflight',side_effect=[
                     {'ok':True,'clear_obstacles':[]},{'ok':False,'reason':'old_obsolete_route_still_collides'}]) as native:
                ready=c.prepare(obs,candidates=((-107.5,-83.5),))
                self.assertFalse(ready['ok'])
                self.assertEqual(ready['failures'][0]['stage'],'green_cutover_coexistence')
                self.assertLess(len(native.call_args.kwargs['ignored_units']),len(native.call_args_list[0].kwargs['ignored_units']))
                c.builder.ensure_plan.assert_not_called()

    def test_old_routes_cannot_retire_without_new_compound_source_and_lab_proof(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs, _ = self.setup_controller(root); c.state = self.prepare(c, obs)
            c.state.update(phase='retire', published=True)
            c.proof = Mock(return_value=None); before = deepcopy(c.state['pending_retirement'])
            result = c.step(obs)
            self.assertEqual(result['status'], 'waiting'); self.assertEqual(c.state['pending_retirement'], before)

    def test_proof_requires_live_both_fixed_raw_feeds_and_fresh_source_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            c, old_obs, core = self.setup_controller(root); c.state = self.prepare(c, old_obs); c.state['published'] = True
            final = replace_network(c._documents()['factory']['plan'], c.state['old_network'], c.state['new_network'])
            obs = native_observation(final, old_obs)
            for p in core['ports'][:2]:
                live = next(e for e in obs['entities'] if e['name'] == 'transport-belt' and e['position'] == p['position'])
                live['belt_inventory'] = {p['item']: 1}
            self.assertIsNotNone(c.proof(obs))
            c.bus_factory.proof.return_value = {'compact_machine_units': list(range(12))}
            self.assertIsNone(c.proof(obs))


if __name__ == '__main__':
    unittest.main()
