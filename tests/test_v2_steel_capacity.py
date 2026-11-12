from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_ai.deterministic_builder import FactoryBuilder,_plan
from factorio_agent.board import entity
from factorio_agent.bus_tap import signature
from factorio_agent.steel_production import steel_core
from factorio_agent.steel_capacity import (SteelCapacity,additive_layout,verify_steel_capacity,validate_parameters,
    NEW_FURNACE_LUA,MODE,_hash,_source,_bound,_key)

try:
    from lupa import LuaRuntime
except ImportError:LuaRuntime=None


def catalog():
    return SimpleNamespace(fingerprint='catalog',recipes={'steel-plate':dict(energy=16,categories=['smelting'],
        ingredients=[dict(name='iron-plate',type='item',amount=5)],products=[dict(name='steel-plate',type='item',amount=1)])},
        entities={'stone-furnace':dict(burner=True,crafting_speed=1,crafting_categories=['smelting'],energy_usage=1500),
                  'inserter':{'energy_usage':220}},items={'coal':{'fuel_value':4000000}})


def observed(core,previous=None):
    previous={signature(q):q for q in(previous or{}).get('entities',[])}
    next_unit=max([q['unit_number']for q in previous.values()]+[999])+1;rows=[]
    for q in core['entities']:
        unit=previous.get(signature(q),{}).get('unit_number')
        if unit is None:unit=next_unit;next_unit+=1
        rows.append(dict(q,unit_number=unit))
    return dict(world_id='w',tick=100,inventory={},entities=rows)


def receipt(old,new,oldobs,newobs):
    row=dict(mode=MODE,world_id='w',catalog_fingerprint='catalog',old_plan_revision=_hash(old),new_plan_revision=_hash(new),
        old_plan=old,new_plan=new,source=_source(old,newobs),retained_units=[_bound(q)for q in oldobs['entities']],
        old_furnace_units=[q['unit_number']for q in oldobs['entities']if q['name']=='stone-furnace'],
        current_furnace_units=[q['unit_number']for q in newobs['entities']if q['name']=='stone-furnace'],
        native_output_lanes={'left':{'steel-plate':4},'right':{'steel-plate':4}})
    row['added_furnace_units']=list(set(row['current_furnace_units'])-set(row['old_furnace_units']))
    row['production_witness']={'ok':True,'furnaces':[dict(unit_number=u,products_finished=1)for u in row['added_furnace_units']]}
    row['receipt_id']=_hash(row);return row


class LayoutTests(unittest.TestCase):
    def test_both_expansions_are_strictly_additive_with_fixed_ports_area_and_old_units(self):
        c=catalog();old=steel_core(c,98,-40)
        for count,rate,added in[(2,15,16),(3,22.5,32)]:
            row=additive_layout(c,old,old,count)
            self.assertEqual(row['core']['rated']['steel_per_minute'],rate);self.assertEqual(len(row['added']),added)
            self.assertEqual(row['plan']['ports'],old['ports']);self.assertEqual(row['core']['reserved_area'],old['reserved_area'])
            self.assertTrue({signature(q)for q in old['entities']}<={signature(q)for q in row['plan']['entities']})
            self.assertFalse(any(q['name']in('splitter','underground-belt')for q in row['added']))

    def test_cold_plan_contains_furnaces_outputs_and_headers_but_no_new_input_arms(self):
        old=steel_core(catalog(),98,-40);row=additive_layout(catalog(),old,old,2)
        self.assertEqual(len(row['new_input_arms']),2);self.assertEqual(len(row['cold_plan']['entities']),14)
        self.assertTrue(all(q in row['cold_plan']['entities']for q in row['new_furnaces']))
        self.assertFalse(any(q in row['cold_plan']['entities']for q in row['new_input_arms']))

    def test_moved_legacy_header_or_invalid_rate_is_rejected(self):
        old=steel_core(catalog(),98,-40);old['entities'][0]['position']['x']+=1
        with self.assertRaises(ValueError):additive_layout(catalog(),old,old,2)
        for value in(True,float('nan'),0,-1):
            with self.assertRaises(ValueError):validate_parameters({'rate_per_minute':value})


