"""transform_plan: rotations/mirrors of whole entity plans and their placement."""

import unittest

from factorio_agent.board import entity
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.manufacturing import green_core
from factorio_agent.science import lab_row, science_row
from factorio_agent.site.fit import orientations, port_score
from factorio_agent.site.geometry import Rect
from factorio_agent.site.transform import Transform, apply, transform_plan, variants
from factorio_agent.smelting_site import district_candidate

GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)
COUNTS = {"iron-ore": 4, "copper-ore": 2}


def district():
    return district_candidate(40, -24, COUNTS)


def science_block():
    science = science_row(8.5, 6.5, 3)
    labs = lab_row(1.5, -7.5)
    return {"science": science, "labs": labs, "entities": science["entities"] + labs["entities"]}


def green():
    core = green_core(10.5, 10.5, 3)
    return {"entities": core["entities"], "ports": core["ports"], "reserved_area": core["reserved_area"]}


PLANS = {"district": district, "science": science_block, "green": green}


def footprints(entities):
    return [GEOMETRY._occupied_by_plan([e]) for e in entities]


def audit(entities):
    """Order-free description: name, occupied tiles, direction, recipe, underground type."""
    rows = set()
    for e, tiles in zip(entities, footprints(entities)):
        rows.add((e["name"], frozenset(tiles), e.get("direction"), e.get("recipe"), e.get("belt_to_ground_type")))
    return rows


class TransformTests(unittest.TestCase):
    def test_every_variant_round_trips_and_stays_overlap_free(self):
        for name, build in PLANS.items():
            plan = build()
            original = audit(plan["entities"])
            for t in variants((7, -3)):
                with self.subTest(plan=name, rotation=t.rotation, mirror=t.mirror):
                    turned = apply(t, plan)
                    boxes = footprints(turned["entities"])
                    self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))
                    self.assertEqual(len(audit(turned["entities"])), len(original))
                    back = apply(t.inverse(), turned)
                    self.assertEqual(audit(back["entities"]), original)
                    self.assertEqual(back, plan)

    def test_y_mirror_is_a_variant_of_x_mirror(self):
        plan = district()
        by_y = transform_plan(plan, mirror="y", origin=(24, -8))
        by_x = transform_plan(plan, rotation=8, mirror="x", origin=(24, -8))
        self.assertEqual(audit(by_y["entities"]), audit(by_x["entities"]))

    def test_reserved_area_maps_onto_itself_or_its_transpose_about_its_centre(self):
        plan = district()
        (x0, y0), (x1, y1) = plan["area"]
        centre = ((x0 + x1) / 2, (y0 + y1) / 2)
        hw, hh = (x1 - x0) / 2, (y1 - y0) / 2
        transposed = [[centre[0] - hh, centre[1] - hw], [centre[0] + hh, centre[1] + hw]]
        for t in variants(centre):
            expected = transposed if t.rotation in (4, 12) else plan["area"]
            self.assertEqual(apply(t, plan)["area"], expected)

    def test_directions_ports_and_pairs(self):
        t = Transform(4, None, (0, 0))
        port = {"item": "coal", "position": {"x": 1.5, "y": 0.5}, "facing": 4, "direction": "input"}
        self.assertEqual(
            apply(t, port), {"item": "coal", "position": {"x": -0.5, "y": 1.5}, "facing": 8, "direction": "input"}
        )
        pair = {"input": [0.5, 0.5], "output": [3.5, 0.5], "direction": 4, "span": 3}
        self.assertEqual(
            apply(t, {"underground_pairs": [pair]})["underground_pairs"][0],
            {"input": [-0.5, 0.5], "output": [-0.5, 3.5], "direction": 8, "span": 3},
        )
        self.assertEqual(Transform(0, "x").direction(4), 12)
        self.assertEqual(Transform(0, "y").direction(0), 8)
        self.assertEqual(Transform(4, "x").inverse(), Transform(4, "x"))
        self.assertEqual(Transform(4).inverse(), Transform(12))

    def test_lane_sides_and_priorities_swap_only_when_mirrored(self):
        splitter = dict(entity("splitter", 0, 0.5, 4), _width=2, _height=1, output_priority="left")
        port = {"item": "x", "position": {"x": 0.5, "y": 0.5}, "facing": 4, "lane_items": {"left": "a", "right": "b"}}
        mirrored = apply(Transform(0, "y"), {"e": [splitter], "p": port})
        self.assertEqual(mirrored["e"][0]["output_priority"], "right")
        self.assertEqual(mirrored["p"]["lane_items"], {"right": "a", "left": "b"})
        turned = apply(Transform(8), {"e": [splitter], "p": port})
        self.assertEqual(turned["e"][0]["output_priority"], "left")
        self.assertEqual(turned["p"]["lane_items"], {"left": "a", "right": "b"})

    def test_unsafe_inputs_raise(self):
        with self.assertRaises(ValueError):
            apply(Transform(0, "x"), [entity("chemical-plant", 0.5, 0.5, 0)])
        with self.assertRaises(ValueError):
            apply(Transform(4), {"clear_obstacles": [{"name": "tree", "position": {"x": 0, "y": 0}}]})
        with self.assertRaises(ValueError):
            Transform(0, None, (0.5, 0))
        with self.assertRaises(ValueError):
            apply(Transform(4), [dict(entity("wooden-chest", 0.5, 0), _width=1, _height=2)])


class FitTests(unittest.TestCase):
    def test_port_score_penalises_ports_facing_away(self):
        port = {"position": {"x": 0, "y": 0}, "facing": 8}
        self.assertLess(port_score(port, (0, -10), "in"), port_score(port, (0, 10), "in"))
        self.assertLess(port_score(port, (0, 10), "out"), port_score(port, (0, -10), "out"))

    def test_orientation_turns_the_district_towards_feeders_and_outlet(self):
        target = Rect(-64, 0, -32, 48)  # the 48 x 32 district stood on end

        def wants(candidate):
            rows = [(b["ports"][0], (-48, -20), "in") for b in candidate["banks"]]
            rows += [(b["ports"][-1], (-48, 52), "out") for b in candidate["banks"]]
            return rows

        best = orientations(lambda x0, y0: district_candidate(x0 + 8, y0 + 8, COUNTS), (48, 32), target, wants)
        self.assertEqual(len(best), 4)  # only the quarter turns stand it on end
        top = best[0]
        self.assertEqual(top.plan["area"], [[-64, 0], [-32, 48]])
        outputs = [b["ports"][-1] for b in top.plan["banks"]]
        self.assertTrue(all(p["facing"] == 8 for p in outputs), outputs)
        self.assertLess(top.score, 1000)


if __name__ == "__main__":
    unittest.main()
