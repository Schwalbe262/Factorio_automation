from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_ai.world_catalog import WorldCatalog
from factorio_agent.construction_mall import (ConstructionMall, mall_core, mall_rates, audit_mall_core,
    execute_mall_stock_limit, MALL_CONTROL_LUA, MALL_PROOF_LUA, _key, _product, _control_entities)
from factorio_agent.board import entity
from factorio_agent.routing import route_trunks
from factorio_agent.transport import compile_crossings, xy
from factorio_agent.mall_stock_transfer import mall_stock_action, execute_take_mall_stock

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


def catalog():
    def recipe(name,seconds,amount,ingredients):
        return dict(category='crafting',energy=seconds,products=[dict(name=name,type='item',amount=amount)],
                    ingredients=[dict(name=n,type='item',amount=q)for n,q in ingredients])
    return WorldCatalog(dict(version='test',entities={'assembling-machine-1':dict(crafting_speed=.5,crafting_categories=['crafting'])},
        items={'transport-belt':{'stack_size':100},'gun-turret':{'stack_size':50}},recipes={
        'transport-belt':recipe('transport-belt',.5,2,[('iron-plate',1),('iron-gear-wheel',1)]),
        'gun-turret':recipe('gun-turret',8,1,[('iron-plate',20),('copper-plate',10),('iron-gear-wheel',10)]),
        'iron-gear-wheel':recipe('iron-gear-wheel',.5,1,[('iron-plate',2)])}))


class MallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.cat=catalog();self.core=mall_core(self.cat,'gun-turret',.5,.5)
        self.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root),expected_world='w',backend='assisted',query=Mock())
        self.builder=SimpleNamespace(bootstrap=Mock());self.perimeter=Mock()
        self.mall=ConstructionMall(self.game,self.builder,self.cat,self.perimeter)
        self.entry=dict(world_id='w',catalog_fingerprint=self.cat.fingerprint,phase='verified',core=self.core,plan=self.core,
            bindings={_key(q):100+i for i,q in enumerate(self.core['entities'])},
            baseline={},sources={},rated=self.core['rated'],source_budgets={},defense_scope_id='mall')
        self.mall.state=dict(world_id='w',catalog_fingerprint=self.cat.fingerprint,products={'gun-turret':self.entry})
        self.obs=dict(ok=True,world_id='w',actor_unit_number=11,tick=100,inventory={},entities=[dict(q,unit_number=self.entry['bindings'][_key(q)])for q in self.core['entities']])
        self.save()

    def save(self):
        self.mall._save();(self.root/'catalog.json').write_text(json.dumps(self.cat.to_dict()))
        (self.root/'pcb-board.json').write_text(json.dumps(dict(world_id='w',catalog_fingerprint=self.cat.fingerprint,
            blocks={'construction_mall_gun-turret':self.core})))

    def test_catalog_rates_and_fill_costs_are_finite_not_guaranteed_surplus(self):
        belt=mall_rates(self.cat,'transport-belt');gun=mall_rates(self.cat,'gun-turret')
        self.assertEqual(belt['nameplate_per_minute'],120);self.assertEqual(belt['raw_per_target_fill'],{'iron-plate':300})
        self.assertEqual(gun['nameplate_per_minute'],3.75);self.assertEqual(gun['raw_per_target_fill'],{'iron-plate':320,'copper-plate':80})
        self.assertEqual(gun['raw_nameplate_per_minute'],{'iron-plate':150,'copper-plate':37.5})

    def test_both_initial_cores_cover_north_south_arms_and_reserve_fixed_expansion(self):
        for product in ('transport-belt','gun-turret'):
            core=mall_core(self.cat,product,.5,.5);self.assertTrue(audit_mall_core(core)['ok'],audit_mall_core(core))
            self.assertEqual(len(core['machines']),2)
            broken=deepcopy(core);broken['poles']=[p for p in core['poles']if p['position']['y']>0]
            self.assertFalse(audit_mall_core(broken)['ok'])

    def test_red_factory_and_future_cells_are_not_advertised_as_finished_mall_products(self):
        for name in ('piercing-rounds-magazine','medium-electric-pole','stone-wall'):
            with self.assertRaises(ValueError):_product({'product':name})

    def test_real_route_destination_remains_in_complete_core_before_external_feed(self):
        port=self.core['ports'][0];x,y=xy(port)
        route=compile_crossings(route_trunks((x-4,y),(x,y),start_direction=4,end_direction=4),[],maximum_span=5)
        self.assertTrue(route['ok']);external=[q for q in route['entities']if q not in self.core['entities']]
        controls=_control_entities(self.core,self.core['entities']+external,route['entities'])
        self.assertTrue(all(q in controls for q in self.core['entities']))
        self.assertFalse(any(q in controls for q in external))
        self.assertIn(entity('transport-belt',x,y,4),controls)

    def test_provider_total_actor_target_and_uncommissioned_recursion_guard(self):
        self.assertTrue(self.mall.has_stock_cell('gun-turret'));self.assertFalse(self.mall.has_stock_cell('transport-belt'))
        self.assertIsNone(self.mall.ensure_stock(self.obs,'transport-belt',200))
        self.assertIsNone(self.mall.ensure_stock(dict(self.obs,inventory={'gun-turret':8}),'gun-turret',8))
        self.mall.proof=Mock(return_value={'native':{'output_stock':0},'source_budgets':{'iron':'deficit'}})
        self.assertEqual(self.mall.ensure_stock(self.obs,'gun-turret',8)['status'],'waiting')

    def test_known_file_configuration_guard_rejects_changed_actor_catalog_or_board_before_query(self):
        action=dict(type='mall_stock_limit',product='gun-turret',expected_world_id='w',expected_actor_unit=11,expected_catalog_fingerprint=self.cat.fingerprint)
        for change in ({'expected_actor_unit':True},{'expected_catalog_fingerprint':'foreign'},{'product':'piercing-rounds-magazine'}):
            with self.assertRaises(ValueError):execute_mall_stock_limit(self.game,{**action,**change})
        self.game.query.assert_not_called()

    def test_stock_unknown_receipt_is_never_reported_as_completed(self):
        action=mall_stock_action(self.game,self.obs,'gun-turret',3,[1])
        self.game.query.side_effect=TimeoutError('after possible insert')
        self.assertEqual(execute_take_mall_stock(self.game,action)['reason'],'mall_stock_transfer_outcome_unknown')
        self.game.query.side_effect=None;self.game.query.return_value={'ok':True,'moved':4}
        self.assertEqual(execute_take_mall_stock(self.game,action)['reason'],'mall_stock_transfer_outcome_unknown')

    def test_partial_paid_frontier_admits_only_exact_future_tap_corner_not_other_belts(self):
        added=[entity('transport-belt',-11.5,-21.5,0),entity('transport-belt',-11.5,-20.5,0),entity('transport-belt',-3.5,-20.5,0)]
        self.mall.extension.state=dict(world_id='w',catalog_fingerprint=self.cat.fingerprint,phase='build',
            parameters={'north_y':-22.5},old_bus={'entities':[]},retained_units=[],added_entities=added)
        self.entry['bus_preview']=dict(north_y=-22.5,ignored_units=[],bus={'trunks':[dict(item='iron-plate',line=0,x=-11.5)]},
            tasks=[{'parameters':dict(item='iron-plate',splitter_y=-20.5)}])
        # A partial normal build already paid two of the three planned tiles.
        obs=dict(self.obs,entities=self.obs['entities']+[dict(q,unit_number=1000+i)for i,q in enumerate(added[:2])]
                 +[dict(entity('transport-belt',-11.5,-19.5,0),unit_number=9999)])
        result=self.mall._future_corner_units(obs,self.entry)
        self.assertTrue(result['ok'],result);self.assertEqual(result['ignored_units'],[1001])
        self.assertEqual(len(self.mall.extension.state['added_bindings']),2)
        replaced=deepcopy(obs);next(q for q in replaced['entities']if q['unit_number']==1001)['unit_number']=2001
        self.assertEqual(self.mall._future_corner_units(replaced,self.entry)['reason'],'mall_extension_paid_frontier_unit_changed')

    def test_later_product_without_extension_does_not_adopt_historical_extension(self):
        self.entry['bus_preview']=dict(north_y=None,ignored_units=[123])
        self.mall.extension.state={'parameters':{'north_y':-100.5},'phase':'verified'}
        self.assertEqual(self.mall._future_corner_units(self.obs,self.entry),{'ok':True,'ignored_units':[123]})
        self.entry['bus_preview']['north_y']=-22.5
        self.mall.extension.state.update(world_id='w',catalog_fingerprint=self.cat.fingerprint)
        self.assertEqual(self.mall._future_corner_units(self.obs,self.entry),{'ok':True,'ignored_units':[123]})


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class MallNativeTests(MallTests):
    def setUp(self):
        super().setUp();self.lua=LuaRuntime(unpack_returned_tuples=True)
        def table(v):
            if isinstance(v,dict):return self.lua.table_from({k:table(q)for k,q in v.items()})
            if isinstance(v,list):return self.lua.table_from([table(q)for q in v])
            return v
        def decode(v):
            if not hasattr(v,'items'):return v
            d=dict(v.items())
            if d and set(d)==set(range(1,len(d)+1)):return [decode(d[i])for i in range(1,len(d)+1)]
            return{k:decode(q)for k,q in d.items()}
        self.table=table
        self.lua.globals().helpers=self.lua.table_from({'json_to_table':lambda v:table(json.loads(v))})
        self.lua.execute('''d={world_id="w"};game={tick=100};f={technologies={["circuit-network"]={researched=true}}};s={};world={}
defines={inventory={chest=1,crafter_input=2},wire_connector_id={circuit_red=1,circuit_green=2},wire_origin={player=1},control_behavior={inserter={hand_read_mode={hold=1}}}}
prototypes={item={}};for _,n in ipairs({"gun-turret","transport-belt","iron-gear-wheel","iron-plate","copper-plate"})do prototypes.item[n]={stack_size=n=="gun-turret"and 50 or 100,type="item"}end
function stack(name,count)return {valid_for_read=true,name=name,count=count,health=1,spoil_tick=0,quality={name="normal"},prototype=prototypes.item[name]}end
function inventory(n)local inv={bar=n+1};for i=1,n do inv[i]={valid_for_read=false}end
 inv.get_bar=function()return inv.bar end;inv.set_bar=function(v)inv.bar=v end
 inv.insert=function(q)for i=1,n do if not inv[i].valid_for_read then inv[i]=stack(q.name,q.count);return q.count end end;return 0 end
 return inv end
main=inventory(20);a={valid=true,unit_number=11,force=f,surface=s,get_main_inventory=function()return main end,can_reach_entity=function()return true end}
function add(q,unit)local e={valid=true,name=q.name,position=q.position,unit_number=unit,direction=q.direction or 0,
 quality={name="normal"},force=f,surface=s,type=q.name=="small-electric-pole"and"electric-pole"or q.name,
 energy=100,products_finished=0,cb={},conns={},inv=inventory(48),out=inventory(4),held_stack={valid_for_read=false}}
 e.get_recipe=function()return q.recipe and{name=q.recipe}end;e.is_connected_to_electric_network=function()return true end
 e.get_inventory=function(index)assert(type(index)=="number","inventory index must be a number");assert(index==1 or index==2,"unsupported inventory index");return e.inv end;e.get_output_inventory=function()return e.out end
 e.get_control_behavior=function()return e.cb end;e.get_or_create_control_behavior=function()return e.cb end
 e.get_transport_line=function()return{get_contents=function()return{}end}end
 e.get_wire_connector=function(id,create)if not e.conns[id]and create then
  local c={owner=e,wire_connector_id=id,connections={}};c.can_wire_reach=function(other)return(e.position.x-other.owner.position.x)^2+(e.position.y-other.owner.position.y)^2<=81 end
  c.is_connected_to=function(other)for _,link in ipairs(c.connections)do if link.target==other then return true end end;return false end
  c.connect_to=function(other)if not c.is_connected_to(other)then c.connections[#c.connections+1]={target=other};other.connections[#other.connections+1]={target=c}end;return true end;e.conns[id]=c end;return e.conns[id]end
 world[q.name..":"..q.position.x..":"..q.position.y]=e;return e end
target=function(p,name)return world[name..":"..p.x..":"..p.y]end
''')
        self.live={_key(q):self.lua.globals().add(table(q),self.entry['bindings'][_key(q)])for q in self.core['entities']}
        self.game.query=Mock(side_effect=lambda body:decode(self.lua.execute('return(function() '+' '.join(body.splitlines())+' end)()')))
        self.config=dict(type='mall_stock_limit',product='gun-turret',expected_world_id='w',expected_actor_unit=11,expected_catalog_fingerprint=self.cat.fingerprint)

    def test_native_bars_all_wire_conditions_and_output_hand_stop_are_proved(self):
        result=execute_mall_stock_limit(self.game,self.config);self.assertTrue(result['ok'],result)
        self.assertEqual(result['target'],8);self.assertEqual(result['chest_bar_capacity'],50)
        self.assertTrue(self.mall._controls(self.obs,self.entry)['ok'])
        arm=self.live[_key(self.core['output_arm'])];arm.cb.circuit_read_hand_contents=False
        self.assertEqual(self.mall._controls(self.obs,self.entry)['reason'],'mall_output_hand_signal_changed')

    def test_current_inventory_enum_works_without_removed_assembling_machine_alias(self):
        self.assertIsNone(self.lua.globals().defines.inventory.assembling_machine_input)
        result=self.mall._native(self.obs,self.entry,MALL_PROOF_LUA)
        self.assertTrue(result['ok'],result)

    def test_native_production_requires_both_counters_and_output_then_survives_legitimate_pickup(self):
        self.assertTrue(execute_mall_stock_limit(self.game,self.config)['ok'])
        self.entry['baseline']={str(self.entry['bindings'][_key(q)]):0 for q in self.core['machines']}
        self.mall._sources=Mock(return_value={'ok':True,'sources':{},'budgets':{}});self.mall._power=Mock(return_value={'ok':True})
        self.perimeter.coverage_proof.return_value={'ok':True};self.save()
        for q in self.core['machines']:self.live[_key(q)].products_finished=1
        chest=self.live[_key(self.core['chest'])];chest.inv[1]=self.lua.globals().stack('gun-turret',2)
        before=self.mall.path.read_bytes();proof=self.mall.proof(self.obs,{'product':'gun-turret'})
        self.assertIsNotNone(proof);self.assertEqual(self.mall.path.read_bytes(),before)
        self.entry['production_witness']=proof['production_witness'];chest.inv[1].valid_for_read=False
        self.assertIsNotNone(self.mall.proof(self.obs,{'product':'gun-turret'}))
        self.live[_key(self.core['machines'][1])].products_finished=0
        self.assertIsNone(self.mall.proof(self.obs,{'product':'gun-turret'}))

    def test_native_cap_input_contamination_and_exact_actor_are_checked(self):
        self.assertTrue(execute_mall_stock_limit(self.game,self.config)['ok'])
        chest=self.live[_key(self.core['chest'])];chest.inv[1]=self.lua.globals().stack('gun-turret',9)
        self.assertEqual(self.mall._native(self.obs,self.entry,MALL_PROOF_LUA)['reason'],'mall_output_exceeds_configured_stock_target')
        chest.inv[1].valid_for_read=False;gear=self.live[_key(self.core['machines'][1])]
        gear.inv[1]=self.lua.globals().stack('copper-plate',1)
        self.assertEqual(self.mall._native(self.obs,self.entry,MALL_PROOF_LUA)['reason'],'mall_machine_input_contaminated')
        self.assertEqual(execute_mall_stock_limit(self.game,{**self.config,'expected_actor_unit':12})['reason'],'mall_actor_changed')

    def test_exact_slot_stock_transfer_conserves_items_and_rejects_metadata_or_foreign_unit(self):
        chest=self.live[_key(self.core['chest'])];chest.inv[1]=self.lua.globals().stack('gun-turret',8)
        action=mall_stock_action(self.game,self.obs,'gun-turret',3,[1]);result=execute_take_mall_stock(self.game,action)
        self.assertEqual(result['moved'],3,result);self.assertEqual(chest.inv[1].count,5);self.assertEqual(self.lua.globals().main[1].count,3)
        chest.inv[1].health=.5
        self.assertEqual(execute_take_mall_stock(self.game,action)['reason'],'mall_stock_source_contaminated')
        self.assertEqual(chest.inv[1].count,5)
        with self.assertRaises(ValueError):execute_take_mall_stock(self.game,{**action,'expected_entity_unit':999})

    def test_short_native_insert_only_debits_actual_exact_source_slot(self):
        chest=self.live[_key(self.core['chest'])];chest.inv[1]=self.lua.globals().stack('gun-turret',8)
        self.lua.execute('main.insert=function(q)main[1]=stack(q.name,2);return 2 end')
        result=execute_take_mall_stock(self.game,mall_stock_action(self.game,self.obs,'gun-turret',5,[1]))
        self.assertEqual(result['moved'],2,result);self.assertEqual(chest.inv[1].count,6)
        self.assertEqual(self.lua.globals().main[1].count,2)


if __name__=='__main__':unittest.main()
