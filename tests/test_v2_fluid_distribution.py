import json
import re
import unittest
from copy import deepcopy
from unittest.mock import patch

from factorio_agent.board import entity
from factorio_agent.fluid_distribution import (
    FluidDistribution,
    execute_build_fluid_distribution,
    FLUID_PROOF_LUA,
    _key,
    _payload,
)
from factorio_agent.oil_gathering import OilGathering
from factorio_agent.water_gathering import WaterGathering
import test_v2_gas_distribution as gas_fixtures
import test_v2_oil_gathering as oil_fixtures
import test_v2_water_gathering as water_fixtures

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


class DistributionTests(unittest.TestCase):
    def setUp(self):
        f = gas_fixtures.GasDistributionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.f = f
        self.c = FluidDistribution(
            f.game, f.builder, f.cat, f.perimeter, source_key="petroleum-gas", aliases=("acid-gas", "rocket-gas")
        )
        self.obs = f.obs
        self.obs["enabled_recipes"] = {"pipe": True, "pipe-to-ground": True}
        self.snapshot = patch.object(
            self.c, "_source_snapshot", return_value=deepcopy(f.game.query.return_value)
        ).start()
        patch("factorio_agent.fluid_distribution.survey_slots", return_value={"ok": True, "legal": [1]}).start()
        patch("factorio_agent.fluid_distribution.preflight", return_value={"ok": True, "clear_obstacles": []}).start()
        self.handoff = patch("factorio_agent.fluid_distribution._handoff", return_value={"ok": True}).start()

        def route(game, builder, obs, source, destination, core, prior):
            sx, sy = source["position"].values()
            dx, dy = destination["position"].values()
            self.assertEqual(sy, dy)
            es = [entity("pipe", sx + i, sy) for i in range(1, int(dx - sx) + 1)]
            return dict(
                ok=True,
                entities=es,
                first_neighbor=dict(es[0], fluidbox_index=1),
                underground_pairs=[],
                maximum_underground_span=10,
                route_metrics={},
            )

        patch("factorio_agent.fluid_distribution.plan_typed_fluid_connection", side_effect=route).start()

    def install(self):
        self.c.state = self.c.prepare(self.obs)
        self.assertTrue(self.c.state["ok"], self.c.state)
        for i, q in enumerate(self.c.state["plan"]["entities"]):
            self.c.state["bindings"][_key(q)] = i + 100
            self.obs["entities"].append(dict(q, unit_number=i + 100))
        first = dict(
            self.c.state["first_neighbor"], unit_number=self.c.state["bindings"][_key(self.c.state["first_neighbor"])]
        )
        registry = deepcopy(self.f.registry)
        registry["handoffs"] = {
            self.c.consumer_id: {
                "petroleum-gas": dict(
                    world_id="w", catalog_fingerprint="c", source=self.f.source, first_neighbor=first, phase="bound"
                )
            }
        }
        self.c.sources_path.write_text(json.dumps(registry), encoding="utf-8")
        return registry

    def test_header_is_readonly_until_normal_construction_and_preserves_old_registry(self):
        before = {p.name: p.read_bytes() for p in self.f.root.glob("*.json")}
        result = self.c.prepare(self.obs)
        self.assertTrue(result["ok"], result)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.f.root.glob("*.json")})
        self.assertEqual(result["original_source"], self.f.source)
        self.assertEqual([p["item"] for p in result["plan"]["ports"]], ["petroleum-gas"] * 2)
        self.assertEqual(set(result["core"]["alias_access"]), {"acid-gas", "rocket-gas"})
        self.f.builder.ensure_plan.assert_not_called()
        self.handoff.assert_not_called()

    def test_unbuilt_external_plans_keep_the_header_off_and_return_site_needs(self):
        from factorio_agent import plan_needs

        def route(game, builder, obs, source, destination, core, prior):
            (sx, sy), (dx, dy) = source["position"].values(), destination["position"].values()
            es = [entity("pipe", sx + i, sy) for i in range(1, int(dx - sx) + 1)]
            step = 1 if dy > sy else -1
            es += [entity("pipe", dx, sy + j) for j in range(step, int(dy - sy) + step, step)]
            return dict(
                ok=True,
                entities=es,
                first_neighbor=dict(es[0], fluidbox_index=1),
                underground_pairs=[],
                maximum_underground_span=10,
                route_metrics={},
            )

        patch("factorio_agent.fluid_distribution.plan_typed_fluid_connection", side_effect=route).start()
        board_path = self.f.root / "pcb-board.json"
        blocking = []
        for _ in range(10):  # each found header is then planned by "another block" (unbuilt), until none fits
            result = self.c.prepare(self.obs)
            if not result.get("ok"):
                break
            self.assertFalse({xy for xy in map(_key, result["core"]["entities"])} & set(map(_key, blocking)))
            blocking += result["core"]["entities"]
            board = json.loads(board_path.read_text(encoding="utf-8"))
            board["blocks"]["future_feed"] = {"entities": blocking}  # planned elsewhere, nothing built yet
            board_path.write_text(json.dumps(board), encoding="utf-8")
        self.assertEqual(result["reason"], "no_isolated_same_fluid_manifold")
        needs = plan_needs.from_result("fluid_distribution", result)
        self.assertEqual(needs["kind"], "site")
        self.assertEqual(needs["blockers"][0]["owner"], "future_feed")
        self.assertIn("manifold_meets_unbuilt_external_plan", needs["rejected_by"])

    def test_claimed_source_blocks_before_any_material_action(self):
        registry = deepcopy(self.f.registry)
        registry["handoffs"] = {"old-basic": {"petroleum-gas": {"source": self.f.source}}}
        self.c.sources_path.write_text(json.dumps(registry), encoding="utf-8")
        result = self.c.prepare(self.obs)
        self.assertEqual(result["reason"], "fluid_source_edge_already_owned")
        self.f.builder.ensure_plan.assert_not_called()
        self.snapshot.assert_not_called()

    def test_native_recipe_lock_prevents_later_tier_header_construction(self):
        self.obs["enabled_recipes"]["pipe-to-ground"] = False
        self.assertFalse(self.c.prepare(self.obs)["ok"])
        self.snapshot.assert_not_called()

    def test_arbitrary_aliases_and_checkpoint_rebinding_are_rejected(self):
        for aliases in (("same", "same"), ("acid-gas", "../escape"), ("petroleum-gas", "another")):
            with self.assertRaises(ValueError):
                FluidDistribution(
                    self.f.game,
                    self.f.builder,
                    self.f.cat,
                    self.f.perimeter,
                    source_key="petroleum-gas",
                    aliases=aliases,
                )
        self.c.state = self.c.prepare(self.obs)
        self.c._save()
        state = json.loads(self.c.path.read_text())
        state["profile"]["source_key"] = "water"
        self.c.path.write_text(json.dumps(state))
        with self.assertRaises(ValueError):
            FluidDistribution(self.f.game, self.f.builder, self.f.cat, self.f.perimeter, **self.c.profile)

    def test_unknown_native_entity_is_not_adopted_as_a_paid_header_pipe(self):
        self.c.state = self.c.prepare(self.obs)
        self.obs["entities"].append(dict(self.c.state["plan"]["entities"][0], unit_number=999))
        self.assertEqual(self.c.step(self.obs)["reason"], "fluid_distribution_unbound_asset_present")

    def test_single_guarded_pipe_build_keeps_durable_exact_intent_without_publishing(self):
        build = dict(self.c.prepare(self.obs)["plan"]["entities"][0], type="build")
        self.f.builder.ensure_plan.return_value = {"type": "build_many", "actions": [build, build]}
        result = self.c.step(self.obs)
        self.assertEqual(result["type"], "build_fluid_distribution")
        self.assertEqual(result["ordinary_build"], build)
        self.assertEqual(json.loads(self.c.path.read_text())["pending_build"], result)
        self.assertNotIn("acid-gas", json.loads(self.c.sources_path.read_text())["sources"])
        self.assertEqual(self.c.step(self.obs)["reason"], "fluid_distribution_build_outcome_requires_reconciliation")

    @unittest.skipUnless(LuaRuntime, "optional isolated Lua dependency unavailable")
    def test_native_reciprocal_purity_and_actual_filled_aliases_for_any_supported_fluid(self):
        self.install()
        lua = LuaRuntime(unpack_returned_tuples=True)

        def table(v):
            if isinstance(v, dict):
                return lua.table_from({k: table(x) for k, x in v.items()})
            if isinstance(v, list):
                return lua.table_from([table(x) for x in v])
            return v

        decode = lambda v: {k: decode(x) for k, x in v.items()} if hasattr(v, "items") else v
        bindings = {**self.c.state["bindings"], _key(self.f.source): self.f.source["unit_number"]}
        lua.globals().nodes = table([dict(q, unit_number=bindings[_key(q)]) for q in self.c.state["nodes"]])
        lua.globals().edges = table(self.c.state["edges"])
        lua.globals().helpers = lua.table_from({"json_to_table": lambda v: table(json.loads(v))})
        lua.execute("""f={};s={};game={tick=50};prototypes={utility_constants={default_pipeline_extent=320}};entities={}
fluid="petroleum-gas";bad=false;empty=false;foreign=false
local function key(q)return q.name..":"..q.position.x..":"..q.position.y end
for _,e in ipairs(nodes)do entities[key(e)]=e end
local function setup(e)
 e.force=f;e.surface=s;e.has_fluid_segment=function()return true end
 e.get_fluid_segment_extent_bounding_box=function()return{left_top={x=0,y=0},right_bottom={x=50,y=10}}end
 e.get_fluid_contents=function()if empty then return{}end;return{[bad and"water"or fluid]=10}end
 e.get_fluid_box_neighbours=function()local peers={};for _,edge in ipairs(edges)do
  if key(edge.a)==key(e)then peers[#peers+1]={entity=entities[key(edge.b)],index=1}end
  if key(edge.b)==key(e)then peers[#peers+1]={entity=entities[key(edge.a)],index=1}end end
  if foreign then peers[#peers+1]={entity={unit_number=9999},index=1}end;return peers end
end
for _,e in ipairs(nodes)do setup(e)end
function target(p,name)return entities[name..":"..p.x..":"..p.y]end""")
        self.f.game.query.side_effect = lambda body: decode(lua.execute(body))
        self.assertIsNotNone(self.c.proof(self.obs), self.c.last_proof_failure)
        for change, restore in (
            ("bad=true", "bad=false"),
            ("empty=true", "empty=false"),
            ("foreign=true", "foreign=false"),
        ):
            lua.execute(change)
            self.assertIsNone(self.c.proof(self.obs), change)
            lua.execute(restore)
        # The shared native proof is parameterized, not fixed to gas.
        lua.execute('fluid="heavy-oil"')
        args = dict(
            source=self.f.source,
            fluid="heavy-oil",
            nodes=[dict(q, unit_number=bindings[_key(q)]) for q in self.c.state["nodes"]],
            edges=self.c.state["edges"],
            aliases={n: p for n, p in zip(self.c.aliases, self.c.state["core"]["ports"])},
            downstream=[],
        )
        self.assertTrue(lua.execute(FLUID_PROOF_LUA.replace("PAYLOAD", _payload(args)))["ok"])


