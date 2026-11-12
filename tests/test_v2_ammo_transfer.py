import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.ammo_transfer import (
    execute_take_full_magazines,
    execute_configure_ammo_buffer,
    TAKE_FULL_MAGAZINES_LUA,
    CONFIGURE_AMMO_BUFFER_LUA,
)
from factorio_agent.runtime import WorldBoundGame

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


def take_action():
    return {
        "type": "take_full_magazines",
        "name": "wooden-chest",
        "position": {"x": 0.5, "y": 0.5},
        "item": "firearm-magazine",
        "count": 5,
        "source_slots": [1],
        "expected_entity_unit": 7,
        "expected_entity_world_id": "w",
        "expected_actor_unit": 11,
    }


class AmmoEnvelopeTests(unittest.TestCase):
    def test_malformed_envelopes_fail_before_native_io(self):
        game = SimpleNamespace(query=Mock(), expected_world="w", backend="assisted")
        changes = (
            {"count": True},
            {"count": 0},
            {"count": 51},
            {"source_slots": [1, 1]},
            {"source_slots": [True]},
            {"source_slots": [49]},
            {"source_slots": []},
            {"item": "iron-plate"},
            {"name": "assembling-machine-1"},
            {"expected_actor_unit": False},
            {"expected_entity_world_id": "other"},
            {"position": {"x": float("nan"), "y": 0.5}},
            {"unknown": "field"},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                execute_take_full_magazines(game, {**take_action(), **change})
        game.query.assert_not_called()

    def test_buffer_configuration_is_bounded_and_keeps_exact_identities(self):
        action = {k: v for k, v in take_action().items() if k not in ("item", "count", "source_slots")}
        action.update(type="configure_ammo_buffer", slots=1)
        game = SimpleNamespace(query=Mock(return_value={"ok": True}), expected_world="w", backend="assisted")
        for slots in (0, True, 2, 50):
            with self.assertRaises(ValueError):
                execute_configure_ammo_buffer(game, {**action, "slots": slots})
        game.query.assert_not_called()
        self.assertTrue(execute_configure_ammo_buffer(game, action)["ok"])
        for body in (TAKE_FULL_MAGAZINES_LUA, CONFIGURE_AMMO_BUFFER_LUA):
            self.assertNotIn("--", " ".join(body.splitlines()))
            self.assertNotIn("source.remove", body)

    def test_worldbound_intercepts_both_helpers_and_records_receipts(self):
        game = WorldBoundGame.__new__(WorldBoundGame)
        game.expected_world = "w"
        game.backend = "assisted"
        game.query = Mock(return_value={"ok": True, "moved": 1})
        game._record_action = Mock(side_effect=lambda action, result: result)
        self.assertEqual(game.act(take_action())["moved"], 1)
        action = {k: v for k, v in take_action().items() if k not in ("item", "count", "source_slots")}
        action.update(type="configure_ammo_buffer", slots=1)
        game.act(action)
        self.assertEqual(game._record_action.call_count, 2)


@unittest.skipUnless(LuaRuntime, "optional Lua runtime: pip install lupa")
class NativeAmmoTransferTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)

        def table(v):
            if isinstance(v, dict):
                return self.lua.table_from({k: table(a) for k, a in v.items()})
            if isinstance(v, list):
                return self.lua.table_from([table(a) for a in v])
            return v

        def decode(v):
            if not hasattr(v, "items"):
                return v
            keys = list(v.keys())
            if keys and all(type(k) is int for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
                return [decode(v[i]) for i in range(1, len(keys) + 1)]
            return {k: decode(a) for k, a in v.items()}

        self.lua.globals().helpers = self.lua.table_from({"json_to_table": lambda raw: table(json.loads(raw))})
        self.lua.execute("""f={};s={};d={world_id="w"};defines={inventory={chest=1}}
prototypes={item={["firearm-magazine"]={type="ammo",magazine_size=10}}};bar=17;capacity=100
local function inventory(size)local inv={valid=true};for i=1,size do inv[i]={valid_for_read=false}end;return inv end
source=inventory(16);dest=inventory(4)
function magazine(count,ammo,quality)return {valid_for_read=true,name="firearm-magazine",count=count,ammo=ammo,
 quality={name=quality or "normal"},prototype=prototypes.item["firearm-magazine"],health=1,spoil_tick=0,item={name="firearm-magazine"}}end
source[1]=magazine(10,10)
source.supports_bar=function()return true end;source.get_bar=function()return bar end;source.set_bar=function(value)bar=value end
dest.get_insertable_count=function()return capacity end
dest.insert=function(spec)local n=math.min(spec.count,capacity);if n<=0 then return 0 end
 if not dest[1].valid_for_read then dest[1]=magazine(n,10)else dest[1].count=dest[1].count+n end
 capacity=capacity-n;return n end
a={valid=true,unit_number=11,force=f,surface=s,get_main_inventory=function()return dest end,can_reach_entity=function()return true end}
chest={valid=true,name="wooden-chest",type="container",unit_number=7,force=f,surface=s,position={x=.5,y=.5},get_inventory=function()return source end}
function target(p,name)return chest end""")
        self.game = SimpleNamespace(
            expected_world="w",
            backend="assisted",
            query=lambda script: decode(
                self.lua.execute("return(function() " + " ".join(script.splitlines()) + " end)()")
            ),
        )

    def test_exact_insert_and_slot_decrement_conserve_counts_and_rounds(self):
        self.lua.execute("source[1]=magazine(3,7);dest[1]=magazine(2,4)")
        result = execute_take_full_magazines(self.game, take_action())
        self.assertEqual(result["moved"], 2)
        self.assertEqual(result["rounds_before"], result["rounds_after"])
        self.assertEqual(self.lua.eval("source[1].count"), 1)
        self.assertEqual(self.lua.eval("source[1].ammo"), 7)
        self.assertEqual(self.lua.eval("dest[1].count"), 4)
        self.assertEqual(self.lua.eval("dest[1].ammo"), 4)

    def test_capacity_bounds_total_transfer_and_full_inventory_moves_nothing(self):
        self.lua.execute("capacity=2")
        result = execute_take_full_magazines(self.game, take_action())
        self.assertEqual(result["moved"], 2)
        self.assertEqual(self.lua.eval("source[1].count"), 8)
        self.assertEqual(execute_take_full_magazines(self.game, take_action())["moved"], 0)
        self.assertEqual(self.lua.eval("source[1].count"), 8)

    def test_single_partial_magazine_is_never_recreated_or_refilled(self):
        self.lua.execute("source[1]=magazine(1,1)")
        result = execute_take_full_magazines(self.game, take_action())
        self.assertEqual(result["moved"], 0)
        self.assertEqual(self.lua.eval("source[1].ammo"), 1)
        self.assertFalse(self.lua.eval("dest[1].valid_for_read"))

    def test_changed_world_actor_chest_quality_or_slot_metadata_blocks_before_transfer(self):
        cases = (
            'd.world_id="other"',
            "a.unit_number=99",
            "chest.unit_number=99",
            'source[1].quality.name="rare"',
            "source[1].health=.5",
            'source[1].name="iron-plate"',
        )
        for change in cases:
            with self.subTest(change=change):
                self.setUp()
                self.lua.execute(change)
                self.assertFalse(execute_take_full_magazines(self.game, take_action())["ok"])
                self.assertEqual(self.lua.eval("source[1].count"), 10)
                self.assertFalse(self.lua.eval("dest[1].valid_for_read"))

    def test_all_selected_slots_validate_before_any_slot_moves(self):
        self.lua.execute('source[2]=magazine(1,10,"rare")')
        result = execute_take_full_magazines(self.game, {**take_action(), "source_slots": [1, 2]})
        self.assertFalse(result["ok"])
        self.assertEqual(self.lua.eval("source[1].count"), 10)

    def test_unsupported_actor_magazine_metadata_is_not_merged_or_normalized(self):
        self.lua.execute("dest[1]=magazine(1,3);dest[1].health=.5")
        result = execute_take_full_magazines(self.game, take_action())
        self.assertEqual(result["reason"], "ammo_actor_magazine_metadata_unsupported")
        self.assertEqual(self.lua.eval("dest[1].health"), 0.5)
        self.assertEqual(self.lua.eval("dest[1].ammo"), 3)
        self.assertEqual(self.lua.eval("source[1].count"), 10)

    def test_single_slot_bar_is_normal_and_does_not_discard_existing_extra_goods(self):
        action = {k: v for k, v in take_action().items() if k not in ("item", "count", "source_slots")}
        action.update(type="configure_ammo_buffer", slots=1)
        self.assertTrue(execute_configure_ammo_buffer(self.game, action)["ok"])
        self.assertEqual(self.lua.eval("bar"), 2)
        self.lua.execute("bar=17;source[2]=magazine(1,10)")
        self.assertFalse(execute_configure_ammo_buffer(self.game, action)["ok"])
        self.assertEqual(self.lua.eval("bar"), 17)
        self.assertEqual(self.lua.eval("source[2].count"), 1)


if __name__ == "__main__":
    unittest.main()