class AncestryTests(unittest.TestCase):
    def setUp(self):
        self.cat=catalog();self.old=steel_core(self.cat,98,-40);self.oldobs=observed(self.old)
        self.new=steel_core(self.cat,98,-40,per_side_count=2);self.obs=observed(self.new,self.oldobs)
        self.row=receipt(self.old,self.new,self.oldobs,self.obs)
        self.steel=SimpleNamespace(state=dict(world_id='w',catalog_fingerprint='catalog',phase='verified',core=self.new,plan=self.new,capacity_revisions=[self.row]),proof=Mock())
        units=[q['unit_number']for q in self.obs['entities']if q['name']=='stone-furnace']
        self.steel.proof.return_value={'furnace_units':units,'rated':self.new['rated'],'output_buffers_present':False}
        self.kw=dict(pinned_plan_revision=_hash(self.old),pinned_source=_source(self.old,self.oldobs),pinned_furnace_units=self.row['old_furnace_units'])

    def check(self):return verify_steel_capacity(None,self.obs,self.cat,self.steel,**self.kw)

    def test_original_consumer_hash_and_two_furnaces_remain_pinned_while_all_four_are_proved(self):
        before=deepcopy(self.kw);result=self.check();self.assertTrue(result['ok'],result)
        self.assertEqual(self.kw,before);self.assertEqual(len(result['original_furnace_units']),2);self.assertEqual(len(result['current_furnace_units']),4)
        self.assertEqual(len(result['chain']),1);self.assertFalse(result['proof']['output_buffers_present'])
        self.steel.proof.assert_called_once_with(self.obs,consumer_furnace_units=result['current_furnace_units'])

    def test_second_additive_revision_is_a_chain_not_blind_hash_replacement(self):
        third=steel_core(self.cat,98,-40,per_side_count=3);obs3=observed(third,self.obs)
        row2=receipt(self.new,third,self.obs,obs3);self.steel.state.update(core=third,plan=third,capacity_revisions=[self.row,row2]);self.obs=obs3
        self.steel.proof.return_value={'furnace_units':[q['unit_number']for q in obs3['entities']if q['name']=='stone-furnace'],'rated':third['rated']}
        result=self.check();self.assertTrue(result['ok'],result);self.assertEqual(len(result['chain']),2)
        self.assertEqual(result['rated']['steel_per_minute'],22.5)

    def test_unreceipted_plan_or_changed_original_unit_or_output_is_rejected(self):
        saved=deepcopy(self.steel.state)
        self.steel.state['capacity_revisions']=[];self.assertFalse(self.check()['ok']);self.steel.state=saved
        next(q for q in self.obs['entities']if q['unit_number']==self.kw['pinned_furnace_units'][0])['unit_number']=90000
        self.assertFalse(self.check()['ok']);self.obs=observed(self.new,self.oldobs)
        self.kw['pinned_source']=dict(self.kw['pinned_source'],unit_number=90000);self.assertFalse(self.check()['ok'])

    def test_missing_retained_bindings_empty_lane_witness_and_receipt_digest_tampering_fail(self):
        original=deepcopy(self.row)
        for mutate in(lambda r:r['retained_units'].pop(),lambda r:r['native_output_lanes']['left'].clear(),lambda r:r.update(source={})):
            row=deepcopy(original);mutate(row);row['receipt_id']=_hash({k:v for k,v in row.items()if k!='receipt_id'})
            self.steel.state['capacity_revisions']=[row];self.assertFalse(self.check()['ok'])
        self.steel.state['capacity_revisions']=[dict(original,receipt_id='forged')];self.assertFalse(self.check()['ok'])


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.c=catalog();old=steel_core(self.c,98,-40);layout=additive_layout(self.c,old,old,2);self.obs=observed(layout['core'],observed(old))
        self.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root),query=Mock())
        self.builder=SimpleNamespace(ensure_plan=Mock(return_value={'status':'succeeded'}))
        self.perimeter=SimpleNamespace(ensure_coverage=Mock(return_value=None),coverage_proof=Mock(return_value={'ok':True}))
        self.p=SteelCapacity(self.game,self.builder,self.c,self.perimeter,steel=SimpleNamespace(state={}))
        oldobs=observed(old)
        self.p.state=dict(layout,world_id='w',catalog_fingerprint='catalog',phase='cold',parameters={'rate_per_minute':15},
            old_state={'core':old},retained_units=[_bound(q)for q in oldobs['entities']],old_furnace_units=[],source=_source(old,self.obs),
            bindings={},baseline={},scope_id='industrial:steel',post_defense_checked=True)
        self.p._upstream=Mock(return_value={'ok':True});self.p._save=Mock()

    def test_no_input_arm_is_authorized_until_actual_new_furnaces_are_empty_zero_baseline(self):
        units=[q['unit_number']for q in self.obs['entities']if q['name']=='stone-furnace'and q['position']in[x['position']for x in self.p.state['new_furnaces']]]
        self.p._native_new=Mock(return_value={'ok':True,'furnaces':[dict(unit_number=u,products_finished=1,empty=False)for u in units]})
        result=self.p.step(self.obs,{'rate_per_minute':15});self.assertEqual(result['reason'],'steel_capacity_empty_new_furnace_baseline_required')
        self.assertEqual(self.builder.ensure_plan.call_count,1);self.assertEqual(self.p.state['phase'],'cold')

    def test_empty_baseline_permits_ordinary_feed_but_does_not_fake_production(self):
        units=[q['unit_number']for q in self.obs['entities']if q['name']=='stone-furnace'and q['position']in[x['position']for x in self.p.state['new_furnaces']]]
        self.p._native_new=Mock(return_value={'ok':True,'furnaces':[dict(unit_number=u,products_finished=0,empty=True)for u in units]})
        self.p.proof=Mock(return_value=None)
        result=self.p.step(self.obs,{'rate_per_minute':15});self.assertEqual(result['status'],'waiting');self.assertEqual(self.p.state['phase'],'feed')
        self.assertEqual(self.builder.ensure_plan.call_count,2);self.assertEqual(len(self.p.state['baseline']),2)

    def test_lost_original_units_block_before_build_or_source_requests(self):
        original=self.p.state['retained_units'][0]['unit_number'];obs=dict(self.obs,entities=[q for q in self.obs['entities']if q['unit_number']!=original])
        self.assertEqual(self.p.step(obs,{'rate_per_minute':15})['reason'],'steel_capacity_retained_unit_changed')
        self.builder.ensure_plan.assert_not_called();self.p._upstream.assert_not_called()

    def test_three_authority_publication_is_durable_idempotent_and_does_not_change_source_ports(self):
        old=self.p.state['old_state']['core'];oldobs=observed(old)
        state=dict(world_id='w',catalog_fingerprint='catalog',phase='verified',core=old,plan=old,sources=[])
        docs={'steel':state,'board':dict(world_id='w',catalog_fingerprint='catalog',blocks={'steel_production':old}),
              'industrial':dict(world_id='w',catalog_fingerprint='catalog',producer_authorizations={'steel':{'plan_revision':_hash(old)}})}
        names={'steel':'steel-production.json','board':'pcb-board.json','industrial':'industrial-operations.json'}
        for key,name in names.items():(self.root/name).write_text(json.dumps(docs[key]))
        self.p._docs=lambda:{key:json.loads((self.root/name).read_text())for key,name in names.items()}
        self.p._save=SteelCapacity._save.__get__(self.p)
        self.p.state['old_state']=state;self.p.state['old_furnace_units']=[q['unit_number']for q in oldobs['entities']if q['name']=='stone-furnace']
        self.p.state['budget']={'iron_additive_delta':37.5}
        units=[q['unit_number']for q in self.obs['entities']if q['name']=='stone-furnace']
        new_units=list(set(units)-set(self.p.state['old_furnace_units']))
        proof=dict(production={'furnace_units':units,'native_output_lanes':{'left':{'steel-plate':1},'right':{'steel-plate':1}}},
            new_furnace_units=new_units,new_furnace_witness={'ok':True,'furnaces':[dict(unit_number=u,products_finished=1)for u in new_units]})
        from factorio_agent import steel_capacity as module
        atomic=module._atomic_json;calls=[]
        def interrupted(path,value):
            if Path(path).name in names.values():
                calls.append(Path(path).name)
                if len(calls)==2:raise OSError('known first document completed')
            return atomic(path,value)
        with patch.object(module,'_atomic_json',side_effect=interrupted),self.assertRaises(OSError):self.p._publish(self.obs,proof)
        saved=json.loads(self.p.path.read_text());self.assertIn('publication_intent',saved)
        self.assertIsNone(self.p._publish(self.obs,{}));self.assertTrue(self.p.state['published'])
        published=json.loads((self.root/'steel-production.json').read_text())
        self.assertEqual(published['plan']['ports'],old['ports']);self.assertEqual(len(published['capacity_revisions']),1)
        self.assertIsNone(self.p._publish(self.obs,{}));self.assertEqual(len(self.p.steel.state['capacity_revisions']),1)
        board=json.loads((self.root/'pcb-board.json').read_text());self.assertEqual(board['consumer_capacity_allocations'][0]['delta_per_minute'],37.5)
        board['foreign']='change';(self.root/'pcb-board.json').write_text(json.dumps(board))
        self.assertEqual(self.p._publish(self.obs,{})['reason'],'steel_capacity_publication_compare_and_swap_failed')