class IndependentIntakeTests(unittest.TestCase):
    def test_water_namespace_builds_and_publishes_a_real_independent_intake(self):
        f = water_fixtures.WaterGatheringTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        original = {"kind": "fluid", "item": "water", "unit_number": 999, "position": {"x": -5, "y": -5}}
        f.registry["sources"]["water"] = deepcopy(original)
        (f.root / "fluid-sources.json").write_text(json.dumps(f.registry))
        f.c = WaterGathering(f.game, f.builder, f.cat, f.perimeter, source_key="acid-water")
        f.install()
        source = dict(
            f.c.state["core"]["ports"][0], unit_number=123, world_id="w", defense_scope_id=f.c.state["defense_scope_id"]
        )
        f.builder.ensure_plan.return_value = {"status": "succeeded"}
        with patch.object(f.c, "proof", return_value={"source": source, "tick": 20}):
            result = f.c.step(f.obs)
        self.assertEqual(result["status"], "succeeded")
        registry = json.loads((f.root / "fluid-sources.json").read_text())
        self.assertEqual(registry["sources"]["water"], original)
        self.assertEqual(registry["sources"]["acid-water"], source)
        self.assertNotEqual(f.c.path.name, "water-gathering.json")
        self.assertIn(f.c.block_id, json.loads((f.root / "pcb-board.json").read_text())["blocks"])

    def test_default_source_paths_and_invalid_profiles(self):
        f = oil_fixtures.OilGatheringTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.assertEqual(f.c.path.name, "oil-gathering.json")
        self.assertEqual(f.c.block_id, "oil_gathering")
        new = OilGathering(f.game, f.builder, f.catalog, f.perimeter, source_key="advanced-crude")
        self.assertEqual(new.path.name, "oil-gathering-advanced-crude.json")
        for invalid in ("../file", "water", "petroleum-gas"):
            with self.assertRaises(ValueError):
                OilGathering(f.game, f.builder, f.catalog, f.perimeter, source_key=invalid)

    @unittest.skipUnless(LuaRuntime, "optional isolated Lua dependency unavailable")
    def test_independent_oil_query_excludes_already_working_native_wells(self):
        f = oil_fixtures.OilGatheringTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        c = OilGathering(f.game, f.builder, f.catalog, f.perimeter, source_key="advanced-crude")
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute("""s={};s.find_entities_filtered=function(_)return{
 {amount=100,position={x=10.5,y=-20.5}},{amount=200,position={x=30.5,y=-20.5}}}end
s.count_entities_filtered=function(q)return q.position.x==10.5 and 1 or 0 end
pos=function(p)return{x=p.x,y=p.y}end
s.is_chunk_generated=function(_)return true end""")

        def query(body):
            if "local wells={}" not in body:
                return {"ok": True}
            # The survey reads its arguments through the game's helpers.json_to_table: decode them here.
            literal = re.search(r'helpers\.json_to_table\(("(?:[^"\\]|\\.)*")\)', body)
            if literal:
                args = json.loads(json.loads(literal.group(1)))

                def table(value):
                    if isinstance(value, dict):
                        return lua.table_from({k: table(v) for k, v in value.items()})
                    if isinstance(value, list):
                        return lua.table_from([table(v) for v in value])
                    return value

                lua.globals().helpers = lua.table_from({"json_to_table": lambda _text: table(args)})
            out = lua.execute(body)
            return {
                "ok": out["ok"],
                "wells": [
                    {"amount": q["amount"], "position": {"x": q["position"]["x"], "y": q["position"]["y"]}}
                    for _, q in out["wells"].items()
                ],
            }

        f.game.query.side_effect = query
        result = c.prepare(f.obs)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["well"]["position"]["x"], 30.5)
        self.assertEqual(result["source_key"], "advanced-crude")


