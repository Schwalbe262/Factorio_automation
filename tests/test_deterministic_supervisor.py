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

    def test_failed_action_stops_after_confirmed_partial_progress_without_replanning(self):
        with TemporaryDirectory() as root:
            game = fake_game(root)
            supervisor = module.DeterministicSupervisor(game)
            choice = {"type": "build_many", "actions": []}
            failure = {"ok": False, "reason": "placement_blocked", "completed": 1, "built": 1}
            game.act.side_effect = lambda action: failure if action["type"] == "build_many" else {"ok": True}
            with patch.object(supervisor, "prepare", side_effect=lambda: self.prepared(supervisor, observation())), \
                 patch.object(supervisor, "next_action", return_value=choice) as choose, \
                 patch("factorio_ai.deterministic_character.ensure_crafting_player", return_value={"status": "ready"}):
                result = supervisor.run(cycles=3, interval=0)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["cycles"], 1)
            self.assertEqual(result["evidence"]["result"]["built"], 1)
            choose.assert_called_once()
            self.assertEqual([call.args[0]["type"] for call in game.act.call_args_list], ["build_many", "stop"])
            self.assertEqual(json.loads((Path(root) / "status.json").read_text())["reason"], "placement_blocked")

    def test_component_failure_is_terminal(self):
        with TemporaryDirectory() as root:
            _, result, persisted = self.run_sequence(root, [observation(), observation(10)],
                ["production", "production"], [{"status": "failed", "reason": "invalid_plan"}] * 2)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["cycles"], 1)
            self.assertEqual(persisted["reason"], "invalid_plan")

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

    def test_component_success_cannot_complete_the_requested_rocket_milestone(self):
        for stage in ("defense", "energy", "armaments"):
            with self.subTest(stage=stage), TemporaryDirectory() as root:
                supervisor, result, _ = self.run_sequence(root, [observation()], [stage],
                    [{"status": "succeeded", "reason": "component observed", "evidence": {"urgent": True}}])
                self.assertEqual(result["status"], "waiting")
                self.assertEqual(result["reason"], "cycle_limit_reached")
                self.assertEqual(supervisor.state.tasks[stage].status, "waiting")

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

    def test_active_character_mining_batch_preempts_new_partial_stock_plan(self):
        with TemporaryDirectory() as root:
            game = fake_game(root)
            supervisor = module.DeterministicSupervisor(game)
            mining = {"type": "mine", "name": "coal", "count": 24, "position": {"x": 10, "y": 5}}
            navigator = Mock()
            navigator.pending_action.return_value = mining
            navigator.execute.return_value = {"ok": True, "status": "running"}
            supervisor.navigator = navigator
            first = observation(inventory={"coal": 1})
            with patch.object(supervisor, "prepare", side_effect=lambda: self.prepared(supervisor, first)), \
                 patch.object(supervisor, "next_action") as replan, \
                 patch("factorio_ai.deterministic_character.ensure_crafting_player", return_value={"status": "ready"}):
                result = supervisor.run(cycles=1, interval=0)
            replan.assert_not_called()
            navigator.execute.assert_called_once_with(mining, first)
            navigator.stop.assert_called_once()
            game.act.assert_not_called()
            self.assertEqual(result["reason"], "cycle_limit_reached")

    def test_resumed_automatic_fuel_ownership_is_loaded_before_bootstrap(self):
        with TemporaryDirectory() as root:
            supervisor = module.DeterministicSupervisor(fake_game(root))
            automated = {"name": "stone-furnace", "position": {"x": 0, "y": 0}}
            ordinary = {"name": "burner-mining-drill", "position": {"x": 5, "y": 5}}
            obs = observation(entities=[automated, ordinary])
            supervisor.bootstrap = Mock()
            supervisor.bootstrap.next_action.return_value = {"status": "waiting", "reason": "ordinary startup"}
            supervisor.builder = Mock(state={"power_sample_tick": 1})
            supervisor.builder.owns_automated_burner.return_value = False
            factory = Mock()
            factory.owns_automated_burner.side_effect = lambda e: e is automated
            with patch.object(supervisor, "prepare_production", side_effect=lambda: setattr(supervisor, "factory", factory)):
                supervisor.next_action(obs, "rocket")
            factory._sync.assert_called_once_with(obs)
            supplied = supervisor.bootstrap.next_action.call_args.args[0]
            self.assertEqual(supplied["entities"], [ordinary])
            supervisor.builder.ensure_power.assert_not_called()

    def production_supervisor(self, root):
        supervisor = module.DeterministicSupervisor(fake_game(root))
        supervisor.bootstrap = Mock()
        supervisor.bootstrap.next_action.return_value = {"status": "succeeded"}
        supervisor.builder = Mock(state={"power_sample_tick": 1})
        supervisor.builder.ensure_power.return_value = {"status": "succeeded"}
        supervisor.builder.owns_automated_burner.return_value = False
        supervisor.factory = Mock()
        supervisor.factory.next_action.return_value = {"type": "research", "technology": "electric-mining-drill"}
        supervisor.fluids = Mock()
        supervisor.fluids.maintain_coproducts.return_value = None
        supervisor.energy = Mock(state={})
        supervisor.energy.next_action.return_value = None
        supervisor.armaments = Mock()
        supervisor.defense = Mock()
        supervisor.defense.requirements.return_value = {"research": ["gun-turret"]}
        supervisor.defense.next_action.return_value = {"status": "succeeded", "evidence": {"urgent": True}}
        return supervisor

    def test_cold_energy_recovery_loads_before_power_verification_with_fluids_attached(self):
        for saved_controller in (False, True):
            with self.subTest(saved_controller=saved_controller), TemporaryDirectory() as root:
                supervisor = self.production_supervisor(root)
                supervisor.catalog = fake_catalog()
                supervisor.builder.state = {} if saved_controller else {"power_verified_once": True}
                if saved_controller:
                    (Path(root) / "energy-expansion.json").write_text(json.dumps({
                        "schema_version": 1, "world_id": "fixture", "last_tick": 200}), encoding="utf-8")
                factory, fluids = supervisor.factory, supervisor.fluids
                supervisor.factory = supervisor.fluids = supervisor.energy = None
                automated = {"name": "burner-mining-drill", "position": {"x": 5, "y": 5}}
                factory.owns_automated_burner.side_effect = lambda e: e is automated
                obs = observation(100, entities=[automated])
                repair = {"type": "build", "name": "transport-belt", "position": {"x": 5, "y": 6}}

                def recover(controller, current):
                    self.assertIs(controller.factory.fluids, fluids)
                    self.assertIs(fluids.factory, controller.factory)
                    self.assertEqual(current, obs)
                    return repair

                with patch("factorio_ai.deterministic_factory.DeterministicFactory", return_value=factory), \
                     patch("factorio_ai.deterministic_fluids.FluidProduction", return_value=fluids), \
                     patch("factorio_ai.deterministic_defense.DeterministicDefense"), \
                     patch("factorio_ai.deterministic_armaments.Armaments"), \
                     patch("factorio_ai.deterministic_energy.EnergyExpansion.next_action", autospec=True, side_effect=recover) as energy:
                    self.assertEqual(supervisor.next_action(obs, "rocket"), repair)
                energy.assert_called_once_with(supervisor.energy, obs)
                self.assertEqual(supervisor.stage, "energy")
                self.assertEqual(supervisor.bootstrap.next_action.call_args.args[0]["entities"], [])
                supervisor.builder.ensure_power.assert_not_called()
                factory.next_action.assert_not_called()

    def test_energy_report_preempts_starter_wait_but_healthy_energy_yields(self):
        with TemporaryDirectory() as root:
            supervisor = self.production_supervisor(root)
            supervisor.builder.state = {"power_verified_once": True}
            waiting = {"status": "waiting", "reason": "waiting for repaired coal feed"}
            supervisor.energy.next_action.return_value = waiting
            self.assertEqual(supervisor.next_action(observation(), "rocket"), waiting)
            supervisor.builder.ensure_power.assert_not_called()
            supervisor.energy.next_action.return_value = None
            supervisor.armaments.next_action.return_value = None
            self.assertEqual(supervisor.next_action(observation(), "rocket")["type"], "research")
            supervisor.builder.ensure_power.assert_called_once()
            self.assertEqual(supervisor.stage, "production")

    def test_initial_power_sample_does_not_start_expansion_before_verified_flow(self):
        with TemporaryDirectory() as root:
            supervisor = self.production_supervisor(root)
            waiting = {"status": "waiting", "reason": "verifying power supply over 30 game seconds"}
            supervisor.builder.ensure_power.return_value = waiting
            self.assertEqual(supervisor.next_action(observation(), "rocket"), waiting)
            supervisor.energy.next_action.assert_not_called()

    def test_saved_energy_from_another_world_cannot_claim_current_power_capability(self):
        with TemporaryDirectory() as root:
            supervisor = self.production_supervisor(root)
            supervisor.builder.state = {}
            supervisor.energy.state = {"world_id": "old-world"}
            (Path(root) / "energy-expansion.json").write_text(json.dumps({
                "schema_version": 1, "world_id": "old-world"}), encoding="utf-8")
            waiting = {"status": "waiting", "reason": "constructing current world power"}
            supervisor.builder.ensure_power.return_value = waiting
            self.assertEqual(supervisor.next_action(observation(), "rocket"), waiting)
            supervisor.energy.next_action.assert_not_called()

    def test_power_milestone_stops_before_factory_energy_expansion(self):
        with TemporaryDirectory() as root:
            supervisor = self.production_supervisor(root)
            supervisor.builder.state = {"power_verified_once": True}
            result = supervisor.next_action(observation(), "power")
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(supervisor.stage, "power")
            supervisor.energy.next_action.assert_not_called()
            supervisor.factory.next_action.assert_not_called()

    def test_ammunition_research_wait_and_local_success_leave_factory_scheduler_running(self):
        with TemporaryDirectory() as root:
            supervisor = self.production_supervisor(root)
            for result in ({"status": "waiting", "evidence": {"technology": "electric-mining-drill"}},
                           {"status": "succeeded", "reason": "turret supply observed"}):
                supervisor.armaments.next_action.return_value = result
                self.assertEqual(supervisor.next_action(observation(), "rocket")["type"], "research")
                self.assertEqual(supervisor.factory.priority_research, ["gun-turret"])
                self.assertEqual(supervisor.stage, "production")

    def test_bounded_ammunition_action_is_never_discarded_for_another_defense_action(self):
        with TemporaryDirectory() as root:
            supervisor = self.production_supervisor(root)
            action = {"type": "insert", "item": "firearm-magazine", "count": 10}
            supervisor.armaments.next_action.return_value = action
            self.assertEqual(supervisor.next_action(observation(), "rocket"), action)
            self.assertEqual(supervisor.stage, "armaments")
            supervisor.defense.next_action.assert_not_called()
            supervisor.factory.next_action.assert_not_called()

    def test_failed_enemy_survey_blocks_expansion_instead_of_silently_losing_defense(self):
        with TemporaryDirectory() as root:
            supervisor = self.production_supervisor(root)
            supervisor.armaments.next_action.return_value = None
            failure = {"status": "blocked", "reason": "enemy observation failed"}
            supervisor.defense.next_action.return_value = failure
            self.assertEqual(supervisor.next_action(observation(), "rocket"), failure)
            supervisor.factory.next_action.assert_not_called()

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
