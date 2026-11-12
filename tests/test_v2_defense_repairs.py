from types import SimpleNamespace
from unittest.mock import Mock
import unittest
from pathlib import Path
import runpy
import json

from factorio_agent.defense_repairs import DefenseRepairs, REPAIR_SITE_LUA
from factorio_agent.core.deterministic_repair_control import BEGIN_REPAIR_LUA, REPAIR_RETURN_HOME_LUA

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


class DefenseRepairTests(unittest.TestCase):
    def setUp(self):
        self.post = dict(position=dict(x=10, y=20), unit_number=42, health=128, max_health=400)
        self.obs = dict(
            ok=True, world_id="w", actor_unit_number=11, inventory={}, enabled_recipes={"repair-pack": True}
        )
        self.game = SimpleNamespace(query=Mock(return_value=dict(ok=True, target=self.post, reachable=True)))
        self.bootstrap = SimpleNamespace(ensure_item=Mock(return_value=dict(status="succeeded")))
        self.perimeter = SimpleNamespace(inspect=Mock(return_value=dict(ok=True, posts=[self.post], changed=[])))
        self.repairs = DefenseRepairs(self.game, self.bootstrap, self.perimeter)

    def test_real_repair_pins_actor_world_and_exact_known_post(self):
        result = self.repairs.next_action(self.obs)
        self.assertEqual(result["type"], "repair")
        self.assertEqual(result["expected_entity_unit"], 42)
        self.assertEqual(result["expected_actor_unit"], 11)
        self.assertEqual(result["expected_world_id"], "w")
        self.bootstrap.ensure_item.assert_called_once_with(self.obs, "repair-pack", 1)

    def test_interrupt_cleanup_precedes_acquisition_or_new_target(self):
        self.obs["repair_pending"] = dict(request="interrupted")
        result = self.repairs.next_action(self.obs)
        self.assertEqual(result["type"], "finish_repair")
        self.assertEqual(result["request"], "interrupted")
        self.perimeter.inspect.assert_not_called()
        self.bootstrap.ensure_item.assert_not_called()

    def test_normal_tool_acquisition_and_reachable_approach_precede_repair(self):
        self.bootstrap.ensure_item.return_value = dict(type="craft", recipe="repair-pack", count=1)
        self.assertEqual(self.repairs.next_action(self.obs)["type"], "craft")
        self.bootstrap.ensure_item.return_value = dict(status="succeeded")
        self.game.query.return_value = dict(ok=True, target=self.post, reachable=False, approach=dict(x=8.5, y=20.5))
        self.assertEqual(
            self.repairs.next_action(self.obs),
            dict(type="move", position=dict(x=8.5, y=20.5), reason="approach a quiet armed post for native repair"),
        )

    def test_changed_or_unsafe_sites_and_locked_tools_do_not_take_priority(self):
        self.game.query.return_value = dict(ok=True)
        self.assertIsNone(self.repairs.next_action(self.obs))
        self.bootstrap.ensure_item.assert_not_called()
        self.perimeter.inspect.return_value["changed"] = ["post"]
        self.game.query.reset_mock()
        self.assertIsNone(self.repairs.next_action(self.obs))
        self.game.query.assert_not_called()
        self.obs["enabled_recipes"] = {}
        self.perimeter.inspect.reset_mock()
        self.assertIsNone(self.repairs.next_action(self.obs))
        self.perimeter.inspect.assert_not_called()

    def test_multiple_carried_tools_cannot_enable_unbounded_native_spending(self):
        self.obs["inventory"]["repair-pack"] = 2
        self.assertEqual(self.repairs.next_action(self.obs)["status"], "blocked")
        self.bootstrap.ensure_item.assert_not_called()

    def test_wall_callback_preserves_exact_owned_unit_and_ordinary_tool_path(self):
        self.game.query.return_value["target"] = dict(self.post, name="stone-wall")
        result = self.repairs.wall_action(self.obs, dict(name="stone-wall", position=self.post["position"]), 42)
        self.assertEqual(result["type"], "repair")
        self.assertEqual(result["name"], "stone-wall")
        self.assertEqual(result["expected_entity_unit"], 42)
        self.assertIsNone(self.repairs.wall_action(self.obs, dict(name="enemy", position=self.post["position"]), 42))


