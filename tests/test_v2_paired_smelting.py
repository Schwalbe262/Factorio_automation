import unittest
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.paired_smelting import paired_bank
from factorio_agent.lanes import inserter_output_lane
from factorio_agent.routing import route_trunks


class PairedProductionTests(unittest.TestCase):
    def test_short_lateral_port_escape_preserves_bounded_clean_corridors(self):
        route=route_trunks((-44.5,-27.5),(-44.5,-16.5),occupied={(-43.5,-21.5)},
                           start_direction=4,end_direction=0,max_nodes=150000)
        self.assertTrue(route['ok'])
        self.assertLessEqual(route['metrics']['length'],1.35*11+4)
        self.assertLessEqual(route['metrics']['turns'],6)

    def test_opposite_producer_rows_share_one_output_without_output_splitters(self):
        geometry=FactoryBuilder.__new__(FactoryBuilder)
        for count in (3,5,6):
            plan=paired_bank(-40,-32,count,'iron-ore')
            boxes=[geometry._occupied_by_plan([e]) for e in plan['entities']]
            self.assertEqual(sum(map(len,boxes)),len(set().union(*boxes)))
            self.assertEqual(plan['required_items']['stone-furnace'],2*count)
            self.assertEqual(plan['required_items']['splitter'],1)
            for output in plan['output_inserters']:
                self.assertEqual(inserter_output_lane(output['inserter']['direction'],4),output['lane'])


if __name__=='__main__':unittest.main()
