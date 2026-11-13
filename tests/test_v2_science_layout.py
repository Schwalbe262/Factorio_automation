"""Red science block as a layout family: genomes, audits, descriptor, library, selection, staged growth."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from test_v2_planned_products import catalog

from factorio_agent import layout_selection as selection
from factorio_agent import science_growth as sg
from factorio_agent import science_layout as sl
from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.manufacturing import lab_green_entry, lab_green_entry_outside
from factorio_agent.site.transform import Transform, apply, in_canonical
from factorio_agent.smelting import merge_entities

REPO = Path(__file__).resolve().parents[1]


def state_of(block, *, machines=2, labs=2):
    """What ``science.prepare`` saves for a block (routes and links merged into the plan)."""
    plan = _plan(merge_entities(block["science"]["entities"], block["labs"]["entities"], block["link"]["entities"]), [])
    row = {"science": block["science"], "labs": block["labs"], "plan": plan, "world_id": "w"}
    row["stage"] = sg.new_stage(row, labs=labs, machines=machines, machine_limit=block["science"]["count"])
    return row


def key(e):
    return (e["name"], e["position"]["x"], e["position"]["y"])


class GenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = catalog()

    def test_every_genome_generates_and_passes_the_audit_at_every_frame(self):
        for frame in range(1, 7):
            for genome in sl.genomes():
                with self.subTest(frame=frame, genome=genome.key()):
                    block = sl.compose(genome, frame)
                    self.assertEqual(sl.audit(self.c, block), [])
                    (x0, y0), (x1, y1) = block["reserved_area"]
                    self.assertEqual(((x1 - x0) % 2, (y1 - y0) % 2), (0, 0))  # quarter turns stay on the grid

    def test_six_distinct_candidates_one_per_niche(self):
        found = sl.candidates(self.c, sl.request_for(6, 6), library=False)["candidates"]
        self.assertEqual(len(found), 6)
        self.assertEqual(len({c["niche"] for c in found}), 6)
        self.assertEqual({c["descriptor"]["machines"] for c in found}, {"single", "direct"})
        self.assertEqual({c["descriptor"]["labs"] for c in found}, {"row", "both_sides", "chain"})
        self.assertGreater(len({tuple(c["descriptor"]["footprint"]) for c in found}), 2)

    def test_genome_round_trip_and_reserved_fields(self):
        for genome in sl.genomes():
            self.assertEqual(sl.ScienceGenome.from_dict(json.loads(json.dumps(genome.to_dict()))), genome)
        with self.assertRaisesRegex(ValueError, "reserved"):
            sl.compose(sl.ScienceGenome(modules=(("x", ("speed-module",)),)), 4)

    def test_single_row_keeps_the_template_geometry(self):
        from factorio_agent.science import science_block

        block = sl.compose(sl.ScienceGenome("single", "row"), 6)
        legacy = science_block(0, 0, 6)
        self.assertEqual(block["science"]["entities"], legacy["science"]["entities"])
        self.assertEqual(block["labs"]["entities"], legacy["labs"]["entities"])
        # The pack link takes the column the routed link took: one east of the 32-tile block.
        self.assertEqual({e["position"]["x"] for e in block["link"]["entities"] if e["direction"] == 0}, {32.5})


class AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = catalog()

    def broken(self, genome, change):
        block = deepcopy(sl.compose(genome, 4))
        change(block)
        return sl.audit(self.c, block)

    def test_a_missing_lab_belt_strands_labs(self):
        def cut(block):
            belts = [e for e in block["labs"]["entities"] if e["name"] == "transport-belt"]
            block["labs"]["entities"].remove(belts[len(belts) // 2])

        findings = self.broken(sl.ScienceGenome("single", "row"), cut)
        self.assertTrue(any(f.startswith("lab_unreachable") or f.startswith("inserter_end_free") for f in findings))

    def test_a_chain_lab_without_its_feeding_lab_is_unreachable(self):
        def drop(block):
            labs = block["labs"]["entities"]
            arm = next(e for e in labs if e["name"] == "inserter" and e["position"]["y"] > labs[0]["position"]["y"])
            arm["direction"] = 8  # now takes from the second-row lab

        findings = self.broken(sl.ScienceGenome("single", "chain"), drop)
        self.assertTrue(any(f.startswith("lab_unreachable") for f in findings), findings)

    def test_a_missing_pole_is_found_in_every_stage(self):
        def pull(block):
            block["science"]["entities"] = [
                e
                for i, e in enumerate(block["science"]["entities"])
                if not (e["name"] == "small-electric-pole" and i < 10)
            ]

        findings = self.broken(sl.ScienceGenome("direct", "row"), pull)
        self.assertTrue(any("unpowered" in f or "machine_slots_unreadable" in f for f in findings), findings)

    def test_packs_on_the_green_lane_or_a_side_load_fail(self):
        def turn(block):
            link = block["link"]["entities"]
            link[-1]["direction"] = 8  # the corner into the lab head becomes a dead end

        findings = self.broken(sl.ScienceGenome("single", "row"), turn)
        self.assertTrue(any(f.startswith("packs_do_not_reach_labs") for f in findings), findings)

    def test_growth_slots_never_need_a_built_entity_moved(self):
        for genome in sl.genomes():
            block = sl.compose(genome, 5)
            slots = sg.machine_slots(block["science"])
            built = {t for e in sl.stage_entities(block, 2, 2) for t in sl._tiles(e)}
            for group in slots[2:]:
                self.assertFalse({t for e in group for t in sl._tiles(e)} & built, genome.key())

    def test_green_entry_fits_every_lab_arrangement_in_every_orientation(self):
        for genome in sl.genomes():
            block = sl.compose(genome, 4)
            self.assertTrue(sl.green_entry_fits(block), genome.key())
            entry = lab_green_entry(block["labs"])
            head = block["labs"]["ports"][0]["position"]
            self.assertEqual((entry["join"]["x"], entry["join"]["y"]), (head["x"], head["y"]))
            for t in (Transform(4, None, (16, 16)), Transform(8, "x", (16, 16))):
                labs = dict(apply(t, block["labs"]), site_transform=t.to_dict())
                self.assertIn("port", in_canonical(labs, lab_green_entry_outside))

    def test_only_the_lab_row_takes_the_blue_extension(self):
        for genome in sl.genomes():
            block = sl.compose(genome, 4)
            self.assertEqual(sl.blue_extension_fits(block), genome.labs == "row", genome.key())


class DescriptorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = catalog()
        cls.by = {
            c["descriptor"]["id"]: c for c in sl.candidates(cls.c, sl.request_for(6, 6), library=False)["candidates"]
        }

    def test_rates_growth_and_counts(self):
        for c in self.by.values():
            d, block = c["descriptor"], c["line"]
            self.assertEqual(d["rate_now"], 12.0)  # two pack machines at the start
            self.assertEqual(d["rate_full"], 36.0)
            self.assertEqual(d["growth"]["slots"], 4)
            self.assertEqual(d["growth"]["lab_slots"], 6)
            self.assertEqual(d["counts_full"]["entities"], len(sl.entities(block)))
            self.assertLess(d["counts"]["entities"], d["counts_full"]["entities"])
            self.assertEqual(d["plates_per_minute_full"], {"copper-plate": 36.0, "iron-plate": 72.0})
            self.assertIn("power_kw_full", d)
            self.assertIsNone(d["pollution_per_min"])  # the test catalog exports no emissions
        # The single row's head gear arm caps it below the frame (30/min plain inserter budget).
        self.assertEqual(self.by["sci-single-row"]["descriptor"]["growth"]["limit"], "gear_output_inserter")
        self.assertEqual(self.by["sci-direct-row"]["descriptor"]["growth"]["limit"], "frame_slots")
        self.assertEqual(self.by["sci-single-row"]["descriptor"]["rebuild_ahead"], 0.0)
        self.assertGreater(self.by["sci-single-bothsides"]["descriptor"]["rebuild_ahead"], 0)
        self.assertEqual(self.by["sci-direct-row"]["descriptor"]["input_sides"], ["W"])

    def test_record_keeps_the_science_descriptor(self):
        c = self.by["sci-direct-chain"]
        option = selection.option(c, selection.Demand(6, 12), 3.0)
        record = selection.record([option], option, config=selection.Settings(), pool_size=1, tick=60)
        self.assertEqual(record["descriptor"]["labs"], "chain")
        self.assertEqual(record["genome"]["machines"], "direct")
        self.assertIn("rebuild_ahead", record["terms"])


class SelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = catalog()

    def ranked(self, frame, ctx, demand):
        found = sl.candidates(self.c, sl.request_for(6, frame), library=False)["candidates"]
        return sl.rank_for_context(found, frame, demand, ctx)

    def test_site_context_changes_the_choice(self):
        demand = selection.demand_for(6, tick=0)
        square = self.ranked(6, selection.SiteContext("square", 40, 40, "W", "E"), demand)
        narrow = self.ranked(6, selection.SiteContext("narrow", 26, 44, "W", "E"), demand)
        self.assertEqual(square[0]["id"], "sci-single-row")  # the proven row where it fits
        self.assertEqual(narrow[0]["candidate"]["descriptor"]["machines"], "direct")
        shallow = self.ranked(2, selection.SiteContext("shallow", 44, 28, "W", "E"), demand)
        self.assertEqual(shallow[0]["candidate"]["descriptor"]["labs"], "both_sides")

    def test_blue_rebuild_keeps_the_lab_row_by_default(self):
        demand = selection.demand_for(6, tick=0)
        for frame in (2, 4, 6):
            top = self.ranked(frame, selection.SiteContext("roomy", 48, 48, "W", "E"), demand)[0]
            self.assertEqual(top["candidate"]["descriptor"]["labs"], "row", frame)

    def test_demand_above_the_gear_arm_ceiling_favours_the_direct_pairs(self):
        roomy = selection.SiteContext("roomy", 48, 48, "W", "E")
        low = self.ranked(6, roomy, selection.Demand(6, 6))
        high = self.ranked(6, roomy, selection.Demand(6, 60, plates_scarce=False))
        self.assertEqual(low[0]["id"], "sci-single-row")
        self.assertEqual(high[0]["id"], "sci-direct-row")

    def test_exploration_picks_among_close_candidates_deterministically(self):
        options = self.ranked(6, selection.SiteContext("roomy", 48, 48, "W", "E"), selection.demand_for(6, tick=0))
        scores = [o["score"] for o in options]
        on = selection.Settings(explore=True, top_k=3, margin=0.15)
        seed = selection.seed_for("world-1", "automation_science")
        self.assertEqual(selection.choose(scores, on, seed), selection.choose(scores, on, seed))
        self.assertEqual(selection.choose(scores, selection.Settings(), seed)[0], 0)


class GrowthTests(unittest.TestCase):
    def test_staged_growth_works_on_every_variant(self):
        for genome in sl.genomes():
            with self.subTest(genome=genome.key()):
                block = sl.compose(genome, 5)
                row = state_of(block)
                stage = row["stage"]
                self.assertEqual((stage["machine_slots"], stage["lab_slots"]), (5, 8))
                self.assertEqual(sg.frame_capacity(row), 5)  # a generated frame is never widened
                self.assertIs(sg.extended_state(row, 5), row)
                plan = sg.build_plan(row)
                names = [e["name"] for e in plan["entities"]]
                self.assertEqual(names.count("lab"), 2)
                self.assertEqual(sum(e.get("recipe") == sl.RED for e in plan["entities"]), 2)
                grown = sg.build_plan(row, labs=8, machines=5)
                self.assertEqual(len(grown["entities"]), len(row["plan"]["entities"]))
                self.assertEqual(len(sg.built_machines(row)), 2)
                deferred = {key(e) for e in stage["growth_entities"]}
                self.assertFalse(deferred & {key(e) for e in plan["entities"]})

    def test_direct_pairs_build_the_gear_machine_with_the_first_pack_machine(self):
        row = state_of(sl.compose(sl.ScienceGenome("direct", "row"), 4), machines=1)
        recipes = [e.get("recipe") for e in sg.build_plan(row)["entities"] if e.get("recipe")]
        self.assertEqual(sorted(recipes), sorted([sl.GEAR, sl.RED]))


class PrepareTests(unittest.TestCase):
    def test_prepare_chooses_a_variant_records_it_and_brings_its_pack_link(self):
        from test_v2_green_site import KINDS, World

        with tempfile.TemporaryDirectory() as directory:
            world = World(Path(directory), KINDS[0])
            layout = world.red["layout"]
            self.assertTrue(layout["variant"].startswith("sci-"))
            self.assertEqual(layout["chosen_tick"], 100)
            self.assertIn("port", layout["terms"])
            genome = sl.ScienceGenome.from_dict(layout["genome"])
            self.assertEqual(world.red["science"]["count"], world.red["stage"]["machine_slots"])
            # The generated link is planned (no routed science -> lab connection).
            self.assertEqual(sorted(c["item"] for c in world.red["connections"]), ["copper-plate", "iron-plate"])
            column = [e for e in world.red["plan"]["entities"] if e["name"] == "transport-belt"]
            self.assertGreater(
                len(column), len([e for e in world.red["science"]["entities"] if e["name"] == "transport-belt"])
            )
            self.assertIn(genome.machines, sl.MACHINE_FORMS)


class LibraryTests(unittest.TestCase):
    def test_committed_section_regenerates_and_passes_the_audit(self):
        doc = json.loads((REPO / "data" / "layouts" / "automation-science-pack.json").read_text(encoding="utf-8"))
        section = doc[sl.SECTION]
        self.assertEqual(section["generator"], sl.GENERATOR_VERSION)
        self.assertGreaterEqual(len(section["entries"]), 6)
        c = catalog()
        request = sl.request_for(section["request"]["rate"], section["request"]["frame"])
        for niche, entry in section["entries"].items():
            with self.subTest(niche=niche):
                self.assertLess(len(json.dumps(entry)), 1600)
                candidate, why = sl.evaluate(c, sl.ScienceGenome.from_dict(entry["genome"]), request)
                self.assertIsNotNone(candidate, why)

    def test_library_entries_are_used_and_a_broken_one_is_skipped(self):
        c = catalog()
        request = sl.request_for(6, 4)
        with tempfile.TemporaryDirectory() as tmp:
            section = sl.build_section(c, request)
            section["entries"]["broken"] = {"genome": {"form": "science", "machines": "twin", "labs": "row"}}
            path = sl.library_path(Path(tmp))
            path.write_text(json.dumps({"entries": {}, sl.SECTION: section}), encoding="utf-8")
            got = sl.candidates(c, request, directory=Path(tmp))
            self.assertIn("broken", [s.get("niche") for s in got["skipped"]])
            self.assertTrue(all(x["source"] == "library" for x in got["candidates"]))

    def test_legacy_record_names_the_template(self):
        record = sl.legacy_record({"science": {}, "labs": {}})
        self.assertEqual(record["variant"], "sci-single-row-template")
        self.assertEqual(record["source"], "built")


if __name__ == "__main__":
    unittest.main()
