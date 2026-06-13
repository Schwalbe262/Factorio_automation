import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from factorio_ai.config import AppConfig
from factorio_ai.controller import FactorioController, RunSummary, StrategyStepSummary
from factorio_ai.llm_log import llm_decision_log_path
from factorio_ai.models import PlannerDecision


def test_config(root: Path) -> AppConfig:
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
            cfg = replace(test_config(Path(temp_dir)), slurm_enabled=True)
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
            cfg = replace(test_config(Path(temp_dir)), slurm_enabled=True)
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
            cfg = replace(test_config(Path(temp_dir)), slurm_enabled=True)
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

    def test_strategy_runner_maps_implemented_material_skills(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FactorioController(test_config(Path(temp_dir)))
            config = controller._skill_run_config("produce_electronic_circuit", target_count=7, max_steps=123)
            self.assertIsNotNone(config)
            self.assertEqual(config["target_item"], "electronic-circuit")
            self.assertEqual(config["target"], 7)
            self.assertEqual(config["max_steps"], 123)

    def test_strategy_runner_does_not_fake_automation_skill(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FactorioController(test_config(Path(temp_dir)))
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
            controller = FakeController(test_config(Path(temp_dir)))
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
            controller = FakeController(test_config(Path(temp_dir)))
            summary = controller.run_autopilot_loop(cycles=1, sleep_seconds=0)
        self.assertFalse(summary.ok)
        self.assertEqual(summary.failures, 1)


if __name__ == "__main__":
    unittest.main()
