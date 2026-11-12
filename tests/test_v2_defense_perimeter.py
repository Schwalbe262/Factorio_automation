from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.defense_perimeter import DefensePerimeter, POSTS, FUTURE_AREAS


class DefensePerimeterTests(unittest.TestCase):
    def perimeter(self, *, ammo=10, held=10, missing=False, commissioned=True):
        c = DefensePerimeter.__new__(DefensePerimeter)
        c.path = Path('unused-defense-test') / 'defense-perimeter.json'
        c.catalog = SimpleNamespace(fingerprint='catalog')
        c.game = SimpleNamespace(query=Mock(return_value={'ok': True, 'resources': 0}))
        c.bootstrap = SimpleNamespace(ensure_item=Mock(side_effect=lambda obs, item, count: {'type': 'craft', 'recipe': item, 'count': count}))
        c._save = Mock()
        spec = {'name': 'west_smelter', 'position': {'x': -50, 'y': -26}, 'commissioned': commissioned}
        if not missing:
            spec['unit_number'] = 100
        c.state = {'schema_version': 1, 'world_id': 'w', 'catalog_fingerprint': 'catalog',
                   'range': 18, 'posts': [spec], 'phase': 'deploy'}
        post = {'name': 'west_smelter', 'position': spec['position'], 'unit_number': 100,
                'ammo': ammo, 'full_magazines': ammo, 'ammo_rounds': ammo * 10, 'health': 400, 'max_health': 400,
                'nearby_enemies': 0}
        if missing:
            post.update(missing=True, ammo=0, full_magazines=0, ammo_rounds=0)
        report = {'ok': True, 'tick': 55, 'posts': [post], 'missing': ['west_smelter'] if missing else [],
                  'unarmed': ['west_smelter'] if not missing and ammo == 0 else [],
                  'refill': ['west_smelter'] if not missing and ammo < 5 else [],
                  'changed': [], 'damaged': [], 'actor_full_magazines': held, 'actor_partial_magazines': 0}
        c.inspect = Mock(return_value=report)
        obs = {'ok': True, 'world_id': 'w', 'tick': 50, 'inventory': {'firearm-magazine': held, 'gun-turret': 1}}
        return c, obs, report

    def test_other_jobs_do_not_start_or_construct_an_uninitialized_perimeter(self):
        c, obs, _ = self.perimeter()
        c.state = {}
        self.assertIsNone(c.upkeep(obs))
        c.inspect.assert_not_called()
        c.bootstrap.ensure_item.assert_not_called()
        c.game.query.assert_not_called()

    def test_repeated_normal_losses_retain_prior_provenance_with_bounded_history(self):
        c,_,report=self.perimeter();spec=c.state['posts'][0]
        spec.update(lost_unit_number=90,lost_unit_numbers=[80])
        report['posts'][0]['missing']=True;c._bind(report)
        self.assertEqual(spec['lost_unit_numbers'],[80,90,100])
        spec.update(unit_number=101,commissioned=True);c._bind(report)
        self.assertEqual(spec['lost_unit_numbers'],[80,90,100,101])
        for unit in range(102,122):spec['unit_number']=unit;c._bind(report)
        self.assertEqual(len(spec['lost_unit_numbers']),16)
        self.assertEqual(spec['lost_unit_numbers'][-2:],[120,121])
        self.assertEqual(spec['lost_unit_origin'],80)

    def test_proactive_deployment_starts_with_western_producers_before_labs(self):
        names = [name for name, _, _ in POSTS]
        self.assertEqual(names[:3], ['west_smelter', 'north_smelter', 'coal_south'])
        self.assertLess(names.index('main_bus_middle'), names.index('labs_north'))
        self.assertEqual(len(names), 8)

    def test_prepare_is_read_only_and_does_not_require_current_enemies(self):
        with tempfile.TemporaryDirectory() as directory:
            game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=directory), query=Mock(return_value={
                'ok': True, 'range': 18, 'posts': [{'name': name, 'anchor': {'x': anchor[0], 'y': anchor[1]},
                                                  'focus': {'x': focus[0], 'y': focus[1]}, 'position': {'x': i * 4, 'y': -20}}
                                                 for i, (name, anchor, focus) in enumerate(POSTS)]}))
            c = DefensePerimeter(game, None, SimpleNamespace(fingerprint='catalog'))
            obs = {'world_id': 'w', 'tick': 1, 'enabled_recipes': {'gun-turret': True, 'firearm-magazine': True}}
            with patch('factorio_agent.defense_perimeter._atomic_json') as write:
                result = c.prepare(obs)
                write.assert_not_called()
            self.assertTrue(result['ok'])
            self.assertEqual(result['requirements'], {'gun-turret': 8, 'firearm-magazine': 80})
            self.assertEqual(result['reserved_areas'], list(FUTURE_AREAS))
            self.assertFalse(c.path.exists())
            self.assertEqual(c.state, {})

    def test_prepare_requires_native_recipes_before_query_or_cost(self):
        c, _, _ = self.perimeter()
        c.state = {}
        self.assertEqual(c.prepare({'world_id': 'w', 'enabled_recipes': {}})['reason'],
                         'native_gun_turret_and_normal_magazine_recipes_required')
        c.game.query.assert_not_called()

    def test_changed_world_blocks_status_without_native_effects(self):
        c, obs, _ = self.perimeter()
        del c.inspect
        self.assertEqual(c.inspect({**obs, 'world_id': 'other'})['reason'], 'defense_perimeter_world_or_catalog_changed')
        c.game.query.assert_not_called()

    def test_site_failure_is_reported_and_never_spends_materials(self):
        c, obs, _ = self.perimeter(missing=True)
        c.game.query.return_value = {'ok': False, 'resources': 0}
        result = c.step(obs)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'prepared_defense_site_changed')
        c.bootstrap.ensure_item.assert_not_called()

    def test_new_post_requires_ten_full_normal_magazines_before_build(self):
        c, obs, _ = self.perimeter(missing=True, held=9)
        action = c.step(obs)
        self.assertEqual(action, {'type': 'craft', 'recipe': 'firearm-magazine', 'count': 10})
        c.game.query.assert_not_called()

    def test_new_post_requires_normally_crafted_turret_item(self):
        c, obs, _ = self.perimeter(missing=True)
        obs['inventory']['gun-turret'] = 0
        self.assertEqual(c.step(obs)['recipe'], 'gun-turret')

    def test_paid_stock_emits_normal_build_at_native_checked_fixed_post(self):
        c, obs, _ = self.perimeter(missing=True)
        action = c.step(obs)
        self.assertEqual((action['type'], action['name'], action['position']),
                         ('build', 'gun-turret', {'x': -50, 'y': -26}))
        c.game.query.assert_called_once()
        c.bootstrap.ensure_item.assert_not_called()

    def test_initial_loading_reaches_ten_even_if_refill_threshold_is_already_met(self):
        c, obs, _ = self.perimeter(ammo=7, commissioned=False)
        action = c.step(obs)
        self.assertEqual((action['type'], action['count']), ('insert', 3))
        self.assertEqual(action['expected_entity_unit'], 100)
        self.assertEqual(action['expected_entity_world_id'], 'w')
        self.assertEqual((action['item'], action['inventory']), ('firearm-magazine', 'turret_ammo'))

    def test_upkeep_refills_below_five_to_ten(self):
        c, obs, _ = self.perimeter(ammo=4)
        action = c.upkeep(obs)
        self.assertEqual((action['type'], action['count']), ('insert', 6))
        self.assertEqual(action['expected_entity_unit'], 100)

    def test_upkeep_leaves_five_or_more_magazines_for_other_job_progress(self):
        for ammo in (5, 9, 10):
            c, obs, _ = self.perimeter(ammo=ammo)
            self.assertIsNone(c.upkeep(obs))
            c.bootstrap.ensure_item.assert_not_called()

    def test_four_full_plus_one_partial_does_not_deadlock_radar_arming(self):
        c,obs,report=self.perimeter(ammo=5)
        report['posts'][0].update(full_magazines=4,ammo_rounds=48)
        action=c.upkeep(obs)
        self.assertEqual((action['type'],action['count']),('insert',6))
        self.assertEqual(action['expected_entity_unit'],100)

    def test_missing_native_full_count_never_infers_five_full_from_item_count(self):
        c,obs,report=self.perimeter(ammo=5)
        report['posts'][0].pop('full_magazines')
        self.assertEqual(c.upkeep(obs)['reason'],'native_full_magazine_count_required')
        c.bootstrap.ensure_item.assert_not_called()

    def test_partial_or_other_quality_actor_ammo_cannot_make_crafting_report_success(self):
        c, obs, report = self.perimeter(ammo=4, held=0)
        obs['inventory']['firearm-magazine'] = 200
        report['actor_partial_magazines'] = 1
        action = c.upkeep(obs)
        self.assertEqual(action['recipe'], 'firearm-magazine')
        called_obs, item, needed = c.bootstrap.ensure_item.call_args.args
        self.assertEqual(called_obs['inventory']['firearm-magazine'], 0)
        self.assertEqual((item, needed), ('firearm-magazine', 6))
        self.assertEqual(obs['inventory']['firearm-magazine'], 200)

    def test_insert_is_bounded_by_native_full_actor_stock(self):
        c, obs, _ = self.perimeter(ammo=0, held=2)
        self.assertEqual(c.upkeep(obs)['count'], 2)

    def test_replaced_post_identity_is_never_refilled_or_built_over(self):
        c, obs, report = self.perimeter(ammo=0)
        report['changed'] = ['west_smelter']
        report['posts'][0]['identity_changed'] = True
        self.assertEqual(c.upkeep(obs)['reason'], 'defense_post_identity_changed')
        self.assertEqual(c.step(obs)['reason'], 'defense_post_identity_changed')
        c.bootstrap.ensure_item.assert_not_called()
        c.game.query.assert_not_called()

    def test_destroyed_known_post_can_be_rebuilt_normally_by_explicit_step(self):
        c, obs, report = self.perimeter(missing=True)
        c.state['posts'][0]['unit_number'] = 99
        self.assertEqual(c.step(obs)['type'], 'build')
        self.assertEqual(c.state['posts'][0]['lost_unit_number'], 99)
        self.assertNotIn('unit_number', c.state['posts'][0])
        self.assertFalse(c.state['posts'][0]['commissioned'])

    def test_other_jobs_do_not_build_the_pending_missing_posts(self):
        c, obs, _ = self.perimeter(missing=True)
        self.assertIsNone(c.upkeep(obs))
        c.game.query.assert_not_called()

    def test_commissioning_proof_needs_every_declared_post_and_initial_load(self):
        c, obs, report = self.perimeter()
        evidence = c.proof(obs)
        self.assertEqual(evidence['native_range'], 18)
        self.assertIn('no automatic-ammo or combat-stress certification', evidence['scope'])
        for key in ('missing', 'changed', 'unarmed', 'refill'):
            changed = deepcopy(report)
            changed[key] = ['west_smelter']
            self.assertIsNone(c._proof(changed))
        c.state['posts'][0]['commissioned'] = False
        self.assertIsNone(c.proof(obs))

    def test_inspect_exposes_native_missing_unarmed_damage_and_identity_status(self):
        c, obs, _ = self.perimeter()
        del c.inspect
        c.game.query.return_value = {'ok': True, 'tick': 99, 'actor_full_magazines': 9,
                                    'actor_partial_magazines': 1, 'posts': [
                                        {'name': 'west_smelter', 'ammo': 0, 'ammo_rounds': 0,
                                         'health': 150, 'max_health': 400, 'identity_changed': True,
                                         'nearby_enemies': 2},
                                        {'name': 'lost', 'missing': True, 'ammo': 0, 'ammo_rounds': 0}]}
        report = c.inspect(obs)
        self.assertEqual(report['missing'], ['lost'])
        self.assertEqual(report['unarmed'], ['west_smelter'])
        self.assertEqual(report['refill'], ['west_smelter'])
        self.assertEqual(report['damaged'], ['west_smelter'])
        self.assertEqual(report['changed'], ['west_smelter'])
        self.assertEqual(report['posts'][0]['nearby_enemies'], 2)
        c._save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
