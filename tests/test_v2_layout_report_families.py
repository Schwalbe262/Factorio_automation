"""The fleet report's layout variants include mine and smelting bank variants."""

import json
from pathlib import Path
import tempfile
import unittest

from factorio_agent.layout_selection import format_variants, run_layouts


class MineAndSmeltingVariants(unittest.TestCase):
    def test_mine_and_smelting_variants_are_reported_per_block(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        layout = {
            "variant": "mine-simple-E-sat-s1",
            "genome": {"form": "mine"},
            "chosen_tick": 600,
            "verified_tick": None,
        }
        banks = [
            {"plan": {"resource": "iron-ore"}, "ready": True, "output_evidence": {"tick": 4200}, "layout": layout},
            {"plan": {"resource": "coal"}, "ready": False, "layout": dict(layout, variant="mine-simple-S-sat-s2")},
            {"plan": {"resource": "copper-ore"}, "ready": True},  # planned before the family: not reported
        ]
        (root / "mining-outposts.json").write_text(json.dumps({"banks": banks}), encoding="utf-8")
        district = {"banks": [{"resource": "iron-ore", "capacity_slots": 12, "per_side_count": 3}]}
        (root / "smelting-district.json").write_text(json.dumps(district), encoding="utf-8")
        rows = run_layouts(root, milestones={"smelting_district": 20.0, "main_bus": 30.0})
        by_block = {r["block"]: r for r in rows}
        mine = by_block["mining:iron-ore"]
        self.assertEqual((mine["variant"], mine["proof"], mine["build_seconds"]), ("mine-simple-E-sat-s1", True, 60))
        self.assertFalse(by_block["mining:coal"]["proof"])
        self.assertNotIn("mining:copper-ore", by_block)
        smelt = by_block["smelting:iron-ore"]
        self.assertEqual((smelt["variant"], smelt["proof"]), ("smelt-12x1-stone", True))
        self.assertEqual(smelt["followup_seconds"], 600)
        text = "\n".join(format_variants(rows))
        for block in ("mining:iron-ore", "mining:coal", "smelting:iron-ore"):
            self.assertIn(block, text)


if __name__ == "__main__":
    unittest.main()
