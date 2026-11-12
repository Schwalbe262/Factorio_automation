import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.nearby_threats import (
    NearbyThreats,
    NEARBY_THREATS_SURVEY_LUA,
    BASTION_PREFLIGHT_LUA,
    attack_contract,
    classify_report,
    factory_reference_specs,
    validate_survey,
)

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


def spec(name, x, y, unit=None):
    q = {"name": name, "position": {"x": x, "y": y}}
    if unit is not None:
        q["unit_number"] = unit
    return q


class ThreatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cat = SimpleNamespace(
            fingerprint="c", recipes={n: {} for n in ("gun-turret", "firearm-magazine", "stone-wall", "repair-pack")}
        )
        self.board = {
            "world_id": "w",
            "catalog_fingerprint": "c",
            "blocks": {
                "production": {
                    "entities": [
                        spec("assembling-machine-1", 30.5, 0.5),
                        spec("gun-turret", 99, 0),
                        spec("small-electric-pole", 150, 0.5),
                    ]
                },
                "forward": {
                    "entities": [
                        spec("gun-turret", 300, 0),
                        spec("stone-wall", 301.5, 0.5),
                        spec("wooden-chest", 300, 0.5),
                    ]
                },
            },
        }
        (self.root / "pcb-board.json").write_text(json.dumps(self.board))
        self.obs = {"ok": True, "world_id": "w", "entities": [spec("assembling-machine-1", 30.5, 0.5, 1)]}
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root), query=Mock())
        self.s = NearbyThreats(self.game, self.cat)

    def target(self, unit=5, x=40, y=0, kind="unit-spawner"):
        return {
            "name": "biter-spawner" if kind == "unit-spawner" else "small-worm-turret",
            "type": kind,
            "unit_number": unit,
            "world_id": "w",
            "surface_index": 1,
            "force_index": 2,
            "position": {"x": x, "y": y},
            "charted": True,
            "factory_distance": 10,
            "health": 579,
            "max_health": 579,
            "combat": {"resistances": {"physical": {"decrease": 2, "percent": 0.15}}},
        }

    def report(self):
        return {
            "ok": True,
            "world_id": "w",
            "tick": 10,
            "surface_name": "nauvis",
            "surface_index": 1,
            "enemy_force_index": 2,
            "gun": {"range": 18, "damage_modifier": 1},
            "ammo": {"physical_damage": 5, "magazine_size": 10},
            "force_modifiers": {"ammo_damage": 0.1, "turret_damage": 0.1},
            "actor": {"character_controller": True},
            "targets": [self.target()],
        }

    def test_references_exclude_all_forward_posts_poles_walls_and_depots(self):
        refs = factory_reference_specs(self.board, self.obs, self.cat)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["expected_unit"], 1)
        self.assertEqual(refs[0]["name"], "assembling-machine-1")
        self.board["blocks"]["forward"]["entities"].append(spec("gun-turret", 38, 0, 999))
        self.assertEqual(factory_reference_specs(self.board, self.obs, self.cat), refs)

    def test_bad_frame_and_all_military_only_board_fail_before_query(self):
        self.board["world_id"] = "other"
        (self.root / "pcb-board.json").write_text(json.dumps(self.board))
        self.assertFalse(self.s.survey(self.obs)["ok"])
        self.game.query.assert_not_called()
        self.board["world_id"] = "w"
        self.board["blocks"].pop("production")
        with self.assertRaises(ValueError):
            factory_reference_specs(self.board, self.obs, self.cat)

    def test_strict_bounded_parameters(self):
        for args in ((97, 128, 48), (96, 129, 48), (96, True, 48), (0, 1, 1), (10, 1, 48)):
            with self.assertRaises(ValueError):
                validate_survey(*args)

    def test_native_fractional_armor_and_damage_modifiers_without_fake_dps_claim(self):
        r = classify_report(self.report())
        q = r["targets"][0]
        self.assertAlmostEqual(r["raw_physical_damage_per_round"], 6.05)
        self.assertAlmostEqual(q["estimated_physical_damage_per_round_lower_bound"], 3.4425)
        self.assertEqual(q["static_health_rounds_bound_without_healing"], 169)
        r = self.report()
        r["targets"][0]["combat"]["resistances"]["physical"]["decrease"] = 10
        self.assertEqual(classify_report(r)["targets"][0]["armor_warning"], "raw_damage_at_or_below_flat_armor")

    def test_worm_range_disadvantage_is_explicit_and_remote_oil_cluster_is_not_attacked(self):
        r = self.report()
        worm = self.target(6, 42, 0, "turret")
        worm["combat"]["range"] = 25
        worm["factory_distance"] = 9
        r["targets"] += [worm, self.target(7, 40, -250, "turret")]
        r = classify_report(r)
        p = attack_contract(r, self.cat)
        self.assertTrue(next(q for q in r["targets"] if q["unit_number"] == 6)["range_disadvantage"])
        self.assertEqual({q["unit_number"] for q in p["target_identities"]}, {5, 6})
        self.assertFalse(p["ready_for_attack"])
        self.assertEqual(p["requirements"]["stone-wall"], 72)
        self.assertIn("worms_outrange_guns_explicit_under_fire_damage_budget_required", p["blockers"])

    def test_foreign_or_uncharted_receipt_is_rejected(self):
        for key, value in (("world_id", "other"), ("charted", False), ("force_index", 1), ("unit_number", True)):
            r = self.report()
            r["targets"][0][key] = value
            self.game.query.return_value = r
            self.assertEqual(self.s.survey(self.obs)["reason"], "nearby_threats_native_survey_receipt_invalid")

    def test_survey_and_prepare_never_persist_or_emit_gameplay_actions(self):
        self.game.query.return_value = self.report()
        before = (self.root / "pcb-board.json").read_bytes()
        r = self.s.prepare(self.obs)
        self.assertTrue(r["ok"])
        self.assertFalse(r["proposal"]["ready_for_attack"])
        self.assertEqual((self.root / "pcb-board.json").read_bytes(), before)
        self.assertEqual([p.name for p in self.root.iterdir()], ["pcb-board.json"])
        for lua in (NEARBY_THREATS_SURVEY_LUA, BASTION_PREFLIGHT_LUA):
            for forbidden in (
                ".destroy(",
                ".damage(",
                ".insert(",
                ".remove(",
                ".set_priority_target(",
                "create_entity",
                "set_research",
                "e.health=",
            ):
                self.assertNotIn(forbidden, lua)

    def test_candidate_review_rejects_overlap_and_excessive_walk_budget_without_query(self):
        same = [[{"x": 1, "y": 1}] * 3]
        self.assertEqual(
            self.s.preflight_bastions(self.obs, same, backstop_position={"x": 1, "y": 1})["reason"],
            "nearby_threats_prospective_bastion_overlap",
        )
        far = [[{"x": 1, "y": 1}, {"x": 1000, "y": 1}, {"x": 6, "y": 5}]]
        self.assertEqual(
            self.s.preflight_bastions(self.obs, far, backstop_position={"x": 1, "y": 1})["reason"],
            "nearby_threats_local_service_budget_exceeded",
        )
        self.game.query.assert_not_called()

    @unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
    def test_actual_lua_charted_only_world_units_ranges_armor_and_partial_ammo(self):
        lua = LuaRuntime(unpack_returned_tuples=True)

        def table(v):
            if isinstance(v, dict):
                return lua.table_from({k: table(a) for k, a in v.items()})
            if isinstance(v, list):
                return lua.table_from([table(a) for a in v])
            return v

        def decode(v):
            if not hasattr(v, "items"):
                return v
            d = dict(v.items())
            if d and set(d) == set(range(1, len(d) + 1)):
                return [decode(d[i]) for i in range(1, len(d) + 1)]
            return {k: decode(a) for k, a in d.items()}

        lua.globals().helpers = lua.table_from({"json_to_table": lambda raw: table(json.loads(raw))})
        lua.execute("""d={world_id="w"};game={tick=20,forces={}};defines={inventory={turret_ammo=1,character_ammo=2},controllers={character=1}}
f={index=1,name="player",recipes={}};enemy={index=2,name="enemy"};game.forces.enemy=enemy;s={name="nauvis",index=1};calls={};all={};hostile=true
f.get_friend=function()return not hostile end;f.get_cease_fire=function()return false end
f.get_ammo_damage_modifier=function()return .1 end;f.get_gun_speed_modifier=function()return .1 end;f.get_turret_attack_modifier=function()return .1 end
enemy.get_evolution_factor=function()return .2 end
for _,n in ipairs{"gun-turret","firearm-magazine","stone-wall","repair-pack"}do f.recipes[n]={enabled=true}end
local function ammo(damage)return{magazine_size=10,ammo_category={name="bullet"},get_ammo_type=function()return{action={type="direct",action_delivery={target_effects={{type="damage",damage={type="physical",amount=damage}}}}}}end}end
prototypes={item={["firearm-magazine"]=ammo(5),["piercing-rounds-magazine"]=ammo(8)},entity={}}
prototypes.entity["gun-turret"]={attack_parameters={range=18,min_range=0,cooldown=6,damage_modifier=1},resistances={},get_max_health=function()return 400 end}
local main={{valid_for_read=true,name="firearm-magazine",quality={name="normal"},count=2,ammo=7,health=1,spoil_tick=0,prototype=prototypes.item["firearm-magazine"]}}
local equipped={{valid_for_read=true,name="firearm-magazine",quality={name="normal"},count=3,ammo=10,health=1,spoil_tick=0,prototype=prototypes.item["firearm-magazine"]}}
a={valid=true,unit_number=11,position={x=30.5,y=.5},health=250,max_health=250,player={controller_type=1},get_main_inventory=function()return main end,get_inventory=function()return equipped end}
function add(name,kind,unit,x,y,force,proto)
 local e={valid=true,name=name,type=kind,unit_number=unit,position={x=x,y=y},force=force,surface=s,prototype=proto or{},health=200,max_health=200,quality={name="normal"}}
 all[#all+1]=e;return e end
prod=add("assembling-machine-1","assembling-machine",1,30.5,.5,f)
nest=add("biter-spawner","unit-spawner",5,28.5,.5,enemy,{resistances={physical={decrease=2,percent=.15}}});nest.health=579;nest.max_health=579
worm=add("small-worm-turret","turret",6,27.5,.5,enemy,{attack_parameters={range=25,cooldown=4},resistances={}})
hidden=add("biter-spawner","unit-spawner",7,36.5,.5,enemy,{})
gun=add("gun-turret","ammo-turret",8,30.5,5.5,f,prototypes.entity["gun-turret"]);gun.health=400;gun.max_health=400;gun.priority_targets={};gun.ignore_unprioritised_targets=false;gun.get_inventory=function()return equipped end
function target(p,name)for _,e in ipairs(all)do if e.valid and e.name==name and e.position.x==p.x and e.position.y==p.y then return e end end end
s.is_chunk_charted=nil;f.is_chunk_charted=function(surface,c)return c.x==0 and c.y==0 end
s.find_entities_filtered=function(q)
 assert(q.area and q.limit,"bounded area and native limit required")
 local l,t=q.area[1][1],q.area[1][2];assert(f.is_chunk_charted(s,{x=math.floor(l/32),y=math.floor(t/32)}),"uncharted scan")
 calls[#calls+1]=q;local found={}
 for _,e in ipairs(all)do
  if e.valid and e.force==q.force and e.position.x>=l and e.position.y>=t and e.position.x<=q.area[2][1]and e.position.y<=q.area[2][2]
  and(not q.name or e.name==q.name)and(e.type=="unit"or e.type=="unit-spawner"or e.type=="turret"or q.name=="gun-turret")then
   found[#found+1]=e;if #found==q.limit then break end
  end
 end;return found
end""")
        self.game.query.side_effect = lambda body: decode(
            lua.execute("return(function() " + " ".join(body.splitlines()) + " end)()")
        )
        r = self.s.survey(self.obs, maximum_distance=10, danger_distance=8)
        self.assertTrue(r["ok"], r)
        self.assertEqual({q["unit_number"] for q in r["targets"]}, {5, 6})
        self.assertTrue(all(q["charted"] and q["world_id"] == "w" and q["force_index"] == 2 for q in r["targets"]))
        self.assertEqual(r["gun"]["range"], 18)
        self.assertFalse(r["gun"]["requires_power"])
        self.assertEqual(r["gun"]["max_health"], 400)
        self.assertEqual(r["ammo"]["physical_damage"], 5)
        self.assertEqual(r["ammo"]["category"], "bullet")
        self.assertEqual(
            r["actor"]["main_ammo"]["by_item"]["firearm-magazine"],
            {"full_normal_magazines": 1, "partial_normal_magazines": 1, "normal_rounds": 17},
        )
        self.assertEqual(r["actor"]["main_ammo"]["normal_rounds"], 17)
        self.assertEqual(r["actor"]["equipped_ammo"]["full_normal_magazines"], 3)
        self.assertAlmostEqual(
            next(q for q in r["targets"] if q["unit_number"] == 5)["estimated_physical_damage_per_round_lower_bound"],
            3.4425,
        )
        limited = self.s.survey(self.obs, maximum_distance=10, danger_distance=8, maximum_entities=1)
        self.assertTrue(limited["truncated"])
        self.assertEqual(len(limited["targets"]), 1)
        lua.execute("hostile=false")
        r = self.s.survey(self.obs, maximum_distance=10, danger_distance=8)
        self.assertFalse(r["hostile"])
        self.assertFalse(r["targets"])
        lua.execute('hostile=true;d.world_id="other"')
        self.assertEqual(
            self.s.survey(self.obs, maximum_distance=10, danger_distance=8)["reason"],
            "nearby_threats_native_world_changed",
        )
        lua.execute('d.world_id="w";prod.unit_number=99')
        self.assertEqual(
            self.s.survey(self.obs, maximum_distance=10, danger_distance=8)["reason"],
            "nearby_threats_no_confirmed_original_factory_assets",
        )


if __name__ == "__main__":
    unittest.main()
