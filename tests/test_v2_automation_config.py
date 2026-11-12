import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factorio_agent import automation_config as module, runtime
from factorio_agent.automation_config import load_automation_config


class AutomationConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "automation_config.json"

    def load(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")
        return load_automation_config(self.path)

    def test_missing_defaults_and_bom_percent_mapping(self):
        options = load_automation_config(self.path)
        self.assertEqual(
            options.to_dict(),
            dict(
                world=dict(starting_area_percent=200, nauvis_cliffs=False, seed=None),
                controller=dict(decision_interval_seconds=60, execution_slice_seconds=25, planner="luna"),
            ),
        )
        self.assertEqual(self.load({"controller": {"planner": "none"}}).controller.planner, "none")
        self.assertIsNone(options.source_sha256)
        self.path.write_text("\ufeff" + json.dumps({"world": {"starting_area_percent": 600}}), encoding="utf-8")
        options = load_automation_config(self.path)
        self.assertEqual(
            options.map_gen_settings(7),
            dict(seed=7, starting_area=6.0, cliff_settings=dict(cliff_elevation_interval=0, richness=0)),
        )
        self.assertEqual(len(options.source_sha256), 64)
        self.assertEqual(options.source_path, self.path)

    def test_configurable_boundaries_and_native_cliffs_default(self):
        for percent in (0, 100, 200, 600):
            options = self.load(
                {
                    "world": {"starting_area_percent": percent, "nauvis_cliffs": True, "seed": 2**32 - 1},
                    "controller": {"decision_interval_seconds": 10, "execution_slice_seconds": 50},
                }
            )
            self.assertEqual(options.map_gen_settings(0), dict(seed=0, starting_area=percent / 100))
        self.load({"controller": {"decision_interval_seconds": 3600, "execution_slice_seconds": 1}})

    def test_unknown_duplicate_nonfinite_and_mistyped_values_fail_closed(self):
        # Build the invalid scalar cases separately to retain exact JSON types.
        cases = [
            [],
            None,
            {"model": "gpt-6-luna"},
            {"world": None},
            {"controller": []},
            {"world": {"enemy_expansion": False}},
            {"controller": {"model": "other"}},
        ]
        cases += [
            {"world": {"starting_area_percent": v}}
            for v in (-1, 601, True, "600", None, float("nan"), float("inf"), 10**400)
        ]
        cases += [{"world": {"seed": v}} for v in (-1, 2**32, True, 1.5, "7")]
        cases += [{"world": {"nauvis_cliffs": v}} for v in (0, None, "false")]
        cases += [
            {"controller": {key: v}}
            for key, values in (
                ("decision_interval_seconds", (9, 3601, True, None, "60", float("nan"))),
                ("execution_slice_seconds", (0, 51, False, "25", float("inf"))),
                ("planner", ("other", None, True, "", "Luna")),
            )
            for v in values
        ]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.load(value)
        for text in ('{"world":{},"world":{}}', '{"world":{"seed":1,"seed":2}}', "{broken"):
            self.path.write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError):
                load_automation_config(self.path)

    def test_source_root_path_independent_of_cwd_and_supports_index_snapshot(self):
        source = self.root / "snapshot/src/factorio_agent/automation_config.py"
        source.parent.mkdir(parents=True)
        shutil.copyfile(module.__file__, source)
        config = self.root / "snapshot/automation_config.json"
        config.write_text('{"world":{"starting_area_percent":300}}', encoding="utf-8")
        self.path.write_text("{invalid cwd config", encoding="utf-8")
        name = "_automation_config_snapshot_test"
        spec = importlib.util.spec_from_file_location(name, source)
        loaded = importlib.util.module_from_spec(spec)
        previous = os.getcwd()
        try:
            sys.modules[name] = loaded
            os.chdir(self.root)
            spec.loader.exec_module(loaded)
            options = loaded.load_automation_config()
        finally:
            os.chdir(previous)
            sys.modules.pop(name, None)
        self.assertEqual(options.source_path, config)
        self.assertEqual(options.map_gen_settings(1)["starting_area"], 3.0)

    def test_runtime_create_pins_exact_future_map_and_explicit_cli_seed_wins(self):
        options = self.load({"world": {"seed": 99}})
        target = self.root / "new-runtime"
        cfg = SimpleNamespace(runtime_dir=target, log_dir=target / "logs")
        settings = target / "vanilla/safe-start-map-gen-settings.json"
        command = ["fake-factorio", "--map-gen-settings", str(settings)]
        with (
            patch.object(runtime, "configuration", return_value=cfg),
            patch.object(runtime, "build_create_no_mod_save_command", return_value=command),
            patch.object(runtime.subprocess, "run") as execute,
        ):
            result = runtime.create(target, seed=7, server_port=34262, rcon_port=27062, automation_config=options)
        self.assertEqual(result["seed"], 7)
        execute.assert_called_once()
        actual = json.loads(settings.read_text())
        self.assertEqual(actual["seed"], 7)
        self.assertEqual(actual["starting_area"], 2.0)
        self.assertEqual(set(actual), {"seed", "starting_area", "cliff_settings"})
        snapshot = json.loads((target / "world-generation-config.json").read_text())
        self.assertEqual(snapshot["map_gen_settings"], actual)
        self.assertEqual(snapshot["requested_config"]["world"]["seed"], 99)
        self.assertEqual(snapshot["effective_world"]["seed"], 7)
        self.assertEqual(snapshot["source_sha256"], options.source_sha256)
        self.assertEqual(snapshot["config_fingerprint"], options.fingerprint)
        before = (target / "world-generation-config.json").read_bytes()
        with patch.object(runtime.subprocess, "run") as again:
            with self.assertRaises(FileExistsError):
                runtime.create(target, seed=8, server_port=34262, rcon_port=27062, automation_config=options)
        self.assertEqual((target / "world-generation-config.json").read_bytes(), before)
        again.assert_not_called()

    def test_runtime_create_loads_source_config_before_creating_any_runtime(self):
        self.path.write_text('{"world":{"seed":true}}', encoding="utf-8")
        target = self.root / "uncreated"
        with patch.object(module, "DEFAULT_PATH", self.path), patch.object(runtime.subprocess, "run") as execute:
            with self.assertRaises(ValueError):
                runtime.create(target, seed=7, server_port=34262, rcon_port=27062)
        self.assertFalse(target.exists())
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
