import tempfile
import socket
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from factorio_ai.deterministic_game import DeterministicGame, run_config, start_world
from factorio_ai.factorio import no_mod_save_path
from factorio_ai.rcon import RconError


class DeterministicGameTests(unittest.TestCase):
    def test_new_world_never_overwrites_existing_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = run_config(runtime=Path(tmp))
            save = no_mod_save_path(cfg)
            save.parent.mkdir(parents=True)
            save.write_bytes(b"irreplaceable save")
            with patch("factorio_ai.deterministic_game.subprocess.Popen") as process:
                with self.assertRaises(FileExistsError):
                    start_world(cfg, seed=1, new_world=True)
                process.assert_not_called()
            self.assertEqual(save.read_bytes(), b"irreplaceable save")

    def test_resume_requires_existing_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                start_world(run_config(runtime=Path(tmp)), seed=1)

    def test_timed_out_action_is_never_replayed(self):
        game = DeterministicGame(run_config())
        game._confirmed = True
        with patch("factorio_ai.deterministic_game.FactorioRconClient") as factory:
            client = factory.return_value.__enter__.return_value
            client.execute.side_effect = TimeoutError("lost response")
            with self.assertRaises(TimeoutError):
                game.query("return {ok=true}")
            self.assertEqual(client.execute.call_count, 1)

    def test_busy_rcon_port_cannot_attach_to_another_world(self):
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            cfg = run_config(runtime=Path(tmp), rcon_port=listener.getsockname()[1])
            with patch("factorio_ai.deterministic_game.subprocess.Popen") as process:
                with self.assertRaisesRegex(RuntimeError, "already in use"):
                    start_world(cfg, seed=1, new_world=True)
                process.assert_not_called()
            self.assertFalse(no_mod_save_path(cfg).exists())

    def test_model_environment_does_not_enable_slurm(self):
        with patch.dict("os.environ", {"FACTORIO_AI_SLURM_ENABLED": "1",
                                       "FACTORIO_AI_REQUIRE_LLM_STRATEGY": "1"}):
            self.assertFalse(run_config().slurm_enabled)

    def test_negative_and_boolean_counts_rejected_before_mutation(self):
        game = DeterministicGame(run_config())
        with patch.object(game, "query") as query:
            for n in [-1, 0, True, 1.5]:
                with self.assertRaises(ValueError):
                    game.act({"type": "mine", "count": n})
            query.assert_not_called()

    def test_partial_or_invalid_source_upgrade_guards_are_rejected_before_rcon(self):
        guard = {"expected_world_id": "world", "expected_unit_number": 7,
                 "exhausted_source_receiver": {"name": "wooden-chest", "position": {"x": .5, "y": .5}},
                 "required_replacement_item": "electric-mining-drill"}
        action = {"type": "mine", "name": "burner-mining-drill", "position": {"x": 1, "y": 2}, "count": 1, **guard}
        invalid = [{k: v for k, v in action.items() if k != field} for field in guard]
        invalid += [{**action, **change} for change in ({"expected_unit_number": True}, {"expected_unit_number": 0},
            {"expected_world_id": ""}, {"type": "build"}, {"count": True}, {"count": 2},
            {"required_replacement_item": "stone-furnace"},
            {"exhausted_source_receiver": {"name": "wooden-chest", "position": {"x": float("nan"), "y": 0}}})]
        invalid += [{"type": "mine", **changes} for changes in (
            {"expected_entity_unit": 7}, {"expected_entity_world_id": "world"},
            {"expected_entity_unit": True, "expected_entity_world_id": "world"},
            {"expected_entity_unit": 7, "expected_entity_world_id": ""})]
        game = DeterministicGame(run_config())
        with patch.object(game, "query") as query:
            for candidate in invalid:
                with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                    game.act(candidate)
            query.assert_not_called()

    @staticmethod
    def belt(index=0, **changes):
        return {"type": "build", "name": "transport-belt", "item": "transport-belt",
                "position": {"x": index + .5, "y": .5}, "direction": 4, **changes}

    @classmethod
    def pipe(cls, index=0, **changes):
        return cls.belt(index, **{"name": "pipe", "item": "pipe", "direction": 0, **changes})

    def test_batch_uses_exact_single_build_validation_and_item_cost_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = DeterministicGame(run_config(runtime=Path(tmp)))
            actions = [self.belt(0), self.pipe(1), self.belt(2, name="small-electric-pole", item="small-electric-pole")]
            outcomes = [{"ok": True, "status": "succeeded", "unit_number": 10},
                        {"ok": True, "status": "succeeded", "unit_number": 11, "reused": True},
                        {"ok": True, "status": "succeeded", "unit_number": 12}]
            with patch.object(game, "query", side_effect=outcomes) as query:
                for child in actions:
                    game.act(child)
                ordinary_commands = list(query.call_args_list)
            with patch.object(game, "query", side_effect=outcomes) as query:
                result = game.act({"type": "build_many", "actions": actions})
                self.assertEqual(query.call_args_list, ordinary_commands)
            # Reused infrastructure follows the original early return and spends
            # no item. Only the two ordinary creations are counted as new builds.
            self.assertEqual((result["completed"], result["built"], result["reused"]), (3, 2, 1))
            self.assertEqual(result["results"], outcomes)
            self.assertTrue(result["ok"])

    def test_batch_failure_keeps_confirmed_prefix_and_never_attempts_later_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = DeterministicGame(run_config(runtime=Path(tmp)))
            succeeded = {"ok": True, "status": "succeeded", "unit_number": 10}
            failure = {"ok": False, "reason": "missing_item:transport-belt"}
            with patch.object(game, "query", side_effect=[succeeded, failure]) as query:
                result = game.act({"type": "build_many", "actions": [self.belt(i) for i in range(3)]})
            self.assertEqual(query.call_count, 2)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], failure["reason"])
            self.assertEqual((result["completed"], result["built"], result["failed_index"]), (1, 1, 1))
            self.assertEqual(result["results"], [succeeded, failure])

    def test_uncertain_batch_response_stops_without_retry_and_preserves_confirmed_prefix(self):
        for error in (TimeoutError("response lost"), RconError("invalid response"), OSError("connection closed")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp:
                game = DeterministicGame(run_config(runtime=Path(tmp)))
                succeeded = {"ok": True, "status": "succeeded", "unit_number": 10}
                with patch.object(game, "query", side_effect=[succeeded, error]) as query:
                    result = game.act({"type": "build_many", "actions": [self.pipe(i) for i in range(3)]})
                self.assertEqual(query.call_count, 2)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["reason"], "build_batch_outcome_unknown")
                self.assertEqual((result["completed"], result["built"], result["uncertain_index"]), (1, 1, 1))
                self.assertEqual(result["results"], [succeeded])
                self.assertEqual(result["exception_type"], type(error).__name__)

    def test_pipe_batch_failure_preserves_paid_and_reused_prefix_and_leaves_later_builds_unattempted(self):
        for reason in ("missing_item:pipe", "placement_blocked", "existing_direction_mismatch"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                game = DeterministicGame(run_config(runtime=Path(tmp)))
                reused = {"ok": True, "status": "succeeded", "unit_number": 10, "reused": True}
                paid = {"ok": True, "status": "succeeded", "unit_number": 11}
                failure = {"ok": False, "reason": reason}
                actions = [self.pipe(0), self.pipe(1), self.pipe(2), self.belt(3)]
                with patch.object(game, "query", side_effect=[reused, paid, failure]) as query:
                    result = game.act({"type": "build_many", "actions": actions})
                self.assertEqual(query.call_count, 3)
                self.assertFalse(result["ok"])
                self.assertEqual((result["completed"], result["built"], result["reused"], result["failed_index"]), (2, 1, 1, 2))
                self.assertEqual(result["results"], [reused, paid, failure])
                self.assertEqual(result["reason"], reason)

    def test_character_backend_refuses_batch_before_mutating(self):
        game = DeterministicGame(run_config(), backend="character")
        with patch.object(game, "query") as query:
            with self.assertRaisesRegex(ValueError, "assisted"):
                game.act({"type": "build_many", "actions": [self.belt()]})
            query.assert_not_called()

    def test_malformed_later_batch_children_are_rejected_before_any_mutation(self):
        game = DeterministicGame(run_config())
        invalid = [None, [], {"type": "build_many", "actions": [self.belt()]}, self.belt(type="mine"),
                   self.belt(name="pipe-to-ground", item="pipe-to-ground"),
                   self.belt(name="assembling-machine-1"), self.belt(name=[]), self.belt(item=None),
                   self.belt(position={"x": .5}), self.belt(position={"x": True, "y": .5}),
                   self.belt(position={"x": float("nan"), "y": .5}), self.belt(direction=True),
                   self.belt(direction=1), self.belt(count=-1), self.belt(reason=object())]
        for child in invalid:
            with self.subTest(child=child), patch.object(game, "query") as query:
                with self.assertRaises(ValueError):
                    game.act({"type": "build_many", "actions": [self.belt(), child]})
                query.assert_not_called()

    def test_batch_limit_is_enforced_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = DeterministicGame(run_config(runtime=Path(tmp)))
            for rows in ([], [self.belt(i) for i in range(33)], "not a list"):
                with self.subTest(rows=len(rows)), patch.object(game, "query") as query:
                    with self.assertRaises(ValueError):
                        game.act({"type": "build_many", "actions": rows})
                    query.assert_not_called()
            with patch.object(game, "query", return_value={"ok": True, "status": "succeeded"}) as query:
                result = game.act({"type": "build_many", "actions": [self.belt(i) for i in range(32)]})
            self.assertEqual(query.call_count, 32)
            self.assertEqual(result["built"], 32)


if __name__ == "__main__":
    unittest.main()
