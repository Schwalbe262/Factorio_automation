from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_agent.board import entity
from factorio_agent.paired_mining import mining_pair
from factorio_agent.mining_layout_policy import (make_policy,validate_policy,oriented_pair_core,capacity_budget,
    layout_contract,validate_extension_contract,candidate_sites,prepare_new_bank,prepare_extension,
    validate_retained_units,diagnose_bank_capacity,survey_patch,plan_offore_exit,PATCH_SURVEY_LUA,CAPACITY_DIAGNOSTIC_LUA)

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


def catalog():
    return SimpleNamespace(fingerprint='c',entities={**{r:{'mining_products':[{'name':r,'amount':1,'type':'item'}]}
        for r in('iron-ore','copper-ore','coal','stone')},'electric-mining-drill':{'mining_speed':.5},'transport-belt':{'belt_speed':.03125}})


def snapshot():
    return dict(ok=True,tick=100,world_id='w',catalog_fingerprint='c',resource='iron-ore',area=[[-32,-16],[16,24]],
        tiles=[{'name':'iron-ore','position':{'x':x+.5,'y':y+.5},'amount':100,'world_id':'w','surface_index':1}
               for x in range(-20,10)for y in range(-8,16)],surface_index=1,
        native=dict(mining_speed=.5,mining_radius=2.49,mining_time=1,belt_speed=.03125,force_productivity_bonus=.1))


