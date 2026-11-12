"""Known-replacement repurchase and new-map receipt-first logistics funding."""
from copy import deepcopy
import json
import unittest

import test_v2_bootstrap_research as fixtures


class LegacyScienceRecoveryTests(unittest.TestCase):
    def setUp(self):
        f=fixtures.BootstrapResearchTests();f.setUp();self.addCleanup(f.doCleanups);self.f=f;self.obs=f.obs
        self.obs['technologies'].pop('logistics');self.obs.update(research='logistics',research_progress=0,tick=6000)
        self.obs['production']['automation-science-pack']={'produced':45,'consumed':45}
        self.gear=next(e for e in self.obs['entities']if e.get('recipe')=='iron-gear-wheel');self.gear['products_finished']=41
        f.red()['products_finished']=35;f.red()['inventory']={'copper-plate':2}
        self.copper=next(e for e in self.obs['entities']if e['name']=='wooden-chest'and e['position']==f.plan['ports'][1]['position'])
        self.copper['inventory']={'copper-plate':18}
        self.iron=next(e for e in self.obs['entities']if e['name']=='wooden-chest'and e['position']==f.plan['ports'][0]['position'])
        self.old=dict(world_id='w',required_packs=20,allocated_inputs={'iron-plate':40,'copper-plate':20},baseline_produced=10,baseline_consumed=10)
        (f.root/'bootstrap-logistics.json').write_text(json.dumps(self.old),encoding='utf-8');self.c=self.make()
        self.events=[];self.ledger=dict(schema_version=2,world_id='w',jobs={'old':dict(key='old',world_id='w',capability='bootstrap_logistics',parameters={})})
        build=dict(type='build',name='wooden-chest',position=self.iron['position'])
        self.pair(build,dict(ok=True,unit_number=1099),1000)
        self.pair(dict(type='insert',name='wooden-chest',position=self.iron['position'],inventory='chest',item='iron-plate',count=40,
            reason='fund finite normal machine-science inputs'),dict(ok=True,moved=40),2000)
        self.pair(build,dict(ok=True,unit_number=self.iron['unit_number']),2720)
        self.flush()

    def make(self):return self.f.make('logistics')
    def pair(self,action,result,tick):
        self.events.extend([dict(key='old',event='action_intent',tick=tick,action=deepcopy(action)),dict(key='old',event='action_result',tick=tick,result=deepcopy(result))])
    def flush(self):
        (self.f.root/'jobs.json').write_text(json.dumps(self.ledger),encoding='utf-8')
        (self.f.root/'job-events.jsonl').write_text(''.join(json.dumps(e)+'\n'for e in self.events),encoding='utf-8')
    def activate(self):
        self.assertEqual(self.c.step(self.obs)['status'],'waiting');self.obs['tick']+=1800
        return self.c.step(self.obs)

    def test_readonly_proposal_proves_paid_replacement_without_inventing_goods_loss(self):
        before=self.c.path.read_bytes();proposal=self.c.prepare_legacy_repurchase(self.obs)
        self.assertTrue(proposal['ok'],proposal);auth=proposal['epoch']['authorization']
        self.assertEqual(auth['repurchase_inputs'],{'iron-plate':40});self.assertEqual(auth['preserved_copper'],20)
        self.assertEqual(auth['receipt_chain']['funding']['original_chest']['unit_number'],1099)
        self.assertEqual(auth['receipt_chain']['replacement']['unit_number'],self.iron['unit_number'])
        self.assertEqual(auth['historical_goods_loss_quantity'],'unknown')
        self.assertEqual(self.c.path.read_bytes(),before);self.f.game.query.assert_not_called()

    def test_one_paid_epoch_preserves_original_metadata_and_partial_restart_budget(self):
        action=self.activate();self.assertEqual((action['type'],action['item'],action['count']),('insert','iron-plate',40))
        self.c.record_action(action,dict(ok=True,moved=12));self.c=self.make()
        action=self.c.step(self.obs);self.assertEqual(action['count'],28)
        self.c.record_action(action,dict(ok=True,moved=28));self.c=self.make()
        self.assertEqual({k:self.c.state[k]for k in self.old},self.old)
        self.assertEqual(self.c.state['legacy_repurchase']['allocated_inputs'],{'iron-plate':40})
        self.assertFalse(self.c.prepare_legacy_repurchase(self.obs)['ok'])
        self.assertEqual(self.c.step(self.obs)['status'],'waiting')

    def test_unknown_ack_preserves_exact_intent_and_never_repays(self):
        action=self.activate();before=self.c.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'unknown'):self.c.record_action(action,dict(ok=True))
        self.assertEqual(self.c.path.read_bytes(),before);self.c=self.make()
        self.assertEqual(self.c.step(self.obs)['reason'],'bootstrap_research_transfer_outcome_requires_reconciliation')

    def test_reused_birth_missing_funding_wrong_world_and_unknown_result_reject(self):
        original=deepcopy(self.events)
        for change in ('reused','missing','unknown','world','pending'):
            with self.subTest(change=change):
                self.events=deepcopy(original);self.ledger['world_id']='w'
                self.ledger['jobs']['old'].pop('pending_action',None)
                if change=='reused':self.events[-1]['result']['reused']=True
                elif change=='missing':self.events=self.events[:2]+self.events[4:]
                elif change=='unknown':self.events[3]['result'].pop('moved')
                elif change=='pending':self.ledger['jobs']['old']['pending_action']={'type':'take'}
                else:self.ledger['world_id']='foreign'
                self.flush();self.assertFalse(self.c.prepare_legacy_repurchase(self.obs)['ok'])

    def test_actor_lab_science_progress_or_nonempty_feed_prevents_new_epoch(self):
        cases=[(self.obs['inventory'],'automation-science-pack',1),(self.gear['inventory'],'iron-plate',1),
            (self.gear['inventory'],'iron-gear-wheel',1),(self.f.red()['inventory'],'copper-plate',1),
            (next(e for e in self.obs['entities']if e['name']=='lab')['inventory'],'automation-science-pack',1)]
        for mapping,key,value in cases:
            old=deepcopy(mapping);mapping[key]=value
            self.assertFalse(self.c.prepare_legacy_repurchase(self.obs)['ok']);mapping.clear();mapping.update(old)
        self.obs['research_progress']=.05
        self.assertFalse(self.c.prepare_legacy_repurchase(self.obs)['ok'])

    def test_no_repurchase_before_thirty_seconds_and_new_native_progress_restarts_wait(self):
        self.c.step(self.obs);self.obs['tick']+=1799;self.c.step(self.obs)
        self.assertNotIn('legacy_repurchase',self.c.state)
        self.f.red()['products_finished']+=1;self.obs['tick']+=1;self.c.step(self.obs)
        self.assertNotIn('legacy_repurchase',self.c.state)

    def test_current_unit_or_original_metadata_change_blocks_continuation(self):
        action=self.activate();self.c.record_action(action,dict(ok=True,moved=40))
        self.gear['unit_number']+=1
        self.assertEqual(self.c.step(self.obs)['reason'],'legacy_science_repurchase_binding_changed')
        self.gear['unit_number']-=1;self.c.state['allocated_inputs']['iron-plate']=0
        self.assertEqual(self.c.step(self.obs)['reason'],'legacy_science_repurchase_binding_changed')

    def test_lifetime_thirtyfive_old_science_cannot_prove_new_twenty_pack_repurchase(self):
        action=self.activate();self.c.record_action(action,dict(ok=True,moved=40))
        self.obs['technologies']['logistics']=True
        self.assertIsNone(self.c.proof(self.obs))
        self.f.red()['products_finished']+=20;self.obs['production']['automation-science-pack'].update(produced=65,consumed=64)
        self.assertIsNone(self.c.proof(self.obs))
        self.obs['production']['automation-science-pack']['consumed']=65
        proof=self.c.proof(self.obs);self.assertEqual(proof['normally_assembled_packs'],20)
        self.assertEqual(proof['research_science_consumed'],20);self.assertEqual(proof['historical_goods_loss_quantity'],'unknown')
        self.assertEqual(self.c.step(self.obs)['status'],'succeeded')

    def test_newly_collected_output_alone_can_fund_the_lab(self):
        action=self.activate();self.c.record_action(action,dict(ok=True,moved=40))
        self.obs['inventory']['automation-science-pack']=10
        self.assertEqual(self.c.step(self.obs)['status'],'waiting')
        output=next(e for e in self.obs['entities']if e['name']=='wooden-chest'and e['position']==self.f.plan['ports'][-1]['position'])
        output['inventory']['automation-science-pack']=4
        action=self.c.step(self.obs);self.assertEqual((action['type'],action['count']),('take',4))
        self.c.record_action(action,dict(ok=True,moved=4))
        action=self.c.step(self.obs);self.assertEqual((action['type'],action['count']),('insert',4))
        self.c.record_action(action,dict(ok=True,moved=4))
        self.assertEqual(self.c.state['legacy_repurchase']['supplied_packs'],4)

    def test_fresh_logistics_uses_receipt_first_budget_and_preserves_unknown_restart(self):
        self.c.path.unlink();self.c=self.make();action=self.c.step(self.obs)
        self.assertEqual((action['type'],action['item'],action['count']),('insert','iron-plate',40))
        self.assertEqual(self.c.state['allocated_inputs'],{});self.assertEqual(self.c.state['schema_version'],1)
        self.c.record_action(action,dict(ok=True,moved=10));self.c=self.make()
        action=self.c.step(self.obs);self.assertEqual(action['count'],30)
        with self.assertRaisesRegex(ValueError,'unknown'):self.c.record_action(action,dict(ok=True))
        self.c=self.make();self.assertEqual(self.c.step(self.obs)['reason'],'bootstrap_research_transfer_outcome_requires_reconciliation')

    def test_fresh_logistics_machine_baseline_and_completion_use_strict_engine(self):
        self.c.path.unlink();self.c=self.make()
        for item in ('iron-plate','copper-plate'):
            action=self.c.step(self.obs);self.assertEqual(action['item'],item)
            self.c.record_action(action,dict(ok=True,moved=action['count']))
        self.obs['technologies']['logistics']=True
        self.assertIsNone(self.c.proof(self.obs));self.f.red()['products_finished']+=20
        self.obs['production']['automation-science-pack']['consumed']+=20
        self.assertEqual(self.c.proof(self.obs)['normally_assembled_packs'],20)
        self.assertEqual(self.c.step(self.obs)['status'],'succeeded')


if __name__=='__main__':unittest.main()
