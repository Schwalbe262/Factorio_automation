"""SmeltingDistrict takes its site and orientation from the master plan."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.site.fakeworld import make_world
from factorio_agent.site.geometry import Rect
from factorio_agent.site.masterplan import plan_site
from factorio_agent.site.transform import Transform, apply
from factorio_agent.smelting import SmeltingDistrict, feed_lane_positions, furnace_bank
from factorio_agent.smelting_site import corridor_guide, plan_candidates, zone_slots

COUNTS = {"iron-ore": 4, "copper-ore": 2}
PLAN = plan_site(make_world(7, size=512))


def inside(area, rect: Rect) -> bool:
    (x0, y0), (x1, y1) = area
    return rect.x0 <= x0 and rect.y0 <= y0 and x1 <= rect.x1 and y1 <= rect.y1


class PlanCandidateTests(unittest.TestCase):
    def test_candidates_sit_in_zone_corners_and_record_their_orientation(self):
        zone = PLAN.zone("smelting").rect
        candidates = plan_candidates(PLAN, COUNTS)
        self.assertTrue(candidates)
        slots = [[[r.x0, r.y0], [r.x1, r.y1]] for r in zone_slots(zone)]
        for candidate in candidates:
            self.assertTrue(inside(candidate["area"], zone))
            self.assertIn(candidate["area"], slots)
            self.assertIn("site_transform", candidate)
            self.assertTrue(all(b["site_transform"] == candidate["site_transform"] for b in candidate["banks"]))

    def test_best_candidate_outputs_face_the_output_edge(self):
        smelting = PLAN.zone("smelting")
        best = plan_candidates(PLAN, COUNTS)[0]
        facing = {"N": 0, "E": 4, "S": 8, "W": 12}[smelting.output_side]
        self.assertEqual({b["ports"][-1]["facing"] for b in best["banks"]}, {facing})

    def test_corridor_guide_is_the_corridor_bounding_box(self):
        corridor = next(c for c in PLAN.corridors if c.kind == "feeder" and c.target == "smelting" and c.segments)
        box = corridor_guide(PLAN, corridor.material)
        for rect in corridor.rects:
            self.assertTrue(box[0] <= rect.x0 and box[1] <= rect.y0 and rect.x1 <= box[2] and rect.y1 <= box[3])
        self.assertIsNone(corridor_guide(PLAN, "unobtainium"))

    def test_turned_bank_selects_the_same_feed_belts(self):
        bank = furnace_bank(10, 10, 3, "iron-ore")
        expected = feed_lane_positions(bank)
        for t in (Transform(4, None, (2, 2)), Transform(8, "x", (2, 2))):
            turned = dict(apply(t, bank), site_transform=t.to_dict())
            self.assertEqual(feed_lane_positions(turned), [apply(t, {"position": p})["position"] for p in expected])


class PrepareOnPlanTests(unittest.TestCase):
    def prepare(self, root, site):
        catalog = SimpleNamespace(
            fingerprint="c",
            recipes={"iron-plate": {"energy": 3.2}, "copper-plate": {"energy": 3.2}},
            entities={"stone-furnace": {"crafting_speed": 1}},
        )
        mining = {
            "world_id": "w",
            "banks": [
                {
                    "ready": True,
                    "nominal_rate_per_minute": 60,
                    "plan": {
                        "resource": r,
                        "ports": [
                            {
                                "item": r,
                                "kind": "item",
                                "direction": "output",
                                "facing": 4,
                                "position": {"x": -0.5, "y": i + 0.5},
                            }
                        ],
                    },
                }
                for i, r in enumerate(("iron-ore", "copper-ore", "coal"))
            ],
        }
        (root / "mining-outposts.json").write_text(json.dumps(mining), encoding="utf-8")
        board = {"world_id": "w", "catalog_fingerprint": "c", "blocks": {}}
        (root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        game = SimpleNamespace(
            cfg=SimpleNamespace(runtime_dir=root), query=Mock(return_value={"ok": True, "legal": [1]})
        )
        builder = FactoryBuilder.__new__(FactoryBuilder)
        routine = SmeltingDistrict(game, None, builder, catalog, site=site)
        route = _plan([], [], underground_pairs=[], route_metrics={"length": 5})
        with (
            patch("factorio_agent.smelting.belt_connection", return_value=route) as belts,
            patch("factorio_agent.smelting.power_connection", return_value=_plan([], [])),
            patch("factorio_agent.smelting.preflight", return_value={"ok": True, "clear_obstacles": []}),
        ):
            result = routine.prepare({"world_id": "w", "technologies": {"logistics": True}, "entities": []})
        return result, belts

    def test_plan_zone_site_is_used_first_with_corridor_guides(self):
        site = SimpleNamespace(plan=Mock(return_value=PLAN))
        with TemporaryDirectory() as directory:
            result, belts = self.prepare(Path(directory), site)
        self.assertTrue(result["ok"], result)
        site.plan.assert_called_once_with("w")
        self.assertTrue(inside(result["plan"]["reserved_area"], PLAN.zone("smelting").rect))
        self.assertIn("site_transform", result["plan"])
        guided = [call.kwargs.get("guide") for call in belts.call_args_list if "guide" in call.kwargs]
        feeds = ("iron-ore", "copper-ore", "coal")
        feeders = [c for c in PLAN.corridors if c.target == "smelting" and c.material in feeds and c.segments]
        self.assertEqual(len(guided), len(feeders))

    def test_failing_site_service_falls_back_to_legacy_anchors(self):
        site = SimpleNamespace(plan=Mock(side_effect=RuntimeError("no survey")))
        with TemporaryDirectory() as directory:
            result, _belts = self.prepare(Path(directory), site)
        self.assertTrue(result["ok"], result)
        self.assertNotIn("site_transform", result["plan"])


if __name__ == "__main__":
    unittest.main()
