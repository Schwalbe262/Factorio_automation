from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.board import entity
from factorio_agent.paired_mining import mining_pair
from factorio_agent.resource_policy import (annotate_mining_exit, ensure_resource_policy, mining_reservations,
                                            policy_specs, resource_preflight, validate_annotations, RESOURCE_CHECK_LUA,
                                            mining_exit_candidates, exit_directions)
from factorio_agent.power_layout import V2FactoryBuilder
from factorio_agent.transport import plan_connection


class PolicyGame:
    def __init__(self, ores, native=()):
        self.ores = ores; self.native = {(e['name'], e['position']['x'], e['position']['y']): e for e in native}; self.calls = 0

    def query(self, script):
        self.calls += 1
        encoded = script.split('helpers.json_to_table(', 1)[1].split(');', 1)[0]
        args = json.loads(json.loads(encoded)); preserved = []
        for spec in args['specs']:
            area = spec['survey_area']
            ores = [name for (x, y), name in self.ores.items() if area[0][0] <= x <= area[1][0] and area[0][1] <= y <= area[1][1]]
            if not ores: continue
            if spec.get('resource_virtual_tunnel'):
                anchors=spec.get('resource_tunnel_anchors')
                if anchors and all(self.native.get((p['name'],p['position']['x'],p['position']['y']),{}).get('unit_number')==p['unit_number'] for p in anchors):
                    preserved.extend(anchors);continue
                return {'ok':False,'reason':'new_underground_tunnel_over_resource_or_mining_reservation'}
            old = self.native.get((spec['name'], spec['position']['x'], spec['position']['y']))
            if old and old['unit_number'] == spec.get('resource_preserved_unit') and old['unit_number'] not in args['ignored']:
                preserved.append({k: old[k] for k in ('name', 'position', 'unit_number', 'direction')}); continue
            if not spec.get('mining_resource'):
                return {'ok': False, 'reason': 'new_entity_over_resource_or_mining_reservation'}
            if any(name != spec['mining_resource'] for name in ores):
                return {'ok': False, 'reason': 'mining_core_resource_mismatch'}
        return {'ok': True, 'resource_grandfathered': preserved}