class MiningPolicyTests(unittest.TestCase):
    def test_declared_flow_growth_and_four_tile_powered_grid_before_planning(self):
        policy=make_policy('w','c','iron-ore');validate_policy(policy,'w','c','iron-ore')
        self.assertEqual((policy['flow_direction'],policy['growth_direction'],policy['field_growth_direction']),(4,12,8))
        self.assertEqual((policy['pair_pitch'],policy['bank_pitch']),(4,8))
        broken=deepcopy(policy);broken['pair_pitch']=3
        with self.assertRaises(ValueError):validate_policy(broken,'w','c','iron-ore')
        self.assertFalse(make_policy('w','c','iron-ore',flow='south_to_north')['orientation_execution_supported'])

    def test_four_pure_orientations_preserve_footprints_but_only_east_executes(self):
        g=FactoryBuilder.__new__(FactoryBuilder)
        for flow in('west_to_east','east_to_west','north_to_south','south_to_north'):
            core=oriented_pair_core({'x':20.5,'y':30.5},3,'coal',flow)
            boxes=[g._occupied_by_plan([e])for e in core['entities']]
            self.assertEqual(sum(map(len,boxes)),len(set().union(*boxes)))
            self.assertEqual(core['ports'][0]['position'],{'x':20.5,'y':30.5})
            self.assertEqual(core['drill_count'],6)

    def test_productivity_and_physical_lane_limits_do_not_become_measured_rate(self):
        p=capacity_budget(30,14,14,belt_speed=.03125,force_productivity_bonus=.1)
        self.assertAlmostEqual(p['planned_per_drill_per_minute'],33)
        self.assertEqual((p['lane_capacity_per_minute'],p['belt_capacity_per_minute']),(450,900))
        self.assertAlmostEqual(p['source_nominal_per_minute'],924);self.assertEqual(p['collector_ceiling_per_minute'],900)
        self.assertEqual((p['drills_per_lane_without_clipping'],p['drills_per_lane_to_saturate']),(13,14))
        self.assertIsNone(p['measured_rate_per_minute'])
        unbalanced=capacity_budget(30,20,2,belt_speed=.03125,force_productivity_bonus=.1)
        self.assertEqual(unbalanced['collector_ceiling_per_minute'],516)
        with self.assertRaises(ValueError):capacity_budget(30,True,1,belt_speed=.03125)

    def test_patch_grid_scoring_counts_unique_ore_and_uses_eight_tile_bank_rows(self):
        s=snapshot();policy=make_policy('w','c','iron-ore');sites=candidate_sites(s,policy,1,[])
        self.assertTrue(sites);self.assertLessEqual(len(sites),12)
        for q in sites:
            self.assertEqual((q['x']-.5)%4,0);self.assertEqual((q['y']-.5)%8,0)
            self.assertGreaterEqual(q['reserved_pair_slots'],2)
            self.assertEqual(q['unique_resource_amount'],q['unique_resource_tiles']*100)
            self.assertLessEqual(q['unique_resource_tiles'],sum(q['row_resource_tiles'].values()))
        foreign=deepcopy(s);foreign['world_id']='other'
        with self.assertRaises(ValueError):candidate_sites(foreign,policy,1,[])

    def bank(self):
        plan=mining_pair(.5,.5,1,'iron-ore');bank=dict(plan=plan,reserved_pair_slots=3,reserved_area=[[-9,-1],[4,6]])
        bank['layout_contract']=layout_contract(bank,make_policy('w','c','iron-ore'));return bank

    def test_extension_keeps_every_asset_and_exact_port_and_rejects_contract_tampering(self):
        bank=self.bank();new=mining_pair(.5,.5,2,'iron-ore')
        self.assertTrue(validate_extension_contract(bank,new)['ok'])
        for change in('port','removed','rotate','undeclared','hash'):
            b=deepcopy(bank);p=deepcopy(new)
            if change=='port':p['ports'][0]['position']['x']+=1
            elif change=='removed':p['entities'].pop(next(i for i,e in enumerate(p['entities'])if e in bank['plan']['entities']))
            elif change=='rotate':p['entities'][0]['direction']=0
            elif change=='undeclared':p['entities'].append(entity('electric-mining-drill',100.5,100.5,8))
            else:b['layout_contract']['reserved_pair_slots']=4
            self.assertFalse(validate_extension_contract(b,p)['ok'],change)

    def test_non_east_execution_and_malformed_inputs_fail_without_native_io(self):
        g=SimpleNamespace(query=Mock());b=SimpleNamespace(catalog=catalog())
        obs={'world_id':'w','resources':{}}
        p=make_policy('w','c','coal',flow='north_to_south')
        r=prepare_new_bank(g,b,obs,'coal',2,[],policy=p)
        self.assertEqual(r['reason'],'mining_orientation_requires_native_exit_and_lane_proof_integration')
        g.query.assert_not_called()
        for n in(0,17,True):self.assertFalse(prepare_new_bank(g,b,obs,'coal',n,[])['ok'])

    def test_new_bank_wrapper_returns_fixed_offore_contract_and_keeps_apron_out_of_power(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        g=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(temp.name)),query=Mock())
        geometry=FactoryBuilder.__new__(FactoryBuilder);builder=SimpleNamespace(catalog=catalog(),_occupied_by_plan=geometry._occupied_by_plan)
        obs={'ok':True,'world_id':'w','tick':100,'entities':[],'resources':{'iron-ore':{'position':{'x':.5,'y':.5}}}}
        def outlet(game,builder,obs,core,future,reservations):
            p=deepcopy(core['ports'][0]);p['position']['x']+=4;p.pop('mining_resource',None)
            path=[{'x':core['ports'][0]['position']['x']+i,'y':p['position']['y']}for i in range(5)]
            es=core['entities']+[entity('transport-belt',q['x'],q['y'],4)for q in path[1:]]
            return dict(ok=True,entities=es,port=p,collector_port=core['ports'][0],path=path,
                output_access=[entity('transport-belt',p['position']['x']+i,p['position']['y'],4)for i in range(1,4)])
        with patch('factorio_agent.mining_layout_policy.survey_patch',return_value=snapshot()),\
             patch('factorio_agent.mining_layout_policy.preflight',return_value={'ok':True,'clear_obstacles':[]}),\
             patch('factorio_agent.mining_layout_policy.plan_offore_exit',side_effect=outlet),\
             patch('factorio_agent.mining_layout_policy.resource_survey',return_value={'ok':True,'rate_per_drill':30,'unique_remaining_resource':5000}),\
             patch('factorio_agent.mining_layout_policy.power_connection',return_value={'ok':True,'entities':[],'wire_hops':1})as power:
            ready=prepare_new_bank(g,builder,obs,'iron-ore',2,[])
        self.assertTrue(ready['ok'],ready);self.assertFalse(ready['ready']);self.assertAlmostEqual(ready['nominal_rate_per_minute'],66)
        self.assertNotIn('mining_resource',ready['plan']['ports'][0]);self.assertIn('collector_port',ready['plan'])
        self.assertTrue(ready['layout_contract']['output_resource_free_verified'])
        self.assertEqual(ready['plan']['layout_contract'],ready['layout_contract'])
        for p in ready['layout_contract']['output_access']:
            self.assertTrue(any(e['position']==p for e in power.call_args.args[4]))
        self.assertFalse(list(Path(temp.name).iterdir()))

    def test_extension_wrapper_retains_identity_and_existing_exit_instead_of_redrawing_it(self):
        bank=self.bank();bank['ready']=True
        source=deepcopy(bank['plan']['ports'][0]);outlet=deepcopy(source);outlet['position']={'x':4.5,'y':.5}
        bank['plan']['collector_port']=source;bank['plan']['ports']=[outlet]
        bank['plan']['collector_connection']=[];bank['layout_contract']=layout_contract(bank,make_policy('w','c','iron-ore'))
        obs={'ok':True,'world_id':'w','entities':[dict(e,unit_number=i+1)for i,e in enumerate(bank['plan']['entities'])]}
        g=SimpleNamespace(query=Mock(return_value={'ok':True,'belt_speed':.03125,'force_productivity_bonus':.1}))
        b=SimpleNamespace(catalog=catalog())
        with patch('factorio_agent.mining_layout_policy.preflight',return_value={'ok':True,'clear_obstacles':[]}),\
             patch('factorio_agent.mining_layout_policy.resource_survey',return_value={'ok':True,'rate_per_drill':30,'unique_remaining_resource':5000}):
            ready=prepare_extension(g,b,obs,bank,2)
        self.assertTrue(ready['ok'],ready);self.assertEqual(ready['plan']['ports'],bank['plan']['ports'])
        self.assertEqual(ready['plan']['collector_port'],source);self.assertEqual(ready['plan']['collector_connection'],[])
        self.assertTrue(validate_retained_units(obs,ready)['ok']);obs['entities'][0]['unit_number']+=1000
        self.assertFalse(validate_retained_units(obs,ready)['ok'])

    def test_extension_rejects_wrong_world_or_catalog_before_native_io(self):
        g=SimpleNamespace(query=Mock());bank=self.bank()
        for world,fingerprint in(('other','c'),('w','other')):
            result=prepare_extension(g,SimpleNamespace(catalog=SimpleNamespace(fingerprint=fingerprint)),
                                     {'world_id':world,'entities':[]},bank,2)
            self.assertEqual(result['reason'],'invalid_mining_extension_contract')
        g.query.assert_not_called()

    def test_diagnostic_separates_installed_exhausted_unpowered_and_timed_measurement(self):
        bank=self.bank();plan=bank['plan'];obs={'ok':True,'world_id':'w','entities':[dict(e,unit_number=i+1)for i,e in enumerate(plan['entities'])]}
        rows=[dict(side=side,position=plan['producer_rows'][side][0]['position'],present=True,unit_number=i+1,
            powered=side=='left',normal_quality=True,speed_bonus=0,productivity_bonus=.1,remaining_resource=2000,
            drop_lane_verified=True,working=side=='left')for i,side in enumerate(('left','right'))]
        g=SimpleNamespace(query=Mock(return_value=dict(ok=True,tick=100,drills=rows,mining_speed=.5,mining_time=1,belt_speed=.03125,force_productivity_bonus=.1)))
        result=diagnose_bank_capacity(g,obs,catalog(),bank)
        self.assertTrue(result['ok'],result);self.assertAlmostEqual(result['installed_collector_ceiling_per_minute'],66)
        self.assertAlmostEqual(result['active_collector_ceiling_per_minute'],33);self.assertIsNone(result['measured_rate_per_minute'])
        rows[0]['remaining_resource']=0;self.assertEqual(diagnose_bank_capacity(g,obs,catalog(),bank)['active_collector_ceiling_per_minute'],0)
        rows[0]['normal_quality']=False;self.assertIn(1,diagnose_bank_capacity(g,obs,catalog(),bank)['unsupported_drill_units'])

    def test_native_scripts_are_flattenable_readonly_and_survey_limits_are_before_io(self):
        for script in(PATCH_SURVEY_LUA,CAPACITY_DIAGNOSTIC_LUA):
            self.assertNotIn('--',script)
            for forbidden in('create_entity','destroy(','.amount=','.researched=','insert{'):self.assertNotIn(forbidden,script)
        g=SimpleNamespace(query=Mock())
        with self.assertRaises(ValueError):survey_patch(g,{'world_id':'w'},catalog(),'iron-ore',[[0,0],[129,1]])
        g.query.assert_not_called()


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class NativeMiningPolicyTests(unittest.TestCase):
    def setUp(self):
        self.lua=LuaRuntime(unpack_returned_tuples=True)
        def table(v):
            if isinstance(v,dict):return self.lua.table_from({k:table(a)for k,a in v.items()})
            if isinstance(v,list):return self.lua.table_from([table(a)for a in v])
            return v
        self.table=table
        self.lua.execute('''d={world_id="w"};f={mining_drill_productivity_bonus=.1};s={};game={tick=100};defines={entity_status={working=1}}
prototypes={entity={["electric-mining-drill"]={mining_speed=.5,mining_drill_radius=2.49},
 ["iron-ore"]={mineable_properties={mining_time=1}},["transport-belt"]={belt_speed=.03125}}}
drill={valid=true,unit_number=10,direction=8,position={x=.5,y=.5},force=f,surface=s,energy=100,quality={name="normal"},
 speed_bonus=0,productivity_bonus=.1,status=1,drop_position={x=.5,y=2.25},is_connected_to_electric_network=function()return true end}
belt={valid=true,direction=4,force=f,surface=s};remaining=1000;mixed=false
target=function(p,name)if name=="electric-mining-drill"then return drill end;if name=="transport-belt"then return belt end end
s.find_entities_filtered=function()return{{name=mixed and"copper-ore"or"iron-ore",amount=remaining}}end
''')

    def test_actual_bonus_and_physical_lane_receipt_with_identity_and_ore_guards(self):
        args=dict(world_id='w',resource='iron-ore',collector_y=2.5,
                  drills=[dict(name='electric-mining-drill',position={'x':.5,'y':.5},direction=8,unit_number=10,side='left')])
        self.lua.globals().x=self.table(args)
        run=lambda:self.lua.execute('return(function()'+' '.join(CAPACITY_DIAGNOSTIC_LUA.splitlines())+'end)()')
        result=run();self.assertTrue(result['ok']);row=result['drills'][1]
        self.assertTrue(row.powered);self.assertTrue(row.drop_lane_verified);self.assertEqual(row.productivity_bonus,.1)
        self.lua.execute('drill.unit_number=11');self.assertEqual(run()['reason'],'mining_capacity_native_drill_identity_changed')
        self.lua.execute('drill.unit_number=10;mixed=true');self.assertEqual(run()['reason'],'mining_capacity_mixed_resource')
        self.lua.execute('mixed=false;remaining=0;drill.energy=0;drill.drop_position.y=2.75')
        row=run()['drills'][1];self.assertFalse(row.powered);self.assertFalse(row.drop_lane_verified);self.assertEqual(row.remaining_resource,0)

    def test_shared_native_exit_selects_first_clear_fixed_east_port_and_preserves_core_scope(self):
        self.lua.execute('''helpers={};a={};f.name="player";blocked=false
defines.build_check_type={manual_ghost=1};prototypes.entity["transport-belt"].collision_mask={layers={object=true}}
target=function()return nil end
s.get_tile=function()return{collides_with=function()return false end}end
s.can_place_entity=function()return true end
local neutral={name="neutral"}
s.find_entities_filtered=function(q)local rows={};local area=q.area;if not area then return rows end
 for x=-10,5 do for y=-3,7 do local p={x=x+.5,y=y+.5}
  if p.x>=area[1][1]and p.x<=area[2][1]and p.y>=area[1][2]and p.y<=area[2][2]then
   rows[#rows+1]={name="coal",type="resource",position=p,amount=100,force=neutral}
  end
 end end
 if blocked and area[1][1]<=3.5 and area[2][1]>=3.5 and area[1][2]<=2.5 and area[2][2]>=2.5 then
  rows[#rows+1]={name="small-electric-pole",type="electric-pole",position={x=3.5,y=2.5},unit_number=99,
   force=f,prototype={collision_mask={colliding_with_tiles_only=false}}}
 end
 return rows end
''')
        self.lua.globals().helpers.json_to_table=lambda raw:self.table(json.loads(raw))
        def python(v):
            if not hasattr(v,'items'):return v
            rows=dict(v.items())
            if rows and set(rows)==set(range(1,len(rows)+1)):return [python(rows[i])for i in range(1,len(rows)+1)]
            return {k:python(a)for k,a in rows.items()}
        game=SimpleNamespace(query=lambda body:python(self.lua.execute('return(function()'+' '.join(body.splitlines())+'end)()')))
        core=mining_pair(.5,.5,1,'coal');future=mining_pair(.5,.5,3,'coal')
        with patch('factorio_agent.mining_layout_policy.preflight',return_value={'ok':True,'clear_obstacles':[]}):
            ready=plan_offore_exit(game,None,{'world_id':'w'},core,future,[])
            self.assertTrue(ready['ok'],ready);self.assertEqual(ready['port']['position'],{'x':6.5,'y':2.5})
            self.assertNotIn('mining_resource',ready['port']);self.assertEqual(ready['exit_length'],3)
            source=next(e for e in ready['entities']if e['name']=='transport-belt'and e['position']==core['ports'][0]['position'])
            self.assertIn('mining_footprint',source);self.assertNotIn('mining_exit',source)
            last=next(e for e in ready['entities']if e['position']==ready['port']['position'])
            self.assertNotIn('mining_resource',last);self.assertEqual(last['direction'],4)
            self.lua.execute('blocked=true');blocked=plan_offore_exit(game,None,{'world_id':'w'},core,future,[])
            self.assertFalse(blocked['ok']);self.assertEqual(blocked['reason'],'no_safe_bounded_mining_exit')


if __name__=='__main__':unittest.main()
