import json
import unittest
from types import SimpleNamespace
from factorio_agent.board import entity
from factorio_agent.routing import route_trunks, expand, metrics
from factorio_agent.transport import compile_crossings, underground_proof, plan_connection
from factorio_agent.power_layout import V2FactoryBuilder


class LayeredCorridorTests(unittest.TestCase):
    def route(self):
        return route_trunks((0.5, 0.5), (10.5, 0.5), start_direction=4, end_direction=4)

    def test_perpendicular_crossing_preserves_foreign_belt_and_pairs_mouths(self):
        foreign = entity("transport-belt", 5.5, 0.5, 0)
        plan = compile_crossings(self.route(), [foreign])
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["required_items"], {"transport-belt": 8, "underground-belt": 2})
        self.assertNotIn(foreign["position"], [e["position"] for e in plan["entities"]])
        self.assertEqual(plan["underground_pairs"][0]["span"], 2)
        self.assertEqual(plan, json.loads(json.dumps(plan)))

    def test_parallel_surface_belt_is_not_silently_reused(self):
        self.assertFalse(compile_crossings(self.route(), [entity("transport-belt", 5.5, 0.5, 4)])["ok"])

    def test_generic_underground_crossing_does_not_side_load_a_mouth_or_change_the_exit(self):
        def route(vertices, last_direction):
            points = expand(vertices)
            segments = []
            vectors = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}
            for i, p in enumerate(points):
                direction = (
                    last_direction
                    if i == len(points) - 1
                    else next(d for d, v in vectors.items() if v == (points[i + 1][0] - p[0], points[i + 1][1] - p[1]))
                )
                segments.append({"position": {"x": p[0], "y": p[1]}, "direction": direction})
            return {"ok": True, "segments": segments, "metrics": metrics(points)}

        entering = route([(0.5, 0.5), (2.5, 0.5), (2.5, -4.5)], 0)
        exiting = route([(0.5, 0.5), (5.5, 0.5), (5.5, 2.5)], 8)
        self.assertFalse(compile_crossings(entering, [entity("transport-belt", 2.5, -0.5, 4)])["ok"])
        self.assertFalse(compile_crossings(exiting, [entity("transport-belt", 4.5, 0.5, 0)])["ok"])

    def test_close_crossings_share_one_tunnel_without_overlapping_mouths(self):
        plan = compile_crossings(
            self.route(), [entity("transport-belt", 3.5, 0.5, 0), entity("transport-belt", 5.5, 0.5, 0)]
        )
        self.assertTrue(plan["ok"])
        self.assertEqual(len(plan["underground_pairs"]), 1)
        self.assertEqual(plan["underground_pairs"][0]["span"], 4)

    def test_fixed_ports_allow_source_input_and_destination_continuation(self):
        plan = compile_crossings(
            self.route(), [entity("transport-belt", -0.5, 0.5, 4), entity("transport-belt", 11.5, 0.5, 4)]
        )
        self.assertTrue(plan["ok"])

    def test_nearby_pole_can_be_escaped_without_a_maze_route(self):
        blockers = {(-45.5, 18.5), (-42.5, 12.5), (-48.5, 2.5), (-51.5, 14.5), (-39.5, 3.5)}
        route = route_trunks(
            (-46.5, 21.5), (-47.5, -28.5), occupied=blockers, start_direction=4, end_direction=4, max_nodes=150000
        )
        self.assertTrue(route["ok"])
        self.assertTrue(all((p["x"], p["y"]) not in blockers for p in route["path"]))
        self.assertLessEqual(route["metrics"]["turns"], 6)
        self.assertLessEqual(route["metrics"]["length"], 1.35 * 51 + 4)

    def test_crossing_nearby_mouth_and_unintended_side_feed_are_rejected(self):
        cross = entity("transport-belt", 5.5, 0.5, 0)
        mouth = dict(entity("underground-belt", 2.5, 0.5, 4), belt_to_ground_type="input")
        self.assertFalse(compile_crossings(self.route(), [cross, mouth])["ok"])
        feed = entity("transport-belt", 3.5, 1.5, 0)
        self.assertFalse(compile_crossings(self.route(), [feed])["ok"])

    def test_observed_underground_pair_must_be_the_planned_partner(self):
        plan = compile_crossings(self.route(), [entity("transport-belt", 5.5, 0.5, 0)])
        obs = {
            "entities": [
                dict(e, unit_number=i + 1, underground_pair_verified=True, underground_neighbour={"unit_number": 999})
                for i, e in enumerate(plan["entities"])
                if e["name"] == "underground-belt"
            ]
        }
        self.assertFalse(underground_proof(obs, plan))

    def test_native_query_failure_is_not_treated_as_clearable_nature(self):
        game = SimpleNamespace(query=lambda _: {"ok": True, "blocked": [], "max_span": 5})
        builder = SimpleNamespace(
            _occupied_by_plan=lambda _: set(), can_place=lambda _: {"ok": False, "reason": "world_identity_changed"}
        )
        a = {"item": "iron-ore", "position": {"x": 0.5, "y": 0.5}, "facing": 4}
        b = {"item": "iron-ore", "position": {"x": 10.5, "y": 0.5}, "facing": 4}
        result = plan_connection(game, builder, {"world_id": "w", "entities": []}, a, b)
        self.assertFalse(result["ok"])
        self.assertEqual(result["native_reason"], "world_identity_changed")

    def test_complete_corridor_selection_checks_foreign_layers_before_choosing(self):
        game = SimpleNamespace(query=lambda _: {"ok": True, "blocked": [], "max_span": 5})
        geometry = __import__(
            "factorio_agent.core.deterministic_builder", fromlist=["FactoryBuilder"]
        ).FactoryBuilder.__new__(
            __import__("factorio_agent.core.deterministic_builder", fromlist=["FactoryBuilder"]).FactoryBuilder
        )
        builder = SimpleNamespace(_occupied_by_plan=geometry._occupied_by_plan, can_place=lambda _: {"ok": True})
        foreign = [entity("transport-belt", 20.5, 0.5 + i, 0) for i in range(6)]
        a = {"item": "iron-ore", "position": {"x": 0.5, "y": 0.5}, "facing": 4}
        b = {"item": "iron-ore", "position": {"x": 20.5, "y": 10.5}, "facing": 8}
        result = plan_connection(game, builder, {"world_id": "w", "entities": foreign}, a, b)
        self.assertTrue(result["ok"])
        self.assertGreater(result["layer_candidates_rejected"], 0)
        self.assertLessEqual(result["route_metrics"]["turns"], 6)

    def test_approved_nature_is_normally_mined_and_bound_to_current_world(self):
        builder = V2FactoryBuilder.__new__(V2FactoryBuilder)
        obstacle = {"name": "tree-01", "position": {"x": 0.5, "y": 0.5}}
        plan = {"clear_obstacles": [obstacle], "clearance_world_id": "w"}
        builder.game = SimpleNamespace(query=lambda _: {"ok": True, "obstacle": obstacle})
        self.assertEqual(builder.ensure_plan({"world_id": "other"}, plan)["status"], "blocked")
        action = builder.ensure_plan({"world_id": "w"}, plan)
        self.assertEqual((action["type"], action["name"], action["count"]), ("mine", "tree-01", 1))


if __name__ == "__main__":
    unittest.main()
