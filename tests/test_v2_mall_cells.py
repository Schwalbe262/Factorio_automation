from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from factorio_agent.construction_mall import (mall_core,mall_rates,audit_mall_core,_hash,_key,_product,
    MALL_PROOF_LUA,execute_mall_stock_limit)
from factorio_agent.mall_cells import PRODUCTS,recipe_tree
from factorio_agent.mall_stock_transfer import mall_stock_action,execute_take_mall_stock
import test_v2_construction_mall as old


def catalog():
    c=old.catalog();data=c.to_dict()
    def r(name,energy,amount,ingredients):return dict(category='crafting',energy=energy,
        products=[dict(name=name,type='item',amount=amount)],ingredients=[dict(name=n,type='item',amount=a)for n,a in ingredients])
    recipes={
        'underground-belt':r('underground-belt',1,2,[('iron-plate',10),('transport-belt',5)]),
        'splitter':r('splitter',1,1,[('iron-plate',5),('electronic-circuit',5),('transport-belt',4)]),
        'assembling-machine-1':r('assembling-machine-1',.5,1,[('iron-plate',9),('iron-gear-wheel',5),('electronic-circuit',3)]),
        'inserter':r('inserter',.5,1,[('iron-plate',1),('iron-gear-wheel',1),('electronic-circuit',1)]),
        'copper-cable':r('copper-cable',.5,2,[('copper-plate',1)]),
        'electronic-circuit':r('electronic-circuit',.5,1,[('iron-plate',1),('copper-cable',3)])}
    data['recipes'].update(recipes)
    for name in recipes:data['items'][name]={'stack_size':50 if name in PRODUCTS else 200}
    return old.WorldCatalog.from_dict(data)


class MallRecipeTreeTests(unittest.TestCase):
    def test_old_commissioned_geometry_is_byte_canonical_and_new_products_supported(self):
        c=old.catalog()
        self.assertEqual(_hash(mall_core(c,'transport-belt',.5,.5)),'19bb6894623200bacb87d98a9833be6a8a231f13aa22fff9ae9badfc30fd64ea')
        self.assertEqual(_hash(mall_core(c,'gun-turret',.5,.5)),'11b04d2a606888b2d339f82313061743cb128adef110e6aa34bf46bab6c13629')
        for product in PRODUCTS:self.assertEqual(_product({'product':product}),product)

    def test_every_machine_gets_every_ingredient_from_normal_arms_and_pure_reachable_headers(self):
        for product,machines in [('underground-belt',3),('splitter',5),('assembling-machine-1',4),('inserter',4)]:
            with self.subTest(product=product):
                core=mall_core(catalog(),product,.5,.5)
                self.assertTrue(audit_mall_core(core)['ok'],audit_mall_core(core));self.assertEqual(len(core['machines']),machines)
                self.assertEqual(len(core['arm_connections']),len(core['arms']))
                self.assertTrue(all(q['name']=='fast-inserter'for q in core['arms']))
                self.assertEqual(core['chest']['position'],{'x':.5,'y':3.5})
                self.assertEqual(len(core['output_access']),2)

    def test_broken_actual_raw_or_direct_arm_paths_are_caught(self):
        for mutation in('raw','direct','ingredient'):
            core=mall_core(catalog(),'splitter',.5,.5)
            if mutation=='raw':
                head=next(q for q in core['entities']if q['name']=='transport-belt');head['direction']=8
            elif mutation=='direct':
                edge=next(q for q in core['arm_connections']if q['source']is not None and q['destination']['name']=='assembling-machine-1')
                edge['arm']['direction']=(edge['arm']['direction']+8)%16
            else:core['recipe_inputs']['splitter'].append('stone-brick')
            self.assertFalse(audit_mall_core(core)['ok'])

    def test_catalog_and_inserter_limits_and_integer_batch_costs_are_honest(self):
        expected={'underground-belt':(24,{'iron-plate':439}),
                  'splitter':(8,{'iron-plate':320,'copper-plate':150}),
                  'assembling-machine-1':(12,{'iron-plate':440,'copper-plate':90}),
                  'inserter':(40,{'iron-plate':200,'copper-plate':75})}
        for product,(rate,raw)in expected.items():
            r=mall_rates(catalog(),product);self.assertEqual(r['nameplate_per_minute'],rate);self.assertEqual(r['raw_per_target_fill'],raw)
            self.assertTrue(any(q['kind']=='raw_inserter'for q in r['capacity_limits']))
        ug=mall_rates(catalog(),'underground-belt');self.assertEqual(ug['machine_nameplate_per_minute'],48)
        self.assertEqual(ug['minimum_batch_excess'],{'transport-belt':1})

    def test_recipe_graph_rejects_cycles_and_hidden_raw_input_dependencies(self):
        for name in('iron-gear-wheel','electronic-circuit'):
            c=catalog();c.recipes[name]['ingredients'].append(dict(type='item',name='wood',amount=1))
            with self.assertRaises(ValueError):recipe_tree(c,'splitter')
        c=catalog();c.recipes['copper-cable']['ingredients']=[dict(type='item',name='electronic-circuit',amount=1)]
        with self.assertRaises(ValueError):recipe_tree(c,'splitter')


