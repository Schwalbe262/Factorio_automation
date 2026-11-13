import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.core.world_catalog import WorldCatalog
from factorio_agent.advanced_petroleum import (
    AdvancedPetroleum,
    advanced_refinery_core,
    advanced_petroleum_rates,
    audit_advanced_core,
    fluid_graph,
    ADVANCED_INSPECT_LUA,
    execute_configure_advanced_petroleum,
    _key,
    _digest,
    INPUT_KEYS,
    OUTPUT_KEYS,
    RECIPES,
)
from factorio_agent.board import entity

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


def catalog_payload():
    def boxes(points, directions, types):
        result = []
        for i, ((x, y), d, side) in enumerate(zip(points, directions, types), 1):
            ps = []
            for _ in range(4):
                ps.append({"x": x, "y": y})
                x, y = -y, x
            result.append(
                {
                    "index": i,
                    "production_type": side,
                    "volume": 1000,
                    "pipe_connections": [{"direction": d, "connection_type": "normal", "positions": ps}],
                }
            )
        return result

    def fluid(n, amount, index=None):
        return dict(name=n, type="fluid", amount=amount, **({"fluidbox_index": index} if index else {}))

    rf = boxes([(-1, 2), (1, 2), (-2, -2), (0, -2), (2, -2)], [8, 8, 0, 0, 0], ["input"] * 2 + ["output"] * 3)
    ch = boxes([(-1, -1), (1, -1), (-1, 1), (1, 1)], [0, 0, 8, 8], ["input"] * 2 + ["output"] * 2)
    tank = boxes([(-1, -1), (1, 1), (1, 1), (-1, -1)], [0, 4, 8, 12], ["none"] * 4)
    entities = {
        "oil-refinery": dict(
            crafting_speed=1,
            energy_usage=7000,
            crafting_categories=["oil-processing"],
            fluidbox_prototypes=rf,
            selection_box={"left_top": {"x": -2.5, "y": -2.5}, "right_bottom": {"x": 2.5, "y": 2.5}},
        ),
        "chemical-plant": dict(
            crafting_speed=1, energy_usage=3500, crafting_categories=["chemistry"], fluidbox_prototypes=ch
        ),
        "storage-tank": dict(
            energy_usage=0,
            fluidbox_prototypes=[
                dict(
                    index=1,
                    production_type="none",
                    volume=25000,
                    pipe_connections=[b["pipe_connections"][0] for b in tank],
                )
            ],
            selection_box={"left_top": {"x": -1.5, "y": -1.5}, "right_bottom": {"x": 1.5, "y": 1.5}},
        ),
    }
    recipes = {
        RECIPES[0]: dict(
            category="oil-processing",
            energy=5,
            ingredients=[fluid("water", 50, 1), fluid("crude-oil", 100, 2)],
            products=[fluid("heavy-oil", 25, 1), fluid("light-oil", 45, 2), fluid("petroleum-gas", 55, 3)],
        ),
        RECIPES[1]: dict(
            category="chemistry",
            energy=2,
            ingredients=[fluid("water", 30), fluid("heavy-oil", 40)],
            products=[fluid("light-oil", 30)],
        ),
        RECIPES[2]: dict(
            category="chemistry",
            energy=2,
            ingredients=[fluid("water", 30), fluid("light-oil", 30)],
            products=[fluid("petroleum-gas", 20)],
        ),
    }
    for name in ("pipe", "pipe-to-ground", "small-electric-pole", *entities):
        recipes.setdefault(
            name,
            dict(category="crafting", energy=0.5, ingredients=[], products=[dict(name=name, type="item", amount=1)]),
        )
    return dict(version="2.1.20", recipes=recipes, entities=entities, technologies={}, items={}, raw_sources=[])


def catalog():
    return WorldCatalog.from_dict(catalog_payload())


