from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.upgrades import next_upgrade


class UpgradePolicyTests(unittest.TestCase):
    def researcher(self, technologies):
        return SimpleNamespace(
            catalog=SimpleNamespace(technologies=technologies),
            step=Mock(side_effect=lambda obs, p: dict(type="research", technology=p["technology"])),
        )

    def test_active_research_is_preserved_without_asking_to_switch(self):
        r = self.researcher({"stone-wall": {"prerequisites": []}})
        self.assertIsNone(next_upgrade({"research": "processing-unit"}, r))
        r.step.assert_not_called()

    def test_normal_defense_unlocks_precede_productivity_but_skip_missing_prerequisites(self):
        r = self.researcher(
            {
                "stone-wall": {"prerequisites": ["automation-science-pack"]},
                "radar": {"prerequisites": []},
                "mining-productivity-1": {"prerequisites": []},
            }
        )
        self.assertEqual(next_upgrade({"technologies": {}}, r)["technology"], "radar")
        self.assertEqual(
            next_upgrade({"technologies": {"automation-science-pack": True}}, r)["technology"], "stone-wall"
        )
        self.assertEqual(
            next_upgrade({"technologies": {"automation-science-pack": True, "stone-wall": True, "radar": True}}, r)[
                "technology"
            ],
            "mining-productivity-1",
        )

    def test_unavailable_science_never_blocks_defensive_work_or_grants_unlocks(self):
        r = self.researcher({"mining-productivity-1": {"prerequisites": []}, "research-speed-1": {"prerequisites": []}})
        r.step.side_effect = [
            {"status": "blocked", "reason": "continuous_science_supply_not_implemented"},
            {"status": "waiting", "reason": "waiting for normal packs"},
        ]
        self.assertIsNone(next_upgrade({"technologies": {}}, r))
        self.assertEqual(r.step.call_count, 2)

    def test_trigger_disabled_and_completed_technologies_are_not_selected(self):
        r = self.researcher(
            {
                "stone-wall": {"prerequisites": [], "enabled": False},
                "radar": {"prerequisites": [], "research_trigger": {"type": "build-entity"}},
                "military": {"prerequisites": []},
            }
        )
        self.assertIsNone(next_upgrade({"technologies": {"military": True}}, r))
        r.step.assert_not_called()


if __name__ == "__main__":
    unittest.main()
