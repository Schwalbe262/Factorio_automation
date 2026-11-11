from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_ai.deterministic_builder import _plan
from factorio_agent.board import entity
from factorio_agent.power_layout import V2FactoryBuilder,compact_coal_cell
from factorio_agent.resource_policy import MINING_NAMES
from factorio_agent.smelting import port

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


class StarterCoalPolicyTests(unittest.TestCase):
    def builder(self,legal=None):
        builder=V2FactoryBuilder.__new__(V2FactoryBuilder)
        builder.state={'world_id':'w','seeds':{}}
        builder.path=Path('unused')/'factory-builder.json'
        builder.game=SimpleNamespace(observe=Mock(return_value={'world_id':'w','entities':[]}),
                                     query=Mock(return_value={'ok':True,'legal':[2] if legal is None else legal}))
        builder.coal_sites=Mock(return_value=[{'x':0,'y':0},{'x':10,'y':0}])
        builder._save=Mock();builder._sync=Mock()
        return builder

    def power(self):
        return _plan([entity('transport-belt',30.5,10.5,4),entity('boiler',34,8)],
            [port('coal',30.5,10.5,4),{'kind':'power','position':{'x':32.5,'y':10.5}}])

    def route(self):
        return _plan([],[],underground_pairs=[],clear_obstacles=[],route_metrics={'turns':2,'length':30,'manhattan_distance':30})

    def test_interior_cell_is_filtered_before_any_route_or_whole_plan_is_selected(self):
        builder=self.builder()
        with patch('factorio_agent.planning.preflight',return_value={'ok':True,'clear_obstacles':[]}) as preflight, \
             patch('factorio_agent.transport.plan_connection',return_value=self.route()) as route:
            result=builder._choose_coal_plan(self.power())
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['drill']['position'],{'x':10,'y':0})
        self.assertEqual(route.call_args.args[3]['mining_resource'],'coal')
        self.assertEqual(route.call_count,1)
        self.assertEqual(preflight.call_count,2)
        self.assertNotIn('coal_plan',builder.state)
        builder._save.assert_not_called()
        script=builder.game.query.call_args.args[0]
        encoded=script.split('local args=helpers.json_to_table(',1)[1].split(');local legal=',1)[0]
        specs=json.loads(json.loads(encoded))['boundaries']
        self.assertTrue(all('mining_resource' not in spec and 'resource_preserved_unit' not in spec for pair in specs for spec in pair))
        self.assertNotIn('burner-inserter',MINING_NAMES)

    def test_whole_plan_failure_tries_next_boundary_without_latching_bad_plan(self):
        builder=self.builder([1,2])
        checks=[{'ok':True,'clear_obstacles':[]},{'ok':False,'reason':'new_entity_over_resource_or_mining_reservation'},
                {'ok':True,'clear_obstacles':[]},{'ok':True,'clear_obstacles':[]}]
        with patch('factorio_agent.planning.preflight',side_effect=checks), \
             patch('factorio_agent.transport.plan_connection',return_value=self.route()) as route:
            result=builder._choose_coal_plan(self.power())
        self.assertTrue(result['ok'])
        self.assertEqual(result['drill']['position'],{'x':10,'y':0})
        self.assertEqual(route.call_count,2)
        builder._save.assert_not_called()

    def test_failed_new_selection_is_not_persisted_by_startup(self):
        builder=self.builder([]);builder.state['power_plan']=self.power()
        builder.ensure_plan=Mock(return_value={'status':'succeeded'})
        result=builder.ensure_power({'world_id':'w','technologies':{'steam-power':True}})
        self.assertEqual(result['status'],'blocked')
        self.assertNotIn('coal_plan',builder.state)
        builder._save.assert_not_called()
        builder.ensure_plan.assert_called_once()

    def test_failed_route_never_returns_a_buildable_partial_cell(self):
        builder=self.builder()
        with patch('factorio_agent.planning.preflight',return_value={'ok':True,'clear_obstacles':[]}), \
             patch('factorio_agent.transport.plan_connection',return_value={'ok':False,'reason':'no_clean_trunk_corridor'}):
            result=builder._choose_coal_plan(self.power())
        self.assertFalse(result['ok'])
        self.assertNotIn('entities',result)
        self.assertEqual(result['failures'][0]['stage'],'route')

    def test_impossible_routes_stop_at_explicit_attempt_budget_with_recent_failures(self):
        builder=self.builder(list(range(1,21)))
        builder.coal_sites.return_value=[{'x':10*i,'y':0} for i in range(20)]
        with patch('factorio_agent.planning.preflight',return_value={'ok':True,'clear_obstacles':[]}), \
             patch('factorio_agent.transport.plan_connection',return_value={'ok':False,'reason':'no_clean_trunk_corridor'}) as route, \
             patch('factorio_agent.power_layout.time.monotonic',return_value=0):
            result=builder._choose_coal_plan(self.power())
        self.assertEqual(result['reason'],'starter_coal_planning_budget_exhausted')
        self.assertEqual((route.call_count,result['route_attempts'],result['route_attempt_budget']),(10,10,10))
        self.assertEqual(result['boundary_candidates'],20)
        self.assertEqual(result['candidates_examined'],10)
        self.assertEqual([row['candidate'] for row in result['failures']],list(range(3,11)))
        self.assertNotIn('coal_plan',builder.state);builder._save.assert_not_called()

    def test_deadline_stops_between_queries_before_another_candidate_or_route(self):
        builder=self.builder();clock=[0]
        def slow_survey(script):
            clock[0]=21
            return {'ok':True,'legal':[2]}
        builder.game.query.side_effect=slow_survey
        with patch('factorio_agent.power_layout.time.monotonic',side_effect=lambda:clock[0]), \
             patch('factorio_agent.planning.preflight') as preflight, \
             patch('factorio_agent.transport.plan_connection') as route:
            result=builder._choose_coal_plan(self.power())
        self.assertEqual(result['reason'],'starter_coal_planning_budget_exhausted')
        self.assertEqual((result['route_attempts'],result['planning_seconds_budget'],result['elapsed_seconds']),(0,20,21))
        self.assertEqual(builder.game.query.call_count,1)
        preflight.assert_not_called();route.assert_not_called();builder._save.assert_not_called()

    def test_saved_working_coal_plan_is_preserved_without_resurvey_or_relocation(self):
        builder=self.builder();saved=compact_coal_cell({'x':4,'y':8})
        builder.state['coal_plan']=deepcopy(saved)
        self.assertEqual(builder._choose_coal_plan(self.power()),saved)
        builder.game.observe.assert_not_called();builder.game.query.assert_not_called()
        builder.coal_sites.assert_not_called();builder._save.assert_not_called()

    def test_saved_incompatible_plan_blocks_startup_without_deleting_or_reselecting(self):
        builder=self.builder();saved=compact_coal_cell({'x':4,'y':8})
        builder.state.update(power_plan=self.power(),coal_plan=deepcopy(saved))
        builder.ensure_plan=Mock(side_effect=[{'status':'succeeded'},
                                {'status':'blocked','reason':'new_entity_over_resource_or_mining_reservation'}])
        with patch.object(builder,'_choose_coal_plan') as choose:
            result=builder.ensure_power({'world_id':'w','technologies':{'steam-power':True}})
        self.assertEqual(result['reason'],'new_entity_over_resource_or_mining_reservation')
        self.assertEqual(builder.state['coal_plan'],saved)
        choose.assert_not_called();builder._save.assert_not_called()

    def test_existing_working_power_verification_keeps_saved_coal_plan(self):
        builder=self.builder();saved=compact_coal_cell({'x':4,'y':8})
        builder.state.update(power_plan=self.power(),coal_plan=deepcopy(saved),power_sample_tick=0)
        builder.ensure_plan=Mock(return_value={'status':'succeeded'});builder._seed=Mock(return_value=None)
        builder.power_evidence=Mock(return_value={'ok':True,'tick':2000,'water':1,'steam':1,'boiler_fuel':1,
                                    'drill_fuel':1,'coal_on_belts':1,'connected_engines':2})
        with patch.object(builder,'_choose_coal_plan') as choose:
            result=builder.ensure_power({'world_id':'w','technologies':{'steam-power':True}})
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(builder.state['coal_plan'],saved)
        choose.assert_not_called();builder.game.observe.assert_not_called()

    def test_bad_survey_indices_and_missing_typed_input_fail_before_routing(self):
        for indices in ([True],[0],[1,1],[{}]):
            builder=self.builder(indices)
            with patch('factorio_agent.transport.plan_connection') as route:
                self.assertEqual(builder._choose_coal_plan(self.power())['reason'],'invalid_starter_boundary_survey')
                route.assert_not_called()
        builder=self.builder()
        self.assertEqual(builder._choose_coal_plan(_plan([],[]))['reason'],'starter_power_requires_one_typed_coal_input')
        builder.game.observe.assert_not_called()

    @unittest.skipUnless(LuaRuntime,'optional Lua runtime: pip install lupa')
    def test_actual_lua_policy_rejects_interior_arm_and_accepts_boundary_cell(self):
        builder=self.builder();lua=LuaRuntime(unpack_returned_tuples=True)
        lua.execute('''f={name="player"};s={};ores={}
for x=-.5,3.5,1 do for y=-.5,1.5,1 do ores[#ores+1]={name="coal",position={x=x,y=y}} end end
for x=9.5,10.5,1 do for y=-.5,.5,1 do ores[#ores+1]={name="coal",position={x=x,y=y}} end end
function target(p,name)return nil end
function s.find_entities_filtered(args)
 local out={};if args.type~="resource" then return out end
 for _,e in ipairs(ores) do local p=e.position;local b=args.area
  if p.x>b[1][1] and p.x<b[2][1] and p.y>b[1][2] and p.y<b[2][2] then out[#out+1]=e end
 end;return out
end
helpers={}''')
        def table(value):
            if isinstance(value,dict):return lua.table_from({k:table(v) for k,v in value.items()})
            if isinstance(value,list):return lua.table_from([table(v) for v in value])
            return value
        lua.globals().helpers.json_to_table=lambda value:table(json.loads(value))
        def query(script):
            result=lua.execute(script)
            return {'ok':result['ok'],'legal':list(result['legal'].values())}
        builder.game.query=Mock(side_effect=query)
        with patch('factorio_agent.planning.preflight',return_value={'ok':True,'clear_obstacles':[]}), \
             patch('factorio_agent.transport.plan_connection',return_value=self.route()):
            result=builder._choose_coal_plan(self.power())
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['drill']['position'],{'x':10,'y':0})


if __name__=='__main__':unittest.main()
