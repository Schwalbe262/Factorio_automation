import unittest
import unittest.mock

from factorio_agent.routing import route_trunks


class TrunkRoutingTests(unittest.TestCase):
    def test_long_connection_has_one_turn_and_preserves_forced_headings(self):
        result = route_trunks((0.5, 0.5), (100.5, 20.5), start_direction=4, end_direction=8)
        self.assertTrue(result["ok"])
        self.assertEqual(result["metrics"]["turns"], 1)
        self.assertEqual(result["segments"][0]["direction"], 4)
        self.assertEqual(result["segments"][-1]["direction"], 8)

    def test_obstacle_is_bypassed_with_a_shared_straight_trunk(self):
        result = route_trunks((0.5, 0.5), (100.5, 0.5), occupied=[(50.5, 0.5)], start_direction=4, end_direction=4)
        self.assertTrue(result["ok"])
        self.assertLessEqual(result["metrics"]["turns"], 4)
        self.assertNotIn({"x": 50.5, "y": 0.5}, result["path"])
        self.assertEqual(len(result["path"]), len({(p["x"], p["y"]) for p in result["path"]}))

    def test_impassable_port_is_reported_instead_of_an_ugly_escape(self):
        result = route_trunks((0.5, 0.5), (20.5, 10.5), occupied=[(1.5, 0.5)], start_direction=4)
        self.assertFalse(result["ok"])

    def test_excessive_detour_and_conflicting_lattices_are_rejected(self):
        self.assertFalse(route_trunks((0.5, 0.5), (10.5, 0.5), occupied=[(5.5, 0.5)], max_turns=1)["ok"])
        self.assertFalse(route_trunks((0.5, 0.5), (10, 10))["ok"])


if __name__ == "__main__":
    unittest.main()


class LongRouteFallbackTests(unittest.TestCase):
    """eval-4177156614: the stone bank sits north of the smelting district, every brick
    block south of the bus; the only corridors need 7 turns, the skeleton families 6."""

    @staticmethod
    def corridor_case():
        from factorio_agent.routing import expand

        # Eight straight runs (7 turns) through an otherwise solid block.
        vertices = [
            (0.5, 0.5),
            (3.5, 0.5),
            (3.5, 30.5),
            (-12.5, 30.5),
            (-12.5, 140.5),
            (10.5, 140.5),
            (10.5, 160.5),
            (6.5, 160.5),
            (6.5, 170.5),
        ]
        corridor = set(expand(vertices))
        xs = [x + 0.5 for x in range(-20, 20)]
        ys = [y + 0.5 for y in range(-5, 176)]
        hard = {(x, y) for x in xs for y in ys if (x, y) not in corridor}
        bounds = {"min_x": xs[0], "max_x": xs[-1], "min_y": ys[0], "max_y": ys[-1]}
        return vertices, hard, bounds

    def test_seven_turn_corridor_needs_the_opt_in_fallback(self):
        from factorio_agent.routing import LONG_ROUTE_TURNS, expand

        vertices, hard, bounds = self.corridor_case()
        args = dict(occupied=hard, bounds=bounds, start_direction=4, end_direction=8, max_nodes=200000)
        self.assertFalse(route_trunks(vertices[0], vertices[-1], **args)["ok"])
        result = route_trunks(vertices[0], vertices[-1], **args, long_route_turns=LONG_ROUTE_TURNS)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["metrics"]["turns"], 7)
        self.assertEqual([(p["x"], p["y"]) for p in result["path"]], expand(vertices))
        self.assertEqual(result["segments"][0]["direction"], 4)
        self.assertEqual(result["segments"][-1]["direction"], 8)

    def test_fallback_keeps_the_turn_and_validator_gates(self):
        from factorio_agent.routing import LONG_ROUTE_ALTERNATIVES

        vertices, hard, bounds = self.corridor_case()
        args = dict(occupied=hard, bounds=bounds, start_direction=4, end_direction=8, max_nodes=200000)
        self.assertFalse(route_trunks(vertices[0], vertices[-1], **args, long_route_turns=6)["ok"])
        offered = []

        def refuse(candidate):
            offered.append(candidate)
            return False

        result = route_trunks(vertices[0], vertices[-1], **args, long_route_turns=10, candidate_validator=refuse)
        self.assertFalse(result["ok"])
        self.assertTrue(1 <= len(offered) <= LONG_ROUTE_ALTERNATIVES)

    def test_short_routes_never_use_the_fallback(self):
        from factorio_agent.routing import LONG_ROUTE_DISTANCE

        hard = {(5.5, y + 0.5) for y in range(-30, 30) if y != 20}
        bounds = {"min_x": -2.5, "max_x": 12.5, "min_y": -30.5, "max_y": 30.5}
        b = (10.5, 0.5)
        self.assertLess(abs(b[0] - 0.5) + abs(b[1] - 0.5), LONG_ROUTE_DISTANCE)
        with unittest.mock.patch("factorio_agent.routing.min_turn_paths", side_effect=AssertionError("fallback")):
            route_trunks(
                (0.5, 0.5), b, occupied=hard, bounds=bounds, start_direction=4, end_direction=4, long_route_turns=10
            )
