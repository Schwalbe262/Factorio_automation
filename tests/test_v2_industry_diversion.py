"""Receipt-audited finite replacement funding, with no native effects."""
from copy import deepcopy
import json
import unittest

import test_v2_bootstrap_research as fixtures


class DiversionRecoveryTests(unittest.TestCase):
    def setUp(self):
        f=fixtures.BootstrapResearchTests();f.setUp();self.addCleanup(f.doCleanups);self.f=f
        self.c=f.make('electric-mining-drill');self.obs=f.obs;self.key='research-job';self.events=[]
        self.ledger=dict(schema_version=2,world_id='w',jobs={self.key:dict(key=self.key,world_id='w',capability='bootstrap_research',
            parameters={'technology':'electric-mining-drill'},created_tick=900,status='waiting')})
        for tick,item in ((1000,'iron-plate'),(1010,'copper-plate')):
            self.obs['tick']=tick;action=self.c.step(self.obs)
            self.assertEqual(action['item'],item)
            receipt=dict(ok=True,status='succeeded',moved=action['count'])
            self.pair(action,receipt,tick);self.c.record_action(action,receipt)
        self.obs['research']='electric-mining-drill';f.red()['products_finished']+=19
        self.gear=next(e for e in self.obs['entities']if e.get('recipe')=='iron-gear-wheel')
        self.gear['products_finished']+=25
        self.obs['production']['automation-science-pack']['consumed']+=19
        self.c.state.update(collected_packs=19,supplied_packs=19);self.c._save()
        self.baseline={k:deepcopy(self.c.state[k])for k in ('baseline_products_finished','baseline_consumed','required_inputs','required_packs','machines','ports','plan_hash')}
        for tick,count in ((1100,3),(1200,1),(1300,2)):
            action,receipt=self.take(count);self.pair(action,receipt,tick)
        self.obs['tick']=2000;self.flush()

    def pair(self,action,receipt,tick):
        self.events.append(dict(key=self.key,event='action_intent',tick=tick,action=deepcopy(action)))
        if receipt is not None:self.events.append(dict(key=self.key,event='action_result',tick=tick,result=deepcopy(receipt)))

    def take(self,count):
        source={k:deepcopy(self.gear[k])for k in ('name','position','unit_number')}
        action=dict(type='take_many',item='iron-gear-wheel',count=10,world_id='w',sources=[dict(source,kind='output')],reason='collect live construction stocks iron-gear-wheel')
        receipt=dict(ok=True,status='succeeded',moved=count,requested=10,sources=[dict(source,moved=count,status='succeeded',available=count)])
        return action,receipt

    def flush(self):
        (self.f.root/'jobs.json').write_text(json.dumps(self.ledger),encoding='utf-8')
        (self.f.root/'job-events.jsonl').write_text(''.join(json.dumps(row)+'\n'for row in self.events),encoding='utf-8')

    def reload(self):self.c=self.f.make('electric-mining-drill');return self.c

    def test_local_power_requires_live_energy_recipes_and_a_lab(self):
        self.assertTrue(self.c.local_power_ready(self.obs))
        red=self.f.red();saved=deepcopy(red)
        for change in ({'energy':0},{'electric_network_connected':False},{'recipe':'iron-gear-wheel'},{'unit_number':None}):
            red.update(change);self.assertFalse(self.c.local_power_ready(self.obs));red.clear();red.update(saved)
        self.obs['entities']=[e for e in self.obs['entities']if e['name']!='lab']
        self.assertFalse(self.c.local_power_ready(self.obs))

    def test_local_power_rejects_board_binding_from_another_world_or_catalog(self):
        board=self.c.board.state
        for field in ('world_id','catalog_fingerprint'):
            saved=board[field];board[field]='other'
            self.assertFalse(self.c.local_power_ready(self.obs));board[field]=saved

    def activate(self):
        self.assertEqual(self.c.step(self.obs)['status'],'waiting')
        self.obs['tick']+=1800
        return self.c.step(self.obs)

    def test_exact_six_receipted_gears_require_only_twelve_paid_iron(self):
        before=self.c.path.read_bytes();proposal=self.c.prepare_diversion_recovery(self.obs)
        self.assertTrue(proposal['ok'],proposal);self.assertEqual(proposal['diverted_gears'],6)
        self.assertEqual(proposal['replacement_inputs'],{'iron-plate':12})
        self.assertEqual(len(proposal['recovery']['authorization']['receipts']),3)
        self.assertEqual(proposal['recovery']['authorization']['first_funding']['tick'],1000)
        self.assertEqual(self.c.path.read_bytes(),before)
        action=self.activate();self.assertEqual((action['type'],action['item'],action['count']),('insert','iron-plate',12))
        self.assertEqual(action['position'],self.c.state['ports'][0]['position'])
        self.assertEqual({k:self.c.state[k]for k in self.baseline},self.baseline)
        self.assertEqual(self.c.state['allocated_inputs'],{'iron-plate':50,'copper-plate':25})
        self.f.game.query.assert_not_called()

    def test_duplicate_raw_receipts_and_restart_never_create_a_second_allowance(self):
        self.events+=deepcopy(self.events[-6:]);self.flush()
        action=self.activate();self.c.record_action(action,dict(ok=True,moved=5))
        self.reload();action=self.c.step(self.obs);self.assertEqual(action['count'],7)
        self.c.record_action(action,dict(ok=True,moved=7));self.reload()
        proposal=self.c.prepare_diversion_recovery(self.obs)
        self.assertEqual(proposal['reason'],'no_new_audited_gear_diversion')
        self.assertEqual(len(self.c.state['diversion_recoveries']),1)
        self.assertEqual(self.c.state['diversion_recoveries'][0]['allocated_inputs'],{'iron-plate':12})
        self.assertEqual({k:self.c.state[k]for k in self.baseline},self.baseline)

    def test_pre_funding_history_and_intentional_science_collection_are_excluded(self):
        action,receipt=self.take(9)
        early=[dict(key=self.key,event='action_intent',tick=950,action=action),dict(key=self.key,event='action_result',tick=950,result=receipt)]
        self.events=early+self.events
        self.pair(dict(type='take',item='automation-science-pack',inventory='chest',count=6),dict(ok=True,moved=6),1400)
        self.flush();proposal=self.c.prepare_diversion_recovery(self.obs)
        self.assertTrue(proposal['ok']);self.assertEqual(proposal['diverted_gears'],6)

    def test_wrong_world_unit_position_or_source_kind_never_authorizes_replacement(self):
        original=deepcopy(self.events)
        changes=[('action',lambda a:a.update(world_id='foreign')),
            ('source',lambda s:s.update(unit_number=999)),('source',lambda s:s.update(position={'x':99,'y':99})),
            ('source',lambda s:s.update(kind='chest'))]
        for kind,change in changes:
            with self.subTest(kind=kind):
                self.events=deepcopy(original);action=self.events[-2]['action']
                change(action if kind=='action'else action['sources'][0]);self.flush()
                self.assertFalse(self.c.prepare_diversion_recovery(self.obs)['ok'])
        self.events=original;self.ledger['world_id']='foreign';self.flush()
        self.assertFalse(self.c.prepare_diversion_recovery(self.obs)['ok'])

    def test_unknown_missing_or_unpaired_receipts_fail_closed(self):
        original=deepcopy(self.events)
        for variant in ('unpaired','unknown','overcount','mismatched_tick','missing_funding','intervening_unrelated_intent'):
            with self.subTest(variant=variant):
                self.events=deepcopy(original)
                if variant=='unpaired':self.events.pop()
                elif variant=='unknown':self.events[-1]['result'].pop('moved')
                elif variant=='overcount':self.events[-1]['result']['moved']=20
                elif variant=='mismatched_tick':self.events[-1]['tick']+=1
                elif variant=='intervening_unrelated_intent':self.events.insert(-1,dict(key=self.key,event='action_intent',tick=1300,action=dict(type='wait')))
                else:self.events=self.events[4:]
                self.flush();proposal=self.c.prepare_diversion_recovery(self.obs)
                self.assertFalse(proposal['ok'],variant)
                self.assertNotIn('diversion_recoveries',self.c.state)

    def test_regular_progress_and_unfunded_or_nonempty_inputs_do_not_activate(self):
        self.c.step(self.obs);self.obs['tick']+=1799
        self.assertEqual(self.c.step(self.obs)['status'],'waiting');self.assertNotIn('diversion_recoveries',self.c.state)
        self.gear['products_finished']+=1;self.obs['tick']+=1
        self.c.step(self.obs);self.assertNotIn('diversion_recoveries',self.c.state)
        self.obs['tick']+=1800;self.gear['inventory']['iron-plate']=2
        self.assertEqual(self.c.step(self.obs)['status'],'waiting');self.assertNotIn('diversion_recoveries',self.c.state)
        self.c.state['allocated_inputs']['iron-plate']=49
        self.assertFalse(self.c.prepare_diversion_recovery(self.obs)['ok'])

    def test_repair_funding_is_paid_partial_and_unknown_preserves_durable_intent(self):
        action=self.activate();before=self.c.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'unknown'):self.c.record_action(action,{'ok':True})
        self.assertEqual(self.c.path.read_bytes(),before)
        self.assertEqual(self.reload().step(self.obs)['reason'],'bootstrap_research_transfer_outcome_requires_reconciliation')
        self.c.record_action(action,{'ok':True,'moved':12});self.reload()
        self.assertEqual(self.c.state['diversion_recoveries'][0]['allocated_inputs'],{'iron-plate':12})
        self.assertEqual(self.c.state['required_inputs']['iron-plate'],50)

    def test_actual_raw_shortage_uses_normal_collection_without_expanding_authorization(self):
        self.obs['inventory']['iron-plate']=0;action=self.activate()
        self.assertEqual((action['type'],action['item'],action['count']),('take','iron-plate',12))
        self.assertNotIn('pending_transfer',self.c.state)
        self.reload();again=self.c.step(self.obs);self.assertEqual(again['count'],12)
        self.assertEqual(len(self.c.state['diversion_recoveries']),1)

    def test_original_identity_and_epoch_hash_remain_mandatory(self):
        action=self.activate();self.c.record_action(action,{'ok':True,'moved':12})
        epoch=self.c.state['diversion_recoveries'][0];epoch['authorization']['replacement_inputs']['iron-plate']=14
        self.c._save();self.reload()
        self.assertEqual(self.c.step(self.obs)['reason'],'bootstrap_research_diversion_recovery_contract_changed')
        self.gear['unit_number']+=1
        self.assertEqual(self.c.step(self.obs)['reason'],'bootstrap_research_bound_entity_changed')

    def test_live_recipe_yield_derives_repair_cost(self):
        self.f.recipes['iron-gear-wheel']['products'][0]['amount']=2
        self.assertEqual(self.c._replacement_inputs(6),{'iron-plate':6})

    def test_recovered_case_finishes_with_twentyfive_fresh_products_and_consumed_packs(self):
        action=self.activate();self.c.record_action(action,{'ok':True,'moved':12})
        self.f.red()['products_finished']=self.c.state['baseline_products_finished']+25
        self.obs['production']['automation-science-pack']['consumed']=self.c.state['baseline_consumed']+25
        self.assertIsNone(self.c.proof(self.obs))
        self.obs['technologies']['electric-mining-drill']=True
        proof=self.c.proof(self.obs);self.assertEqual(proof['normally_assembled_packs'],25)
        self.assertEqual(proof['research_science_consumed'],25)
        self.assertEqual(proof['effective_input_allowance'],{'iron-plate':62,'copper-plate':25})
        self.assertEqual(proof['diversion_recoveries'][0]['allocated_inputs'],{'iron-plate':12})
        self.assertEqual(self.c.step(self.obs)['status'],'succeeded')

    def test_legitimate_prior_research_progress_keeps_existing_positive_consumption_proof(self):
        self.obs['technologies']['electric-mining-drill']=True
        self.f.red()['products_finished']=self.c.state['baseline_products_finished']+25
        self.obs['production']['automation-science-pack']['consumed']=self.c.state['baseline_consumed']+1
        self.assertEqual(self.c.proof(self.obs)['research_science_consumed'],1)


if __name__=='__main__':unittest.main()
