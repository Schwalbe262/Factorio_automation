from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.storage_transfer import execute_deposit, DEPOSIT_LUA


def action():
    return {
        "type": "deposit",
        "name": "wooden-chest",
        "position": {"x": 0.5, "y": 0.5},
        "item": "wood",
        "count": 1000,
        "expected_entity_unit": 55,
        "expected_entity_world_id": "w",
    }


def receipt(moved=298, **extra):
    return {
        "ok": True,
        "status": "succeeded" if moved else "waiting",
        "moved": moved,
        "requested": 1000,
        "unit_number": 55,
        "world_id": "w",
        "item": "wood",
        "quality": "normal",
        **extra,
    }


class StorageTransferTests(unittest.TestCase):
    def game(self, result=None):
        return SimpleNamespace(
            backend="assisted", expected_world="w", query=Mock(return_value=receipt() if result is None else result)
        )

    def test_actual_deposit_is_one_bounded_native_transfer(self):
        game = self.game()
        self.assertEqual(execute_deposit(game, action())["moved"], 298)
        game.query.assert_called_once()

    def test_malformed_envelopes_reject_before_any_native_io(self):
        changes = (
            {"count": True},
            {"count": 0},
            {"count": 1001},
            {"count": 1.5},
            {"name": "lab"},
            {"item": ""},
            {"expected_entity_unit": True},
            {"expected_entity_unit": 0},
            {"expected_entity_world_id": ""},
            {"expected_entity_world_id": "other"},
            {"position": {"x": float("nan"), "y": 0.5}},
            {"position": {"x": True, "y": 0.5}},
            {"inventory": "ammo"},
            {"reason": 1},
        )
        for change in changes:
            game = self.game()
            with self.subTest(change=change), self.assertRaises(ValueError):
                execute_deposit(game, {**action(), **change})
            game.query.assert_not_called()

    def test_all_three_ordinary_chest_types_are_supported(self):
        for name in ("wooden-chest", "iron-chest", "steel-chest"):
            self.assertEqual(execute_deposit(self.game(), {**action(), "name": name})["moved"], 298)

    def test_character_backend_preserves_native_reach_guard(self):
        game = self.game()
        game.backend = "character"
        execute_deposit(game, action())
        script = game.query.call_args.args[0]
        self.assertIn("local character_backend=true;", script)
        self.assertIn("character_backend and not a.can_reach_entity(e)", script)

    def test_unknown_backend_is_rejected_before_query(self):
        game = self.game()
        game.backend = "admin"
        with self.assertRaises(ValueError):
            execute_deposit(game, action())
        game.query.assert_not_called()

    def test_short_insertion_reports_only_actual_moved_count(self):
        result = receipt(2)
        self.assertEqual(execute_deposit(self.game(result), action()), result)

    def test_full_chest_or_no_eligible_stock_is_known_waiting_zero(self):
        self.assertEqual(execute_deposit(self.game(receipt(0)), action())["status"], "waiting")

    def test_exact_native_guard_failures_remain_known_zero_effects(self):
        result = receipt(0, ok=False, status="blocked", reason="storage_chest_changed")
        self.assertEqual(execute_deposit(self.game(result), action()), result)

    def test_native_metadata_failure_does_not_authorize_transferring_ammo_or_tools(self):
        result = receipt(0, ok=False, status="blocked", reason="storage_item_metadata_unsupported")
        self.assertEqual(execute_deposit(self.game(result), action()), result)
        self.assertIn('proto.type~="item"', DEPOSIT_LUA)
        self.assertIn("stack.health==1", DEPOSIT_LUA)
        self.assertIn("stack.spoil_tick==0", DEPOSIT_LUA)
        self.assertIn("stack.item==nil", DEPOSIT_LUA)

    def test_transport_exception_returns_unknown_without_retrying(self):
        game = self.game()
        game.query.side_effect = TimeoutError("lost receipt")
        result = execute_deposit(game, action())
        self.assertEqual(result["reason"], "storage_transfer_outcome_unknown")
        self.assertNotIn("moved", result)
        game.query.assert_called_once()

    def test_native_exception_keeps_only_confirmed_moved_prefix(self):
        result = {
            "ok": False,
            "status": "blocked",
            "reason": "storage_transfer_outcome_unknown",
            "confirmed_moved": 2,
            "exception": "changed",
        }
        self.assertEqual(execute_deposit(self.game(result), action()), result)

    def test_unexpected_receipts_cannot_be_accepted_or_retried(self):
        results = (
            {},
            receipt(1001),
            receipt(-1),
            receipt(True),
            receipt(3, status="waiting"),
            receipt(0, status="succeeded"),
            receipt(unit_number=56),
            receipt(world_id="other"),
            receipt(quality="legendary"),
            receipt(item="firearm-magazine"),
            receipt(requested=True),
            receipt(0, ok=False, status="blocked", reason="unexpected_Lua_error"),
        )
        for result in results:
            with self.subTest(result=result):
                game = self.game(result)
                self.assertEqual(execute_deposit(game, action())["reason"], "storage_transfer_outcome_unknown")
                game.query.assert_called_once()

    def test_normal_insert_decrements_exact_slot_and_rollback_has_no_item_grant(self):
        flat = " ".join(DEPOSIT_LUA.splitlines())
        self.assertNotIn("--", flat)
        self.assertIn("stack.count=before-inserted", flat)
        self.assertIn('dest.remove{name=x.item,quality="normal",count=inserted}', flat)
        self.assertNotIn("source.remove", flat)
        self.assertNotIn("source.insert", flat)
        self.assertNotIn("create_entity", flat)
        for guard in (
            "e.unit_number~=x.expected_entity_unit",
            "e.force~=f",
            "e.surface~=s",
            "e.position.x~=x.position.x",
            "d.world_id~=x.expected_entity_world_id",
        ):
            self.assertLess(flat.index(guard), flat.index("dest.insert"))


if __name__ == "__main__":
    unittest.main()
