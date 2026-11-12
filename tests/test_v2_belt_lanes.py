import unittest

from factorio_agent.board import entity
from factorio_agent.compact_green import _transport


class BeltLanePhysicsTests(unittest.TestCase):
    def test_sole_perpendicular_feed_is_a_corner_preserving_both_lanes(self):
        _, graph = _transport([entity('transport-belt', 0, -1, 8), entity('transport-belt', 0, 0, 4)], [])
        self.assertEqual(graph[(0, -1)], ((0, 0), {'left': 'left', 'right': 'right'}))

    def test_empty_inline_tail_forces_straight_north_side_load_to_left(self):
        es = [entity('transport-belt', 0, -1, 8), entity('transport-belt', 0, 0, 4), entity('transport-belt', -1, 0, 4)]
        _, graph = _transport(es, [])
        self.assertEqual(graph[(0, -1)][1], {'left': 'left', 'right': 'left'})
        self.assertEqual(graph[(-1, 0)][1], {'left': 'left', 'right': 'right'})

    def test_opposed_perpendicular_feeds_make_straight_two_lane_mixer(self):
        es = [entity('transport-belt', 0, -1, 8), entity('transport-belt', 0, 0, 4), entity('transport-belt', 0, 1, 0)]
        _, graph = _transport(es, [])
        self.assertEqual(graph[(0, -1)][1], {'left': 'left', 'right': 'left'})
        self.assertEqual(graph[(0, 1)][1], {'left': 'right', 'right': 'right'})

    def test_opposing_front_belt_does_not_create_an_inline_input(self):
        es = [entity('transport-belt', 0, -1, 8), entity('transport-belt', 0, 0, 4), entity('transport-belt', 1, 0, 12)]
        _, graph = _transport(es, [])
        self.assertEqual(graph[(0, -1)][1], {'left': 'left', 'right': 'right'}); self.assertNotIn((1, 0), graph)

    def test_underground_jump_is_not_a_surface_inline_feed(self):
        es = [entity('transport-belt', 0, -1, 8), entity('transport-belt', 0, 0, 4),
              dict(entity('underground-belt', -1, 0, 4), belt_to_ground_type='input'),
              dict(entity('underground-belt', 3, 0, 4), belt_to_ground_type='output')]
        _, graph = _transport(es, [{'input': [-1, 0], 'output': [3, 0], 'direction': 4, 'span': 4}])
        self.assertEqual(graph[(0, -1)][1], {'left': 'left', 'right': 'right'})
        self.assertEqual(graph[(-1, 0)], ((3, 0), {'left': 'left', 'right': 'right'}))


if __name__ == '__main__': unittest.main()
