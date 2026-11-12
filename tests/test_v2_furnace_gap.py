import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.furnace_gap import execute_gap_build

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


def action():
    return dict(
        type="build_furnace_gap",
        position={"x": -40, "y": -32},
        direction=0,
        resource="iron-ore",
        expected_old_unit=4139,
        expected_actor_unit=11,
        expected_world_id="w",
    )


class GapEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = dict(
            phase="upgrade",
            world_id="w",
            resource="iron-ore",
            pending=dict(name="stone-furnace", direction=0, unit_number=4139, position={"x": -40, "y": -32}),
        )
        self.path = self.root / "furnace-upgrade.json"
        self.path.write_text(json.dumps(self.state))
        self.game = SimpleNamespace(
            expected_world="w", backend="assisted", cfg=SimpleNamespace(runtime_dir=self.root), query=Mock()
        )

    def test_rejects_wrong_or_uncommitted_identity_before_query(self):
        for key, value in [
            ("expected_old_unit", 4140),
            ("expected_world_id", "other"),
            ("position", {"x": -36, "y": -32}),
        ]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                execute_gap_build(self.game, dict(action(), **{key: value}))
        self.state.pop("pending")
        self.path.write_text(json.dumps(self.state))
        with self.assertRaises(ValueError):
            execute_gap_build(self.game, action())
        self.game.query.assert_not_called()


@unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
class NativeGapTests(GapEnvelopeTests):
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

        self.lua.globals().helpers = self.lua.table_from({"json_to_table": lambda raw: table(json.loads(raw))})
        self.lua.execute("""f={};s={};d={world_id="w"};defines={build_check_type={manual_ghost=1}}
prototypes={entity={["steel-furnace"]={collision_box={left_top={x=-.7,y=-.7},right_bottom={x=.7,y=.7}}}}}
stock={["steel-furnace"]=1,coal=3};capacity=100;built_count=0;foreign=false;collision=false
inv={get_item_count=function(q)return stock[q.name]or 0 end,get_insertable_count=function(q)return capacity end,
 insert=function(q)local n=math.min(capacity,q.count);stock[q.name]=(stock[q.name]or 0)+n;return n end,
 remove=function(q)local n=math.min(stock[q.name]or 0,q.count);stock[q.name]=(stock[q.name]or 0)-n;return n end}
a={valid=true,force=f,surface=s,unit_number=11,get_main_inventory=function()return inv end}
ground={valid=true,type="item-entity",stack={valid_for_read=true,prototype={type="item"},name="coal",count=1,
 quality={name="normal"},health=1,spoil_tick=0}}
ground.destroy=function()ground.valid=false end
function target(p,name)return nil end
s.can_place_entity=function(q)return not collision and(q.forced or not ground.valid)end
s.find_entities_filtered=function(q)if foreign then return {{type="transport-belt"}}end;return ground.valid and{ground}or{} end
s.create_entity=function(q)assert(not ground.valid);assert(stock["steel-furnace"]==0);built_count=built_count+1;return {unit_number=9001}end
""")
        self.game.query = Mock(
            side_effect=lambda script: decode(
                self.lua.execute("return(function() " + " ".join(script.splitlines()) + " end)()")
            )
        )

    def test_one_command_conserves_spill_and_pays_exactly_one_furnace(self):
        self.assertEqual(execute_gap_build(self.game, action(), dry_run=True)["status"], "ready")
        self.assertEqual(self.lua.eval("stock.coal"), 3)
        result = execute_gap_build(self.game, action())
        self.assertTrue(result["ok"])
        self.assertEqual(result["picked"], {"coal": 1})
        self.assertEqual(
            (self.lua.eval("stock.coal"), self.lua.eval('stock["steel-furnace"]'), self.lua.eval("built_count")),
            (4, 0, 1),
        )

    def test_contamination_capacity_collision_and_actor_changes_have_no_effects(self):
        for code in (
            'ground.stack.quality.name="rare"',
            "capacity=0",
            "foreign=true",
            "collision=true",
            "a.unit_number=12",
        ):
            with self.subTest(code=code):
                self.setUp()
                self.lua.execute(code)
                result = execute_gap_build(self.game, action())
                self.assertFalse(result["ok"])
                self.assertEqual(
                    (
                        self.lua.eval("stock.coal"),
                        self.lua.eval('stock["steel-furnace"]'),
                        self.lua.eval("built_count"),
                    ),
                    (3, 1, 0),
                )
