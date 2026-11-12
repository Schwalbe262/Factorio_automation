from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.defense_coverage import coverage_cells, coverage_report, select_cover, native_candidate_survey
from factorio_agent.defense_perimeter import DefensePerimeter


class DefenseCoverageTests(unittest.TestCase):
    AREAS = [[[20, 0], [24, 4]]]

    def controller(self):
        c = DefensePerimeter.__new__(DefensePerimeter)
        c.path = Path('unused-defense-test') / 'defense-perimeter.json'
        c.catalog = SimpleNamespace(fingerprint='catalog')
        c._save = Mock()
        c.bootstrap = SimpleNamespace(ensure_item=Mock(side_effect=lambda obs, item, count: {
            'type': 'craft', 'recipe': item, 'count': count}))
        native = {'ok': True, 'range': 18, 'magazine_size': 10,
                  'candidates': [{'position': {'x': 10, 'y': 0}, 'local_walk_access': True}],
                  'access_scope': 'native local walk component'}
        c.game = SimpleNamespace(query=Mock(side_effect=lambda script:
            deepcopy(native) if 'attack_parameters' in script else {'ok': True, 'resources': 0}))
        c.state = {'world_id': 'w', 'catalog_fingerprint': 'catalog', 'range': 18, 'phase': 'armed',
                   'posts': [{'name': 'old', 'position': {'x': 0, 'y': 0},
                              'unit_number': 100, 'commissioned': True}]}
        c.live = {'old': {'unit_number': 100, 'ammo': 10, 'full_magazines': 10, 'ammo_rounds': 100}}
        c.held = 10

        def inspect(obs):
            posts = []
            for spec in c.state['posts']:
                row = {'name': spec['name'], 'position': spec['position'], 'health': 400, 'max_health': 400,
                       'nearby_enemies': 0, **deepcopy(c.live.get(spec['name'], {
                           'missing': True, 'ammo': 0, 'full_magazines': 0, 'ammo_rounds': 0}))}
                if spec.get('unit_number') and row.get('unit_number') != spec['unit_number'] and not row.get('missing'):
                    row['identity_changed'] = True
                posts.append(row)
            return {'ok': True, 'tick': 50, 'posts': posts, 'actor_full_magazines': c.held,
                    'missing': [p['name'] for p in posts if p.get('missing')],
                    'changed': [p['name'] for p in posts if p.get('identity_changed')],
                    'unarmed': [p['name'] for p in posts if not p.get('missing') and p['ammo_rounds'] <= 0],
                    'refill': [p['name'] for p in posts if not p.get('missing') and p['full_magazines'] < 5]}
        c.inspect = Mock(side_effect=inspect)
        obs = {'ok': True, 'world_id': 'w', 'tick': 49,
               'enabled_recipes': {'gun-turret': True, 'firearm-magazine': True},
               'inventory': {'gun-turret': 1, 'firearm-magazine': 10}}
        return c, obs

    def install(self, c, obs):
        prepared = c.prepare_coverage(obs, 'stone-1', self.AREAS)
        self.assertTrue(prepared['ok'], prepared)
        c.state['posts'].extend(prepared['new_posts'])
        c.state['coverage_scopes'] = {'stone-1': prepared['scope']}
        return prepared['new_posts'][0]['name']

    def test_cell_metric_rejects_hole_even_when_outer_corners_covered(self):
        cells = coverage_cells([[[0, 0], [20, 20]]])
        posts = [{'position': {'x': x, 'y': y}} for x, y in ((0, 0), (0, 20), (20, 0), (20, 20))]
        report = coverage_report(cells, posts, 10)
        self.assertFalse(report['complete'])
        self.assertLess(report['covered_fraction'], 1)
        self.assertIn(12, report['uncovered_cells'])

    def test_complete_cells_are_area_weighted_and_fit_one_disk(self):
        cells = coverage_cells([[[0, 0], [5, 4]]])
        self.assertEqual([c['weight'] for c in cells], [16, 4])
        report = coverage_report(cells, [{'position': {'x': 0, 'y': 2}}], 5)
        self.assertEqual(report['covered_fraction'], .8)
        self.assertFalse(report['complete'])

    def test_invalid_or_unbounded_geometry_is_rejected_before_native_io(self):
        c, obs = self.controller()
        for areas in ([], [[[0, 0], [float('nan'), 2]]], [[[0, 0], [0, 2]]], [[[0, 0], [1000, 1000]]]):
            self.assertFalse(c.prepare_coverage(obs, 'stone-1', areas)['ok'])
        self.assertFalse(c.prepare_coverage(obs, '../unsafe', self.AREAS)['ok'])
        self.assertFalse(c.prepare_coverage({**obs, 'world_id': 'other'}, 'stone-1', self.AREAS)['ok'])
        c.game.query.assert_not_called()
        with self.assertRaises(ValueError):
            coverage_cells(self.AREAS, cell_size=0)

    def test_prepare_is_readonly_and_preserves_existing_units(self):
        c, obs = self.controller()
        before = deepcopy(c.state)
        result = c.prepare_coverage(obs, 'stone-1', self.AREAS)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['requirements'], {'gun-turret': 1, 'firearm-magazine': 10})
        self.assertEqual(c.state, before)
        c._save.assert_not_called()
        c.bootstrap.ensure_item.assert_not_called()
        self.assertTrue(result['scope']['geometry']['complete'])

    def test_no_native_clear_accessible_candidate_blocks_without_spending(self):
        c, obs = self.controller()
        c.game.query.return_value = None
        c.game.query.side_effect = lambda script: {'ok': True, 'range': 18, 'magazine_size': 10, 'candidates': []}
        self.assertEqual(c.prepare_coverage(obs, 'stone-1', self.AREAS)['reason'],
                         'no_clear_accessible_complete_defense_coverage')
        c._save.assert_not_called()
        c.bootstrap.ensure_item.assert_not_called()

    def test_new_post_requires_ten_full_magazines_and_paid_turret_before_build(self):
        c, obs = self.controller()
        c.held = 9
        self.assertEqual(c.ensure_coverage(obs, 'stone-1', self.AREAS)['recipe'], 'firearm-magazine')
        self.assertEqual(c.state['posts'][0]['unit_number'], 100)
        c.held = 10
        obs['inventory']['gun-turret'] = 0
        self.assertEqual(c.ensure_coverage(obs, 'stone-1', self.AREAS)['recipe'], 'gun-turret')
        obs['inventory']['gun-turret'] = 1
        action = c.ensure_coverage(obs, 'stone-1', self.AREAS)
        self.assertEqual((action['type'], action['name'], action['position']),
                         ('build', 'gun-turret', {'x': 10, 'y': 0}))

    def test_partial_magazine_does_not_satisfy_ten_full_initial_load(self):
        c, obs = self.controller()
        name = self.install(c, obs)
        c.live[name] = {'unit_number': 201, 'ammo': 10, 'full_magazines': 9, 'ammo_rounds': 99}
        action = c.ensure_coverage(obs, 'stone-1', self.AREAS)
        self.assertEqual((action['type'], action['count'], action['expected_entity_unit']), ('insert', 1, 201))
        self.assertFalse(c.state['coverage_scopes']['stone-1']['commissioned'])
        c.live[name].update(ammo=11, full_magazines=10, ammo_rounds=109)
        self.assertIsNone(c.ensure_coverage(obs, 'stone-1', self.AREAS))
        self.assertTrue(c.coverage_proof(obs, 'stone-1')['geometry']['complete'])

    def test_commissioned_scope_uses_five_magazine_upkeep_threshold(self):
        c, obs = self.controller()
        name = self.install(c, obs)
        c.live[name] = {'unit_number': 201, 'ammo': 10, 'full_magazines': 10, 'ammo_rounds': 100}
        self.assertIsNone(c.ensure_coverage(obs, 'stone-1', self.AREAS))
        c.live[name].update(ammo=5, full_magazines=4, ammo_rounds=41)
        action=c.ensure_coverage(obs,'stone-1',self.AREAS)
        self.assertEqual((action['type'],action['count']),('insert',6))
        c.live[name].update(ammo=4, full_magazines=3, ammo_rounds=31)
        action = c.ensure_coverage(obs, 'stone-1', self.AREAS)
        self.assertEqual((action['type'], action['count']), ('insert', 7))

    def test_missing_existing_post_blocks_additive_gate_without_rebuild(self):
        c, obs = self.controller()
        name = self.install(c, obs)
        c.live[name] = {'unit_number': 201, 'ammo': 10, 'full_magazines': 10, 'ammo_rounds': 100}
        self.assertIsNone(c.ensure_coverage(obs, 'stone-1', self.AREAS))
        del c.live['old']
        action = c.ensure_coverage(obs, 'stone-1', self.AREAS)
        self.assertEqual(action['reason'], 'existing_defense_post_missing_requires_explicit_repair')
        self.assertEqual(c.state['posts'][0]['unit_number'], 100)

    def test_changed_identity_or_scope_geometry_never_adopts_replacement(self):
        c, obs = self.controller()
        name = self.install(c, obs)
        c.state['posts'][1]['unit_number'] = 201
        c.live[name] = {'unit_number': 202, 'ammo': 10, 'full_magazines': 10, 'ammo_rounds': 100}
        self.assertEqual(c.ensure_coverage(obs, 'stone-1', self.AREAS)['reason'], 'defense_post_identity_changed')
        self.assertEqual(c.state['posts'][1]['unit_number'], 201)
        self.assertEqual(c.ensure_coverage(obs, 'stone-1', [[[20, 0], [25, 4]]])['reason'],
                         'defense_coverage_scope_geometry_changed')

    def test_native_survey_checks_ores_reservations_and_walk_access_without_effects(self):
        game = SimpleNamespace(query=Mock(return_value={'ok': True}))
        native_candidate_survey(game, [{'x': 10, 'y': 0}], self.AREAS, self.AREAS, [[0, -10], [30, 20]])
        script = game.query.call_args.args[0]
        flattened = ' '.join(script.splitlines())
        self.assertNotIn('--', flattened)
        self.assertIn('s.count_entities_filtered{area=area,type="resource"}==0', flattened)
        self.assertIn('not reserved(area)', flattened)
        self.assertIn('name="character"', flattened)
        self.assertIn('reachable[key(x,y)]', flattened)
        for effect in ('.create_entity', '.insert(', '.destroy(', '.mine_entity', 'request_path'):
            self.assertNotIn(effect, flattened)

    def test_planned_site_access_is_rechecked_before_emitting_build(self):
        c, obs = self.controller()
        self.install(c, obs)
        c.game.query.side_effect = lambda script: ({'ok': True, 'candidates': []}
            if 'attack_parameters' in script else {'ok': True, 'resources': 0})
        action = c.ensure_coverage(obs, 'stone-1', self.AREAS)
        self.assertEqual(action['reason'], 'prepared_defense_coverage_site_or_access_changed')
        self.assertNotIn('type', action)

    def test_invalid_uninitialized_request_cannot_start_initial_perimeter(self):
        c, obs = self.controller()
        c.state = {}
        c.step = Mock()
        self.assertEqual(c.ensure_coverage(obs, '../unsafe', self.AREAS)['status'], 'blocked')
        c.step.assert_not_called()
        c.game.query.assert_not_called()

    def test_repeated_scope_prepare_does_not_append_or_resurvey(self):
        c, obs = self.controller()
        self.install(c, obs)
        before = deepcopy(c.state)
        c.game.query.reset_mock()
        c._save.reset_mock()
        result = c.prepare_coverage(obs, 'stone-1', self.AREAS)
        self.assertTrue(result['existing'])
        self.assertEqual(result['new_posts'], [])
        self.assertEqual(before, c.state)
        c.game.query.assert_not_called()
        c._save.assert_not_called()

    def test_proposed_posts_have_nonoverlapping_footprints(self):
        cells = coverage_cells([[[0, 0], [30, 4]]])
        candidates = [{'position': {'x': x, 'y': -2}} for x in range(31)]
        chosen = select_cover(cells, [], candidates, 10)
        self.assertIsNotNone(chosen)
        for i, post in enumerate(chosen):
            for other in chosen[i+1:]:
                self.assertGreaterEqual(abs(post['position']['x']-other['position']['x']), 3)


if __name__ == '__main__':
    unittest.main()
