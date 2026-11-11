import unittest
from types import SimpleNamespace

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_agent.board import entity
from factorio_agent.manufacturing import green_core
from factorio_agent.routing import clearance_lanes, route_trunks
from factorio_agent.smelting import merge_entities
from factorio_agent.transport import compile_crossings, plan_connection


class ObstacleClearanceRoutingTests(unittest.TestCase):
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
