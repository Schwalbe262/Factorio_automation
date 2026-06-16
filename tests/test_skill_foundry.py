import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from factorio_ai import skill_foundry
from factorio_ai.models import PlannerDecision


GOOD_SKILL = '''from __future__ import annotations

from factorio_ai.models import PlannerDecision, total_item_count


class MakeThingSkill:
    """Craft iron gear wheels until a target is reached."""

    def __init__(self, target: int = 5):
        self.target = target

    def next_action(self, observation):
        have = total_item_count(observation, "iron-gear-wheel")
        if have >= self.target:
            return PlannerDecision(None, "gear target reached", done=True)
        return PlannerDecision({"type": "craft", "recipe": "iron-gear-wheel", "count": 1}, "craft a gear")
'''


class StaticSafetyGateTests(unittest.TestCase):
    def test_accepts_good_module(self):
        result = skill_foundry.static_safety_gate(GOOD_SKILL)
        self.assertTrue(result.passed, result.reasons)

    def test_rejects_os_import(self):
        code = "import os\n" + GOOD_SKILL
        result = skill_foundry.static_safety_gate(code)
        self.assertFalse(result.passed)

    def test_rejects_from_os_import(self):
        code = "from os import path\n" + GOOD_SKILL
        result = skill_foundry.static_safety_gate(code)
        self.assertFalse(result.passed)

    def test_rejects_relative_import(self):
        code = "from . import planner\n" + GOOD_SKILL
        result = skill_foundry.static_safety_gate(code)
        self.assertFalse(result.passed)

    def test_rejects_eval(self):
        code = GOOD_SKILL.replace('"craft a gear")', '"craft a gear") if eval("1") else None')
        result = skill_foundry.static_safety_gate(code)
        self.assertFalse(result.passed)

    def test_rejects_getattr_dunder_escape(self):
        code = (
            "from factorio_ai.models import PlannerDecision\n\n\n"
            "class EscapeSkill:\n"
            "    def next_action(self, observation):\n"
            "        x = getattr(observation, '__class__')\n"
            "        return PlannerDecision(None, 'x', done=True)\n"
        )
        result = skill_foundry.static_safety_gate(code)
        self.assertFalse(result.passed)

    def test_rejects_subclasses_string_escape(self):
        code = (
            "from factorio_ai.models import PlannerDecision\n\n\n"
            "class EscapeSkill:\n"
            "    marker = '__subclasses__'\n"
            "    def next_action(self, observation):\n"
            "        return PlannerDecision(None, 'x', done=True)\n"
        )
        result = skill_foundry.static_safety_gate(code)
        self.assertFalse(result.passed)

    def test_rejects_two_classes(self):
        code = GOOD_SKILL + "\n\nclass OtherSkill:\n    def next_action(self, observation):\n        return None\n"
        result = skill_foundry.static_safety_gate(code)
        self.assertFalse(result.passed)

    def test_rejects_missing_next_action(self):
        code = (
            "from factorio_ai.models import PlannerDecision\n\n\n"
            "class NoActionSkill:\n"
            "    def run(self, observation):\n"
            "        return PlannerDecision(None, 'x', done=True)\n"
        )
        result = skill_foundry.static_safety_gate(code)
        self.assertFalse(result.passed)

    def test_rejects_toplevel_call(self):
        code = "from factorio_ai.models import PlannerDecision\nLEAK = open('x')\n" + GOOD_SKILL
        result = skill_foundry.static_safety_gate(code)
        self.assertFalse(result.passed)

    def test_rejects_syntax_error(self):
        result = skill_foundry.static_safety_gate("class Broken(:\n")
        self.assertFalse(result.passed)


class OfflineReplayGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._prev = os.environ.get("FACTORIO_AI_GENERATED_SKILLS_DIR")
        os.environ["FACTORIO_AI_GENERATED_SKILLS_DIR"] = str(self.dir)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("FACTORIO_AI_GENERATED_SKILLS_DIR", None)
        else:
            os.environ["FACTORIO_AI_GENERATED_SKILLS_DIR"] = self._prev
        self._tmp.cleanup()

    def _write(self, code: str) -> Path:
        path = self.dir / "candidate.py"
        path.write_text(code, encoding="utf-8")
        return path

    def test_good_module_passes(self):
        path = self._write(GOOD_SKILL)
        result = skill_foundry.offline_replay_gate(path, skill_foundry._synthetic_samples())
        self.assertTrue(result.passed, result.reasons)

    def test_bad_action_type_fails(self):
        code = GOOD_SKILL.replace('"type": "craft"', '"type": "teleport"')
        path = self._write(code)
        result = skill_foundry.offline_replay_gate(path, skill_foundry._synthetic_samples())
        self.assertFalse(result.passed)

    def test_raising_module_fails(self):
        code = (
            "from factorio_ai.models import PlannerDecision\n\n\n"
            "class BoomSkill:\n"
            "    def next_action(self, observation):\n"
            "        raise RuntimeError('boom')\n"
        )
        path = self._write(code)
        result = skill_foundry.offline_replay_gate(path, skill_foundry._synthetic_samples())
        self.assertFalse(result.passed)

    def test_non_decision_return_fails(self):
        code = (
            "from factorio_ai.models import PlannerDecision\n\n\n"
            "class StringSkill:\n"
            "    def next_action(self, observation):\n"
            "        return 'not a decision'\n"
        )
        path = self._write(code)
        result = skill_foundry.offline_replay_gate(path, skill_foundry._synthetic_samples())
        self.assertFalse(result.passed)

    def test_loader_returns_fresh_class_after_file_change(self):
        path = self._write(GOOD_SKILL)
        first = skill_foundry.load_generated_skill_class(path)
        self.assertEqual(first.__name__, "MakeThingSkill")
        path.write_text(GOOD_SKILL.replace("MakeThingSkill", "RenamedSkill"), encoding="utf-8")
        second = skill_foundry.load_generated_skill_class(path)
        self.assertEqual(second.__name__, "RenamedSkill")

    def test_loader_refuses_path_outside_generated_dir(self):
        outside = Path(self._tmp.name).parent / "evil.py"
        outside.write_text(GOOD_SKILL, encoding="utf-8")
        try:
            with self.assertRaises(ValueError):
                skill_foundry.load_generated_skill_class(str(outside))
        finally:
            outside.unlink(missing_ok=True)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._prev = os.environ.get("FACTORIO_AI_GENERATED_SKILLS_DIR")
        os.environ["FACTORIO_AI_GENERATED_SKILLS_DIR"] = str(self.dir)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("FACTORIO_AI_GENERATED_SKILLS_DIR", None)
        else:
            os.environ["FACTORIO_AI_GENERATED_SKILLS_DIR"] = self._prev
        self._tmp.cleanup()

    def test_round_trip_and_atomic_write(self):
        skill_foundry.update_skill("foo_skill", status="registered", file_path="x.py", class_name="FooSkill")
        reloaded = skill_foundry.load_registry()
        self.assertIn("foo_skill", reloaded["skills"])
        self.assertEqual(reloaded["skills"]["foo_skill"]["status"], "registered")
        # registry file is valid JSON, no leftover tmp
        self.assertTrue((self.dir / "registry.json").exists())
        self.assertFalse((self.dir / "registry.json.tmp").exists())

    def test_registered_filter_requires_existing_file(self):
        # registered but no file on disk -> excluded
        skill_foundry.update_skill("ghost_skill", status="registered", file_path="missing.py")
        self.assertNotIn("ghost_skill", skill_foundry.registered_generated_skills())
        # write the file -> included
        (self.dir / "ghost_skill.py").write_text(GOOD_SKILL, encoding="utf-8")
        skill_foundry.update_skill("ghost_skill", file_path="ghost_skill.py")
        # file_path is resolved relative to repo root unless absolute; use absolute here
        skill_foundry.update_skill("ghost_skill", file_path=str(self.dir / "ghost_skill.py"))
        self.assertIn("ghost_skill", skill_foundry.registered_generated_skills())

    def test_set_status_disabled_drops_from_registered(self):
        (self.dir / "live_skill.py").write_text(GOOD_SKILL, encoding="utf-8")
        skill_foundry.update_skill(
            "live_skill", status="registered", file_path=str(self.dir / "live_skill.py")
        )
        self.assertIn("live_skill", skill_foundry.registered_generated_skills())
        skill_foundry.set_skill_status("live_skill", "disabled", "regressed live")
        self.assertNotIn("live_skill", skill_foundry.registered_generated_skills())


class DevelopSkillTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._prev = os.environ.get("FACTORIO_AI_GENERATED_SKILLS_DIR")
        os.environ["FACTORIO_AI_GENERATED_SKILLS_DIR"] = str(self.dir)
        self.cfg = SimpleNamespace(runtime_dir=self.dir, log_dir=self.dir)
        from factorio_ai import slurm_worker

        self._slurm = slurm_worker
        self._orig_call = slurm_worker.call_llm_json_with_diagnostics

    def tearDown(self):
        self._slurm.call_llm_json_with_diagnostics = self._orig_call
        if self._prev is None:
            os.environ.pop("FACTORIO_AI_GENERATED_SKILLS_DIR", None)
        else:
            os.environ["FACTORIO_AI_GENERATED_SKILLS_DIR"] = self._prev
        self._tmp.cleanup()

    def _patch_llm(self, code: str, class_name: str = "MakeThingSkill"):
        def fake(system, prompt, schema=None, *, kind="llm", task_id="", max_tokens=None):
            return {"class_name": class_name, "code": code}, {"llm_trace": {}}

        self._slurm.call_llm_json_with_diagnostics = fake

    def test_pipeline_registers_good_skill(self):
        self._patch_llm(GOOD_SKILL)
        spec = {"skill_name": "make_thing", "reason": "need gears", "target_item": "iron-gear-wheel"}
        result = skill_foundry.develop_skill(self.cfg, spec, run_sandbox=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "registered")
        entry = skill_foundry.registry_status("make_thing")
        self.assertEqual(entry["status"], "registered")
        self.assertTrue((self.dir / "make_thing.py").exists())
        self.assertIn("make_thing", skill_foundry.registered_generated_skills())

    def test_pipeline_rejects_unsafe_skill(self):
        self._patch_llm("import os\n" + GOOD_SKILL)
        spec = {"skill_name": "bad_thing", "reason": "x"}
        result = skill_foundry.develop_skill(self.cfg, spec, run_sandbox=False, max_attempts=2)
        self.assertFalse(result["ok"])
        entry = skill_foundry.registry_status("bad_thing")
        self.assertEqual(entry["status"], "failed")
        self.assertFalse((self.dir / "bad_thing.py").exists())
        # the rejected attempt is archived as training data
        attempts = list((self.dir / "generated-skills" / "attempts").glob("bad_thing-*.py"))
        self.assertTrue(attempts)


class CodegenPromptTests(unittest.TestCase):
    def test_hand_examples_pass_static_safety_gate(self):
        # The examples we feed the model must themselves be gate-valid, or we teach it bad patterns.
        for index, src in enumerate(skill_foundry._HAND_EXAMPLES):
            result = skill_foundry.static_safety_gate(src)
            self.assertTrue(result.passed, msg=f"example {index} failed static gate: {result.reasons}")

    def test_hand_examples_pass_offline_replay_gate(self):
        samples = skill_foundry._synthetic_samples()
        with tempfile.TemporaryDirectory() as tmp:
            for index, src in enumerate(skill_foundry._HAND_EXAMPLES):
                path = Path(tmp) / f"example_{index}.py"
                path.write_text(src, encoding="utf-8")
                result = skill_foundry.offline_replay_gate(path, samples)
                self.assertTrue(result.passed, msg=f"example {index} failed replay gate: {result.reasons}")

    def test_vocabulary_lists_real_names(self):
        vocab = skill_foundry._codegen_vocabulary()
        for name in ("stone-furnace", "iron-plate", "transport-belt", "coal"):
            self.assertIn(name, vocab)


if __name__ == "__main__":
    unittest.main()