@unittest.skipUnless(LuaRuntime, "Lua runtime required")
class NativeRepairSiteTests(unittest.TestCase):
    def run_site(self, changes=""):
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute("""
game={tick=100};f={};s={};d={world_id="w"};defines={inventory={turret_ammo=1}}
e={valid=true,unit_number=42,force=f,surface=s,position={x=10,y=20},health=128,max_health=400}
e.get_inventory=function()return{{valid_for_read=true,name="firearm-magazine",quality={name="normal"},count=2,
 ammo=10,prototype={magazine_size=10}}}end
a={valid=true,unit_number=11,force=f,surface=s,can_reach_entity=function()return true end}
target=function()return e end
s.find_non_colliding_position=function()return{x=8.5,y=20.5}end
s.count_entities_filtered=function()return 0 end
s.find_entities_filtered=function()return{}end
x={world="w",actor=11,posts={{position={x=10,y=20},unit_number=42}}}
""")
        lua.execute(changes)
        return lua.execute(REPAIR_SITE_LUA)

    def test_only_live_exact_paid_post_in_quiet_service_site_is_selected(self):
        self.assertEqual(self.run_site()["target"]["unit_number"], 42)
        for change in (
            "e.unit_number=99",
            "e.force={}",
            "e.surface={}",
            "e.health=0",
            "e.health=400",
            "e.position.x=11",
            "s.count_entities_filtered=function()return 1 end",
            "e.get_inventory=function()return{}end",
            "s.find_non_colliding_position=function()return nil end",
        ):
            with self.subTest(change=change):
                self.assertIsNone(self.run_site(change)["target"])

    def test_worm_effective_range_and_world_actor_changes_fail_closed(self):
        worm = "s.find_entities_filtered=function()return{{position={x=20,y=20},prototype={attack_parameters={range=25}}}}end"
        self.assertIsNone(self.run_site(worm)["target"])
        for change in ('d.world_id="other"', "a.unit_number=99", "a.force={}"):
            self.assertFalse(self.run_site(change)["ok"])

    def test_wall_repair_requires_live_exact_registered_armed_protection(self):
        setup = """
x.posts[1].name="stone-wall"
g={valid=true,force=f,surface=s,unit_number=99,health=400,position={x=9,y=20},
 prototype={attack_parameters={range=18}},get_inventory=e.get_inventory}
x.guards={{position=g.position,unit_number=99}}
target=function(p,name)if name=="stone-wall"then return e else return g end end
"""
        self.assertEqual(self.run_site(setup)["target"]["name"], "stone-wall")
        self.assertIsNone(self.run_site(setup + "g.unit_number=100")["target"])
        self.assertIsNone(self.run_site(setup + "g.get_inventory=function()return{}end")["target"])

    def test_native_remote_view_exits_before_pack_claim_and_unexitables_do_not_spend(self):
        fixture = runpy.run_path(str(Path(__file__).with_name("test_v2_repair_control_live.py")))["FIXTURE_LUA"]

        def evaluate(**options):
            body = fixture.replace("OPTIONS", json.dumps(json.dumps(options))).replace("BODY", BEGIN_REPAIR_LUA)
            lua = LuaRuntime(unpack_returned_tuples=True)
            lua.globals().helpers = lua.table_from({"json_to_table": lambda unused: lua.table_from(options)})
            return lua.execute(body)

        good = evaluate(remote=True)
        self.assertEqual(good["result"]["status"], "running")
        self.assertEqual(good["cursor_count"], 1)
        bad = evaluate(remote=True, remote_exit_fails=True)
        self.assertFalse(bad["result"]["ok"])
        self.assertEqual(bad["cursor_count"], 0)
        self.assertEqual(bad["inventory_count"], 1)
        self.assertFalse(bad["pending"])

    def test_native_return_requires_owned_clean_actor_and_quiet_clear_spawn(self):
        fixture = runpy.run_path(str(Path(__file__).with_name("test_v2_repair_control_live.py")))["FIXTURE_LUA"]
        base = """f.get_spawn_position=function()return{x=0,y=0}end
s.find_non_colliding_position=function()return{x=0,y=0}end
s.count_entities_filtered=function()return 0 end
s.find_entities_filtered=function()return{}end
"""

        def evaluate(change=""):
            lua = LuaRuntime(unpack_returned_tuples=True)
            lua.globals().helpers = lua.table_from({"json_to_table": lambda _: lua.table_from({})})
            body = fixture.replace("OPTIONS", json.dumps("{}")).replace("BODY", base + change + REPAIR_RETURN_HOME_LUA)
            return lua.execute(body)["result"]

        self.assertTrue(evaluate()["ok"])
        self.assertEqual(evaluate()["position"]["x"], 0)
        for change in (
            "d.native_repair={};",
            'a.player.name="Other";',
            "s.count_entities_filtered=function()return 1 end;",
            "s.find_non_colliding_position=function()return nil end;",
        ):
            self.assertFalse(evaluate(change)["ok"])


if __name__ == "__main__":
    unittest.main()
