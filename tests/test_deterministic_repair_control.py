"""Repair transaction orchestration, cleanup and ordinary strict navigation."""
import json
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai import deterministic_repair_control as repair
from factorio_ai.deterministic_navigation import CharacterNavigator


ACTION = {"type": "repair", "name": "steel-chest", "position": {"x": 3.5, "y": .5},
          "expected_world_id": "fixture", "expected_actor_unit": 15, "expected_entity_unit": 44}
RUNNING = {"ok": True, "status": "running"}
FINISHED = {"ok": True, "status": "succeeded", "repaired": 12, "durability_used": 6}


class RepairControlTests(unittest.TestCase):
    def game(self, *results):
        return SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path("unused")), query=Mock(side_effect=results))

    def run_burst(self, game, action=ACTION, **options):
        with patch.object(repair, "stop_requested", return_value=options.get("stop", False)), \
                patch.object(repair.time, "sleep"), \
                patch.object(repair.time, "monotonic", side_effect=options.get("clock", [0] * 50)):
            return repair.run_repair(game, action)

    def test_invalid_identity_and_coordinates_cannot_issue_any_query(self):
        game = self.game()
        invalid = [{**ACTION, key: value} for key, value in (
            ("type", "build"), ("expected_world_id", ""), ("expected_world_id", 1),
            ("expected_actor_unit", True), ("expected_actor_unit", 0), ("expected_entity_unit", -1),
            ("expected_entity_unit", 44.0), ("name", ""), ("position", {"x": float("nan"), "y": 0}),
            ("position", {"x": 1, "y": True}), ("position", []), ("request", 2))]
        invalid += [{k: v for k, v in ACTION.items() if k != field}
                    for field in ("expected_world_id", "expected_actor_unit", "expected_entity_unit")]
        for action in invalid:
            with self.subTest(action=action), self.assertRaises(ValueError):
                repair.run_repair(game, action)
        game.query.assert_not_called()

    def test_engine_end_of_burst_returns_paid_receipt_and_uses_one_exact_request(self):
        game = self.game(RUNNING, RUNNING, {"ok": True, "status": "waiting"}, FINISHED)
        self.assertEqual(self.run_burst(game), FINISHED)
        bodies = [call.args[0] for call in game.query.call_args_list]
        self.assertTrue(bodies[0].endswith(repair.BEGIN_REPAIR_LUA))
        self.assertTrue(all(body.endswith(repair.PULSE_REPAIR_LUA) for body in bodies[1:-1]))
        self.assertTrue(bodies[-1].endswith(repair.FINISH_REPAIR_LUA))
        requests = [json.loads(json.loads(re.search(r'json_to_table\(("(?:[^"\\]|\\.)*")\)', body)[1]))
                    for body in bodies]
        self.assertEqual(len({row["request"] for row in requests}), 1)
        self.assertTrue(requests[0]["request"])
        self.assertTrue(all(row["expected_actor_unit"] == 15 for row in requests))

    def test_pulse_count_and_wall_deadline_are_bounded_and_cleanup_runs(self):
        game = self.game(RUNNING, *([RUNNING] * repair.MAX_REPAIR_PULSES), FINISHED)
        self.assertEqual(self.run_burst(game), FINISHED)
        self.assertEqual(game.query.call_count, repair.MAX_REPAIR_PULSES + 2)
        game = self.game(RUNNING, FINISHED)
        self.assertEqual(self.run_burst(game, clock=[0, 2]), FINISHED)
        self.assertEqual(game.query.call_count, 2)

    def test_operator_stop_releases_claim_without_any_new_repair_input(self):
        game = self.game(RUNNING, FINISHED)
        self.assertEqual(self.run_burst(game, stop=True), FINISHED)
        self.assertEqual(game.query.call_count, 2)
        self.assertTrue(game.query.call_args.args[0].endswith(repair.FINISH_REPAIR_LUA))

    def test_lost_prepare_or_pulse_reply_still_attempts_exact_request_cleanup(self):
        for outcomes in ((OSError("lost prepare reply"), FINISHED),
                         (RUNNING, OSError("lost pulse reply"), FINISHED)):
            with self.subTest(outcomes=outcomes):
                game = self.game(*outcomes)
                with self.assertRaises(OSError):
                    self.run_burst(game)
                self.assertTrue(game.query.call_args.args[0].endswith(repair.FINISH_REPAIR_LUA))

    def test_failed_cleanup_or_owner_guard_is_not_reported_as_success(self):
        failure = {"ok": False, "reason": "native_repair_cursor_changed"}
        game = self.game(RUNNING, {"ok": True, "status": "waiting"}, failure)
        self.assertEqual(self.run_burst(game), failure)
        already = {"ok": True, "status": "succeeded", "reason": "native_repair_already_stopped"}
        game = self.game(failure, already)
        self.assertEqual(self.run_burst(game), failure)
        self.assertEqual(game.query.call_count, 2)

    def test_cleanup_after_restart_does_not_restart_repair_input(self):
        action = repair.pending_repair({"world_id": "fixture", "actor_unit_number": 15,
                                        "repair_pending": {"request": "saved-request"}})
        self.assertEqual(action["type"], "finish_repair")
        game = self.game(FINISHED)
        self.assertEqual(self.run_burst(game, action), FINISHED)
        game.query.assert_called_once()
        self.assertTrue(game.query.call_args.args[0].endswith(repair.FINISH_REPAIR_LUA))
        self.assertIsNone(repair.pending_repair({}))
        for pending in ({}, {"request": ""}, [], False, "request"):
            with self.subTest(pending=pending), self.assertRaises(ValueError):
                repair.pending_repair({"world_id": "fixture", "actor_unit_number": 15,
                                       "repair_pending": pending})

    def test_strict_repair_walks_into_reach_and_pending_mining_keeps_priority(self):
        game = SimpleNamespace(backend="character", query=Mock(return_value={"ok": True, "within": False}),
                               act=Mock(return_value=FINISHED))
        navigator = CharacterNavigator(game)
        navigator.pending_action = Mock(return_value=None)
        navigator._input = Mock(return_value=RUNNING)
        self.assertEqual(navigator.execute(ACTION, {}), RUNNING)
        navigator._input.assert_called_once_with({"type": "move", "position": ACTION["position"]})
        game.act.assert_not_called()
        self.assertNotIn("teleport", game.query.call_args.args[0])
        game.query.return_value = {"ok": True, "within": True}
        navigator._input.reset_mock()
        self.assertEqual(navigator.execute(ACTION, {}), FINISHED)
        navigator._input.assert_not_called()
        game.act.assert_called_once_with(ACTION)
        pending = {"type": "mine", "name": "coal", "position": {"x": 1, "y": 1}, "count": 12}
        navigator.pending_action.return_value = pending
        game.query.reset_mock(); game.act.reset_mock()
        navigator.execute(ACTION, {})
        navigator._input.assert_called_once_with(pending)
        game.query.assert_not_called(); game.act.assert_not_called()


if __name__ == "__main__":
    unittest.main()
