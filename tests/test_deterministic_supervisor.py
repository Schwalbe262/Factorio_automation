import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai import deterministic_supervisor as module
from factorio_ai.deterministic_state import RunState, request_stop


def observation(tick=0, **changes):
    return {"ok": True, "world_id": "fixture", "tick": tick, "inventory": {},
            "entities": [], "technologies": {}, "production": {}, **changes}


def fake_game(root):
    return SimpleNamespace(
        cfg=SimpleNamespace(runtime_dir=Path(root), log_dir=Path(root) / "logs", server_port=34213),
        backend="assisted", query=Mock(return_value={"initialized": False}),
        initialize=Mock(return_value={"ok": True}), observe=Mock(return_value=observation()),
        act=Mock(return_value={"ok": True}), save=Mock())


def fake_catalog():
    return SimpleNamespace(fingerprint="catalog-fixture", to_dict=lambda: {}, first_rocket_bom=lambda: {})


class SupervisorLifecycleTests(unittest.TestCase):
    def assert_prepare_order(self, initialized, expected):
        with TemporaryDirectory() as root:
            game = fake_game(root)
            calls = []
            game.query.side_effect = lambda body: calls.append("probe") or {"initialized": initialized}
            game.initialize.side_effect = lambda: calls.append("initialize") or {"ok": True}
            game.observe.side_effect = lambda: calls.append("observe") or observation()
            supervisor = module.DeterministicSupervisor(game)
            with patch.object(supervisor, "connect_character", side_effect=lambda: calls.append("connect") or {"status": "ready"}), \
                 patch.object(module.WorldCatalog, "from_game", side_effect=lambda query: calls.append("catalog") or fake_catalog()), \
                 patch("factorio_ai.deterministic_bootstrap.DeterministicBootstrap"):
                result = supervisor.prepare()
            self.assertEqual(calls, expected)
            self.assertEqual(result["world_id"], "fixture")
            self.assertIsInstance(supervisor.state, RunState)

    def test_resume_reconnects_before_initialization_can_reject_offline_actor(self):
        self.assert_prepare_order(True, ["probe", "connect", "initialize", "catalog", "observe"])

    def test_fresh_world_initializes_actor_before_connecting_player(self):
        self.assert_prepare_order(False, ["probe", "initialize", "connect", "initialize", "catalog", "observe"])

    def test_prepare_failure_writes_terminal_status_and_cleans_up(self):
        with TemporaryDirectory() as root:
            game = fake_game(root)
            game.query.side_effect = TimeoutError("world probe timed out")
            result = module.DeterministicSupervisor(game).run(cycles=1, interval=0)
            persisted = json.loads((Path(root) / "status.json").read_text())
            self.assertEqual(result["status"], "failed")
            self.assertEqual(persisted["reason"], "world probe timed out")
            self.assertEqual(persisted["evidence"]["exception_type"], "TimeoutError")
            self.assertEqual(persisted["cycle"], 0)
            game.act.assert_called_once_with({"type": "stop"})
            game.save.assert_called_once()
            self.assertFalse((Path(root) / "checkpoint.json").exists())

    def prepared(self, supervisor, first):
        supervisor.catalog = fake_catalog()
        supervisor.state = RunState("fixture", supervisor.catalog.fingerprint, first["tick"])
        return first

    def run_sequence(self, root, observations, stages, choices=None):
        game = fake_game(root)
        supervisor = module.DeterministicSupervisor(game)
        game.observe.side_effect = observations[1:] + [observations[-1]]
        stage_iter = iter(stages)
        choice_iter = iter(choices or [{"status": "waiting", "reason": "fixture work"}] * len(stages))

        def next_action(obs, until):
            supervisor.stage = next(stage_iter)
            return next(choice_iter)

        with patch.object(supervisor, "prepare", side_effect=lambda: self.prepared(supervisor, observations[0])), \
             patch.object(supervisor, "next_action", side_effect=next_action), \
             patch("factorio_ai.deterministic_character.ensure_crafting_player", return_value={"status": "ready"}), \
             patch.object(module.time, "sleep"), patch.object(module.time, "monotonic", return_value=0):
            result = supervisor.run(cycles=len(stages), interval=0)
        return supervisor, result, json.loads((Path(root) / "status.json").read_text())

    def test_cycle_limit_overwrites_last_running_status_and_saves_checkpoint(self):
        with TemporaryDirectory() as root:
            supervisor, result, persisted = self.run_sequence(root, [observation()], ["bootstrap"],
                [{"type": "craft", "recipe": "lab", "count": 1}])
            self.assertEqual(result["status"], "waiting")
            self.assertEqual(result["reason"], "cycle_limit_reached")
            self.assertEqual(persisted["reason"], result["reason"])
            self.assertEqual(persisted["cycle"], 1)
            self.assertEqual(supervisor.game.act.call_args.args[0], {"type": "stop"})
            self.assertTrue((Path(root) / "checkpoint.json").exists())

    def test_operator_stop_written_during_prepare_is_honored_before_action(self):
        with TemporaryDirectory() as root:
            game = fake_game(root)
            supervisor = module.DeterministicSupervisor(game)

            def prepare():
                request_stop(Path(root) / "stop.json")
                return self.prepared(supervisor, observation())

            with patch.object(supervisor, "prepare", side_effect=prepare), patch.object(supervisor, "next_action") as action:
                result = supervisor.run(cycles=5, interval=0)
            persisted = json.loads((Path(root) / "status.json").read_text())
            self.assertEqual(result["reason"], "operator_stop_requested")
            self.assertEqual(persisted["status"], "waiting")
            self.assertEqual(persisted["cycle"], 0)
            action.assert_not_called()
            game.act.assert_called_once_with({"type": "stop"})

    def test_interrupt_while_connecting_is_persisted_as_operator_stop(self):
        with TemporaryDirectory() as root:
            supervisor = module.DeterministicSupervisor(fake_game(root))
            with patch.object(supervisor, "prepare", side_effect=InterruptedError("operator_stop_requested")):
                result = supervisor.run(cycles=1, interval=0)
            self.assertEqual(result["status"], "waiting")
            self.assertEqual(json.loads((Path(root) / "status.json").read_text())["reason"], "operator_stop_requested")

    def test_cleanup_failure_preserves_prior_failure_in_terminal_report(self):
        with TemporaryDirectory() as root:
            game = fake_game(root)
            game.query.side_effect = RuntimeError("connection failed")
            game.save.side_effect = OSError("save unavailable")
            result = module.DeterministicSupervisor(game).run(cycles=1, interval=0)
            self.assertEqual(result["reason"], "run cleanup failed")
            self.assertEqual(result["evidence"]["prior_result"]["reason"], "connection failed")
            self.assertEqual(result["evidence"]["errors"], ["save unavailable"])

    def test_power_builds_remain_progress_across_bootstrap_fuel_chores(self):
        with TemporaryDirectory() as root:
            built = [{"name": "boiler", "unit_number": 1}, {"name": "steam-engine", "unit_number": 2},
                     {"name": "offshore-pump", "unit_number": 3}]
            rows = [observation(0, entities=built[:1]),
                    observation(18000, entities=built[:1], inventory={"coal": 5}),
                    observation(35000, entities=built[:2]),
                    observation(55000, entities=built[:2], inventory={"coal": 10}),
                    observation(70000, entities=built[:3])]
            supervisor, result, _ = self.run_sequence(root, rows, ["power", "bootstrap", "power", "bootstrap", "power"])
            self.assertEqual(result["reason"], "cycle_limit_reached")
            self.assertEqual(supervisor.state.tasks["objective"].last_progress_tick, 70000)

    def test_extra_coal_and_plates_cannot_hide_lab_bootstrap_stall(self):
        with TemporaryDirectory() as root:
            rows = [observation(tick,
                inventory={"coal": 10 + index * 1000},
                production={"iron-plate": {"produced": 50 + index * 500},
                            "copper-plate": {"produced": 10 + index * 100}},
                technologies={"steam-power": True, "electronics": True},
                entities=[{"name": "burner-mining-drill", "unit_number": n} for n in range(4)])
                for index, tick in enumerate([0, 18000, 36000])]
            supervisor, result, persisted = self.run_sequence(root, rows, ["bootstrap"] * 3)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["reason"], "no objective progress for ten game minutes")
            self.assertEqual(persisted["reason"], result["reason"])
            self.assertEqual(supervisor.state.tasks["objective"].last_progress_tick, 0)


if __name__ == "__main__":
    unittest.main()
