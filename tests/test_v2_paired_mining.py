import unittest
from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_agent.paired_mining import mining_pair,retain_output


class PairedMiningTests(unittest.TestCase):
    def test_growth_keeps_port_and_existing_drills_while_feeding_opposite_sides(self):
        geometry=FactoryBuilder.__new__(FactoryBuilder)
        previous=None
        for count in (1,2,3,5):
            plan=mining_pair(16.5,-64.5,count,'iron-ore')
            boxes=[geometry._occupied_by_plan([e]) for e in plan['entities']]
            self.assertEqual(sum(map(len,boxes)),len(set().union(*boxes)))
            self.assertEqual(plan['ports'][0]['position'],{'x':18.5,'y':-62.5})
            self.assertEqual({e['direction'] for e in plan['producer_rows']['left']},{8})
            self.assertEqual({e['direction'] for e in plan['producer_rows']['right']},{0})
            if previous:
                for e in previous['entities']:self.assertIn(e,plan['entities'])
            previous=plan

    def test_resource_adjustment_retains_fixed_port_with_short_orthogonal_collar(self):
        fixed=mining_pair(10.5,-35.5,2,'copper-ore')['ports'][0]
        previous=None
        for pairs in (2,3):
            plan=retain_output(mining_pair(8.5,-37.5,pairs,'copper-ore'),fixed)
            self.assertEqual(plan['ports'][0],fixed)
            self.assertEqual(plan['collector_port']['position'],{'x':10.5,'y':-35.5})
            self.assertEqual(len(plan['collector_connection']),5)
            geometry=FactoryBuilder.__new__(FactoryBuilder)
            boxes=[geometry._occupied_by_plan([e]) for e in plan['entities']]
            self.assertEqual(sum(map(len,boxes)),len(set().union(*boxes)))
            if previous:self.assertEqual(previous['collector_connection'],plan['collector_connection'])
            previous=plan


if __name__=='__main__':unittest.main()
