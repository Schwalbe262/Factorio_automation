import json
import math
import unittest

from factorio_agent.resource_policy import resource_decision


class ResourceDecisionTests(unittest.TestCase):
    def decide(self, **changes):
        args = dict(item='iron-plate', available=0, required=100,
                    produced_per_minute=30, consumed_per_minute=10,
                    current_cells=1, extra_cell_raw_cost=10)
        args.update(changes)
        return resource_decision(**args)

    def test_existing_stock_never_expands_even_with_zero_or_negative_live_net(self):
        for rate in (0, 30):
            r=self.decide(available=100, produced_per_minute=rate, consumed_per_minute=40)
            self.assertEqual(r['action'],'advance');self.assertEqual(r['evidence']['extra_cells'],0)
        r=self.decide(available=120,stage={'held':20})
        self.assertEqual(r['action'],'collect_stock')

    def test_full_buffers_are_collected_and_blocked_transport_is_repaired(self):
        r=self.decide(available=40,produced_per_minute=0,stage={'held':0,'output_full':True})
        self.assertEqual(r['action'],'collect_stock')
        r=self.decide(stage={'downstream_blocked':True})
        self.assertEqual(r['action'],'repair_downstream')
        self.assertEqual(r['evidence']['bottleneck'],'downstream')

    def test_small_remaining_bill_waits_instead_of_buying_more_cells(self):
        r=self.decide(available=9,required=10)
        self.assertEqual(r['action'],'wait');self.assertEqual(r['evidence']['wait_seconds'],3)
        self.assertEqual(r['evidence']['target_cells'],1)

    def test_finite_bill_comparison_charges_cost_funding_and_build_time(self):
        r=self.decide(available=10)
        self.assertEqual(r['action'],'expand_supply')
        e=r['evidence'];self.assertEqual(e['wait_seconds'],270)
        for row in e['alternatives']:
            setup=row['funding_seconds']+15
            expected=setup+60*max(0,90+row['raw_cost']-20*setup/60)/row['projected_net_per_minute']
            self.assertAlmostEqual(row['projected_wait_seconds'],expected)
            self.assertAlmostEqual(row['save_seconds'],270-expected)
        self.assertLessEqual(e['extra_cells'],2);self.assertLess(e['projected_wait_seconds'],e['wait_seconds'])

    def test_coal_deficit_recommends_only_bounded_current_cells_without_infinite_json(self):
        r=self.decide(item='coal',available=8,required=40,produced_per_minute=30,
                      consumed_per_minute=32,extra_cell_raw_cost=4)
        self.assertEqual(r['action'],'expand_supply')
        e=r['evidence'];self.assertIsNone(e['wait_seconds']);self.assertIsNone(e['save_seconds'])
        self.assertIn(e['extra_cells'],(1,2));self.assertGreater(e['projected_net_per_minute'],0)
        json.dumps(r,allow_nan=False)
        # Two additions cannot fix a larger structural deficit.
        r=self.decide(item='coal',available=100,required=200,consumed_per_minute=100)
        self.assertEqual(r['action'],'wait')

    def test_plate_ore_and_smelting_bottlenecks_are_distinguished(self):
        r=self.decide(stage={'ore_stock':0,'ore_per_minute':20})
        self.assertEqual((r['action'],r['evidence']['bottleneck']),('wait','ore'))
        r=self.decide(consumed_per_minute=50,stage={'ore_stock':0,'ore_per_minute':40})
        self.assertEqual(r['evidence']['bottleneck'],'ore')
        r=self.decide(available=10,stage={'ore_stock':1000,'ore_per_minute':20})
        self.assertEqual((r['action'],r['evidence']['bottleneck']),('expand_supply','smelting'))

    def test_unknown_or_zero_productivity_never_invents_a_future_machine_rate(self):
        for rate in (None,0):
            self.assertEqual(self.decide(produced_per_minute=rate)['action'],'wait')
        r=self.decide(produced_per_minute=0,consumed_per_minute=0,available=10,
                      stage={'cell_capacity_per_minute':30})
        self.assertEqual(r['action'],'expand_supply')

    def test_invalid_numbers_flags_or_ambiguous_cost_models_are_rejected(self):
        for field,value in (('available',-1),('required',True),('produced_per_minute',math.nan),
                            ('extra_cell_raw_cost',math.inf),('current_cells',1.5)):
            with self.subTest(field=field),self.assertRaises(ValueError):self.decide(**{field:value})
        for stage in ({'future_rate':300},{'held':1},{'output_full':1}):
            with self.subTest(stage=stage),self.assertRaises(ValueError):self.decide(stage=stage)


if __name__=='__main__':unittest.main()
