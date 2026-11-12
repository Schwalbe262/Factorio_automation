import unittest
from factorio_agent.core.deterministic_character import BIND_CRAFTING_PLAYER_LUA
try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


@unittest.skipUnless(LuaRuntime,'executable Lua fixture')
class NativeClientIdentityTests(unittest.TestCase):
    def fixture(self):
        lua=LuaRuntime(unpack_returned_tuples=True)
        lua.execute('''f={};s={};defines={controllers={character=1}};a={valid=true,force=f,surface=s,unit_number=18};d={actor=a}
old={valid=true,get_max_inventory_index=function()return 0 end};old.destroy=function()old.valid=false end
p={valid=true,name="NEC",index=1,connected=true,force=f,surface=s,character=old}
p.set_controller=function(args)p.character=args.character;a.player=p end
game={connected_players={p},get_player=function(key)if key==1 or key=="NEC"then return p end end}''')
        return lua

    def test_unique_steam_named_client_is_pinned_without_a_second_starter_actor(self):
        lua=self.fixture();result=lua.execute(BIND_CRAFTING_PLAYER_LUA)
        self.assertEqual(result['status'],'ready');self.assertEqual(result['player_name'],'NEC')
        self.assertEqual(lua.eval('d.crafting_player_name'),'NEC')
        self.assertTrue(lua.eval('p.character==a'));self.assertFalse(lua.eval('old.valid'))

    def test_ambiguous_connected_players_do_not_steal_either_controller(self):
        lua=self.fixture();lua.execute('game.connected_players[2]={force=f,surface=s,connected=true,name="other"}')
        result=lua.execute(BIND_CRAFTING_PLAYER_LUA)
        self.assertEqual(result['reason'],'dedicated_client_identity_ambiguous')
        self.assertTrue(lua.eval('p.character==old'));self.assertTrue(lua.eval('old.valid'))

    def test_later_connection_uses_the_pinned_index_instead_of_new_name_guessing(self):
        lua=self.fixture();lua.execute(BIND_CRAFTING_PLAYER_LUA)
        lua.execute('game.connected_players[2]={force=f,surface=s,connected=true,name="other"}')
        self.assertEqual(lua.execute(BIND_CRAFTING_PLAYER_LUA)['status'],'ready')
        self.assertEqual(lua.eval('d.crafting_player_index'),1)

    def test_missing_pinned_owner_never_falls_back_to_a_different_named_player(self):
        lua=self.fixture();lua.execute(BIND_CRAFTING_PLAYER_LUA)
        lua.execute('''other={valid=true,name="FactoryAutomaton",index=2,connected=true,character=old}
game.get_player=function(key)if key=="FactoryAutomaton"then return other end end''')
        result=lua.execute(BIND_CRAFTING_PLAYER_LUA)
        self.assertEqual(result['reason'],'dedicated_pinned_owner_changed')
        self.assertTrue(lua.eval('other.character==old'));self.assertTrue(lua.eval('p.character==a'))

    def test_renamed_pinned_owner_requires_review_instead_of_rewriting_identity(self):
        lua=self.fixture();lua.execute(BIND_CRAFTING_PLAYER_LUA);lua.execute('p.name="renamed"')
        result=lua.execute(BIND_CRAFTING_PLAYER_LUA)
        self.assertEqual(result['reason'],'dedicated_pinned_owner_changed')
        self.assertEqual(lua.eval('d.crafting_player_name'),'NEC')
