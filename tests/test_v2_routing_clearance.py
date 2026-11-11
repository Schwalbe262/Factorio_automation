import unittest
from types import SimpleNamespace
from unittest.mock import patch

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_agent.board import entity
from factorio_agent.manufacturing import green_core
from factorio_agent.routing import blocked_port_prefix,clear_skeleton,clearance_lanes,expand,obstacle_index,route_trunks
from factorio_agent.smelting import merge_entities
from factorio_agent.transport import compile_crossings, plan_connection


class ObstacleClearanceRoutingTests(unittest.TestCase):
    def test_indexed_segment_prune_finds_far_hard_cells_and_preserves_canonical_geometry(self):
        bounds={'min_x':-.5,'max_x':60.5,'min_y':-.5,'max_y':20.5}
        points=[(.5,.5),(40.5,.5),(40.5,10.5)]
        self.assertFalse(blocked_port_prefix(points,{(20.5,.5)}))
        rows,cols=obstacle_index({(20.5,.5)})
        self.assertIsNone(clear_skeleton(points,rows,cols,bounds,6))
        rows,cols=obstacle_index(set())
        redundant=[(.5,.5),(.5,.5),(10.5,.5),(40.5,.5),(40.5,10.5)]
        self.assertEqual(clear_skeleton(redundant,rows,cols,bounds,6),tuple(points))
        self.assertEqual(expand(redundant),expand(points))
        self.assertIsNone(clear_skeleton(points,rows,cols,dict(bounds,max_x=39.5),6))
        self.assertIsNone(clear_skeleton(points,rows,cols,bounds,0))
        loop=[(.5,.5),(10.5,.5),(10.5,10.5),(5.5,10.5),(5.5,.5)]
        self.assertIsNone(clear_skeleton(loop,rows,cols,bounds,6))

    def test_rejected_long_segments_never_materialize_and_fallback_shares_one_total_budget(self):
        hard={(x+.5,y+.5)for x in range(1,36)for y in range(1,25)}
        hard.update((1.5,y+.5)for y in range(25,29))
        required={(38.5,.5),(38.5,25.5),(27.5,25.5),(27.5,28.5)}
        expanded=[]
        def tracked(vertices):
            path=expand(vertices);self.assertFalse(set(path)&hard);expanded.append(len(path));return path
        with patch('factorio_agent.routing.expand',side_effect=tracked):
            result=route_trunks((.5,.5),(28.5,28.5),occupied=hard,
                bounds={'min_x':-15.5,'max_x':44.5,'min_y':-15.5,'max_y':44.5},
                start_direction=4,end_direction=4,max_nodes=24000,
                candidate_validator=lambda c:required<={(e['position']['x'],e['position']['y'])for e in c['segments']})
        self.assertTrue(result['ok'],result)
        self.assertEqual(sum(expanded),result['visited']);self.assertLessEqual(sum(expanded),24000)
        self.assertGreater(result['rejected_skeletons'],0)
        self.assertEqual(result['metrics']['length'],78)
        self.assertLessEqual(result['metrics']['turns'],6)

    def test_hard_first_port_step_fails_without_spending_expansion_budget(self):
        result=route_trunks((.5,.5),(100.5,-70.5),occupied={(1.5,.5)},start_direction=4,end_direction=0)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'],'no_clean_trunk_corridor')
        self.assertEqual(result['visited'],0)

    def test_short_escape_can_turn_before_a_later_blocked_forward_cell(self):
        short=[(.5,.5),(1.5,.5),(1.5,-5.5),(10.5,-5.5),(10.5,-6.5)]
        self.assertFalse(blocked_port_prefix(short,{(2.5,.5)}))
        self.assertTrue(blocked_port_prefix(short,{(10.5,-5.5)}))
        self.assertTrue(blocked_port_prefix([(.5,.5),(1.5,.5),(4.5,.5),(4.5,-5.5)],{(2.5,.5)}))

    def test_accepted_legacy_bundle_corridor_keeps_channel_for_later_gear_feed(self):
        core=green_core(.5,.5,7)
        geometry=FactoryBuilder.__new__(FactoryBuilder)
        builder=SimpleNamespace(_occupied_by_plan=geometry._occupied_by_plan,can_place=lambda es:{'ok':True})
        game=SimpleNamespace(query=lambda _: {'ok':True,'blocked':[],'max_span':5})
        entities=core['entities']
        for index,(source,target) in enumerate(core['connections'][:7]):
            route=plan_connection(game,builder,{'world_id':'w','entities':[]},source,target,entities)
            self.assertTrue(route['ok'],(index,route))
            if index==3:
                # The tempting new 62-tile corridor blocks the subsequent
                # straight gear feed. Keep the accepted 78-tile legacy choice.
                self.assertEqual(route['route_metrics']['length'],78)
                self.assertEqual(route['route_metrics']['turns'],3)
            if index==6:
                self.assertEqual(route['route_metrics']['length'],14)
                self.assertEqual(route['route_metrics']['turns'],0)
            entities=merge_entities(entities,route['entities'])

    def test_outer_edge_lane_and_three_tile_entry_crossbar_fit_original_length_gate(self):
        start, end = (.5, .5), (28.5, 28.5)
        blocked = {(x + .5, y + .5) for x in range(1, 36) for y in range(1, 25)}
        blocked.update((1.5, y + .5) for y in range(25, 29))
        limits = {'min_x': -15.5, 'max_x': 44.5, 'min_y': -15.5, 'max_y': 44.5}
        required = {(38.5, .5), (38.5, 25.5), (27.5, 25.5), (27.5, 28.5)}
        self.assertIn(38.5, clearance_lanes(blocked, start, end, limits)[0])
        result = route_trunks(start, end, occupied=blocked, bounds=limits, start_direction=4, end_direction=4,
                              max_nodes=150000, candidate_validator=lambda c: required <= {
                                  (e['position']['x'], e['position']['y']) for e in c['segments']})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['metrics']['length'], 78)
        self.assertLessEqual(result['metrics']['turns'], 6)
        self.assertLessEqual(result['metrics']['length'], 1.35 * 56 + 4)

    def test_six_turn_approach_can_use_end_escape_three_and_outside_clearance_bar(self):
        start, end = (.5, .5), (28.5, 7.5)
        blocked = {(x + .5, y + .5) for x in range(7, 24) for y in range(-2, 6)}
        blocked.update({(7.5, -2.5), (3.5, -.5), (3.5, -1.5)})
        blocked.update((27.5, y + .5) for y in range(1, 5))
        blocked.update((x + .5, y + .5) for x in range(7, 10) for y in range(-8, -5))
        foreign = [entity('transport-belt', x + .5, -3.5, 4 if x < 5 else 0) for x in range(1, 6)]
        required = {(1.5, -2.5), (6.5, -2.5), (6.5, -3.5), (25.5, -3.5), (25.5, 7.5)}
        def layers(candidate):
            positions = {(e['position']['x'], e['position']['y']) for e in candidate['segments']}
            return required <= positions and compile_crossings(candidate, foreign)['ok']
        result = route_trunks(start, end, occupied=blocked, start_direction=4, end_direction=4,
                              max_nodes=150000, candidate_validator=layers)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['metrics']['turns'], 6)
        self.assertEqual(result['metrics']['length'], 43)
        self.assertLessEqual(result['metrics']['length'], 1.35 * 35 + 4)


if __name__ == '__main__':
    unittest.main()
