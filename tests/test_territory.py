import unittest

from factorio_ai import territory as t


class RectTests(unittest.TestCase):
    def test_intersects_and_gap(self):
        a = t.Rect(0, 0, 10, 10)
        b = t.Rect(5, 5, 15, 15)
        c = t.Rect(20, 20, 30, 30)
        self.assertTrue(a.intersects(b))
        self.assertFalse(a.intersects(c))
        # gap makes near-but-not-touching rects "intersect" (clearance)
        d = t.Rect(11, 0, 20, 10)
        self.assertFalse(a.intersects(d))
        self.assertTrue(a.intersects(d, gap=2))

    def test_inflate_and_center(self):
        a = t.Rect(0, 0, 10, 10)
        self.assertEqual(a.center, (5, 5))
        self.assertEqual(a.inflate(1), t.Rect(-1, -1, 11, 11))

    def test_reserved_box_dims_has_growth_headroom(self):
        w, h = t.reserved_box_dims(12, 13, growth_factor=2.0, io_margin=4)
        self.assertGreater(w, 12 * 2)  # grew along the row axis + margins
        self.assertEqual(h, 13 + 8)


class AllocateTests(unittest.TestCase):
    def test_places_at_free_anchor(self):
        res = t.allocate_box(0, 0, 10, 10, occupied=[])
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "placed_at_anchor")

    def test_shifts_when_anchor_blocked(self):
        blocker = t.Rect(-10, -10, 10, 10)
        res = t.allocate_box(0, 0, 10, 10, occupied=[blocker], step=8, gap=2)
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "placed_in_ring")
        placed = t.rect_from_bounds(res["rect"])
        self.assertFalse(placed.intersects(blocker, gap=2))  # no overlap with the blocker

    def test_no_free_space_when_fully_blocked(self):
        # a giant occupied rect covering the whole search area
        wall = t.Rect(-10000, -10000, 10000, 10000)
        res = t.allocate_box(0, 0, 10, 10, occupied=[wall], max_rings=5)
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "no_free_space")

    def test_two_allocations_never_overlap(self):
        occ = []
        r1 = t.allocate_box(0, 0, 20, 20, occupied=occ)
        occ.append(t.rect_from_bounds(r1["rect"]))
        r2 = t.allocate_box(0, 0, 20, 20, occupied=occ, gap=2)
        rect1 = t.rect_from_bounds(r1["rect"])
        rect2 = t.rect_from_bounds(r2["rect"])
        self.assertFalse(rect1.intersects(rect2, gap=2))


class OccupiedAndRegisterTests(unittest.TestCase):
    def test_occupied_rects_from_world_memory(self):
        wm = {
            "factory": {"zones": [{"bounds": {"min_x": 0, "min_y": 0, "max_x": 10, "max_y": 10}}]},
            "resources": {"patches": [{"bounds": {"min_x": 50, "min_y": 0, "max_x": 60, "max_y": 10}}]},
        }
        sites = [{"reserved_box": {"min_x": 100, "min_y": 0, "max_x": 120, "max_y": 20}}]
        rects = t.occupied_rects(wm, sites)
        self.assertEqual(len(rects), 3)

    def test_register_site_records_reserved(self):
        wm: dict = {}
        rect = t.Rect(0, 0, 20, 20)
        t.register_site(wm, "ec-1", rect, target_item="electronic-circuit")
        self.assertEqual(len(wm["reserved_sites"]), 1)
        self.assertEqual(wm["reserved_sites"][0]["site_id"], "ec-1")
        # re-registering the same id replaces, not duplicates
        t.register_site(wm, "ec-1", t.Rect(0, 0, 30, 30))
        self.assertEqual(len(wm["reserved_sites"]), 1)


if __name__ == "__main__":
    unittest.main()
