import unittest

from factorio_agent.power_layout import compact_coal_cell
from factorio_agent.transport import compile_crossings


class CompactCoalGeometryTests(unittest.TestCase):
    def test_drill_output_passes_fuel_pickup_before_main_port(self):
        plan=compact_coal_cell({"x":0,"y":0})
        belts={(e["position"]["x"],e["position"]["y"]):e for e in plan["entities"] if e["name"]=="transport-belt"}
        self.assertEqual(len(belts),5)
        # Native east-facing drill drops at (1.5,-0.5); east-pickup inserter
        # at (1.5,0.5) takes from (2.5,0.5) and drops inside the drill.
        self.assertIn((1.5,-.5),belts)
        self.assertIn((2.5,.5),belts)
        self.assertEqual(plan["ports"][0]["facing"],4)
        inserter=next(e for e in plan["entities"] if e["name"]=="burner-inserter")
        self.assertEqual(inserter["position"],{"x":1.5,"y":.5})
        self.assertEqual(inserter["direction"],4)
        self.assertEqual(plan["required_items"]["transport-belt"],5)
        self.assertEqual(plan['ports'][0]['position'],{'x':3.5,'y':1.5})
        self.assertNotIn('mining_resource',plan['ports'][0])
        self.assertNotIn('mining_resource',belts[(3.5,1.5)])

    def test_straight_published_tail_preserves_real_upstream_feed_without_crossing_waiver(self):
        plan=compact_coal_cell({'x':0,'y':0})
        for start,expected in(((2.5,1.5),False),((3.5,1.5),True)):
            route=dict(ok=True,segments=[dict(position=dict(x=start[0]+i,y=start[1]),direction=4)for i in range(4)],metrics=dict(length=3,turns=0))
            foreign=[e for e in plan['entities']if e['name']=='transport-belt'and e['position']!=dict(x=start[0],y=start[1])
                     and(e['position']['x'],e['position']['y'])!=(3.5,1.5)]
            compiled=compile_crossings(route,foreign)
            self.assertEqual(compiled['ok'],expected,compiled)
            if not expected:self.assertEqual(compiled['reason'],'foreign_surface_belt_would_feed_route')
            else:self.assertEqual(len(compiled['entities']),4)


if __name__ == "__main__":
    unittest.main()
