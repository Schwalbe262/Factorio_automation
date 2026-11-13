"""Growth options of built blocks as data (``block_growth.growth_options``)."""

import json
import tempfile
import unittest
from pathlib import Path

from factorio_agent.block_growth import growth_options
from factorio_agent.production_layout import design_line
from factorio_agent.production_rows import validate_parameters
from test_v2_production_layout import PLATES, catalog
from v2_blocks_fixture import real_catalog

BLUE_CATALOG = real_catalog()


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

    @unittest.skipIf(BLUE_CATALOG is None, "real catalog not available")
    def test_two_to_four_machine_blocks_report_rate_slots_and_unit(self):
        from factorio_agent.chemical_science import chemical_science_core

        core = chemical_science_core(BLUE_CATALOG, 10.5, 10.5, 2)
        state = {"world_id": "w", "core": core, "plan": {"entities": core["entities"]}, "rated": core["rated"]}
        (self.root / "chemical-science.json").write_text(json.dumps(state), encoding="utf-8")
        (option,) = growth_options(self.root, "w")
        self.assertEqual((option["capability"], option["kind"], option["slots"]), ("chemical_science", "rebuild", 2))
        self.assertEqual(option["cost"]["items"]["assembling-machine-1"], 1)
        self.assertAlmostEqual(option["max_rate_in_place"], 2 * option["rate_now"], 3)


@unittest.skipIf(BLUE_CATALOG is None, "real catalog not available")
class RateParameterTests(unittest.TestCase):
    def test_rate_sizes_two_to_four_machines_and_slots_stay_clear(self):
        from factorio_agent import advanced_circuits as ac
        from factorio_agent import chemical_science as cs
        from factorio_agent.blocks.geometry import footprint

        c = BLUE_CATALOG
        for module, rates, core, key in (
            (cs, cs.chemical_science_rates, cs.chemical_science_core, "science_per_minute"),
            (ac, ac.advanced_circuit_rates, ac.advanced_circuit_core, "advanced_circuits_per_minute"),
        ):
            two, four = rates(c, 2)[key], rates(c, 4)[key]
            self.assertEqual(module.count_for_rate(c, two), 2)
            self.assertEqual(module.count_for_rate(c, two + 0.01), 3)
            self.assertEqual(module.count_for_rate(c, four * 3), 4)
            built = core(c, 10.5, 10.5, 2)
            tiles = set().union(*(footprint(c, e) for e in built["entities"]))
            self.assertEqual(len(module.growth_slots(built)), 2)
            for group in module.growth_slots(built):
                self.assertFalse(set().union(*(footprint(c, e) for e in group)) & tiles)
        cs.validate_chemical_science({})
        cs.validate_chemical_science({"rate_per_minute": 7.5})
        with self.assertRaises(ValueError):
            ac.validate_advanced_circuits({"rate_per_minute": 0})


class ProviderTests(unittest.TestCase):
    def test_the_advanced_circuit_bottleneck_reads_block_growth(self):
        from factorio_agent.growth_providers import provider_for

        name, provider = provider_for({"block": "advanced_circuit_production", "item": "advanced-circuit"})
        self.assertEqual(name, "blocks")
        with tempfile.TemporaryDirectory() as tmp:
            state = {
                "world_id": "w",
                "core": {"red_machines": [{}, {}], "growth_slots": [[{"name": "assembling-machine-1"}]]},
                "plan": {},
                "rated": {"advanced_circuits_per_minute": 4},
            }
            (Path(tmp) / "advanced-circuits.json").write_text(json.dumps(state), encoding="utf-8")
            (option,) = provider({"block": "advanced_circuit_production"}, {"world_id": "w"}, {"root": tmp})
        self.assertEqual((option["kind"], option["feasible"], option["gain_per_minute"]), ("rebuild", False, 2.0))
        self.assertEqual(option["cost"], {"assembling-machine-1": 1})


class RootWiringTests(unittest.TestCase):
    def test_growth_nodes_and_the_bottleneck_receipt_hand_the_runtime_root_to_providers(self):
        from unittest.mock import patch

        from factorio_agent import bottleneck_audit, growth_providers
        from factorio_agent.jobs import CAPABILITIES

        seen = []

        def spy(root, world_id=None):
            seen.append(root)
            return []

        row = {
            "block": "advanced_circuit_production",
            "item": "advanced-circuit",
            "kind": "at_capacity",
            "lost_seconds_per_minute": 30,
        }
        with patch("factorio_agent.block_growth.growth_options", side_effect=spy):
            growth_providers.growth_nodes([row], [], [], {"world_id": "w", "tick": 1}, CAPABILITIES, root="R")
            bottleneck_audit.assess(
                row, {"world_id": "w"}, {"jobs": [], "queue_nodes": [], "capabilities": CAPABILITIES, "root": "Q"}
            )
        self.assertEqual(seen[:2], ["R", "Q"])
        source = Path(bottleneck_audit.__file__).read_text(encoding="utf-8")
        self.assertIn('"root": str(root)', source)  # receipt() builds the provider context with its root


if __name__ == "__main__":
    unittest.main()
