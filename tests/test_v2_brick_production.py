from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.brick_production import BrickProduction, audit_brick_core, brick_core, brick_rates


MODULE='factorio_agent.brick_production.'


def catalog():
    return SimpleNamespace(fingerprint='catalog',recipes={'stone-brick':{
        'energy':3.2,'categories':['smelting'],'ingredients':[{'name':'stone','type':'item','amount':2}],
        'products':[{'name':'stone-brick','type':'item','amount':1}]}},
        entities={'stone-furnace':{'burner':True,'crafting_speed':1,'crafting_categories':['smelting'],'energy_usage':1500},
                  'inserter':{},'fast-inserter':{}},items={'coal':{'fuel_value':4000000}})


def sources():
    return ({'item':'stone','direction':'output','facing':4,'unit_number':100,'world_id':'w',
             'position':{'x':-20.5,'y':44.5},'material_supply_id':'stone-bank','mining_resource':'stone',
             'nominal_shared_rate_per_minute':60},
            {'item':'coal','direction':'output','facing':4,'unit_number':5278,'world_id':'w',
             'position':{'x':-60.5,'y':31.5},'material_supply_id':'coal-bank','nominal_shared_rate_per_minute':60})


class BrickCoreTests(unittest.TestCase):
    def test_catalog_balances_and_fixed_opposite_output_lanes(self):
        core=brick_core(catalog());audit=audit_brick_core(core,catalog())
        self.assertTrue(audit['ok'],audit)
        self.assertEqual((audit['furnaces'],audit['automatic_inputs'],audit['opposite_lane_outputs']),(2,2,2))
        self.assertEqual(core['rated']['brick_per_minute'],37.5)
        self.assertEqual(core['rated']['stone_per_minute'],75)
        self.assertEqual(core['rated']['coal_per_minute_at_max_catalog_usage'],2.7)
        self.assertEqual(core['required_items'],{'stone-furnace':2,'fast-inserter':2,'inserter':2,
                         'small-electric-pole':2,'transport-belt':33,'splitter':1})
        self.assertEqual(core['ports'][-1]['position'],{'x':92.5,'y':-15.5})
        self.assertEqual(core['ports'][-1]['lane_items'],{'left':'stone-brick','right':'stone-brick'})
        self.assertFalse(any('recipe' in e for e in core['entities']))

    def test_recipe_speed_yield_and_fuel_are_actual_catalog_values(self):
        c=catalog();c.recipes['stone-brick'].update(energy=8)
        c.recipes['stone-brick']['products'][0]['amount']=2
        c.recipes['stone-brick']['ingredients'][0]['amount']=4
        c.entities['stone-furnace'].update(crafting_speed=2,energy_usage=1000)
        c.items['coal']['fuel_value']=8000000
        rated=brick_rates(c)
        self.assertEqual((rated['brick_per_minute'],rated['stone_per_minute'],rated['coal_per_minute_at_max_catalog_usage']),(60,120,.9))
        for mutate in (lambda c:c.recipes['stone-brick']['products'][0].update(probability=.5),
                       lambda c:c.recipes['stone-brick']['ingredients'][0].update(name='iron-plate'),
                       lambda c:c.items['coal'].update(fuel_value=0)):
            c=catalog();mutate(c)
            with self.assertRaises(ValueError):brick_rates(c)

    def test_fast_input_is_mandatory_but_normal_output_has_capacity(self):
        core=brick_core(catalog());audit=audit_brick_core(core,catalog())
        self.assertAlmostEqual(audit['inserter_budget']['input_items_per_minute_each'],38.85)
        self.assertEqual(audit['inserter_budget']['output_items_per_minute_each'],18.75)
        core['entities'][1]['name']='inserter'
        self.assertIn('wrong_input_inserter_type',audit_brick_core(core,catalog())['errors'])
        c=catalog();c.recipes['stone-brick']['energy']=1
        self.assertIn('brick_fast_input_inserter_budget_exceeded',audit_brick_core(brick_core(c),c)['errors'])

    def test_growth_keeps_every_old_asset_ports_and_reservation(self):
        initial=brick_core(catalog())
        for count in (2,3):
            expanded=brick_core(catalog(),per_side_count=count)
            self.assertTrue(audit_brick_core(expanded,catalog())['ok'])
            self.assertEqual(expanded['ports'],initial['ports'])
            self.assertEqual(expanded['reserved_area'],initial['reserved_area'])
            self.assertTrue(all(e in expanded['entities'] for e in initial['entities']))

    def test_wrong_typed_ports_broken_feed_and_output_side_fail_audit(self):
        core=brick_core(catalog());core['ports'][-1]['item']='steel-plate'
        self.assertIn('brick_typed_ports_or_recipe_changed',audit_brick_core(core,catalog())['errors'])
        core=brick_core(catalog());core['entities']=[e for e in core['entities'] if e['position']!={'x':78.5,'y':-21.5}]
        self.assertFalse(audit_brick_core(core,catalog())['ok'])
        core=brick_core(catalog());core['output_inserters'][1]['lane']='right'
        self.assertIn('output_not_native_opposite_lane',audit_brick_core(core,catalog())['errors'])


