import tempfile
import socket
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from factorio_ai.deterministic_game import DeterministicGame, run_config, start_world
from factorio_ai.factorio import no_mod_save_path


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


if __name__ == "__main__":
    unittest.main()