class AdvancedGeometryTests(unittest.TestCase):
    def test_catalog_bom_cracking_capacity_and_shared_output_budget(self):
        r = advanced_petroleum_rates(catalog())
        self.assertEqual(
            (r["crude_per_minute"], r["water_per_minute_at_full_cracking"], r["gas_per_minute_at_full_cracking"]),
            (1200, 1590, 1170),
        )
        self.assertEqual(r["gross_per_minute"], {"heavy-oil": 300, "light-oil": 540, "petroleum-gas": 660})
        self.assertEqual(r["electric_kw"], 840)
        c = catalog()
        c.recipes[RECIPES[2]]["energy"] = 10
        with self.assertRaisesRegex(ValueError, "cannot clear"):
            advanced_petroleum_rates(c)

    def test_all_five_networks_reach_actual_machine_ports_without_contact_and_power_is_connected(self):
        c = catalog()
        b = FactoryBuilder.__new__(FactoryBuilder)
        for x, y in ((0.5, 0.5), (-31.5, 64.5)):
            p = advanced_refinery_core(c, x, y)
            audit = audit_advanced_core(p, c, b)
            self.assertTrue(audit["ok"], audit)
            self.assertEqual(p["required_items"]["oil-refinery"], 1)
            self.assertEqual(p["required_items"]["chemical-plant"], 2)
            self.assertEqual(p["required_items"]["storage-tank"], 3)
            self.assertEqual(len(p["underground_pairs"]), 3)
            self.assertTrue(all(q["span"] <= 4 for q in p["underground_pairs"]))
            self.assertEqual([q["reserve"] for q in p["controls"]], [500, 500])
        bad = advanced_refinery_core(c, 0.5, 0.5)
        bad["fluid_groups"]["water"].append(entity("pipe", -18.5, -6.5))
        self.assertFalse(audit_advanced_core(bad, c, b)["ok"])

    def test_changed_native_port_geometry_fails_closed(self):
        c = catalog()
        c.entities["oil-refinery"]["fluidbox_prototypes"][0]["pipe_connections"][0]["positions"][0]["x"] = -2
        with self.assertRaisesRegex(ValueError, "geometry changed"):
            advanced_refinery_core(c, 0.5, 0.5)


class AdvancedControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.catalog = catalog()
        self.game = SimpleNamespace(
            cfg=SimpleNamespace(runtime_dir=self.tmp.name), expected_world="w", query=Mock(return_value={"ok": True})
        )
        self.builder = FactoryBuilder.__new__(FactoryBuilder)
        self.builder.ensure_plan = Mock(return_value={"status": "succeeded"})
        self.perimeter = SimpleNamespace(
            coverage_proof=Mock(return_value={"ok": True}), ensure_coverage=Mock(return_value=None)
        )
        self.controller = AdvancedPetroleum(self.game, self.builder, self.catalog, self.perimeter)
        self.obs = dict(
            ok=True,
            world_id="w",
            tick=10,
            actor_unit_number=7,
            entities=[],
            technologies={"advanced-oil-processing": True, "circuit-network": True},
            enabled_recipes={n: True for n in self.catalog.recipes},
        )

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value), encoding="utf-8")

    def test_native_technology_gate_precedes_every_query_and_does_not_persist(self):
        self.obs["technologies"]["advanced-oil-processing"] = False
        self.assertEqual(self.controller.prepare(self.obs)["reason"], "advanced_oil_and_circuit_research_required")
        self.game.query.assert_not_called()
        self.assertFalse(self.controller.path.exists())

    def test_canonical_basic_input_cannot_be_silently_reused_or_alias_invented(self):
        self.write(
            "fluid-sources.json",
            dict(
                world_id="w",
                catalog_fingerprint=self.catalog.fingerprint,
                sources={"crude-oil": dict(item="crude-oil")},
                handoffs={},
            ),
        )
        r = self.controller._sources(self.obs)
        self.assertEqual(
            (r["reason"], r["source_key"]), ("advanced_independent_fluid_source_required", "advanced-crude")
        )
        self.game.query.assert_not_called()

    def test_exact_alias_already_claimed_is_rejected_before_any_query(self):
        sources = {
            k: dict(
                kind="fluid",
                item=f,
                direction="output",
                facing=4,
                name="pipe",
                position={"x": 1.5, "y": i + 0.5},
                fluidbox_index=1,
                unit_number=i + 20,
                world_id="w",
                defense_scope_id="upstream",
            )
            for i, (f, k) in enumerate(INPUT_KEYS.items())
        }
        self.write(
            "fluid-sources.json",
            dict(
                world_id="w",
                catalog_fingerprint=self.catalog.fingerprint,
                sources=sources,
                handoffs={"basic": {"crude-oil": {"source": sources["advanced-crude"]}}},
            ),
        )
        self.assertEqual(self.controller._sources(self.obs)["reason"], "advanced_input_already_claimed")
        self.game.query.assert_not_called()

    def _whole_prepare(self):
        c = self.controller
        self.write(
            "pcb-board.json",
            dict(
                world_id="w",
                catalog_fingerprint=self.catalog.fingerprint,
                blocks={"basic_petroleum": {"reserved_area": [[0, 0], [32, 16]]}},
            ),
        )
        src = {
            f: dict(
                name="pipe",
                kind="fluid",
                item=f,
                direction="output",
                position={"x": 32.5, "y": y},
                facing=4,
                fluidbox_index=1,
                unit_number=i + 20,
                world_id="w",
                defense_scope_id="upstream",
            )
            for i, (f, y) in enumerate((("crude-oil", 6.5), ("water", 3.5)))
        }
        c._sources = Mock(
            return_value=dict(
                ok=True,
                sources=src,
                segments={
                    f: dict(extent={"left_top": {"x": 30, "y": 0}, "right_bottom": {"x": 33, "y": 10}}, limit=320)
                    for f in src
                },
            )
        )
        pole = dict(
            name="small-electric-pole",
            position={"x": 0.5, "y": 20.5},
            direction=0,
            unit_number=99,
            electric_network_id=2,
        )
        self.obs["entities"] = [pole]
        self.perimeter.prepare_coverage = Mock(return_value={"ok": True})

        def route(game, builder, obs, source, dest, core, prior):
            es = [entity("pipe", 32.5 + i, source["position"]["y"]) for i in range(1, 5)]
            return _plan(
                es, [], underground_pairs=[], first_neighbor=dict(es[0], fluidbox_index=1), maximum_underground_span=10
            )

        with (
            patch("factorio_agent.advanced_petroleum.unbuilt_external_keepouts", return_value=set()),
            patch("factorio_agent.advanced_petroleum.survey_slots", return_value={"ok": True, "legal": [1]}),
            patch("factorio_agent.advanced_petroleum.plan_typed_fluid_connection", side_effect=route),
            patch(
                "factorio_agent.advanced_petroleum.power_connection",
                return_value=_plan([], [], source_unit_number=99, wire_hops=1),
            ),
            patch("factorio_agent.advanced_petroleum.preflight", return_value={"ok": True, "clear_obstacles": []}),
        ):
            prepared = c.prepare(self.obs)
        return c, src, prepared

    def test_whole_prepare_uses_real_isolation_pair_contract_and_keeps_both_paid_source_gaps(self):
        c, src, prepared = self._whole_prepare()
        self.assertTrue(prepared["ok"], prepared)
        full = {_key(e) for e in prepared["plan"]["entities"]}
        cold = {_key(e) for e in prepared["cold_plan"]["entities"]}
        self.assertEqual(len(full - cold), 2)
        self.assertEqual(full - cold, {_key(r["first_neighbor"]) for r in prepared["routes"].values()})
        self.assertEqual(prepared["sources"], src)
        self.assertFalse(c.path.exists())

    def install(self, phase="feed"):
        core = advanced_refinery_core(self.catalog, 0.5, 0.5)
        bindings = {_key(q): i + 100 for i, q in enumerate(core["entities"])}
        groups, edges = fluid_graph(core)
        plan = _plan(core["entities"], core["ports"], reserved_area=core["reserved_area"])
        state = dict(
            world_id="w",
            catalog_fingerprint=self.catalog.fingerprint,
            phase=phase,
            core=core,
            plan=plan,
            cold_plan=plan,
            bindings=bindings,
            routes={},
            sources={},
            fluid_nodes=[dict(q, fluid=f) for f, g in groups.items() for q in g],
            fluid_edges=edges,
            baseline={str(bindings[_key(q)]): 0 for q in core["machines"]},
            defense_scope_id="scope",
            power_source={},
            construction_authorized=True,
        )
        self.controller.state = state
        self.obs["entities"] = [dict(q, unit_number=bindings[_key(q)]) for q in core["entities"]]
        self.write("catalog.json", catalog_payload())
        self.write(
            "pcb-board.json",
            dict(world_id="w", catalog_fingerprint=self.catalog.fingerprint, blocks={"advanced_petroleum": plan}),
        )
        self.write(
            "fluid-sources.json",
            dict(
                world_id="w",
                catalog_fingerprint=self.catalog.fingerprint,
                sources={"petroleum-gas": {"untouched": True}},
                handoffs={},
            ),
        )
        self.controller._save()
        return state

    def test_a_site_service_supplies_the_refinery_anchor(self):
        from types import SimpleNamespace

        from factorio_agent import block_sites
        from factorio_agent.advanced_petroleum import advanced_refinery_core
        from factorio_agent.site.geometry import Rect

        width, height, dx, dy = block_sites.footprint(lambda x, y: advanced_refinery_core(self.catalog, x, y))
        calls = []

        class Site:
            def allocate_candidates(self, kind, w, h, *, name, count, returns_to_bus, world_id):
                calls.append((kind, w, h, name))
                return [SimpleNamespace(name=name + "#0", rect=Rect(80, -40, 80 + w, -40 + h))]

        self.controller.site = Site()
        c, src, prepared = self._whole_prepare()
        self.assertTrue(prepared["ok"], prepared)
        self.assertEqual(calls, [("production", width, height, "advanced-petroleum")])
        self.assertEqual(prepared["core"]["anchor"], {"x": 80 + dx, "y": -40 + dy})
        self.assertEqual(prepared["site_allocation"], "advanced-petroleum#0")
        area = prepared["core"]["reserved_area"]
        self.assertEqual((area[0][0], area[0][1]), (80, -40))

        class Empty(Site):
            def allocate_candidates(self, *a, **k):
                return []

        self.controller.site = Empty()
        self.assertEqual(self.controller.prepare(self.obs)["reason"], "advanced_no_site_allocation")

    def test_proof_requires_every_old_and_new_machine_to_have_actual_counter_delta(self):
        state = self.install()
        c = self.controller
        c._sources = Mock(return_value={"ok": True})
        c._native = Mock(
            return_value=dict(
                ok=True,
                tick=20,
                machines=[
                    dict(unit_number=state["bindings"][_key(q)], products_finished=int(i != 2))
                    for i, q in enumerate(state["core"]["machines"])
                ],
            )
        )
        self.assertIsNone(c.proof(self.obs))
        self.assertEqual(c.last_proof_failure["reason"], "advanced_all_three_native_product_counters_must_advance")
        c._native.return_value["machines"][-1]["products_finished"] = 1
        proof = c.proof(self.obs)
        self.assertTrue(proof)
        self.assertEqual(set(proof["sources"]), set(OUTPUT_KEYS))
        self.assertNotIn("capacity", proof["sources"]["heavy-oil"])
        self.obs["entities"][0]["unit_number"] += 10000
        self.assertIsNone(c.proof(self.obs))

    def test_cold_before_any_input_configuration_and_no_success_without_proof(self):
        state = self.install("cold")
        c = self.controller
        c._sources = Mock(return_value={"ok": True})
        cold = dict(
            ok=True,
            machines=[
                dict(unit_number=state["bindings"][_key(q)], products_finished=0) for q in state["core"]["machines"]
            ],
        )
        c._native = Mock(side_effect=[cold, {"ok": False, "reason": "advanced_circuit_controls_not_configured"}])
        r = c.step(self.obs)
        self.assertEqual(r["type"], "configure_advanced_petroleum")
        self.assertEqual(state["phase"], "cold")
        self.assertEqual(r["plan_sha256"], _digest(state["plan"]))
        self.assertEqual(c.builder.ensure_plan.call_args.args[1], state["cold_plan"])

    def test_verified_publication_appends_three_outputs_and_preserves_basic_descriptor(self):
        state = self.install()
        c = self.controller
        c._sources = Mock(return_value={"ok": True})
        c.proof = Mock(
            return_value={
                "sources": {
                    f: dict(p, unit_number=state["bindings"][_key(p)], world_id="w")
                    for f, p in state["core"]["outputs"].items()
                }
            }
        )
        self.assertEqual(c.step(self.obs)["status"], "succeeded")
        registry = json.loads(self.controller.sources_path.read_text(encoding="utf-8"))
        self.assertEqual(registry["sources"]["petroleum-gas"], {"untouched": True})
        self.assertTrue(set(OUTPUT_KEYS.values()) <= set(registry["sources"]))

    def test_configuration_exception_or_unexpected_success_receipt_is_unknown(self):
        state = self.install("cold")
        action = dict(
            type="configure_advanced_petroleum",
            expected_world_id="w",
            expected_actor_unit=7,
            expected_catalog_fingerprint=self.catalog.fingerprint,
            plan_sha256=_digest(state["plan"]),
        )
        self.game.query.side_effect = TimeoutError("no receipt")
        self.assertEqual(
            execute_configure_advanced_petroleum(self.game, action)["reason"], "advanced_configuration_outcome_unknown"
        )
        self.game.query.side_effect = None
        self.game.query.return_value = {"ok": True}
        self.assertEqual(
            execute_configure_advanced_petroleum(self.game, action)["reason"], "advanced_configuration_outcome_unknown"
        )
        action["plan_sha256"] = "changed"
        with self.assertRaises(ValueError):
            execute_configure_advanced_petroleum(self.game, action)