class BrickControllerTests(unittest.TestCase):
    def coordinator(self, root):
        game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root),query=Mock(return_value={'ok':True}))
        builder=SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,
                                ensure_plan=Mock(return_value={'status':'running','action':{'type':'build'}}))
        return BrickProduction(game,builder,catalog())

    def obs(self):
        return {'world_id':'w','tick':100,'entities':[],
                'enabled_recipes':{'stone-brick':True,'fast-inserter':True}}

    def board(self, root, **blocks):
        Path(root,'pcb-board.json').write_text(json.dumps({'world_id':'w','catalog_fingerprint':'catalog','blocks':blocks}))

    def test_recipe_and_fast_arm_unlock_fail_before_query_or_spending(self):
        c=self.coordinator('unused');obs=self.obs();obs['enabled_recipes']['stone-brick']=False
        self.assertEqual(c.prepare(obs)['reason'],'native_stone_brick_recipe_required')
        obs=self.obs();obs['enabled_recipes']['fast-inserter']=False
        self.assertEqual(c.prepare(obs)['reason'],'native_fast_input_inserters_required')
        c.game.query.assert_not_called();c.builder.ensure_plan.assert_not_called()

    def test_missing_or_stale_registry_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            c=self.coordinator(root)
            self.assertEqual(c.prepare(self.obs())['reason'],'brick_material_sources_not_commissioned')
            Path(root,'material-sources.json').write_text(json.dumps({'world_id':'other','catalog_fingerprint':'catalog','sources':{}}))
            self.assertEqual(c.prepare(self.obs())['reason'],'brick_material_registry_identity_changed')
            c.game.query.assert_not_called()

    def test_exact_world_unit_type_and_resource_markers_are_required(self):
        for mutate in (lambda p:p[0].update(unit_number=True),lambda p:p[0].update(world_id='other'),
                       lambda p:p[1].update(item='stone'),lambda p:p[0].update(mining_resource='coal'),
                       lambda p:p[0].update(material_supply_id='')):
            c=self.coordinator('unused');ports=list(sources());mutate(ports)
            result=c.prepare(self.obs(),stone_source=ports[0],coal_source=ports[1])
            self.assertEqual(result['reason'],'brick_exact_registered_typed_sources_required')
            c.game.query.assert_not_called()

    def test_native_changed_source_stops_before_terrain_or_build(self):
        c=self.coordinator('unused');stone,coal=sources()
        c.game.query.return_value={'ok':False,'reason':'brick_registered_source_changed'}
        with patch(MODULE+'survey_slots') as survey:
            self.assertEqual(c.prepare(self.obs(),stone_source=stone,coal_source=coal)['reason'],'brick_registered_source_changed')
            survey.assert_not_called();c.builder.ensure_plan.assert_not_called()
        code=c.game.query.call_args.args[0]
        for guard in ('e.unit_number~=spec.unit_number','e.force~=f','e.surface~=s','v.name~=spec.item'):
            self.assertIn(guard,code)

    def prepare_fixture(self, root, *, check=None, power=None, route=None):
        c=self.coordinator(root);self.board(root);stone,coal=sources()
        Path(root,'material-sources.json').write_text(json.dumps({'world_id':'w','catalog_fingerprint':'catalog',
              'sources':{'stone':stone,'brick-coal':coal}}))
        simple_route=route or _plan([],[],underground_pairs=[],clear_obstacles=[],route_metrics={'length':10,'turns':2})
        with patch(MODULE+'survey_slots',return_value={'ok':True,'legal':[1]}), \
             patch(MODULE+'plan_connection',return_value=simple_route) as routes, \
             patch(MODULE+'connect_power_groups',return_value=power or {'ok':True,'entities':[]}), \
             patch(MODULE+'preflight',return_value=check or {'ok':True,'clear_obstacles':[]}), \
             patch(MODULE+'_atomic_json') as write:
            prepared=c.prepare(self.obs(),anchors=((82,-18),))
            write.assert_not_called();c.builder.ensure_plan.assert_not_called()
            return c,prepared,routes

    def test_whole_prepare_is_read_only_preserves_source_marker_and_reports_supply_ceiling(self):
        with tempfile.TemporaryDirectory() as root:
            c,prepared,routes=self.prepare_fixture(root)
            self.assertTrue(prepared['ok'],prepared)
            self.assertEqual(prepared['stone_supply_ceiling_brick_per_minute'],30)
            self.assertEqual(prepared['core']['rated']['brick_per_minute'],37.5)
            self.assertEqual(prepared['sources'][0]['mining_resource'],'stone')
            self.assertEqual(routes.call_args_list[0].args[3]['mining_resource'],'stone')
            self.assertEqual(prepared['sources'][1]['unit_number'],5278)
            self.assertEqual(c.state,{})
            self.assertFalse(c.path.exists())
            self.assertEqual(json.loads(Path(root,'pcb-board.json').read_text())['blocks'],{})

    def test_resource_preflight_or_power_failure_produces_no_buildable_plan(self):
        for option in ({'check':{'ok':False,'reason':'new_entity_over_resource_or_mining_reservation'}},
                       {'power':{'ok':False,'reason':'no_supplied_pole_network'}},
                       {'route':{'ok':False,'reason':'no_clean_trunk_corridor'}}):
            with tempfile.TemporaryDirectory() as root:
                _,prepared,_=self.prepare_fixture(root,**option)
                self.assertFalse(prepared['ok'])
                self.assertNotIn('plan',prepared)

    def test_lost_source_blocks_resume_before_builder_could_rebuild_it(self):
        with tempfile.TemporaryDirectory() as root:
            c,prepared,_=self.prepare_fixture(root);c.state=prepared
            c.game.query.return_value={'ok':False,'reason':'brick_registered_source_changed'}
            with patch(MODULE+'_atomic_json') as write:
                self.assertEqual(c.step(self.obs())['reason'],'brick_registered_source_changed')
                write.assert_not_called();c.builder.ensure_plan.assert_not_called()

    def test_step_only_delegates_prepared_normal_build_and_preserves_board(self):
        with tempfile.TemporaryDirectory() as root:
            c,prepared,_=self.prepare_fixture(root);c.state=prepared
            self.assertEqual(c.step(self.obs())['action'],{'type':'build'})
            c.builder.ensure_plan.assert_called_once_with(self.obs(),prepared['plan'])
            self.assertEqual(json.loads(Path(root,'pcb-board.json').read_text())['blocks']['brick_production'],prepared['plan'])
            c.state['world_id']='other';c.builder.ensure_plan.reset_mock()
            self.assertEqual(c.step(self.obs())['reason'],'brick_world_or_catalog_changed')
            c.builder.ensure_plan.assert_not_called()

    def proof_fixture(self):
        c=self.coordinator('unused');core=brick_core(catalog());stone,coal=sources()
        c.state={'world_id':'w','catalog_fingerprint':'catalog','core':core,
                 'plan':_plan(deepcopy(core['entities']),core['ports'],underground_pairs=[]),'sources':[stone,coal]}
        obs=self.obs()
        for index,spec in enumerate(core['entities']):
            live=dict(spec,unit_number=index+1000,energy=100,electric_network_connected=True)
            if spec['name']=='stone-furnace':live.update(recipe='stone-brick',products_finished=1,inventory={'coal':1})
            obs['entities'].append(live)
        lanes={'ok':True,'tick':101,'left':{'stone-brick':1},'right':{'stone-brick':1},
               'input_left':{'stone':1},'input_right':{'coal':1}}
        c.game.query.side_effect=lambda code:deepcopy(lanes) if 'local result={ok=true,tick=game.tick}' in code else {'ok':True}
        return c,obs,lanes

    def test_commissioning_proves_both_output_lanes_distinct_inputs_without_mutating_plan(self):
        c,obs,_=self.proof_fixture();before=deepcopy(c.state)
        evidence=c.proof(obs)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence['rated']['brick_per_minute'],37.5)
        self.assertEqual(c.state,before)
        self.assertEqual(evidence['furnace_units'],[1000,1004])

    def test_unarmed_furnace_unpowered_fast_arm_and_wrong_recipe_fail_commissioning(self):
        for mutate in (lambda es:es[0].update(products_finished=0),lambda es:es[0].update(inventory={}),
                       lambda es:es[1].update(electric_network_connected=False),lambda es:es[0].update(recipe='steel-plate')):
            c,obs,_=self.proof_fixture();mutate(obs['entities'])
            self.assertIsNone(c.proof(obs))

    def test_empty_output_or_contaminated_input_lane_cannot_be_commissioned(self):
        for side,content in (('right',{}),('left',{'steel-plate':1}),('input_left',{'coal':1}),('input_right',{})):
            c,obs,lanes=self.proof_fixture();lanes[side]=content
            self.assertIsNone(c.proof(obs))


if __name__=='__main__':unittest.main()
