import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_agent.smelting import SmeltingDistrict,furnace_bank,merge_entities
from factorio_agent.jobs import StarterExecutors
from factorio_agent.board import entity


class FurnaceBankGeometryTests(unittest.TestCase):
    def test_dual_lane_feed_and_plate_output_do_not_overlap_machines(self):
        geometry=FactoryBuilder.__new__(FactoryBuilder)
        for resource,count in (('iron-ore',4),('copper-ore',2)):
            bank=furnace_bank(24,-48,count,resource)
            boxes=[geometry._occupied_by_plan([e]) for e in bank['entities']]
            self.assertEqual(sum(map(len,boxes)),len(set().union(*boxes)))
            self.assertEqual(bank['required_items']['stone-furnace'],count)
            self.assertEqual(len(bank['ports']),3)

    def test_conflicting_shared_belt_orientation_is_not_silently_overwritten(self):
        with self.assertRaises(ValueError):
            merge_entities([entity('transport-belt',.5,.5,4)],[entity('transport-belt',.5,.5,0)])

    def test_automatic_furnace_is_excluded_from_starter_upkeep_and_old_fuel_is_recovered(self):
        with TemporaryDirectory() as directory:
            routine=SmeltingDistrict.__new__(SmeltingDistrict);routine.path=Path(directory)/'district.json'
            furnace=entity('stone-furnace',-40,-32)
            routine.state={'world_id':'w','plan':{'entities':[furnace]}}
            owned=dict(furnace,unit_number=1,inventory={'coal':8},products_finished=0,remaining_burning_fuel=0)
            starter=dict(entity('stone-furnace',10,-60),unit_number=2,inventory={})
            obs={'world_id':'w','tick':100,'entities':[owned,starter]}
            executor=StarterExecutors.__new__(StarterExecutors)
            executor.builder=SimpleNamespace(owns_automated_burner=lambda e:False,_sync=lambda obs:None);executor.smelting=routine
            self.assertEqual(executor.starter_observation(obs)['entities'],[starter])
            executor.bootstrap=SimpleNamespace(next_action=lambda filtered:filtered)
            self.assertEqual(executor.step('starter',obs)['entities'],[starter])
            action=routine.clean_feed_baseline(obs)
            self.assertEqual((action['type'],action['inventory'],action['count']),('take','fuel',8))
            self.assertNotIn('automatic_feed_baseline',routine.state)
            owned['inventory']={};self.assertIsNone(routine.clean_feed_baseline(obs))
            self.assertTrue(routine.state['automatic_feed_baseline']['fuel_inventory_empty'])


if __name__=='__main__':unittest.main()
