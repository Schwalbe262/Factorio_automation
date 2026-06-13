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

    def test_expand_iron_smelting_skill_is_implemented(self):
        status = skill_status("expand_iron_smelting")
        self.assertTrue(status.implemented)
        self.assertEqual(status.executor, "ExpandIronSmeltingSkill")
        self.assertFalse(status.codex_required)

    def test_expand_copper_smelting_skill_is_implemented(self):
        status = skill_status("expand_copper_smelting")
        self.assertTrue(status.implemented)
        self.assertEqual(status.executor, "ExpandCopperSmeltingSkill")
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

    def test_coal_supply_skill_is_implemented(self):
        status = skill_status("setup_coal_supply")
        self.assertTrue(status.implemented)
        self.assertEqual(status.executor, "CoalSupplySkill")
        self.assertFalse(status.codex_required)

    def test_coal_fuel_feed_skill_is_implemented(self):
        status = skill_status("connect_coal_fuel_feed")
        self.assertTrue(status.implemented)
        self.assertEqual(status.executor, "CoalFuelFeedSkill")
        self.assertFalse(status.codex_required)

    def test_setup_power_skill_is_implemented(self):
        status = skill_status("setup_power")
        self.assertTrue(status.implemented)
        self.assertEqual(status.executor, "SetupPowerSkill")

    def test_research_automation_skill_is_implemented(self):
        status = skill_status("research_automation")
        self.assertTrue(status.implemented)
        self.assertEqual(status.executor, "ResearchAutomationSkill")
        self.assertFalse(status.codex_required)

    def test_circuit_automation_skill_is_implemented(self):
        status = skill_status("automate_electronic_circuit_line")
        self.assertTrue(status.implemented)
        self.assertEqual(status.executor, "CircuitAutomationSkill")
        self.assertFalse(status.codex_required)

    def test_research_logistics_skill_is_implemented(self):
        status = skill_status("research_logistics")
        self.assertTrue(status.implemented)
        self.assertEqual(status.executor, "ResearchTechnologySkill")
        self.assertFalse(status.codex_required)

    def test_starter_defense_skill_is_implemented(self):
        status = skill_status("build_starter_defense")
        self.assertTrue(status.implemented)
        self.assertEqual(status.executor, "StarterDefenseSkill")
        self.assertFalse(status.codex_required)

    def test_build_item_mall_skill_is_implemented(self):
        status = skill_status("bootstrap_build_item_mall")
        self.assertTrue(status.implemented)
        self.assertEqual(status.executor, "BuildItemMallSkill")
        self.assertFalse(status.codex_required)

    def test_factory_layout_skill_is_implemented(self):
        status = skill_status("plan_factory_site")
        self.assertTrue(status.implemented)
        self.assertEqual(status.executor, "FactoryLayoutImprovementSkill")
        self.assertFalse(status.codex_required)

    def test_missing_skill_writes_backlog(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir)
            annotated = annotate_strategy_with_skill_status(
                {
                    "selected_skill": "plan_rail_network",
                    "reason": "remote resources need a rail corridor",
                    "blockers": ["rail network planner"],
                },
                runtime_dir=runtime,
            )
            self.assertFalse(annotated["skill_status"]["implemented"])
            backlog = runtime / "missing-skills.jsonl"
            self.assertTrue(backlog.exists())
            self.assertIn("plan_rail_network", backlog.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
