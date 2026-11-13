import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent import plan_needs, postmortem, remedy
from factorio_agent.oil_gathering import (
    OIL_SCOUT_GENERATE_WAIT_TICKS,
    OIL_SCOUT_MAX_TARGETS,
    OIL_SCOUT_MOVES_PER_TARGET,
    OIL_SURVEY_LUA,
    OIL_SURVEY_RADIUS_MAX,
    OilGathering,
    _key,
    _xy,
    oil_core,
    survey_radius,
    survey_rings,
)
from factorio_agent.resource_policy import mining_reservations

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


def catalog():
    return SimpleNamespace(
        fingerprint="catalog",
        technologies={"oil-processing": {"research_trigger": {"type": "mine-entity", "entities": ["crude-oil"]}}},
        entities={
            "pumpjack": {
                "selection_box": {"left_top": {"x": -1.5, "y": -1.5}, "right_bottom": {"x": 1.5, "y": 1.5}},
                "fluidbox_prototypes": [
                    {
                        "index": 1,
                        "production_type": "output",
                        "volume": 1000,
                        "pipe_connections": [
                            {
                                "connection_type": "normal",
                                "direction": 0,
                                "flow_direction": "output",
                                "positions": [
                                    {"x": 1, "y": -1},
                                    {"x": 1, "y": 1},
                                    {"x": -1, "y": 1},
                                    {"x": -1, "y": -1},
                                ],
                            }
                        ],
                    }
                ],
            }
        },
    )


class OilGatheringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = catalog()
        self.board = {"world_id": "w", "catalog_fingerprint": "catalog", "blocks": {}}
        (self.root / "pcb-board.json").write_text(json.dumps(self.board))
        self.game = SimpleNamespace(
            cfg=SimpleNamespace(runtime_dir=self.root),
            query=Mock(
                side_effect=lambda body: (
                    {"ok": True, "wells": [{"position": {"x": 10.5, "y": -20.5}, "amount": 10000}]}
                    if "local wells={}" in body
                    else {"ok": True}
                )
            ),
        )
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        self.builder = SimpleNamespace(
            _occupied_by_plan=geometry._occupied_by_plan,
            ensure_plan=Mock(return_value={"type": "craft", "recipe": "pumpjack", "count": 1}),
        )
        self.perimeter = SimpleNamespace(
            prepare_coverage=Mock(
                return_value={"ok": True, "new_posts": [], "requirements": {"gun-turret": 1, "firearm-magazine": 10}}
            ),
            ensure_coverage=Mock(return_value=None),
            coverage_proof=Mock(return_value={"geometry": {"complete": True}}),
        )
        self.c = OilGathering(self.game, self.builder, self.catalog, self.perimeter)
        self.obs = {
            "ok": True,
            "world_id": "w",
            "tick": 10,
            "entities": [],
            "technologies": {"oil-gathering": True},
            "enabled_recipes": {"pumpjack": True},
        }
        self.preflight = patch(
            "factorio_agent.oil_gathering.preflight", return_value={"ok": True, "clear_obstacles": []}
        ).start()
        self.power = patch(
            "factorio_agent.oil_gathering.power_connection", return_value={"ok": True, "entities": [], "wire_hops": 1}
        ).start()
        self.addCleanup(patch.stopall)

    def install(self):
        self.c.state = self.c.prepare(self.obs)
        self.assertTrue(self.c.state["ok"], self.c.state)
        self.obs["entities"] = [dict(e, unit_number=i + 10) for i, e in enumerate(self.c.state["plan"]["entities"])]
        self.assertIsNone(self.c._bind(self.obs))

    def test_geometry_uses_all_four_rotated_native_external_ports_and_paid_items(self):
        expected = {0: (1, -2), 4: (2, 1), 8: (-1, 2), 12: (-2, -1)}
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        for direction, (x, y) in expected.items():
            p = oil_core(
                self.catalog, {"x": 0.5, "y": 0.5}, direction, power_offset=(0, 2) if direction != 8 else (0, -2)
            )
            self.assertEqual(p["pipes"][0]["position"], {"x": x + 0.5, "y": y + 0.5})
            boxes = [geometry._occupied_by_plan([e]) for e in p["entities"]]
            self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))
            self.assertEqual(p["required_items"], {"pipe": 3, "small-electric-pole": 1, "pumpjack": 1})
            self.assertEqual(p["ports"][0]["kind"], "fluid")
            self.assertTrue(all("mining_resource" not in e for e in p["entities"] if e["name"] != "pumpjack"))

    def test_unsupported_connection_catalog_blocks_instead_of_guessing(self):
        self.catalog.entities["pumpjack"]["fluidbox_prototypes"][0]["pipe_connections"][0]["positions"] = []
        self.assertEqual(self.c.prepare(self.obs)["reason"], "unsupported_native_pumpjack_geometry")
        self.power.assert_not_called()

    def test_prepare_requires_natural_unlock_and_trigger_before_native_io(self):
        self.assertEqual(
            self.c.prepare({**self.obs, "technologies": {}})["reason"],
            "natural_oil_gathering_and_enabled_pumpjack_required",
        )
        self.catalog.technologies["oil-processing"]["research_trigger"]["type"] = "nothing"
        self.assertEqual(
            self.c.prepare(self.obs)["reason"], "installed_oil_processing_crude_extraction_trigger_required"
        )
        self.game.query.assert_not_called()

    def test_prepare_is_readonly_and_needs_whole_plan_and_defense_preflight(self):
        before = (self.root / "pcb-board.json").read_bytes()
        with patch("factorio_agent.oil_gathering._atomic_json") as write:
            result = self.c.prepare(self.obs)
            write.assert_not_called()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.c.state, {})
        self.assertEqual((self.root / "pcb-board.json").read_bytes(), before)
        self.assertFalse(self.c.path.exists())
        self.assertGreaterEqual(self.preflight.call_count, 2)
        self.perimeter.prepare_coverage.assert_called_once()

    def test_existing_fluid_network_is_not_reused_or_contaminated(self):
        self.game.query.side_effect = lambda body: (
            {"ok": True, "wells": [{"position": {"x": 10.5, "y": -20.5}, "amount": 10000}]}
            if "local wells={}" in body
            else {"ok": False, "reason": "oil_outlet_near_existing_fluid_network"}
        )
        result = self.c.prepare(self.obs)
        self.assertFalse(result["ok"])
        self.assertIn("oil_outlet_near_existing_fluid_network", result["candidate_failures"])
        self.power.assert_not_called()

    def test_defense_action_blocks_all_pumpjack_construction(self):
        self.perimeter.ensure_coverage.return_value = {"type": "craft", "recipe": "firearm-magazine", "count": 10}
        result = self.c.step(self.obs)
        self.assertEqual(result["recipe"], "firearm-magazine")
        self.builder.ensure_plan.assert_not_called()
        block = json.loads((self.root / "pcb-board.json").read_text())["blocks"]["oil_gathering"]
        self.assertEqual(block["mining_reserved_area"], self.c.state["core"]["mining_footprint"])
        self.assertEqual(
            mining_reservations(self.game, "w"), [{"resource": "crude-oil", "area": block["mining_reserved_area"]}]
        )

    def test_ready_coverage_delegates_only_ordinary_paid_construction(self):
        action = self.c.step(self.obs)
        self.assertEqual(action, {"type": "craft", "recipe": "pumpjack", "count": 1})
        self.assertFalse(self.c.sources_path.exists())

    def test_changed_registered_oil_layout_is_not_overwritten(self):
        self.c.step(self.obs)
        board = json.loads((self.root / "pcb-board.json").read_text())
        board["blocks"]["oil_gathering"]["ports"][0]["position"]["x"] += 1
        (self.root / "pcb-board.json").write_text(json.dumps(board))
        before = (self.root / "pcb-board.json").read_bytes()
        self.builder.ensure_plan.reset_mock()
        self.assertEqual(self.c.step(self.obs)["reason"], "oil_gathering_reservation_changed")
        self.assertEqual((self.root / "pcb-board.json").read_bytes(), before)
        self.builder.ensure_plan.assert_not_called()

    def test_crude_reservation_cannot_expand_exemption_over_pipes_or_service_area(self):
        self.c.step(self.obs)
        board = json.loads((self.root / "pcb-board.json").read_text())
        board["blocks"]["oil_gathering"]["mining_reserved_area"] = self.c.state["core"]["reserved_area"]
        (self.root / "pcb-board.json").write_text(json.dumps(board))
        with self.assertRaises(ValueError):
            mining_reservations(self.game, "w")

    def test_saved_units_never_adopt_replacements_or_missing_assets(self):
        self.install()
        old = self.obs["entities"][0]["unit_number"]
        self.obs["entities"][0]["unit_number"] += 100
        self.assertEqual(self.c.step(self.obs)["reason"], "owned_oil_asset_missing_or_replaced")
        self.assertEqual(self.c.state["bindings"][_key(self.obs["entities"][0])], old)
        self.builder.ensure_plan.assert_not_called()

    def test_changed_world_blocks_before_build_or_query(self):
        self.install()
        self.game.query.reset_mock()
        self.assertEqual(
            self.c.step({**self.obs, "world_id": "other"})["reason"], "oil_gathering_world_or_catalog_changed"
        )
        self.game.query.assert_not_called()
        self.builder.ensure_plan.assert_not_called()

    def test_native_crude_peer_or_natural_research_failure_cannot_publish_source(self):
        self.install()
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.game.query.side_effect = None
        for reason in (
            "native_crude_pipe_peer_missing",
            "pumpjack_not_normally_powered",
            "native_crude_and_natural_research_required",
            "native_oil_asset_identity_changed",
        ):
            self.game.query.return_value = {"ok": False, "reason": reason}
            self.assertEqual(self.c.step(self.obs)["status"], "waiting")
            self.assertFalse(self.c.sources_path.exists())
        script = self.game.query.call_args.args[0]
        self.assertIn("previous.get_fluid_box_neighbours(1)", script)
        self.assertIn("e.get_fluid_box_neighbours(1)", script)
        self.assertIn('not f.technologies["oil-processing"].researched', script)
        self.assertNotIn("--", " ".join(script.splitlines()))
        for forbidden in (
            "set_research",
            ".researched=true",
            "insert_fluid",
            "set_fluid",
            "create_entity",
            "mine_entity",
        ):
            self.assertNotIn(forbidden, script)

    def native_success(self):
        chain = [self.c.state["core"]["pumpjack"]] + self.c.state["core"]["pipes"]
        return {
            "ok": True,
            "tick": 55,
            "oil_processing_researched": True,
            "chain": [dict(e, unit_number=self.c.state["bindings"][_key(e)], crude=10) for e in chain],
        }

    def test_proven_source_publishes_exact_output_unit_and_preserves_other_sources(self):
        self.install()
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.game.query.side_effect = None
        self.game.query.return_value = self.native_success()
        other = {"world_id": "w", "catalog_fingerprint": "catalog", "sources": {"water": {"unit_number": 99}}}
        self.c.sources_path.write_text(json.dumps(other))
        result = self.c.step(self.obs)
        self.assertEqual(result["status"], "succeeded")
        saved = json.loads(self.c.sources_path.read_text())
        self.assertEqual(saved["sources"]["water"], {"unit_number": 99})
        source = saved["sources"]["crude-oil"]
        self.assertEqual(source["unit_number"], self.native_success()["chain"][-1]["unit_number"])
        self.assertEqual((source["kind"], source["item"], source["direction"]), ("fluid", "crude-oil", "output"))

    def test_other_registered_crude_identity_is_never_overwritten(self):
        self.install()
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.game.query.side_effect = None
        self.game.query.return_value = self.native_success()
        other = {"world_id": "w", "catalog_fingerprint": "catalog", "sources": {"crude-oil": {"unit_number": 999}}}
        self.c.sources_path.write_text(json.dumps(other))
        before = self.c.sources_path.read_bytes()
        self.assertEqual(self.c.step(self.obs)["reason"], "published_crude_output_identity_changed")
        self.assertEqual(self.c.sources_path.read_bytes(), before)

    @unittest.skipUnless(LuaRuntime, "optional Lua runtime: pip install lupa")
    def test_flattened_native_proof_executes_real_peer_power_fluid_and_trigger_guards(self):
        self.install()
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
            keys = list(v.keys())
            if keys and all(type(k) is int for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
                return [decode(v[i]) for i in range(1, len(keys) + 1)]
            return {k: decode(a) for k, a in v.items()}

        chain = [self.c.state["core"]["pumpjack"]] + self.c.state["core"]["pipes"]
        lua.globals().specs = table([dict(e, unit_number=self.c.state["bindings"][_key(e)]) for e in chain])
        lua.globals().well_position = table(self.c.state["well"]["position"])
        lua.globals().helpers = lua.table_from({"json_to_table": lambda raw: table(json.loads(raw))})
        lua.execute("""f={technologies={["oil-processing"]={researched=true}}};s={};game={tick=100};powered=true;one_way=0;foreign_fluid=false
function pos(p)return {x=p.x,y=p.y}end
local function setup(i,e)
 e.force=f;e.surface=s;e.energy=100;e.is_connected_to_electric_network=function()return powered end
 e.get_fluid_contents=function()if foreign_fluid then return {water=10}else return {["crude-oil"]=10}end end
 e.get_fluid_box_neighbours=function()local peers={}
  if i>1 and one_way~=i then peers[#peers+1]={entity=specs[i-1],index=1}end
  if i<#specs then peers[#peers+1]={entity=specs[i+1],index=1}end
  return peers end
end
for i,e in ipairs(specs)do setup(i,e)end
function target(p,name)
 if name=="crude-oil"then return {name=name,amount=10000,position=well_position}end
 for _,e in ipairs(specs)do if e.name==name and e.position.x==p.x and e.position.y==p.y then return e end end end""")
        self.game.query = lambda body: decode(
            lua.execute("return (function() " + " ".join(body.splitlines()) + " end)()")
        )
        self.assertIsNotNone(self.c.proof(self.obs))
        for change, restore in (
            ("one_way=3", "one_way=0"),
            ("powered=false", "powered=true"),
            ('f.technologies["oil-processing"].researched=false', 'f.technologies["oil-processing"].researched=true'),
            ("foreign_fluid=true", "foreign_fluid=false"),
            ("specs[2].unit_number=999", "specs[2].unit_number=" + str(self.c.state["bindings"][_key(chain[1])])),
        ):
            with self.subTest(change=change):
                lua.execute(change)
                self.assertIsNone(self.c.proof(self.obs))
                lua.execute(restore)


class OilFieldSurveyTests(unittest.TestCase):
    """No crude-oil field in range: survey rings, nearest connection, scouting, needs, the remedy rung."""

    setUp = OilGatheringTests.setUp

    def survey(self, wells=(), scout=None, unknown=0, isolate=lambda body: {"ok": True}):
        reply = {"ok": True, "wells": list(wells), "unknown_chunks": unknown}
        if scout:
            reply["scout"] = scout
        self.game.query.side_effect = lambda body: reply if "local wells={}" in body else isolate(body)

    def payload(self):
        body = [c.args[0] for c in self.game.query.call_args_list if "local wells={}" in c.args[0]][-1]
        return json.loads(json.loads(body.split("helpers.json_to_table(", 1)[1].split(");local c=", 1)[0]))

    def well(self, x, y, distance):
        return {"position": {"x": x, "y": y}, "amount": 10000, "distance": distance}

    def test_rings_widen_to_a_hard_maximum_only_under_the_remedy_rung(self):
        self.assertEqual(survey_rings(survey_radius()), [256, 384, 512])
        with remedy.applied({"oil_survey_radius": 5000}):
            self.assertEqual(survey_radius(), OIL_SURVEY_RADIUS_MAX)
            self.assertEqual(survey_rings(survey_radius()), [256, 384, 512, 640, 768, 896, 1024])
        with remedy.applied({"oil_survey_radius": "far"}):
            self.assertEqual(survey_radius(), 512)
        rung = next(r for r in remedy.ladder("oil_gathering") if r.name == "site:wider_survey")
        self.assertEqual(rung.options["oil_survey_radius"], OIL_SURVEY_RADIUS_MAX)
        self.assertEqual(rung.kinds, ("site",))
        self.assertIn("oil_gathering", postmortem.DRY_RUN)

    def test_survey_is_centred_on_the_refinery_origin_not_a_fixed_point(self):
        self.board["blocks"]["steel_production"] = {"reserved_area": [[100, -60], [120, -40]]}
        (self.root / "pcb-board.json").write_text(json.dumps(self.board))
        self.survey([self.well(10.5, -20.5, 40)])
        self.assertTrue(self.c.prepare(self.obs)["ok"])
        args = self.payload()
        self.assertEqual(args["center"], {"x": 105.5, "y": -71.5})
        self.assertEqual(args["radius"], 512)
        with remedy.applied({"oil_survey_radius": 1024}):
            self.c.prepare(self.obs)
        self.assertEqual(self.payload()["radius"], 1024)

    def test_nearest_connection_first_ring_by_ring_with_a_per_ring_cap(self):
        near = [self.well(10.5 + 4 * i, -20.5, 100 + i) for i in range(10)]
        far = self.well(300.5, 0.5, 400)
        tried = []

        def preflight(game, builder, obs, entities):
            pump = next(e for e in entities if e["name"] == "pumpjack")["position"]
            tried.append((pump["x"], pump["y"]))
            return {"ok": pump["x"] == 300.5, "reason": "blocked", "clear_obstacles": []}

        self.preflight.side_effect = preflight
        self.survey(near + [far])
        result = self.c.prepare(self.obs)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["well"]["position"], far["position"])
        order = list(dict.fromkeys(tried))
        # Ring 256: the 8 nearest; ring 384: the 2 left over; ring 512: the far well.
        self.assertEqual(order, [_xy(w) for w in near] + [_xy(far)])

    def test_no_field_in_range_blocks_with_site_needs_naming_the_radius(self):
        self.survey([])
        result = self.c.step(self.obs)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "no_crude_field_within_survey_radius")
        needs = plan_needs.from_result("oil_gathering", result)
        self.assertEqual(needs["kind"], "site")
        self.assertEqual(needs["change"], "a crude-oil field within 512 tiles (charted)")
        tried = [{"rung": "replan", "signature": plan_needs.signature(needs)}]
        rung, _ = remedy.choose("oil_gathering", needs, tried)
        self.assertEqual(rung.name, "site:wider_survey")
        with remedy.applied(rung.options):
            needs = plan_needs.from_result("oil_gathering", dict(self.c.prepare(self.obs), status="blocked"))
        self.assertEqual(needs["change"], "a crude-oil field within 1024 tiles (charted)")
        self.assertFalse(self.c.path.exists())

    def test_wells_that_do_not_fit_also_return_site_needs(self):
        self.preflight.return_value = {"ok": False, "reason": "entity_collision"}
        self.survey([self.well(10.5, -20.5, 40)])
        result = self.c.prepare(self.obs)
        self.assertEqual(result["reason"], "no_defended_crude_well_with_native_outlet_and_power")
        needs = plan_needs.from_result("oil_gathering", dict(result, status="blocked"))
        self.assertEqual(needs["kind"], "site")
        self.assertEqual(needs["rejected_by"], {"entity_collision": 16})

    def test_ungenerated_area_scouts_with_a_paid_move_then_waits_with_a_named_unblock(self):
        scout = {"chunk": {"x": 12, "y": -3}, "distance": 420, "stand": {"x": 368.5, "y": -79.5}}
        self.survey([], scout=scout, unknown=5)
        prepared = self.c.prepare(self.obs)
        self.assertNotIn("ok", prepared)
        self.assertIsNone(remedy.fits(prepared))  # a wait, not a plan and not a dead end
        self.obs["position"] = {"x": 0, "y": 0}
        action = self.c.step(self.obs)
        self.assertEqual((action["type"], action["position"]), ("move", scout["stand"]))
        self.assertFalse(self.c.path.exists())
        self.obs["position"] = {"x": 366.0, "y": -80.0}
        waiting = self.c.step(self.obs)
        self.assertEqual(waiting["status"], "waiting")
        self.assertIn("chunk (12,-3) generated", waiting["wait_for"])
        self.obs["tick"] += OIL_SCOUT_GENERATE_WAIT_TICKS
        self.assertEqual(self.c.step(self.obs)["reason"], "oil_scout_target_stayed_ungenerated")
        self.c.prepare(self.obs)
        self.assertEqual(self.payload()["abandoned"], ["12,-3"])

    def test_scouting_is_bounded_then_blocks_with_needs(self):
        scout = {"chunk": {"x": 12, "y": -3}, "distance": 420, "stand": {"x": 368.5, "y": -79.5}}
        abandoned = ["%d,0" % i for i in range(OIL_SCOUT_MAX_TARGETS)]
        self.c.scout_path.write_text(json.dumps({"world_id": "w", "abandoned": abandoned}))
        self.survey([], scout=scout, unknown=7)
        result = self.c.step(self.obs)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("7 chunks unexplored, scouting exhausted", result["plan_needs"]["change"])
        self.c.scout_path.write_text(json.dumps({"world_id": "other", "abandoned": abandoned}))
        self.obs["position"] = {"x": 0, "y": 0}
        self.assertEqual(self.c.step(self.obs)["type"], "move")

    @unittest.skipUnless(LuaRuntime, "optional Lua runtime: pip install lupa")
    def test_survey_lua_sorts_by_connection_and_scouts_beside_the_nearest_ungenerated_chunk(self):
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
            keys = list(v.keys())
            if keys and all(type(k) is int for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
                return [decode(v[i]) for i in range(1, len(keys) + 1)]
            return {k: decode(a) for k, a in v.items()}

        lua.globals().helpers = lua.table_from({"json_to_table": lambda raw: table(json.loads(raw))})
        lua.execute("""
function pos(p)return {x=p.x,y=p.y}end
ores={{position={x=200.5,y=0.5},amount=10},{position={x=30.5,y=-10.5},amount=10},
 {position={x=-60.5,y=0.5},amount=0},{position={x=700.5,y=0.5},amount=10}}
enemies={}
s={}
function s.find_entities_filtered(q)local out={}
 for _,e in ipairs(ores)do if (e.position.x-q.position.x)^2+(e.position.y-q.position.y)^2<=q.radius^2 then out[#out+1]=e end end
 return out end
function s.is_chunk_generated(c)return c.x<=5 end
water={}
function s.find_non_colliding_position(name,p,r,precision)
 if water[math.floor(p.x/32)..","..math.floor(p.y/32)] then return nil end
 return {x=p.x,y=p.y} end
function s.count_entities_filtered(q)local n=0
 for _,e in ipairs(enemies)do if e.x>=q.area[1][1] and e.x<q.area[2][1] and e.y>=q.area[1][2] and e.y<q.area[2][2] then n=n+1 end end
 return n end""")
        args = {
            "center": {"x": 0.5, "y": 0.5},
            "radius": 256,
            "limit": 8,
            "abandoned": [],
            "window": 4,
            "candidates": 16,
            "stand_radius": 16,
        }

        def run(a):
            body = OIL_SURVEY_LUA.replace("PAYLOAD", json.dumps(json.dumps(a)))
            self.assertNotIn("--", body)
            return decode(lua.execute("return (function() " + " ".join(body.splitlines()) + " end)()"))

        result = run(args)
        self.assertEqual([w["position"]["x"] for w in result["wells"]], [30.5, 200.5])
        self.assertEqual(result["wells"][0]["distance"], 41)
        self.assertGreater(result["unknown_chunks"], 0)
        self.assertEqual(result["scout"]["chunk"], {"x": 6, "y": 0})
        self.assertEqual(result["scout"]["stand"], {"x": 5 * 32 + 16.5, "y": 16.5})
        lua.execute("enemies={{x=180,y=10}}")
        result = run(args)
        self.assertNotEqual(result["scout"]["stand"], {"x": 5 * 32 + 16.5, "y": 16.5})
        lua.execute("enemies={}")
        result = run(dict(args, abandoned=["6,0"]))
        self.assertNotEqual(result["scout"]["chunk"], {"x": 6, "y": 0})
        # eval-1575138435: the chunk beside the target was deep water; the stand is the nearest land chunk.
        lua.execute('water={["5,0"]=true}')
        result = run(args)
        self.assertEqual(result["scout"]["chunk"], {"x": 6, "y": 0})
        self.assertNotEqual(result["scout"]["stand"], {"x": 5 * 32 + 16.5, "y": 16.5})
        self.assertIn(result["scout"]["stand"]["x"], (4 * 32 + 16.5, 5 * 32 + 16.5))
        lua.execute("water={}")

    def test_a_scout_target_the_moves_do_not_reach_is_given_up(self):
        # eval-1575138435, 320 min: every move to the stand failed position_blocked; after
        # OIL_SCOUT_MOVES_PER_TARGET moves without getting closer the target is abandoned.
        scout = {"chunk": {"x": 24, "y": -6}, "distance": 900, "stand": {"x": 752.5, "y": -175.5}}
        self.survey([], scout=scout, unknown=5)
        self.obs["position"] = {"x": 80.5, "y": -751.5}
        for _ in range(OIL_SCOUT_MOVES_PER_TARGET):
            self.assertEqual(self.c.step(self.obs)["type"], "move")
        waiting = self.c.step(self.obs)
        self.assertEqual((waiting["status"], waiting["reason"]), ("waiting", "oil_scout_target_unreachable"))
        self.c.prepare(self.obs)
        self.assertEqual(self.payload()["abandoned"], ["24,-6"])

    def test_a_walk_that_gets_closer_is_not_given_up(self):
        scout = {"chunk": {"x": 24, "y": -6}, "distance": 900, "stand": {"x": 752.5, "y": -175.5}}
        self.survey([], scout=scout, unknown=5)
        for x in range(0, 100 * (OIL_SCOUT_MOVES_PER_TARGET + 3), 100):
            self.obs["position"] = {"x": float(x), "y": -175.5}
            self.assertEqual(self.c.step(self.obs)["type"], "move")


if __name__ == "__main__":
    unittest.main()