class ResourcePolicyTests(unittest.TestCase):
    def setUp(self):
        self.builder = FactoryBuilder.__new__(FactoryBuilder)

    def test_factory_on_ore_cannot_use_coarse_resource_cell_flag(self):
        game = PolicyGame({(.5, .5): 'coal'})
        plan = {'resource_cell': True, 'entities': [entity('assembling-machine-1', .5, .5)]}
        self.assertFalse(ensure_resource_policy(game, self.builder, {'world_id': 'w', 'tick': 1, 'entities': []}, plan)['ok'])

    def test_forbidden_types_and_unscoped_mining_tags_fail_before_query(self):
        for name in ('assembling-machine-1', 'stone-furnace', 'lab', 'splitter', 'underground-belt', 'wooden-chest'):
            game = Mock()
            result = resource_preflight(game, self.builder, {'world_id': 'w', 'entities': []},
                                        [dict(entity(name, .5, .5), mining_resource='coal', mining_footprint=[[0, 0], [4, 4]])])
            self.assertFalse(result['ok']); game.query.assert_not_called()
        with self.assertRaises(ValueError):
            validate_annotations([dict(entity('transport-belt', .5, .5), mining_resource='coal')], self.builder)

    def test_only_local_mining_entities_have_annotations_and_exact_footprints(self):
        core = mining_pair(16.5, -64.5, 2, 'iron-ore')
        validate_annotations(core['entities'], self.builder)
        self.assertTrue(all(e['mining_resource'] == 'iron-ore' for e in core['entities']))
        self.assertEqual(core['ports'][0]['mining_resource'], 'iron-ore')
        box = self.builder._occupied_by_plan(core['entities'])
        self.assertEqual(core['mining_footprint'], [[min(x for x, _ in box) - .5, min(y for _, y in box) - .5],
                                                  [max(x for x, _ in box) + .5, max(y for _, y in box) + .5]])

    def test_tagged_core_rejects_mixed_resource_patch(self):
        core = mining_pair(.5, .5, 1, 'coal')
        game = PolicyGame({(.5, .5): 'iron-ore'})
        self.assertEqual(resource_preflight(game, self.builder, {'world_id': 'w', 'entities': []}, core['entities'])['reason'],
                         'mining_core_resource_mismatch')

    def test_exact_live_old_asset_is_grandfathered_but_ignored_recovery_is_not(self):
        old = dict(entity('transport-belt', .5, .5, 4), unit_number=7)
        obs = {'world_id': 'w', 'entities': [old]}; game = PolicyGame({(.5, .5): 'coal'}, [old])
        self.assertTrue(resource_preflight(game, self.builder, obs, [entity('transport-belt', .5, .5, 4)])['ok'])
        self.assertFalse(resource_preflight(game, self.builder, obs, [entity('transport-belt', .5, .5, 4)], ignored_units=(7,))['ok'])
        self.assertFalse(resource_preflight(game, self.builder, obs, [entity('transport-belt', .5, .5, 0)])['ok'])

    def test_cache_avoids_per_action_rpc_and_lost_old_unit_cannot_be_rebuilt(self):
        old = dict(entity('transport-belt', .5, .5, 4), unit_number=7)
        game = PolicyGame({(.5, .5): 'coal'}, [old]); obs = {'world_id': 'w', 'tick': 1, 'entities': [old]}
        plan = {'entities': [entity('transport-belt', .5, .5, 4)]}
        self.assertTrue(ensure_resource_policy(game, self.builder, obs, plan)['ok'])
        self.assertTrue(ensure_resource_policy(game, self.builder, {**obs, 'tick': 2}, plan)['ok'])
        self.assertEqual(game.calls, 1)
        game.native = {}
        self.assertFalse(ensure_resource_policy(game, self.builder, {**obs, 'tick': 3, 'entities': []}, plan)['ok'])
        self.assertEqual(game.calls, 2)

    def test_v2_build_gate_blocks_before_any_ordinary_build_or_collection(self):
        builder = V2FactoryBuilder.__new__(V2FactoryBuilder); builder.game = PolicyGame({(.5, .5): 'coal'})
        with patch.object(FactoryBuilder, 'ensure_plan') as normal:
            result = builder.ensure_plan({'world_id': 'w', 'tick': 1, 'entities': []}, {'entities': [entity('lab', .5, .5)]})
            self.assertEqual(result['status'], 'blocked'); normal.assert_not_called()

    def test_straight_exit_scope_never_tags_later_trunk_or_allows_turns(self):
        es = [entity('transport-belt', i + .5, .5, 4) for i in range(8)]
        annotate_mining_exit(es, 'coal', {'x': .5, 'y': .5}, 3)
        validate_annotations(es, self.builder)
        self.assertEqual([i for i, e in enumerate(es) if 'mining_resource' in e], [0, 1, 2, 3])
        changed = deepcopy(es); changed[2]['direction'] = 8
        with self.assertRaises(ValueError): validate_annotations(changed, self.builder)
        with self.assertRaises(ValueError): annotate_mining_exit(es, 'coal', {'x': .5, 'y': .5}, 12)

    def test_whole_exit_candidates_are_east_first_then_only_one_initial_corner(self):
        candidates = mining_exit_candidates({'x': -63.5, 'y': 24.5})
        self.assertEqual([exit_directions(p) for p in candidates], [[4]*12, [4]+[0]*11, [4]+[8]*11])
        self.assertEqual(candidates[2][6], {'x': -62.5, 'y': 29.5})

    def test_south_exit_metadata_requires_all_ordered_surface_cells_and_leaves_hub_untagged(self):
        path = mining_exit_candidates({'x': -63.5, 'y': 24.5})[2][:7]
        es = [entity('transport-belt', p['x'], p['y'], d)
              for p,d in zip(path, exit_directions(path)+[4])]
        es.append(entity('transport-belt', -61.5, 29.5, 4))
        annotate_mining_exit(es, 'coal', path[0], 5, path=path)
        validate_annotations(es, self.builder)
        self.assertEqual([e['mining_exit']['step'] for e in es if e.get('mining_exit')], list(range(6)))
        self.assertNotIn('mining_resource', es[-2])
        self.assertNotIn('mining_resource', es[-1])
        for mutation in ('gap', 'underground', 'forged_position', 'forged_length', 'forged_direction'):
            broken = deepcopy(es)
            if mutation == 'gap': del broken[3]
            elif mutation == 'underground': broken[3]['name'] = 'underground-belt'
            elif mutation == 'forged_position': broken[3]['mining_exit']['path'][2]['x'] += 1
            elif mutation == 'forged_length': broken[3]['mining_exit']['length'] -= 1
            else: broken[3]['direction'] = 4
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_annotations(broken, self.builder)

    def test_exit_paths_reject_overlong_two_corner_loop_or_backtracking_metadata(self):
        bad_paths = [
            [{'x': i+.5, 'y': .5} for i in range(14)],
            [{'x': .5, 'y': .5}, {'x': 1.5, 'y': .5}, {'x': 1.5, 'y': 1.5}, {'x': 2.5, 'y': 1.5}],
            [{'x': .5, 'y': .5}, {'x': 1.5, 'y': .5}, {'x': .5, 'y': .5}],
            [{'x': .5, 'y': .5}, {'x': 1.5, 'y': .5}, {'x': 1.5, 'y': 1.5}, {'x': 1.5, 'y': .5}],
            [{'x': .5, 'y': .5}, {'x': .5, 'y': 1.5}],
        ]
        for path in bad_paths:
            with self.subTest(path=path), self.assertRaises(ValueError): exit_directions(path)

    def test_new_underground_mouths_cannot_hide_ore_under_the_buried_span(self):
        mouths=[dict(entity('underground-belt',.5,.5,4),belt_to_ground_type='input'),
                dict(entity('underground-belt',4.5,.5,4),belt_to_ground_type='output')]
        game=PolicyGame({(2.5,.5):'coal'})
        result=resource_preflight(game,self.builder,{'world_id':'w','entities':[]},mouths)
        self.assertEqual(result['reason'],'new_underground_tunnel_over_resource_or_mining_reservation')

    def test_verified_old_tunnel_is_grandfathered_until_either_exact_mouth_is_lost(self):
        mouths=[dict(entity('underground-belt',.5,.5,4),belt_to_ground_type='input',unit_number=8,
                     underground_pair_verified=True,underground_neighbour={'unit_number':9}),
                dict(entity('underground-belt',4.5,.5,4),belt_to_ground_type='output',unit_number=9,
                     underground_pair_verified=True,underground_neighbour={'unit_number':8})]
        specs=[{k:v for k,v in e.items() if k not in ('unit_number','underground_pair_verified','underground_neighbour')} for e in mouths]
        game=PolicyGame({(2.5,.5):'coal'},mouths);obs={'world_id':'w','tick':1,'entities':mouths}
        self.assertTrue(ensure_resource_policy(game,self.builder,obs,{'entities':specs})['ok'])
        self.assertTrue(ensure_resource_policy(game,self.builder,{**obs,'tick':2},{'entities':specs})['ok'])
        self.assertEqual(game.calls,1)
        game.native.pop(('underground-belt',4.5,.5))
        self.assertFalse(ensure_resource_policy(game,self.builder,{**obs,'tick':3,'entities':mouths[:1]},{'entities':specs})['ok'])

    def test_new_mouth_pairing_with_a_foreign_existing_mouth_also_checks_buried_ore(self):
        first=dict(entity('underground-belt',.5,.5,4),belt_to_ground_type='input')
        last=dict(entity('underground-belt',4.5,.5,4),belt_to_ground_type='output',unit_number=9)
        game=PolicyGame({(2.5,.5):'coal'},[last])
        self.assertFalse(resource_preflight(game,self.builder,{'world_id':'w','entities':[last]},[first])['ok'])

    def test_recovered_old_underground_mouth_is_excluded_from_new_pair_survey(self):
        first = dict(entity('underground-belt', .5, .5, 4), belt_to_ground_type='input')
        obsolete = dict(entity('underground-belt', 4.5, .5, 4), belt_to_ground_type='output', unit_number=91)
        obs = {'world_id': 'w', 'entities': [obsolete]}
        self.assertTrue(any(e.get('resource_virtual_tunnel') for e in policy_specs(self.builder, obs, [first])))
        specs = policy_specs(self.builder, obs, [first], ignored_units=[91])
        self.assertFalse(any(e.get('resource_virtual_tunnel') for e in specs))
        replacement = dict(entity('underground-belt', 6.5, .5, 4), belt_to_ground_type='output')
        specs = policy_specs(self.builder, obs, [first, replacement], ignored_units=[91])
        self.assertEqual(len([e for e in specs if e.get('resource_virtual_tunnel')]), 5)

    def test_transport_accepts_bounded_south_exit_under_unchanged_whole_route_limits(self):
        path = mining_exit_candidates({'x': .5, 'y': .5})[2][:7]
        survey = {'ok': True, 'blocked': [], 'clearable': [], 'max_span': 5, 'exit_prefix': path, 'exit_length': 5}
        game = SimpleNamespace(query=lambda _: survey)
        builder = SimpleNamespace(_occupied_by_plan=self.builder._occupied_by_plan, can_place=lambda _: {'ok': True})
        source = {'item': 'coal', 'position': path[0], 'facing': 4, 'mining_resource': 'coal'}
        destination = {'item': 'coal', 'position': {'x': 14.5, 'y': 12.5}, 'facing': 4}
        result = plan_connection(game, builder, {'world_id': 'w', 'entities': []}, source, destination)
        self.assertTrue(result['ok'], result)
        validate_annotations(result['entities'], self.builder)
        self.assertLessEqual(result['route_metrics']['turns'], 6)
        self.assertLessEqual(result['route_metrics']['length'], 1.35*26+4)

    def test_registered_mining_growth_reservations_are_loaded_without_other_factory_zones(self):
        with tempfile.TemporaryDirectory() as directory:
            board = {'world_id': 'w', 'blocks': {'mine': {'resource': 'coal', 'reserved_area': [[0, 0], [8, 8]],
                                                       'entities': [entity('electric-mining-drill', .5, .5)]},
                                               'factory': {'reserved_area': [[20, 20], [40, 40]], 'entities': []}}}
            Path(directory, 'pcb-board.json').write_text(json.dumps(board))
            game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=directory))
            self.assertEqual(mining_reservations(game, 'w'), [{'resource': 'coal', 'area': [[0, 0], [8, 8]]}])
            with self.assertRaises(ValueError): mining_reservations(game, 'other')

    def test_transport_forces_complete_mining_prefix_and_marks_only_on_patch_tiles(self):
        survey = {'ok': True, 'blocked': [], 'clearable': [], 'max_span': 5,
                  'exit_prefix': [{'x': i + .5, 'y': .5} for i in range(5)], 'exit_length': 3}
        game = SimpleNamespace(query=lambda _: survey)
        builder = SimpleNamespace(_occupied_by_plan=self.builder._occupied_by_plan, can_place=lambda _: {'ok': True})
        source = {'item': 'coal', 'position': {'x': .5, 'y': .5}, 'facing': 4, 'mining_resource': 'coal'}
        destination = {'item': 'coal', 'position': {'x': 10.5, 'y': 5.5}, 'facing': 4}
        result = plan_connection(game, builder, {'world_id': 'w', 'entities': []}, source, destination)
        self.assertTrue(result['ok'], result)
        by_position = {(e['position']['x'], e['position']['y']): e for e in result['entities']}
        for i in range(4):
            self.assertEqual(by_position[i + .5, .5]['direction'], 4)
            self.assertEqual(by_position[i + .5, .5]['mining_exit']['step'], i)
        self.assertNotIn('mining_resource', by_position[4.5, .5])

    def test_transport_native_resource_endpoint_failures_are_not_relaxed(self):
        game = SimpleNamespace(query=Mock(return_value={'ok': False, 'reason': 'corridor_destination_on_resource_or_mining_reservation'}))
        source = {'item': 'coal', 'position': {'x': .5, 'y': .5}, 'facing': 4}
        destination = {'item': 'coal', 'position': {'x': 10.5, 'y': .5}, 'facing': 4}
        self.assertFalse(plan_connection(game, None, {'world_id': 'w', 'entities': []}, source, destination)['ok'])
        source['mining_resource'] = 'iron-ore'
        self.assertEqual(plan_connection(game, None, {'world_id': 'w', 'entities': []}, source, destination)['reason'],
                         'invalid_registered_mining_exit_source')

    def test_native_guard_code_checks_nearby_drills_and_has_no_line_comments(self):
        self.assertNotIn('--', RESOURCE_CHECK_LUA)
        self.assertIn('mining_local_asset_without_nearby_drill', RESOURCE_CHECK_LUA)
        self.assertIn('old.unit_number==spec.resource_preserved_unit', RESOURCE_CHECK_LUA)


if __name__ == '__main__':
    unittest.main()
