import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factorio_ai import skill_foundry
from factorio_ai.config import AppConfig
from factorio_ai.controller import FactorioController, RunSummary
from factorio_ai.skill_registry import IMPLEMENTED_SKILLS, skill_status


GOOD_OVERRIDE = '''from __future__ import annotations

from factorio_ai.models import PlannerDecision


class SetupPowerOverrideSkill:
    """A repaired setup_power that never loops forever on a blocked tile."""

    def next_action(self, observation):
        return PlannerDecision(None, "steam power ready", done=True)
'''


def _cfg(tmp: Path) -> AppConfig:
    return AppConfig(
        factorio_exe=tmp / "factorio.exe",
        runtime_dir=tmp,
        mod_runtime_dir=tmp / "mods",
        save_path=tmp / "save.zip",
        rcon_host="127.0.0.1",
        rcon_port=27015,
        rcon_password="x",
        server_port=34197,
        log_dir=tmp,
        agent_player_name="AI",
        slurm_enabled=False,
    )


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.gen = self.dir / "gen"
        self.gen.mkdir(parents=True, exist_ok=True)
        self._prev = os.environ.get("FACTORIO_AI_GENERATED_SKILLS_DIR")
        os.environ["FACTORIO_AI_GENERATED_SKILLS_DIR"] = str(self.gen)
        self.cfg = _cfg(self.dir)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("FACTORIO_AI_GENERATED_SKILLS_DIR", None)
        else:
            os.environ["FACTORIO_AI_GENERATED_SKILLS_DIR"] = self._prev
        self._tmp.cleanup()

    def _register_override(self):
        (self.gen / "setup_power.py").write_text(GOOD_OVERRIDE, encoding="utf-8")
        skill_foundry.update_skill(
            "setup_power",
            status="override_registered",
            is_override=True,
            base_skill="setup_power",
            class_name="SetupPowerOverrideSkill",
            file_path=str(self.gen / "setup_power.py"),
        )


class OverrideDevelopTests(_Base):
    def _patch_llm(self, code: str):
        from factorio_ai import slurm_worker

        def fake(system, prompt, schema=None, *, kind="llm", task_id="", max_tokens=None):
            return {"class_name": "SetupPowerOverrideSkill", "code": code}, {"llm_trace": {}}

        return patch.object(slurm_worker, "call_llm_json_with_diagnostics", fake)

    def test_override_registers_override_status_when_sandbox_passes(self):
        spec = {"skill_name": "setup_power", "mode": "override", "reason": "cannot place entity"}
        passing = skill_foundry.GateResult("sandbox_dryrun", True, [], {"steps": 5})
        with self._patch_llm(GOOD_OVERRIDE), patch.object(skill_foundry, "sandbox_dryrun_gate", return_value=passing):
            result = skill_foundry.develop_skill(self.cfg, spec, max_attempts=1)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "override_registered")
        self.assertTrue(result.get("is_override"))
        self.assertIn("sandbox_dryrun", result["gates_passed"])
        self.assertIsNotNone(skill_foundry.registered_override("setup_power"))

    def test_override_rejected_when_sandbox_unavailable(self):
        # Auto-replacing a core skill must be sandbox-proven; a skipped sandbox must NOT register.
        spec = {"skill_name": "setup_power", "mode": "override", "reason": "x"}
        skipped = skill_foundry.GateResult("sandbox_dryrun", True, [], {"skipped": "sandbox off"})
        with self._patch_llm(GOOD_OVERRIDE), patch.object(skill_foundry, "sandbox_dryrun_gate", return_value=skipped):
            result = skill_foundry.develop_skill(self.cfg, spec, max_attempts=1)
        self.assertFalse(result["ok"])
        self.assertIsNone(skill_foundry.registered_override("setup_power"))


