import unittest

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_agent.outposts import MiningOutposts,mining_bank,validate_parameters


class AlignedMiningBankTests(unittest.TestCase):
    def test_collector_is_outside_drill_footprints_and_power_poles_do_not_overlap(self):
        geometry=FactoryBuilder.__new__(FactoryBuilder)
        for count in (1,2,8):
            p=mining_bank(.5,.5,count,'iron-ore')
            footprints=[geometry._occupied_by_plan([e]) for e in p['entities']]
            self.assertEqual(sum(map(len,footprints)),len(set().union(*footprints)))
            self.assertEqual(p['required_items']['electric-mining-drill'],count)
            self.assertEqual(p['ports'][0]['facing'],4)

    def test_bad_resource_and_nonfinite_rate_rejected_before_game_access(self):
        for p in ({'resource':'uranium-ore','rate_per_minute':30},
                  {'resource':'iron-ore','rate_per_minute':float('nan')},
                  {'resource':'iron-ore','rate_per_minute':0}):
            with self.assertRaises(ValueError):validate_parameters(p)

    def test_power_loss_does_not_trigger_duplicate_mining_bank(self):
        outpost=MiningOutposts.__new__(MiningOutposts)
        plan=mining_bank(.5,.5,1,'iron-ore')
        outpost.state={'world_id':'w','banks':[{'plan':plan,'ready':True,'nominal_rate_per_minute':30}]}
        obs={'world_id':'w','tick':400,'technologies':{'electric-mining-drill':True},
             'entities':[dict(e,unit_number=i+1,energy=0,electric_network_connected=False)
                         for i,e in enumerate(plan['entities'])]}
        result=outpost.step(obs,{'resource':'iron-ore','rate_per_minute':60})
        self.assertEqual(result['status'],'waiting')
        self.assertEqual(len(outpost.state['banks']),1)


if __name__=='__main__':unittest.main()
