import copy
import unittest

from factorio_agent.blocks.audit import audit_row
from factorio_agent.blocks.geometry import xy
from factorio_agent.blocks.params import RowLimits
from factorio_agent.blocks.row import row_block
from factorio_agent.blocks.sizing import size_row
from factorio_agent.routing import VECTORS
from v2_blocks_fixture import FLOWS, fake_catalog, real_catalog, real_catalog_path

AM1 = "assembling-machine-1"


def plan_for(catalog, recipe="automation-science-pack", count=3, flow="east", inserter="inserter"):
    sizing = size_row(catalog, recipe, 1, machine=AM1).with_count(count)
    return row_block(catalog, sizing, origin=(4.5, 9.5), flow=flow, inserter=inserter)


def without(plan, predicate):
    broken = copy.deepcopy(plan)
    index = next(i for i, e in enumerate(broken["entities"]) if predicate(e))
    broken["entities"].pop(index)
    return broken


def machines(plan):
    return [e for e in plan["entities"] if e["name"] == AM1]


def middle(plan):
    return xy(machines(plan)[1])


def codes(errors):
    return {e.split(":")[0] for e in errors}


class BrokenPlanContract:
    """Each sabotage must be caught in every orientation; subclasses set ``self.catalog``."""

    def check(self, mutate, expected, **kw):
        for flow in FLOWS:
            with self.subTest(flow=flow, expected=expected):
                plan = plan_for(self.catalog, flow=flow, **kw)
                self.assertEqual(audit_row(self.catalog, plan), [])
                self.assertIn(expected, codes(audit_row(self.catalog, mutate(plan))))

    def test_missing_input_inserter(self):
        def mutate(plan):
            m = middle(plan)
            return without(plan, lambda e: e["name"] == "inserter" and _feeds(plan, e, m, input_side=True))

        self.check(mutate, "ingredient_unreachable")

    def test_missing_output_inserter(self):
        def mutate(plan):
            m = middle(plan)
            return without(plan, lambda e: e["name"] == "inserter" and _feeds(plan, e, m, input_side=False))

        self.check(mutate, "machine_output_missing")

    def test_flipped_input_belt(self):
        def mutate(plan):
            broken = copy.deepcopy(plan)
            port = xy(next(p for p in broken["ports"] if p["direction"] == "input"))
            belt = min(
                (e for e in broken["entities"] if e["name"] == "transport-belt" and xy(e) != port),
                key=lambda e: abs(xy(e)[0] - port[0]) + abs(xy(e)[1] - port[1]),
            )
            belt["direction"] = (belt["direction"] + 8) % 16
            return broken

        self.check(mutate, "belt_not_on_port_line")
        self.check(mutate, "ingredient_unreachable")

    def test_swapped_recipe(self):
        def mutate(plan):
            broken = copy.deepcopy(plan)
            machines(broken)[1]["recipe"] = "iron-gear-wheel"
            return broken

        self.check(mutate, "machine_recipe_mismatch")

    def test_deleted_pole(self):
        def mutate(plan):
            m = middle(plan)
            return without(
                plan, lambda e: e["name"] == "small-electric-pole" and abs(xy(e)[0] - m[0]) + abs(xy(e)[1] - m[1]) == 2
            )

        self.check(mutate, "unpowered")

    def test_disconnected_pole_network(self):
        def mutate(plan):
            broken = copy.deepcopy(plan)
            pole = next(e for e in broken["entities"] if e["name"] == "small-electric-pole")
            pole["position"] = {"x": pole["position"]["x"] - 40, "y": pole["position"]["y"] - 40}
            return broken

        self.check(mutate, "poles_not_wire_connected")

    def test_overlapping_machines(self):
        def mutate(plan):
            broken = copy.deepcopy(plan)
            first, second = machines(broken)[:2]
            second["position"] = {
                "x": (first["position"]["x"] + second["position"]["x"]) / 2,
                "y": (first["position"]["y"] + second["position"]["y"]) / 2,
            }
            return broken

        self.check(mutate, "footprint_overlap")

    def test_reversed_input_inserter_spills_product(self):
        def mutate(plan):
            broken = copy.deepcopy(plan)
            m = middle(broken)
            arm = next(e for e in broken["entities"] if e["name"] == "inserter" and _feeds(broken, e, m, True))
            arm["direction"] = (arm["direction"] + 8) % 16
            return broken

        self.check(mutate, "product_on_input_belt")

    def test_port_off_belt(self):
        def mutate(plan):
            broken = copy.deepcopy(plan)
            port = broken["ports"][-1]
            port["position"] = {"x": port["position"]["x"] + 50, "y": port["position"]["y"]}
            return broken

        self.check(mutate, "port_not_on_facing_belt")

    def test_long_handed_replaced_by_plain(self):
        def mutate(plan):
            broken = copy.deepcopy(plan)
            for e in broken["entities"]:
                if e["name"] == "long-handed-inserter":
                    e["name"] = "fast-inserter"
            return broken

        self.check(mutate, "ingredient_unreachable", recipe="inserter", count=1, inserter="fast-inserter")


def _feeds(plan, arm, machine_xy, input_side):
    """True when ``arm`` is the plain inserter adjacent to that machine on the given side."""
    vx, vy = VECTORS[arm["direction"]]
    p = xy(arm)
    drop = (p[0] - vx, p[1] - vy)
    pickup = (p[0] + vx, p[1] + vy)
    inside = drop if input_side else pickup
    return abs(inside[0] - machine_xy[0]) <= 1 and abs(inside[1] - machine_xy[1]) <= 1


class FakeAudit(BrokenPlanContract, unittest.TestCase):
    def setUp(self):
        self.catalog = fake_catalog()

    def test_throughput_findings(self):
        plan = plan_for(self.catalog, recipe="iron-gear-wheel", count=2, inserter="fast-inserter")
        self.assertEqual(audit_row(self.catalog, plan), [])
        slow = copy.deepcopy(plan)
        for e in slow["entities"]:
            if e["name"] == "fast-inserter":
                e["name"] = "inserter"
        found = codes(audit_row(self.catalog, slow))
        self.assertIn("inserter_throughput_insufficient", found)
        self.assertIn("output_inserter_throughput_insufficient", found)
        tight = RowLimits(lane_items_per_minute={"transport-belt": 100})
        found = codes(audit_row(self.catalog, plan, limits=tight))
        self.assertIn("lane_capacity_exceeded", found)
        self.assertIn("output_lane_capacity_exceeded", found)

    def test_wrong_lane_port_starves_machines(self):
        plan = plan_for(self.catalog, recipe="logistic-science-pack", count=2)
        broken = copy.deepcopy(plan)
        for port in broken["ports"]:
            port.pop("lane", None)
        broken["ports"] = [p for p in broken["ports"] if p["item"] != "inserter"]
        self.assertIn("ingredient_unreachable", codes(audit_row(self.catalog, broken)))

    def test_entity_outside_reservation_and_unknown_entity(self):
        plan = plan_for(self.catalog)
        broken = copy.deepcopy(plan)
        broken["reserved_area"] = [[0, 0], [1, 1]]
        broken["entities"].append({"name": "mystery", "position": {"x": 0.5, "y": 0.5}, "direction": 0})
        found = codes(audit_row(self.catalog, broken))
        self.assertTrue({"entity_outside_reserved_area", "unknown_entity"} <= found)


@unittest.skipIf(real_catalog_path() is None, "real eval-780299489 catalog not available")
class RealAudit(BrokenPlanContract, unittest.TestCase):
    def setUp(self):
        self.catalog = real_catalog()


if __name__ == "__main__":
    unittest.main()
