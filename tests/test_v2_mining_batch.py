from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.mining_batch import execute_mine_many, MINE_MANY_LUA


def request(count=2):
    return {
        "type": "mine_many",
        "actions": [
            {
                "type": "mine",
                "name": "transport-belt",
                "position": {"x": i + 0.5, "y": 0.5},
                "count": 1,
                "expected_entity_unit": i + 10,
                "expected_entity_world_id": "w",
            }
            for i in range(count)
        ],
    }


def success(unit):
    return {"ok": True, "status": "succeeded", "unit_number": unit}


class MiningBatchTests(unittest.TestCase):
    def game(self, receipt=None):
        return SimpleNamespace(backend="assisted", expected_world="w", query=Mock(return_value=receipt))

    def test_all_children_share_one_native_query(self):
        receipt = {"ok": True, "status": "succeeded", "completed": 2, "results": [success(10), success(11)]}
        game = self.game(receipt)
        self.assertEqual(execute_mine_many(game, request()), receipt)
        game.query.assert_called_once()

    def test_later_invalid_child_rejects_entire_envelope_before_first_effect(self):
        for mutate in (
            lambda row: row.update(count=2),
            lambda row: row.update(expected_entity_world_id="other"),
            lambda row: row.update(expected_entity_unit=True),
            lambda row: row["position"].update(x=float("nan")),
            lambda row: row.update(type="build"),
        ):
            action = request()
            mutate(action["actions"][1])
            game = self.game()
            with self.assertRaises(ValueError):
                execute_mine_many(game, action)
            game.query.assert_not_called()

    def test_character_backend_is_rejected_without_native_io(self):
        game = self.game()
        game.backend = "character"
        with self.assertRaises(ValueError):
            execute_mine_many(game, request())
        game.query.assert_not_called()

    def test_duplicate_units_or_tiles_and_oversized_batches_are_rejected(self):
        for action in (request(0), request(9), {"type": "mine_many", "actions": [request()["actions"][0]] * 2}):
            game = self.game()
            with self.assertRaises(ValueError):
                execute_mine_many(game, action)
            game.query.assert_not_called()

    def test_known_failure_returns_exact_confirmed_prefix(self):
        receipt = {
            "ok": False,
            "status": "blocked",
            "reason": "mining_failed",
            "completed": 1,
            "results": [success(10), {"ok": False, "reason": "mining_failed"}],
            "failed_index": 1,
        }
        self.assertEqual(execute_mine_many(self.game(receipt), request()), receipt)

    def test_native_preflight_failure_confirms_zero_effects(self):
        receipt = {
            "ok": False,
            "status": "blocked",
            "reason": "owned_mine_target_changed",
            "completed": 0,
            "results": {},
            "failed_index": 1,
        }
        self.assertEqual(execute_mine_many(self.game(receipt), request())["results"], [])

    def test_lua_error_after_confirmed_prefix_retains_unknown_status(self):
        receipt = {
            "ok": False,
            "status": "blocked",
            "reason": "mine_batch_outcome_unknown",
            "completed": 1,
            "results": [success(10)],
            "uncertain_index": 1,
        }
        self.assertEqual(execute_mine_many(self.game(receipt), request()), receipt)

    def test_transport_error_never_retries_and_requires_reconciliation(self):
        game = self.game()
        game.query.side_effect = TimeoutError("receipt lost")
        receipt = execute_mine_many(game, request())
        self.assertEqual(receipt["reason"], "mine_batch_outcome_unknown")
        self.assertEqual(receipt["completed"], 0)
        game.query.assert_called_once()

    def test_unexpected_native_receipts_cannot_be_retried_as_known_failures(self):
        for receipt in (
            None,
            {},
            {"ok": False, "reason": "Lua error"},
            {"ok": True, "status": "succeeded", "completed": 1, "results": [success(10)]},
            {"ok": True, "status": "succeeded", "completed": 2, "results": [success(10), success(99)]},
            {"ok": False, "status": "blocked", "reason": "bad", "completed": 1, "results": [success(10), None]},
        ):
            self.assertEqual(execute_mine_many(self.game(receipt), request())["reason"], "mine_batch_outcome_unknown")

    def test_native_source_guards_precede_effect_and_survive_rcon_line_flattening(self):
        flat = " ".join(MINE_MANY_LUA.splitlines())
        self.assertNotIn("--", flat)
        self.assertLess(flat.index("for i,spec in ipairs(batch.actions)"), flat.index("a.mine_entity(e)"))
        for guard in (
            "e.force~=f",
            "e.surface~=s",
            "e.position.x~=spec.position.x",
            'e.type=="resource"',
            "protected_artifact",
            "pcall(function()",
            "mine_batch_outcome_unknown",
        ):
            self.assertIn(guard, flat)


if __name__ == "__main__":
    unittest.main()
