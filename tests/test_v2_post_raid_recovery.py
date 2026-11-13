from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.post_raid_recovery import (
    PostRaidRecovery,
    execute_recover_raid_post,
    FILE,
    _hash,
    _valid_receipt,
    RECOVERY_GUARD_LUA,
    RECOVER_POST_LUA,
)

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


@unittest.skipUnless(LuaRuntime, "optional executable native Lua fixture")
class PostRaidRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lua = LuaRuntime(unpack_returned_tuples=True)

        def table(q):
            if isinstance(q, dict):
                return self.lua.table_from({k: table(v) for k, v in q.items()})
            if isinstance(q, list):
                return self.lua.table_from([table(v) for v in q])
            return q

        def decode(q):
            if not hasattr(q, "items"):
                return q
            keys = list(q.keys())
            if keys and all(type(k) is int for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
                return [decode(q[i]) for i in range(1, len(keys) + 1)]
            return {k: decode(v) for k, v in q.items()}

        self.table = table
        self.lua.globals().helpers = self.lua.table_from({"json_to_table": lambda raw: table(json.loads(raw))})
        self.lua.execute(r"""
f={index=1};s={index=1};d={world_id="w"};game={tick=100};world={};mine_calls=0
defines={inventory={character_ammo=1,turret_ammo=2}}
prototypes={item={["gun-turret"]={type="item",stack_size=50},["firearm-magazine"]={type="ammo",magazine_size=10,stack_size=100},
 ["piercing-rounds-magazine"]={type="ammo",magazine_size=10,stack_size=100}},entity={worm={type="turret",attack_parameters={range=25}}}}
function stack(name,count,rounds)return {valid_for_read=true,name=name,count=count,ammo=rounds,quality={name="normal"},health=1,spoil_tick=0,prototype=prototypes.item[name]}end
function inventory(n)
 local inv={valid=true};for i=1,n do inv[i]={valid_for_read=false}end
 inv.supports_filters=function()return false end;inv.get_insertable_count=function()return full_inventory and 0 or 1000 end
 return inv
end
main=inventory(20);equipped=inventory(3);main[1]=stack("gun-turret",2);main[2]=stack("firearm-magazine",2,5);main[3]=stack("piercing-rounds-magazine",1,10)
equipped[1]=stack("piercing-rounds-magazine",1,4)
a={valid=true,unit_number=11,force=f,surface=s,position={x=0,y=0},get_main_inventory=function()return main end,
 get_inventory=function()return equipped end,can_reach_entity=function()return reachable~=false end}
player={name="FactoryAutomaton",index=1,connected=true,character=a};a.player=player;d.actor=a;d.crafting_player_index=1;d.crafting_actor_unit_number=11
local function key(p)return p.x..":"..p.y end
function gun(unit,x,y,health)
 local inv=inventory(1);inv[1]=stack("piercing-rounds-magazine",10,7)
 local e={valid=true,name="gun-turret",type="ammo-turret",unit_number=unit,position={x=x,y=y},health=health or 400,max_health=400,
  quality={name="normal"},force=f,surface=s,prototype={attack_parameters={range=18}},get_inventory=function()return inv end}
 world[key(e.position)]=e;return e
end
function target(p,name)local e=world[key(p)];return e and e.valid and e.name==name and e or nil end
function remove(unit)for k,e in pairs(world)do if e.unit_number==unit then e.valid=false;world[k]=nil end end end
function into(inv,source)
 for i=1,#inv do if not inv[i].valid_for_read then inv[i]=stack(source.name,source.count,source.ammo);return true end end;return false
end
a.mine_entity=function(e)
 mine_calls=mine_calls+1;local marker=nil;for _,records in pairs(d.post_raid_recoveries)do for _,v in pairs(records)do if v.old_unit==e.unit_number then marker=v end end end
 assert(marker and marker.phase=="intent"and marker.before.snapshot and marker.old_ammo["piercing-rounds-magazine"].rounds==97)
 if mine_returns_false then return false end
 assert(into(main,stack("gun-turret",1)))
 for i=1,#e.get_inventory()do local st=e.get_inventory()[i];if st.valid_for_read then assert(into(autoequip and equipped or main,st))end end
 remove(e.unit_number)
 if consume_round then equipped[2].ammo=equipped[2].ammo-1 end
 if shuffle then main[9]=main[2];main[2]={valid_for_read=false}end
 if response_lost then error("lost after ordinary engine mine")end
 return true
end
s.count_entities_filtered=function(spec)return hostile and 1 or 0 end
s.find_entities_filtered=function(spec)return worm and{{position={x=20,y=0},prototype={attack_parameters={range=25}}}}or{}end
s.find_non_colliding_position=function(_,p)return no_approach and nil or{x=p.x,y=p.y}end
""")
        self.game = SimpleNamespace(
            expected_world="w",
            backend="assisted",
            cfg=SimpleNamespace(runtime_dir=self.root),
            query=lambda script: decode(
                self.lua.execute("return(function() " + " ".join(script.splitlines()) + " end)()")
            ),
        )
        self.catalog = SimpleNamespace(fingerprint="c")
        self.bootstrap = SimpleNamespace(ensure_item=Mock(return_value=dict(status="succeeded")))
        self.builder = SimpleNamespace(bootstrap=self.bootstrap)
        posts = [
            dict(
                name="raid" + str(i),
                position=dict(x=10 + 2 * (i % 3), y=2 * (i // 3)),
                ammo_item="piercing-rounds-magazine",
            )
            for i in range(6)
        ]
        targets = [
            dict(name="biter-spawner", unit_number=900, position=dict(x=30, y=0), force_index=2, surface_index=1)
        ]
        plan = dict(
            entities=[dict(name="gun-turret", position=p["position"]) for p in posts],
            ports=[],
            reserved_area=[[9, -1], [15, 3]],
        )
        verification = dict(
            world_id="w",
            target_set_hash=_hash(targets),
            group_id="group",
            single_wave=True,
            targets_cleared=1,
            tick=80,
            native_projectile_deaths=[dict(unit_number=900, own_gun_projectile=True)],
        )
        self.doc = dict(
            world_id="w",
            catalog_fingerprint="c",
            id="operation",
            actor_unit=11,
            phase="verified",
            targets=targets,
            target_set_hash=_hash(targets),
            verification=verification,
            wave=dict(
                block="defense_raid",
                plan=plan,
                posts=posts,
                bindings={p["name"]: 100 + i for i, p in enumerate(posts)},
                group_id="group",
                deployment_ids={p["name"]: "deploy" + str(i) for i, p in enumerate(posts)},
            ),
        )
        self.board = dict(world_id="w", catalog_fingerprint="c", blocks={"defense_raid": deepcopy(plan)})
        self.perimeter = dict(
            world_id="w",
            catalog_fingerprint="c",
            posts=[dict(name="rear" + str(i), position=dict(x=0, y=i * 2), unit_number=200 + i) for i in range(2)],
        )
        self.write("nest-sweep.json", self.doc)
        self.write("pcb-board.json", self.board)
        self.write("defense-perimeter.json", self.perimeter)
        self.lua.globals().saved = self.table(self.doc)
        self.lua.globals().plan_hash = _hash(plan)
        self.lua.execute(r"""
d.nest_assaults={operation={world_id="w",catalog_fingerprint="c",actor_unit=11,player_force_index=1,surface_index=1,target_set_hash=saved.target_set_hash,wave_group_id="group",
 deaths={["900"]={unit_number=900,name="biter-spawner",position={x=30,y=0},own_gun_projectile=true}},lost_posts={}}}
d.defense_deployment_groups={group={phase="complete",owner_kind="nest_sweep",actor_unit=11,world_id="w",catalog_fingerprint="c",plan_revision=plan_hash,
 receipt={group_id="group",paid_turrets=6,loaded_magazines=120,loaded_rounds=1200,ammo_item="piercing-rounds-magazine",same_tick=true,actor_unit=11,world_id="w",catalog_fingerprint="c",plan_revision=plan_hash}}}
d.defense_deployments={}
for i,p in ipairs(saved.wave.posts)do local unit=99+i;gun(unit,p.position.x,p.position.y)
 d.defense_deployments["deploy"..(i-1)]={phase="complete",owner_kind="nest_sweep",world_id="w",catalog_fingerprint="c",actor_unit=11,
  post_name=p.name,unit_number=unit,ammo_item="piercing-rounds-magazine",receipt={deployment_id="deploy"..(i-1),paid_turrets=1,loaded_magazines=20,
   loaded_rounds=200,ammo_item="piercing-rounds-magazine",position=p.position,actor_unit=11,world_id="w",catalog_fingerprint="c"}}
end
gun(200,0,0);gun(201,0,2)
""")
        self.obs = dict(
            ok=True,
            world_id="w",
            tick=100,
            actor_unit_number=11,
            inventory={"repair-pack": 1},
            enabled_recipes={"repair-pack": True},
        )
        self.ctrl = PostRaidRecovery(self.game, self.builder, self.catalog, sweep=SimpleNamespace(state=self.doc))

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value))

    def read(self, name):
        return json.loads((self.root / name).read_text())

    def action(self):
        action = self.ctrl.step(self.obs)
        self.assertIsNotNone(action, self.ctrl.last_diagnostic)
        return action

    def test_prepare_readonly_checks_native_victory_paid_identities_and_cover(self):
        before = {p.name: p.read_bytes() for p in self.root.glob("*.json")}
        r = self.ctrl.prepare(self.obs)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["native"]["posts"]), 6)
        self.assertTrue(all(q["safe"] and q["covered"] for q in r["native"]["posts"]))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.glob("*.json")})

    def test_native_partial_red_autoequip_rounds_and_yellow_actor_stock_conserved(self):
        self.lua.execute("autoequip=true;shuffle=true")
        action = self.action()
        r = execute_recover_raid_post(self.game, action)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["old_ammo"]["piercing-rounds-magazine"]["rounds"], 97)
        self.assertEqual(
            r["after"]["ammo"]["piercing-rounds-magazine"]["rounds"]
            - r["before"]["ammo"]["piercing-rounds-magazine"]["rounds"],
            97,
        )
        self.assertEqual(r["after"]["ammo"]["firearm-magazine"], r["before"]["ammo"]["firearm-magazine"])
        self.assertEqual(self.lua.eval("equipped[2].ammo"), 7)
        self.assertEqual(self.lua.eval("mine_calls"), 1)
        self.assertTrue(execute_recover_raid_post(self.game, action)["ok"])
        self.assertEqual(self.lua.eval("mine_calls"), 1)

    def test_damaged_post_uses_existing_native_tool_action_before_any_mine(self):
        self.lua.execute('world["14:0"].health=7')
        action = self.action()
        self.assertEqual(action["type"], "repair")
        self.assertEqual(action["expected_entity_unit"], 102)
        self.assertTrue(action["return_to_spawn"])
        self.bootstrap.ensure_item.assert_called_with(self.obs, "repair-pack", 1)
        self.assertEqual(self.lua.eval("mine_calls"), 0)
        self.lua.execute('world["14:0"].health=400')
        self.assertEqual(self.action()["type"], "recover_raid_post")

    def test_move_funding_and_pending_repair_cleanup_use_existing_paths(self):
        self.lua.execute('world["14:0"].health=7;reachable=false')
        self.assertEqual(self.action()["type"], "move")
        self.bootstrap.ensure_item.return_value = {"type": "craft", "recipe": "repair-pack", "count": 1}
        self.assertEqual(self.action()["type"], "craft")
        self.obs["repair_pending"] = {"request": "prior"}
        self.assertEqual(self.action()["type"], "finish_repair")

    def test_full_health_and_safe_range_guard_before_ordinary_mine(self):
        for change in (
            'world["14:0"].health=399',
            "hostile=true",
            "worm=true",
            "full_inventory=true",
            'world["14:0"].unit_number=999',
            'world["14:0"].get_inventory()[1].quality.name="rare"',
        ):
            with self.subTest(change=change):
                self.setUp()
                action = self.action()
                self.lua.execute(change)
                r = execute_recover_raid_post(self.game, action)
                self.assertFalse(r["ok"], r)
                self.assertEqual(self.lua.eval("mine_calls"), 0)

    def test_original_victory_group_marker_and_foreign_post_are_never_adopted(self):
        for change in (
            'd.nest_assaults.operation.deaths["900"].own_gun_projectile=false',
            "d.defense_deployment_groups.group.receipt.paid_turrets=5",
            "d.defense_deployments.deploy0.actor_unit=12",
            'world["10:0"].force={index=3}',
        ):
            with self.subTest(change=change):
                self.setUp()
                self.lua.execute(change)
                self.assertFalse(self.ctrl.prepare(self.obs)["ok"])
                self.assertEqual(self.lua.eval("mine_calls"), 0)

    def test_lost_reply_after_mining_resumes_only_from_durable_exact_intent(self):
        self.lua.execute("autoequip=true;response_lost=true")
        action = self.action()
        r = execute_recover_raid_post(self.game, action)
        self.assertEqual(r["reason"], "post_raid_recovery_outcome_unknown")
        self.assertEqual(self.lua.eval("mine_calls"), 1)
        self.lua.execute("response_lost=false")
        restarted = PostRaidRecovery(self.game, self.builder, self.catalog, sweep=self.ctrl.sweep)
        next_action = restarted.step(self.obs)
        self.assertEqual(next_action["post_name"], action["post_name"])
        r = execute_recover_raid_post(self.game, next_action)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["recovered_from_intent"])
        self.assertEqual(self.lua.eval("mine_calls"), 1)

    def test_unknown_absence_or_changed_inventory_never_creates_recovery_receipt(self):
        self.lua.execute("remove(102)")
        self.assertFalse(self.ctrl.prepare(self.obs)["ok"])
        self.setUp()
        self.lua.execute("autoequip=true;response_lost=true")
        action = self.action()
        execute_recover_raid_post(self.game, action)
        self.lua.execute("response_lost=false;equipped[2].ammo=6")
        r = execute_recover_raid_post(self.game, action)
        self.assertEqual(r["reason"], "post_raid_recovery_outcome_unknown")
        self.assertEqual(self.lua.eval("mine_calls"), 1)

    def test_complete_wave_keeps_original_history_and_reports_no_standing_fiction(self):
        original = (self.root / "nest-sweep.json").read_bytes()
        board = (self.root / "pcb-board.json").read_bytes()
        for _ in range(6):
            action = self.action()
            self.assertTrue(execute_recover_raid_post(self.game, action)["ok"])
        self.assertIsNone(self.ctrl.step(self.obs))
        proof = self.ctrl.proof(self.obs)
        self.assertIsNotNone(proof, self.ctrl.last_diagnostic)
        self.assertEqual(len(proof["recovered_posts"]), 6)
        self.assertEqual(proof["surviving_posts"], [])
        self.assertEqual(len(proof["history"]), 6)
        self.assertEqual((self.root / "nest-sweep.json").read_bytes(), original)
        self.assertEqual((self.root / "pcb-board.json").read_bytes(), board)
        self.perimeter["posts"].append(dict(name="unrelated", position={"x": 5, "y": 5}, unit_number=999))
        self.write("defense-perimeter.json", self.perimeter)
        self.assertIsNotNone(self.ctrl.proof(self.obs))

    def test_known_combat_loss_is_recorded_without_recovery_or_rebuild(self):
        self.lua.execute('remove(102);d.nest_assaults.operation.lost_posts["102"]=90')
        action = self.action()
        history = next(iter(self.read(FILE)["operations"].values()))["history"]
        self.assertEqual(history[0]["kind"], "combat_loss")
        self.assertEqual(history[0]["recovered_turrets"], 0)
        self.assertEqual(action["type"], "recover_raid_post")

    def test_action_and_owner_revision_guards_fail_before_io(self):
        action = self.action()
        query = Mock(side_effect=AssertionError("native I/O forbidden"))
        old = self.game.query
        self.game.query = query
        for change in (
            {"post_name": "foreign"},
            {"expected_actor_unit": True},
            {"position": {"x": 0, "y": 0}},
            {"recovery_id": "x" * 64},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                execute_recover_raid_post(self.game, dict(action, **change))
        query.assert_not_called()
        self.game.query = old
        self.doc["verification"]["tick"] = 81
        self.write("nest-sweep.json", self.doc)
        with self.assertRaises(ValueError):
            execute_recover_raid_post(self.game, action)

    def test_partial_round_loss_is_unknown_and_never_normalized_or_remined(self):
        self.lua.execute("autoequip=true;consume_round=true")
        action = self.action()
        r = execute_recover_raid_post(self.game, action)
        self.assertEqual(r["reason"], "post_raid_recovery_outcome_unknown")
        self.assertEqual(self.lua.eval("equipped[2].ammo"), 6)
        self.assertEqual(execute_recover_raid_post(self.game, action)["reason"], "post_raid_recovery_outcome_unknown")
        self.assertEqual(self.lua.eval("mine_calls"), 1)

    def test_malformed_receipt_and_flattened_lua(self):
        action = self.action()
        r = execute_recover_raid_post(self.game, action)
        self.assertTrue(r["ok"], r)
        p = next(iter(self.read(FILE)["operations"].values()))["authority"]
        p = dict(p, recovery_id=action["recovery_id"], post_name=action["post_name"])
        for change in ({"tick": None}, {"actor_unit": True}, {"full_health_before": False}, {"unit_number": 999}):
            self.assertFalse(_valid_receipt(dict(r, **change), p))
        self.assertNotIn("--", RECOVERY_GUARD_LUA + RECOVER_POST_LUA)


if __name__ == "__main__":
    unittest.main()