@unittest.skipUnless(old.LuaRuntime,'optional executable Lua fixture')
class MallRecipeTreeNativeTests(unittest.TestCase):
    def setUp(self):
        # Reuse the commissioned cap implementation's existing native API fixture.
        fixture=old.MallNativeTests('test_native_bars_all_wire_conditions_and_output_hand_stop_are_proved')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        for name in('cat','core','game','builder','perimeter','mall','entry','obs','root','lua','live','config'):
            setattr(self,name,getattr(fixture,name))

    save=old.MallTests.save

    def install(self,product):
        self.cat=catalog();self.core=mall_core(self.cat,product,.5,.5)
        self.entry=dict(world_id='w',catalog_fingerprint=self.cat.fingerprint,phase='verified',core=self.core,plan=self.core,
            bindings={_key(q):1000+i for i,q in enumerate(self.core['entities'])},baseline={},sources={},rated=self.core['rated'],source_budgets={},defense_scope_id='mall')
        self.mall.catalog=self.cat;self.mall.state=dict(world_id='w',catalog_fingerprint=self.cat.fingerprint,products={product:self.entry})
        self.obs=dict(ok=True,world_id='w',actor_unit_number=11,tick=100,inventory={},entities=[dict(q,unit_number=self.entry['bindings'][_key(q)])for q in self.core['entities']])
        for name,item in self.cat.items.items():
            self.lua.execute('prototypes.item['+json.dumps(name)+']={type="item",stack_size='+str(item['stack_size'])+'}')
        table=self.lua.globals().helpers.json_to_table
        self.live={_key(q):self.lua.globals().add(table(json.dumps(q)),self.entry['bindings'][_key(q)])for q in self.core['entities']}
        for q in self.core['arm_connections']:
            e=self.live[_key(q['arm'])];e.pickup_position=table(json.dumps(q['pickup_position']));e.drop_position=table(json.dumps(q['drop_position']))
        self.mall._save();(self.root/'catalog.json').write_text(json.dumps(self.cat.to_dict()))
        (self.root/'pcb-board.json').write_text(json.dumps(dict(world_id='w',catalog_fingerprint=self.cat.fingerprint,blocks={'construction_mall_'+product:self.core})))
        self.config=dict(type='mall_stock_limit',product=product,expected_world_id='w',expected_actor_unit=11,expected_catalog_fingerprint=self.cat.fingerprint)

    def test_all_four_products_have_native_machine_arm_hand_stops_and_bar_backups(self):
        for product,target in [('underground-belt',50),('splitter',20),('assembling-machine-1',20),('inserter',50)]:
            with self.subTest(product=product):
                self.install(product);r=execute_mall_stock_limit(self.game,self.config);self.assertTrue(r['ok'],r)
                self.assertEqual(r['target'],target);self.assertEqual(r['chest_bar_capacity'],50)
                self.assertTrue(self.mall._controls(self.obs,self.entry)['ok'])
                machine=self.live[_key(self.core['final_machine'])]
                machine.cb.circuit_condition.constant=target+1
                self.assertFalse(self.mall._controls(self.obs,self.entry)['ok'])

    def test_new_native_ingredient_endpoints_and_exact_quantity_cap_fail_closed(self):
        self.install('inserter');self.assertTrue(execute_mall_stock_limit(self.game,self.config)['ok'])
        r=self.mall._native(self.obs,self.entry,MALL_PROOF_LUA);self.assertTrue(r['ok'],r)
        arm=self.live[_key(self.core['arms'][0])];arm.drop_position.x=99
        self.assertEqual(self.mall._native(self.obs,self.entry,MALL_PROOF_LUA)['reason'],'mall_ordinary_inserter_target_changed')
        self.install('splitter');self.assertTrue(execute_mall_stock_limit(self.game,self.config)['ok'])
        chest=self.live[_key(self.core['chest'])];chest.inv[1]=self.lua.globals().stack('splitter',21)
        self.assertEqual(self.mall._native(self.obs,self.entry,MALL_PROOF_LUA)['reason'],'mall_output_exceeds_configured_stock_target')

    def test_all_new_product_collectors_take_only_paid_capped_output_and_refill_after_collection(self):
        for product in PRODUCTS:
            with self.subTest(product=product):
                self.install(product);self.assertTrue(execute_mall_stock_limit(self.game,self.config)['ok'])
                chest=self.live[_key(self.core['chest'])];chest.inv[1]=self.lua.globals().stack(product,10)
                action=mall_stock_action(self.game,self.obs,product,3,[1]);r=execute_take_mall_stock(self.game,action)
                self.assertTrue(r['ok'],r);self.assertEqual(r['moved'],3);self.assertEqual(chest.inv[1].count,7)
                self.assertEqual(self.live[_key(self.core['final_machine'])].cb.circuit_condition.constant,self.core['target'])
                self.entry['phase']='bus';self.assertEqual(self.mall.pending_items,(product,))
                other=next(p for p in PRODUCTS if p!=product)
                self.assertEqual(self.mall.prepare(self.obs,{'product':other})['reason'],'another_mall_cell_must_commission_first')


if __name__=='__main__':unittest.main()
