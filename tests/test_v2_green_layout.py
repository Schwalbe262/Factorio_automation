"""Green science candidates: the direct-insertion cluster and the split row (sub-clusters on a shared belt)."""

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from test_v2_planned_products import catalog

from factorio_agent import green_layout as gl
from factorio_agent import layout_selection as selection
from factorio_agent.blocks.cluster_audit import audit_cluster
from factorio_agent.green_science import green_cluster


class SplitGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = catalog()

    def test_split_generates_and_passes_its_audit_at_every_frame(self):
        for frame in range(1, 7):
            with self.subTest(frame=frame):
                plan = gl.split_block(self.c, frame)
                self.assertEqual(gl.audit_split(self.c, plan), [])
                self.assertEqual(sum(e.get("recipe") == gl.PACK for e in plan["entities"]), gl.START)
                self.assertEqual(len(plan["growth_slots"]), frame - gl.START)
                (x0, y0), (x1, y1) = plan["reserved_area"]
                self.assertEqual(((x1 - x0) % 2, (y1 - y0) % 2), (0, 0))
                self.assertEqual({p["item"] for p in plan["ports"] if p["direction"] == "input"}, set(gl.PLATES))

    def test_origin_moves_the_reservation_corner(self):
        plan = gl.split_block(self.c, 4, origin=(10, -20))
        self.assertEqual(plan["reserved_area"][0], [10, -20])
        self.assertEqual(gl.audit_split(self.c, plan), [])

    def test_intermediates_ride_their_own_lanes_and_a_broken_arm_is_found(self):
        plan = gl.split_block(self.c, 4)
        self.assertEqual(gl.audit_split(self.c, plan), [])
        broken = deepcopy(plan)
        pack = next(m for m in broken["entities"] if m.get("recipe") == gl.PACK)["position"]
        feed = next(e for e in broken["entities"] if e["position"] == {"x": pack["x"], "y": pack["y"] + 2})
        feed["direction"] = 4  # the pack machine's feed arm turned sideways
        self.assertTrue(gl.audit_split(self.c, broken))

    def test_sub_clusters_are_the_cluster_generator_and_pass_its_audit(self):
        from factorio_agent.blocks.cluster import cluster_block

        rate = 6 * 5.0
        for item in ("inserter", "transport-belt"):
            plan = cluster_block(self.c, item, rate, sourced=gl.PLATES.__contains__, chest=None, output_side="bottom")
            self.assertEqual(audit_cluster(self.c, plan), [], item)


class DescriptorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = catalog()
        cls.cluster = gl.describe(cls.c, gl.GreenGenome("cluster"), green_cluster(cls.c, (0.5, 0.5)), 5)
        cls.split = gl.describe(cls.c, gl.GreenGenome("split"), gl.split_block(cls.c), 5)

    def test_growth_stories_differ(self):
        self.assertEqual(self.cluster["growth"]["kind"], "none")
        self.assertEqual(self.cluster["growth"]["beyond"], "second_cluster_or_rebuild")
        self.assertEqual(self.split["growth"]["kind"], "in_place")
        self.assertEqual(self.split["growth"]["slots"], gl.FRAME - gl.START)
        self.assertEqual(self.split["growth"]["max_rate_in_place"], 30.0)
        self.assertEqual(self.split["growth"]["capability"], "science_growth")
        self.assertEqual(self.cluster["rate_now"], self.split["rate_now"])

    def test_the_cluster_is_cheaper_to_start(self):
        self.assertLess(self.cluster["raw_total"], self.split["raw_total"])
        self.assertLess(self.cluster["counts"]["entities"], self.split["counts"]["entities"])
        self.assertLess(self.cluster["area"], self.split["area"])

    def test_selection_keeps_the_cluster_unless_the_site_favours_the_split(self):
        def option(d, port):
            candidate = {"genome": gl.GreenGenome(d["form"]), "descriptor": d, "niche": d["form"]}
            return selection.option(candidate, selection.Demand(5, 5), port)

        unscored = dict(self.split, growth=dict(self.split["growth"], max_rate_in_place=self.split["rate_now"]))
        even = selection.ranked([option(self.cluster, 20), option(unscored, 20)])
        self.assertEqual(even[0]["candidate"]["descriptor"]["form"], "cluster")
        lab_far = selection.ranked([option(self.cluster, 140), option(unscored, 20)])
        self.assertEqual(lab_far[0]["candidate"]["descriptor"]["form"], "split")


class PlacementTests(unittest.TestCase):
    def test_both_candidates_are_placed_and_the_record_names_the_runner_up(self):
        from test_v2_green_site import KINDS, WORLD, World

        from factorio_agent import green_science as gs
        from factorio_agent.manufacturing import lab_green_entry_outside
        from factorio_agent.site.transform import in_canonical

        with tempfile.TemporaryDirectory() as directory:
            world = World(Path(directory), KINDS[0])
            lab_port = in_canonical(world.labs, lab_green_entry_outside)["port"]
            cores = gs.planned_green_layouts(world.site, WORLD, world.green.catalog, lab_port, rate=5, tick=100)
            forms = [c["layout"]["genome"]["form"] for c in cores]
            self.assertEqual(forms[0], "cluster")  # the cheaper start wins by default
            self.assertIn("split", forms)
            first = cores[0]["layout"]
            self.assertEqual(first["runner_ups"][0]["variant"], "green-split-6")
            split = next(c for c in cores if c["layout"]["genome"]["form"] == "split")
            self.assertEqual(set(split["cells"]), {"inserters", "belts", "packs"})
            self.assertEqual(split["layout"]["descriptor"]["growth"]["max_rate_in_place"], 30.0)
            gs.settle_green(world.site, WORLD, None)


if __name__ == "__main__":
    unittest.main()
