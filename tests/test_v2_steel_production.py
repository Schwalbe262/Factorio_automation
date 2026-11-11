from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.board import entity
from factorio_agent.steel_production import SteelProduction, steel_rates, steel_core, audit_steel_core


def catalog():
    return SimpleNamespace(fingerprint='catalog', recipes={'steel-plate': {
        'energy': 16, 'categories': ['smelting'], 'ingredients': [{'name': 'iron-plate', 'type': 'item', 'amount': 5}],
        'products': [{'name': 'steel-plate', 'type': 'item', 'amount': 1}]}},
        entities={'stone-furnace': {'burner': True, 'crafting_speed': 1, 'crafting_categories': ['smelting'], 'energy_usage': 1500}},
        items={'coal': {'fuel_value': 4000000}})


def sources():
    return ({'item': 'iron-plate', 'direction': 'output', 'priority': 'surplus', 'facing': 4, 'unit_number': 1,
             'position': {'x': -20.5, 'y': -40.5}, 'priority_splitter': {'position': {'x': -21, 'y': -39.5}, 'unit_number': 3}},
            {'item': 'coal', 'direction': 'output', 'facing': 4, 'unit_number': 2, 'position': {'x': -20.5, 'y': -36.5}})


class SteelCoreTests(unittest.TestCase):
    def test_two_stone_furnaces_have_catalog_rated_balances(self):
        rates = steel_rates(catalog())
        self.assertEqual(rates['steel_per_minute'], 7.5)
        self.assertEqual(rates['iron_per_minute'], 37.5)
        self.assertEqual(rates['coal_per_minute_at_max_catalog_usage'], 2.7)
        self.assertEqual(rates['burner_watts'], 180000)

    def test_recipe_yield_time_machine_speed_and_fuel_are_not_hardcoded(self):
        c = catalog(); c.recipes['steel-plate']['energy'] = 8; c.recipes['steel-plate']['products'][0]['amount'] = 2
        c.recipes['steel-plate']['ingredients'][0]['amount'] = 4
        c.entities['stone-furnace'].update(crafting_speed=2, energy_usage=1000)
        c.items['coal']['fuel_value'] = 8000000
        rates = steel_rates(c)
        self.assertEqual((rates['steel_per_minute'], rates['iron_per_minute'], rates['coal_per_minute_at_max_catalog_usage']), (60, 120, .9))

    def test_unsupported_recipes_and_invalid_fuel_fail_explicitly(self):
        for mutate in (lambda c: c.recipes['steel-plate']['products'][0].update(probability=.5),
                       lambda c: c.recipes['steel-plate']['ingredients'][0].update(type='fluid'),
                       lambda c: c.items['coal'].update(fuel_value=0),
                       lambda c: c.entities['stone-furnace'].update(crafting_categories=['crafting'])):
            c = catalog(); mutate(c)
            with self.assertRaises(ValueError): steel_rates(c)

    def test_opposing_pair_has_no_overlap_and_both_automatic_inputs(self):
        plan = steel_core(catalog())
        audit = audit_steel_core(plan, catalog())
        self.assertTrue(audit['ok'], audit)
        self.assertEqual((audit['furnaces'], audit['automatic_inputs'], audit['opposite_lane_outputs']), (2, 2, 2))
        self.assertEqual(plan['required_items'], {'stone-furnace': 2, 'inserter': 4, 'small-electric-pole': 2,
                                                  'transport-belt': 33, 'splitter': 1})
        self.assertEqual(plan['input_lane_items'], {'left': 'iron-plate', 'right': 'coal'})
        self.assertFalse(any('recipe' in e for e in plan['entities'] if e['name'] == 'stone-furnace'))

    def test_expansion_preserves_every_old_entity_and_all_fixed_ports(self):
        first = steel_core(catalog())
        for count in (2, 3):
            expanded = steel_core(catalog(), per_side_count=count)
            self.assertTrue(audit_steel_core(expanded, catalog())['ok'])
            self.assertEqual(expanded['ports'], first['ports'])
            self.assertEqual(expanded['reserved_area'], first['reserved_area'])
            self.assertTrue(all(e in expanded['entities'] for e in first['entities']))

    def test_broken_feed_and_wrong_output_side_are_detected(self):
        plan = steel_core(catalog())
        plan['entities'] = [e for e in plan['entities'] if e['position'] != {'x': 78.5, 'y': -43.5}]
        self.assertFalse(audit_steel_core(plan, catalog())['ok'])
        plan = steel_core(catalog()); plan['output_inserters'][1]['lane'] = 'right'
        self.assertIn('output_not_native_opposite_lane', audit_steel_core(plan, catalog())['errors'])

    def test_only_active_furnace_pairs_are_built(self):
        plan = steel_core(catalog())
        self.assertEqual(sum(e['name'] == 'stone-furnace' for e in plan['entities']), 2)
        self.assertEqual(plan['reserved_pairs'], 3)
        self.assertEqual(plan['ports'][-1]['position'], {'x': 92.5, 'y': -37.5})


