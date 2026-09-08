"""Opt-in catalog and inert Lua checks; no world helpers or actions are used."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai import deterministic_game
from factorio_ai.deterministic_game import DeterministicGame, run_config
from factorio_ai.deterministic_fluids import FluidProduction
from factorio_ai.deterministic_petroleum_bus import ensure_petroleum_bus
from factorio_ai.world_catalog import WorldCatalog


@unittest.skipUnless(os.environ.get("FACTORIO_PETROLEUM_LIVE_TEST") == "1", "requires inert RCON fixture")
class PetroleumBusLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = DeterministicGame(run_config(
            seed=1, runtime=Path(os.environ.get("FACTORIO_PETROLEUM_RUNTIME", "runtime/deterministic/factory-fixture")),
            server_port=34214, rcon_port=int(os.environ.get("FACTORIO_PETROLEUM_RCON_PORT", "27029"))))

    def check_endpoint(self, **changes):
        options = {"present": True, "owned": True, "segment": True, "fluid": "petroleum-gas", "world": "test"}
        options.update(changes)
        port = {"kind": "fluid", "item": "petroleum-gas", "direction": "output", "position": {"x": .5, "y": .5}}
        plan = {"ports": [port], "entities": []}
        fluids = SimpleNamespace(state={"sources": {"basic-oil-processing": plan}},
            builder=SimpleNamespace(ensure_plan=Mock(return_value={"status": "succeeded"})),
            factory=SimpleNamespace(ensure_power_connection=Mock(return_value={"status": "succeeded"})),
            game=SimpleNamespace(query=Mock(return_value={"ok": True, "world_id": "test"})),
            _connect_pipe=Mock(return_value={"status": "succeeded"}))
        ensure_petroleum_bus(fluids, {"world_id": "test"}, plan)
        body = fluids.game.query.call_args.args[0]
        shadow = '''
local game=nil;local storage=nil;local f={}
local o=helpers.json_to_table(OPTIONS_JSON);local d={world_id=o.world}
local function target()
 if not o.present then return nil end
 return {force=o.owned and f or {},get_fluid_segment_id=function() return o.segment and 7 or nil end,
         get_fluid_contents=function() if o.fluid=="empty" then return {} end;return {[o.fluid]=10} end}
end
'''.replace("OPTIONS_JSON", json.dumps(json.dumps(options)))
        with patch.object(deterministic_game, "_HELPERS", ""):
            result = self.live.query(shadow + body)
        fluids.game.query.return_value = result
        fluids._connect_pipe.reset_mock()
        outcome = ensure_petroleum_bus(fluids, {"world_id": "test"}, plan)
        return outcome, fluids._connect_pipe.call_count

    def test_empty_or_petroleum_owned_segments_pass_without_game_actions(self):
        for fluid in ("empty", "petroleum-gas"):
            with self.subTest(fluid=fluid):
                outcome, calls = self.check_endpoint(fluid=fluid)
                self.assertEqual((outcome["status"], calls), ("succeeded", 1))

    def test_missing_foreign_unsegmented_or_other_fluid_never_reaches_builder(self):
        for options in ({"present": False}, {"owned": False}, {"segment": False},
                        {"fluid": "water"}, {"fluid": "heavy-oil"}, {"world": "other"}):
            with self.subTest(options=options):
                outcome, calls = self.check_endpoint(**options)
                self.assertEqual((outcome["status"], calls), ("blocked", 0))

    def test_empty_taps_require_opt_in_and_the_same_owned_segment(self):
        fluids = object.__new__(FluidProduction)
        fluids.game = SimpleNamespace(query=Mock(return_value={"ok": True}))
        destination = {"position": {"x": 10.5, "y": .5}}
        for allow_empty in (False, True):
            source = {"position": {"x": .5, "y": .5}, "item": "petroleum-gas", "allow_empty_segment": allow_empty}
            fluids._network_taps(source, destination)
            body = fluids.game.query.call_args.args[0]
            shadow = '''
local game=nil;local storage=nil;local f={}
local function pos(p) return {x=p.x,y=p.y} end
local function pipe(x,segment,contents)
 return {position={x=x,y=.5},get_fluid_segment_id=function() return segment end,
         get_fluid_contents=function() return contents end}
end
local pipes={pipe(1.5,7,{}),pipe(2.5,7,{['petroleum-gas']=10}),pipe(3.5,7,{water=10}),pipe(4.5,9,{})}
local s={find_entities_filtered=function(spec)
 assert(spec.force==f and spec.type=='pipe','only current owned pipes may be surveyed');return pipes
end}
local function target(p) return pipe(p.x,p.x==.5 and 7 or 8,{}) end
'''
            with patch.object(deterministic_game, "_HELPERS", ""):
                proof = self.live.query(shadow + body)
            self.assertTrue(proof["ok"], proof)
            self.assertEqual({p["x"] for p in proof["taps"]}, {1.5, 2.5} if allow_empty else {2.5})

    def test_actual_catalog_silo_reaches_advanced_refining_and_preserves_all_outputs(self):
        path = Path(os.environ.get("FACTORIO_PETROLEUM_CATALOG", "runtime/deterministic/adapter-smoke/catalog.json"))
        if not path.exists():
            self.skipTest("actual exported main catalog unavailable")
        catalog = WorldCatalog.from_dict(json.loads(path.read_text(encoding="utf-8-sig")))
        order = catalog.technology_order("rocket-silo", include_researched=True)
        for earlier, later in (("chemical-science-pack", "advanced-oil-processing"),
                               ("advanced-oil-processing", "rocket-silo"), ("electric-engine", "rocket-silo")):
            self.assertLess(order.index(earlier), order.index(later))
        self.assertIn("electric-engine-unit", [r["name"] for r in catalog.recipes["rocket-silo"]["ingredients"]])
        self.assertIn("lubricant", [r["name"] for r in catalog.recipes["electric-engine-unit"]["ingredients"]])
        self.assertEqual({r["name"] for r in catalog.recipes["advanced-oil-processing"]["products"]},
                         {"heavy-oil", "light-oil", "petroleum-gas"})
        self.assertIn("basic-oil-processing", catalog.recipes)


if __name__ == "__main__":
    unittest.main()
