import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.board import entity
from factorio_agent.chemical_production import chemical_plan_isolation
from factorio_agent.fluid_products import (
    FluidProducts,
    fluid_product_core,
    fluid_product_rates,
    fluid_product_edges,
    FLUID_PRODUCT_PROOF_LUA,
    SOURCE_LUA,
    IRON_SOURCE_LUA,
    _key,
    _payload,
)
import test_v2_chemical_production as chemistry

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


def catalog():
    value = chemistry.catalog()
    value.recipes["sulfuric-acid"] = dict(
        category="chemistry",
        energy=1,
        ingredients=[
            dict(type="item", name="iron-plate", amount=1),
            dict(type="item", name="sulfur", amount=5),
            dict(type="fluid", name="water", amount=100),
        ],
        products=[dict(type="fluid", name="sulfuric-acid", amount=50)],
    )
    value.recipes["lubricant"] = dict(
        category="chemistry",
        energy=1,
        ingredients=[dict(type="fluid", name="heavy-oil", amount=10)],
        products=[dict(type="fluid", name="lubricant", amount=10)],
    )
    return value


class FluidProductGeometryTests(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog()
        self.builder = FactoryBuilder.__new__(FactoryBuilder)

    def test_two_plant_acid_rate_accounts_for_sulfur_limit_and_both_water_consumers(self):
        acid = fluid_product_rates(self.catalog, "sulfuric-acid")
        self.assertEqual(acid["fluid_per_minute"], 1200)
        self.assertEqual(acid["machine_alone_per_minute"], 3000)
        self.assertEqual(acid["inputs_per_minute"], {"iron-plate": 24, "water": 4200, "petroleum-gas": 1800})
        lube = fluid_product_rates(self.catalog, "lubricant")
        self.assertEqual(lube["fluid_per_minute"], 600)
        self.assertEqual(lube["inputs_per_minute"], {"heavy-oil": 600})
        self.catalog.recipes["lubricant"]["products"][0]["probability"] = 0.5
        with self.assertRaises(ValueError):
            fluid_product_rates(self.catalog, "lubricant")

    def test_whole_paid_core_access_and_future_plant_never_overlap(self):
        for product, count in (("sulfuric-acid", 2), ("lubricant", 1)):
            core = fluid_product_core(self.catalog, product, 0.5, 0.5)
            boxes = [
                self.builder._occupied_by_plan([q])
                for q in core["entities"] + core["output_access"] + core["expansion_keepouts"]
            ]
            self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))
            self.assertEqual(core["required_items"]["chemical-plant"], count)
            self.assertEqual(core["output_port"]["position"], {"x": 14.5, "y": 3.5})
            self.assertEqual(core["output_port"]["facing"], 4)
            self.assertIn("EAST", core["growth_direction"])
        with self.assertRaises(ValueError):
            fluid_product_core(self.catalog, "lubricant", 0, 0.5)

    def test_water_connects_both_plants_gas_only_precursor_and_output_only_acid(self):
        core = fluid_product_core(self.catalog, "sulfuric-acid", 0.5, 0.5)
        networks = {}
        for fluid, nodes in core["fluid_groups"].items():
            edges = fluid_product_edges(self.catalog, core, fluid, nodes, [])
            machines = [e["a"] for e in edges if e["a"]["name"] == "chemical-plant"]
            self.assertEqual(len(machines), 2 if fluid == "water" else 1)
            networks[fluid] = dict(
                nodes=nodes, source={"unit_number": 0}, route=dict(underground_pairs=[], maximum_underground_span=10)
            )
        game = SimpleNamespace(query=Mock(return_value={"ok": True}))
        self.assertTrue(chemical_plan_isolation(game, networks, core)["ok"])
        networks["petroleum-gas"]["nodes"].append(entity("pipe", -1.5, -3.5))
        self.assertFalse(chemical_plan_isolation(game, networks, core)["ok"])
        sulfur = next(q for q in core["arms"] if q["feed"] == "sulfur")
        self.assertEqual(
            (sulfur["direction"], sulfur["from_machine"], sulfur["to_machine"]),
            (4, _key(core["machines"][1]), _key(core["machine"])),
        )

    def test_native_physical_geometry_changes_fail_closed(self):
        self.catalog.entities["chemical-plant"]["fluidbox_prototypes"][2]["pipe_connections"][0]["positions"][0][
            "x"
        ] = 0
        with self.assertRaises(ValueError):
            fluid_product_core(self.catalog, "lubricant", 0.5, 0.5)


class FluidProductControllerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.catalog = catalog()
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root), expected_world="w", query=Mock())
        self.builder = SimpleNamespace(
            ensure_plan=Mock(return_value={"status": "succeeded"}),
            _occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,
        )
        self.perimeter = SimpleNamespace(
            prepare_coverage=Mock(return_value={"ok": True}),
            ensure_coverage=Mock(return_value=None),
            coverage_proof=Mock(return_value={"ok": True}),
        )
        self.controller = FluidProducts(self.game, self.builder, self.catalog, self.perimeter)
        self.obs = dict(
            ok=True,
            world_id="w",
            tick=10,
            entities=[],
            inventory={},
            technologies={"sulfur-processing": True, "lubricant": True},
            enabled_recipes={
                name: True
                for name in (
                    "sulfuric-acid",
                    "sulfur",
                    "lubricant",
                    "chemical-plant",
                    "pipe",
                    "pipe-to-ground",
                    "small-electric-pole",
                    "fast-inserter",
                    "transport-belt",
                )
            },
        )
        self.source = dict(
            kind="fluid",
            item="heavy-oil",
            name="pipe",
            position={"x": -30.5, "y": -5.5},
            facing=4,
            direction="output",
            fluidbox_index=1,
            world_id="w",
            unit_number=99,
        )
        self.registry = dict(world_id="w", catalog_fingerprint="c", sources={"heavy-oil": self.source}, handoffs={})
        self.write("fluid-sources.json", self.registry)
        self.write("pcb-board.json", dict(world_id="w", catalog_fingerprint="c", blocks={}))
        self.game.query.return_value = dict(
            ok=True,
            snapshots={
                "heavy-oil": dict(
                    amount=100, extent={"left_top": {"x": -31, "y": -6}, "right_bottom": {"x": -30, "y": -5}}, limit=320
                )
            },
        )

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value), encoding="utf-8")

    def prepare(self):
        def route(game, builder, obs, source, destination, core, *args, **kw):
            from factorio_agent.transport import VECTORS

            dx, dy = VECTORS[source["facing"]]
            p = source["position"]
            first = entity("pipe", p["x"] + dx, p["y"] + dy)
            return dict(
                ok=True,
                entities=[first, entity("pipe", **destination["position"])],
                first_neighbor=dict(first, fluidbox_index=1),
                underground_pairs=[],
                maximum_underground_span=10,
                independent_clear_cells=[],
            )

        with (
            patch("factorio_agent.fluid_products.survey_slots", return_value={"ok": True, "legal": [1]}),
            patch("factorio_agent.fluid_products.plan_typed_fluid_connection", side_effect=route),
            patch("factorio_agent.fluid_products.connect_power_groups", return_value={"ok": True, "entities": []}),
            patch("factorio_agent.fluid_products.preflight", return_value={"ok": True, "clear_obstacles": []}),
            patch("factorio_agent.fluid_products.chemical_plan_isolation", return_value={"ok": True}),
        ):
            return self.controller.prepare(self.obs, {"product": "lubricant"})

    def test_invalid_parameters_and_native_unlocks_fail_before_any_query_or_new_state(self):
        for parameters in ({}, {"product": []}, {"product": "sulfur"}, {"product": "lubricant", "amount": 100}):
            with self.assertRaises(ValueError):
                self.controller.prepare(self.obs, parameters)
        self.obs["technologies"]["lubricant"] = False
        self.assertEqual(
            self.controller.prepare(self.obs, {"product": "lubricant"})["reason"],
            "native_fluid_product_technology_and_recipes_required",
        )
        self.game.query.assert_not_called()
        self.assertFalse(self.controller.path.exists())
        self.builder.ensure_plan.assert_not_called()

    def test_real_empty_or_consumed_source_cannot_be_silently_retargeted(self):
        self.game.query.return_value["snapshots"]["heavy-oil"]["amount"] = 0
        self.assertFalse(self.controller._sources(self.obs, "lubricant")["ok"])
        self.registry["handoffs"]["other"] = {"heavy-oil": {"source": self.source, "phase": "bound"}}
        self.write("fluid-sources.json", self.registry)
        self.assertEqual(
            self.controller._sources(self.obs, "lubricant")["reason"],
            "fluid_product_source_needs_paid_independent_branch",
        )
        self.registry["sources"].clear()
        self.write("fluid-sources.json", self.registry)
        self.assertEqual(self.controller._sources(self.obs, "lubricant")["registry_key"], "lubricant-heavy-oil")

    def test_whole_prepare_is_readonly_and_preserves_cardinal_heavy_west_source(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.source["facing"] = 12
        self.write("fluid-sources.json", self.registry)
        before["fluid-sources.json"] = (self.root / "fluid-sources.json").read_bytes()
        ready = self.prepare()
        self.assertTrue(ready["ok"], ready)
        self.assertLess(ready["core"]["machine"]["position"]["x"], self.source["position"]["x"])
        self.assertEqual(ready["sources"]["heavy-oil"], self.source)
        self.assertFalse(self.controller.path.exists())
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_a_site_service_supplies_the_cell_anchor(self):
        from types import SimpleNamespace

        from factorio_agent import block_sites
        from factorio_agent.fluid_products import fluid_product_core
        from factorio_agent.site.geometry import Rect

        _, _, dx, dy = block_sites.footprint(lambda x, y: fluid_product_core(self.catalog, "lubricant", x, y))
        sx, sy = self.source["position"]["x"], self.source["position"]["y"]
        x0, y0 = int(sx + 10 - dx), int(sy + 8 - dy)  # a block near the source, as the master plan would give

        class Site:
            def allocate_candidates(self, kind, w, h, *, name, count, returns_to_bus, world_id):
                return [SimpleNamespace(name=name + "#0", rect=Rect(x0, y0, x0 + w, y0 + h))]

        self.controller.site = Site()
        ready = self.prepare()
        self.assertTrue(ready["ok"], ready)
        self.assertEqual(ready["core"]["anchor"], {"x": x0 + dx, "y": y0 + dy})
        self.assertEqual(ready["site_allocation"], "fluid_product_lubricant#0")
        self.controller.site = SimpleNamespace(allocate_candidates=lambda *a, **k: [])
        self.assertEqual(self.prepare()["reason"], "fluid_product_no_site_allocation")

    def acid_on_planned_bus(self):
        """Acid sources in the registry, a built planned bus and a site block beside it (side right)."""
        import test_v2_planned_tap as tapfix

        from factorio_agent import block_sites
        from factorio_agent.site.geometry import Rect

        harness = tapfix.Harness(self.root)
        board = harness.board()
        board.update(catalog_fingerprint="c")
        self.write("pcb-board.json", board)
        sources = {}
        for n, (fluid, key) in enumerate((("water", "acid-water"), ("petroleum-gas", "acid-gas"))):
            sources[key] = dict(self.source, item=fluid, position={"x": 80.5, "y": -40.5 - 4 * n}, unit_number=90 + n)
        self.registry["sources"] = sources
        self.write("fluid-sources.json", self.registry)
        snap = dict(
            amount=100, extent={"left_top": {"x": 80, "y": -50}, "right_bottom": {"x": 81, "y": -40}}, limit=320
        )
        self.game.query.return_value = dict(ok=True, snapshots={"water": snap, "petroleum-gas": dict(snap)})
        _, _, dx, dy = block_sites.footprint(lambda x, y: fluid_product_core(self.catalog, "sulfuric-acid", x, y))
        x0, y0 = 90, -60

        class Site:
            def allocate_candidates(self, kind, w, h, *, name, count, returns_to_bus, world_id):
                rect = Rect(x0, y0, x0 + w, y0 + h)
                return [SimpleNamespace(name=name + "#0", rect=rect, side="right", along0=20, along1=40)]

        self.controller.site = Site()
        return harness

    def prepare_acid(self):
        def belt(game, builder, obs, source, destination, reserved, **kw):
            ends = [entity("transport-belt", **p["position"], direction=p["facing"]) for p in (source, destination)]
            return {"ok": True, "entities": ends, "underground_pairs": [], "clear_obstacles": []}

        def route(game, builder, obs, source, destination, core, *args, **kw):
            first = entity("pipe", source["position"]["x"] + 1, source["position"]["y"])
            return dict(
                ok=True,
                entities=[first, entity("pipe", **destination["position"])],
                first_neighbor=dict(first, fluidbox_index=1),
                underground_pairs=[],
                maximum_underground_span=10,
                independent_clear_cells=[],
            )

        with (
            patch("factorio_agent.planned_tap.max_span", return_value=5),
            patch("factorio_agent.transport.plan_connection", side_effect=belt),
            patch("factorio_agent.fluid_products.survey_slots", return_value={"ok": True, "legal": [1]}),
            patch("factorio_agent.fluid_products.plan_typed_fluid_connection", side_effect=route),
            patch("factorio_agent.fluid_products.pipeline_extent", return_value={"ok": True}),
            patch("factorio_agent.fluid_products.connect_power_groups", return_value={"ok": True, "entities": []}),
            patch("factorio_agent.fluid_products.preflight", return_value={"ok": True, "clear_obstacles": []}),
            patch("factorio_agent.fluid_products.chemical_plan_isolation", return_value={"ok": True}),
        ):
            return self.controller.prepare(self.obs, {"product": "sulfuric-acid"})

    def test_acid_iron_comes_from_a_planned_bus_tap_on_a_planned_world(self):
        from factorio_agent import planned_bus as pb
        from factorio_agent.transport import xy

        harness = self.acid_on_planned_bus()
        self.obs["entities"] = harness.entities  # the observed trunk belts the tap replaces
        self.controller._iron_source = Mock(side_effect=AssertionError("development-map bus read"))
        ready = self.prepare_acid()
        self.assertTrue(ready["ok"], ready)
        self.assertEqual(ready["iron_supply"], "planned_bus_tap")
        self.assertNotIn("iron-plate", ready["sources"])
        (tap,) = ready["taps"]
        self.assertEqual(
            (tap["item"], tap["owner"], tap["side"]), ("iron-plate", "fluid_product_sulfuric-acid", "right")
        )
        self.assertEqual(tap["demand_per_minute"], 24)
        tapped = {(e["name"], *xy(e)) for e in tap["entities"]}
        self.assertFalse(tapped & {(e["name"], *xy(e)) for e in ready["plan"]["entities"]})
        # The step publishes the tap with the bus and cuts it over before the cell is built.
        self.controller.state = dict(world_id="w", catalog_fingerprint="c", products={"sulfuric-acid": ready})
        cut = dict(type="mine", name="transport-belt")
        with (
            patch("factorio_agent.planned_tap.cutover", return_value=cut) as cutover,
            patch("factorio_agent.fluid_products.surface_pipe_proof", return_value={"ok": True}),
            patch("factorio_agent.fluid_products.fluid_crossing_proof", return_value={"ok": True}),
            patch("factorio_agent.fluid_products._handoff", return_value={"ok": True}),
        ):
            self.assertEqual(self.controller.step(self.obs, {"product": "sulfuric-acid"}), cut)
        cutover.assert_called_once()
        self.builder.ensure_plan.assert_not_called()
        state = json.loads((harness.root / pb.STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual([t["name"] for t in state["taps"]], ["fluid_product_sulfuric-acid:iron-plate"])
        self.assertIsNone(self.controller.proof(self.obs, {"product": "sulfuric-acid"}))

    def test_a_planned_bus_tap_that_fits_nowhere_returns_tap_needs(self):
        from factorio_agent import plan_needs

        self.acid_on_planned_bus()
        self.obs["entities"] = []  # no observed trunk belts: every tap row is rejected
        failed = self.prepare_acid()
        self.assertFalse(failed["ok"])
        self.assertEqual(plan_needs.from_result("fluid_product", failed)["kind"], "tap")
        self.assertEqual(failed["plan_needs"]["item"], "iron-plate")

    def test_tap_success_is_a_prerequisite_and_never_premature_product_success(self):
        params = dict(
            name="fluid-acid-iron", item="iron-plate", splitter_y=-10.5, priority="surplus", demand_per_minute=24
        )
        self.controller.prepare = Mock(
            return_value=dict(ok=False, reason="acid_paid_iron_tap_required", tap_parameters=params)
        )
        self.controller.tap.step = Mock(return_value=dict(status="succeeded"))
        result = self.controller.step(self.obs, {"product": "sulfuric-acid"})
        self.assertEqual(result["status"], "running")
        self.builder.ensure_plan.assert_not_called()
        self.assertEqual(self.controller.state["acid_tap_parameters"], params)

    def test_defense_gate_precedes_paid_production_and_known_unit_replacement_blocks(self):
        ready = self.prepare()
        self.controller.state = dict(world_id="w", catalog_fingerprint="c", products={"lubricant": ready})
        self.perimeter.ensure_coverage.return_value = dict(type="craft", recipe="gun-turret", count=1)
        result = self.controller.step(self.obs, {"product": "lubricant"})
        self.assertEqual(result["recipe"], "gun-turret")
        self.builder.ensure_plan.assert_not_called()
        spec = ready["plan"]["entities"][0]
        ready["bindings"][_key(spec)] = 10
        self.obs["entities"] = [dict(spec, unit_number=11)]
        self.assertEqual(
            self.controller.step(self.obs, {"product": "lubricant"})["reason"],
            "fluid_product_owned_asset_lost_or_replaced",
        )

    def test_native_recipe_is_configured_after_paid_build_without_adopting_wrong_recipe(self):
        ready = self.prepare()
        machine = ready["core"]["machine"]
        live = dict(machine, recipe=None, unit_number=12)
        self.obs["entities"] = [live]
        self.assertIsNone(self.controller._bind(self.obs, ready))
        live["recipe"] = "sulfur"
        self.assertEqual(self.controller._bind(self.obs, ready)["reason"], "fluid_product_owned_configuration_changed")


@unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
class NativeFluidProductProofTests(unittest.TestCase):
    def setUp(self):
        self.configure("sulfuric-acid")

    def configure(self, product):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.catalog = catalog()
        self.core = fluid_product_core(self.catalog, product, 0.5, 0.5)

        def table(value):
            if isinstance(value, dict):
                return self.lua.table_from({k: table(v) for k, v in value.items()})
            if isinstance(value, list):
                return self.lua.table_from([table(v) for v in value])
            return value

        self.table = table
        bindings = {_key(q): i + 10 for i, q in enumerate(self.core["entities"])}
        groups = {
            name: [dict(q, unit_number=bindings[_key(q)]) for q in nodes]
            for name, nodes in self.core["fluid_groups"].items()
        }
        edges = [
            edge
            for fluid, nodes in self.core["fluid_groups"].items()
            for edge in fluid_product_edges(self.catalog, self.core, fluid, nodes, [])
        ]
        self.args = dict(
            product=product,
            groups=groups,
            edges=edges,
            source_units=[],
            prototype_box_count=4,
            downstream=[],
            machines=[
                dict(
                    q,
                    unit_number=bindings[_key(q)],
                    physical_boxes=self.core["physical_boxes"][_key(q)],
                    required_boxes=self.core["required_boxes"][_key(q)],
                )
                for q in self.core["machines"]
            ],
            arms=[dict(q, unit_number=bindings[_key(q)]) for q in self.core["arms"]],
            belts=[
                dict(q, unit_number=bindings[_key(q)]) for q in self.core["entities"] if q["name"] == "transport-belt"
            ],
            output=dict(self.core["output_port"], unit_number=bindings[_key(self.core["output_port"])]),
        )
        self.lua.globals().helpers = self.lua.table_from({"json_to_table": lambda raw: table(json.loads(raw))})
        self.lua.globals().specs = table([dict(q, unit_number=bindings[_key(q)]) for q in self.core["entities"]])
        self.lua.globals().args = table(self.args)
        self.lua.execute(
            """f={};s={};game={tick=30};prototypes={utility_constants={default_pipeline_extent=320}};powered=true;dirty=false;foreign=false;reverse_missing=false;wrong_recipe=false;dry=false;merged=false;cross_merge=false;wrong_pickup=false;wrong_belt=false;oversize=false
local function key(q)return q.name..":"..q.position.x..":"..q.position.y end
entities={};for _,q in ipairs(specs)do entities[key(q)]=q end
local function runtime(q,index)if q.name=="chemical-plant"and q.recipe~="sulfur"then if index<=2 then return 1 else return 2 end end;return index end
for _,q in ipairs(specs)do
 q.valid=true;q.force=f;q.surface=s;q.energy=100;q.products_finished=2
 q.is_connected_to_electric_network=function()return powered end
 q.get_recipe=function()return{name=wrong_recipe and"wrong"or q.recipe}end
 q.get_fluid_contents=function()if dry then return{}end;for name,nodes in pairs(args.groups)do for _,n in ipairs(nodes)do if key(q)==key(n)then return{[dirty and"wrong"or name]=10}end end end;return{}end
 q.has_fluid_segment=function()return true end
 q.get_fluid_segment_extent_bounding_box=function()return{left_top={x=0,y=0},right_bottom={x=oversize and321 or20,y=20}}end
 q.get_fluid_box_prototype=function(index)
  if index>2 then error("compressed runtime bounds")end
  if q.recipe=="sulfur"then return{index=index}end
  if index==1 then if cross_merge then return{{index=1},{index=3}}end;if merged then return{{index=1},{index=2}}end;return{index=1}end
  if merged then return{{index=3},{index=4}}end;return{index=3}end
 q.get_fluid=function(index)
  for _,m in ipairs(args.machines)do if key(q)==key(m)then for physical,name in pairs(m.physical_boxes)do if runtime(q,tonumber(physical))==index then return{name=dirty and"wrong"or name,amount=10}end end end end
  return nil end
 q.get_fluid_box_neighbours=function(index)local rows={}
  for _,e in ipairs(args.edges)do
   if key(e.a)==key(q)and runtime(e.a,e.a.fluidbox_index)==index then rows[#rows+1]={entity=entities[key(e.b)],index=runtime(e.b,e.b.fluidbox_index)}end
   if not reverse_missing and key(e.b)==key(q)and runtime(e.b,e.b.fluidbox_index)==index then rows[#rows+1]={entity=entities[key(e.a)],index=runtime(e.a,e.a.fluidbox_index)}end
  end
  if foreign then rows[#rows+1]={entity={unit_number=999},index=1}end;return rows end
 q.get_transport_line=function()return{get_contents=function()return{{name=wrong_belt and"copper-plate"or"iron-plate",count=2}}end}end
 if q.name=="fast-inserter"then local dx=q.direction==12 and -1 or1;q.pickup_position={x=q.position.x+dx,y=q.position.y};q.drop_position={x=q.position.x-dx,y=q.position.y};if wrong_pickup then q.pickup_position.x=999 end end
end
function target(p,name)return entities[name..":"..p.x..":"..p.y]end""".replace("and321", "and 321")
            .replace("or20", "or 20")
            .replace("or1", "or 1")
        )

    def run_proof(self):
        return dict(
            self.lua.execute(
                "return(function() "
                + " ".join(FLUID_PRODUCT_PROOF_LUA.replace("PAYLOAD", _payload(self.args)).splitlines())
                + " end)()"
            ).items()
        )

    def test_acid_and_lubricant_positive_native_flow_and_compressed_slots(self):
        self.assertTrue(self.run_proof()["ok"], self.run_proof())
        self.lua.execute("merged=true")
        self.assertTrue(self.run_proof()["ok"])
        self.configure("lubricant")
        self.assertTrue(self.run_proof()["ok"])
        self.lua.execute("merged=true")
        self.assertTrue(self.run_proof()["ok"])

    def test_native_power_recipe_peer_contamination_and_extent_fail_closed(self):
        for change, restore in (
            ("powered=false", "powered=true"),
            ("wrong_recipe=true", "wrong_recipe=false"),
            ("dirty=true", "dirty=false"),
            ("foreign=true", "foreign=false"),
            ("reverse_missing=true", "reverse_missing=false"),
            ("dry=true", "dry=false"),
            ("oversize=true", "oversize=false"),
            ("wrong_belt=true", "wrong_belt=false"),
        ):
            self.lua.execute(change)
            self.assertFalse(self.run_proof()["ok"], change)
            self.lua.execute(restore)
        self.lua.execute("cross_merge=true")
        self.assertEqual(self.run_proof()["reason"], "fluid_product_different_fluids_merged")

    def test_changed_unit_or_native_direct_inserter_vector_never_proves_product(self):
        machine = self.lua.globals().entities[_key(self.core["machine"])]
        machine.unit_number = 999
        self.assertFalse(self.run_proof()["ok"])
        machine.unit_number = self.args["machines"][0]["unit_number"]
        machine.products_finished = 0
        self.assertEqual(self.run_proof()["reason"], "fluid_product_actual_recipe_products_required")
        machine.products_finished = 2
        arm = self.lua.globals().entities[_key(self.core["arms"][1])]
        arm.pickup_position.x = 999
        self.assertEqual(self.run_proof()["reason"], "fluid_product_precursor_direct_feed_missing")

    def test_all_native_scripts_are_flattenable_and_readonly(self):
        for script in (FLUID_PRODUCT_PROOF_LUA, SOURCE_LUA, IRON_SOURCE_LUA):
            self.assertNotIn("--", script)
            for forbidden in (
                "create_entity",
                "insert_fluid",
                "set_fluid",
                "remove_item",
                ".researched=",
                "begin_crafting",
            ):
                self.assertNotIn(forbidden, script)


if __name__ == "__main__":
    unittest.main()
