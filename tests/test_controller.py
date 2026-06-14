import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from factorio_ai.config import AppConfig
from factorio_ai.controller import FactorioController, ModlessFactorioController, RunSummary, StrategyStepSummary
from factorio_ai.llm_log import llm_decision_log_path
from factorio_ai.models import PlannerDecision


def make_test_config(root: Path) -> AppConfig:
    return AppConfig(
        factorio_exe=Path("factorio.exe"),
        runtime_dir=root / "runtime",
        mod_runtime_dir=root / "runtime" / "mods",
        save_path=root / "runtime" / "saves" / "test.zip",
        rcon_host="127.0.0.1",
        rcon_port=27015,
        rcon_password="factorio-ai",
        server_port=34197,
        log_dir=root / "logs",
        agent_player_name="AI",
        slurm_enabled=False,
    )


class ControllerTests(unittest.TestCase):
    def test_required_remote_llm_pending_does_not_submit_strategy_task(self):
        class FakeController(FactorioController):
            def observe(self):
                return {"ok": True, "tick": 1, "inventory": {}, "entities": [], "enemies": [], "research": {}}

        pending_status = {
            "ok": True,
            "llm_ready": False,
            "missing": ["Slurm worker job pending GPU allocation", "GPU allocation"],
            "remote": {
                "pending_jobs": [
                    {
                        "id": "677406",
                        "state": "PENDING",
                        "reason": "(Priority)",
                        "start_time": "2026-06-19T11:30:00",
                    }
                ]
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = FakeController(cfg)
            with (
                patch("factorio_ai.remote_slurm.llm_status", return_value=pending_status) as status,
                patch("factorio_ai.remote_slurm.request_strategy") as request_strategy,
            ):
                with self.assertRaisesRegex(RuntimeError, "remote Slurm LLM not ready"):
                    controller.strategy_decision("launch_rocket_program", require_llm=True)

            log_path = llm_decision_log_path(cfg.log_dir)
            rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        status.assert_called_once()
        request_strategy.assert_not_called()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "remote_slurm")
        self.assertIn("Slurm worker job pending GPU allocation", rows[0]["error"])
        self.assertIn("677406", rows[0]["error"])

    def test_remote_action_hint_skips_queue_when_llm_pending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = FactorioController(cfg)
            decision = PlannerDecision(
                action={"type": "wait", "ticks": 60},
                reason="test",
                done=False,
            )
            with (
                patch(
                    "factorio_ai.remote_slurm.llm_status",
                    return_value={"ok": True, "llm_ready": False, "missing": ["GPU allocation"], "remote": {}},
                ) as status,
                patch("factorio_ai.remote_slurm.request_plan") as request_plan,
            ):
                action = controller._maybe_apply_remote_hint({}, decision, "produce_iron_plate")

        status.assert_called_once()
        request_plan.assert_not_called()
        self.assertEqual(action, {"type": "wait", "ticks": 60})

    def test_background_layout_work_submits_simulation_task_during_active_skill(self):
        observation = {
            "tick": 1,
            "inventory": {},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 10,
                    "recipe": "electronic-circuit",
                    "position": {"x": 0, "y": 0},
                    "electric_network_connected": True,
                    "inventories": {},
                }
            ],
            "resources": [],
            "research": {"technologies": {}},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = FactorioController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS": "0",
                        "FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "queue",
                    },
                ),
                patch("factorio_ai.remote_slurm.submit_task", return_value="layout-task.json") as submit_task,
            ):
                controller._maybe_progress_background_layout_work(
                    observation,
                    "launch_rocket_program",
                    "bootstrap_build_item_mall",
                    4,
                )
            log_path = cfg.log_dir / "layout-improvement-background.jsonl"
            log_text = log_path.read_text(encoding="utf-8")

        submit_task.assert_called_once()
        submitted = submit_task.call_args.args[0]
        self.assertEqual(submitted["type"], "layout_improvement_request")
        self.assertEqual(submitted["payload"]["active_skill"], "bootstrap_build_item_mall")
        self.assertIn("layout_task_submitted", log_text)

    def test_blocked_strategy_submits_background_layout_work(self):
        observation = {
            "tick": 1,
            "inventory": {},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 10,
                    "recipe": "electronic-circuit",
                    "position": {"x": 0, "y": 0},
                    "electric_network_connected": True,
                    "inventories": {},
                }
            ],
            "resources": [],
            "research": {"technologies": {}},
        }

        class FakeController(FactorioController):
            def observe(self):
                return observation

            def strategy_decision(self, objective, require_llm=False):
                return {
                    "selected_skill": "future_build_item_skill",
                    "reason": "needs missing build item executor",
                    "skill_status": {
                        "name": "future_build_item_skill",
                        "implemented": False,
                        "executor": None,
                        "codex_required": True,
                    },
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = FakeController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS": "0",
                        "FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "queue",
                    },
                ),
                patch("factorio_ai.remote_slurm.submit_task", return_value="layout-blocked.json") as submit_task,
            ):
                summary = controller.run_strategy_step("launch_rocket_program")

            log_path = cfg.log_dir / "layout-improvement-background.jsonl"
            rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            wait_state = json.loads((cfg.runtime_dir / "codex-wait.json").read_text(encoding="utf-8"))

        self.assertFalse(summary.ok)
        submit_task.assert_called_once()
        submitted = submit_task.call_args.args[0]
        self.assertEqual(submitted["type"], "layout_improvement_request")
        self.assertEqual(submitted["payload"]["active_skill"], "codex_wait:future_build_item_skill")
        self.assertEqual(rows[0]["event"], "layout_blocked_strategy_detected")
        self.assertEqual(rows[-1]["event"], "layout_task_submitted")
        self.assertTrue(wait_state["active"])
        self.assertEqual(wait_state["selected_skill"], "future_build_item_skill")
        self.assertEqual(wait_state["active_skill"], "codex_wait:future_build_item_skill")

    def test_blocked_no_mod_strategy_can_autostart_codex_wait_layout_loop(self):
        observation = {
            "tick": 1,
            "inventory": {},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 11,
                    "recipe": "transport-belt",
                    "position": {"x": 1, "y": 1},
                    "electric_network_connected": True,
                    "inventories": {},
                }
            ],
            "resources": [],
            "research": {"technologies": {}},
        }

        class DummyProcess:
            pid = 4321

        class FakeController(ModlessFactorioController):
            def observe(self):
                return observation

            def strategy_decision(self, objective, require_llm=False):
                return {
                    "selected_skill": "future_build_item_skill",
                    "reason": "needs missing build item executor",
                    "skill_status": {
                        "name": "future_build_item_skill",
                        "implemented": False,
                        "executor": None,
                        "codex_required": True,
                    },
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = FakeController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_CODEX_WAIT_LAYOUT_AUTOSTART": "1",
                        "FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS": "0",
                        "FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "queue",
                    },
                ),
                patch("factorio_ai.controller.subprocess.Popen", return_value=DummyProcess()) as popen,
                patch("factorio_ai.remote_slurm.submit_task", return_value="layout-blocked.json"),
            ):
                summary = controller.run_strategy_step("launch_rocket_program")

            process_state = json.loads((cfg.runtime_dir / "codex-wait-layout-loop.json").read_text(encoding="utf-8"))
            rows = [
                json.loads(line)
                for line in (cfg.log_dir / "layout-improvement-background.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertFalse(summary.ok)
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertIn("run-no-mod-codex-wait-layout-loop", command)
        self.assertEqual(process_state["pid"], 4321)
        self.assertIn("run-no-mod-codex-wait-layout-loop", process_state["command"])
        self.assertTrue(any(row["event"] == "layout_codex_wait_loop_started" for row in rows))

    def test_manual_codex_work_start_autostarts_no_mod_layout_loop(self):
        class DummyProcess:
            pid = 9876

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = ModlessFactorioController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_CODEX_WAIT_LAYOUT_AUTOSTART": "1",
                        "FACTORIO_AI_CODEX_WAIT_LAYOUT_SLEEP_SECONDS": "0",
                    },
                ),
                patch("factorio_ai.controller.subprocess.Popen", return_value=DummyProcess()) as popen,
            ):
                summary = controller.begin_codex_work(
                    "launch_rocket_program",
                    "future_build_item_skill",
                    "Codex is implementing the missing build item executor.",
                )

            wait_state = json.loads((cfg.runtime_dir / "codex-wait.json").read_text(encoding="utf-8"))
            process_state = json.loads((cfg.runtime_dir / "codex-wait-layout-loop.json").read_text(encoding="utf-8"))
            rows = [
                json.loads(line)
                for line in (cfg.log_dir / "layout-improvement-background.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertIn("run-no-mod-codex-wait-layout-loop", command)
        self.assertTrue(summary["waitState"]["active"])
        self.assertEqual(summary["layoutLoop"]["pid"], 9876)
        self.assertTrue(wait_state["active"])
        self.assertEqual(wait_state["selected_skill"], "future_build_item_skill")
        self.assertEqual(process_state["pid"], 9876)
        self.assertTrue(any(row["event"] == "layout_codex_wait_loop_started" for row in rows))

    def test_manual_codex_work_finish_clears_wait_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_test_config(Path(temp_dir))
            controller = FactorioController(cfg)
            controller.begin_codex_work(
                "launch_rocket_program",
                "future_build_item_skill",
                "Codex is implementing the missing build item executor.",
            )
            summary = controller.finish_codex_work(
                "future_build_item_skill",
                clear_reason="new build item executor committed",
            )
            wait_state = json.loads((cfg.runtime_dir / "codex-wait.json").read_text(encoding="utf-8"))

        self.assertTrue(summary["ok"])
        self.assertFalse(summary["waitState"]["active"])
        self.assertFalse(wait_state["active"])
        self.assertEqual(wait_state["clear_reason"], "new build item executor committed")

    def test_no_mod_strategy_step_strict_real_player_rejects_virtual_agent(self):
        class FakeController(ModlessFactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "tick": 1,
                    "player": {"name": "server", "kind": "server", "character_valid": False},
                    "execution": {"mode": "virtual", "virtual": True, "character_valid": False},
                    "inventory": {},
                    "entities": [],
                    "resources": [],
                    "enemies": [],
                    "research": {"technologies": {}},
                }

            def strategy_decision(self, *args, **kwargs):
                raise AssertionError("strict execution guard should run before LLM strategy")

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            with patch.dict("os.environ", {"FACTORIO_AI_REQUIRE_REAL_PLAYER": "1"}):
                summary = controller.run_strategy_step("launch_rocket_program", require_llm=True)

        self.assertFalse(summary.ok)
        self.assertEqual(summary.strategy["source"], "execution_guard")
        self.assertIn("virtual server agent", summary.reason)

    def test_no_mod_action_strict_real_player_rejects_virtual_agent(self):
        class FakeController(ModlessFactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "tick": 1,
                    "player": {"name": "server", "kind": "server", "character_valid": False},
                    "execution": {"mode": "virtual", "virtual": True, "character_valid": False},
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            with patch.dict("os.environ", {"FACTORIO_AI_REQUIRE_REAL_PLAYER": "1"}):
                response = controller.act({"type": "wait", "ticks": 1})

        self.assertFalse(response["ok"])
        self.assertIn("virtual server agent", response["reason"])

    def test_no_mod_action_strict_real_player_rejects_dead_character(self):
        class FakeController(ModlessFactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "tick": 1,
                    "player": {"name": "r1jae", "kind": "player", "character_valid": False},
                    "execution": {"mode": "player", "virtual": False, "character_valid": False},
                    "enemies": [],
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            with patch.dict("os.environ", {"FACTORIO_AI_REQUIRE_REAL_PLAYER": "1"}):
                response = controller.act({"type": "wait", "ticks": 1})

        self.assertFalse(response["ok"])
        self.assertIn("has no valid character", response["reason"])

    def test_no_mod_action_strict_real_player_rejects_remote_controller(self):
        class FakeModless:
            def act(self, action, player_name=None):
                return {"ok": False, "reason": "restore failed in test"}

        class FakeController(ModlessFactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "tick": 1,
                    "player": {
                        "name": "r1jae",
                        "kind": "player",
                        "character_valid": True,
                        "controller_is_character": False,
                    },
                    "execution": {
                        "mode": "player",
                        "virtual": False,
                        "character_valid": True,
                        "controller_is_character": False,
                    },
                    "enemies": [],
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            controller._modless = FakeModless()
            with patch.dict("os.environ", {"FACTORIO_AI_REQUIRE_REAL_PLAYER": "1"}):
                response = controller.act({"type": "wait", "ticks": 1})

        self.assertFalse(response["ok"])
        self.assertIn("not in character controller mode", response["reason"])
        self.assertIn("restore_character_controller failed", response["reason"])

    def test_no_mod_action_strict_real_player_restores_remote_controller_before_action(self):
        remote_observation = {
            "ok": True,
            "tick": 1,
            "player": {
                "name": "r1jae",
                "kind": "player",
                "position": {"x": 0.0, "y": 0.0},
                "character_valid": True,
                "controller_is_character": False,
            },
            "execution": {
                "mode": "player",
                "virtual": False,
                "character_valid": True,
                "controller_is_character": False,
            },
            "enemies": [],
        }
        character_observation = {
            "ok": True,
            "tick": 2,
            "player": {
                "name": "r1jae",
                "kind": "player",
                "position": {"x": 0.0, "y": 0.0},
                "character_valid": True,
                "controller_is_character": True,
            },
            "execution": {
                "mode": "player",
                "virtual": False,
                "character_valid": True,
                "controller_is_character": True,
            },
            "enemies": [],
        }

        class FakeModless:
            def __init__(self):
                self.actions = []

            def act(self, action, player_name=None):
                self.actions.append((action, player_name))
                return {"ok": True, "action": action.get("type")}

        class FakeController(ModlessFactorioController):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.observations = [remote_observation, character_observation]

            def observe(self):
                if self.observations:
                    return self.observations.pop(0)
                return character_observation

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            fake_modless = FakeModless()
            controller._modless = fake_modless
            with patch.dict("os.environ", {"FACTORIO_AI_REQUIRE_REAL_PLAYER": "1"}):
                response = controller.act({"type": "wait", "ticks": 1})

        self.assertTrue(response["ok"])
        self.assertEqual(response["action"], "wait")
        self.assertEqual(fake_modless.actions[0][0]["type"], "restore_character_controller")
        self.assertEqual(fake_modless.actions[1][0]["type"], "wait")

    def test_no_mod_action_strict_real_player_pauses_near_enemy(self):
        class FakeController(ModlessFactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "tick": 1,
                    "player": {
                        "name": "r1jae",
                        "kind": "player",
                        "position": {"x": 0.0, "y": 0.0},
                        "character_valid": True,
                        "controller_is_character": True,
                    },
                    "execution": {
                        "mode": "player",
                        "virtual": False,
                        "character_valid": True,
                        "controller_is_character": True,
                    },
                    "enemies": [
                        {
                            "name": "small-biter",
                            "type": "unit",
                            "position": {"x": 12.0, "y": 0.0},
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            with patch.dict("os.environ", {"FACTORIO_AI_REQUIRE_REAL_PLAYER": "1"}):
                response = controller.act({"type": "wait", "ticks": 1})

        self.assertFalse(response["ok"])
        self.assertIn("enemy small-biter", response["reason"])
        self.assertIn("near player", response["reason"])

    def test_no_mod_strategy_step_strict_real_player_stops_before_near_enemy_cycle(self):
        class FakeModless:
            def __init__(self):
                self.actions = []

            def act(self, action, player_name=None):
                self.actions.append((action, player_name))
                return {"ok": True, "action": action.get("type")}

        class FakeController(ModlessFactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "tick": 1,
                    "player": {
                        "name": "r1jae",
                        "kind": "player",
                        "position": {"x": 0.0, "y": 0.0},
                        "character_valid": True,
                        "controller_is_character": True,
                    },
                    "execution": {
                        "mode": "player",
                        "virtual": False,
                        "character_valid": True,
                        "controller_is_character": True,
                    },
                    "enemies": [
                        {
                            "name": "small-biter",
                            "type": "unit",
                            "position": {"x": 12.0, "y": 0.0},
                        }
                    ],
                }

            def strategy_decision(self, *args, **kwargs):
                raise AssertionError("enemy execution guard should run before LLM strategy")

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            fake_modless = FakeModless()
            controller._modless = fake_modless
            with patch.dict("os.environ", {"FACTORIO_AI_REQUIRE_REAL_PLAYER": "1"}):
                summary = controller.run_strategy_step("launch_rocket_program", require_llm=True)

        self.assertFalse(summary.ok)
        self.assertEqual(summary.selected_skill, "build_starter_defense")
        self.assertEqual(summary.strategy["source"], "execution_guard")
        self.assertIn("near player", summary.reason)
        self.assertEqual(fake_modless.actions[0][0]["type"], "stop")

    def test_no_mod_action_strict_real_player_pauses_path_near_enemy(self):
        class FakeController(ModlessFactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "tick": 1,
                    "player": {
                        "name": "r1jae",
                        "kind": "player",
                        "position": {"x": 0.0, "y": 0.0},
                        "character_valid": True,
                        "controller_is_character": True,
                    },
                    "execution": {
                        "mode": "player",
                        "virtual": False,
                        "character_valid": True,
                        "controller_is_character": True,
                    },
                    "enemies": [
                        {
                            "name": "biter-spawner",
                            "type": "unit-spawner",
                            "position": {"x": 40.0, "y": 8.0},
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            with patch.dict("os.environ", {"FACTORIO_AI_REQUIRE_REAL_PLAYER": "1"}):
                response = controller.act({"type": "move_to", "position": {"x": 80.0, "y": 0.0}})

        self.assertFalse(response["ok"])
        self.assertIn("near movement path", response["reason"])

    def test_auto_agent_player_name_uses_first_connected_player(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), agent_player_name="auto")
            controller = ModlessFactorioController(cfg)

        self.assertEqual(controller._agent_parameter(), {})
        self.assertNotIn("player_name", controller._agent_action({"type": "wait", "ticks": 1}))

    def test_wait_for_move_refreshes_direction_until_arrival(self):
        class MovingController(FactorioController):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.positions = [
                    {"x": 0.0, "y": 0.0},
                    {"x": 0.25, "y": 0.0},
                    {"x": 1.0, "y": 0.0},
                ]
                self.actions = []
                self.stopped = False

            def observe(self):
                position = self.positions.pop(0) if self.positions else {"x": 1.0, "y": 0.0}
                return {
                    "player": {
                        "name": "r1jae",
                        "kind": "player",
                        "position": position,
                        "character_valid": True,
                        "move": {"active": True},
                    }
                }

            def act(self, action):
                self.actions.append(action)
                return {"ok": True, "action": action.get("type")}

            def stop_agent(self):
                self.stopped = True
                return {"ok": True, "action": "stop"}

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = MovingController(make_test_config(Path(temp_dir)))
            with patch("factorio_ai.controller.time.sleep", return_value=None):
                arrived, reason = controller._wait_for_move(
                    {"type": "move_to", "position": {"x": 1.0, "y": 0.0}, "tolerance": 0.1}
                )

        self.assertTrue(arrived, reason)
        self.assertTrue(controller.stopped)
        self.assertTrue(any(action["type"] == "move_to" for action in controller.actions))

    def test_no_mod_move_to_can_use_gui_keyboard_executor(self):
        class FakeController(ModlessFactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "player": {
                        "name": "r1jae",
                        "kind": "player",
                        "position": {"x": 0.0, "y": 0.0},
                        "character_valid": True,
                    },
                    "execution": {"mode": "player", "virtual": False, "character_valid": True},
                }

        class FakeDriver:
            def __init__(self, cfg):
                self.cfg = cfg
                self.held = []
                self.clicks = []

            def activate_factorio(self, timeout_seconds=30.0):
                return True

            def click_window_ratio(self, x_ratio, y_ratio):
                self.clicks.append((x_ratio, y_ratio))

            def hold_keys(self, keys, duration_seconds):
                self.held.append((list(keys), duration_seconds))

        fake_driver = FakeDriver(None)
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_REQUIRE_REAL_PLAYER": "1",
                        "FACTORIO_AI_USE_GUI_INPUT_FOR_MOVEMENT": "1",
                    },
                ),
                patch("factorio_ai.vanilla_gui.VanillaGuiDriver", return_value=fake_driver),
            ):
                response = controller.act({"type": "move_to", "position": {"x": 1.0, "y": 0.0}})

        self.assertTrue(response["ok"])
        self.assertEqual(response["mode"], "gui-input")
        self.assertEqual(response["keys"], ["d"])
        self.assertEqual(fake_driver.clicks, [(0.5, 0.5)])
        self.assertEqual(fake_driver.held[0][0], ["d"])

    def test_autopilot_pulses_layout_work_while_codex_wait_state_is_active(self):
        observation = {
            "tick": 2,
            "inventory": {},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 20,
                    "recipe": "transport-belt",
                    "position": {"x": 12, "y": 4},
                    "electric_network_connected": True,
                    "inventories": {},
                }
            ],
            "resources": [],
            "research": {"technologies": {}},
        }

        class FailingController(FactorioController):
            def observe(self):
                return observation

            def run_strategy_step(self, **kwargs):
                raise RuntimeError("strategy endpoint unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            (cfg.runtime_dir / "codex-wait.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "objective": "launch_rocket_program",
                        "selected_skill": "future_build_item_skill",
                        "active_skill": "codex_wait:future_build_item_skill",
                        "reason": "Codex is implementing the missing build item executor.",
                    }
                ),
                encoding="utf-8",
            )
            controller = FailingController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS": "0",
                        "FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "queue",
                    },
                ),
                patch("factorio_ai.remote_slurm.submit_task", return_value="layout-wait.json") as submit_task,
                patch("factorio_ai.remote_slurm.read_task_state", return_value=("running", None, "")),
            ):
                summary = controller.run_autopilot_loop(cycles=1, sleep_seconds=0)

            rows = [
                json.loads(line)
                for line in (cfg.log_dir / "layout-improvement-background.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertFalse(summary.ok)
        submit_task.assert_called_once()
        submitted = submit_task.call_args.args[0]
        self.assertEqual(submitted["payload"]["active_skill"], "codex_wait:future_build_item_skill")
        self.assertTrue(any(row["event"] == "layout_codex_wait_heartbeat" for row in rows))
        self.assertTrue(any(row["event"] == "layout_task_submitted" for row in rows))

    def test_codex_wait_layout_loop_keeps_submitting_until_wait_clears(self):
        observation = {
            "tick": 3,
            "inventory": {},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 30,
                    "recipe": "electronic-circuit",
                    "position": {"x": 20, "y": 4},
                    "electric_network_connected": True,
                    "inventories": {},
                }
            ],
            "resources": [],
            "research": {"technologies": {}},
        }

        class WaitingController(FactorioController):
            def observe(self):
                return observation

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            (cfg.runtime_dir / "codex-wait.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "objective": "launch_rocket_program",
                        "selected_skill": "future_build_item_skill",
                        "active_skill": "codex_wait:future_build_item_skill",
                        "reason": "Codex is implementing the missing build item executor.",
                    }
                ),
                encoding="utf-8",
            )
            controller = WaitingController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS": "0",
                        "FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "queue",
                    },
                ),
                patch(
                    "factorio_ai.remote_slurm.submit_task",
                    side_effect=["layout-wait-1.json", "layout-wait-2.json"],
                ) as submit_task,
                patch("factorio_ai.remote_slurm.read_task_state", return_value=("result", {"ok": True}, "")),
            ):
                summary = controller.run_codex_wait_layout_loop(cycles=2, sleep_seconds=0)

            rows = [
                json.loads(line)
                for line in (cfg.log_dir / "layout-improvement-background.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(summary.ok)
        self.assertEqual(summary.cycles, 2)
        self.assertTrue(summary.wait_active)
        self.assertEqual(submit_task.call_count, 2)
        self.assertTrue(any(row["event"] == "layout_codex_wait_heartbeat" for row in rows))
        self.assertTrue(any(row["event"] == "layout_result" for row in rows))
        self.assertEqual(rows[-1]["event"], "layout_task_submitted")

    def test_idle_layout_loop_submits_when_autopilot_heartbeat_missing(self):
        observation = {
            "tick": 4,
            "inventory": {},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 40,
                    "recipe": "transport-belt",
                    "position": {"x": 10, "y": 4},
                    "electric_network_connected": True,
                    "inventories": {},
                }
            ],
            "resources": [],
            "research": {"technologies": {}},
        }

        class IdleController(FactorioController):
            def observe(self):
                return observation

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = IdleController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "queue",
                        "FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS": "999",
                    },
                ),
                patch("factorio_ai.remote_slurm.submit_task", return_value="layout-idle.json") as submit_task,
                patch("factorio_ai.remote_slurm.read_task_state", return_value=("running", None, "")),
            ):
                summary = controller.run_idle_layout_loop(cycles=1, sleep_seconds=0, min_submit_interval_seconds=0)

            rows = [
                json.loads(line)
                for line in (cfg.log_dir / "layout-improvement-background.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(summary.ok)
        self.assertEqual(summary.idle_cycles, 1)
        self.assertEqual(summary.busy_cycles, 0)
        submit_task.assert_called_once()
        submitted = submit_task.call_args.args[0]
        self.assertIn("idle:autopilot_heartbeat_missing", submitted["payload"]["active_skill"])
        self.assertTrue(any(row["event"] == "layout_idle_scheduler_heartbeat" for row in rows))
        self.assertTrue(any(row["event"] == "layout_task_submitted" for row in rows))

    def test_idle_layout_loop_submits_when_autopilot_heartbeat_is_stale(self):
        observation = {
            "tick": 5,
            "inventory": {},
            "entities": [],
            "resources": [],
            "research": {"technologies": {}},
        }

        class IdleController(FactorioController):
            def observe(self):
                return observation

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            (cfg.runtime_dir / "autopilot-heartbeat.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "state": "cycle_start",
                        "updated_at": (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(),
                        "objective": "launch_rocket_program",
                        "cycle": 3,
                    }
                ),
                encoding="utf-8",
            )
            controller = IdleController(cfg)
            with (
                patch.dict("os.environ", {"FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "queue"}),
                patch("factorio_ai.remote_slurm.submit_task", return_value="layout-stale.json") as submit_task,
                patch("factorio_ai.remote_slurm.read_task_state", return_value=("running", None, "")),
            ):
                summary = controller.run_idle_layout_loop(
                    cycles=1,
                    sleep_seconds=0,
                    stale_seconds=15,
                    min_submit_interval_seconds=0,
                )

        self.assertTrue(summary.ok)
        self.assertEqual(summary.idle_cycles, 1)
        submit_task.assert_called_once()
        submitted = submit_task.call_args.args[0]
        self.assertIn("autopilot_heartbeat_stale", submitted["payload"]["active_skill"])

    def test_idle_layout_loop_skips_when_autopilot_heartbeat_is_fresh_busy(self):
        class BusyController(FactorioController):
            def observe(self):
                raise AssertionError("fresh busy autopilot should not be observed by idle layout loop")

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            (cfg.runtime_dir / "autopilot-heartbeat.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "state": "cycle_start",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "objective": "launch_rocket_program",
                        "cycle": 3,
                    }
                ),
                encoding="utf-8",
            )
            controller = BusyController(cfg)
            with patch("factorio_ai.remote_slurm.submit_task") as submit_task:
                summary = controller.run_idle_layout_loop(cycles=1, sleep_seconds=0, stale_seconds=15)

            rows = [
                json.loads(line)
                for line in (cfg.log_dir / "layout-improvement-background.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(summary.ok)
        self.assertEqual(summary.idle_cycles, 0)
        self.assertEqual(summary.busy_cycles, 1)
        submit_task.assert_not_called()
        self.assertTrue(any(row["event"] == "layout_idle_scheduler_busy" for row in rows))

    def test_autopilot_loop_writes_heartbeat(self):
        class OneStepController(FactorioController):
            def run_strategy_step(self, **kwargs):
                return StrategyStepSummary(
                    ok=True,
                    reason="test cycle complete",
                    objective=kwargs.get("objective", "launch_rocket_program"),
                    selected_skill="produce_iron_plate",
                    strategy={"selected_skill": "produce_iron_plate"},
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_test_config(Path(temp_dir))
            controller = OneStepController(cfg)
            summary = controller.run_autopilot_loop(cycles=1, sleep_seconds=0)
            heartbeat = json.loads((cfg.runtime_dir / "autopilot-heartbeat.json").read_text(encoding="utf-8"))

        self.assertTrue(summary.ok)
        self.assertEqual(heartbeat["state"], "stopped")
        self.assertEqual(heartbeat["cycle"], 1)
        self.assertEqual(heartbeat["objective"], "launch_rocket_program")

    def test_strategy_runner_maps_implemented_material_skills(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FactorioController(make_test_config(Path(temp_dir)))
            config = controller._skill_run_config("produce_electronic_circuit", target_count=7, max_steps=123)
            self.assertIsNotNone(config)
            self.assertEqual(config["target_item"], "electronic-circuit")
            self.assertEqual(config["target"], 7)
            self.assertEqual(config["max_steps"], 123)
            coal_config = controller._skill_run_config("setup_coal_supply", target_count=18, max_steps=456)
            self.assertIsNotNone(coal_config)
            self.assertEqual(coal_config["target_item"], "coal")
            self.assertEqual(coal_config["target"], 18)
            self.assertEqual(coal_config["max_steps"], 456)
            fuel_feed_config = controller._skill_run_config("connect_coal_fuel_feed", max_steps=321)
            self.assertIsNotNone(fuel_feed_config)
            self.assertEqual(fuel_feed_config["target_item"], "coal")
            self.assertEqual(fuel_feed_config["goal"], "connect_coal_fuel_feed")
            self.assertEqual(fuel_feed_config["max_steps"], 321)

    def test_strategy_runner_does_not_fake_automation_skill(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FactorioController(make_test_config(Path(temp_dir)))
            belt_config = controller._skill_run_config("build_belt_smelting_line", target_count=12, max_steps=222)
            self.assertIsNotNone(belt_config)
            self.assertEqual(belt_config["goal"], "build_belt_smelting_line")
            self.assertEqual(belt_config["target"], 12)
            self.assertEqual(belt_config["max_steps"], 222)
            power_config = controller._skill_run_config("setup_power", max_steps=333)
            self.assertIsNotNone(power_config)
            self.assertEqual(power_config["goal"], "setup_power")
            self.assertEqual(power_config["max_steps"], 333)
            research_config = controller._skill_run_config("research_automation", max_steps=444)
            self.assertIsNotNone(research_config)
            self.assertEqual(research_config["goal"], "research_automation")
            self.assertEqual(research_config["max_steps"], 444)
            circuit_line_config = controller._skill_run_config("automate_electronic_circuit_line", target_count=6, max_steps=555)
            self.assertIsNotNone(circuit_line_config)
            self.assertEqual(circuit_line_config["goal"], "automate_electronic_circuit_line")
            self.assertEqual(circuit_line_config["target"], 6)
            self.assertEqual(circuit_line_config["max_steps"], 555)
            logistics_config = controller._skill_run_config("research_logistics", max_steps=666)
            self.assertIsNotNone(logistics_config)
            self.assertEqual(logistics_config["goal"], "research_logistics")
            self.assertEqual(logistics_config["max_steps"], 666)
            expand_config = controller._skill_run_config("expand_iron_smelting", target_count=38, max_steps=777)
            self.assertIsNotNone(expand_config)
            self.assertEqual(expand_config["goal"], "expand_iron_smelting")
            self.assertEqual(expand_config["target"], 38)
            self.assertEqual(expand_config["max_steps"], 777)
            copper_expand_config = controller._skill_run_config("expand_copper_smelting", target_count=56, max_steps=888)
            self.assertIsNotNone(copper_expand_config)
            self.assertEqual(copper_expand_config["goal"], "expand_copper_smelting")
            self.assertEqual(copper_expand_config["target"], 56)
            self.assertEqual(copper_expand_config["max_steps"], 888)
            defense_config = controller._skill_run_config("build_starter_defense", max_steps=999)
            self.assertIsNotNone(defense_config)
            self.assertEqual(defense_config["goal"], "build_starter_defense")
            self.assertEqual(defense_config["target_item"], "gun-turret")
            self.assertEqual(defense_config["max_steps"], 999)
            mall_config = controller._skill_run_config("bootstrap_build_item_mall", target_count=24, max_steps=1111)
            self.assertIsNotNone(mall_config)
            self.assertEqual(mall_config["goal"], "bootstrap_build_item_mall")
            self.assertEqual(mall_config["target_item"], "transport-belt")
            self.assertEqual(mall_config["target"], 24)
            self.assertEqual(mall_config["max_steps"], 1111)
            layout_config = controller._skill_run_config("plan_factory_site", max_steps=2)
            self.assertIsNotNone(layout_config)
            self.assertEqual(layout_config["goal"], "plan_factory_site")
            self.assertEqual(layout_config["target_item"], "layout-plan")
            self.assertEqual(layout_config["max_steps"], 2)
            dry_run_config = controller._skill_run_config("expand_iron_smelting", max_steps=0)
            self.assertIsNotNone(dry_run_config)
            self.assertEqual(dry_run_config["max_steps"], 0)

    def test_strategy_step_summary_serializes_run(self):
        run = RunSummary(
            ok=True,
            reason="done",
            steps=3,
            item_count=5,
            log_path=Path("logs/test.jsonl"),
            item_name="electronic-circuit",
        )
        summary = StrategyStepSummary(
            ok=True,
            reason="done",
            objective="launch_rocket_program",
            selected_skill="produce_electronic_circuit",
            strategy={"selected_skill": "produce_electronic_circuit"},
            run=run,
        )
        payload = summary.to_dict()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["run"]["itemName"], "electronic-circuit")

    def test_autopilot_loop_records_each_cycle(self):
        class FakeController(FactorioController):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.calls = 0

            def run_strategy_step(self, **kwargs):
                self.calls += 1
                return StrategyStepSummary(
                    ok=True,
                    reason="done",
                    objective=kwargs.get("objective", "launch_rocket_program"),
                    selected_skill="produce_iron_plate",
                    strategy={"selected_skill": "produce_iron_plate"},
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            summary = controller.run_autopilot_loop(cycles=2, sleep_seconds=0)
            lines = summary.log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertTrue(summary.ok)
        self.assertEqual(summary.cycles, 2)
        self.assertEqual(controller.calls, 2)
        self.assertEqual(len(lines), 2)

    def test_finite_autopilot_loop_reports_failed_cycle(self):
        class FakeController(FactorioController):
            def run_strategy_step(self, **kwargs):
                return StrategyStepSummary(
                    ok=False,
                    reason="executor missing",
                    objective=kwargs.get("objective", "launch_rocket_program"),
                    selected_skill="missing",
                    strategy={},
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            summary = controller.run_autopilot_loop(cycles=1, sleep_seconds=0)
        self.assertFalse(summary.ok)
        self.assertEqual(summary.failures, 1)


if __name__ == "__main__":
    unittest.main()
