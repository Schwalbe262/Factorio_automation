from copy import deepcopy
import unittest
from unittest.mock import Mock

from factorio_agent.construction_supply import V2Bootstrap


class FuelHysteresisTests(unittest.TestCase):
    def setUp(self):
        self.b=V2Bootstrap.__new__(V2Bootstrap)
        self.b._existing_cells=Mock(return_value={'ok':True,'cells':[]})
        self.b.ensure_item=Mock(return_value={'status':'waiting','reason':'coal_needed'})
        self.obs={'inventory':{'coal':1},'entities':[dict(name='stone-furnace',position={'x':0,'y':0},inventory={})]}

    def test_live_coal_or_remaining_energy_yields_to_primary_work(self):
        for fields in ({'inventory':{'coal':1}},{'remaining_burning_fuel':2_000_000}):
            with self.subTest(fields=fields):
                obs=deepcopy(self.obs);obs['entities'][0].update(fields)
                self.assertIsNone(self.b.maintain_fuel(obs))
        self.b.ensure_item.assert_not_called();self.b._existing_cells.assert_not_called()

    def test_full_or_blocked_outputs_do_not_consume_the_last_coal(self):
        for status in ('full_output','waiting_for_space','waiting_for_space_in_destination'):
            with self.subTest(status=status):
                obs=deepcopy(self.obs);obs['entities'][0]['status_name']=status
                self.assertIsNone(self.b.maintain_fuel(obs))
        self.b.ensure_item.assert_not_called()

    def test_real_empty_consumer_still_receives_normal_coal(self):
        before=deepcopy(self.obs);action=self.b.maintain_fuel(self.obs)
        self.assertEqual((action['type'],action['item'],action['count']),('insert','coal',1))
        self.assertEqual(self.obs,before)

    def test_nearly_exhausted_burning_fuel_stays_urgent(self):
        self.obs['entities'][0]['remaining_burning_fuel']=500_000
        self.assertEqual(self.b.maintain_fuel(self.obs)['type'],'insert')