class OverridePrecedenceTests(_Base):
    def test_active_override_takes_precedence_over_hand_written(self):
        self._register_override()
        status = skill_status("setup_power")
        self.assertEqual(status.executor, "SetupPowerOverrideSkill")
        self.assertFalse(status.codex_required)

    def test_quarantined_override_falls_back_to_hand_written(self):
        self._register_override()
        skill_foundry.set_skill_status("setup_power", "quarantined", "regressed live")
        status = skill_status("setup_power")
        self.assertEqual(status.executor, IMPLEMENTED_SKILLS["setup_power"])  # SetupPowerSkill
        self.assertIsNone(skill_foundry.registered_override("setup_power"))


class ControllerSelfRepairTests(_Base):
    def test_repeated_failures_enqueue_override_request(self):
        with patch.dict(
            "os.environ",
            {"FACTORIO_AI_SKILL_REPAIR_ENABLED": "1", "FACTORIO_AI_IMPL_REPAIR_FAIL_LIMIT": "2"},
        ):
            controller = FactorioController(self.cfg)
            fail = RunSummary(False, "cannot place entity", 5, 0, self.dir / "x.log", "steam")
            controller._track_implemented_skill_result("launch_rocket_program", "setup_power", fail, {"priority": 80})
            controller._track_implemented_skill_result("launch_rocket_program", "setup_power", fail, {"priority": 80})
        queue = {item["skill_name"]: item for item in skill_foundry.load_foundry_queue(self.dir)}
        self.assertIn("setup_power", queue)
        self.assertEqual(queue["setup_power"].get("mode"), "override")
        self.assertEqual(queue["setup_power"].get("source"), "autopilot_repair")

    def test_success_resets_failure_counter(self):
        with patch.dict(
            "os.environ",
            {"FACTORIO_AI_SKILL_REPAIR_ENABLED": "1", "FACTORIO_AI_IMPL_REPAIR_FAIL_LIMIT": "2"},
        ):
            controller = FactorioController(self.cfg)
            fail = RunSummary(False, "boom", 1, 0, self.dir / "x.log", "s")
            ok = RunSummary(True, "ok", 1, 1, self.dir / "x.log", "s")
            controller._track_implemented_skill_result("o", "setup_power", fail, {})
            controller._track_implemented_skill_result("o", "setup_power", ok, {})
            controller._track_implemented_skill_result("o", "setup_power", fail, {})
        names = {item["skill_name"] for item in skill_foundry.load_foundry_queue(self.dir)}
        self.assertNotIn("setup_power", names)  # reset after success -> 1 < limit

    def test_repair_disabled_does_not_enqueue(self):
        with patch.dict("os.environ", {"FACTORIO_AI_SKILL_REPAIR_ENABLED": "0", "FACTORIO_AI_IMPL_REPAIR_FAIL_LIMIT": "2"}):
            controller = FactorioController(self.cfg)
            fail = RunSummary(False, "boom", 1, 0, self.dir / "x.log", "s")
            controller._track_implemented_skill_result("o", "setup_power", fail, {})
            controller._track_implemented_skill_result("o", "setup_power", fail, {})
            controller._track_implemented_skill_result("o", "setup_power", fail, {})
        names = {item["skill_name"] for item in skill_foundry.load_foundry_queue(self.dir)}
        self.assertNotIn("setup_power", names)

    def test_foundry_candidates_allow_override_for_implemented_skill(self):
        skill_foundry.enqueue_foundry_request(self.dir, "setup_power", mode="override", reason="cannot place entity")
        controller = FactorioController(self.cfg)
        specs = {spec["skill_name"]: spec for spec in controller._foundry_candidates()}
        self.assertIn("setup_power", specs)
        self.assertEqual(specs["setup_power"].get("mode"), "override")

    def test_foundry_candidates_drop_implemented_skill_in_new_mode(self):
        skill_foundry.enqueue_foundry_request(self.dir, "setup_power", mode="new", reason="stale")
        controller = FactorioController(self.cfg)
        specs = {spec["skill_name"] for spec in controller._foundry_candidates()}
        self.assertNotIn("setup_power", specs)

    def test_active_override_routes_to_generated_run_config(self):
        self._register_override()
        controller = FactorioController(self.cfg)
        self.assertTrue(controller._is_generated_skill("setup_power"))
        config = controller._skill_run_config("setup_power")
        self.assertIsNotNone(config)
        self.assertEqual(type(config["skill"]).__name__, "SetupPowerOverrideSkill")


