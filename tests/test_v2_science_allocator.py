"""Red and green science blocks take allocator sites from the shared SiteService."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_v2_production_layout import catalog as layout_catalog
from test_v2_site_service import service

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.green_science import planned_green_clusters
from factorio_agent.science import legacy_science_candidates, planned_science_candidates
from factorio_agent.site.geometry import Rect
from factorio_agent.site.placement import settle
from factorio_agent.site.planmodel import SiteConfig

GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)


def inside(area, rect: Rect) -> bool:
    (x0, y0), (x1, y1) = area
    return rect.x0 <= x0 and rect.y0 <= y0 and x1 <= rect.x1 and y1 <= rect.y1


class ScienceAllocatorTests(unittest.TestCase):
    def test_science_blocks_fill_allocations_and_spares_are_released(self):
        with TemporaryDirectory() as directory:
            site, _, _ = service(Path(directory))
            blocks = planned_science_candidates(site, "w1", 3)
            self.assertGreaterEqual(len(blocks), 2)
            for block in blocks:
                allocation = site.allocation(block["allocation"], "w1")
                self.assertFalse(allocation.returns_to_bus)
                for key in ("science", "labs"):
                    self.assertTrue(inside(block[key]["reserved_area"], allocation.rect), key)
                    self.assertIn("site_transform", block[key])
                entities = block["science"]["entities"] + block["labs"]["entities"]
                boxes = [GEOMETRY._occupied_by_plan([e]) for e in entities]
                self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))
            settle(site, "w1", "automation_science", blocks[1]["allocation"])
            names = [a.name for a in site.plan("w1").allocations]
            self.assertEqual(names, [blocks[1]["allocation"]])
            settle(site, "w1", "automation_science", None)
            self.assertEqual(site.plan("w1").allocations, [])

    def test_plate_inputs_sit_on_the_bus_side_of_the_block(self):
        with TemporaryDirectory() as directory:
            site, _, _ = service(Path(directory))
            block = planned_science_candidates(site, "w1", 3)[0]
            allocation = site.allocation(block["allocation"], "w1")
            tap = allocation.tap_rect.centre()
            far = allocation.rect.centre()
            for port in (p for p in block["science"]["ports"] if p["direction"] == "input"):
                p = port["position"]
                near = abs(p["x"] - tap[0]) + abs(p["y"] - tap[1])
                self.assertLess(near, abs(far[0] - tap[0]) + abs(far[1] - tap[1]) + 16)

    def test_without_a_site_service_the_legacy_list_is_used(self):
        self.assertEqual(planned_science_candidates(None, "w1", 3), [])
        legacy = legacy_science_candidates(3)
        self.assertEqual(len(legacy), 7)
        self.assertEqual(legacy[0]["science"]["reserved_area"], [[-48.0, -72.0], [-16.0, -56.0]])


class GreenAllocatorTests(unittest.TestCase):
    """The green unit (a direct-insertion cluster) takes a small allocator block on any district depth."""

    LAB = {"position": {"x": 0.5, "y": 0.5}}

    def test_default_district_depth_holds_the_green_cluster(self):
        with TemporaryDirectory() as directory:
            site, _, _ = service(Path(directory))
            cores = planned_green_clusters(site, "w1", layout_catalog(), self.LAB)
            self.assertTrue(cores)
            for core in cores:
                self.assertTrue(inside(core["reserved_area"], site.allocation(core["allocation"], "w1").rect))
                boxes = [GEOMETRY._occupied_by_plan([e]) for e in core["entities"]]
                self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))
                self.assertEqual(core["connections"], [])  # no internal routes

    def test_shallow_district_strips_still_hold_the_green_cluster(self):
        config = SiteConfig(district_depth=32, district_depth_fallbacks=(24,))
        with TemporaryDirectory() as directory:
            site, _, _ = service(Path(directory), config=config)
            self.assertTrue(planned_green_clusters(site, "w1", layout_catalog(), self.LAB))


if __name__ == "__main__":
    unittest.main()
