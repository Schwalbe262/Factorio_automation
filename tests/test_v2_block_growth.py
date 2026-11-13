"""Growth options of built blocks as data (``block_growth.growth_options``)."""

import json
import tempfile
import unittest
from pathlib import Path

from factorio_agent.block_growth import growth_options
from factorio_agent.production_layout import design_line
from factorio_agent.production_rows import validate_parameters
from test_v2_production_layout import PLATES, catalog


def save(root: Path, item: str, line: dict, world="w"):
    state = {"item": item, "world_id": world, "line": line}
    (root / f"production-row-{item}.json").write_text(json.dumps(state), encoding="utf-8")


class GrowthOptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.catalog = catalog()

    def tearDown(self):
        self.tmp.cleanup()

    def line(self, product, rate, **kw):
        return design_line(
            self.catalog,
            product,
            rate,
            inserter="inserter",
            enabled=sorted(self.catalog.recipes),
            sourced=PLATES.__contains__,
            chest="iron-chest",
            **kw,
        )

    def test_a_row_with_reserved_slots_offers_one_machine_more(self):
        line = self.line("iron-gear-wheel", 45, output="belt")
        save(self.root, "iron-gear-wheel", line)
        (option,) = growth_options(self.root, "w")
        self.assertEqual(option["kind"], "in_place")
        self.assertGreater(option["slots"], 0)
        self.assertAlmostEqual(
            option["rate_now"] + option["step_rate"], option["action"]["parameters"]["rate_per_minute"], 3
        )
        self.assertEqual(option["action"]["parameters"]["output"], "belt")
        validate_parameters(option["action"]["parameters"])
        self.assertIn(line["main"]["machine"], option["cost"]["items"])

    def test_a_full_row_needs_a_rebuild_and_other_worlds_are_ignored(self):
        line = self.line("pipe", 30)
        line["main"]["growth"]["remaining_slots"] = 0
        save(self.root, "pipe", line)
        save(self.root, "iron-gear-wheel", self.line("iron-gear-wheel", 45), world="other")
        (option,) = growth_options(self.root, "w")
        self.assertEqual((option["kind"], option["action"]), ("rebuild", None))

    def test_fixed_blocks_say_what_growth_needs(self):
        (self.root / "chemical-science.json").write_text(json.dumps({"world_id": "w"}), encoding="utf-8")
        (option,) = growth_options(self.root, "w")
        self.assertEqual(
            (option["capability"], option["kind"], option["limit"]), ("chemical_science", "fixed", "no_rate_parameter")
        )


if __name__ == "__main__":
    unittest.main()
