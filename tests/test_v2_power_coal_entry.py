"""The boiler's coal belt must face the coal source instead of looping around the steam bank."""

from types import SimpleNamespace
import unittest

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.core.factory_templates import COAL_ENTRIES, build_template


def coal_port(bank):
    return next(p for p in bank["ports"] if p["item"] == "coal")


class CoalEntryTemplateTests(unittest.TestCase):
    def test_every_entry_builds_and_keeps_the_inserter_fed(self):
        for rotation in (0, 4, 8, 12):
            for entry in COAL_ENTRIES:
                bank = build_template("steam_bank", rotation=rotation, coal_entry=entry)
                self.assertTrue(bank["ok"], (rotation, entry, bank))
                belts = {
                    (e["position"]["x"], e["position"]["y"]) for e in bank["entities"] if e["name"] == "transport-belt"
                }
                port = coal_port(bank)
                self.assertIn((port["position"]["x"], port["position"]["y"]), belts)
                self.assertEqual(len(belts), 5)

    def test_default_layout_is_unchanged(self):
        self.assertEqual(build_template("steam_bank"), build_template("steam_bank", coal_entry="west"))

    def test_entries_differ_in_port_side_and_direction(self):
        ports = {entry: coal_port(build_template("steam_bank", coal_entry=entry)) for entry in COAL_ENTRIES}
        self.assertEqual({p["facing"] for p in ports.values()}, {4, 12, 0})
        self.assertLess(ports["west"]["position"]["x"], ports["east"]["position"]["x"])
        self.assertGreater(ports["south"]["position"]["y"], ports["west"]["position"]["y"])

    def test_unknown_entry_is_rejected(self):
        self.assertFalse(build_template("steam_bank", coal_entry="up")["ok"])


class CoalEntryChoiceTests(unittest.TestCase):
    def variants(self, coal):
        builder = FactoryBuilder.__new__(FactoryBuilder)
        builder.coal_sites = lambda: [coal] if coal else []
        return builder._steam_bank_variants({"x": 0.5, "y": 0.5}, 0)

    def first_facing(self, coal):
        return coal_port(self.variants(coal)[0])["facing"]

    def test_coal_to_the_west_enters_eastbound(self):
        self.assertEqual(self.first_facing({"x": -80, "y": 3}), 4)

    def test_coal_to_the_east_enters_westbound_without_looping_around(self):
        self.assertEqual(self.first_facing({"x": 80, "y": 3}), 12)

    def test_coal_below_enters_straight_upward(self):
        self.assertEqual(self.first_facing({"x": 0, "y": 90}), 0)

    def test_choice_follows_the_bank_rotation(self):
        builder = FactoryBuilder.__new__(FactoryBuilder)
        builder.coal_sites = lambda: [{"x": 0, "y": -90}]
        port = coal_port(builder._steam_bank_variants({"x": 0.5, "y": 0.5}, 8)[0])
        self.assertEqual(port["facing"], 8)

    def test_all_variants_are_offered_and_no_coal_keeps_the_default_first(self):
        self.assertEqual(len(self.variants({"x": 50, "y": 50})), 3)
        self.assertEqual(coal_port(self.variants(None)[0])["facing"], 4)


if __name__ == "__main__":
    unittest.main()
