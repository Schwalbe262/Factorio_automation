import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factorio_ai import cli as cli_module
from factorio_ai.cli import _observer_player_control_problem


class CliTests(unittest.TestCase):
    def test_no_mod_autopilot_blocks_auto_observer_control_without_explicit_override(self):
        cfg = SimpleNamespace(agent_player_name="auto")

        with patch.dict(
            "os.environ",
            {
                "FACTORIO_AI_REQUIRE_REAL_PLAYER": "1",
                "FACTORIO_AI_USE_GUI_INPUT_FOR_MOVEMENT": "1",
            },
            clear=True,
        ):
            problem = _observer_player_control_problem(cfg)

        self.assertIn("refusing to control", problem)
        self.assertIn("FACTORIO_AI_ALLOW_OBSERVER_CONTROL", problem)

    def test_no_mod_autopilot_allows_virtual_ai_agent(self):
        cfg = SimpleNamespace(agent_player_name="AI")

        with patch.dict("os.environ", {"FACTORIO_AI_REQUIRE_REAL_PLAYER": "1"}, clear=True):
            problem = _observer_player_control_problem(cfg)

        self.assertEqual(problem, "")

    def test_no_mod_autopilot_allows_auto_only_with_explicit_override(self):
        cfg = SimpleNamespace(agent_player_name="auto")

        with patch.dict(
            "os.environ",
            {
                "FACTORIO_AI_REQUIRE_REAL_PLAYER": "1",
                "FACTORIO_AI_ALLOW_OBSERVER_CONTROL": "1",
            },
            clear=True,
        ):
            problem = _observer_player_control_problem(cfg)

        self.assertEqual(problem, "")

    def test_no_mod_logistics_research_command_uses_modless_controller(self):
        cfg = SimpleNamespace(log_dir=Path("logs"), agent_player_name="AI")
        summary = SimpleNamespace(
            ok=True,
            reason="logistics research completed",
            steps=12,
            item_count=0,
            log_path=Path("logs/logistics-research.jsonl"),
        )

        with (
            patch("factorio_ai.cli.load_config", return_value=cfg),
            patch("factorio_ai.cli.ModlessFactorioController") as controller_class,
        ):
            controller_class.return_value.run_logistics_research_mvp.return_value = summary
            cli_module.main(["run-no-mod-logistics-research-mvp", "--max-steps", "77"])

        controller_class.return_value.run_logistics_research_mvp.assert_called_once_with(max_steps=77)

    def test_no_mod_build_item_mall_command_uses_modless_controller(self):
        cfg = SimpleNamespace(log_dir=Path("logs"), agent_player_name="AI")
        summary = SimpleNamespace(
            ok=True,
            reason="transport-belt mall completed",
            steps=12,
            item_name="transport-belt",
            item_count=20,
            log_path=Path("logs/build-item-mall-transport-belt.jsonl"),
        )

        with (
            patch("factorio_ai.cli.load_config", return_value=cfg),
            patch("factorio_ai.cli.ModlessFactorioController") as controller_class,
        ):
            controller_class.return_value.run_build_item_mall_mvp.return_value = summary
            cli_module.main(
                [
                    "run-no-mod-build-item-mall-mvp",
                    "--item",
                    "transport-belt",
                    "--target",
                    "20",
                    "--max-steps",
                    "88",
                ]
            )

        controller_class.return_value.run_build_item_mall_mvp.assert_called_once_with(
            target_item="transport-belt",
            target=20,
            max_steps=88,
        )


if __name__ == "__main__":
    unittest.main()
