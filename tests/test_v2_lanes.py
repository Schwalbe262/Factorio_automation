import unittest
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.lanes import lane_balancer,inserter_output_lane


class ExplicitLaneGeometryTests(unittest.TestCase):
    def test_far_lane_rule_and_inline_flip_are_not_confused(self):
        self.assertEqual(inserter_output_lane(0,4),'right')
        self.assertEqual(inserter_output_lane(8,4),'left')
        self.assertEqual(inserter_output_lane(0,4,mirrored=True),'right')
        self.assertEqual(inserter_output_lane(0,0),'right')
        self.assertEqual(inserter_output_lane(0,0,mirrored=True),'left')
        with self.assertRaises(ValueError):inserter_output_lane(0,4,straight=False)

    def test_distribution_preserves_output_heading_and_has_no_prospective_overlap(self):
        geometry=FactoryBuilder.__new__(FactoryBuilder)
        for direction in (0,4,8,12):
            p=lane_balancer(.5,.5,'iron-plate',direction)
            boxes=[geometry._occupied_by_plan([e]) for e in p['entities']]
            self.assertEqual(sum(map(len,boxes)),len(set().union(*boxes)))
            self.assertEqual(p['ports'][-1]['facing'],direction)
            self.assertEqual(p['ports'][-1]['lane_items'],{'left':'iron-plate','right':'iron-plate'})
            self.assertEqual(p['required_items']['splitter'],1)

    def test_lane_junction_has_a_straight_upstream_stub_and_opposite_side_loads(self):
        p=lane_balancer(.5,.5,'copper-plate')
        specs={(e['name'],e['position']['x'],e['position']['y'],e['direction']) for e in p['entities']}
        self.assertIn(('transport-belt',4.5,.5,4),specs)
        self.assertIn(('transport-belt',5.5,-.5,8),specs)
        self.assertIn(('transport-belt',5.5,1.5,0),specs)


if __name__=='__main__':unittest.main()
