from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import unittest

from factorio_agent.board import entity
from factorio_agent.bus_tap import extend_bus, preserve_bus_ports, signature
from factorio_agent.main_bus import BusMaterial, ConsumerTap, plan_main_bus
from factorio_agent.mall_bus_extension import extend_frontier,propose_mall_taps
from factorio_ai.deterministic_builder import FactoryBuilder


def bus():
    return plan_main_bus((BusMaterial('iron-plate',-11.5,(375,),2),BusMaterial('copper-plate',-3.5,(150,),2)),
        (ConsumerTap('ammo','iron-plate',-20.5,'critical',demand_per_minute=120),),north_y=-24.5,south_y=-4.5,port_x=12.5)


class MallFrontierTests(unittest.TestCase):
    def test_paid_extension_is_only_new_north_belts_and_preserves_all_old_ports_layers_and_capacity(self):
        old=bus();result=extend_frontier(old,-36.5);self.assertTrue(result['ok'],result);new=result['bus']
        self.assertEqual(len(result['added_entities']),24)
        self.assertTrue({signature(q)for q in old['entities']}<={signature(q)for q in new['entities']})
        self.assertTrue(all(p in new['ports']for p in old['ports']))
        self.assertEqual(new['capacity'],old['capacity']);self.assertEqual(new['underground_pairs'],old['underground_pairs'])
        self.assertTrue(any(p['name']=='iron-plate:0:frontier:-36.5'for p in new['ports']))

    def test_later_ordinary_taps_keep_historical_frontier_and_canonical_descriptors(self):
        old=bus();extended=extend_frontier(old,-36.5)['bus']
        result=extend_bus(extended,dict(name='mall-belt',item='iron-plate',splitter_y=-30.5,priority='surplus',demand_per_minute=180),{'entities':[]})
        self.assertTrue(result['ok'],result)
        self.assertTrue(all(p in result['bus']['ports']for p in extended['ports']))
        next_extension=extend_frontier(result['bus'],-48.5)
        self.assertTrue(next_extension['ok'],next_extension)
        self.assertTrue(all(p in next_extension['bus']['ports']for p in extended['ports']))

    def test_historical_alias_cannot_be_replaced_by_a_splitter_or_moved_to_foreign_tile(self):
        old=extend_frontier(bus(),-36.5)['bus']
        result=extend_bus(old,dict(name='bad',item='iron-plate',splitter_y=-24.5,priority='surplus',demand_per_minute=1),{'entities':[]})
        self.assertFalse(result['ok'])
        changed=deepcopy(old);alias=next(p for p in changed['ports']if ':frontier:'in p['name']);alias['position']['x']+=1
        with self.assertRaises(ValueError):preserve_bus_ports(changed,deepcopy(old))

    def test_consumer_contract_and_frontier_metadata_changes_fail_closed(self):
        old=bus();new=extend_frontier(old,-36.5)['bus']
        next(p for p in new['ports']if p['name']=='ammo')['demand_per_minute']+=1
        with self.assertRaises(ValueError):preserve_bus_ports(old,new)
        for value in(-24.5,-23.5,-100.5,True,float('nan')):
            with self.subTest(value=value),self.assertRaises(ValueError):extend_frontier(old,value)

    def test_occupied_first_outward_cell_chooses_further_paid_frontier_without_phantom_aliases(self):
        old=bus();rows=[dict(q,unit_number=100+i)for i,q in enumerate(old['entities'])]
        rows += [dict(entity('transport-belt',13.5,-35.5+i,0),unit_number=10000+i)for i in range(12)]
        obs={'ok':True,'world_id':'w','entities':rows}
        result=propose_mall_taps(old,{'iron-plate':180},'transport-belt',obs,FactoryBuilder.__new__(FactoryBuilder),{'blocks':{}})
        self.assertTrue(result['ok'],result);self.assertEqual(result['north_y'],-48.5)
        self.assertEqual(result['tasks'][0]['tap_port']['position'],{'x':12.5,'y':-47.5})
        self.assertFalse(any(p['name'].endswith('frontier:-36.5')for p in result['bus']['ports']))
        self.assertTrue(result['station_rejections'])


if __name__=='__main__':unittest.main()
