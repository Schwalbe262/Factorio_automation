from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_agent.board import entity
from factorio_agent.routing import route_trunks,VECTORS
from factorio_agent.petroleum import compile_pipe_crossings
from factorio_agent.joint_fluid_routing import trusted_prior,crossing_permission,validate_underpasses
from factorio_agent.chemical_production import fluid_crossing_proof

try:
    from lupa import LuaRuntime
except ImportError:LuaRuntime=None


class JointFluidTests(unittest.TestCase):
    def setUp(self):
        self.core={'product':'sulfur'};self.prior=[entity('pipe',.5,y)for y in(-1.5,-.5,.5,1.5,2.5)]
        self.halo={p for q in self.prior for p in{(q['position']['x'],q['position']['y'])}|
                   {(q['position']['x']+dx,q['position']['y']+dy)for dx,dy in VECTORS.values()}}
        route=dict(ok=True,entities=self.prior,independent_clear_cells=[list(p)for p in self.halo])
        self.context=trusted_prior('w',self.core,{'petroleum-gas':{'route':route}})

    def test_exact_own_three_cell_halo_is_usable_only_buried_with_straight_native_mouths(self):
        allowed,owners=crossing_permission(self.context,'w',self.core,self.prior,set(),set())
        row=route_trunks((-3.5,.5),(3.5,.5),start_direction=4,end_direction=4)
        compiled=compile_pipe_crossings(row,allowed,10);proof=validate_underpasses(compiled,allowed,owners)
        self.assertTrue(proof['ok'],proof);self.assertEqual(len(proof['crossings']),1)
        self.assertEqual(proof['crossings'][0]['pair'],{'first':[-1.5,.5],'last':[2.5,.5],'direction':4,'span':4})
        self.assertEqual(proof['crossings'][0]['pipe']['position'],{'x':.5,'y':.5})
        self.assertFalse(validate_underpasses(dict(compiled,underground_pairs=[]),allowed,owners)['ok'])

    def test_original_native_future_resource_and_core_hard_cells_are_never_cleared(self):
        for label in('native','future','resource','core'):
            hard={(-.5,.5)}
            allowed,_=crossing_permission(self.context,'w',self.core,self.prior,hard,set())
            self.assertFalse(allowed&hard,label)
        allowed,_=crossing_permission(self.context,'w',self.core,self.prior,set(),{('pipe',.5,.5)})
        self.assertNotIn((.5,.5),allowed)

    def test_forged_context_or_different_plan_does_not_grant_soft_cells(self):
        for context,world,core,prior in(({},'w',self.core,self.prior),(self.context,'other',self.core,self.prior),
            (self.context,'w',{'product':'other'},self.prior),(self.context,'w',self.core,self.prior[:-1])):
            with self.assertRaises(ValueError):crossing_permission(context,world,core,prior,set(),set())
        self.assertEqual(crossing_permission(None,'w',self.core,self.prior,set(),set()),(set(),{}))

    def test_changed_owned_crossing_native_unit_is_rejected_before_query(self):
        allowed,owners=crossing_permission(self.context,'w',self.core,self.prior,set(),set())
        compiled=compile_pipe_crossings(route_trunks((-3.5,.5),(3.5,.5),start_direction=4,end_direction=4),allowed,10)
        own=validate_underpasses(compiled,allowed,owners)['crossings']
        plan=dict(fluid_underground_pairs=compiled['underground_pairs'],owned_fluid_crossings=own,entities=self.prior)
        game=SimpleNamespace(query=Mock());obs={'world_id':'w','entities':[dict(entity('pipe',.5,.5),unit_number=99)]}
        result=fluid_crossing_proof(game,obs,plan,owned_bindings={'pipe:0.5:0.5':10})
        self.assertEqual(result['reason'],'chemical_owned_crossing_unit_changed');game.query.assert_not_called()
        changed=deepcopy(plan);changed['entities']=[]
        self.assertEqual(fluid_crossing_proof(game,obs,changed,owned_bindings={})['reason'],'chemical_owned_crossing_contract_invalid')

    def test_joint_prospective_preview_cannot_become_an_executable_chemical_checkpoint(self):
        from factorio_agent.chemical_production import ChemicalProduction
        obj=ChemicalProduction.__new__(ChemicalProduction);obj.state={'world_id':'w','catalog_fingerprint':'c',
            'products':{'sulfur':{'prospective_only':True}}};obj.catalog=SimpleNamespace(fingerprint='c')
        self.assertEqual(obj.step({'world_id':'w'},{'product':'sulfur'})['reason'],'chemical_prospective_preview_is_not_executable')

    def test_both_water_gas_owner_directions_but_no_other_fluid_get_the_same_guard(self):
        route=dict(ok=True,entities=self.prior,independent_clear_cells=[list(p)for p in self.halo])
        for fluid in('water','petroleum-gas'):
            context=trusted_prior('w',self.core,{fluid:{'route':route}})
            cells,owners=crossing_permission(context,'w',self.core,self.prior,set(),set())
            self.assertTrue(cells);self.assertEqual(owners[.5,.5]['fluid'],fluid)
        context=trusted_prior('w',self.core,{'crude-oil':{'route':route}})
        self.assertEqual(crossing_permission(context,'w',self.core,self.prior,set(),set()),(set(),{}))