@unittest.skipUnless(LuaRuntime, "isolated lupa dependency required")
class AdvancedNativeTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        core = advanced_refinery_core(catalog(), 0.5, 0.5)
        groups, edges = fluid_graph(core)
        self.core = core
        self.args = dict(
            world_id="w",
            core=core,
            nodes=[dict(q, fluid=f) for f, g in groups.items() for q in g],
            edges=edges,
            bindings={_key(q): i + 100 for i, q in enumerate(core["entities"])},
            sources={},
            gaps=[],
            handoffs=[],
            cold=True,
            controls=True,
            configure=True,
            actor_unit=7,
        )

        def table(v):
            if isinstance(v, dict):
                return self.lua.table_from({k: table(z) for k, z in v.items()})
            if isinstance(v, list):
                return self.lua.table_from([table(z) for z in v])
            return v

        self.lua.globals().X = table(self.args)
        self.lua.execute(r"""
f={technologies={["circuit-network"]={researched=true}}};s={};a={valid=true,unit_number=7};d={world_id="w"};game={tick=123}
prototypes={utility_constants={default_pipeline_extent=320}}
defines={wire_connector_id={circuit_red=1,circuit_green=2},wire_origin={player=1},control_behavior={single_fluid_box={exclusive_mode={send_contents=1}}}}
function key(q)return q.name..":"..q.position.x..":"..q.position.y end
E={}
function target(p,name)return E[key{name=name,position=p}]end
local specs={};for _,q in ipairs(X.nodes)do specs[#specs+1]=q end;for _,q in ipairs(X.core.machines)do specs[#specs+1]=q end
for _,q in ipairs(specs)do
 local e={valid=true,name=q.name,position=q.position,direction=q.direction or 0,force=f,surface=s,quality={name="normal"},energy=100,
  unit_number=X.bindings[key(q)],products_finished=0,contents={},peers={},connectors={},physical={},runtime={}}
 E[key(q)]=e
 if q.recipe then
  for fluid,index in pairs(X.core.machine_boxes[key(q)])do e.physical[index]=fluid end
 else e.physical[1]=q.fluid end
 for index in pairs(e.physical)do e.runtime[index]=index end
 if q.name=="oil-refinery"then e.runtime={[1]=2,[2]=1,[3]=5,[4]=3,[5]=4}end
 e.get_recipe=function()return q.recipe and{name=q.recipe}or nil end
 e.is_connected_to_electric_network=function()return true end
 e.get_fluid_contents=function()return e.contents end
 e.get_fluid_box_prototype=function(i)for physical,runtime in pairs(e.runtime)do if runtime==i then return {index=physical}end end;error("inactive storage")end
 e.get_fluid=function(i)for physical,runtime in pairs(e.runtime)do if runtime==i then local fluid=e.physical[physical];return{name=fluid,amount=e.contents[fluid]or 0}end end end
 e.has_fluid_segment=function()return true end
 e.get_fluid_segment_extent_bounding_box=function()return{left_top={x=-25,y=-25},right_bottom={x=25,y=25}}end
 e.get_fluid_box_neighbours=function(i)return e.peers[i]or{}end
 e.get_control_behavior=function()return e.cb end
 e.get_or_create_control_behavior=function()e.cb=e.cb or{};return e.cb end
 e.get_wire_connector=function(id,create)
  if not e.connectors[id]and create then
   local c={owner=e,wire_connector_id=id,connections={}}
   c.can_wire_reach=function(other)return true end
   c.is_connected_to=function(other)for _,p in ipairs(c.connections)do if p.target==other then return true end end;return false end
   c.connect_to=function(other)table.insert(c.connections,{target=other});table.insert(other.connections,{target=c});return true end
   e.connectors[id]=c
  end
  return e.connectors[id]
 end
end
for _,q in ipairs(X.core.entities)do if not E[key(q)]then
 local e={valid=true,name=q.name,position=q.position,direction=q.direction or 0,unit_number=X.bindings[key(q)],force=f,surface=s,connectors={}}
 E[key(q)]=e
 e.get_wire_connector=function(id,create)
  if not e.connectors[id]and create then local c={owner=e,wire_connector_id=id,connections={}}
   c.can_wire_reach=function()return true end
   c.is_connected_to=function(other)for _,p in ipairs(c.connections)do if p.target==other then return true end end;return false end
   c.connect_to=function(other)table.insert(c.connections,{target=other});table.insert(other.connections,{target=c});return true end;e.connectors[id]=c end
  return e.connectors[id]
 end
end end
for _,q in ipairs(X.edges)do local a=E[key(q.a)];local b=E[key(q.b)];local ai=a.runtime[q.a.fluidbox_index];local bi=b.runtime[q.b.fluidbox_index]
 a.peers[ai]=a.peers[ai]or{};b.peers[bi]=b.peers[bi]or{}
 table.insert(a.peers[ai],{entity=b,index=bi});table.insert(b.peers[bi],{entity=a,index=ai})
end
""")

    def run_lua(self):
        return self.lua.execute(ADVANCED_INSPECT_LUA.replace("helpers.json_to_table(PAYLOAD)", "X"))

    def test_actual_code_configures_two_independent_tank_reserve_circuits_before_any_fluids(self):
        r = self.run_lua()
        self.assertTrue(r["ok"], r["reason"])
        self.assertTrue(r["cold"])
        self.assertTrue(r["controls_checked"])
        self.lua.execute("X.configure=false")
        self.assertTrue(self.run_lua()["ok"])
        self.lua.execute(
            "for _,q in ipairs(X.core.controls)do E[key(q.machine)].cb.circuit_condition.constant=0;break end"
        )
        self.assertEqual(self.run_lua()["reason"], "advanced_circuit_controls_not_configured")

    def test_real_output_and_permuted_runtime_boxes_prove_without_any_fluid_insert(self):
        self.assertTrue(self.run_lua()["ok"])
        self.lua.execute(
            "X.configure=false;X.cold=false;for _,e in pairs(E)do if e.physical then for _,fluid in pairs(e.physical)do e.contents[fluid]=10 end;e.products_finished=2 end end"
        )
        r = self.run_lua()
        self.assertTrue(r["ok"], r["reason"])
        self.assertEqual(r["stock"]["heavy-oil"], 10)
        self.lua.execute('E[key(X.core.machines[1])].get_fluid=function(i)return{name="water",amount=1}end')
        self.assertEqual(self.run_lua()["reason"], "advanced_wrong_runtime_fluid_box")

    def test_cold_network_contamination_and_missing_reciprocal_peer_fail_before_configuration(self):
        self.lua.execute('E[key(X.nodes[1])].contents["sulfuric-acid"]=1')
        self.assertEqual(self.run_lua()["reason"], "advanced_native_fluid_contamination")
        self.lua.execute("E[key(X.nodes[1])].contents={};local e=E[key(X.edges[1].a)];e.peers={}")
        self.assertEqual(self.run_lua()["reason"], "advanced_reciprocal_typed_edge_missing")
        self.lua.execute("for _,q in ipairs(X.core.controls)do assert(E[key(q.tank)].cb==nil)end")

    def test_actor_changed_before_configuration_has_no_ui_effect(self):
        self.lua.execute("a.unit_number=8")
        self.assertEqual(self.run_lua()["reason"], "advanced_native_actor_changed")
        self.lua.execute("for _,q in ipairs(X.core.controls)do assert(E[key(q.tank)].cb==nil)end")


if __name__ == "__main__":
    unittest.main()
