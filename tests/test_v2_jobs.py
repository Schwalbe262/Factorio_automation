"""Safety/recovery tests for real multi-process gameplay job boundaries."""
import json
import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock,patch
import subprocess
import sys
import tempfile
import unittest

from factorio_agent.core.deterministic_state import RunLock
from factorio_agent.jobs import Ledger, Request, control, progress_key, run_slice, submit
from factorio_agent.__main__ import main


class Game:
    def __init__(self):
        self.observation = {"ok": True, "world_id": "world", "tick": 100, "entities": [], "production": {}}
        self.actions = []
        self.lose_receipt = False
        self.saved = 0

    def observe(self):
        return json.loads(json.dumps(self.observation))

    def act(self, action):
        self.actions.append(action)
        if self.lose_receipt:
            raise TimeoutError("server outcome unknown")
        return {"ok": True}

    def save(self):
        self.saved += 1


class Executor:
    def __init__(self, result, proof=None):
        self.result, self.evidence = result, proof
        self.calls = 0

    def step(self, capability, obs, parameters=None):
        self.calls += 1
        return self.result

    def proof(self, capability, obs, result, parameters=None):
        return self.evidence


class JobBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = Game()

    def request(self, key="test", capability="starter", tick=100):
        return Request(key, "world", tick, capability)

    def test_unrelated_ore_production_cannot_mask_stalled_science(self):
        obs=self.game.observation
        for capability in ('bootstrap_logistics','bootstrap_research','starter_research'):
            before=progress_key(obs,capability)
            obs['production']['iron-plate']={'produced':100,'consumed':20}
            obs['production']['coal']={'produced':400,'consumed':300}
            obs['inventory']={'iron-plate':300}
            self.assertEqual(before,progress_key(obs,capability))
            obs['production'].clear()

    def test_real_science_or_research_advances_the_science_watchdog(self):
        obs=self.game.observation
        for field,value in (('research','logistics'),('research_progress',.1)):
            before=progress_key(obs,'bootstrap_logistics');obs[field]=value
            self.assertNotEqual(before,progress_key(obs,'bootstrap_logistics'))
        before=progress_key(obs,'bootstrap_logistics')
        obs['production']['automation-science-pack']={'produced':1,'consumed':0}
        self.assertNotEqual(before,progress_key(obs,'bootstrap_logistics'))

    def test_science_construction_counts_while_unrelated_defense_does_not(self):
        obs=self.game.observation;before=progress_key(obs,'bootstrap_logistics')
        obs['entities']=[{'name':'gun-turret','unit_number':1}]
        self.assertEqual(before,progress_key(obs,'bootstrap_logistics'))
        obs['entities'].append({'name':'assembling-machine-1','unit_number':2})
        self.assertNotEqual(before,progress_key(obs,'bootstrap_logistics'))

    def test_worker_stop_is_lock_independent_input_and_stops_before_next_action(self):
        submit(self.root, self.game, self.request())
        before = (self.root / 'jobs.json').read_bytes()
        with RunLock(self.root / 'writer.lock'), redirect_stdout(io.StringIO()):
            main(['--runtime', str(self.root), 'stop-worker'])
        self.assertEqual((self.root / 'jobs.json').read_bytes(), before)
        executor = Executor({'type': 'craft', 'recipe': 'transport-belt', 'count': 1})
        result = run_slice(self.root, self.game, executor, 'test', seconds=.1)
        self.assertEqual(result['reason'], 'operator_stop_requested')
        self.assertEqual(executor.calls, 0)
        self.assertEqual(self.game.actions, [])
        control(self.root, self.game, 'test', 'pause')
        with redirect_stdout(io.StringIO()):
            main(['--runtime', str(self.root), 'clear-worker-stop'])
        self.assertFalse((self.root / 'stop.json').exists())
        self.assertEqual(Ledger(self.root).data['jobs']['test']['status'], 'paused')

    def test_cli_does_not_clear_another_operator_stop_marker(self):
        marker = self.root / 'stop.json'
        marker.write_text('{"source":"human-emergency-stop"}', encoding='utf-8')
        before = marker.read_bytes()
        with redirect_stdout(io.StringIO()):
            main(['--runtime', str(self.root), 'stop-worker'])
        self.assertEqual(marker.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'another operator'):
            main(['--runtime', str(self.root), 'clear-worker-stop'])
        self.assertEqual(marker.read_bytes(), before)

    def test_retry_is_idempotent_but_key_cannot_change_effect(self):
        original = submit(self.root, self.game, self.request())
        self.game.observation["tick"] = 6000
        # Retried request may have a stale observation; it returns the same receipt.
        self.assertEqual(submit(self.root, self.game, self.request()), original)
        with self.assertRaisesRegex(ValueError, "another request"):
            submit(self.root, self.game, self.request(capability="power"))
        self.assertEqual(len(Ledger(self.root).data["jobs"]), 1)

    def test_world_and_snapshot_binding_reject_wrong_requests(self):
        for request in (Request("x", "other", 100, "starter"), self.request(tick=101)):
            with self.assertRaises(ValueError):
                submit(self.root, self.game, request)
        self.game.observation["tick"] = 4000
        with self.assertRaisesRegex(ValueError, "stale"):
            submit(self.root, self.game, self.request())

    def test_missing_prerequisite_or_second_active_job_rejected(self):
        with self.assertRaisesRegex(ValueError, "prerequisites"):
            submit(self.root, self.game, self.request(capability="power"))
        submit(self.root, self.game, self.request())
        with self.assertRaisesRegex(ValueError, "another job"):
            submit(self.root, self.game, self.request(key="other"))

    def test_executor_success_needs_independent_postcondition(self):
        submit(self.root, self.game, self.request())
        result = run_slice(self.root, self.game, Executor({"status": "succeeded"}), "test")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "postcondition_not_verified")

    def test_action_marks_old_production_evidence_as_retained_without_erasing_it(self):
        submit(self.root, self.game, self.request())
        old = {'machines_finished': [18, 11, 2, 1, 0, 0]}
        run_slice(self.root, self.game, Executor({'status': 'waiting', 'evidence': old}),
                  'test', seconds=.001)
        self.game.observation['tick'] = 200
        result = run_slice(self.root, self.game, Executor({'type': 'craft', 'recipe': 'lab', 'count': 1}),
                           'test', seconds=.001)
        self.assertEqual(result['evidence'], old)
        self.assertEqual(result['evidence_observed_tick'], 100)
        self.assertEqual(result['evidence_status'], 'retained_from_prior_observation')
        self.assertEqual(result['last_tick'], 200)

    def test_fresh_postcondition_evidence_replaces_retained_observation_metadata(self):
        submit(self.root, self.game, self.request())
        result = run_slice(self.root, self.game,
                           Executor({'status': 'succeeded'}, {'native_output': 145}), 'test')
        self.assertEqual(result['evidence'], {'native_output': 145})
        self.assertEqual(result['evidence_status'], 'verified_postcondition')
        self.assertEqual(result['evidence_observed_tick'], 100)

    def test_proof_exception_cannot_leave_success_in_persisted_receipt(self):
        submit(self.root,self.game,self.request())
        executor=Executor({'status':'succeeded'}, {'measured':True})
        def fail_proof(*args,**kwargs):
            self.assertNotEqual(Ledger(self.root).data['jobs']['test']['status'],'succeeded')
            raise AttributeError('simulated missing postcondition dependency')
        executor.proof=fail_proof
        with self.assertRaises(AttributeError):run_slice(self.root,self.game,executor,'test')
        saved=Ledger(self.root).data['jobs']['test']
        self.assertEqual((saved['status'],saved['reason'],saved['evidence']),
                         ('blocked','postcondition_verification_error',{}))

    def test_blocked_capacity_guard_preserves_exact_scalar_metrics_in_receipt(self):
        submit(self.root,self.game,self.request())
        result=run_slice(self.root,self.game,Executor(dict(status='blocked',reason='expand_mining',
            required_nominal_ore_rate=150,available_nominal_ore_rate=120,capacity_basis='native remaining')), 'test')
        self.assertEqual(result['evidence'],dict(required_nominal_ore_rate=150,
            available_nominal_ore_rate=120,capacity_basis='native remaining'))
        self.assertEqual(Ledger(self.root).data['jobs']['test']['evidence'],result['evidence'])

    def test_blocked_geometry_exposes_one_finite_position_without_large_native_details(self):
        submit(self.root,self.game,self.request())
        result=run_slice(self.root,self.game,Executor({'status':'blocked',
            'reason':'block_protected_entity_obstruction','entity':'transport-belt',
            'position':{'x':-11.5,'y':-140.5},'full_map':[{}]*1000}), 'test')
        self.assertEqual(result['evidence'],{'entity':'transport-belt','position':{'x':-11.5,'y':-140.5}})

    def test_verified_success_survives_process_restart_but_rollback_invalidates_it(self):
        submit(self.root, self.game, self.request())
        result = run_slice(self.root, self.game, Executor({"status": "succeeded"}, {"measured": True}), "test")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(Ledger(self.root).data["jobs"]["test"]["evidence"], {"measured": True})
        self.game.observation["tick"] = 90
        executor = Executor({"type": "build", "name": "lab"})
        result = run_slice(self.root, self.game, executor, "test")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("rollback", result["reason"])
        self.assertEqual(result["evidence"], {})
        self.assertEqual(executor.calls, 0)

    def test_lost_mutation_receipt_is_never_replayed_on_restart(self):
        submit(self.root, self.game, self.request())
        self.game.lose_receipt = True
        executor = Executor({"type": "build", "name": "lab"})
        with self.assertRaises(TimeoutError):
            run_slice(self.root, self.game, executor, "test")
        self.assertEqual(len(self.game.actions), 1)
        self.assertTrue(Ledger(self.root).data["jobs"]["test"]["pending_action"])
        self.game.lose_receipt = False
        result = run_slice(self.root, self.game, executor, "test")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(len(self.game.actions), 1)

    def test_time_slice_yields_without_claiming_success(self):
        submit(self.root, self.game, self.request())
        result = run_slice(self.root, self.game, Executor({"status": "waiting", "reason": "crafting"}), "test", seconds=.001)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["reason"], "crafting")
        self.assertEqual(result['slice_yield_reason'],'execution_slice_complete')
        self.assertEqual(self.game.actions, [])

    def test_real_power_blocker_survives_the_slice_and_restart(self):
        submit(self.root,self.game,self.request())
        result=run_slice(self.root,self.game,Executor({'status':'waiting',
            'reason':'waiting for dedicated coal, water, and steam flow','drill_fuel':0,
            'water':200,'steam':400,'energized_engines':2}),'test',seconds=.001)
        saved=Ledger(self.root).data['jobs']['test']
        self.assertEqual(saved['reason'],result['reason'])
        self.assertEqual(saved['evidence']['drill_fuel'],0)
        self.assertEqual(saved['evidence']['water'],200)
        self.assertEqual(saved['evidence']['steam'],400)
        self.assertNotEqual(saved['reason'],'execution_slice_complete')

    def test_model_can_pause_a_slow_goal_do_another_job_and_resume(self):
        submit(self.root, self.game, self.request())
        control(self.root, self.game, "test", "pause")
        submit(self.root, self.game, self.request(key="other"))
        with self.assertRaisesRegex(ValueError, "another job"):
            control(self.root, self.game, "test", "resume")
        run_slice(self.root, self.game, Executor({"status": "succeeded"}, {"measured": True}), "other")
        control(self.root, self.game, "test", "resume")
        self.assertEqual(Ledger(self.root).data["jobs"]["test"]["status"], "waiting")

    def test_pause_can_preserve_a_job_when_its_character_is_unavailable(self):
        submit(self.root,self.game,self.request())
        self.game.observation={'ok':False,'reason':'agent_dead'}
        self.game.query=Mock(return_value={'ok':True,'world_id':'world','tick':101})
        result=control(self.root,self.game,'test','pause')
        self.assertEqual(result['status'],'paused');self.assertEqual(self.game.actions,[])
        with self.assertRaisesRegex(RuntimeError,'agent_dead'):control(self.root,self.game,'test','resume')

    def test_checkpoint_preserves_native_world_even_when_character_observation_fails(self):
        self.game.query=Mock(return_value={'ok':True,'world_id':'world','tick':101})
        with patch('factorio_agent.runtime.game_for',return_value=self.game),redirect_stdout(io.StringIO()):
            main(['--runtime',str(self.root),'checkpoint'])
        self.assertEqual(self.game.saved,1)

    def test_reconciliation_discards_uncertain_intent_and_requires_fresh_planning(self):
        submit(self.root, self.game, self.request())
        self.game.lose_receipt = True
        with self.assertRaises(TimeoutError):
            run_slice(self.root, self.game, Executor({"type": "build", "name": "lab"}), "test")
        control(self.root, self.game, "test", "reconcile")
        self.game.lose_receipt = False
        run_slice(self.root, self.game, Executor({"status": "succeeded"}, {"observed_lab": True}), "test")
        self.assertEqual(len(self.game.actions), 1)

    def test_retry_replans_known_block_without_erasing_history_or_replaying_actions(self):
        submit(self.root,self.game,self.request())
        run_slice(self.root,self.game,Executor({'status':'blocked','reason':'placement obstructed'}),'test')
        before=Ledger(self.root).data['jobs']['test'].copy()
        control(self.root,self.game,'test','retry')
        current=Ledger(self.root).data['jobs']['test']
        self.assertEqual(current['created_tick'],before['created_tick'])
        self.assertEqual(current['actions'],before['actions'])
        self.assertEqual(self.game.actions,[])
        result=run_slice(self.root,self.game,Executor({'status':'succeeded'},{'native':True}),'test')
        self.assertEqual(result['status'],'succeeded')
        with self.assertRaisesRegex(ValueError,'known blocked'):
            control(self.root,self.game,'test','retry')

    def test_retry_cannot_skip_uncertain_action_reconciliation(self):
        submit(self.root,self.game,self.request());self.game.lose_receipt=True
        with self.assertRaises(TimeoutError):
            run_slice(self.root,self.game,Executor({'type':'build','name':'lab'}),'test')
        with self.assertRaisesRegex(ValueError,'known blocked'):
            control(self.root,self.game,'test','retry')

    def test_unknown_batch_receipt_keeps_durable_intent_for_explicit_reconciliation(self):
        submit(self.root,self.game,self.request())
        self.game.act=lambda _: {'ok':False,'reason':'build_batch_outcome_unknown','completed':2}
        with self.assertRaisesRegex(RuntimeError,'batch outcome unknown'):
            run_slice(self.root,self.game,Executor({'type':'build_many','actions':[]}),'test')
        job=Ledger(self.root).data['jobs']['test']
        self.assertEqual(job['reason'],'action_outcome_unknown_reconcile_required')
        self.assertIn('pending_action',job)

    def test_retry_excludes_only_blocked_wait_and_preserves_consumed_execution_budget(self):
        submit(self.root,self.game,self.request());self.game.observation['tick']=50000
        run_slice(self.root,self.game,Executor({'status':'blocked','reason':'repair needed'}),'test')
        created=Ledger(self.root).data['jobs']['test']['created_tick']
        self.game.observation['tick']=200000
        control(self.root,self.game,'test','retry')
        job=Ledger(self.root).data['jobs']['test']
        self.assertEqual(job['created_tick'],created)
        self.assertEqual(job['paused_ticks'],150000)
        self.game.observation['tick']=300000
        result=run_slice(self.root,self.game,Executor({'status':'succeeded'},{'native':True}),'test')
        self.assertEqual(result['reason'],'job_deadline_exceeded')

    def test_uncertain_storage_deposit_keeps_intent_and_forbids_retry(self):
        submit(self.root,self.game,self.request())
        self.game.act=lambda _: {'ok':False,'reason':'storage_transfer_outcome_unknown','confirmed_moved':10}
        with self.assertRaisesRegex(RuntimeError,'batch outcome unknown'):
            run_slice(self.root,self.game,Executor({'type':'deposit','item':'wood','count':20}),'test')
        job=Ledger(self.root).data['jobs']['test']
        self.assertEqual(job['reason'],'action_outcome_unknown_reconcile_required')
        self.assertIn('pending_action',job)
        self.assertEqual(job['uncertain_batch_receipt']['confirmed_moved'],10)
        with self.assertRaises(ValueError):control(self.root,self.game,'test','retry')

    def test_uncertain_mall_pickup_keeps_intent_and_requires_reobservation_before_replanning(self):
        submit(self.root,self.game,self.request())
        self.game.act=lambda _: {'ok':False,'reason':'mall_stock_transfer_outcome_unknown'}
        with self.assertRaisesRegex(RuntimeError,'batch outcome unknown'):
            run_slice(self.root,self.game,Executor({'type':'take_mall_stock','item':'transport-belt','count':50}),'test')
        job=Ledger(self.root).data['jobs']['test']
        self.assertEqual(job['reason'],'action_outcome_unknown_reconcile_required')
        self.assertEqual(job['pending_action']['count'],50)
        with self.assertRaises(ValueError):control(self.root,self.game,'test','retry')
        control(self.root,self.game,'test','reconcile')
        self.assertNotIn('pending_action',Ledger(self.root).data['jobs']['test'])

    def test_uncertain_paid_pipe_repair_keeps_intent_and_original_paid_prefix(self):
        submit(self.root,self.game,self.request())
        self.game.act=lambda _: {'ok':False,'reason':'infrastructure_repair_outcome_unknown',
                                 'original_reason':'infrastructure_material_conservation_failed','paid_pipes':2}
        with self.assertRaisesRegex(RuntimeError,'batch outcome unknown'):
            run_slice(self.root,self.game,Executor({'type':'repair_registered_infrastructure','transaction_id':'a'*64}),'test')
        job=Ledger(self.root).data['jobs']['test']
        self.assertEqual(job['reason'],'action_outcome_unknown_reconcile_required')
        self.assertEqual(job['pending_action']['transaction_id'],'a'*64)
        self.assertEqual(job['uncertain_batch_receipt']['paid_pipes'],2)
        with self.assertRaises(ValueError):control(self.root,self.game,'test','retry')
        with self.assertRaisesRegex(ValueError,'reviewed native reconciliation'):
            control(self.root,self.game,'test','reconcile')
        self.assertIn('pending_action',Ledger(self.root).data['jobs']['test'])

    def test_unknown_belt_repair_cannot_clear_intent_without_a_matching_review_epoch(self):
        submit(self.root,self.game,self.request())
        self.game.act=lambda _: {'ok':False,'reason':'infrastructure_repair_outcome_unknown','paid_belts':6}
        with self.assertRaises(RuntimeError):
            run_slice(self.root,self.game,Executor({'type':'repair_registered_belts','transaction_id':'a'*64}),'test')
        with self.assertRaisesRegex(ValueError,'reviewed native reconciliation'):
            control(self.root,self.game,'test','reconcile')
        self.assertEqual(Ledger(self.root).data['jobs']['test']['pending_action']['transaction_id'],'a'*64)

    def test_intentional_pause_does_not_spend_execution_deadline(self):
        submit(self.root, self.game, self.request())
        control(self.root, self.game, "test", "pause")
        self.game.observation["tick"] = 200000
        control(self.root, self.game, "test", "resume")
        result = run_slice(self.root, self.game, Executor({"status":"succeeded"}, {"measured":True}), "test")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["paused_ticks"], 199900)

    def test_os_lock_prevents_another_process_writing_same_world(self):
        script = ("from pathlib import Path; from factorio_agent.core.deterministic_state import RunLock; "
                  "RunLock(Path(__import__('sys').argv[1])).acquire()")
        with RunLock(self.root / "writer.lock"):
            result = subprocess.run([sys.executable, "-B", "-c", script, str(self.root / "writer.lock")],
                                    capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another process owns", result.stderr)
        # The lock is released by the OS, not by trusting stale PID metadata.
        with RunLock(self.root / "writer.lock"):
            pass


if __name__ == "__main__":
    unittest.main()
