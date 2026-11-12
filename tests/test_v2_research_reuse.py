from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_agent.jobs import StarterExecutors


class FundedResearchReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.executor=StarterExecutors.__new__(StarterExecutors)
        self.executor.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root))
        self.executor.builder=SimpleNamespace(_sync=Mock())
        self.executor._approved_initial_defense=Mock(return_value=None)
        self.batch=SimpleNamespace(state={'world_id':'w','allocated_inputs':{'iron-plate':50}},
            step=Mock(return_value={'status':'waiting','reason':'repairing_existing_machine_batch'}),
            proof=Mock(return_value={'native_researched':True,'normally_assembled_packs':25}),record_action=Mock())
        self.executor._bootstrap_research=Mock(return_value=self.batch)
        self.params={'technology':'electric-mining-drill'};self.obs={'world_id':'w','tick':1}

    def create_batch(self):
        (self.root/'bootstrap-research-electric-mining-drill.json').write_text('{}',encoding='utf-8')

    def test_existing_machine_contract_is_executed_before_remote_power_or_new_handcraft_budget(self):
        self.create_batch()
        with patch('factorio_agent.core.deterministic_repair_control.pending_repair',return_value=None):
            result=self.executor.step('starter_research',self.obs,self.params)
        self.assertEqual(result['reason'],'repairing_existing_machine_batch')
        self.batch.step.assert_called_once_with(self.obs)

    def test_no_real_batch_or_different_technology_does_not_force_machine_tier(self):
        self.assertIsNone(self.executor._existing_startup_batch(self.params))
        self.executor._bootstrap_research.assert_not_called()
        self.create_batch();self.assertIsNone(self.executor._existing_startup_batch({'technology':'automation'}))
        self.batch.state={};self.assertIsNone(self.executor._existing_startup_batch(self.params))

    def test_bound_batch_failure_is_preserved_instead_of_silently_funding_another_path(self):
        self.create_batch();self.batch.step.return_value={'status':'blocked','reason':'bootstrap_research_world_changed'}
        with patch('factorio_agent.core.deterministic_repair_control.pending_repair',return_value=None):
            result=self.executor.step('starter_research',self.obs,self.params)
        self.assertEqual(result['reason'],'bootstrap_research_world_changed')

    def test_same_goal_transfer_receipt_and_completion_proof_stay_with_original_owner(self):
        self.create_batch();action={'type':'insert','item':'iron-plate','count':12};receipt={'ok':True,'moved':12}
        self.executor.record_action('starter_research',self.obs,action,receipt,self.params)
        self.batch.record_action.assert_called_once_with(action,receipt)
        proof=self.executor.proof('starter_research',self.obs,{},self.params)
        self.assertEqual(proof['normally_assembled_packs'],25);self.batch.proof.assert_called_once_with(self.obs)

    def test_native_completed_target_does_not_wait_for_obsolete_remote_power_or_machine_contract(self):
        self.create_batch();self.obs['technologies']={'electric-mining-drill':True}
        self.executor.research=SimpleNamespace(step=Mock(return_value={'status':'succeeded','reason':'target researched naturally'}),
            proof=Mock(return_value={'technology':'electric-mining-drill','already_researched':True}))
        with patch('factorio_agent.core.deterministic_repair_control.pending_repair',return_value=None):
            result=self.executor.step('starter_research',self.obs,self.params)
        self.assertEqual(result['status'],'succeeded');self.batch.step.assert_not_called()
        self.assertTrue(self.executor.proof('starter_research',self.obs,{},self.params)['already_researched'])

    def test_logistics_resumes_paid_current_owner_instead_of_reusing_its_shared_machines(self):
        self.create_batch();self.obs['research']='electric-mining-drill'
        with patch('factorio_agent.core.deterministic_repair_control.pending_repair',return_value=None):
            result=self.executor.step('bootstrap_logistics',self.obs,{})
        self.assertEqual(result['reason'],'repairing_existing_machine_batch')
        action={'type':'insert','item':'iron-plate','count':12};receipt={'ok':True,'moved':12}
        self.executor.record_action('bootstrap_logistics',self.obs,action,receipt,{})
        self.batch.record_action.assert_called_once_with(action,receipt)

    def test_prerequisite_completion_never_claims_logistics_completion(self):
        self.create_batch();self.obs['research']='electric-mining-drill'
        self.batch.step.return_value={'status':'succeeded','evidence':{'technology':'electric-mining-drill'}}
        with patch('factorio_agent.core.deterministic_repair_control.pending_repair',return_value=None):
            result=self.executor.step('bootstrap_logistics',self.obs,{})
        self.assertEqual(result['status'],'waiting')
        self.assertEqual(result['reason'],'funded_science_prerequisite_finished')

    def test_unfunded_or_native_finished_owner_does_not_capture_the_cell(self):
        self.create_batch();self.obs['research']='electric-mining-drill'
        self.batch.state['allocated_inputs']={}
        self.assertIsNone(self.executor._funded_startup_owner(self.obs))
        self.batch.state['allocated_inputs']={'iron-plate':50}
        self.obs['technologies']={'electric-mining-drill':True}
        self.assertIsNone(self.executor._funded_startup_owner(self.obs))

    def test_any_funded_finite_red_batch_owns_its_running_research(self):
        (self.root/'bootstrap-research-steel-processing.json').write_text('{}',encoding='utf-8')
        self.obs['research']='steel-processing'
        self.assertIs(self.executor._funded_startup_owner(self.obs),self.batch)
        self.executor._bootstrap_research.assert_called_once_with({'technology':'steel-processing'})
        for running in('radar','logistics',None,'../escape',7):
            self.obs['research']=running
            self.assertIsNone(self.executor._funded_startup_owner(self.obs))

    def test_powered_local_science_runs_without_claiming_remote_source_is_ready(self):
        self.executor.industry=SimpleNamespace(local_power_ready=Mock(return_value=True),
            step=Mock(return_value={'status':'waiting','reason':'native_machine_science'}))
        with patch('factorio_agent.core.deterministic_repair_control.pending_repair',return_value=None):
            result=self.executor.step('bootstrap_logistics',self.obs,{})
        self.assertEqual(result['reason'],'native_machine_science')
        self.executor.industry.local_power_ready.assert_called_once_with(self.obs)

    def test_logistics_acknowledges_its_own_recovery_after_shared_owner_finishes(self):
        self.executor.industry=SimpleNamespace(record_action=Mock())
        action={'type':'insert','item':'iron-plate','count':40};receipt={'ok':True,'moved':40}
        self.executor.record_action('bootstrap_logistics',self.obs,action,receipt,{})
        self.executor.industry.record_action.assert_called_once_with(action,receipt)
        self.batch.record_action.assert_not_called()