class BudgetTests(unittest.TestCase):
    def test_capacity_deltas_stay_outside_fixed_source_descriptor_and_shared_coal_is_not_multiplied(self):
        with tempfile.TemporaryDirectory()as directory:
            root=Path(directory);c=catalog();old=steel_core(c,98,-40,per_side_count=2);new=steel_core(c,98,-40,per_side_count=3)
            iron=dict(item='iron-plate',position={'x':12.5,'y':-117.5});coal=dict(item='coal',material_supply_id='shared',position={'x':108.5,'y':32.5})
            game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root));p=SteelCapacity(game,Mock(),c,Mock(),steel=SimpleNamespace(state={'sources':[iron,coal]}))
            values={'bus-factory.json':dict(world_id='w',catalog_fingerprint='catalog',bus={'ports':[dict(iron,priority='surplus',demand_per_minute=37.5),dict(item='iron-plate',priority='critical',demand_per_minute=608.5)],'capacity':{'materials':{'iron-plate':{'supply_ceiling_per_minute':375}}}}),
                'material-sources.json':dict(world_id='w',catalog_fingerprint='catalog',sources={'steel-coal':coal}),
                'material-supply.json':dict(world_id='w',catalog_fingerprint='catalog',supplies={'coal':{'phase':'verified','material_supply_id':'shared','bank':{}}}),
                'brick-production.json':dict(world_id='w',catalog_fingerprint='catalog',phase='verified',sources=[coal],core={'rated':{'coal_per_minute_at_max_catalog_usage':2.7}}),
                'pcb-board.json':{'consumer_capacity_allocations':[dict(receipt_id='steel15',consumer='steel_production',
                    item='iron-plate',old_per_minute=37.5,new_per_minute=75,delta_per_minute=37.5)]}}
            values['pcb-board.json']['shared_main_bus']=deepcopy(values['bus-factory.json']['bus'])
            for name,value in values.items():(root/name).write_text(json.dumps(value))
            original=deepcopy(iron)
            with patch('factorio_agent.steel_capacity.diagnose_bank_capacity',return_value={'ok':True,'all_installed_drop_lanes_verified':True,'active_collector_ceiling_per_minute':66}):
                budget=p._upstream({'world_id':'w'},new,old)
            self.assertTrue(budget['ok']);self.assertEqual(budget['iron_additive_delta'],37.5);self.assertEqual(budget['all_claimed_iron_peak_after'],721)
            self.assertEqual(budget['guaranteed_surplus_per_minute'],0);self.assertEqual(budget['shared_coal_potential'],66)
            self.assertAlmostEqual(budget['coal_required_with_brick'],10.8);self.assertEqual(iron,original)
            with patch('factorio_agent.steel_capacity.diagnose_bank_capacity',return_value={'ok':True,'all_installed_drop_lanes_verified':True,'active_collector_ceiling_per_minute':5}):
                self.assertEqual(p._upstream({'world_id':'w'},new,old)['reason'],'expand_shared_industrial_coal_before_steel_capacity')


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class NativeBaselineTests(unittest.TestCase):
    def test_actual_normal_empty_zero_furnace_is_distinguished_from_production_and_quality_contamination(self):
        lua=LuaRuntime(unpack_returned_tuples=True)
        lua.execute('''x={world_id="w",furnaces={{position={x=102,y=-40},unit_number=1}}};d={world_id="w"};f={};s={};game={tick=10};defines={inventory={crafter_input=1}}
input={{valid_for_read=false}};output={{valid_for_read=false}};fuel={{valid_for_read=false}}
e={unit_number=1,force=f,surface=s,quality={name="normal"},products_finished=0};e.get_inventory=function(index)assert(index==defines.inventory.crafter_input,"actual 2.1 inventory enum required");return input end;e.get_output_inventory=function()return output end;e.get_fuel_inventory=function()return fuel end;target=function()return e end''')
        result=lua.execute(NEW_FURNACE_LUA);self.assertTrue(result['ok']);self.assertTrue(result['furnaces'][1]['empty'])
        lua.execute('fuel[1]={valid_for_read=true,name="coal",count=8,quality={name="normal"},health=1,spoil_tick=0}')
        result=lua.execute(NEW_FURNACE_LUA);self.assertTrue(result['ok']);self.assertTrue(result['furnaces'][1]['empty'])
        self.assertEqual(result['furnaces'][1]['fuel_items'],8)
        lua.execute('fuel[1].quality.name="uncommon"')
        self.assertEqual(lua.execute(NEW_FURNACE_LUA)['reason'],'steel_capacity_new_furnace_contaminated')
        lua.execute('fuel[1].quality.name="normal";input[1]={valid_for_read=true,name="iron-plate",count=1,quality={name="normal"},health=1,spoil_tick=0}')
        self.assertFalse(lua.execute(NEW_FURNACE_LUA)['furnaces'][1]['empty'])
        lua.execute('input[1]={valid_for_read=false}')
        lua.execute('output[1]={valid_for_read=true,name="steel-plate",count=1,quality={name="normal"},health=1,spoil_tick=0};e.products_finished=1')
        result=lua.execute(NEW_FURNACE_LUA);self.assertTrue(result['ok']);self.assertFalse(result['furnaces'][1]['empty'])
        lua.execute('output[1].quality.name="uncommon"')
        self.assertEqual(lua.execute(NEW_FURNACE_LUA)['reason'],'steel_capacity_new_furnace_contaminated')


if __name__=='__main__':unittest.main()
