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
