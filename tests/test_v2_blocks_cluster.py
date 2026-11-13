"""Direct-insertion clusters and the layout-principle audit (docs/v2/LAYOUT_PRINCIPLES.md)."""

import copy
import unittest

from test_v2_production_layout import catalog as layout_catalog
from test_v2_production_layout import line_for
from v2_blocks_fixture import _entity, _recipe

from factorio_agent.blocks.cluster import (
    DIRECT_MAX_CONSUMERS,
    MAX_ARMS_PER_EDGE,
    cluster_block,
    decide_edge,
    grow_decision,
)
from factorio_agent.blocks.cluster_audit import MAX_BELTS_PER_SLOT, audit_cluster, layout_findings
from factorio_agent.blocks.render import render_ascii
from factorio_agent.core.world_catalog import WorldCatalog
from factorio_agent.manufacturing import green_core
from factorio_agent.site.transform import Transform, apply, variants
from factorio_agent.transport import xy

PLATES = ("iron-plate", "copper-plate", "steel-plate")
LETTERS = {
    "copper-cable": "K",
    "electronic-circuit": "C",
    "inserter": "I",
    "iron-gear-wheel": "G",
    "transport-belt": "B",
    "logistic-science-pack": "S",
    "automation-science-pack": "R",
    "firearm-magazine": "A",
    "engine-unit": "E",
    "pipe": "P",
}


def catalog():
    data = layout_catalog().to_dict()
    data["entities"]["underground-belt"] = _entity("underground-belt", electric=False)
    data["entities"]["splitter"] = _entity("splitter", electric=False)
    data["recipes"]["firearm-magazine"] = _recipe(1, {"iron-plate": 4}, {"firearm-magazine": 1})
    data["recipes"]["engine-unit"] = _recipe(
        10, {"steel-plate": 1, "iron-gear-wheel": 1, "pipe": 2}, {"engine-unit": 1}, "advanced-crafting"
    )
    data["recipes"]["gear-hog"] = _recipe(1, {"iron-gear-wheel": 20}, {"gear-hog": 1})
    data["recipes"]["fan-a"] = _recipe(1, {"iron-gear-wheel": 1, "iron-plate": 1}, {"fan-a": 1})
    data["recipes"]["fan-b"] = _recipe(1, {"iron-gear-wheel": 1, "fan-a": 1}, {"fan-b": 1})
    data["recipes"]["fan-c"] = _recipe(1, {"iron-gear-wheel": 1, "fan-b": 1}, {"fan-c": 1})
    return WorldCatalog.from_dict(data)


def block(c, product, rate=5, **kw):
    return cluster_block(c, product, rate, sourced=PLATES.__contains__, **kw)


def counts(plan):
    found = {}
    for e in plan["entities"]:
        found[e["name"]] = found.get(e["name"], 0) + 1
    return found


def size(plan):
    (x0, y0), (x1, y1) = plan["reserved_area"]
    return x1 - x0, y1 - y0


