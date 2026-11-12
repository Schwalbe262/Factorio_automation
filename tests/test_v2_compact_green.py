from copy import deepcopy
import unittest

from factorio_agent.compact_green import (
    compact_green_core,
    audit_compact_green,
    audit_lab_injection,
    _recipe_catalog,
)
from factorio_agent.manufacturing import lab_green_entry
from factorio_agent.science import lab_row
from factorio_agent.transport import xy


class CompactGreenTests(unittest.TestCase):
    def setUp(self):
        self.plan = compact_green_core(0.5, 0.5)

    def remove(self, name, x, y):
        self.plan["entities"] = [
            e for e in self.plan["entities"] if not (e["name"] == name and xy(e) == (x + 0.5, y + 0.5))
        ]

    def errors(self):
        result = audit_compact_green(self.plan)
        self.assertFalse(result["ok"])
        return " ".join(result["errors"])

    def test_complete_block_has_35_per_minute_capacity_and_12_raw_rooted_machines(self):
        audit = audit_compact_green(self.plan)
        self.assertTrue(audit["ok"], audit["errors"])
        self.assertEqual(audit["machines_ready"], 12)
        self.assertEqual(audit["raw_items_per_minute"], {"iron-plate": 192.5, "copper-plate": 52.5})
        self.assertEqual(audit["machine_equivalents"]["iron-gear-wheel"], 0.875)
        self.assertEqual(audit["machine_equivalents"]["copper-cable"], 0.875)
        self.assertEqual((audit["reservation_width"], audit["reservation_height"]), (32, 28))
        self.assertEqual((audit["entities"], audit["normal_belts"], audit["underground_pairs"]), (216, 155, 3))
        self.assertEqual(len(self.plan["extension_slots"]), 4)
        self.assertNotIn("splitter", self.plan["required_items"])

    def test_shared_bus_interfaces_remain_fixed_after_translation(self):
        plan = compact_green_core(43.5, -78.5)
        self.assertEqual(
            [(p["item"], xy(p), p["facing"]) for p in plan["ports"]],
            [
                ("iron-plate", (40.5, -77.5), 4),
                ("copper-plate", (40.5, -80.5), 4),
                ("logistic-science-pack", (69.5, -59.5), 4),
            ],
        )
        self.assertTrue(audit_compact_green(plan)["ok"])

    def test_every_individual_science_pickup_is_required(self):
        for x in range(1, 26, 4):
            with self.subTest(x=x):
                self.plan = compact_green_core(0.5, 0.5)
                self.remove("inserter", x, 14)
                self.assertIn("unreachable_ingredients", self.errors())

    def test_every_individual_science_output_must_reach_fixed_output(self):
        for x in range(1, 26, 4):
            with self.subTest(x=x):
                self.plan = compact_green_core(0.5, 0.5)
                self.remove("inserter", x, 18)
                self.assertIn("machine_output_unconnected", self.errors())

    def test_both_real_raw_bus_inputs_are_required(self):
        for item in ("iron-plate", "copper-plate"):
            with self.subTest(item=item):
                self.plan = compact_green_core(0.5, 0.5)
                self.plan["ports"] = [p for p in self.plan["ports"] if p["item"] != item]
                self.assertIn("unreachable_ingredients", self.errors())

    def test_direct_wire_insertion_is_required_and_yellow_is_too_slow(self):
        self.remove("fast-inserter", 20, 4)
        self.assertIn("unreachable_ingredients", self.errors())
        self.plan = compact_green_core(0.5, 0.5)
        arm = next(e for e in self.plan["entities"] if e["name"] == "fast-inserter" and xy(e) == (20.5, 4.5))
        arm["name"] = "inserter"
        self.assertIn("inserter_capacity_insufficient", self.errors())

    def test_both_gear_branches_are_required_for_real_consumers(self):
        for x, y in ((6, 4), (8, 6)):
            with self.subTest(branch=(x, y)):
                self.plan = compact_green_core(0.5, 0.5)
                self.remove("fast-inserter", x, y)
                self.assertIn("unreachable_ingredients", self.errors())

    def test_mixer_must_deliver_both_items_before_the_first_pickup(self):
        feeder = next(e for e in self.plan["entities"] if xy(e) == (-1.5, 14.5))
        feeder["direction"] = 8
        self.assertIn("science_input_lane_mismatch", self.errors())

    def test_underground_mouths_need_native_straight_exits_and_roles(self):
        self.remove("transport-belt", 25, 3)
        self.assertIn("underground_requires_straight_approach_and_exit", self.errors())
        self.plan = compact_green_core(0.5, 0.5)
        mouth = next(e for e in self.plan["entities"] if e["name"] == "underground-belt")
        mouth["belt_to_ground_type"] = "output"
        self.assertIn("missing_or_wrong_underground_mouth", self.errors())

    def test_full_machine_footprint_is_checked_and_extension_slots_stay_empty(self):
        duplicate = deepcopy(next(e for e in self.plan["entities"] if e.get("recipe") == "copper-cable"))
        duplicate["position"]["x"] += 1
        self.plan["entities"].append(duplicate)
        self.assertIn("prospective_footprint_overlap", self.errors())
        self.plan = compact_green_core(0.5, 0.5)
        slot = self.plan["extension_slots"][0]
        self.plan["entities"].append(dict(slot, name="transport-belt", direction=4))
        self.assertIn("extension_slot_occupied", self.errors())

    def test_rate_audit_uses_catalog_machine_speed(self):
        catalog = _recipe_catalog()
        catalog.entities["assembling-machine-1"]["crafting_speed"] = 0.25
        audit = audit_compact_green(self.plan, catalog)
        self.assertFalse(audit["ok"])
        self.assertIn("machine_capacity_insufficient:logistic-science-pack", audit["errors"])

    def test_electrical_consumers_need_internal_power_coverage(self):
        self.remove("small-electric-pole", 20, 2)
        self.assertIn("unpowered_consumer_position", self.errors())

    def test_current_lab_injection_reaches_all_eight_labs(self):
        labs = lab_row(17.5, -111.5, 8)
        entry = lab_green_entry(labs)
        self.assertEqual(audit_lab_injection(labs, entry), {"ok": True, "labs": 8, "labs_reached": 8})

    def test_old_injection_position_omits_the_last_lab(self):
        labs = lab_row(17.5, -111.5, 8)
        entry = lab_green_entry(labs)
        for e in entry["entities"]:
            e["position"]["x"] -= 4
        entry["port"]["position"]["x"] -= 4
        for key in ("input", "output"):
            entry["pair"][key][0] -= 4
        audit = audit_lab_injection(labs, entry)
        self.assertEqual(audit, {"ok": False, "labs": 8, "labs_reached": 7})


if __name__ == "__main__":
    unittest.main()