@unittest.skipUnless(LuaRuntime,'optional executable Lua runtime')
class NativeOwnedCrossingTests(unittest.TestCase):
    def setUp(self):
        self.lua=LuaRuntime(unpack_returned_tuples=True)
        def table(v):
            if isinstance(v,dict):return self.lua.table_from({k:table(q)for k,q in v.items()})
            if isinstance(v,list):return self.lua.table_from([table(q)for q in v])
            return v
        self.lua.globals().helpers=self.lua.table_from({'json_to_table':lambda v:table(__import__('json').loads(v))})
        self.lua.execute('''d={world_id="w"};f={index=1,name="player"};s={};a={};resource=false;span=10
prototypes={entity={["pipe-to-ground"]={fluidbox_prototypes={{pipe_connections={{connection_type="underground",max_underground_distance=10}}}}},pipe={collision_mask={layers={}}}}}
e={name="pipe",type="pipe",valid=true,unit_number=10,force=f,surface=s,position={x=.5,y=.5},quality={name="normal"},fluid="petroleum-gas",
 prototype={collision_mask={colliding_with_tiles_only=false}}};e.get_fluid_contents=function()return{[e.fluid]=100}end
target=function(p,n)if n=="pipe"and p.x==.5 and p.y==.5 then return e end end
s.count_entities_filtered=function()return resource and 1 or 0 end;s.find_tiles_filtered=function()return{}end
s.find_entities_filtered=function(q)local b=q.area;if b[1][1]<=.5 and b[2][1]>=.5 and b[1][2]<=.5 and b[2][2]>=.5 then return{e}end;return{}end
''')
        self.game=SimpleNamespace(query=lambda body:dict(self.lua.execute('return(function()'+' '.join(body.splitlines())+'end)()').items()))
        pair={'first':[-1.5,.5],'last':[2.5,.5],'direction':4,'span':4};q=dict(entity('pipe',.5,.5),fluid='petroleum-gas',owner_revision='a'*64)
        self.plan=dict(entities=[entity('pipe',.5,.5)],fluid_underground_pairs=[pair],owned_fluid_crossings=[dict(schema='same-consumer-fluid-v1',pipe=q,pair=pair)])
        self.obs={'world_id':'w','entities':[dict(entity('pipe',.5,.5),unit_number=10)]}

    def run_guard(self):
        with patch('factorio_agent.chemical_production.mining_reservations',return_value=[]):
            return fluid_crossing_proof(self.game,self.obs,self.plan,owned_bindings={'pipe:0.5:0.5':10})

    def test_native_bound_gas_is_allowed_but_changed_unit_or_fluid_is_rejected_atomically(self):
        self.assertTrue(self.run_guard()['ok'])
        self.lua.execute('e.unit_number=11')
        self.assertEqual(self.run_guard()['reason'],'chemical_owned_crossing_unit_changed')
        self.lua.execute('e.unit_number=10;e.fluid="water"')
        self.assertEqual(self.run_guard()['reason'],'chemical_owned_crossing_fluid_changed')

    def test_native_resource_growth_still_blocks_the_owned_underpass(self):
        self.lua.execute('resource=true')
        self.assertEqual(self.run_guard()['reason'],'chemical_fluid_crossing_resource_changed')

    def test_native_reverse_direction_requires_its_saved_water_type_not_gas(self):
        self.plan['owned_fluid_crossings'][0]['pipe']['fluid']='water'
        self.lua.execute('e.fluid="water"');self.assertTrue(self.run_guard()['ok'])
        self.lua.execute('e.fluid="petroleum-gas"')
        self.assertEqual(self.run_guard()['reason'],'chemical_owned_crossing_fluid_changed')


if __name__=='__main__':unittest.main()
