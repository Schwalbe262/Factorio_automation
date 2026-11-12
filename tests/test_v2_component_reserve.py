from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.component_reserve import ComponentReserve,validate_parameters


def recipe(name,ingredients,yield_count=1):
    return dict(name=name,ingredients=[{'name':n,'amount':c,'type':'item'}for n,c in ingredients],
        products=[{'name':name,'amount':yield_count,'type':'item'}])

class ComponentReserveTests(unittest.TestCase):
    def setUp(self):
        recipes={r['name']:r for r in (recipe('iron-gear-wheel',[('iron-plate',2)]),
            recipe('transport-belt',[('iron-gear-wheel',1),('iron-plate',1)],2),
            recipe('pipe',[('iron-plate',1)]),recipe('electric-mining-drill',[('iron-plate',10),('iron-gear-wheel',5)]),
            recipe('automation-science-pack',[('copper-plate',1),('iron-gear-wheel',1)]))}
        self.catalog=SimpleNamespace(recipes=recipes,recipe_for_product=recipes.get)
        self.bootstrap=SimpleNamespace(_unreserved_stock_observation=Mock(side_effect=lambda obs,item:obs),ensure_item=Mock())
        self.c=ComponentReserve(SimpleNamespace(),self.bootstrap,self.catalog)
        self.obs=dict(world_id='w',tick=100,inventory={'iron-plate':20},entities=[],crafting_queue=[],
            enabled_recipes={name:True for name in recipes},known_project_construction_budget={
                'base_material_upper_bound':{'iron-plate':15},'unresolved_materials':[]})

    def test_validation_rejects_raw_science_ammo_bulk_and_invalid_absolute_targets(self):
        for items in ({},{'iron-plate':1},{'automation-science-pack':1},{'firearm-magazine':1},
                {'pipe':0},{'pipe':51},{'pipe':True},{'pipe':1.5}):
            with self.subTest(items=items),self.assertRaises(ValueError):validate_parameters({'items':items})
        with self.assertRaises(ValueError):validate_parameters({'items':{'pipe':1},'world':'w'})

    def test_current_primary_raw_reserve_blocks_spending_without_debit_or_grant(self):
        self.bootstrap.ensure_item.return_value={'type':'craft','recipe':'transport-belt','count':2}
        result=self.c.step(self.obs,{'items':{'transport-belt':4}})
        self.assertEqual(result['status'],'blocked')
        row=result['evidence']['blocked'][0]
        self.assertEqual(row['reason'],'component_preparation_would_spend_primary_raw_reserve')
        self.assertEqual(row['evidence']['action_raw_cost'],{'iron-plate':6})
        self.assertEqual(self.obs['inventory']['iron-plate'],20)

    def test_native_locked_later_tier_is_not_inferred_from_catalog_prototype(self):
        self.obs['enabled_recipes']['electric-mining-drill']=False
        result=self.c.step(self.obs,{'items':{'electric-mining-drill':1}})
        self.assertEqual(result['status'],'blocked');self.bootstrap.ensure_item.assert_not_called()

    def test_busy_first_item_does_not_hide_collectible_second_component(self):
        action={'type':'take_many','item':'pipe','count':3,'world_id':'w','sources':[{'unit_number':2}]}
        self.bootstrap.ensure_item.side_effect=[{'status':'waiting','reason':'engine crafting queue is busy'},action]
        result=self.c.step(self.obs,{'items':{'transport-belt':4,'pipe':3}})
        self.assertEqual(result,action);self.assertEqual(self.bootstrap.ensure_item.call_count,2)
        self.assertEqual(self.c.last_evidence['item'],'pipe')

    def test_real_public_raw_output_can_preserve_budget_and_ownership_is_filtered(self):
        self.obs['inventory']['iron-plate']=10
        public={'name':'stone-furnace','unit_number':1,'position':{'x':1,'y':1},'output_inventory':{'iron-plate':10}}
        private={'name':'wooden-chest','unit_number':2,'position':{'x':2,'y':1},'inventory':{'iron-plate':100}}
        self.obs['entities']=[public,deepcopy(public),private]
        self.bootstrap._unreserved_stock_observation.side_effect=lambda obs,item:dict(obs,entities=obs['entities'][:2])
        pickup={'type':'take_many','item':'iron-plate','count':10,'world_id':'w','sources':[{'unit_number':1}]}
        self.bootstrap.ensure_item.side_effect=[{'type':'craft','recipe':'pipe','count':5},pickup]
        self.assertEqual(self.c.step(self.obs,{'items':{'pipe':5}}),pickup)
        self.assertEqual(self.c.last_evidence['public_raw']['iron-plate'],10)
        self.assertEqual(self.c.last_evidence['reserve_prefetch_item'],'iron-plate')
        self.assertEqual(self.c.last_evidence['action_raw_cost'],{})

    def test_existing_intermediates_reduce_actual_raw_craft_cost(self):
        self.obs['inventory']['iron-gear-wheel']=2
        action={'type':'craft','recipe':'transport-belt','count':2};self.bootstrap.ensure_item.return_value=action
        self.assertEqual(self.c.step(self.obs,{'items':{'transport-belt':4}}),action)
        self.assertEqual(self.c.last_evidence['action_raw_cost'],{'iron-plate':2})

    def test_already_fulfilled_absolute_target_never_spends_again(self):
        self.obs['inventory']['pipe']=10
        params={'items':{'pipe':10}}
        self.assertEqual(self.c.step(self.obs,params)['status'],'succeeded')
        self.assertIsNotNone(self.c.proof(self.obs,params));self.bootstrap.ensure_item.assert_not_called()
        self.obs['inventory']['pipe']=9;self.assertIsNone(self.c.proof(self.obs,params))

    def test_unknown_main_or_component_cost_fails_closed(self):
        self.obs['known_project_construction_budget']['unresolved_materials']=['unknown']
        self.assertEqual(self.c.step(self.obs,{'items':{'pipe':1}})['status'],'blocked')
        self.bootstrap.ensure_item.assert_not_called()
        self.obs['known_project_construction_budget']['unresolved_materials']=[]
        self.catalog.recipes.pop('pipe')
        self.assertEqual(self.c.step(self.obs,{'items':{'pipe':1}})['status'],'blocked')
        self.bootstrap.ensure_item.assert_not_called()

    def test_unrelated_craft_is_rejected_even_if_helper_or_catalog_returns_it(self):
        self.bootstrap.ensure_item.return_value={'type':'craft','recipe':'automation-science-pack','count':1}
        self.assertEqual(self.c.step(self.obs,{'items':{'pipe':1}})['status'],'blocked')

    def test_material_collection_is_allowed_without_spending_current_reserve(self):
        self.obs['inventory']['iron-plate']=0
        action={'type':'take_many','item':'iron-plate','count':3,'world_id':'w','sources':[{'unit_number':1}]}
        self.bootstrap.ensure_item.return_value=action
        self.assertEqual(self.c.step(self.obs,{'items':{'pipe':3}}),action)