@unittest.skipUnless(LuaRuntime, "optional isolated Lua dependency unavailable")
class AtomicBuildTests(unittest.TestCase):
    def setUp(self):
        f = DistributionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.fixture = f
        self.c = f.c
        self.game = f.f.game
        build = dict(self.c.prepare(f.obs)["plan"]["entities"][0], type="build")
        f.f.builder.ensure_plan.return_value = build
        self.action = self.c.step(f.obs)
        self.game.backend = "assisted"
        (f.f.root / "catalog.json").write_text("{}", encoding="utf-8")
        p = patch("factorio_agent.core.world_catalog.WorldCatalog.from_dict", return_value=f.f.cat)
        p.start()
        self.addCleanup(p.stop)
        self.lua = lua = LuaRuntime(unpack_returned_tuples=True)

        def table(v):
            if isinstance(v, dict):
                return lua.table_from({k: table(x) for k, x in v.items()})
            if isinstance(v, list):
                return lua.table_from([table(x) for x in v])
            return v

        self.decode = lambda v: {k: self.decode(x) for k, x in v.items()} if hasattr(v, "items") else v
        lua.globals().helpers = lua.table_from({"json_to_table": lambda v: table(json.loads(v))})
        lua.globals().source_spec = table(f.f.source)
        lua.execute("""d={world_id="w"};f={};s={};game={tick=100};world={};foreign=false;creates=0
prototypes={utility_constants={default_pipeline_extent=320},entity={pipe={items_to_place_this={{name="pipe"}}}}}
function k(p,n)return n..":"..p.x..":"..p.y end
source={valid=true,name="pipe",position=source_spec.position,unit_number=source_spec.unit_number,force=f,surface=s,direction=0}
source.has_fluid_segment=function()return true end
source.get_fluid_contents=function()return{["petroleum-gas"]=10}end
source.get_fluid_segment_extent_bounding_box=function()return{left_top={x=129,y=-62},right_bottom={x=151,y=-61}}end
world[k(source.position,source.name)]=source
function target(p,n)return world[k(p,n)]end
inv={count=2};inv.get_item_count=function(_)return inv.count end
inv.remove=function(q)local n=math.min(inv.count,q.count);inv.count=inv.count-n;return n end
inv.insert=function(q)inv.count=inv.count+q.count;return q.count end
a={valid=true,get_main_inventory=function()return inv end,position={x=0,y=0},build_distance=10}
s.count_entities_filtered=function(_)return 0 end
s.find_entities_filtered=function(_)if foreign then return{{unit_number=777,position={x=151.5,y=-60.5},prototype={fluidbox_prototypes={{}}}}}end;return{}end
s.can_place_entity=function(_)return true end
s.create_entity=function(q)creates=creates+1;local e={name=q.name,position=q.position,unit_number=900+creates,force=f,surface=s,direction=q.direction}
 world[k(q.position,q.name)]=e;return e end
success=function(t)t=t or{};t.ok=true;return t end
failure=function(reason)return{ok=false,reason=reason}end""")
        self.game.query.side_effect = lambda body: self.decode(lua.execute(body))

    def test_same_native_query_guard_and_ordinary_build_conserve_real_pipe_cost(self):
        result = execute_build_fluid_distribution(self.game, self.action)
        self.assertTrue(result["ok"])
        self.assertEqual(self.lua.eval("inv.count"), 1)
        self.assertEqual(self.lua.eval("creates"), 1)
        state = json.loads(self.c.path.read_text())
        self.assertEqual(state["bindings"][_key(self.action["ordinary_build"])], 901)
        self.assertNotIn("pending_build", state)
        self.assertEqual(self.game.query.call_count, 1)

    def test_foreign_fluid_neighbor_rejects_before_ordinary_material_debit(self):
        self.lua.execute("foreign=true")
        result = execute_build_fluid_distribution(self.game, self.action)
        self.assertFalse(result["ok"])
        self.assertTrue(result["pre_effect"])
        self.assertEqual(self.lua.eval("inv.count"), 2)
        self.assertEqual(self.lua.eval("creates"), 0)

    def test_world_identity_rejects_before_ordinary_material_debit(self):
        self.lua.execute('d.world_id="foreign"')
        result = execute_build_fluid_distribution(self.game, self.action)
        self.assertFalse(result["ok"])
        self.assertEqual(self.lua.eval("inv.count"), 2)

    def test_lost_native_receipt_keeps_intent_and_cannot_replay_after_restart(self):
        execute = self.game.query.side_effect

        def lost(body):
            execute(body)
            raise TimeoutError("lost receipt")

        self.game.query.side_effect = lost
        with self.assertRaises(TimeoutError):
            execute_build_fluid_distribution(self.game, self.action)
        self.assertEqual(self.lua.eval("inv.count"), 1)
        self.assertEqual(self.lua.eval("creates"), 1)
        state = json.loads(self.c.path.read_text())
        self.assertEqual(state["build_phase"], "outcome_unknown")
        with self.assertRaises(ValueError):
            execute_build_fluid_distribution(self.game, self.action)
        self.assertEqual(self.game.query.call_count, 1)
        self.c.state = state
        self.assertEqual(
            self.c.step(self.fixture.obs)["reason"], "fluid_distribution_build_outcome_requires_reconciliation"
        )
