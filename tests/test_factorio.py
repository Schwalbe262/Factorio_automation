import json
import tempfile
import unittest
from pathlib import Path

from factorio_ai.config import AppConfig
from factorio_ai.factorio import (
    build_create_no_mod_save_command,
    build_start_no_mod_server_command,
    no_mod_save_path,
    write_no_mod_map_gen_settings,
    write_no_mod_server_settings,
    write_server_settings,
)


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


class FactorioProcessConfigTests(unittest.TestCase):
    def test_development_server_is_single_review_client_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_server_settings(test_config(Path(temp_dir)))
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["max_players"], 1)
        self.assertFalse(payload["visibility"]["public"])
        self.assertFalse(payload["visibility"]["lan"])

    def test_no_mod_server_is_lan_visible_without_custom_mod(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = test_config(Path(temp_dir))
            path = write_no_mod_server_settings(cfg)
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["max_players"], 8)
        self.assertFalse(payload["visibility"]["public"])
        self.assertTrue(payload["visibility"]["lan"])
        self.assertEqual(payload["allow_commands"], "admins-only")

    def test_no_mod_commands_use_isolated_official_mod_list(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = test_config(Path(temp_dir))
            save_path = no_mod_save_path(cfg)
            create_command = build_create_no_mod_save_command(cfg, save_path)
            server_settings = write_no_mod_server_settings(cfg)
            start_command = build_start_no_mod_server_command(cfg, save_path=save_path, server_settings=server_settings)
            joined = " ".join(create_command + start_command)
            mod_list = cfg.runtime_dir / "vanilla" / "mods" / "mod-list.json"
            mod_list_text = mod_list.read_text(encoding="utf-8")
            map_gen_path = cfg.runtime_dir / "vanilla" / "safe-start-map-gen-settings.json"
            map_gen = json.loads(map_gen_path.read_text(encoding="utf-8"))
        self.assertIn("--create", create_command)
        self.assertIn("--map-gen-settings", create_command)
        self.assertIn(str(map_gen_path), create_command)
        self.assertEqual(map_gen["starting_area"], 4)
        self.assertFalse(map_gen["peaceful_mode"])
        self.assertGreater(map_gen["autoplace_controls"]["enemy-base"]["frequency"], 0)
        self.assertIn("--start-server", start_command)
        self.assertIn("--config", create_command)
        self.assertIn("--config", start_command)
        self.assertIn(str(cfg.runtime_dir / "vanilla" / "mods"), joined)
        self.assertIn(str(cfg.runtime_dir / "vanilla" / "server-config.ini"), joined)
        self.assertIn('"name": "space-age"', mod_list_text)
        self.assertNotIn("factorio_ai_autoplayer", joined)
        self.assertNotIn("factorio_ai_autoplayer", mod_list_text)

    def test_no_mod_map_gen_settings_keep_enemies_but_expand_safe_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_no_mod_map_gen_settings(test_config(Path(temp_dir)))
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["starting_area"], 4)
        self.assertFalse(payload["peaceful_mode"])
        self.assertEqual(payload["starting_points"], [{"x": 0, "y": 0}])
        self.assertGreater(payload["autoplace_controls"]["enemy-base"]["size"], 0)


if __name__ == "__main__":
    unittest.main()
