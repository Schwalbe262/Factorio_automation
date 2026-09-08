"""Execute construction checks against local tables, without any world mutation."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factorio_ai import deterministic_game
from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_ai.deterministic_game import DeterministicGame, run_config


SHADOW = '''
local storage=nil;local game=nil;local o=helpers.json_to_table(OPTIONS)
local f={};local inv=setmetatable({},{__index=function() error("unexpected inventory access") end})
local a={valid=true,get_main_inventory=function() return inv end}
local old={name=o.name,position={x=0,y=0},direction=o.actual,force=o.foreign and {} or f,unit_number=3818}
local function target(p,name) if name==old.name and p.x==0 and p.y==0 then return old end end
local s=setmetatable({},{__index=function() error("unexpected construction or terrain access") end})
local function success(t) t.ok=true;return t end
local function failure(reason) return {ok=false,reason=reason} end
'''


@unittest.skipUnless(os.environ.get("FACTORIO_TURRET_FACING_LIVE_TEST") == "1", "requires opt-in existing RCON server")
class TurretFacingLuaTests(unittest.TestCase):
    def execute(self, mode, name, actual, desired, *, foreign=False, backend="assisted"):
        with TemporaryDirectory() as temporary:
            game = DeterministicGame(run_config(runtime=Path(temporary),
                rcon_port=int(os.environ.get("FACTORIO_TURRET_FACING_RCON_PORT", "27025"))), backend=backend)
            spec = {"name": name, "position": {"x": 0, "y": 0}, "direction": desired}
            with patch.object(game, "query", return_value={"ok": True}) as query, patch.object(game, "_record_action"):
                if mode == "placement":
                    FactoryBuilder(game, None, SimpleNamespace(entities={})).can_place([spec])
                else:
                    game.act({"type": "build", **spec})
                body = query.call_args.args[0]
            options = {"name": name, "actual": actual, "foreign": foreign}
            with patch.object(deterministic_game, "_HELPERS", ""):
                return game.query(SHADOW.replace("OPTIONS", json.dumps(json.dumps(options))) + body)

    def test_placement_accepts_owned_turret_facing_but_retains_other_geometry(self):
        for name, actual, desired, foreign, expected in (
                ("gun-turret", 12, 0, False, True), ("gun-turret", 12, 0, True, False),
                ("inserter", 12, 0, False, False), ("transport-belt", 12, 0, False, False),
                ("steam-engine", 0, 8, False, True)):
            with self.subTest(name=name, foreign=foreign):
                result = self.execute("placement", name, actual, desired, foreign=foreign)
                self.assertEqual(result["ok"], expected, result)

    def test_reuse_spends_no_items_and_keeps_direction_guards_in_both_backends(self):
        for backend in ("assisted", "character"):
            for name, foreign, expected in (("gun-turret", False, True), ("gun-turret", True, False),
                                             ("inserter", False, False), ("transport-belt", False, False)):
                with self.subTest(backend=backend, name=name, foreign=foreign):
                    result = self.execute("reuse", name, 12, 0, foreign=foreign, backend=backend)
                    self.assertEqual(result["ok"], expected, result)
                    if expected:
                        self.assertEqual((result["status"], result["reused"], result["unit_number"]), ("succeeded", True, 3818))
                    else:
                        self.assertEqual(result["reason"], "existing_direction_mismatch")
