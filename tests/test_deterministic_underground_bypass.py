from copy import deepcopy
import unittest

from factorio_ai.deterministic_input_links import _geometry, _path, _point, _powered_prefix
from factorio_ai.deterministic_underground_bypass import propose_collinear_bypasses
from factorio_ai.factory_templates import DIRECTIONS


def chain(direction=0, origin=(.5, .5)):
    dx, dy = DIRECTIONS[direction]

    def row(t, lateral=0, name="transport-belt", facing=None):
        return {"name": name, "position": {"x": origin[0] + dx*t - dy*lateral,
                                           "y": origin[1] + dy*t + dx*lateral},
                "direction": direction if facing is None else facing}

    path = [row(t, name="long-handed-inserter" if t in (3, 8) else "transport-belt",
                facing=(direction+8) % 16 if t in (3, 8) else direction)
            for t in (0, 1, 3, 5, 6, 8, 10, 11)]
    source, destination = row(-1), row(12)
    poles = [row(t, -3, "small-electric-pole", 0) for t in (3, 8)]
    port = lambda entity, role: {"kind": "item", "item": "copper-plate", "direction": role,
                                 "position": deepcopy(entity["position"]), "facing": direction}
    plan = {"ok": True, "key": "copper-input", "entities": [source, *path, *poles, destination],
            "ports": [], "source_port": port(source, "output"), "consumer_port": port(destination, "input")}
    return plan, path, row


