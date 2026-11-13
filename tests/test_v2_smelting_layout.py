"""Smelting bank shape family (smelting_layout) around smelting.paired_furnace_bank."""

from types import SimpleNamespace
import unittest

from factorio_agent import layout_selection as selection
from factorio_agent import smelting_layout as bank
from factorio_agent.smelting import paired_furnace_bank

CATALOG = SimpleNamespace(entities={}, fingerprint="c")
REQUEST = {"resource": "iron-ore", "start_per_row": 2}


class Family(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.found = bank.candidates(CATALOG, REQUEST, library=False)
        cls.by_id = {c["descriptor"]["id"]: c for c in cls.found["candidates"]}

    def test_the_district_bank_is_the_default_genome_unchanged(self):
        block = bank.generate(bank.BankGenome(), "iron-ore", 3, length=48)
        direct = paired_furnace_bank(0, 0, 3, "iron-ore", length=48)
        self.assertEqual(block["banks"][0]["entities"], direct["entities"])
        self.assertEqual(block["banks"][0]["ports"], direct["ports"])

    def test_the_saturated_reference_shape_is_a_candidate(self):
        # Two rows of 24 stone furnaces around one shared centre output: each row one lane, one full belt.
        d = self.by_id["smelt-24x1-stone"]["descriptor"]
        self.assertEqual((d["input_side"], d["output"]), ("outer", "shared_centre"))
        self.assertEqual(d["rate_full"], 900.0)
        self.assertTrue(d["saturates_belt"])
        self.assertFalse(self.by_id["smelt-12x1-stone"]["descriptor"]["saturates_belt"])
        twin = self.by_id["smelt-12x2-stone"]["descriptor"]  # two shorter units: the same rate, half the length
        self.assertEqual(twin["rate_full"], d["rate_full"])
        self.assertLess(twin["length"], d["length"])
        self.assertEqual(twin["depth"], 2 * d["depth"])

    def test_unbuilt_forms_are_reported_not_chosen(self):
        why = {s["id"]: s["why"] for s in self.found["skipped"]}
        self.assertIn("does_not_generate", why["smelt-12x1-centrein-stone"])
        self.assertIn("audit", why["smelt-12x1-electric"])  # a 3 x 3 furnace in a 2 x 2 slot
        steel = self.by_id["smelt-12x1-steel"]["descriptor"]
        self.assertEqual(steel["rate_full"], 2 * self.by_id["smelt-12x1-stone"]["descriptor"]["rate_full"])
        self.assertFalse(steel["buildable"])

    def test_every_candidate_passes_the_audit_and_the_audit_catches_overlap(self):
        for c in self.found["candidates"]:
            self.assertEqual(bank.audit(c["line"]), [])
        block = bank.generate(bank.BankGenome(units=2), "iron-ore", 2)
        block["banks"][1]["entities"] = [dict(e) for e in block["banks"][0]["entities"]]
        self.assertIn("tile_overlap", bank.audit(block))

    def test_bandwidth_findings_of_the_shared_head_are_reported(self):
        d = self.by_id["smelt-12x1-stone"]["descriptor"]
        self.assertIn("side_load_before_splitter", d["bandwidth_findings"])  # the head the feed-head work replaces

    def test_selection_contexts(self):
        found = self.found["candidates"]
        district = bank.contexts()[0]
        ids = [o["id"] for o in bank.rank(found, selection.Demand(75, 600), district)]
        self.assertEqual(ids[0], "smelt-12x1-stone")  # the district's own shape when growth is expected
        self.assertNotIn("smelt-24x1-stone", ids)  # 84 tiles do not fit the 48-tile district
        long = bank.rank(found, selection.Demand(75, 600), bank.contexts()[1])
        self.assertEqual(long[0]["id"], "smelt-24x1-stone")
        steel = bank.rank(found, selection.Demand(75, 600), bank.contexts()[2])
        self.assertEqual(steel[0]["id"], "smelt-12x1-steel")

    def test_a_built_district_bank_gets_its_variant_record(self):
        record = bank.record_for({"capacity_slots": 12, "per_side_count": 3})
        self.assertEqual(record["variant"], "smelt-12x1-stone")
        self.assertEqual(record["descriptor"]["rate_now"], 112.5)


class Library(unittest.TestCase):
    def test_every_committed_entry_regenerates(self):
        data = bank.load_library()
        self.assertIsNotNone(data)
        found = bank.candidates(CATALOG, REQUEST)
        self.assertFalse([s for s in found["skipped"] if "niche" in s])
        library = {c["niche"] for c in found["candidates"] if c["source"] == "library"}
        self.assertEqual(library, set(data["entries"]))


if __name__ == "__main__":
    unittest.main()