class SandboxIsolationTests(_Base):
    def test_sandbox_server_uses_isolated_runtime_dir(self):
        # Regression: the override sandbox shares nothing with the live server. Sharing
        # runtime_dir made Factorio's write-data lock collide -> 2nd server never started
        # -> RCON refused -> gate skipped -> overrides could never apply.
        from types import SimpleNamespace

        from factorio_ai import factorio, skill_foundry

        (self.dir / "factorio.exe").write_text("x", encoding="utf-8")
        live_save = factorio.no_mod_save_path(self.cfg)
        live_save.parent.mkdir(parents=True, exist_ok=True)
        live_save.write_bytes(b"savezip")
        skill_file = self.gen / "sb.py"
        skill_file.write_text("class SbSkill:\n    def next_action(self, o):\n        return None\n", encoding="utf-8")

        captured: dict = {}

        def fake_build(cfg, *, save_path=None, console_log=None):
            captured["runtime_dir"] = cfg.runtime_dir
            captured["server_port"] = cfg.server_port
            captured["save_path"] = save_path
            return ["noop"]

        class FakeProc:
            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        class FakeController:
            def __init__(self, cfg):
                pass

            def _run_skill(self, **kwargs):
                return SimpleNamespace(ok=True, steps=3, reason="ok")

        with patch.dict("os.environ", {"FACTORIO_AI_FOUNDRY_SANDBOX_ENABLED": "1"}), \
                patch.object(factorio, "build_start_no_mod_server_command", fake_build), \
                patch.object(factorio, "wait_for_rcon", lambda *a, **k: None), \
                patch("subprocess.Popen", return_value=FakeProc()), \
                patch("factorio_ai.controller.ModlessFactorioController", FakeController), \
                patch.object(skill_foundry, "load_generated_skill_class", return_value=type("D", (), {})):
            result = skill_foundry.sandbox_dryrun_gate(self.cfg, skill_file, steps=2)

        self.assertTrue(result.passed, result.details)
        self.assertNotIn("skipped", result.details)
        # isolated: a fresh dir nested under the live runtime, not the live runtime itself
        self.assertNotEqual(captured["runtime_dir"], self.cfg.runtime_dir)
        self.assertIn("sandbox", str(captured["runtime_dir"]))
        self.assertEqual(captured["save_path"], captured["runtime_dir"] / live_save.name)


class SandboxProgressTests(unittest.TestCase):
    def test_progress_counts_placed_entities_and_target_items(self):
        from factorio_ai.skill_foundry import _sandbox_progress

        before = {"entities": [{"name": "a"}], "inventory": {"iron-plate": 5}}
        after = {"entities": [{"name": "a"}, {"name": "b"}], "inventory": {"iron-plate": 10}}
        # 1 entity placed + (10-5) target items * 5 weight
        self.assertEqual(_sandbox_progress(before, after, "iron-plate"), 1 + 5 * 5)

    def test_no_change_is_zero_progress(self):
        from factorio_ai.skill_foundry import _sandbox_progress

        obs = {"entities": [{"name": "a"}], "inventory": {"iron-plate": 5}}
        self.assertEqual(_sandbox_progress(obs, obs, "iron-plate"), 0)

    def test_handles_missing_keys(self):
        from factorio_ai.skill_foundry import _sandbox_progress

        self.assertEqual(_sandbox_progress({}, {}, None), 0)


