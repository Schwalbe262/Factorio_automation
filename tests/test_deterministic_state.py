import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from factorio_ai.deterministic_state import (
    CheckpointError, RunAlreadyOwnedError, RunLock, RunState, TaskResult, TaskStatus,
    append_task_event, clear_stop, load_run_state, request_stop, save_run_state, stop_requested,
)


class DeterministicStateTests(unittest.TestCase):
    def test_restart_preserves_goal_evidence_and_retry_budget(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            state = RunState("world-a", "2.1.9-space-age")
            failure = TaskResult("failed", "coal route obstructed", failure_code="route_collision")
            state.record_task("coal_feed", failure, tick=100, progress={"connected_tiles": 3})
            state.record_task("iron", TaskResult("succeeded", evidence={"produced": 50}), tick=101, progress=50)
            save_run_state(path, state)
            resumed = load_run_state(path, world_id="world-a", game_fingerprint="2.1.9-space-age", tick=102)
            self.assertEqual(resumed.tasks["iron"].status, "succeeded")
            self.assertEqual(resumed.tasks["iron"].evidence, {"produced": 50})
            resumed.record_task("coal_feed", failure, tick=103, progress={"connected_tiles": 3})
            bounded = resumed.record_task("coal_feed", failure, tick=104, progress={"connected_tiles": 3})
            self.assertEqual(bounded.status, TaskStatus.BLOCKED)
            self.assertEqual(bounded.failure_code, "retry_budget_exhausted")
            self.assertEqual(bounded.evidence["attempts"], 3)

    def test_rollback_world_and_prototype_changes_invalidate_all_completion(self):
        for world, fingerprint, tick, reason in [
            ("world-b", "game-a", 200, "world_changed"),
            ("world-a", "game-b", 200, "game_fingerprint_changed"),
            ("world-a", "game-a", 99, "game_tick_rolled_back"),
        ]:
            with self.subTest(reason=reason), TemporaryDirectory() as root:
                path = Path(root) / "state.json"
                state = RunState("world-a", "game-a")
                state.record_task("rocket", TaskResult("succeeded", evidence={"rockets": 1}), tick=100)
                save_run_state(path, state)
                resumed = load_run_state(path, world_id=world, game_fingerprint=fingerprint, tick=tick)
                self.assertEqual(resumed.tasks, {})
                self.assertEqual(resumed.generation, 1)
                self.assertEqual(resumed.invalidation_reason, reason)

    def test_wait_and_unrelated_goal_progress_do_not_hide_stall(self):
        state = RunState("world", "game")
        state.record_task("power", TaskResult("waiting", "waiting for steam"), tick=1, progress=0)
        state.record_task("coal", TaskResult("running"), tick=500, progress=1000)
        state.record_task("power", TaskResult("waiting", "waiting for steam"), tick=501, progress=0)
        self.assertTrue(state.is_stalled("power", tick=501, max_stall_ticks=400))
        self.assertFalse(state.is_stalled("coal", tick=501, max_stall_ticks=400))
        self.assertEqual(state.tasks["power"].status, "waiting")
        state.record_task("power", TaskResult("running"), tick=502, progress=1)
        self.assertFalse(state.is_stalled("power", tick=502, max_stall_ticks=400))

    def test_failure_oscillation_does_not_reset_retry_budget(self):
        state = RunState("world", "game")
        for tick, code in enumerate(["missing_belts", "no_power", "missing_belts", "no_power", "missing_belts"]):
            result = state.record_task("bootstrap", TaskResult("failed", code, failure_code=code), tick=tick, progress=0)
        self.assertEqual(result.status, TaskStatus.BLOCKED)
        self.assertEqual(result.evidence["attempts"], 3)

    def test_local_progress_allows_new_attempts_and_records_rollback(self):
        state = RunState("world", "game")
        failure = TaskResult("failed", "collision")
        state.record_task("route", failure, tick=10, progress=0, max_retries=1)
        result = state.record_task("route", failure, tick=11, progress=1, max_retries=2)
        self.assertEqual(result.status, TaskStatus.FAILED)
        state.record_task("different", TaskResult("waiting"), tick=1)
        self.assertNotIn("route", state.tasks)
        self.assertEqual(state.generation, 1)

    def test_atomic_checkpoint_keeps_previous_state_on_replace_failure(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            state = RunState("world", "game", 1)
            save_run_state(path, state)
            before = path.read_bytes()
            state.last_tick = 2
            with patch("factorio_ai.deterministic_state.os.replace", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    save_run_state(path, state)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(root).glob("*.tmp")), [])

    def test_corrupt_checkpoint_fails_without_overwriting_evidence(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text('{"incomplete":', encoding="utf-8")
            with self.assertRaises(CheckpointError):
                load_run_state(path, world_id="world", game_fingerprint="game", tick=0)
            self.assertEqual(path.read_text(), '{"incomplete":')

    def test_journal_carries_world_goal_and_generation(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            state = RunState("world", "game", 8, generation=2)
            result = TaskResult("blocked", "no reachable ore", {"resource": "iron-ore"}, "missing_resource")
            append_task_event(path, state, "iron", result)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual((payload["world_id"], payload["goal_id"], payload["generation"], payload["tick"]), ("world", "iron", 2, 8))
            self.assertEqual(payload["result"]["status"], "blocked")

    def test_lock_excludes_competitor_and_survives_stale_pid_metadata(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "run.lock"
            # A recycled/live PID in a stale file must not prevent recovery.
            path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            with RunLock(path):
                with self.assertRaises(RunAlreadyOwnedError):
                    RunLock(path).acquire()
            self.assertTrue(path.exists())
            with RunLock(path):
                pass

    def test_os_lock_released_after_abrupt_process_exit(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "run.lock"
            code = "from pathlib import Path; import os,sys; from factorio_ai.deterministic_state import RunLock; owner=RunLock(Path(sys.argv[1])).acquire(); os._exit(0)"
            env = dict(os.environ)
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            result = subprocess.run([sys.executable, "-c", code, str(path)], timeout=10,
                                    capture_output=True, env=env,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            with RunLock(path):
                pass

    def test_stop_flag_persists_until_explicit_resume(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "stop.json"
            self.assertFalse(stop_requested(path))
            request_stop(path)
            self.assertTrue(stop_requested(path))
            clear_stop(path)
            clear_stop(path)
            self.assertFalse(stop_requested(path))

    def test_invalid_status_and_retry_budget_rejected(self):
        with self.assertRaises(ValueError):
            TaskResult("done")
        with self.assertRaises(ValueError):
            RunState("world", "game").record_task("x", TaskResult("waiting"), tick=1, max_retries=0)


if __name__ == "__main__":
    unittest.main()
