from types import SimpleNamespace
import unittest
from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_agent.board import entity
from factorio_agent.planning import preflight


class NativeClearanceShapeTests(unittest.TestCase):
    def test_empty_lua_table_is_normalized_before_composing_route_clearances(self):
        geometry=FactoryBuilder.__new__(FactoryBuilder)
        game=SimpleNamespace(query=lambda _: {'ok':True,'clear_obstacles':{}})
        result=preflight(game,geometry,{'world_id':'w'},[entity('transport-belt',.5,.5,4)])
        self.assertEqual(result['clear_obstacles']+[{'name':'tree'}],[{'name':'tree'}])

    def test_unexpected_nonempty_native_mapping_does_not_become_obstacle_names(self):
        geometry=FactoryBuilder.__new__(FactoryBuilder)
        game=SimpleNamespace(query=lambda _: {'ok':True,'clear_obstacles':{'unexpected':1}})
        result=preflight(game,geometry,{'world_id':'w'},[entity('transport-belt',.5,.5,4)])
        self.assertEqual(result['reason'],'invalid_native_clearance_shape')


if __name__=='__main__':unittest.main()