class FailureDiagnosticsTests(unittest.TestCase):
    def _obs(self):
        return {
            "entities": [
                {"type": "tree", "name": "tree-01", "position": {"x": 5, "y": 3}},
                {"type": "simple-entity", "name": "big-rock", "position": {"x": 6, "y": 3}},
                {"type": "cliff", "name": "cliff", "position": {"x": 7, "y": 3}},
                {"type": "assembling-machine", "name": "assembling-machine-1", "position": {"x": 0, "y": 0}},
            ]
        }

    def test_placement_failure_augments_with_obstacles(self):
        from factorio_ai.controller import _failure_diagnostics

        diag = _failure_diagnostics("setup_power", ["action failed: cannot place entity"], self._obs())
        self.assertIn("placement_obstacles", diag)
        po = diag["placement_obstacles"]
        self.assertEqual(po["obstacle_counts"], {"tree": 1, "rock": 1, "cliff": 1})
        self.assertTrue(po["nearby_obstacles"])
        self.assertIn("mine", po["hint"])

    def test_non_placement_failure_yields_no_diagnostics(self):
        from factorio_ai.controller import _failure_diagnostics

        self.assertEqual(_failure_diagnostics("setup_power", ["out of iron"], self._obs()), {})

    def test_placement_failure_without_obstacles_yields_nothing(self):
        from factorio_ai.controller import _failure_diagnostics

        self.assertEqual(_failure_diagnostics("setup_power", ["cannot place entity"], {"entities": []}), {})

    def test_augmenter_registry_is_extensible(self):
        from factorio_ai import controller

        def fake(skill, reasons, obs):
            return {"missing_observation": "fake_signal", "ok": True}

        controller._OBSERVATION_AUGMENTERS.append(fake)
        try:
            diag = controller._failure_diagnostics("x", ["anything"], {"entities": []})
            self.assertIn("fake_signal", diag)
        finally:
            controller._OBSERVATION_AUGMENTERS.remove(fake)


class PowerRootCauseTests(unittest.TestCase):
    def test_power_dependent_skill_with_dead_power_flags_root_cause(self):
        from factorio_ai.controller import _failure_diagnostics

        obs = {
            "entities": [
                {"name": "steam-engine", "type": "generator", "electric_network_connected": False},
                {"name": "boiler", "type": "boiler", "electric_network_connected": False},
            ]
        }
        diag = _failure_diagnostics("research_automation", ["cannot place entity"], obs)
        self.assertIn("power_health", diag)
        self.assertTrue(diag["power_health"]["broken"])
        self.assertEqual(diag["power_health"]["root_cause_skill"], "setup_power")

    def test_connected_generator_is_not_flagged(self):
        from factorio_ai.controller import _failure_diagnostics

        obs = {"entities": [{"name": "steam-engine", "electric_network_connected": True}]}
        self.assertNotIn("power_health", _failure_diagnostics("research_automation", ["x"], obs))

    def test_non_power_dependent_skill_not_flagged(self):
        from factorio_ai.controller import _failure_diagnostics

        obs = {"entities": [{"name": "steam-engine", "electric_network_connected": False}]}
        self.assertNotIn("power_health", _failure_diagnostics("stockpile_coal", ["x"], obs))


class StaleInProgressRecoveryTests(_Base):
    def test_stale_in_progress_becomes_eligible_again(self):
        from datetime import timedelta

        skill_foundry.update_skill("stuck_skill", status="in_progress")
        # Fresh in_progress is not eligible (a develop is presumably running).
        self.assertEqual(skill_foundry.eligible_for_generation("stuck_skill"), (False, "in_progress"))
        # Once stale (the develop was interrupted), it must become eligible again.
        future = skill_foundry._now() + timedelta(seconds=2000)
        with patch.object(skill_foundry, "_now", return_value=future):
            ok, _why = skill_foundry.eligible_for_generation("stuck_skill")
        self.assertTrue(ok)


class CodegenPromptObstacleTests(unittest.TestCase):
    def test_override_prompt_has_obstacle_pattern_and_injected_diagnostics(self):
        from factorio_ai.skill_foundry import _build_codegen_prompt

        spec = {
            "skill_name": "setup_power",
            "mode": "override",
            "reason": "cannot place entity",
            "diagnostics": {"placement_obstacles": {"obstacle_counts": {"tree": 3}, "hint": "clear it"}},
        }
        prompt = _build_codegen_prompt(spec, [{"entities": []}], "")
        self.assertIn("OBSTACLES", prompt)
        self.assertIn("mine", prompt)
        self.assertIn("placement_obstacles", prompt)


if __name__ == "__main__":
    unittest.main()
