from types import SimpleNamespace
import unittest
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.board import entity
from factorio_agent.planning import preflight,OWNED_CONFIGURATION_GUARD

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


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

    @unittest.skipUnless(LuaRuntime,'Lua runtime required')
    def test_native_gun_aim_direction_does_not_change_square_placement_but_force_and_belts_stay_exact(self):
        lua=LuaRuntime(unpack_returned_tuples=True)
        setup='f={};old={direction=4,type="gun-turret",force=f};spec={name="gun-turret",direction=0};'
        self.assertTrue(lua.execute(setup+OWNED_CONFIGURATION_GUARD+'return{ok=true}')['ok'])
        self.assertFalse(lua.execute(setup+'old.force={};'+OWNED_CONFIGURATION_GUARD+'return{ok=true}')['ok'])
        self.assertFalse(lua.execute(setup+'old.type="transport-belt";spec.name="transport-belt";'+OWNED_CONFIGURATION_GUARD+'return{ok=true}')['ok'])


if __name__=='__main__':unittest.main()
