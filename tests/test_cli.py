import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factorio_ai import cli as cli_module
from factorio_ai.cli import _config_with_port_overrides, _observer_player_control_problem
from factorio_ai.config import AppConfig


def make_config() -> AppConfig:
    return AppConfig(
        factorio_exe=Path("factorio.exe"),
        runtime_dir=Path("runtime"),
        mod_runtime_dir=Path("runtime/mods"),
        save_path=Path("runtime/saves/test.zip"),
        rcon_host="127.0.0.1",
        rcon_port=27015,
        rcon_password="factorio-ai",
        server_port=34197,
        log_dir=Path("logs"),
        agent_player_name="AI",
        slurm_enabled=False,
    )


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

    def test_main_does_not_shadow_module_imports_as_locals(self):
        # A `from . import X` inside main() turns the module-level X into an unassigned local for the
        # whole function, so any handler running before that import line raises UnboundLocalError.
        # (This regressed remote_slurm and broke every scheduler command.) Guard the shared imports.
        locals_in_main = cli_module.main.__code__.co_varnames
        for shared in ("remote_slurm",):
            self.assertNotIn(
                shared,
                locals_in_main,
                msg=f"'{shared}' must stay the module-level import, not a local inside main()",
            )

    def test_no_mod_server_port_overrides_are_applied_without_mutating_config(self):
        cfg = make_config()

        updated = _config_with_port_overrides(cfg, SimpleNamespace(server_port=34200, rcon_port=27016))

        self.assertEqual(cfg.server_port, 34197)
        self.assertEqual(cfg.rcon_port, 27015)
        self.assertEqual(updated.server_port, 34200)
        self.assertEqual(updated.rcon_port, 27016)

    def test_invalid_port_override_is_rejected(self):
        with self.assertRaises(SystemExit):
            _config_with_port_overrides(make_config(), SimpleNamespace(server_port=70000, rcon_port=None))


if __name__ == "__main__":
    unittest.main()
