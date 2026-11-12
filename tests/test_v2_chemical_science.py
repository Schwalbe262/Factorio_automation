from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.board import entity
from factorio_agent.chemical_science import (ChemicalScience, PRODUCT, BLUE_SCIENCE_PROOF_LUA,
    _key, chemical_science_core, chemical_science_rates, audit_chemical_science_core, blue_lab_extension)
from factorio_agent.science import lab_row
from factorio_agent.smelting import port
from factorio_agent.transport import xy

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


def catalog():
    return SimpleNamespace(fingerprint='c', entities={'assembling-machine-1': {'crafting_speed': .5, 'crafting_categories': ['crafting']}},
        recipes={PRODUCT: {'energy': 24, 'category': 'crafting', 'products': [{'name': PRODUCT, 'type': 'item', 'amount': 2}],
            'ingredients': [{'name': n, 'type': 'item', 'amount': a} for n, a in (('engine-unit', 2), ('advanced-circuit', 3), ('sulfur', 1))]}})


class BlueCoreTests(unittest.TestCase):
    def test_actual_catalog_balance_and_all_future_fixed_port_cells(self):
        c = catalog(); r = chemical_science_rates(c)
        self.assertEqual((r['science_per_minute'], r['engine_per_minute'], r['advanced_circuits_per_minute'], r['sulfur_per_minute']), (5, 5, 7.5, 2.5))
        small = chemical_science_core(c, .5, .5)
        for count in (2, 3, 4):
            p = chemical_science_core(c, .5, .5, count); a = audit_chemical_science_core(p, c)
            self.assertTrue(a['ok'], a['errors']); self.assertEqual(a['machine_count'], count)
            self.assertEqual(p['ports'], small['ports']); self.assertEqual(p['reserved_area'], [[-4, 0], [27, 13]])
            self.assertTrue(all(e in p['entities'] for e in small['entities'])); self.assertEqual(p['underground_pairs'], [])
            self.assertFalse(any('pickup_position' in e or 'drop_position' in e for e in p['entities']))
        self.assertEqual(chemical_science_rates(c, 4)['science_per_minute'], 10)

    def test_changed_category_or_overloaded_native_recipe_is_rejected(self):
        c = catalog(); c.recipes[PRODUCT]['category'] = 'chemistry'
        with self.assertRaises(ValueError): chemical_science_rates(c)
        c = catalog(); c.recipes[PRODUCT]['ingredients'][0]['amount'] = 100
        with self.assertRaises(ValueError): chemical_science_rates(c)

    def test_missing_lane_wrong_long_arm_and_future_collision_fail_static_audit(self):
        c = catalog(); p = chemical_science_core(c, .5, .5)
        p['entities'] = [e for e in p['entities'] if xy(e) != (.5, 4.5)]
        self.assertFalse(audit_chemical_science_core(p, c)['ok'])
        p = chemical_science_core(c, .5, .5)
        next(e for e in p['entities'] if e['name'] == 'long-handed-inserter')['name'] = 'inserter'
        self.assertFalse(audit_chemical_science_core(p, c)['ok'])
        p = chemical_science_core(c, .5, .5); p['entities'].append(entity('small-electric-pole', 15.5, 6.5))
        self.assertIn('future_blue_slot_occupied', audit_chemical_science_core(p, c)['errors'])

    def test_missing_empty_tail_reveals_native_both_lane_engine_corner(self):
        c = catalog(); p = chemical_science_core(c, .5, .5)
        p['entities'] = [e for e in p['entities'] if xy(e) != (-1.5, 3.5)]
        self.assertTrue(any(s.startswith('blue_mixed_lane_mismatch') for s in audit_chemical_science_core(p, c)['errors']))

    def test_south_header_long_arms_skip_copper_row_and_preserve_all_original_labs(self):
        labs = lab_row(17.5, -111.5); before = deepcopy(labs); new = blue_lab_extension(labs)
        self.assertEqual(labs, before); self.assertEqual(xy(new['outside_port']), (15.5, -107.5))
        self.assertEqual(xy(new['registered_port']), (16.5, -107.5))
        self.assertEqual(sum(e['name'] == 'long-handed-inserter' for e in new['entities']), 8)
        self.assertTrue(all(e['direction'] == 8 for e in new['entities'] if e['name'] == 'long-handed-inserter'))
        self.assertFalse(any(xy(e)[1] == -108.5 for e in new['entities']))
        self.assertEqual(new['original_entities'], before['entities'])
        self.assertEqual(max(xy(e)[0] for e in new['entities'] if e['name'] == 'transport-belt'), 45.5)


class BlueControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.cat = catalog(); self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root), query=Mock(return_value={'ok': True, 'pole_index': 1}))
        self.builder = SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,
            ensure_plan=Mock(return_value={'type': 'craft', 'recipe': 'assembling-machine-1', 'count': 1}))
        self.perim = SimpleNamespace(prepare_coverage=Mock(return_value={'ok': True}), ensure_coverage=Mock(return_value=None), coverage_proof=Mock(return_value={'complete': True}))
        self.labs = lab_row(17.5, -111.5)
        self.science = SimpleNamespace(state={'world_id': 'w', 'catalog_fingerprint': 'c', 'labs': self.labs, 'plan': deepcopy(self.labs)})
        self.copper = [entity('transport-belt', 16.5+i, -108.5, 4) for i in range(30)]
        self.board = {'world_id': 'w', 'catalog_fingerprint': 'c', 'blocks': {'laboratories': self.labs,
            'green': {'reserved_area': [[39, -82], [71, -54]], 'entities': self.copper}}}
        (self.root/'pcb-board.json').write_text(json.dumps(self.board)); (self.root/'automation-science.json').write_text(json.dumps(self.science.state))
        self.sources = {}
        self.components = {}
        for index, item in enumerate(('engine-unit', 'advanced-circuit', 'sulfur')):
            source = dict(port(item, 190.5+index*5, -50.5, 4, direction='output'), name='transport-belt', unit_number=100+index, world_id='w')
            self.sources[item] = source
            plan = _plan([entity('transport-belt', *xy(source), 4)], [source])
            state = {'phase': 'verified', 'world_id': 'w', 'catalog_fingerprint': 'c', 'plan': plan,
                     'core': {'reserved_area': [[114, -115], [145, -96]] if item == 'engine-unit' else [[48, -120], [79, -98]]}}
            if item == 'sulfur': state = {'world_id': 'w', 'catalog_fingerprint': 'c', 'products': {'sulfur': {'phase': 'verified', 'plan': plan}}}
            self.components[item] = SimpleNamespace(state=state, proof=Mock(return_value={'source' if item == 'sulfur' else 'output': source}))
        self.obs = {'ok': True, 'world_id': 'w', 'tick': 10, 'technologies': {PRODUCT: True},
            'enabled_recipes': {name: True for name in (PRODUCT, 'assembling-machine-1', 'inserter', 'long-handed-inserter')},
            'entities': [dict(e, unit_number=1000+i) for i, e in enumerate(self.labs['entities']+self.copper)]
                + [dict(e, direction=4) for e in self.sources.values()]}
        self.c = ChemicalScience(self.game, self.builder, self.cat, self.perim, engine=self.components['engine-unit'],
            advanced=self.components['advanced-circuit'], chemical=self.components['sulfur'], science=self.science)
        self.land = patch('factorio_agent.chemical_science.survey_slots', return_value={'ok': True, 'legal': [1]}).start()
        self.preflight = patch('factorio_agent.chemical_science.preflight', return_value={'ok': True, 'clear_obstacles': []}).start()
        self.route = patch('factorio_agent.chemical_science.plan_connection', side_effect=lambda game, builder, obs, source, dest, reserved:
            _plan([entity('transport-belt', *xy(dest), dest['facing'])], [], underground_pairs=[], route_metrics={'length': 10, 'turns': 1})).start()
        self.power = patch('factorio_agent.chemical_science.connect_power_groups', return_value={'ok': True, 'entities': []}).start()
        self.addCleanup(patch.stopall)

    def install(self):
        self.c.state = self.c.prepare(self.obs); self.assertTrue(self.c.state['ok'], self.c.state)
        actual = {(e['name'], *xy(e)) for e in self.obs['entities']}
        self.obs['entities'] += [dict(e, unit_number=5000+i) for i, e in enumerate(self.c.state['plan']['entities']) if (e['name'], *xy(e)) not in actual]
        self.assertIsNone(self.c._bind(self.obs))

    def test_readonly_four_routes_whole_future_preflight_and_exact_lab_apron(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with patch('factorio_agent.chemical_science._atomic_json') as write:
            result = self.c.prepare(self.obs); write.assert_not_called()
        self.assertTrue(result['ok'], result); self.assertEqual(self.route.call_count, 4); self.assertEqual(self.preflight.call_count, 2)
        self.assertEqual(self.c.state, {}); self.assertFalse(self.c.path.exists())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        self.assertEqual(self.route.call_args.args[4]['position'], {'x': 15.5, 'y': -107.5})
        self.assertEqual(result['lab_interface']['apron'], [{'x': 15.5, 'y': -107.5}, {'x': 16.5, 'y': -107.5}])
        self.assertEqual(len([e for e in result['retained_lab_units'] if e['name'] == 'lab']), 8)
        self.assertEqual(result['sources'], self.sources); self.builder.ensure_plan.assert_not_called()

    def test_missing_natural_technology_or_each_uncommissioned_source_gates_all_cost(self):
        self.assertEqual(self.c.prepare({**self.obs, 'technologies': {}})['reason'], 'native_chemical_science_technology_and_recipes_required')
        for item in self.sources:
            state = self.components[item].state; entry = state['products']['sulfur'] if item == 'sulfur' else state
            entry['phase'] = 'build'; result = self.c.prepare(self.obs)
            self.assertEqual(result['reason'], 'commissioned_automatic_blue_ingredient_producer_required'); self.assertEqual(result['item'], item)
            entry['phase'] = 'verified'
        self.assertFalse(self.c.path.exists()); self.assertEqual(self.route.call_count, 0)

    def test_identical_copper_belt_on_blue_header_is_rejected_despite_can_place_success(self):
        self.obs['entities'].append(dict(entity('transport-belt', 17.5, -107.5, 4), unit_number=999, belt_inventory={'copper-plate': 8}))
        result = self.c.prepare(self.obs)
        self.assertEqual(result['reason'], 'new_blue_lab_interface_occupied_by_unowned_existing_asset'); self.assertEqual(self.route.call_count, 0)

    def test_unbuilt_external_tunnel_or_approach_halo_blocks_lab_interface(self):
        self.board['blocks']['future'] = {'reserved_area': [[200, 0], [210, 10]],
            'entities': [entity('transport-belt', 14.5, -107.5, 4)]}
        (self.root/'pcb-board.json').write_text(json.dumps(self.board))
        self.assertEqual(self.c.prepare(self.obs)['reason'], 'blue_lab_interface_conflicts_with_unbuilt_external_hardware')
        self.assertEqual(self.route.call_count, 0)

    def test_whole_future_core_area_avoids_unbuilt_external_hardware(self):
        self.board['blocks']['future'] = {'reserved_area': [[300, 0], [310, 10]],
            'entities': [entity('transport-belt', 166.5, -97.5, 4)]}
        (self.root/'pcb-board.json').write_text(json.dumps(self.board))
        result = self.c.prepare(self.obs); self.assertTrue(result['ok'], result)
        self.assertNotEqual(result['core']['reserved_area'], [[147, -104], [178, -91]])

    def test_single_twelve_route_budget_is_shared_across_all_candidate_cores(self):
        self.land.return_value = {'ok': True, 'legal': list(range(1, 7))}
        def route(game, builder, obs, source, dest, reserved):
            if source['item'] == 'sulfur': return {'ok': False, 'reason': 'no_clean_trunk_corridor', 'visited': 400}
            return _plan([], [], underground_pairs=[], route_metrics={'length': 10, 'turns': 1})
        self.route.side_effect = route
        result = self.c.prepare(self.obs)
        self.assertFalse(result['ok']); self.assertEqual(result['route_attempts'], 12); self.assertEqual(self.route.call_count, 12)

    def test_route_failure_keeps_precise_bounded_evidence_and_no_failed_checkpoint(self):
        self.route.side_effect = lambda *args: {'ok': False, 'reason': 'no_clean_trunk_corridor', 'visited': 321, 'path': ['huge']*100}
        result = self.c.step(self.obs)
        self.assertEqual(result['status'], 'blocked'); self.assertEqual(result['evidence']['failures'][0]['detail']['visited'], 321)
        self.assertNotIn('"path"', json.dumps(result)); self.assertFalse(self.c.path.exists())

    def test_original_units_and_protected_post_gate_before_new_factory_construction(self):
        self.install(); self.perim.ensure_coverage.return_value = {'type': 'craft', 'recipe': 'firearm-magazine', 'count': 10}
        self.assertEqual(self.c.step(self.obs)['recipe'], 'firearm-magazine'); self.builder.ensure_plan.assert_not_called()
        self.perim.ensure_coverage.return_value = None; self.preflight.return_value = {'ok': False, 'reason': 'turret_on_feed'}
        self.assertEqual(self.c.step(self.obs)['reason'], 'blue_site_changed_after_defense'); self.builder.ensure_plan.assert_not_called()
        lab = next(e for e in self.obs['entities'] if e['name'] == 'lab'); lab['unit_number'] += 1
        self.assertEqual(self.c.step(self.obs)['reason'], 'blue_original_lab_asset_missing_or_replaced')

    def test_bound_producer_plan_and_source_unit_change_block_before_builder(self):
        self.install(); plan = self.components['engine-unit'].state['plan']; plan['changed'] = True
        self.assertEqual(self.c.step(self.obs)['reason'], 'blue_bound_producer_plan_changed'); del plan['changed']
        source = next(e for e in self.obs['entities'] if e.get('unit_number') == 100); source['unit_number'] += 500
        self.assertEqual(self.c.step(self.obs)['reason'], 'blue_bound_ingredient_outlet_changed'); self.builder.ensure_plan.assert_not_called()

    def test_only_port_metadata_is_published_idempotently_not_blue_entities_in_green_lab_view(self):
        self.install(); before = deepcopy(self.science.state)
        self.assertIsNone(self.c._publish_lab_port(self.obs)); self.assertIsNone(self.c._publish_lab_port(self.obs))
        self.assertEqual(self.science.state['labs']['entities'], before['labs']['entities'])
        self.assertEqual(self.science.state['plan'], before['plan'])
        self.assertEqual(self.science.state['labs']['ports'], before['labs']['ports']+[self.c.state['lab_interface']['registered_port']])
        board = json.loads((self.root/'pcb-board.json').read_text())
        self.assertEqual(board['blocks']['laboratories'], self.science.state['labs'])
        self.assertEqual(board['blocks']['green']['entities'], self.copper)

    @unittest.skipUnless(LuaRuntime, 'optional executable Lua runtime')
    def test_actual_lua_three_inputs_power_normal_quality_long_lab_arms_and_all_eight_stocks(self):
        self.install(); lua = LuaRuntime(unpack_returned_tuples=True)
        def table(value):
            if isinstance(value, dict): return lua.table_from({k: table(v) for k, v in value.items()})
            if isinstance(value, list): return lua.table_from([table(v) for v in value])
            return value
        def decode(value):
            if not hasattr(value, 'items'): return value
            pairs = dict(value.items())
            if pairs and all(type(k) is int for k in pairs) and set(pairs) == set(range(1, len(pairs)+1)):
                return [decode(pairs[i]) for i in range(1, len(pairs)+1)]
            return {k: decode(v) for k, v in pairs.items()}
        lua.globals().helpers = lua.table_from({'json_to_table': lambda raw: table(json.loads(raw))})
        lua.execute('''f={};s={};game={tick=20};defines={inventory={crafter_input=1,lab_input=2}};entities={}
powered=true;wrong_recipe=false;wrong_quality=false;foreign_item=false;wrong_lane=false;empty_output=false;last_lab_empty=false
function k(q)return q.name..":"..q.position.x..":"..q.position.y end
function target(p,name)return entities[name..":"..p.x..":"..p.y]end
function belt(q,items)
 q.force=f;q.surface=s;q.get_max_transport_line_index=function()return 2 end
 q.get_transport_line=function(index)return{get_contents=function()
  if empty_output and q.is_outlet then return{}end
  local result={};for _,name in ipairs(items[wrong_lane and 3-index or index]or{})do
   result[#result+1]={name=foreign_item and"copper-plate"or name,quality=wrong_quality and"uncommon"or"normal",count=1}end
  return result end}end;entities[k(q)]=q;return q
end
function machine(q)
 q.force=f;q.surface=s;q.energy=100;q.products_finished=1;q.is_connected_to_electric_network=function()return powered end
 q.get_recipe=function()return{name=wrong_recipe and"electronic-circuit"or"chemical-science-pack"}end
 q.get_inventory=function()return{{valid_for_read=true,name=foreign_item and"copper-plate"or"engine-unit",quality={name=wrong_quality and"uncommon"or"normal"}}}end
 q.get_output_inventory=function()return{{valid_for_read=true,name="chemical-science-pack",quality={name="normal"}}}end
 entities[k(q)]=q;return q
end
function arm(q)
 q.force=f;q.surface=s;q.energy=100;q.is_connected_to_electric_network=function()return powered end
 q.drop_position={x=q.drop_position.x,y=q.drop_position.y+.2};q.held_stack={valid_for_read=false};entities[k(q)]=q;return q
end
function lab(q,index)
 q.force=f;q.surface=s;q.energy=100;q.is_connected_to_electric_network=function()return powered end
 q.get_inventory=function()return{
  {valid_for_read=true,name="automation-science-pack",count=2,quality={name="normal"}},
  {valid_for_read=true,name="logistic-science-pack",count=2,quality={name="normal"}},
  {valid_for_read=true,name="chemical-science-pack",count=last_lab_empty and index==8 and 0 or 2,quality={name=wrong_quality and"uncommon"or"normal"}}}end
 entities[k(q)]=q;return q
end''')
        state = self.c.state; core = state['core']; audit = state['audit']; units = state['bindings']
        def bound(e): return dict(e, unit_number=units[_key(e)])
        for e in core['machines']: lua.globals().machine(table(bound(e)))
        for e in audit['arm_targets']: lua.globals().arm(table(bound(e)))
        for e in audit['belt_contracts']:
            items = e['allowed_items']; lua.globals().belt(table(bound(e)), table([items, items]))
        for m in core['machines']:
            point = xy(m)[0], xy(m)[1]-3
            e = next(e for e in audit['belt_contracts'] if xy(e) == point)
            lua.globals().belt(table(bound(e)), table([['engine-unit'], ['advanced-circuit']]))
        for e in state['lab_interface']['entities']:
            if e['name'] == 'transport-belt': lua.globals().belt(table(bound(e)), table([[PRODUCT], [PRODUCT]]))
            else:
                q = dict(bound(e), pickup_position={'x': xy(e)[0], 'y': xy(e)[1]+2}, drop_position={'x': xy(e)[0], 'y': xy(e)[1]-2})
                lua.globals().arm(table(q))
        for source in state['sources'].values():
            lua.globals().belt(table(dict(source, name='transport-belt', direction=source['facing'])), table([[source['item']], [source['item']]]))
        outlet = dict(core['ports'][-1], name='transport-belt')
        lua.globals().belt(table(dict(bound(outlet), direction=12, is_outlet=True)), table([[PRODUCT], [PRODUCT]]))
        for i, q in enumerate(e for e in state['retained_lab_units'] if e['name'] == 'lab'):
            lua.globals().lab(table(q), i+1)
        self.game.query.side_effect = lambda body: decode(lua.execute('return(function() '+' '.join(body.splitlines())+' end)()'))
        proof = self.c.proof(self.obs); self.assertIsNotNone(proof, self.c.last_proof_failure)
        self.assertEqual(len(proof['science_units']), 2); self.assertEqual(len(proof['lab_units']), 8)
        self.assertEqual(proof['lab_science_stock'], 16); self.assertEqual(proof['source'], proof['output'])
        for change, restore in (('powered=false', 'powered=true'), ('wrong_recipe=true', 'wrong_recipe=false'),
                ('wrong_quality=true', 'wrong_quality=false'), ('foreign_item=true', 'foreign_item=false'),
                ('wrong_lane=true', 'wrong_lane=false'), ('empty_output=true', 'empty_output=false'), ('last_lab_empty=true', 'last_lab_empty=false')):
            lua.execute(change); self.assertIsNone(self.c.proof(self.obs), change); lua.execute(restore)
        q = next(e for e in state['lab_interface']['entities'] if e['name'] == 'long-handed-inserter')
        arm = lua.globals().entities[_key(q)]; arm.pickup_position.y -= 1
        self.assertIsNone(self.c.proof(self.obs)); arm.pickup_position.y += 1
        arm.held_stack = table({'valid_for_read': True, 'name': 'copper-plate', 'quality': {'name': 'normal'}})
        self.assertIsNone(self.c.proof(self.obs)); arm.held_stack = table({'valid_for_read': False})
        self.components['sulfur'].proof.return_value = None
        self.assertIsNone(self.c.proof(self.obs)); self.assertEqual(self.c.last_proof_failure['reason'], 'blue_fresh_bound_producer_proof_changed')
        for forbidden in ('create_entity', 'set_recipe', 'insert_fluid', 'set_research', '.insert(', '.remove('):
            self.assertNotIn(forbidden, BLUE_SCIENCE_PROOF_LUA)


if __name__ == '__main__': unittest.main()
