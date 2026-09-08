from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_fluids import FluidProduction, _diverse_source_pairs
from tests import test_deterministic_fluids as fixtures


class TapDiversityTests(unittest.TestCase):
    def test_distinct_sources_precede_destination_variants_without_dropping_pairs(self):
        sources = [{"x": x, "y": 0} for x in range(14)]
        destinations = [{"x": 30, "y": y} for y in range(3)]
        pairs = [(a, b) for a in sources for b in destinations]
        original = deepcopy(pairs)
        ranked = _diverse_source_pairs(pairs)
        self.assertEqual([a for a, _ in ranked[:14]], sources)
        self.assertEqual([b for _, b in ranked[:14]], [destinations[0]] * 14)
        self.assertEqual(sorted(json.dumps(p, sort_keys=True) for p in ranked),
                         sorted(json.dumps(p, sort_keys=True) for p in pairs))
        self.assertEqual(pairs, original)
        self.assertEqual(_diverse_source_pairs([]), [])

    def test_cost_hint_promotes_distinct_sources_stably_and_ignores_invalid_values(self):
        sources = [{"x": x, "y": 0} for x in range(5)]
        pairs = [(s, {"x": 30, "y": y}) for s in sources for y in range(2)]
        for invalid in (None, True, 0, -1, float("nan"), float("inf"), "1", {}):
            with self.subTest(invalid=invalid):
                costs = {(0, 0): invalid, (1, 0): 20, (2, 0): 20, (3, 0): 5}
                ranked = _diverse_source_pairs(pairs, costs)
                self.assertEqual([a["x"] for a, _ in ranked[:5]], [3, 1, 2, 0, 4])
                self.assertEqual(ranked[5:], pairs[1::2])


class PipeConnectionDiversityTests(unittest.TestCase):
    def setUp(self):
        fixtures.FluidProductionTests.setUp(self)
        self.source, self.destination = fixtures.FluidProductionTests.underground_geometry(self)
        self.fluids._sync(self.obs)
        self.taps = [{"x": 100 + i, "y": 0} for i in range(14)]
        self.destinations = [{"x": 0, "y": i} for i in range(3)]
        self.builder.route.return_value = {"ok": False, "reason": "no route within bounds"}
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        self.network = {"taps": self.taps, "destination_taps": self.destinations, "connected": False}
        self.fluids._network_taps = Mock(side_effect=[self.network, {"connected": True}])
        self.escape_entities = [
            {"name": "pipe", "position": self.taps[9], "direction": 0},
            {"name": "pipe-to-ground", "position": {"x": 109, "y": 1}, "direction": 0},
            {"name": "pipe-to-ground", "position": {"x": 109, "y": 8}, "direction": 8},
            {"name": "pipe", "position": {"x": 109, "y": 9}, "direction": 0}]

    def connect(self):
        return self.fluids._connect_pipe(self.obs, self.source, self.destination, "water-branch", {"entities": []})

    def test_tenth_verified_source_reaches_existing_escape_and_retains_typed_persistent_link(self):
        def escape(obs, source, destination, reserved):
            if source["position"] == self.taps[9] and source["facing"] == 8:
                return {"ok": True, "entities": deepcopy(self.escape_entities)}
            return {"ok": False, "reason": "no outlet route"}
        self.fluids._underground_escape = Mock(side_effect=escape)
        self.assertEqual(self.connect()["status"], "succeeded")
        link = self.fluids.state["links"]["water-branch"]
        self.assertEqual(link["entities"], [{**e, "_fluid": self.source["item"]} for e in self.escape_entities])
        self.factory.register_plan.assert_called_once_with("fluid-link:water-branch", link, self.obs)
        self.builder.ensure_plan.assert_called_once_with(self.obs, link)
        reloaded = FluidProduction(self.game, self.builder, self.catalog)
        self.assertEqual(reloaded.state["links"]["water-branch"], link)
        source_calls = [c for c in self.fluids._underground_escape.call_args_list if c.args[1].get("facing") is not None]
        self.assertLessEqual(len(source_calls), 1 + 12 * 4)

    def test_twelve_pair_four_facing_bound_survives_many_destination_variants(self):
        self.fluids._underground_escape = Mock(return_value={"ok": False, "reason": "no outlet route"})
        self.assertEqual(self.connect()["status"], "blocked")
        calls = [c for c in self.fluids._underground_escape.call_args_list if c.args[1].get("facing") is not None]
        self.assertEqual(len(calls), 1 + 12 * 4)
        self.assertEqual({call.args[1]["position"]["x"] for call in calls[1:]}, set(range(100, 112)))
        self.factory.register_plan.assert_not_called()
        self.assertNotIn("water-branch", self.fluids.state["links"])

    def test_failed_search_cost_promotes_a_tap_but_budget_exhaustion_does_not(self):
        for reason, expected in (("no route within bounds", 109), ("route search budget exhausted", 100)):
            with self.subTest(reason=reason):
                self.fluids._network_taps.side_effect = None
                self.fluids._network_taps.return_value = self.network
                def route(source, destination, *args):
                    return {"ok": False, "reason": reason,
                            "visited": 5 if source == self.taps[9] else 100}
                self.builder.route.side_effect = route
                self.fluids._underground_escape = Mock(return_value={"ok": False, "reason": "no outlet route"})
                self.assertEqual(self.connect()["status"], "blocked")
                calls = [c for c in self.fluids._underground_escape.call_args_list if c.args[1].get("facing") is not None]
                self.assertEqual(calls[1].args[1]["position"]["x"], expected)

    def test_existing_link_bypasses_candidate_search_and_keeps_segment_verification(self):
        link = {"ok": True, "entities": [{**e, "_fluid": self.source["item"]} for e in self.escape_entities], "ports": []}
        self.fluids.state["links"]["water-branch"] = deepcopy(link)
        self.fluids._network_taps.side_effect = None
        self.fluids._network_taps.return_value = {"connected": False}
        self.fluids._underground_escape = Mock()
        self.assertEqual(self.connect()["status"], "blocked")
        self.builder.route.assert_not_called()
        self.fluids._underground_escape.assert_not_called()
        self.assertEqual(self.fluids.state["links"]["water-branch"], link)


if __name__ == "__main__":
    unittest.main()
