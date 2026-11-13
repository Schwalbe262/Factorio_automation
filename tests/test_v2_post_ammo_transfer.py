from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.post_ammo_transfer import (
    ACTION,
    RED,
    YELLOW,
    POLICY_FILE,
    active_reservations,
    execute_upgrade_post_ammunition,
    revision,
    signature,
    UPGRADE_POST_AMMUNITION_LUA,
)

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


class PostAmmoFixture(unittest.TestCase):
    def context(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.posts = [
            dict(name=name, position=dict(x=x, y=y), unit_number=unit, coverage_scope="scope", local_walk_access=True)
            for name, x, y, unit in (("front", 0, 0, 7), ("rear-a", -5, 0, 8), ("rear-b", 0, 5, 9))
        ]
        self.perimeter = dict(world_id="w", catalog_fingerprint="c", posts=deepcopy(self.posts))
        self.save_perimeter()
        self.action = dict(
            type=ACTION,
            name="gun-turret",
            post_name="front",
            position=dict(x=0, y=0),
            item=RED,
            count=10,
            expected_entity_unit=7,
            expected_entity_world_id="w",
            expected_actor_unit=11,
            expected_catalog_fingerprint="c",
        )
        self.payload = dict(
            action=deepcopy(self.action),
            post=signature(self.posts[0]),
            supports=[signature(q) for q in self.posts[1:]],
            stock=dict(name=YELLOW, count=10, ammo=7),
            reservation=active_reservations(self.root, "w", "c"),
            created_tick=10,
        )
        self.save_intent()
        self.game.cfg = SimpleNamespace(runtime_dir=self.root)

    def save_perimeter(self):
        (self.root / "defense-perimeter.json").write_text(json.dumps(self.perimeter))

    def save_intent(self):
        key = revision(self.payload)
        self.action = dict(self.payload["action"], transaction_id=key)
        self.state = dict(
            world_id="w",
            catalog_fingerprint="c",
            preferences={"front": {"item": RED}},
            transactions={key: dict(phase="pending", payload=deepcopy(self.payload))},
            conversions={},
        )
        (self.root / POLICY_FILE).write_text(json.dumps(self.state))


class PostAmmoEnvelopeTests(PostAmmoFixture):
    def setUp(self):
        self.game = SimpleNamespace(expected_world="w", backend="assisted", query=Mock())
        self.context()

    def test_invalid_envelope_rejected_before_io(self):
        for change in (
            {"count": True},
            {"count": 0},
            {"count": 11},
            {"item": YELLOW},
            {"name": "iron-chest"},
            {"expected_actor_unit": False},
            {"expected_entity_unit": 0},
            {"expected_entity_world_id": "other"},
            {"position": dict(x=float("nan"), y=0)},
            {"transaction_id": "x" * 64},
            {"unknown": True},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                execute_upgrade_post_ammunition(self.game, {**self.action, **change})
        self.game.query.assert_not_called()

    def test_exact_immutable_intent_and_registered_unit_are_required(self):
        for field, value in (("unit_number", 70), ("position", dict(x=1, y=0)), ("coverage_scope", "other")):
            old = deepcopy(self.perimeter)
            self.perimeter["posts"][0][field] = value
            self.save_perimeter()
            with self.assertRaises(ValueError):
                execute_upgrade_post_ammunition(self.game, self.action)
            self.perimeter = old
            self.save_perimeter()
        self.state["transactions"][self.action["transaction_id"]]["payload"]["created_tick"] = 11
        (self.root / POLICY_FILE).write_text(json.dumps(self.state))
        with self.assertRaises(ValueError):
            execute_upgrade_post_ammunition(self.game, self.action)
        self.game.query.assert_not_called()

    def test_changed_support_or_new_active_reservation_blocks_before_io(self):
        self.perimeter["posts"][1]["unit_number"] = 80
        self.save_perimeter()
        with self.assertRaises(ValueError):
            execute_upgrade_post_ammunition(self.game, self.action)
        self.perimeter["posts"] = deepcopy(self.posts)
        self.save_perimeter()
        sweep = dict(
            world_id="w",
            catalog_fingerprint="c",
            id="raid",
            phase="stock",
            ammo_item=RED,
            requirements={RED: 200},
            wave={},
        )
        (self.root / "nest-sweep.json").write_text(json.dumps(sweep))
        with self.assertRaises(ValueError):
            execute_upgrade_post_ammunition(self.game, self.action)
        self.game.query.assert_not_called()

    def test_completed_operations_do_not_reserve_two_hundred_forever(self):
        sweep = dict(
            world_id="w",
            catalog_fingerprint="c",
            id="raid",
            phase="verified",
            ammo_item=RED,
            requirements={RED: 200},
            wave={},
        )
        (self.root / "nest-sweep.json").write_text(json.dumps(sweep))
        self.assertEqual(active_reservations(self.root, "w", "c")["count"], 0)
        sweep["phase"] = "stock"
        (self.root / "nest-sweep.json").write_text(json.dumps(sweep))
        self.assertEqual(active_reservations(self.root, "w", "c")["count"], 200)
        sweep["phase"] = "fight"
        (self.root / "nest-sweep.json").write_text(json.dumps(sweep))
        self.assertEqual(active_reservations(self.root, "w", "c")["count"], 0)

    def test_unstarted_legacy_assault_preserves_explicit_whole_operation_stock(self):
        assault = dict(
            world_id="w",
            catalog_fingerprint="c",
            id="raid",
            phase="stock",
            ammo_item=RED,
            requirements={RED: 200},
            active_stage=0,
            stages=[dict(posts=[dict(name=n) for n in ("a", "b", "c")], bindings={})],
        )
        (self.root / "nest-assault.json").write_text(json.dumps(assault))
        self.assertEqual(active_reservations(self.root, "w", "c")["count"], 200)
        assault.update(started_tick=10, phase="deploy")
        assault["stages"][0]["bindings"]["a"] = 7
        (self.root / "nest-assault.json").write_text(json.dumps(assault))
        self.assertEqual(active_reservations(self.root, "w", "c")["count"], 40)


@unittest.skipUnless(LuaRuntime, "optional executable native Lua fixture")
class NativePostAmmoTests(PostAmmoFixture):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)

        def table(v):
            if isinstance(v, dict):
                return self.lua.table_from({k: table(a) for k, a in v.items()})
            if isinstance(v, list):
                return self.lua.table_from([table(a) for a in v])
            return v

        def decode(v):
            if not hasattr(v, "items"):
                return v
            keys = list(v.keys())
            if keys and all(type(k) is int for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
                return [decode(v[i]) for i in range(1, len(keys) + 1)]
            return {k: decode(a) for k, a in v.items()}

        self.lua.globals().helpers = self.lua.table_from({"json_to_table": lambda raw: table(json.loads(raw))})
        self.lua.execute("""
game={tick=20};f={recipes={["piercing-rounds-magazine"]={enabled=true}}};s={};d={world_id="w"}
defines={inventory={turret_ammo=1}};prototypes={item={},entity={["gun-turret"]={attack_parameters={range=18}}}}
for _,name in ipairs{"firearm-magazine","piercing-rounds-magazine"}do
 prototypes.item[name]={type="ammo",magazine_size=10,stack_size=100,ammo_category={name="bullet"}}end
function slot(name,count,ammo)
 local obj={data={valid_for_read=not not name,name=name,count=count or 0,ammo=ammo or 0,quality={name="normal"},
  prototype=name and prototypes.item[name],health=1,spoil_tick=0}}
 setmetatable(obj,{__index=function(q,k)return q.data[k]end,__newindex=function(q,k,v)
  q.data[k]=v;if k=="count"and v==0 then q.data.valid_for_read=false end end})
 obj.swap_stack=function(other)local a,b=obj.data,other.data;obj.data=b;other.data=a;return true end
 return obj
end
function inventory(n)
 local inv={valid=true};for i=1,n do inv[i]=slot()end
 inv.get_insertable_count=function(spec)
  local total=0;for i=1,#inv do local st=inv[i]
   if not st.valid_for_read then total=total+100 elseif st.name==spec.name then total=total+100-st.count end
  end;return total
 end
 inv.insert=function(spec)
  local remaining=spec.count
  for i=1,#inv do local st=inv[i]
   if st.valid_for_read and st.name==spec.name then local n=math.min(100-st.count,remaining);st.count=st.count+n;remaining=remaining-n end
  end
  for i=1,#inv do if remaining<=0 then break end
   if not inv[i].valid_for_read then local n=math.min(100,remaining);inv[i]=slot(spec.name,n,10);remaining=remaining-n end
  end;return spec.count-remaining
 end;return inv
end
main=inventory(5);main[1]=slot("piercing-rounds-magazine",11,3);main[2]=slot("firearm-magazine",2,4)
a={valid=true,unit_number=11,force=f,surface=s,get_main_inventory=function()return main end,can_reach_entity=function()return true end}
world={};function key(p)return p.x..":"..p.y end
function gun(unit,x,y)
 local inv=inventory(1);inv[1]=slot("firearm-magazine",10,7)
 local e={valid=true,unit_number=unit,force=f,surface=s,position={x=x,y=y},quality={name="normal"},health=400,max_health=400,
 get_inventory=function()return inv end};world[key(e.position)]=e;return e
end
front=gun(7,0,0);rear_a=gun(8,-5,0);rear_b=gun(9,0,5)
function target(p,name)return world[key(p)]end
s.count_entities_filtered=function()return 0 end
""")
        self.game = SimpleNamespace(
            expected_world="w",
            backend="assisted",
            query=lambda script: decode(
                self.lua.execute("return(function() " + " ".join(script.splitlines()) + " end)()")
            ),
        )
        self.context()

    def act(self):
        return execute_upgrade_post_ammunition(self.game, self.action)

    def test_partial_yellow_whole_stack_swap_and_full_red_tails_conserve_each_item(self):
        result = self.act()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["yellow_recovered"], dict(count=10, rounds=97, full=9))
        self.assertEqual(result["after_post"][RED], dict(count=10, rounds=100, full=10))
        self.assertEqual((self.lua.eval("main[3].name"), self.lua.eval("main[3].ammo")), (YELLOW, 7))
        self.assertEqual((self.lua.eval("main[1].count"), self.lua.eval("main[1].ammo")), (1, 3))
        self.assertEqual((self.lua.eval("main[2].count"), self.lua.eval("main[2].ammo")), (2, 4))
        for name in (YELLOW, RED):
            for value in ("count", "rounds"):
                self.assertEqual(
                    result["before_main"][name][value] + result["before_post"][name][value],
                    result["after_main"][name][value] + result["after_post"][name][value],
                )
        self.assertEqual(result["start_tick"], result["finish_tick"])
        self.assertEqual(self.lua.eval("front.health"), 400)

    def test_firing_between_inspection_and_atomic_swap_uses_actual_partial_rounds(self):
        self.lua.execute("front.get_inventory(1)[1].ammo=3")
        result = self.act()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["yellow_recovered"]["rounds"], 93)
        self.assertEqual(self.lua.eval("main[3].ammo"), 3)

    def test_same_receipt_replay_never_charges_twice_and_unknown_intent_never_replays(self):
        first = self.act()
        second = self.act()
        self.assertTrue(second["reused_receipt"])
        self.assertEqual(second["after_main"], first["after_main"])
        self.assertEqual(self.lua.eval("main[1].count"), 1)
        self.lua.execute('d.defense_ammo_upgrades["' + self.action["transaction_id"] + '"].phase="intent"')
        result = self.act()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "post_ammunition_uncertain_prior_effects")

    def test_all_preconditions_before_first_swap_or_debit(self):
        cases = (
            "a.unit_number=12",
            'd.world_id="other"',
            "front.unit_number=70",
            "rear_a.health=1",
            "rear_b.get_inventory(1)[1].count=0",
            "main[1].ammo=1;main[1].count=10",
            'front.get_inventory(1)[1].quality.name="rare"',
            "main[1].health=.5",
            "front.get_inventory(1)[1].count=11",
            'front.get_inventory(1)[1].name="iron-plate"',
        )
        for change in cases:
            with self.subTest(change=change):
                self.setUp()
                self.lua.execute(change)
                before = self.lua.eval("main[1].count")
                r = self.act()
                self.assertFalse(r["ok"], r)
                self.assertEqual(self.lua.eval("main[1].count"), before)
                self.assertTrue(self.lua.eval("d.defense_ammo_upgrades==nil"))

    def test_no_empty_slot_means_no_yellow_spill_and_red_refill_preserves_red_partial(self):
        self.lua.execute('for i=3,5 do main[i]=slot("firearm-magazine",1,10)end')
        r = self.act()
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "post_ammunition_empty_yellow_recovery_slot_required")
        self.lua.execute('front.get_inventory(1)[1]=slot("piercing-rounds-magazine",3,6)')
        self.payload["stock"] = dict(name=RED, count=3, ammo=6)
        self.payload["action"]["count"] = 8
        self.save_intent()
        r = self.act()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["after_post"][RED], dict(count=11, rounds=106, full=10))

    def test_reserved_raid_stock_cannot_be_spent_by_conversion(self):
        sweep = dict(
            world_id="w",
            catalog_fingerprint="c",
            id="raid",
            phase="stock",
            ammo_item=RED,
            requirements={RED: 200},
            wave={},
        )
        (self.root / "nest-sweep.json").write_text(json.dumps(sweep))
        self.payload["reservation"] = active_reservations(self.root, "w", "c")
        self.save_intent()
        self.lua.execute(
            'main[1]=slot("piercing-rounds-magazine",100,10);main[3]=slot("piercing-rounds-magazine",100,10)'
        )
        r = self.act()
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "post_ammunition_full_red_prepayment_or_reserved_stock_missing")
        self.assertEqual(self.lua.eval("main[1].count"), 100)

    def test_native_scripts_survive_project_query_flattening(self):
        self.assertNotIn("--", UPGRADE_POST_AMMUNITION_LUA)
        self.assertNotIn("set_stack", UPGRADE_POST_AMMUNITION_LUA)
        self.assertNotIn("drain_ammo", UPGRADE_POST_AMMUNITION_LUA)
        self.assertNotIn("add_ammo", UPGRADE_POST_AMMUNITION_LUA)
        self.assertTrue(self.act()["ok"])


if __name__ == "__main__":
    unittest.main()
