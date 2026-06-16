import os
import tempfile
import unittest
from pathlib import Path

from factorio_ai import skill_foundry
from factorio_ai.config import AppConfig
from factorio_ai.controller import FactorioController
from factorio_ai.skill_registry import skill_status


GOOD_SKILL = '''from __future__ import annotations

from factorio_ai.models import PlannerDecision, total_item_count


class MakeThingSkill:
    def __init__(self, target: int = 5):
        self.target = target

    def next_action(self, observation):
        have = total_item_count(observation, "iron-gear-wheel")
        if have >= self.target:
            return PlannerDecision(None, "done", done=True)
        return PlannerDecision({"type": "craft", "recipe": "iron-gear-wheel", "count": 1}, "craft a gear")
'''


def _make_cfg(tmp: Path) -> AppConfig:
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


class GeneratedSkillPickupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._prev = os.environ.get("FACTORIO_AI_GENERATED_SKILLS_DIR")
        os.environ["FACTORIO_AI_GENERATED_SKILLS_DIR"] = str(self.dir)
        (self.dir / "make_thing.py").write_text(GOOD_SKILL, encoding="utf-8")
        skill_foundry.update_skill(
            "make_thing",
            status="registered",
            class_name="MakeThingSkill",
            file_path=str(self.dir / "make_thing.py"),
            target_item="iron-gear-wheel",
            default_target=5,
            default_max_steps=300,
            log_prefix="strategy-generated-make_thing",
        )
        self.controller = FactorioController(_make_cfg(self.dir))

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("FACTORIO_AI_GENERATED_SKILLS_DIR", None)
        else:
            os.environ["FACTORIO_AI_GENERATED_SKILLS_DIR"] = self._prev
        self._tmp.cleanup()

    def test_skill_status_reports_registered_generated_skill(self):
        status = skill_status("make_thing")
        self.assertTrue(status.implemented)
        self.assertFalse(status.codex_required)
        self.assertEqual(status.executor, "MakeThingSkill")

    def test_run_config_resolves_generated_skill(self):
        config = self.controller._skill_run_config("make_thing")
        self.assertIsNotNone(config)
        self.assertTrue(hasattr(config["skill"], "next_action"))
        self.assertEqual(config["target_item"], "iron-gear-wheel")
        self.assertEqual(config["goal"], "make_thing")

    def test_disabled_skill_reverts_to_codex_required(self):
        skill_foundry.set_skill_status("make_thing", "disabled", "regressed")
        self.assertIsNone(self.controller._skill_run_config("make_thing"))
        status = skill_status("make_thing")
        self.assertFalse(status.implemented)
        self.assertTrue(status.codex_required)

    def test_foundry_candidates_skip_already_implemented_skills(self):
        # A stale missing-skills backlog / queue entry that names a hand-written skill must not be
        # offered to the foundry for codegen (it already has an executor).
        skill_foundry.enqueue_foundry_request(self.dir, "build_starter_defense", reason="stale backlog")
        skill_foundry.enqueue_foundry_request(self.dir, "needs_generation", reason="genuinely missing")
        names = {spec["skill_name"] for spec in self.controller._foundry_candidates()}
        self.assertNotIn("build_starter_defense", names)
        self.assertIn("needs_generation", names)
        # the implemented skill is also pruned from the persisted queue
        remaining = {item["skill_name"] for item in skill_foundry.load_foundry_queue(self.dir)}
        self.assertNotIn("build_starter_defense", remaining)


if __name__ == "__main__":
    unittest.main()
