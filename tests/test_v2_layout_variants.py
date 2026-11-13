"""Candidate layouts: genomes, descriptors, niche archive, library, selection and exploration."""

import json
import tempfile
import unittest
from pathlib import Path

from test_v2_planned_products import catalog, enabled

from factorio_agent import layout_selection as selection
from factorio_agent import layout_variants as variants
from factorio_agent.blocks.cluster_audit import layout_findings
from factorio_agent.production_layout import cluster_line, rows

PLATES = ("iron-plate", "copper-plate", "steel-plate")
REPO = Path(__file__).resolve().parents[1]


def request(c, item, rate, sourced=PLATES):
    return dict(
        product=item,
        rate=rate,
        inserter="inserter",
        enabled=enabled(c),
        sourced=frozenset(sourced).__contains__,
        chest="iron-chest",
    )


def found(c, item, rate, sourced=PLATES):
    return variants.candidates(c, request(c, item, rate, sourced), library=False)["candidates"]


class CandidateSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = catalog()

    def test_several_distinct_audited_shapes_per_item(self):
        for item, rate, least in (
            ("transport-belt", 30, 4),
            ("firearm-magazine", 10, 4),
            ("engine-unit", 6, 3),
            ("automation-science-pack", 6, 4),
        ):
            with self.subTest(item=item):
                cands = found(self.c, item, rate)
                self.assertGreaterEqual(len(cands), least)
                ids = {x["descriptor"]["id"] for x in cands}
                self.assertEqual(len(ids), len(cands))
                self.assertEqual(len({x["niche"] for x in cands}), len(cands))  # one per niche
                self.assertGreater(len({tuple(x["descriptor"]["footprint"]) for x in cands}), 1)
                self.assertEqual({x["descriptor"]["form"] for x in cands}, {"row", "cluster"})
                for x in cands:
                    self.assertEqual(variants.audit(self.c, x["line"]), [], x["descriptor"]["id"])

    def test_green_science_and_inserter_have_one_cluster_topology(self):
        # The pack machine needs two belt-fed neighbours and a free side (only the 4 x 2 turned
        # grid); the inserter's tree is a chain. Rows cannot carry either tree.
        for item, rate in (("logistic-science-pack", 5), ("inserter", 30)):
            with self.subTest(item=item):
                cands = found(self.c, item, rate, ("iron-plate", "copper-plate"))
                self.assertGreaterEqual(len(cands), 1)
                for x in cands:
                    self.assertEqual(x["descriptor"]["form"], "cluster")
                    self.assertEqual(variants.audit(self.c, x["line"]), [])

    def test_generation_is_deterministic_and_regenerates_from_the_genome(self):
        req = request(self.c, "transport-belt", 30)
        first = variants.candidates(self.c, req, library=False)["candidates"]
        second = variants.candidates(self.c, req, library=False)["candidates"]
        self.assertEqual([x["descriptor"] for x in first], [x["descriptor"] for x in second])
        for x in first:
            genome = variants.Genome.from_dict(json.loads(json.dumps(x["genome"].to_dict())))
            self.assertEqual(genome, x["genome"])
            again = variants.generate(self.c, genome, **variants.request_args(req))
            self.assertEqual(again["entities"], x["line"]["entities"])

    def test_bounded_genome_space(self):
        for item in ("transport-belt", "logistic-science-pack", "engine-unit"):
            req = request(self.c, item, 10)
            pool = variants.genomes(self.c, product=item, enabled=req["enabled"], sourced=req["sourced"])
            self.assertLessEqual(len(pool), variants.MAX_GENOMES)

    def test_reserved_module_and_beacon_fields_are_refused_until_placed(self):
        genome = variants.Genome("row", "assembling-machine-1", modules=(("pipe", ("speed-module",)),))
        self.assertEqual(variants.Genome.from_dict(genome.to_dict()), genome)
        with self.assertRaisesRegex(ValueError, "reserved"):
            variants.generate(self.c, genome, **variants.request_args(request(self.c, "pipe", 10)))
        self.assertEqual(variants.Genome("row", "m").to_dict()["modules"], {})


class DescriptorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = catalog()
        cls.by_id = {x["descriptor"]["id"]: x for x in found(cls.c, "firearm-magazine", 10)}

    def test_counts_footprint_rate_and_power_match_the_line(self):
        for x in self.by_id.values():
            d, line = x["descriptor"], x["line"]
            names = [e["name"] for e in line["entities"]]
            self.assertEqual(d["counts"]["entities"], len(names))
            self.assertEqual(d["counts"]["belts"], names.count("transport-belt"))
            self.assertEqual(d["counts"]["inserters"], names.count("inserter") + names.count("long-handed-inserter"))
            self.assertEqual(d["counts"]["machines"], sum(1 for e in line["entities"] if e.get("recipe")))
            (x0, y0), (x1, y1) = line["reserved_area"]
            self.assertEqual(d["footprint"], [x1 - x0, y1 - y0])
            self.assertEqual(d["rate_now"], round(min(line["rated_per_minute"], line["stock_rate_per_minute"]), 3))
            power = sum(
                self.c.entities[n].get("energy_usage", 0) * 60 / 1000
                for n in names
                if self.c.entities[n].get("electric")
            )
            self.assertAlmostEqual(d["power_kw"], round(power, 1))
            self.assertIsNone(d["pollution_per_min"])  # the test catalog exports no emissions
            self.assertGreater(d["raw_total"], 0)

    def test_growth_ceiling_rows_and_clusters(self):
        row = next(x for x in self.by_id.values() if x["descriptor"]["id"].endswith("-default"))
        main = row["line"]["main"]
        g = row["descriptor"]["growth"]
        self.assertEqual(g["kind"], "in_place")
        self.assertEqual(g["slots"], main["growth"]["remaining_slots"])
        per = main["rated_per_minute"] / main["count"]
        arm = 30.0  # the plain inserter of the stock tail caps the in-place rate
        self.assertAlmostEqual(g["max_rate_in_place"], min(per * (main["count"] + g["slots"]), arm), places=3)
        cluster = next(x for x in self.by_id.values() if x["descriptor"]["form"] == "cluster")
        self.assertEqual(cluster["descriptor"]["growth"]["kind"], "none")
        self.assertEqual(cluster["descriptor"]["growth"]["slots"], 0)

    def test_port_relations_cover_same_opposite_adjacent(self):
        relations = {x["descriptor"]["port_relation"] for x in self.by_id.values()}
        self.assertEqual(relations, {"same", "opposite", "adjacent"})


class SelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = catalog()
        cls.req = request(cls.c, "firearm-magazine", 10)
        cls.cands = variants.candidates(cls.c, cls.req, library=False)["candidates"]

    def best(self, demand, ctx):
        return selection.rank_for_context(self.c, self.cands, self.req, demand, ctx)[0]

    def test_port_sides_change_the_choice(self):
        low = selection.Demand(10, 10)
        back = self.best(low, selection.SiteContext("back", 40, 40, "W", "W"))
        across = self.best(low, selection.SiteContext("across", 40, 40, "W", "E"))
        self.assertNotEqual(back["id"], across["id"])
        self.assertEqual(back["candidate"]["descriptor"]["port_relation"], "same")

    def test_low_and_high_demand_change_the_choice(self):
        ctx = selection.TYPICAL_CONTEXTS[0]
        low = self.best(selection.Demand(10, 10), ctx)
        high = self.best(selection.Demand(10, 40, plates_scarce=False), ctx)
        self.assertEqual(low["candidate"]["descriptor"]["form"], "cluster")
        self.assertEqual(high["candidate"]["descriptor"]["growth"]["kind"], "in_place")

    def test_narrow_allocation_drops_what_does_not_fit(self):
        deep = selection.rank_for_context(
            self.c, self.cands, self.req, selection.Demand(10, 10), selection.SiteContext("deep", 40, 40)
        )
        narrow = selection.rank_for_context(
            self.c, self.cands, self.req, selection.Demand(10, 10), selection.SiteContext("narrow", 12, 8)
        )
        self.assertLess(len(narrow), len(deep))
        for o in narrow:
            self.assertLessEqual(min(o["candidate"]["descriptor"]["footprint"]), 8)

    def test_power_and_pollution_weights_are_off_by_default(self):
        d = dict(self.cands[0]["descriptor"], power_kw=100.0)
        total, terms = selection.score(d, selection.Demand(10, 20))
        self.assertNotIn("power", terms)
        tight = selection.score(d, selection.Demand(10, 20, power_budget_kw=1.0))[1]
        self.assertGreater(tight["over_budget"], 0)
        self.assertGreater(selection.score(d, selection.Demand(10, 20, w_power=1.0))[0], total)