class GreenUnitTests(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog()
        self.plan = block(self.catalog, "logistic-science-pack")

    def test_green_unit_is_six_machines_in_a_compact_cluster(self):
        n = counts(self.plan)
        self.assertEqual(n["assembling-machine-1"], 6)
        self.assertLessEqual(n["inserter"], 14)
        self.assertLess(n["transport-belt"], 60)
        self.assertLessEqual(n["small-electric-pole"], 8)
        self.assertNotIn("underground-belt", n)
        self.assertNotIn("splitter", n)
        width, height = size(self.plan)
        self.assertLessEqual(width * height, 26 * 16)
        self.assertEqual(audit_cluster(self.catalog, self.plan), [])
        # Two plate inputs (one belt, one lane each), one pack output.
        inputs = [p for p in self.plan["ports"] if p["direction"] == "input"]
        self.assertEqual(sorted(p["item"] for p in inputs), ["copper-plate", "iron-plate"])
        self.assertEqual({p["lane"] for p in inputs}, {"left", "right"})
        self.assertEqual(self.plan["ports"][-1]["item"], "logistic-science-pack")
        picture = render_ascii(self.catalog, self.plan, LETTERS)
        for letter in "KCIGBS":
            self.assertIn(letter * 3, picture)

    def test_every_intermediate_edge_is_direct_insertion(self):
        edges = {(e["from"], e["to"]): e["kind"] for e in self.plan["edges"]}
        self.assertEqual(set(edges.values()), {"direct"})
        self.assertIn(("iron-gear-wheel", "transport-belt"), edges)
        self.assertIn(("iron-gear-wheel", "inserter"), edges)
        self.assertIn(("copper-cable", "electronic-circuit"), edges)
        # One machine per recipe serves every consumer; sub-assemblers are far under-used.
        nodes = {n["recipe"]: n for n in self.plan["nodes"]}
        self.assertLess(nodes["inserter"]["utilisation"], 0.2)

    def test_every_rotation_and_mirror_audits_clean(self):
        for base in variants():
            turn = Transform(base.rotation, base.mirror, (0.5, 0.5))
            with self.subTest(rotation=turn.rotation, mirror=turn.mirror):
                self.assertEqual(audit_cluster(self.catalog, apply(turn, self.plan)), [])

    def test_origin_translates_the_whole_block(self):
        moved = block(self.catalog, "logistic-science-pack", origin=(10.5, -7.5))
        self.assertEqual(len(moved["entities"]), len(self.plan["entities"]))
        a, b = xy(self.plan["entities"][0]), xy(moved["entities"][0])
        self.assertEqual((b[0] - a[0], b[1] - a[1]), (10, -8))

    def test_output_side_is_a_choice_with_one_shared_reservation(self):
        plan = block(self.catalog, "transport-belt", 30, chest="iron-chest")
        self.assertGreaterEqual(len(plan["output_sides"]), 2)
        areas = set()
        for side in plan["output_sides"]:
            other = block(self.catalog, "transport-belt", 30, chest="iron-chest", output_side=side)
            self.assertEqual(other["output_side"], side)
            self.assertEqual(audit_cluster(self.catalog, other), [])
            areas.add(str(other["reserved_area"]))
        self.assertEqual(len(areas), 1)


class OtherProductTests(unittest.TestCase):
    def test_the_same_generator_lays_out_red_belts_inserters_ammo_and_engines(self):
        c = catalog()
        expect = {
            "automation-science-pack": 2,
            "transport-belt": 2,
            "inserter": 4,
            "firearm-magazine": 1,
            "engine-unit": 3,
        }
        for product, machines in expect.items():
            with self.subTest(product=product):
                plan = block(c, product, 10, chest="iron-chest")
                self.assertEqual(counts(plan)["assembling-machine-1"], machines)
                self.assertEqual(audit_cluster(c, plan), [])
                self.assertLessEqual(counts(plan)["transport-belt"], 6 + 8 * machines)


class EdgeDecisionTests(unittest.TestCase):
    def test_low_flow_one_to_one_is_direct_insertion(self):
        self.assertEqual(decide_edge(5, 1, 1, 30)[0], "direct")
        self.assertEqual(decide_edge(60, 1, DIRECT_MAX_CONSUMERS, 30)[0], "direct")

    def test_fan_out_or_high_flow_needs_a_belt(self):
        self.assertEqual(decide_edge(5, 1, DIRECT_MAX_CONSUMERS + 1, 30)[0], "belt")
        self.assertEqual(decide_edge(5, 3, 2, 30)[0], "belt")
        self.assertEqual(decide_edge(MAX_ARMS_PER_EDGE * 30 + 1, 1, 1, 30)[0], "belt")

    def test_cluster_refuses_a_belt_edge_so_the_caller_uses_a_row(self):
        c = catalog()
        with self.assertRaisesRegex(ValueError, "needs_belt"):
            block(c, "gear-hog", 30)  # 20 gears per craft: the gear edge exceeds three inserters
        with self.assertRaisesRegex(ValueError, "needs_belt"):
            block(c, "fan-c", 1)  # one gear machine for three consumers: fans out beyond adjacency

    def test_growth_re_evaluates_edges_without_touching_the_built_block(self):
        c = catalog()
        plan = block(c, "logistic-science-pack")
        before = copy.deepcopy(plan)
        self.assertEqual({e["kind"] for e in grow_decision(c, plan, 1)}, {"direct"})
        grown = {(e["from"], e["to"]): e["kind"] for e in grow_decision(c, plan, 12)}
        # Twelve pack machines: one inserter and one belt machine feed them over a belt lane.
        self.assertEqual(grown[("inserter", "logistic-science-pack")], "belt")
        self.assertEqual(grown[("transport-belt", "logistic-science-pack")], "belt")
        self.assertEqual(plan, before)


class LayoutPrincipleTests(unittest.TestCase):
    """The audit fails the old shapes, so the generator cannot regress into them."""

    def setUp(self):
        self.catalog = catalog()
        self.plan = block(self.catalog, "logistic-science-pack")

    def sabotaged(self, extra=(), drop=lambda e: False):
        plan = copy.deepcopy(self.plan)
        plan["entities"] = [e for e in plan["entities"] if not drop(e)] + list(extra)
        return plan

    def test_internal_crossing_is_a_finding(self):
        under = {"name": "underground-belt", "position": {"x": 30.5, "y": 30.5}, "direction": 4}
        found = layout_findings(self.catalog, self.sabotaged([under]))
        self.assertTrue(any(f.startswith("internal_crossing") for f in found), found)

    def test_belt_wrapped_around_a_machine_is_a_finding(self):
        plan = block(self.catalog, "firearm-magazine", 10, chest="iron-chest")  # input belt on top already
        self.assertEqual(layout_findings(self.catalog, plan), [])
        x, y = xy(next(e for e in plan["entities"] if e.get("recipe")))
        for side in (-3, 3):  # continue the belt down both sides: a loop around the machine
            plan["entities"] += [
                {"name": "transport-belt", "position": {"x": x + side, "y": y + d}, "direction": 8} for d in (-1, 0, 1)
            ]
        found = layout_findings(self.catalog, plan)
        self.assertTrue(any(f.startswith("belt_wraps_machine") for f in found), found)

    def test_missing_direct_inserter_starves_the_consumer(self):
        machines = {e["recipe"]: xy(e) for e in self.plan["entities"] if e.get("recipe")}
        gx, gy = machines["iron-gear-wheel"]
        bx, by = machines["transport-belt"]
        gap = ((gx + bx) / 2, (gy + by) / 2)
        found = audit_cluster(self.catalog, self.sabotaged(drop=lambda e: xy(e) == gap))
        self.assertIn("ingredient_unreachable:transport-belt:iron-gear-wheel", found)

    def test_mixed_lane_and_lane_capacity_are_findings(self):
        plan = copy.deepcopy(self.plan)
        for port in plan["ports"]:
            if port["direction"] == "input":
                port["item"] = "iron-plate"
        plan["ports"].insert(0, dict(plan["ports"][0], item="copper-plate"))
        found = audit_cluster(self.catalog, plan)
        self.assertTrue(any(f.startswith("lane_mixed") for f in found), found)
        loud = copy.deepcopy(self.plan)
        for node in loud["nodes"]:
            node["crafts_needed"] = 10_000
        found = audit_cluster(self.catalog, loud)
        self.assertTrue(any(f.startswith("declared_rate_above_nameplate") for f in found), found)

    def test_the_old_green_core_fails_the_principles(self):
        old = green_core(0.5, 0.5, 1, narrow=True)
        found = layout_findings(self.catalog, old)
        self.assertTrue(any(f.startswith("internal_crossing") for f in found), found)
        self.assertTrue(any(f.startswith(("too_many_belts", "block_too_sparse")) for f in found), found)

    def test_rows_satisfy_the_same_principles(self):
        c = self.catalog
        for product, rate in (("pipe", 30), ("transport-belt", 60), ("iron-gear-wheel", 45)):
            with self.subTest(product=product):
                line = line_for(c, product, rate)
                slots = sum(
                    r["count"] + r["growth"]["remaining_slots"] for r in (line["main"], line["sub"]) if r is not None
                )
                plan = {"entities": line["entities"], "ports": line["inputs"]}
                self.assertEqual(layout_findings(c, plan, slots=slots), [])
                belts = sum(1 for e in line["entities"] if e["name"] == "transport-belt")
                self.assertLessEqual(belts, MAX_BELTS_PER_SLOT * slots)


if __name__ == "__main__":
    unittest.main()