class SteelControllerTests(unittest.TestCase):
    def coordinator(self, root):
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root), query=Mock(return_value={'ok': True}))
        builder = SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,
                                  ensure_plan=Mock())
        return SteelProduction(game, builder, catalog())

    def obs(self):
        return {'world_id': 'w', 'tick': 100, 'technologies': {'steel-processing': True},
                'enabled_recipes': {'steel-plate': True}, 'entities': []}

    def test_native_research_gate_has_no_ordinary_cost_or_query(self):
        c = self.coordinator('unused')
        self.assertEqual(c.prepare({'technologies': {}})['reason'], 'native_steel_processing_required')
        c.game.query.assert_not_called(); c.builder.ensure_plan.assert_not_called()

    def test_running_bus_without_explicit_surplus_handoff_is_not_modified(self):
        c = self.coordinator('unused')
        self.assertEqual(c.prepare(self.obs())['reason'], 'steel_requires_installed_surplus_main_bus_tap')
        iron, coal = sources(); iron['priority'] = 'critical'
        self.assertEqual(c.prepare(self.obs(), iron_source=iron, coal_source=coal)['reason'], 'steel_requires_installed_surplus_main_bus_tap')
        c.game.query.assert_not_called()

    def test_inexact_or_untyped_coal_source_fails_before_query(self):
        c = self.coordinator('unused'); iron, coal = sources()
        coal['item'] = 'iron-ore'
        self.assertEqual(c.prepare(self.obs(), iron_source=iron, coal_source=coal)['reason'], 'steel_requires_installed_typed_coal_output')
        iron, coal = sources(); coal['unit_number'] = True
        self.assertEqual(c.prepare(self.obs(), iron_source=iron, coal_source=coal)['reason'], 'steel_source_exact_native_identities_required')
        c.game.query.assert_not_called()

    def test_whole_prepare_is_read_only_and_returns_reviewable_plan(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'pcb-board.json').write_text(json.dumps({'world_id': 'w', 'catalog_fingerprint': 'catalog', 'blocks': {}}))
            c = self.coordinator(root); iron, coal = sources()
            route = _plan([], [], underground_pairs=[], clear_obstacles=[], route_metrics={'turns': 2, 'length': 10})
            with patch('factorio_agent.steel_production.survey_slots', return_value={'ok': True, 'legal': [1]}), \
                 patch('factorio_agent.steel_production.plan_connection', return_value=route), \
                 patch('factorio_agent.steel_production.connect_power_groups', return_value={'ok': True, 'entities': []}), \
                 patch('factorio_agent.steel_production.preflight', return_value={'ok': True, 'clear_obstacles': []}), \
                 patch('factorio_agent.steel_production._atomic_json') as write:
                prepared = c.prepare(self.obs(), iron_source=iron, coal_source=coal, anchors=((82, -40),))
                self.assertTrue(prepared['ok'], prepared)
                self.assertTrue(prepared['audit']['ok'])
                self.assertEqual(len(prepared['route_metrics']), 2)
                self.assertEqual(c.state, {})
                write.assert_not_called(); c.builder.ensure_plan.assert_not_called()
                self.assertFalse(c.path.exists())

    def proof_fixture(self):
        c = self.coordinator('unused'); core = steel_core(catalog()); iron, coal = sources()
        extra = [entity('transport-belt', *iron['position'].values(), 4), entity('transport-belt', *coal['position'].values(), 4),
                 dict(entity('splitter', -21, -39.5, 0), splitter_input_priority='left', splitter_output_priority='left')]
        c.state = {'world_id': 'w', 'catalog_fingerprint': 'catalog', 'core': core,
                   'plan': _plan(core['entities'], core['ports'], underground_pairs=[]),
                   'sources': [iron, coal], 'surplus_splitter': iron['priority_splitter']}
        obs = self.obs()
        for i, spec in enumerate(core['entities'] + extra):
            live = dict(spec, unit_number=i + 100, energy=100, electric_network_connected=True)
            if spec['name'] == 'stone-furnace': live.update(recipe='steel-plate', products_finished=1, inventory={'coal': 1})
            if spec in extra[:2]:
                live['unit_number'] = 1 if spec == extra[0] else 2
                live['belt_inventory'] = {'iron-plate': 1} if spec == extra[0] else {'coal': 1}
            if spec == extra[-1]: live['unit_number'] = 3
            obs['entities'].append(live)
        c.game.query.return_value = {'ok': True, 'tick': 100, 'left': {'steel-plate': 1}, 'right': {'steel-plate': 1},
                                    'input_left': {'iron-plate': 1}, 'input_right': {'coal': 1}}
        return c, obs

    def test_commissioning_requires_both_native_lanes_and_live_automatic_furnaces(self):
        c, obs = self.proof_fixture()
        self.assertIsNotNone(c.proof(obs))
        c.game.query.return_value['right'] = {}
        self.assertIsNone(c.proof(obs))
        c, obs = self.proof_fixture(); obs['entities'][0]['products_finished'] = 0
        self.assertIsNone(c.proof(obs))
        c, obs = self.proof_fixture(); obs['entities'][0]['inventory'] = {}
        self.assertIsNone(c.proof(obs))

    def test_commissioning_rejects_changed_surplus_policy_and_contaminated_coal(self):
        c, obs = self.proof_fixture(); obs['entities'][-1]['splitter_output_priority'] = 'right'
        self.assertIsNone(c.proof(obs))
        c, obs = self.proof_fixture(); obs['entities'][-2]['belt_inventory'] = {'iron-ore': 1}
        self.assertIsNone(c.proof(obs))


if __name__ == '__main__':
    unittest.main()
