import unittest
from pathlib import Path
import tempfile

from factorio_ai.deterministic_builder import power_flow_ready
from factorio_agent.electric_mining import ElectricCoal


class ElectricSupplyProofTests(unittest.TestCase):
    def test_electric_supply_requires_power_instead_of_fabricated_fuel(self):
        evidence={'ok':True,'water':200,'steam':400,'boiler_fuel':8_000_000,'drill_fuel':0,
                  'coal_on_belts':8,'connected_engines':2,'drill_powered':True}
        self.assertTrue(power_flow_ready(evidence))
        evidence['drill_powered']=False
        self.assertFalse(power_flow_ready(evidence))
        evidence['drill_fuel']=4_000_000
        self.assertTrue(power_flow_ready(evidence))

    def test_native_item_leaving_sample_tile_does_not_erase_recent_output_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            routine=ElectricCoal.__new__(ElectricCoal);routine.path=Path(directory)/'state.json'
            drill={'name':'electric-mining-drill','position':{'x':.5,'y':.5}}
            belt={'name':'transport-belt','position':{'x':2.5,'y':.5}}
            routine.state={'phase':'verify_output','sample_started_tick':0,'replacement':drill,'receiver':belt,
                           'power_plan':{'wire_hops':3}}
            obs={'tick':400,'entities':[dict(drill,unit_number=1,electric_network_connected=True,energy=1000),
                  dict(belt,unit_number=2,belt_inventory={'coal':1})]}
            self.assertIsNotNone(routine.proof(obs))
            obs['tick']=420;obs['entities'][1]['belt_inventory']={}
            proof=routine.proof(obs)
            self.assertIsNotNone(proof)
            self.assertEqual(proof['coal_on_direct_output_now'],0)
            self.assertEqual(proof['output_evidence_tick'],400)
            obs['tick']=1001
            self.assertIsNone(routine.proof(obs))


if __name__=='__main__':unittest.main()
