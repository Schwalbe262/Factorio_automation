from copy import deepcopy
from types import SimpleNamespace
import unittest

from factorio_ai.deterministic_bypass_selection import input_bypass_candidates
from tests.test_deterministic_underground_bypass import chain


def fixture():
    parent, segment, row = chain()
    child_port = {**deepcopy(parent["consumer_port"]), "position": {"x": 20.5, "y": 20.5}}
    child = {"entities": [], "source_port": deepcopy(parent["source_port"]),
             "consumer_port": child_port,
             "upstream_tap": {"link_key": "parent", "belt": deepcopy(parent["entities"][-1])}}
    state = {"links": {"parent": parent, "child": child}, "blocks": {
        "red": {"entities": [{"name": "assembling-machine-1", "recipe": "red", "position": {"x": 40, "y": 40}}],
                "ports": [deepcopy(parent["consumer_port"])]},
        "cable": {"entities": [{"name": "assembling-machine-1", "recipe": "cable", "position": {"x": 60, "y": 60}}],
                  "ports": [deepcopy(child_port)]}}}
    catalog = SimpleNamespace(recipes={name: {"ingredients": [{"name": "copper-plate", "amount": 1}]}
                                      for name in ("red", "cable")})
    return state, catalog, segment, row


class InputBypassSelectionTests(unittest.TestCase):
    def select(self, state, catalog, cycles=None):
        return input_bypass_candidates(state, catalog, cycles or {"red": 30, "cable": 45}, max_distance=5)

    def test_shared_demand_selects_chain_without_mutating_paid_plans(self):
        state, catalog, segment, _ = fixture()
        before = deepcopy(state)
        choices = self.select(state, catalog)
        self.assertTrue(choices)
        key, proposal, rate = choices[0]
        self.assertEqual((key, rate), ("parent", 75))
        self.assertEqual(proposal["entry"]["old"], segment[0])
        self.assertEqual(proposal["retained_segment"], segment[1:-1])
        self.assertEqual(state, before)

    def test_no_expansion_for_sufficient_allowance(self):
        state, catalog, _, _ = fixture()
        self.assertEqual(self.select(state, catalog, {"red": 30, "cable": 30}), [])

    def test_branch_before_crossing_does_not_add_demand_to_crossing(self):
        state, catalog, _, _ = fixture()
        state["links"]["child"]["upstream_tap"]["belt"] = deepcopy(state["links"]["parent"]["entities"][0])
        self.assertEqual(self.select(state, catalog), [])

    def test_cycle_or_mismatched_material_does_not_contribute(self):
        for change in ("cycle", "material", "unowned"):
            state, catalog, _, _ = fixture()
            if change == "cycle":
                state["links"]["child"]["upstream_tap"]["link_key"] = "child"
            elif change == "material":
                state["links"]["child"]["source_port"]["item"] = "iron-plate"
            else:
                state["blocks"].pop("cable")
            self.assertEqual(self.select(state, catalog), [])

    def test_reserved_additional_cells_divide_recipe_demand(self):
        state, catalog, _, _ = fixture()
        extra = deepcopy(state["blocks"]["cable"])
        extra["entities"][0]["position"] = {"x": 80, "y": 80}
        extra["ports"][0]["position"] = {"x": 81.5, "y": 80.5}
        state["blocks"]["cable2"] = extra
        self.assertEqual(self.select(state, catalog), [])  # 30 + 45/2

    def test_internal_tap_keeps_protected_crossing_but_can_upgrade_upstream_crossing(self):
        state, catalog, segment, _ = fixture()
        state["links"]["child"]["upstream_tap"]["belt"] = deepcopy(segment[4])
        choices = self.select(state, catalog)
        self.assertTrue(choices)
        self.assertTrue(all(p["exit"] == segment[4] for _, p, _ in choices))
        self.assertTrue(all(len(p["plan"]["underground_pairs"]) == 1 for _, p, _ in choices))

    def test_missing_or_unsupported_source_path_retains_original(self):
        state, catalog, _, _ = fixture()
        state["links"]["parent"]["source_port"]["position"] = {"x": 90, "y": 90}
        self.assertEqual(self.select(state, catalog), [])

    def test_existing_receipt_is_never_overwritten_by_another_crossing(self):
        state, catalog, _, _ = fixture()
        state["input_bypasses"] = {"parent": {"phase": "published"}}
        self.assertEqual(self.select(state, catalog), [])

    def test_tap_must_retain_exact_parent_belt_identity(self):
        for mutation in ({"name": "pipe"}, {"direction": 8}):
            state, catalog, _, _ = fixture()
            state["links"]["child"]["upstream_tap"]["belt"].update(mutation)
            self.assertEqual(self.select(state, catalog), [])

    def test_nonfinite_or_invalid_demand_never_triggers_construction(self):
        state, catalog, _, _ = fixture()
        for value in (float("nan"), float("inf"), -1, True, "75"):
            self.assertEqual(self.select(state, catalog, {"red": value, "cable": 0}), [])
        catalog.recipes["cable"]["ingredients"][0]["amount"] = float("nan")
        self.assertEqual(self.select(state, catalog), [])


if __name__ == "__main__":
    unittest.main()