class UndergroundBypassTests(unittest.TestCase):
    def propose(self, plan, path, limit=5, owners=None):
        return propose_collinear_bypasses(plan, path, max_distance=limit, other_plans=owners or [])

    def test_audited_shape_has_six_paid_new_pieces_and_preserves_original_endpoints_and_poles(self):
        plan, path, _ = chain(origin=(12.5, 22.5))
        before = deepcopy(plan), deepcopy(path)
        proposals = self.propose(plan, path)
        self.assertEqual([p["offset"] for p in proposals], [1, -1, 2, -2])
        proposal = proposals[0]
        self.assertEqual(len(proposal["new_entities"]), 6)
        self.assertEqual(proposal["entry"], {"old": path[0], "new": {**path[0], "direction": 4}})
        self.assertEqual(proposal["exit"], path[-1])
        self.assertEqual(proposal["retained_segment"], path[1:-1])
        self.assertEqual([_point(p) for p in proposal["new_entities"]],
                         [(13.5, 22.5), (13.5, 21.5), (13.5, 16.5), (13.5, 15.5), (13.5, 12.5), (13.5, 11.5)])
        for key in ("ports", "source_port", "consumer_port", "key"):
            self.assertEqual(proposal["plan"][key], plan[key])
        self.assertEqual(proposal["plan"]["entities"][-1], plan["entities"][-1])
        for pole in [row for row in plan["entities"] if row["name"] == "small-electric-pole"]:
            self.assertIn(pole, proposal["plan"]["entities"])
        self.assertEqual((plan, path), before)
        proposal["retained_segment"][0]["direction"] = 12
        self.assertEqual((plan, path), before)
        self.assertFalse(proposal["flow_verified"])
        self.assertFalse(proposal["placement_verified"])

    def test_all_cardinals_offsets_and_catalog_ranges_form_complete_directed_paths(self):
        for direction in DIRECTIONS:
            for limit in (1, 3, 5, 9):
                plan, path, _ = chain(direction)
                proposals = self.propose(plan, path, limit)
                self.assertEqual(len(proposals), 4)
                for proposal in proposals:
                    new = proposal["plan"]
                    belts, edges, inlets = _geometry(new)
                    with self.subTest(direction=direction, limit=limit, offset=proposal["offset"]):
                        route = _path(belts, edges, _point(plan["source_port"]), _point(plan["consumer_port"]))
                        self.assertIsNotNone(route)
                        self.assertFalse(any(row["name"].endswith("inserter") for row in route))
                        self.assertEqual(inlets, [])
                        self.assertEqual(_powered_prefix(new, route)["underground_pairs"], new["underground_pairs"])
                        self.assertTrue(all(pair["max_distance"] == limit for pair in new["underground_pairs"]))

    def test_wrong_identity_missing_arm_off_axis_turn_and_unprotected_endpoint_are_unsupported(self):
        for mutation in (lambda p: p[2].update(direction=0),
                         lambda p: p[2].update(unit_number=123),
                         lambda p: p[2].update(name="inserter"),
                         lambda p: p[3]["position"].update(x=1.5),
                         lambda p: p[3].update(direction=4),
                         lambda p: p.pop(0),
                         lambda p: p.pop()):
            plan, path, _ = chain()
            changed = deepcopy(path)
            mutation(changed)
            self.assertEqual(self.propose(plan, changed), [])
        for limit in (0, -1, True, 5.0, None):
            plan, path, _ = chain()
            self.assertEqual(self.propose(plan, path, limit), [])

    def test_internal_incoming_or_outgoing_branch_and_external_arm_taps_reject(self):
        for extra in (lambda row: row(5, -1, facing=4),
                      lambda row: row(2),
                      lambda row: row(5, 1, "inserter", 12),
                      lambda row: row(5, 1, "inserter", 4)):
            plan, path, row = chain()
            plan["entities"].append(extra(row))
            self.assertEqual(self.propose(plan, path), [])

    def test_unsupported_geometry_is_rejected_even_when_it_matches_the_canonical_plan(self):
        for mutation in (lambda p: p[2].update(name="inserter"),
                         lambda p: p[3]["position"].update(x=1.5),
                         lambda p: p[3].update(direction=4)):
            plan, path, _ = chain()
            mutation(path)  # Path rows are the same canonical entity objects here.
            self.assertEqual(self.propose(plan, path), [])
        plan, _, row = chain()
        plan["entities"] = [row(t) for t in range(-1, 13)]
        self.assertEqual(self.propose(plan, plan["entities"][1:-1]), [])

    def test_piece_budget_rejects_large_proposals_without_changing_the_original_route(self):
        plan, _, row = chain()
        path = [row(t, name="long-handed-inserter" if t in (3, 8) else "transport-belt",
                    facing=8 if t in (3, 8) else 0) for t in range(101) if t not in (2, 4, 7, 9)]
        plan["entities"] = [row(-1), *path, row(101)]
        plan["consumer_port"]["position"] = row(101)["position"]
        before = deepcopy(plan)
        self.assertEqual(self.propose(plan, path, limit=1), [])
        self.assertEqual(len(self.propose(plan, path, limit=9)), 4)
        self.assertEqual(plan, before)

    def test_other_ownership_or_saved_tap_on_changed_hardware_rejects_but_shared_exit_and_poles_survive(self):
        plan, path, row = chain()
        for owner in ({"entities": [deepcopy(path[0])]}, {"entities": [deepcopy(path[2])]},
                      {"upstream_tap": {"link_key": "copper-input", "belt": deepcopy(path[3])}},
                      {"entities": [row(5, 1, "inserter", 12)]}):
            self.assertEqual(self.propose(plan, path, owners=[owner]), [])
        allowed = {"entities": [deepcopy(path[-1]), *[deepcopy(e) for e in plan["entities"]
                                                      if e["name"] == "small-electric-pole"]]}
        self.assertEqual(len(self.propose(plan, path, owners=[allowed])), 4)

    def test_canonical_typed_endpoint_and_provenance_cannot_be_rotated_or_retired(self):
        for field in ("ports", "source_port", "consumer_port", "upstream_tap", "upstream_tail", "consumer_entry"):
            plan, path, _ = chain()
            reference = {"position": deepcopy(path[0 if field == "source_port" else 3]["position"])}
            plan[field] = [reference] if field == "ports" else reference
            self.assertEqual(self.propose(plan, path), [])

    def test_adjacent_belts_in_other_owner_plans_cannot_feed_or_drain_the_retired_segment(self):
        plan, path, row = chain()
        for adjacent in (row(5, -1, facing=4), row(2)):
            self.assertEqual(self.propose(plan, path, owners=[{"entities": [adjacent]}]), [])
        shared_upstream = {"entities": [deepcopy(plan["entities"][0])]}
        self.assertEqual(len(self.propose(plan, path, owners=[shared_upstream])), 4)

    def test_occupied_new_mouth_skips_only_that_candidate_and_does_not_release_its_owner(self):
        plan, path, row = chain()
        owner = {"entities": [row(1, 1, "wooden-chest", 0)]}
        before = deepcopy(owner)
        proposals = self.propose(plan, path, owners=[owner])
        self.assertEqual([p["offset"] for p in proposals], [-1, 2, -2])
        self.assertEqual(owner, before)

    def test_new_surface_feed_into_unrelated_owned_belt_is_not_proposed(self):
        plan, path, row = chain()
        plan["entities"].append(row(-1, 1))
        self.assertNotIn(1, [p["offset"] for p in self.propose(plan, path)])

    def test_intercepted_candidate_is_skipped_and_existing_other_tunnel_is_preserved(self):
        plan, path, row = chain()
        inlet, outlet = row(2, 1, "underground-belt"), row(4, 1, "underground-belt")
        inlet["belt_to_ground_type"], outlet["belt_to_ground_type"] = "input", "output"
        pair = {"input": deepcopy(inlet), "output": deepcopy(outlet), "max_distance": 5}
        plan["entities"].extend([inlet, outlet])
        plan["underground_pairs"] = [pair]
        proposals = self.propose(plan, path)
        self.assertEqual([p["offset"] for p in proposals], [-1, 2, -2])
        for proposal in proposals:
            self.assertEqual(proposal["plan"]["underground_pairs"][0], pair)
            self.assertIn(inlet, proposal["plan"]["entities"])
            self.assertIn(outlet, proposal["plan"]["entities"])

    def test_one_long_crossing_is_supported_and_needs_only_one_catalog_bounded_pair(self):
        plan, path, _ = chain()
        segment = path[:5]
        proposals = self.propose(plan, segment)
        self.assertEqual(len(proposals), 4)
        self.assertEqual(len(proposals[0]["new_entities"]), 4)
        self.assertEqual(len(proposals[0]["plan"]["underground_pairs"]), 1)

    def test_conflicting_duplicate_identity_and_malformed_metadata_fail_closed(self):
        plan, path, _ = chain()
        plan["entities"].append({**deepcopy(path[2]), "unit_number": 321})
        self.assertEqual(self.propose(plan, path), [])
        plan, path, _ = chain()
        plan["entities"].append(None)
        self.assertEqual(self.propose(plan, path), [])
        self.assertEqual(propose_collinear_bypasses({}, [], max_distance=5, other_plans=[]), [])
        self.assertEqual(propose_collinear_bypasses(None, [], max_distance=5, other_plans=[]), [])


if __name__ == "__main__":
    unittest.main()
