import unittest

from factorio_agent.routing import route_trunks


class TrunkRoutingTests(unittest.TestCase):
    def test_long_connection_has_one_turn_and_preserves_forced_headings(self):
        result = route_trunks((.5,.5),(100.5,20.5),start_direction=4,end_direction=8)
        self.assertTrue(result["ok"])
        self.assertEqual(result["metrics"]["turns"],1)
        self.assertEqual(result["segments"][0]["direction"],4)
        self.assertEqual(result["segments"][-1]["direction"],8)

    def test_obstacle_is_bypassed_with_a_shared_straight_trunk(self):
        result = route_trunks((.5,.5),(100.5,.5),occupied=[(50.5,.5)],start_direction=4,end_direction=4)
        self.assertTrue(result["ok"])
        self.assertLessEqual(result["metrics"]["turns"],4)
        self.assertNotIn({"x":50.5,"y":.5},result["path"])
        self.assertEqual(len(result["path"]),len({(p["x"],p["y"]) for p in result["path"]}))

    def test_impassable_port_is_reported_instead_of_an_ugly_escape(self):
        result = route_trunks((.5,.5),(20.5,10.5),occupied=[(1.5,.5)],start_direction=4)
        self.assertFalse(result["ok"])

    def test_excessive_detour_and_conflicting_lattices_are_rejected(self):
        self.assertFalse(route_trunks((.5,.5),(10.5,.5),occupied=[(5.5,.5)],max_turns=1)["ok"])
        self.assertFalse(route_trunks((.5,.5),(10,10))["ok"])


if __name__ == "__main__":
    unittest.main()
