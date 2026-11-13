from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.core.world_catalog import WorldCatalog
from factorio_agent.registered_asset_repair import (
    RegisteredAssetRepair,
    execute_repair_registered_assets,
    FILE,
    _key,
    _receipt_valid,
    ASSET_GUARD_LUA,
    BUILD_ASSETS_LUA,
)

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


@unittest.skipUnless(LuaRuntime, "optional executable native Lua fixture")
class RegisteredAssetRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lua = LuaRuntime(unpack_returned_tuples=True)

        def table(v):
            if isinstance(v, dict):
                return self.lua.table_from({k: table(q) for k, q in v.items()})
            if isinstance(v, list):
                return self.lua.table_from([table(q) for q in v])
            return v

        def decode(v):
            if not hasattr(v, "items"):
                return v
            keys = list(v.keys())
            if keys and all(type(k) is int for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
                return [decode(v[i]) for i in range(1, len(keys) + 1)]
            return {k: decode(q) for k, q in v.items()}

        self.lua.globals().helpers = self.lua.table_from({"json_to_table": lambda raw: table(json.loads(raw))})
        self.lua.execute(r"""
f={index=1};s={};d={world_id="w"};game={tick=20};world={};grounds={};next_unit=200
defines={wire_connector_id={pole_copper=1}}
prototypes={entity={},utility_constants={default_pipeline_extent=320}}
main={valid=true}
for i,name in ipairs{"pipe","transport-belt","small-electric-pole"}do
 local data={valid_for_read=true,name=name,count=10,quality={name="normal"},prototype={type="item"},health=1,spoil_tick=0}
 main[i]=setmetatable({},{__index=data,__newindex=function(q,k,v)data[k]=v;if k=="count"and v==0 then data.valid_for_read=false end end})
end
a={valid=true,force=f,surface=s,unit_number=11,get_main_inventory=function()return main end}
function key(p)return p.x..":"..p.y end
function basic(name,unit,x,y)
 local e={valid=true,name=name,type=name,unit_number=unit,position={x=x,y=y},force=f,surface=s,direction=0,quality={name="normal"},health=150,max_health=150,
  prototype={fluidbox_prototypes={}},electric_network_id=7,energy=100,products_finished=10}
 e.is_connected_to_electric_network=function()return true end
 world[key(e.position)]=e;return e
end
function pipe(unit,x,y)
 local e=basic("pipe",unit,x,y);e.prototype.fluidbox_prototypes={{index=1}}
 e.get_fluid_contents=function()return {["petroleum-gas"]=100}end
 e.get_fluid_segment_extent_bounding_box=function()return {left_top={x=-1,y=0},right_bottom={x=5,y=1}}end
 e.get_fluid_box_neighbours=function()
  local out={};for _,delta in ipairs{{1,0},{-1,0},{0,1},{0,-1}}do local other=world[key{x=x+delta[1],y=y+delta[2]}]
   if other and other.valid and other.name=="pipe"then out[#out+1]={entity=other,index=1}end
  end
  if x==2.5 and y==.5 then out[#out+1]={entity=world[key{x=4.5,y=.5}],index=1}end
  return out
 end;return e
end
function belt(unit,x,y,created)
 local e=basic("transport-belt",unit,x,y);e.direction=4;e.items=created and{{},{}}or{{{name="plastic-bar",quality="normal",count=1}}, {}}
 e.get_transport_line=function(lane)return {get_contents=function()return e.items[lane]end}end
 setmetatable(e,{__index=function(q,k)if k=="belt_neighbours"then
  local before=world[key{x=x-1,y=y}];local after=world[key{x=x+1,y=y}]
  return {inputs=before and before.name=="transport-belt"and{before}or{},outputs=after and after.name=="transport-belt"and{after}or{}}
 end end});return e
end
function pole(unit,x,y)
 local e=basic("small-electric-pole",unit,x,y)
 e.get_wire_connector=function()
  local out={};for _,other in pairs(world)do if other.valid and other.name=="small-electric-pole"and other~=e
   and math.abs(other.position.x-x)<=7.5 and math.abs(other.position.y-y)<=7.5 then out[#out+1]={target={owner=other}}end end
  return {connections=out}
 end;return e
end
pipe(99,-.5,.5);pipe(100,.5,.5);pipe(102,2.5,.5)
machine=basic("chemical-plant",103,4.5,.5)
machine.get_recipe=function()return {name="plastic-bar"}end
machine.get_fluid_box_prototype=function(i)if i~=1 then error("out of bounds")end;return {index=1}end
machine.get_fluid=function()return {name="petroleum-gas",amount=100}end
machine.get_fluid_contents=function()return {["petroleum-gas"]=100}end
machine.get_fluid_box_neighbours=function()local other=world[key{x=2.5,y=.5}];return other and{{entity=other,index=1}}or{}end
belt(110,.5,4.5);belt(112,2.5,4.5);pole(120,.5,7.5);pole(122,-4.5,7.5)
arm=basic("fast-inserter",130,3.5,4.5)
remnant={valid=true,type="corpse",name="belt-remnants",position={x=1.5,y=4.5}};grounds[1]=remnant
grounds[2]={valid=true,type="item-entity",name="item-on-ground",position={x=1.5,y=4.5},stack={valid_for_read=true,name="plastic-bar",count=4,
 quality={name="normal"},prototype={type="item"},health=1,spoil_tick=0}}
function target(p,name)local e=world[key(p)];return e and e.valid and e.name==name and e or nil end
s.can_place_entity=function(spec)return not world[key(spec.position)]end
s.find_entities_filtered=function(spec)
 if spec.type=="resource"then return ore and{{name="iron-ore"}}or{}end
 local out={};for _,collection in ipairs{world,grounds}do for _,e in pairs(collection)do local p=e.position
  local kind=not spec.type or spec.type==e.type
  if type(spec.type)=="table"then for _,t in pairs(spec.type)do if t==e.type then kind=true end end end
  if e.valid and kind and p.x>=spec.area[1][1]and p.x<=spec.area[2][1]and p.y>=spec.area[1][2]and p.y<=spec.area[2][2]then out[#out+1]=e end
 end end;return out
end
s.create_entity=function(spec)
 local marker=nil;for _,m in pairs(d.registered_asset_repairs)do marker=m end
 assert(marker.before.main_items.pipe==10 and marker.before.goods.combined["plastic-bar"]==6 and marker.before.wires and marker.before.fluids)
 if fail_create then return nil end
 next_unit=next_unit+1;local e
 if spec.name=="pipe"then e=pipe(next_unit,spec.position.x,spec.position.y)
 elseif spec.name=="small-electric-pole"then e=pole(next_unit,spec.position.x,spec.position.y);if delay_energy then machine.energy=0;arm.energy=0 end
 else e=belt(next_unit,spec.position.x,spec.position.y,true)
  if pickup then for _,g in ipairs(grounds)do if g.valid and g.type=="item-entity"then e.items[1][#e.items[1]+1]={name=g.stack.name,count=g.stack.count,quality="normal"};g.valid=false end end end
 end
 if not spec.preserve_ghosts_and_corpses or lose_corpse then remnant.valid=false end
 if disconnect and e.name=="small-electric-pole"then e.electric_network_id=8 end
 if contaminate and e.name=="pipe"then e.get_fluid_contents=function()return {water=100}end end
 return e
end
""")
        self.game = SimpleNamespace(
            expected_world="w",
            backend="assisted",
            cfg=SimpleNamespace(runtime_dir=self.root),
            query=lambda script: decode(
                self.lua.execute("return(function() " + " ".join(script.splitlines()) + " end)()")
            ),
        )
        self.catalog = WorldCatalog(dict(version="test"))
        self.builder = FactoryBuilder(
            self.game, SimpleNamespace(ensure_item=Mock(return_value={"type": "take_mall_stock"})), self.catalog
        )

        def node(name, x, y, unit, direction=0, **kw):
            return dict(name=name, position=dict(x=x, y=y), unit_number=unit, direction=direction, **kw)

        nodes = [node("pipe", i - 0.5, 0.5, 99 + i) for i in range(4)] + [
            node("chemical-plant", 4.5, 0.5, 103, recipe="plastic-bar")
        ]
        nodes += [node("transport-belt", i + 0.5, 4.5, 110 + i, 4) for i in range(3)]
        nodes += [
            node("small-electric-pole", 0.5, 7.5, 120),
            node("small-electric-pole", 5.5, 7.5, 121),
            node("fast-inserter", 3.5, 4.5, 130),
        ]
        spec = lambda q: {k: v for k, v in q.items() if k != "unit_number"}
        self.source = dict(
            spec(nodes[0]),
            world_id="w",
            unit_number=99,
            kind="fluid",
            item="petroleum-gas",
            direction="output",
            facing=4,
            fluidbox_index=1,
        )
        first = spec(nodes[1])
        handoff = dict(consumer_id="chemical:plastic-bar", source=self.source, first_neighbor=first)
        es = [spec(q) for q in nodes[1:]]
        plan = dict(entities=es, ports=[dict(es[6], kind="item", item="plastic-bar")], underground_pairs=[])
        self.entry = dict(
            world_id="w",
            catalog_fingerprint=self.catalog.fingerprint,
            phase="verified",
            product="plastic-bar",
            plan=plan,
            sources={"petroleum-gas": self.source},
            fluid_handoffs={"petroleum-gas": handoff},
            bindings={_key(q): q["unit_number"] for q in nodes[1:]},
        )
        self.doc = dict(
            world_id="w",
            catalog_fingerprint=self.catalog.fingerprint,
            products={"plastic-bar": self.entry, "sulfur": {"preserve": ["everything"]}},
        )
        self.board = dict(
            world_id="w", catalog_fingerprint=self.catalog.fingerprint, blocks={"chemical_plastic-bar": deepcopy(plan)}
        )
        self.registry = dict(
            world_id="w",
            catalog_fingerprint=self.catalog.fingerprint,
            sources={"plastic-gas": self.source},
            handoffs={
                "chemical:plastic-bar": {
                    "petroleum-gas": dict(
                        world_id="w",
                        catalog_fingerprint=self.catalog.fingerprint,
                        phase="bound",
                        source=self.source,
                        first_neighbor=dict(first, unit_number=100),
                    )
                }
            },
        )
        self.write("chemical-production.json", self.doc)
        self.write("pcb-board.json", self.board)
        self.write("fluid-sources.json", self.registry)
        self.obs = dict(
            ok=True,
            world_id="w",
            actor_unit_number=11,
            entities=[
                dict(name="small-electric-pole", position=dict(x=0.5, y=7.5), unit_number=120),
                dict(name="small-electric-pole", position=dict(x=-4.5, y=7.5), unit_number=122),
            ],
        )
        self.payload = dict(
            nodes=nodes,
            typed={_key(q): ["plastic-bar"] for q in nodes if q["name"] == "transport-belt"},
            edges=[dict(a=_key(nodes[5 + i]), b=_key(nodes[6 + i])) for i in range(2)],
            pairs=[],
            kinds={q: True for q in ("pipe", "transport-belt", "small-electric-pole")},
            fluid_nodes=nodes[:4],
            fluid_edges=[dict(a=a, b=b) for a, b in zip(nodes[:4], nodes[1:5])],
            machine=_key(nodes[4]),
            prototype_box_count=1,
            physical_boxes={"1": "petroleum-gas"},
            fluid_source=_key(nodes[0]),
            anchor=dict(name="small-electric-pole", position=dict(x=0.5, y=7.5), unit_number=120, network=7),
            preserved_pipes=[],
        )
        adapter = patch(
            "factorio_agent.registered_asset_repair._adapter", side_effect=lambda *args: deepcopy(self.payload)
        )
        adapter.start()
        self.addCleanup(adapter.stop)
        self.ref = SimpleNamespace(state=deepcopy(self.doc))
        self.ctrl = RegisteredAssetRepair(self.game, self.builder, self.catalog, chemical=self.ref)

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value))

    def read(self, name):
        return json.loads((self.root / name).read_text())

    def test_prepare_is_readonly_and_batches_exact_mixed_materials(self):
        old = {p.name: p.read_bytes() for p in self.root.glob("*.json")}
        r = self.ctrl.prepare(self.obs)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["cost"], {"pipe": 1, "transport-belt": 1, "small-electric-pole": 1})
        self.assertEqual([q["old_unit"] for q in r["payload"]["repairs"]], [101, 111, 121])
        self.assertEqual(old, {p.name: p.read_bytes() for p in self.root.glob("*.json")})

    def test_paid_mixed_batch_pickup_conservation_replay_and_nested_publication(self):
        self.lua.execute("pickup=true")
        action = self.ctrl.step(self.obs)
        before_board = (self.root / "pcb-board.json").read_bytes()
        before_registry = (self.root / "fluid-sources.json").read_bytes()
        r = execute_repair_registered_assets(self.game, action)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["paid_count"], 3)
        self.assertEqual(r["before"]["goods"]["combined"], r["after"]["goods"]["combined"])
        self.assertEqual(r["after"]["goods"]["floor"], {})
        self.assertEqual(r["before"]["goods"]["floor"], {"plastic-bar": 4})
        self.assertEqual(r["before"]["corpses"][0]["name"], "belt-remnants")
        self.assertTrue(execute_repair_registered_assets(self.game, action)["ok"])
        self.assertEqual(self.lua.eval("main[1].count"), 9)
        self.lua.execute("game.tick=21")
        restarted = RegisteredAssetRepair(self.game, self.builder, self.catalog, chemical=self.ref)
        self.assertIsNone(restarted.step(self.obs))
        self.assertTrue(restarted.last_diagnostic["ok"], restarted.last_diagnostic)
        after = self.read("chemical-production.json")
        updated = after["products"]["plastic-bar"]
        history = updated.pop("asset_repair_history")
        self.assertEqual(len(history), 1)
        expected = deepcopy(self.doc)
        expected["products"]["plastic-bar"]["bindings"] = updated["bindings"]
        self.assertEqual(after, expected)
        self.assertEqual((self.root / "pcb-board.json").read_bytes(), before_board)
        self.assertEqual((self.root / "fluid-sources.json").read_bytes(), before_registry)

    def test_same_tick_charge_latency_records_paid_receipt_and_waits_without_repayment(self):
        self.lua.execute("delay_energy=true")
        action = self.ctrl.step(self.obs)
        r = execute_repair_registered_assets(self.game, action)
        self.assertTrue(r["ok"], r)
        self.assertIsNone(self.ctrl.step(self.obs))
        self.assertEqual(self.ctrl.last_diagnostic["reason"], "waiting_for_normal_post_tick_asset_health")
        self.assertEqual(self.read(FILE)["transactions"][action["transaction_id"]]["phase"], "native_complete")
        self.assertEqual(self.lua.eval("main[3].count"), 9)
        self.lua.execute("game.tick=21;machine.energy=100;arm.energy=100")
        self.assertIsNone(self.ctrl.step(self.obs))
        self.assertTrue(self.ctrl.last_diagnostic["ok"])

    def test_all_funds_and_all_sites_before_first_debit(self):
        changes = (
            "main[3].count=0",
            'main[1].quality.name="rare"',
            "a.unit_number=12",
            'd.world_id="other"',
            "ore=true",
            "pipe(999,1.5,.5)",
            "pipe(999,1.5,1.5)",
            "world[key{x=.5,y=7.5}].electric_network_id=8",
        )
        for change in changes:
            with self.subTest(change=change):
                self.setUp()
                action = self.ctrl.step(self.obs)
                self.lua.execute(change)
                before = self.lua.eval("main[1].count")
                r = execute_repair_registered_assets(self.game, action)
                self.assertFalse(r["ok"], r)
                self.assertFalse(r.get("effects_started", True), r)
                self.assertEqual(self.lua.eval("main[1].count"), before)
                self.assertTrue(self.lua.eval("d.registered_asset_repairs==nil"))

    def test_after_effect_failures_keep_full_before_and_exact_paid_prefix(self):
        for change in ("fail_create=true", "lose_corpse=true", "disconnect=true", "contaminate=true"):
            with self.subTest(change=change):
                self.setUp()
                action = self.ctrl.step(self.obs)
                self.lua.execute(change)
                r = execute_repair_registered_assets(self.game, action)
                self.assertEqual(r["reason"], "infrastructure_repair_outcome_unknown", r)
                self.assertGreaterEqual(r["paid_count"], 1)
                self.assertEqual(
                    self.lua.eval(
                        "d.registered_asset_repairs["
                        + json.dumps(action["transaction_id"])
                        + "].before.main_items.pipe"
                    ),
                    10,
                )
                second = execute_repair_registered_assets(self.game, action)
                self.assertEqual(second["reason"], "infrastructure_repair_outcome_unknown")
                self.assertEqual(second["paid_count"], r["paid_count"])

    def test_pending_interruption_refunds_nothing_and_requests_normal_stock(self):
        self.ctrl.step(self.obs)
        self.lua.execute("main[2].count=0")
        self.assertEqual(self.ctrl.step(self.obs), {"type": "take_mall_stock"})
        self.builder.bootstrap.ensure_item.assert_called_with(self.obs, "transport-belt", 1)
        self.assertEqual(len(self.read(FILE)["transactions"]), 1)

    def test_owner_root_registry_and_board_cas_fail_closed(self):
        for filename, mutate in (
            ("chemical-production.json", lambda d: d["products"]["sulfur"].update(changed=True)),
            ("pcb-board.json", lambda d: d.update(changed=True)),
            (
                "fluid-sources.json",
                lambda d: d["handoffs"]["chemical:plastic-bar"]["petroleum-gas"].update(phase="declared"),
            ),
        ):
            with self.subTest(filename=filename):
                self.setUp()
                action = self.ctrl.step(self.obs)
                doc = self.read(filename)
                mutate(doc)
                self.write(filename, doc)
                with self.assertRaises(ValueError):
                    execute_repair_registered_assets(self.game, action)
                self.assertEqual(self.lua.eval("main[1].count"), 10)

    def test_foreign_pins_protected_first_neighbor_and_unknown_live_units_block(self):
        self.write("foreign.json", dict(witness={"unit_number": 101}))
        self.assertFalse(self.ctrl.prepare(self.obs)["ok"])
        (self.root / "foreign.json").unlink()
        self.lua.execute("world[key{x=.5,y=.5}]=nil")
        self.assertFalse(self.ctrl.prepare(self.obs)["ok"])

    def test_malformed_envelopes_are_rejected_before_native_io(self):
        action = self.ctrl.step(self.obs)
        query = Mock(side_effect=AssertionError("I/O forbidden"))
        self.game.query = query
        for delta in (
            dict(owner="chemical:sulfur"),
            dict(position={"x": 1, "y": 1}),
            dict(expected_actor_unit=True),
            dict(transaction_id="g" * 64),
            dict(expected_world_id=None),
        ):
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                execute_repair_registered_assets(self.game, dict(action, **delta))
        query.assert_not_called()

    def test_strict_receipt_schema_rejects_missing_baselines_ticks_and_moved_units(self):
        action = self.ctrl.step(self.obs)
        r = execute_repair_registered_assets(self.game, action)
        p = self.read(FILE)["transactions"][action["transaction_id"]]["payload"]
        self.assertTrue(_receipt_valid(r, p, action["transaction_id"]))
        mutations = [
            lambda q: q.pop("tick"),
            lambda q: q.update(start_tick=None, tick=None),
            lambda q: q.pop("before"),
            lambda q: q["before"].pop("goods"),
            lambda q: q["before"]["main_items"].update(pipe=True),
            lambda q: q.update(actor_unit=True),
            lambda q: q.update(catalog_fingerprint="foreign"),
            lambda q: q["replacements"][0].update(position={"x": 99, "y": 99}),
            lambda q: q["after"]["goods"]["combined"].update(fake=1),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                bad = deepcopy(r)
                mutate(bad)
                self.assertFalse(_receipt_valid(bad, p, action["transaction_id"]))

    def test_publication_restart_does_not_duplicate_history_or_mutate_other_products(self):
        action = self.ctrl.step(self.obs)
        self.assertTrue(execute_repair_registered_assets(self.game, action)["ok"])
        self.lua.execute("game.tick=21")
        from factorio_agent import registered_asset_repair as module

        real = module._atomic_json

        def interrupted(path, value):
            real(path, value)
            if Path(path).name == "chemical-production.json":
                raise OSError("interrupt after normal atomic rename")

        with patch.object(module, "_atomic_json", side_effect=interrupted):
            self.assertIsNone(self.ctrl.step(self.obs))
        self.assertFalse(self.ctrl.last_diagnostic["ok"])
        self.assertIsNone(self.ctrl.step(self.obs))
        self.assertTrue(self.ctrl.last_diagnostic["ok"])
        self.assertEqual(
            len(self.read("chemical-production.json")["products"]["plastic-bar"]["asset_repair_history"]), 1
        )

    def test_same_tick_positive_old_buffers_still_wait_for_fresh_game_tick(self):
        action = self.ctrl.step(self.obs)
        self.assertTrue(execute_repair_registered_assets(self.game, action)["ok"])
        self.assertIsNone(self.ctrl.step(self.obs))
        self.assertEqual(self.ctrl.last_diagnostic["reason"], "waiting_for_normal_post_tick_asset_health")
        self.assertEqual(self.read("chemical-production.json"), self.doc)
        self.lua.execute("game.tick=21")
        self.assertIsNone(self.ctrl.step(self.obs))
        self.assertTrue(self.ctrl.last_diagnostic["ok"])

    def test_changed_native_peers_network_recipe_and_corpse_metadata_fail_closed(self):
        for change in (
            "pipe(999,2.5,1.5)",
            'machine.get_recipe=function()return{name="sulfur"}end',
            "world[key{x=.5,y=7.5}].electric_network_id=8",
            'grounds[2].stack.quality.name="rare"',
        ):
            with self.subTest(change=change):
                self.setUp()
                action = self.ctrl.step(self.obs)
                self.lua.execute(change)
                r = execute_repair_registered_assets(self.game, action)
                self.assertFalse(r["ok"], r)
                self.assertFalse(r["effects_started"])
                self.assertEqual(self.lua.eval("main[1].count"), 10)

    def test_compressed_runtime_fluid_index_uses_actual_prototype_mapping(self):
        self.payload["physical_boxes"] = {"3": "petroleum-gas"}
        self.payload["fluid_edges"][-1]["b"] = dict(self.payload["fluid_edges"][-1]["b"], fluidbox_index=3)
        self.lua.execute(
            'machine.get_fluid_box_prototype=function(i)if i~=1 then error("out of bounds")end;return {index=3}end'
        )
        self.assertTrue(self.ctrl.prepare(self.obs)["ok"])
        action = self.ctrl.step(self.obs)
        self.assertTrue(execute_repair_registered_assets(self.game, action)["ok"])

    def test_flattened_lua_has_no_line_comments(self):
        self.assertNotIn("--", ASSET_GUARD_LUA + BUILD_ASSETS_LUA)


if __name__ == "__main__":
    unittest.main()
