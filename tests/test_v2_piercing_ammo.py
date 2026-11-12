from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.core.world_catalog import WorldCatalog
from factorio_agent.piercing_ammo import (PiercingAmmo,piercing_core,piercing_rates,audit_piercing_core,
    execute_configure_piercing,execute_supply_piercing_input,PIERCING_CONTROL_LUA,PIERCING_INSPECT_LUA,_key)

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


def catalog():
    return WorldCatalog({'version':'2.1.20','entities':{'assembling-machine-1':{'crafting_speed':.5}},
        'items':{'piercing-rounds-magazine':{'stack_size':100}},'recipes':{
        'firearm-magazine':{'energy':1,'ingredients':[{'name':'iron-plate','type':'item','amount':4}],
            'products':[{'name':'firearm-magazine','type':'item','amount':1}]},
        'piercing-rounds-magazine':{'energy':6,'ingredients':[{'name':'copper-plate','type':'item','amount':2},
            {'name':'steel-plate','type':'item','amount':1},{'name':'firearm-magazine','type':'item','amount':2}],
            'products':[{'name':'piercing-rounds-magazine','type':'item','amount':2}]}}})


class PiercingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.cat=catalog();self.core=piercing_core(self.cat,.5,.5)
        self.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root),expected_world='w',backend='assisted',query=Mock())
        self.bootstrap=Mock();self.builder=SimpleNamespace(bootstrap=self.bootstrap);self.perimeter=Mock()
        self.p=PiercingAmmo(self.game,self.builder,self.cat,self.perimeter)
        self.p.state=dict(world_id='w',catalog_fingerprint=self.cat.fingerprint,phase='feeding',core=self.core,plan=self.core,
            bindings={_key(q):i+100 for i,q in enumerate(self.core['entities'])},baseline={},defense_scope_id='factory',
            rated=self.core['rated'],input_logistics='assisted finite ordinary raw batch')
        self.obs={'ok':True,'world_id':'w','actor_unit_number':11,'inventory':{'iron-plate':1000,'copper-plate':100,'steel-plate':100},'entities':[]}
        self.save()

    def save(self):
        self.p._save();(self.root/'catalog.json').write_text(json.dumps(self.cat.to_dict()))
        (self.root/'pcb-board.json').write_text(json.dumps({'world_id':'w','catalog_fingerprint':self.cat.fingerprint,'blocks':{'piercing_ammo':self.core}}))

    def test_real_catalog_chain_rates_and_finite_material_cost_do_not_claim_steady_steel_supply(self):
        r=piercing_rates(self.cat)
        self.assertEqual((r['local_yellow_nameplate_per_minute'],r['red_nameplate_per_minute']),(60,40))
        self.assertEqual(r['raw_for_200_red'],{'iron-plate':800,'copper-plate':200,'steel-plate':100})
        self.assertEqual(r['raw_at_red_nameplate_per_minute']['steel-plate'],20)
        self.assertIsNone(r['measured_red_per_minute'])

    def test_all_six_machines_seventeen_arms_and_future_yellow_slots_pass_pure_geometry_power_wire_audit(self):
        self.assertTrue(audit_piercing_core(self.core)['ok']);self.assertEqual(len(self.core['machines']),6)
        self.assertEqual(len(self.core['arms']),17);self.assertEqual(len(self.core['yellow_machines']),2)
        broken=deepcopy(self.core);broken['poles']=[];self.assertFalse(audit_piercing_core(broken)['ok'])
        self.assertEqual(sum(q['caps'].get('iron-plate',0)for q in self.core['input_buffers']),256)

    def test_courier_handles_native_y_before_x_objects_and_never_requests_either_ammo_tier(self):
        native={'output_full_magazines':0,'input_buffers':[dict(position={'y':q['chest']['position']['y'],'x':q['chest']['position']['x']},
            unit_number=self.p.state['bindings'][_key(q['chest'])],contents={})for q in self.core['input_buffers']]}
        a=self.p._supply(self.obs,native)
        self.assertEqual(a['type'],'supply_piercing_input');self.assertIn(a['item'],('copper-plate','steel-plate','iron-plate'))
        self.assertNotEqual(a['item'],'firearm-magazine');self.assertLessEqual(a['count'],50)

    def test_actual_production_proof_finishes_before_optional_input_topoffs(self):
        self.p._bind=Mock(return_value=None);self.p._power=Mock(return_value={'ok':True})
        self.perimeter.ensure_coverage.return_value=None
        self.p._native=Mock(return_value={'ok':True})
        witness={'tick':200,'red_chest_stock':145,'machine_units':[1,2,3,4,5,6]}
        self.p.proof=Mock(return_value={'production_witness':witness})
        self.p._supply=Mock(return_value={'type':'supply_piercing_input','item':'iron-plate'})
        result=self.p.step(self.obs)
        self.assertEqual(result['status'],'succeeded');self.assertTrue(self.p.commissioned)
        self.assertEqual(self.p.state['production_witness'],witness)
        self.p._supply.assert_not_called()

    def test_known_file_input_envelope_rejects_ammo_foreign_chest_and_changed_units_before_io(self):
        q=self.core['input_buffers'][0];a=dict(type='supply_piercing_input',name='iron-chest',position=q['chest']['position'],
            item='iron-plate',count=50,expected_entity_unit=self.p.state['bindings'][_key(q['chest'])],expected_entity_world_id='w',
            expected_actor_unit=11,expected_catalog_fingerprint=self.cat.fingerprint)
        for change in({'item':'firearm-magazine'},{'item':'piercing-rounds-magazine'},{'count':True},{'count':51},
                {'expected_entity_unit':9999},{'position':{'x':999,'y':999}},{'expected_actor_unit':0}):
            with self.subTest(change=change),self.assertRaises(ValueError):execute_supply_piercing_input(self.game,{**a,**change})
        self.game.query.assert_not_called()

    def test_readonly_prepare_requires_actual_research_instead_of_unlocking_circuits(self):
        self.p.state={};result=self.p.prepare(self.obs)
        self.assertEqual(result['reason'],'piercing_factory_requires_normal_circuit_network_research')
        self.game.query.assert_not_called()

    def test_empty_output_provider_waits_for_separate_upkeep_and_never_returns_raw_courier_to_sweep(self):
        self.p.state['phase']='verified'
        self.p.proof=Mock(return_value={'native':{'actor_full_red':150,'output_full_magazines':0}})
        self.p._supply=Mock(return_value={'type':'supply_piercing_input'})
        action=self.p.ensure_full_magazines(self.obs,200)
        self.assertEqual(action['status'],'waiting');self.p._supply.assert_not_called()


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class NativePiercingTests(PiercingTests):
    def setUp(self):
        super().setUp();self.lua=LuaRuntime(unpack_returned_tuples=True)
        def table(v):
            if isinstance(v,dict):return self.lua.table_from({k:table(q)for k,q in v.items()})
            if isinstance(v,list):return self.lua.table_from([table(q)for q in v])
            return v
        self.table=table
        def decode(v):
            if not hasattr(v,'items'):return v
            d=dict(v.items())
            if d and set(d)==set(range(1,len(d)+1)):return [decode(d[i])for i in range(1,len(d)+1)]
            return{k:decode(q)for k,q in d.items()}
        self.lua.globals().helpers=self.lua.table_from({'json_to_table':lambda value:table(json.loads(value))})
        self.lua.execute('''d={world_id="w"};game={tick=100};f={technologies={["circuit-network"]={researched=true}}};s={};world={}
defines={inventory={chest=1},wire_connector_id={circuit_red=1,circuit_green=2},wire_origin={player=1},control_behavior={inserter={hand_read_mode={hold=1}}}}
prototypes={item={["piercing-rounds-magazine"]={stack_size=100,magazine_size=10,type="ammo"},["firearm-magazine"]={stack_size=100,magazine_size=10,type="ammo"},
 ["iron-plate"]={stack_size=100,type="item"},["steel-plate"]={stack_size=100,type="item"},["copper-plate"]={stack_size=100,type="item"}}}
function inventory(n)local inv={bar=n+1,valid=true};for i=1,n do inv[i]={valid_for_read=false}end
 inv.get_bar=function()return inv.bar end;inv.set_bar=function(v)inv.bar=v end
 inv.get_item_count=function(q)local count=0;for i=1,n do local st=inv[i];if st.valid_for_read and st.name==q.name and st.quality.name==q.quality then count=count+st.count end end;return count end
 inv.insert=function(q)for i=1,n do local st=inv[i];if not st.valid_for_read then inv[i]=stack(q.name,q.count);return q.count end end;return 0 end
 inv.remove=function(q)for i=1,n do local st=inv[i];if st.valid_for_read and st.name==q.name then local moved=math.min(q.count,st.count);st.count=st.count-moved;return moved end end;return 0 end
 return inv end
function stack(name,count)return {valid_for_read=true,name=name,count=count,ammo=10,health=1,spoil_tick=0,quality={name="normal"},prototype=prototypes.item[name]}end
main=inventory(20);a={valid=true,unit_number=11,force=f,surface=s,get_main_inventory=function()return main end}
function add(q,unit)
 local e={valid=true,name=q.name,position=q.position,unit_number=unit,direction=q.direction or 0,quality={name="normal"},force=f,surface=s,
  type=q.name=="small-electric-pole"and"electric-pole"or q.name=="assembling-machine-1"and"assembling-machine"or q.name=="iron-chest"and"container"or"inserter",
  energy=100,products_finished=0,cb={},conns={},inv=inventory(48),out=inventory(4),held_stack={valid_for_read=false}}
 e.is_connected_to_electric_network=function()return true end;e.get_recipe=function()return q.recipe and{name=q.recipe}end
 e.get_inventory=function()return e.inv end;e.get_output_inventory=function()return e.out end
 e.get_control_behavior=function()return e.cb end;e.get_or_create_control_behavior=function()return e.cb end
 e.get_transport_line=function()return{get_contents=function()return{}end}end
 e.get_wire_connector=function(id,create)
  if not e.conns[id]and create then local c={owner=e,wire_connector_id=id,connections={}}
   c.can_wire_reach=function(other)return(e.position.x-other.owner.position.x)^2+(e.position.y-other.owner.position.y)^2<=81 end
   c.is_connected_to=function(other)for _,link in pairs(c.connections)do if link.target==other then return true end end;return false end
   c.connect_to=function(other)if not c.is_connected_to(other)then c.connections[#c.connections+1]={target=other};other.connections[#other.connections+1]={target=c}end;return true end
   e.conns[id]=c
  end;return e.conns[id]end
 world[q.name..":"..q.position.x..":"..q.position.y]=e;return e end
target=function(p,name)return world[name..":"..p.x..":"..p.y]end
''')
        self.live={}
        for q in self.core['entities']:
            self.live[_key(q)]=self.lua.globals().add(table(q),self.p.state['bindings'][_key(q)])
        self.game.query=Mock(side_effect=lambda body:decode(self.lua.execute('return(function() '+' '.join(body.splitlines())+' end)()')))
        self.config=dict(type='configure_piercing_factory',expected_world_id='w',expected_actor_unit=11,expected_catalog_fingerprint=self.cat.fingerprint)

    def test_actual_lua_configures_native_bars_stock_conditions_wire_networks_and_local_yellow_limits(self):
        result=execute_configure_piercing(self.game,self.config);self.assertTrue(result['ok'],result)
        self.assertEqual(result['chest_bar_capacity'],200)
        proof=self.p._native(self.obs,PIERCING_CONTROL_LUA);self.assertTrue(proof['ok'],proof)
        self.assertEqual(self.live[_key(self.core['chest'])].inv.bar,3)
        for q in self.core['machines']:
            self.assertEqual(self.live[_key(q)].cb.circuit_condition.first_signal.name,'piercing-rounds-magazine')
            self.assertEqual(self.live[_key(q)].cb.circuit_condition.constant,200)
        self.assertTrue(all(self.live[_key(q)].inserter_stack_size_override==1 for q in self.core['arms']))

    def test_all_six_real_counters_must_advance_and_proof_does_not_latch_a_fake_production_witness(self):
        self.assertTrue(execute_configure_piercing(self.game,self.config)['ok'])
        self.p.state['baseline']={str(self.p.state['bindings'][_key(q)]):0 for q in self.core['machines']};self.save()
        for q in self.core['machines']:self.live[_key(q)].products_finished=1
        self.live[_key(self.core['chest'])].inv[1]=self.lua.globals().stack('piercing-rounds-magazine',2)
        self.perimeter.coverage_proof.return_value={'ok':True};before=self.p.path.read_bytes()
        proof=self.p.proof(self.obs);self.assertIsNotNone(proof);self.assertEqual(proof['native']['output_full_magazines'],2)
        self.assertEqual(self.p.path.read_bytes(),before)
        self.live[_key(self.core['yellow_machines'][0])].products_finished=0
        self.assertIsNone(self.p.proof(self.obs))

    def test_conserved_plate_courier_targets_owned_buffer_and_rejects_wrong_actor_or_cap_before_effect(self):
        buffer=self.core['input_buffers'][0];unit=self.p.state['bindings'][_key(buffer['chest'])]
        self.lua.globals().main[1]=self.lua.globals().stack('iron-plate',100)
        action=dict(type='supply_piercing_input',name='iron-chest',position=buffer['chest']['position'],item='iron-plate',count=50,
            expected_entity_unit=unit,expected_entity_world_id='w',expected_actor_unit=11,expected_catalog_fingerprint=self.cat.fingerprint)
        result=execute_supply_piercing_input(self.game,action);self.assertEqual(result['moved'],50,result)
        self.assertEqual(self.lua.globals().main[1].count,50)
        self.assertEqual(execute_supply_piercing_input(self.game,{**action,'expected_actor_unit':12})['moved'],0)
        dest=self.live[_key(buffer['chest'])].inv;dest[1].count=120
        self.assertEqual(execute_supply_piercing_input(self.game,action)['moved'],0)
        self.assertEqual(self.lua.globals().main[1].count,50)

    def test_wrong_recipe_changed_unit_and_changed_circuit_bar_fail_native_proof(self):
        self.assertTrue(execute_configure_piercing(self.game,self.config)['ok'])
        chest=self.live[_key(self.core['chest'])];chest.inv.bar=4
        self.assertFalse(self.p._native(self.obs,PIERCING_CONTROL_LUA)['ok'])
        chest.inv.bar=3;chest.unit_number=999
        self.assertFalse(self.p._native(self.obs,PIERCING_INSPECT_LUA)['ok'])

    def test_unexpected_opposite_color_network_cannot_inject_a_fake_stock_signal(self):
        self.assertTrue(execute_configure_piercing(self.game,self.config)['ok'])
        arm=self.live[_key(self.core['global_arms'][0])];pole=self.live[_key(self.core['poles'][0])]
        arm.get_wire_connector(2,True).connect_to(pole.get_wire_connector(2,True))
        result=self.p._native(self.obs,PIERCING_CONTROL_LUA)
        self.assertEqual(result['reason'],'piercing_foreign_or_unplanned_circuit_connection')


if __name__=='__main__':unittest.main()