class ExplorationTests(unittest.TestCase):
    def test_off_by_default_and_deterministic_within_the_margin(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(selection.settings(Path(tmp), env={}).explore)
            self.assertTrue(selection.settings(Path(tmp), env={selection.ENV_EXPLORE: "1"}).explore)
            (Path(tmp) / selection.SETTINGS_FILE).write_text('{"explore": false}', encoding="utf-8")
            self.assertFalse(selection.settings(Path(tmp), env={selection.ENV_EXPLORE: "1"}).explore)
        scores = [100.0, 104.0, 108.0, 200.0]
        off = selection.Settings()
        self.assertEqual({selection.choose(scores, off, s)[0] for s in range(50)}, {0})
        on = selection.Settings(explore=True, top_k=4, margin=0.1)
        picks = {selection.choose(scores, on, s)[0] for s in range(50)}
        self.assertEqual(picks, {0, 1, 2})  # 200 is outside the margin
        for seed in (1, 7, 12345):
            self.assertEqual(selection.choose(scores, on, seed), selection.choose(scores, on, seed))
        self.assertEqual(selection.seed_for("w", "x"), selection.seed_for("w", "x"))


class LibraryTests(unittest.TestCase):
    def test_every_committed_entry_regenerates_and_passes_the_audit(self):
        c = catalog()
        files = sorted((REPO / "data" / "layouts").glob("*.json"))
        self.assertTrue(files)
        checked = 0
        for path in files:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("item") in ("mining-patch", "smelting-bank"):
                continue  # another family's library (tests/test_v2_mine_layout.py, test_v2_smelting_layout.py)
            self.assertEqual(doc["generator"], variants.GENERATOR_VERSION, path.name)
            if c.recipe_for_product(doc["item"]) is None:
                continue  # the fake catalog does not know this item
            req = request(c, doc["item"], doc["request"]["rate"], doc["request"]["sourced"])
            req["enabled"] = sorted(set(req["enabled"]) | set(variants.LIBRARY_MACHINES))
            for key, entry in doc["entries"].items():
                with self.subTest(item=doc["item"], niche=key):
                    self.assertLess(len(json.dumps(entry)), 1600)
                    candidate, why = variants.evaluate(c, variants.Genome.from_dict(entry["genome"]), req)
                    if candidate is None and why.startswith("does_not_generate") and "not enabled" in why:
                        continue  # a machine the fake catalog lacks
                    self.assertIsNotNone(candidate, why)
                    checked += 1
        self.assertGreater(checked, 10)

    def test_library_entries_are_used_and_a_broken_one_is_skipped(self):
        c = catalog()
        req = request(c, "firearm-magazine", 10)
        with tempfile.TemporaryDirectory() as tmp:
            doc = variants.build_library(c, req)
            first = next(iter(doc["entries"]))
            doc["entries"]["broken"] = {
                "genome": {
                    "form": "cluster",
                    "machine": "assembling-machine-1",
                    "grid": [9, 9],
                    "cells": {},
                    "output_side": "left",
                }
            }
            variants.library_path("firearm-magazine", Path(tmp)).write_text(json.dumps(doc), encoding="utf-8")
            got = variants.candidates(c, req, directory=Path(tmp))
            self.assertEqual(got["library"], "firearm-magazine.json")
            self.assertEqual([s["niche"] for s in got["skipped"]], ["broken"])
            self.assertTrue(any(x["source"] == "library" and x["niche"] == first for x in got["candidates"]))


class RecordTests(unittest.TestCase):
    def test_fleet_section_reads_layout_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {
                "item": "pipe",
                "phase": "verified",
                "layout": {
                    "variant": "row-am1-default",
                    "chosen_tick": 600,
                    "verified_tick": 6600,
                    "explored": True,
                    "descriptor": {"power_kw": 150.0, "pollution_per_min": None},
                },
            }
            (Path(tmp) / "production-row-pipe.json").write_text(json.dumps(state), encoding="utf-8")
            found_rows = selection.run_layouts(Path(tmp), milestones={"main_bus": 1.0, "logistic_science": 3.5})
            self.assertEqual(found_rows[0]["build_seconds"], 100)
            self.assertEqual(found_rows[0]["followup_seconds"], 100)  # proof at 110 s, next milestone at 210 s
            lines = selection.format_variants(found_rows + found_rows)
            self.assertIn("layout variants used", lines[0])
            self.assertEqual(lines[1].strip(), "produce:pipe")
            self.assertIn("row-am1-default", lines[2])
            self.assertIn("2/2", lines[2])

    def test_variants_of_one_block_are_compared_and_the_leader_named(self):
        rows = []
        for variant, proofs, follow in (
            ("sci-single-row", (1, 1, 1), (300, 320, 340)),
            ("sci-direct-row", (1, 0, 1), (200, None, 220)),
        ):
            for proof, seconds in zip(proofs, follow):
                rows.append(
                    {
                        "block": "automation_science",
                        "variant": variant,
                        "explored": variant == "sci-direct-row",
                        "proof": bool(proof),
                        "build_seconds": 400,
                        "followup_seconds": seconds,
                        "power_kw": 477.3,
                        "pollution_per_min": None,
                    }
                )
        summary = selection.variant_summary(rows)
        single = summary[("automation_science", "sci-single-row")]
        self.assertEqual((single["proof_rate"], single["median_followup_seconds"]), (1.0, 320))
        self.assertEqual(summary[("automation_science", "sci-direct-row")]["median_followup_seconds"], 210)
        lines = selection.format_variants(rows)
        self.assertEqual(lines[1].strip(), "automation_science")
        self.assertIn("-> leading: sci-single-row", lines[-1])  # every proof passed beats the quicker follow-up

    def test_science_records_and_a_template_block_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            red = {"science": {"template": "permanent_red_science_row"}, "labs": {}, "stage": {}}
            (Path(tmp) / "automation-science.json").write_text(json.dumps(red), encoding="utf-8")
            rows = selection.run_layouts(Path(tmp))
            self.assertEqual(rows[0]["block"], "automation_science")
            self.assertEqual(rows[0]["variant"], "sci-single-row-template")
            self.assertFalse(rows[0]["proof"])
            red["layout"] = {"variant": "sci-direct-row", "chosen_tick": 3600, "verified_tick": 3600 * 3}
            (Path(tmp) / "automation-science.json").write_text(json.dumps(red), encoding="utf-8")
            rows = selection.run_layouts(Path(tmp), milestones={"research_logistics": 5.0, "main_bus": 4.0})
            self.assertEqual(
                (rows[0]["proof"], rows[0]["build_seconds"], rows[0]["followup_seconds"]), (True, 120, 120)
            )

    def test_cluster_line_pins_a_genome_and_rejects_a_bad_one(self):
        c = catalog()
        req = request(c, "transport-belt", 30)
        line = cluster_line(
            c,
            "transport-belt",
            30,
            inserter="inserter",
            enabled=req["enabled"],
            sourced=req["sourced"],
            chest="iron-chest",
            grid=(1, 2),
            turned=True,
        )
        self.assertEqual(line["main"]["grid"]["rows"], 2)
        for row in rows(line):
            self.assertEqual(layout_findings(c, row), [])
        with self.assertRaises(ValueError):
            cluster_line(
                c,
                "transport-belt",
                30,
                inserter="inserter",
                enabled=req["enabled"],
                sourced=req["sourced"],
                chest="iron-chest",
                grid=(5, 3),
            )


if __name__ == "__main__":
    unittest.main()
