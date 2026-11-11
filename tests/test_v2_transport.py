import json
import unittest
from factorio_agent.board import entity
from factorio_agent.routing import route_trunks
from factorio_agent.transport import compile_crossings,underground_proof


class LayeredCorridorTests(unittest.TestCase):
    def route(self):
        return route_trunks((.5,.5),(10.5,.5),start_direction=4,end_direction=4)

    def test_perpendicular_crossing_preserves_foreign_belt_and_pairs_mouths(self):
        foreign=entity('transport-belt',5.5,.5,0)
        plan=compile_crossings(self.route(),[foreign])
        self.assertTrue(plan['ok'])
        self.assertEqual(plan['required_items'],{'transport-belt':8,'underground-belt':2})
        self.assertNotIn(foreign['position'],[e['position'] for e in plan['entities']])
        self.assertEqual(plan['underground_pairs'][0]['span'],2)
        self.assertEqual(plan,json.loads(json.dumps(plan)))

    def test_parallel_surface_belt_is_not_silently_reused(self):
        self.assertFalse(compile_crossings(self.route(),[entity('transport-belt',5.5,.5,4)])['ok'])

    def test_close_crossings_share_one_tunnel_without_overlapping_mouths(self):
        plan=compile_crossings(self.route(),[entity('transport-belt',3.5,.5,0),entity('transport-belt',5.5,.5,0)])
        self.assertTrue(plan['ok'])
        self.assertEqual(len(plan['underground_pairs']),1)
        self.assertEqual(plan['underground_pairs'][0]['span'],4)

    def test_fixed_ports_allow_source_input_and_destination_continuation(self):
        plan=compile_crossings(self.route(),[entity('transport-belt',-.5,.5,4),entity('transport-belt',11.5,.5,4)])
        self.assertTrue(plan['ok'])

    def test_nearby_pole_can_be_escaped_without_a_maze_route(self):
        blockers={(-45.5,18.5),(-42.5,12.5),(-48.5,2.5),(-51.5,14.5),(-39.5,3.5)}
        route=route_trunks((-46.5,21.5),(-47.5,-28.5),occupied=blockers,
                           start_direction=4,end_direction=4,max_nodes=150000)
        self.assertTrue(route['ok'])
        self.assertTrue(all((p['x'],p['y']) not in blockers for p in route['path']))
        self.assertLessEqual(route['metrics']['turns'],6)
        self.assertLessEqual(route['metrics']['length'],1.35*51+4)

    def test_crossing_nearby_mouth_and_unintended_side_feed_are_rejected(self):
        cross=entity('transport-belt',5.5,.5,0)
        mouth=dict(entity('underground-belt',2.5,.5,4),belt_to_ground_type='input')
        self.assertFalse(compile_crossings(self.route(),[cross,mouth])['ok'])
        feed=entity('transport-belt',3.5,1.5,0)
        self.assertFalse(compile_crossings(self.route(),[feed])['ok'])

    def test_observed_underground_pair_must_be_the_planned_partner(self):
        plan=compile_crossings(self.route(),[entity('transport-belt',5.5,.5,0)])
        obs={'entities':[dict(e,unit_number=i+1,underground_pair_verified=True,
                underground_neighbour={'unit_number':999})
              for i,e in enumerate(plan['entities']) if e['name']=='underground-belt']}
        self.assertFalse(underground_proof(obs,plan))


if __name__=='__main__':unittest.main()
