from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.fluid_handoffs import bind_handoff, bound_handoffs, declare_handoff

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


class FluidHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "fluid-sources.json"
        self.catalog = SimpleNamespace(fingerprint="catalog")
        self.obs = {"world_id": "w"}
        self.source = dict(
            kind="fluid",
            item="petroleum-gas",
            direction="output",
            facing=4,
            name="pipe",
            position={"x": 10.5, "y": 0.5},
            fluidbox_index=1,
            unit_number=10,
            world_id="w",
            defense_scope_id="p",
        )
        self.neighbor = dict(name="pipe", position={"x": 11.5, "y": 0.5}, direction=0, fluidbox_index=1)
        self.registry = dict(
            world_id="w", catalog_fingerprint="catalog", sources={"petroleum-gas": deepcopy(self.source)}
        )
        self.path.write_text(json.dumps(self.registry))
        self.game = SimpleNamespace(
            expected_world="w", cfg=SimpleNamespace(runtime_dir=self.root), query=Mock(return_value={"ok": True})
        )

    def declare(self, neighbor=None, owner="chemical:plastic-bar"):
        return declare_handoff(
            self.game, self.catalog, self.obs, owner, "petroleum-gas", self.source, neighbor or self.neighbor
        )

    def bind(self, unit=11):
        return bind_handoff(
            self.game,
            self.catalog,
            self.obs,
            "chemical:plastic-bar",
            "petroleum-gas",
            self.source,
            dict(self.neighbor, unit_number=unit),
        )

    def test_declaration_binding_and_retry_preserve_all_other_sources(self):
        self.assertTrue(self.declare()["ok"])
        before = self.path.read_bytes()
        self.assertTrue(self.declare()["ok"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(bound_handoffs(json.loads(before), self.obs, self.catalog, self.source), [])
        self.assertTrue(self.bind()["ok"])
        bound = self.path.read_bytes()
        self.assertTrue(self.bind()["ok"])
        self.assertEqual(self.path.read_bytes(), bound)
        saved = json.loads(bound)
        self.assertEqual(saved["sources"], self.registry["sources"])
        self.assertEqual(
            bound_handoffs(saved, self.obs, self.catalog, self.source)[0]["first_neighbor"]["unit_number"], 11
        )
        self.assertTrue(self.declare()["ok"])
        self.assertEqual(self.path.read_bytes(), bound)

    def test_changed_geometry_unit_and_second_consumer_do_not_retarget(self):
        with self.assertRaises(ValueError):
            self.declare(dict(self.neighbor, position={"x": 10.5, "y": 1.5}))
        self.assertFalse(self.bind()["ok"])
        self.assertTrue(self.declare()["ok"])
        self.assertTrue(self.bind()["ok"])
        before = self.path.read_bytes()
        self.assertFalse(self.bind(12)["ok"])
        self.assertFalse(self.declare(owner="chemical:sulfur")["ok"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_native_rejection_never_publishes_a_handoff(self):
        self.game.query.return_value = {"ok": False, "reason": "handoff_source_identity_changed"}
        before = self.path.read_bytes()
        self.assertFalse(self.declare()["ok"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_explicit_published_alias_uses_same_fluid_without_moving_canonical_source(self):
        self.registry["sources"] = {
            "plastic-gas": deepcopy(self.source),
            "petroleum-gas": dict(self.source, unit_number=99),
        }
        self.path.write_text(json.dumps(self.registry))
        self.assertTrue(self.declare()["ok"])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["sources"], self.registry["sources"])
        self.source["unit_number"] = 12
        with self.assertRaises(ValueError):
            self.declare()


@unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
class NativeFluidHandoffTests(FluidHandoffTests):
    def setUp(self):
        super().setUp()
        self.lua = LuaRuntime(unpack_returned_tuples=True)

        def table(v):
            if isinstance(v, dict):
                return self.lua.table_from({k: table(a) for k, a in v.items()})
            if isinstance(v, list):
                return self.lua.table_from([table(a) for a in v])
            return v

        def decode(v):
            return {k: decode(a) for k, a in v.items()} if hasattr(v, "items") else v

        self.lua.globals().helpers = self.lua.table_from({"json_to_table": lambda v: table(json.loads(v))})
        self.lua.execute("""f={};s={};present=false;reciprocal=true;contamination=false
src={force=f,surface=s,unit_number=10,position={x=10.5,y=.5},get_fluid_contents=function()return {["petroleum-gas"]=1}end}
dest={force=f,surface=s,unit_number=11,position={x=11.5,y=.5},get_fluid_contents=function()return {[contamination and"water"or"petroleum-gas"]=1}end}
src.get_fluid_box_neighbours=function()return {{entity=dest,index=1}}end
dest.get_fluid_box_neighbours=function()return reciprocal and{{entity=src,index=1}}or{}end
function target(p,name)if p.x==10.5 then return src end;return present and dest or nil end
""")
        self.game.query = Mock(
            side_effect=lambda script: decode(
                self.lua.execute("return(function() " + " ".join(script.splitlines()) + " end)()")
            )
        )

    def bind(self, unit=11):
        if json.loads(self.path.read_text()).get("handoffs"):
            self.lua.execute("present=true")
        return super().bind(unit)

    def test_native_rejection_never_publishes_a_handoff(self):
        self.lua.execute("src.unit_number=99")
        before = self.path.read_bytes()
        self.assertFalse(self.declare()["ok"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_reciprocal_peer_or_contamination_cannot_bind(self):
        self.assertTrue(self.declare()["ok"])
        before = self.path.read_bytes()
        self.lua.execute("reciprocal=false")
        self.assertFalse(self.bind()["ok"])
        self.assertEqual(self.path.read_bytes(), before)
        self.lua.execute("reciprocal=true;contamination=true")
        self.assertFalse(self.bind()["ok"])
        self.assertEqual(self.path.read_bytes(), before)
        self.lua.execute("contamination=false")
        self.assertTrue(self.bind()["ok"])
