"""Mining patch layout family (mine_layout): genomes, frames, audit, descriptor, selection, library."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from factorio_agent import mine_layout as mine
from factorio_agent.mining_layout_policy import make_policy, patch_grid

CATALOG = SimpleNamespace(entities={}, fingerprint="c")


def drills_of(candidate):
    return {
        (e["position"]["x"], e["position"]["y"]) for e in candidate["line"]["entities"] if e["name"] == mine.ELECTRIC
    }


class Family(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = mine.reference_snapshot(ragged=True)
        cls.found = mine.candidates(CATALOG, cls.snapshot, library=False)
        cls.by_id = {c["descriptor"]["id"]: c for c in cls.found["candidates"]}

    def test_every_genome_generates_and_passes_the_audit(self):
        self.assertEqual(self.found["skipped"], [])
        self.assertEqual(len(self.found["candidates"]), self.found["generated"])
        for c in self.found["candidates"]:
            self.assertEqual(mine.audit(self.snapshot, c["line"], 30.0), [], c["descriptor"]["id"])

    def test_simple_is_the_patch_grid_and_the_spacings_trade_drills_for_coverage(self):
        simple = self.by_id["mine-simple-E-sat-s1"]
        grid = patch_grid(self.snapshot, make_policy("reference", "reference", "iron-ore"))
        grid_drills = {(d["x"], d["y"]) for row in grid["rows"] for d in row["drills"]}
        self.assertEqual(drills_of(simple), grid_drills)  # the same MPP "simple" frame the banks use
        compact, sparse = self.by_id["mine-compact-E-sat-s1"], self.by_id["mine-sparse-E-sat-s1"]
        counts = [c["descriptor"]["drills_full"] for c in (sparse, simple, compact)]
        self.assertEqual(counts, sorted(counts))  # sparse fewest drills, compact most
        self.assertLess(counts[0], counts[1])
        self.assertGreater(compact["descriptor"]["counts_full"]["undergrounds"], 0)  # poles on the collector
        self.assertEqual(simple["descriptor"]["counts_full"]["undergrounds"], 0)

    def test_descriptor_and_merge_plan(self):
        d = self.by_id["mine-simple-E-sat-s1"]["descriptor"]
        for key in (
            "ore_covered_tiles",
            "drills_full",
            "rate_now",
            "rate_full",
            "power_kw",
            "frame_rects",
            "growth_steps",
        ):
            self.assertIn(key, d)
        self.assertEqual(d["rate_now"], 60.0)  # one full pair at 30/min per drill
        self.assertEqual(sum(d["growth_steps"]), d["drills_full"])
        self.assertTrue(d["buildable"])
        single = self.by_id["mine-simple-E-sin-s1"]["descriptor"]
        self.assertEqual(single["trunk"]["belts"], 1)
        self.assertLessEqual(single["rate_full"], 900.0)  # one yellow belt
        self.assertGreater(d["trunk"]["belts"], 1)  # saturation opens more trunk belts
        self.assertFalse(single["buildable"] or self.by_id["mine-sparse-E-sat-s1"]["descriptor"]["buildable"])

    def test_merge_by_saturation_and_the_two_belt_splitter(self):
        # Three pairs per row: 90/min on each collector lane, 180/min per row.
        row = {"drills": [{"position": {"x": 0.5, "y": 0.5}, "direction": d} for d in (8, 0)] * 3, "columns": [0.5]}
        plan = mine.merge_plan([deepcopy(row) for _ in range(3)], 30.0, 450.0, "saturate")
        # The head row joins head-on (90 + 90), two side-loads fill the near lane to 450.
        self.assertEqual((plan["belts"], plan["balancer"]), (1, "none"))
        plan = mine.merge_plan([deepcopy(row) for _ in range(4)], 30.0, 450.0, "saturate")
        self.assertEqual((plan["belts"], plan["balancer"]), (2, "2-2 splitter"))
        self.assertEqual(plan["clipped_per_minute"], 0)
        single = mine.merge_plan([deepcopy(row) for _ in range(6)], 30.0, 450.0, "single")
        self.assertGreater(single["clipped_per_minute"], 0)

    def test_the_audit_rejects_overlap_and_unpowered_drills(self):
        line = deepcopy(self.by_id["mine-simple-E-sat-s1"]["line"])
        drill = next(e for e in line["entities"] if e["name"] == mine.ELECTRIC)
        line["entities"].append(deepcopy(drill))
        self.assertIn("entity_overlap", mine.audit(self.snapshot, line, 30.0))
        line = deepcopy(self.by_id["mine-simple-E-sat-s1"]["line"])
        line["rows"][0]["poles"] = []
        self.assertIn("drill_unpowered", mine.audit(self.snapshot, line, 30.0))

    def test_reservations_and_neighbouring_ore_stay_out_of_the_frame(self):
        reserved = [[[30, 0], [40, 8]]]
        snap = deepcopy(self.snapshot)
        snap["tiles"] += [{"name": "stone", "position": {"x": x + 0.5, "y": 30.5}, "amount": 500} for x in range(40)]
        found = mine.candidates(CATALOG, snap, reserved, edges=("west_to_east",), starts=(1,), library=False)
        self.assertTrue(found["candidates"])
        for c in found["candidates"]:
            tiles = set()
            for r in c["descriptor"]["frame_rects"]:
                tiles |= {
                    (x + 0.5, y + 0.5)
                    for x in range(int(r[0][0]), int(r[1][0]))
                    for y in range(int(r[0][1]), int(r[1][1]))
                }
            self.assertFalse({t for t in tiles if 30 <= t[0] <= 40 and 0 <= t[1] <= 8})
            self.assertFalse({t for t in tiles if t[1] == 30.5})

    def test_selection_contexts(self):
        cands = self.found["candidates"]
        east = mine.rank(cands, mine.demand_for(60, tick=0), mine.consumer_point(self.snapshot, "E"))
        self.assertEqual(east[0]["candidate"]["descriptor"]["edge"], "E")  # the edge facing the consumer
        west = mine.rank(cands, mine.demand_for(120, tick=0), mine.consumer_point(self.snapshot, "W"))
        self.assertEqual(west[0]["candidate"]["descriptor"]["edge"], "W")
        self.assertEqual(west[0]["candidate"]["genome"].start_pairs, 2)  # 120/min needs two pairs now


class RuntimeChoice(unittest.TestCase):
    def test_default_is_the_simple_buildable_genome_at_the_requested_start(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        snapshot = mine.reference_snapshot()
        chosen = mine.choose_bank(CATALOG, snapshot, [], flow="south_to_north", pairs=1, root=Path(temp.name))
        self.assertEqual(chosen["record"]["variant"], "mine-simple-N-sat-s1")
        self.assertEqual(chosen["start_pairs"], 1)
        self.assertFalse(chosen["record"]["explored"])
        self.assertEqual(chosen["record"]["runner_ups"][0]["variant"], "mine-simple-N-sat-s2")

    def test_exploration_may_take_the_build_ahead_start(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        (Path(temp.name) / "layout-settings.json").write_text(json.dumps({"explore": True}), encoding="utf-8")
        chosen = mine.choose_bank(CATALOG, mine.reference_snapshot(), [], flow="west_to_east", pairs=1, root=temp.name)
        self.assertEqual(chosen["record"]["pool"], 2)
        self.assertIn(chosen["start_pairs"], (1, 2))
        self.assertEqual(chosen["record"]["explored"], chosen["start_pairs"] == 2)


class Library(unittest.TestCase):
    def test_every_committed_entry_regenerates_and_passes_the_audit(self):
        data = mine.load_library()
        self.assertIsNotNone(data)
        self.assertEqual(data["generator"], mine.GENERATOR_VERSION)
        found = mine.candidates(CATALOG, mine.reference_snapshot(), edges=("west_to_east",), starts=(1,))
        sources = {mine.library_niche(c["genome"]): c["source"] for c in found["candidates"]}
        for key, entry in data["entries"].items():
            genome = mine.MineGenome.from_dict(entry["genome"])
            self.assertEqual(mine.library_niche(genome), key)
            self.assertEqual(sources.get(key), "library", key)


if __name__ == "__main__":
    unittest.main()
