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


if __name__ == "__main__":
    unittest.main()
