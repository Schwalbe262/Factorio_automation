import tempfile
import unittest
from pathlib import Path

from factorio_ai.skill_registry import annotate_strategy_with_skill_status, skill_status


class SkillRegistryTests(unittest.TestCase):
    def test_implemented_skill_status(self):
        status = skill_status("produce_iron_plate")
        self.assertTrue(status.implemented)
        self.assertFalse(status.codex_required)

    def test_copper_skill_is_implemented(self):
        status = skill_status("produce_copper_plate")
        self.assertTrue(status.implemented)
        self.assertEqual(status.executor, "CopperPlateSkill")
        self.assertFalse(status.codex_required)

    def test_electronic_circuit_skill_is_implemented(self):
        status = skill_status("produce_electronic_circuit")
        self.assertTrue(status.implemented)
        self.assertEqual(status.executor, "ElectronicCircuitSkill")
        self.assertFalse(status.codex_required)

    def test_belt_smelting_skill_is_implemented(self):
        status = skill_status("build_belt_smelting_line")
        self.assertTrue(status.implemented)
        self.assertEqual(status.executor, "BeltSmeltingLineSkill")
        self.assertFalse(status.codex_required)

    def test_missing_skill_writes_backlog(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            annotated = annotate_strategy_with_skill_status(
                {
                    "selected_skill": "setup_power",
                    "reason": "electricity is needed",
                    "blockers": ["electric power"],
                },
                runtime_dir=runtime,
            )
            self.assertFalse(annotated["skill_status"]["implemented"])
            backlog = runtime / "missing-skills.jsonl"
            self.assertTrue(backlog.exists())
            self.assertIn("setup_power", backlog.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
