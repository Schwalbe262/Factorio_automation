import tempfile
import unittest
from pathlib import Path

from factorio_ai.config import AppConfig
from factorio_ai.controller import FactorioController, RunSummary, StrategyStepSummary


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
            self.assertIsNone(controller._skill_run_config("automate_electronic_circuit_line"))
            self.assertIsNone(controller._skill_run_config("expand_iron_smelting"))

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


if __name__ == "__main__":
    unittest.main()
