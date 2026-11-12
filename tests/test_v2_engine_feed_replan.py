from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.core.deterministic_state import _atomic_json
from factorio_agent.board import entity
from factorio_agent.engine_production import EngineProduction,engine_core,audit_engine_core,_key,_hash,SCOPE_ID
from factorio_agent.smelting import merge_entities
import test_v2_engine_production as engine_tests


class EngineFeedReplanTests(unittest.TestCase):
    def setUp(self):
        engine_tests.EngineControllerTests.setUp(self)
        self.c.state=self.c.prepare(self.obs);core=engine_core(self.cat,118.5,-114.5)
        core['entities'].remove(entity('transport-belt',116.5,-107.5,4));core['required_items']['transport-belt']-=1
        steel=self.c._steel_source(self.obs)['source']
        self.c.state.update(phase='build',iron_source=deepcopy(self.iron),core=core,audit=audit_engine_core(core,self.cat))
        shared=[dict(entity('transport-belt',*s['position'].values(),s['facing']),unit_number=s['unit_number'])for s in(self.iron,steel)]
        old_external=[entity('transport-belt',110.5,-48.5,0),entity('transport-belt',134.5,-95.5,4),
                      entity('transport-belt',144.5,-108.5,12),entity('transport-belt',145.5,-108.5,12)]
        self.c.state['plan']=_plan(merge_entities(core['entities'],old_external,[{k:v for k,v in e.items()if k!='unit_number'}for e in shared]),
            core['ports'],reserved_area=core['reserved_area'],underground_pairs=core['underground_pairs'],layout_policy='test')
        self.c.state['bindings']={_key(e):e['unit_number']for e in shared};self.c._save()
        foreign=entity('transport-belt',110.5,-48.5,4);self.foreign=dict(foreign,unit_number=777)
        self.foreign_plan=_plan([foreign],[],reserved_area=[[176,-64],[194,-53]])
        self.board['blocks']={'engine_production':self.c.state['plan'],'chemical_plastic-bar':self.foreign_plan}
        self.board_path=self.root/'pcb-board.json';self.board_path.write_text(json.dumps(self.board))
        self.chemical_path=self.root/'chemical-production.json';self.chemical_path.write_text(json.dumps({
            'world_id':'w','catalog_fingerprint':'c','products':{'plastic-bar':dict(world_id='w',catalog_fingerprint='c',
                phase='verified',plan=self.foreign_plan,bindings={_key(foreign):777})}}))
        posts=[dict(name='post2',position={'x':135,'y':-95},unit_number=7152,coverage_scope=SCOPE_ID,commissioned=True),
               dict(name='post4',position={'x':146,'y':-109},unit_number=7154,coverage_scope=SCOPE_ID,commissioned=True)]
        self.defense.state={'posts':posts,'coverage_scopes':{SCOPE_ID:{'new_posts':['post2','post4']}}}
        self.defense.coverage_proof.return_value={'posts':deepcopy(posts),'geometry':{'complete':True}}
        self.tap.state={'phase':'verified','parameters':self.c.state['parameters']}
        self.tap.proof=Mock(return_value={'port':deepcopy(self.iron)})
        self.obs['entities']=shared+[self.foreign]+[dict(entity('gun-turret',*p['position'].values()),unit_number=p['unit_number'])for p in posts]
        def route(game,builder,obs,source,destination,reserved,**kwargs):
            es=[entity('transport-belt',*source['position'].values(),source['facing'])];pairs=[]
            if source['item']=='steel-plate':
                es += [dict(entity('underground-belt',110.5,-47.5,0),belt_to_ground_type='input'),
                       dict(entity('underground-belt',110.5,-49.5,0),belt_to_ground_type='output')]
                pairs=[dict(input=[110.5,-47.5],output=[110.5,-49.5],direction=0,span=2)]
            return _plan(es,[],underground_pairs=pairs,route_metrics={'length':12,'turns':2})
        self.route.side_effect=route
        self.pipe=patch('factorio_agent.engine_production.surface_pipe_proof',return_value={'ok':True}).start()

    def test_readonly_replan_releases_only_exact_receiver_and_keeps_foreign_out_of_bindings(self):
        original=self.c.path.read_bytes();board=self.board_path.read_bytes()
        with patch('factorio_agent.engine_production._atomic_json')as write:
            candidate=self.c.prepare_prebuild_feed_replan(self.obs);write.assert_not_called()
        self.assertTrue(candidate['ok'],candidate)
        self.assertEqual(candidate['core'],engine_core(self.cat,118.5,-114.5))
        self.assertTrue(candidate['audit']['ok']);self.assertNotEqual(candidate['old_core_revision'],candidate['new_core_revision'])
        self.assertEqual(candidate['ignored_old_posts'],[7152,7154])
        self.assertEqual(candidate['preserved_foreign_assets'][0]['unit_number'],777)
        self.assertNotIn(_key(self.foreign),candidate['bindings'])
        self.assertNotIn(self.foreign['position'],[e['position']for e in candidate['plan']['entities']])
        for call in self.route.call_args_list[-2:]:
            self.assertEqual(call.kwargs['released_unbuilt_receiver'],{'block':'engine_production','plan_revision':_hash(self.c.state['plan'])})
            self.assertEqual(call.kwargs['ignored_units'],[7152,7154])
        self.assertEqual(self.c.path.read_bytes(),original);self.assertEqual(self.board_path.read_bytes(),board)

    def test_unknown_foreign_unit_core_asset_owned_binding_and_relocation_costs_block(self):
        self.obs['entities'][2]['unit_number']=778
        self.assertEqual(self.c.prepare_prebuild_feed_replan(self.obs)['reason'],'unbound_engine_asset_is_not_exact_verified_foreign_asset')
        self.obs['entities'][2]['unit_number']=777
        self.obs['entities'].append(dict(self.c.state['core']['machines'][0],unit_number=888))
        self.assertEqual(self.c.prepare_prebuild_feed_replan(self.obs)['reason'],'engine_core_construction_already_started');self.obs['entities'].pop()
        self.c.state['bindings']['owned-missing']=888;self.c._save()
        self.assertEqual(self.c.prepare_prebuild_feed_replan(self.obs)['reason'],'engine_owned_consumer_construction_already_started')
        self.c.state['bindings'].pop('owned-missing');self.c._save();self.c.relocation=SimpleNamespace(state={'paid':True})
        self.assertEqual(self.c.prepare_prebuild_feed_replan(self.obs)['reason'],'engine_feed_replan_must_precede_relocation_costs')

    def test_source_or_board_revision_changes_never_overwrite_authority(self):
        self.obs['entities'][0]['unit_number']=999
        self.assertEqual(self.c.prepare_prebuild_feed_replan(self.obs)['reason'],'engine_fixed_feed_source_changed')
        self.obs['entities'][0]['unit_number']=90
        board=json.loads(self.board_path.read_text());board['blocks']['engine_production']['layout_policy']='foreign-change'
        self.board_path.write_text(json.dumps(board));before=self.board_path.read_bytes()
        self.assertEqual(self.c.prepare_prebuild_feed_replan(self.obs)['reason'],'engine_feed_replan_registered_plan_changed')
        self.assertEqual(self.board_path.read_bytes(),before)

    def test_later_verified_tap_does_not_replace_the_original_engine_source_contract(self):
        self.tap.state={'phase':'verified','parameters':{'name':'advanced-circuit-copper'}}
        source=self.c.state['iron_source'];split=source['priority_splitter']
        bus=self.board['shared_main_bus'];bus['ports'].append({k:v for k,v in source.items()if k not in('unit_number','priority_splitter')})
        bus['stations'].append(dict(name=source['name'],priority='surplus',splitter=dict(split)))
        bus['entities'].append(dict(entity('splitter',*split['position'].values(),0),splitter_input_priority='left',splitter_output_priority='left'))
        docs=self.tap._documents();docs['board']=self.board;docs['factory'].update(phase='verified',bus=bus)
        self.tap._documents.return_value=docs;self.board_path.write_text(json.dumps(self.board))
        self.assertTrue(self.c.prepare_prebuild_feed_replan(self.obs)['ok'])
        bus['ports'][-1]['position']={'x':13.5,'y':-92.5}
        self.assertEqual(self.c.prepare_prebuild_feed_replan(self.obs)['reason'],'engine_fixed_tap_proof_missing')

    def test_step_replans_before_bind_or_relocation_and_adds_only_one_core_belt(self):
        core=deepcopy(self.c.state['core']);bindings=deepcopy(self.c.state['bindings'])
        self.c.relocation=SimpleNamespace(state={},active=False,step=Mock())
        result=self.c.step(self.obs)
        self.assertEqual(result['reason'],'engine_unbuilt_feeds_replanned_before_defense_relocation')
        expected=deepcopy(self.c.state['core']);expected['entities'].remove(entity('transport-belt',116.5,-107.5,4))
        expected['required_items']['transport-belt']-=1
        self.assertEqual(expected,core);self.assertTrue(self.c.state['audit']['ok']);self.assertEqual(self.c.state['bindings'],bindings)
        self.assertEqual(self.c.state['feed_replan']['phase'],'adopted');self.c.relocation.step.assert_not_called()
        self.assertEqual(json.loads(self.board_path.read_text())['blocks']['engine_production'],self.c.state['plan'])
        self.assertEqual(self.c.state['preserved_foreign_assets'][0]['unit_number'],777)

    def test_publication_interrupt_resumes_new_board_old_state_with_exact_intent(self):
        candidate=self.c.prepare_prebuild_feed_replan(self.obs);self.assertTrue(candidate['ok'])
        self.c.state['feed_replan']=dict(attempt=1,phase='publishing',old_plan_revision=_hash(self.c.state['plan']),
            new_plan_revision=candidate['new_plan_revision'],guard=self.c._feed_guard(),candidate=candidate)
        self.c._save()
        def interrupted(path,data):
            if Path(path)==self.c.path:raise OSError('interrupted between board and producer publication')
            return _atomic_json(path,data)
        with patch('factorio_agent.engine_production._atomic_json',side_effect=interrupted):
            with self.assertRaises(OSError):self.c._publish_feed_replan(self.obs)
        self.assertEqual(json.loads(self.c.path.read_text())['feed_replan']['phase'],'publishing')
        resumed=EngineProduction(self.game,self.builder,self.cat,self.defense,tap=self.tap,steel=self.steel)
        self.assertEqual(resumed.step(self.obs)['reason'],'engine_unbuilt_feeds_replanned_before_defense_relocation')
        self.assertEqual(resumed.state['feed_replan']['phase'],'adopted')
        self.assertEqual(resumed.state['core'],candidate['core']);self.assertEqual(resumed.state['audit'],candidate['audit'])
        self.assertNotIn(_key(self.foreign),resumed.state['bindings'])

    def test_publication_rejects_new_foreign_future_authority_without_overwriting_it(self):
        candidate=self.c.prepare_prebuild_feed_replan(self.obs)
        self.c.state['feed_replan']=dict(attempt=1,phase='publishing',old_plan_revision=_hash(self.c.state['plan']),
            new_plan_revision=candidate['new_plan_revision'],guard=self.c._feed_guard(),candidate=candidate);self.c._save()
        board=json.loads(self.board_path.read_text());board['blocks']['new-advanced']={'reserved_area':[[80,80],[90,90]],'entities':[]}
        self.board_path.write_text(json.dumps(board));before=self.board_path.read_bytes()
        self.assertEqual(self.c._publish_feed_replan(self.obs)['reason'],'engine_feed_replan_foreign_environment_changed')
        self.assertEqual(self.board_path.read_bytes(),before)

    def test_noncanonical_old_core_or_changed_publication_core_is_not_adopted(self):
        self.c.state['core']['layout_policy']='unexpected-core-revision';self.c._save()
        self.assertEqual(self.c.prepare_prebuild_feed_replan(self.obs)['reason'],'engine_prebuild_core_is_not_exact_canonical_or_legacy_tail')
        self.c.state['core']['layout_policy']='fixed-engine-component-headers-v1';self.c._save()
        candidate=self.c.prepare_prebuild_feed_replan(self.obs);self.assertTrue(candidate['ok'])
        candidate['core']['layout_policy']='unexpected-candidate-core-revision'
        self.c.state['feed_replan']=dict(attempt=1,phase='publishing',old_plan_revision=_hash(self.c.state['plan']),
            new_plan_revision=candidate['new_plan_revision'],guard=self.c._feed_guard(),candidate=candidate);self.c._save()
        before=self.board_path.read_bytes()
        self.assertEqual(self.c._publish_feed_replan(self.obs)['reason'],'engine_feed_publication_core_upgrade_changed')
        self.assertEqual(self.board_path.read_bytes(),before)

    def test_failed_automatic_attempt_is_not_repeated_and_preserved_foreign_change_blocks(self):
        self.route.side_effect=None;self.route.return_value={'ok':False,'reason':'no_clean_trunk_corridor'}
        self.assertEqual(self.c.step(self.obs)['reason'],'engine_prebuild_feed_replan_failed')
        calls=self.route.call_count;self.assertEqual(self.c.step(self.obs)['reason'],'engine_feed_replan_already_attempted')
        self.assertEqual(self.route.call_count,calls)
        observed=self.obs['entities'].pop(2)
        self.assertEqual(self.c.step(self.obs)['reason'],'engine_feed_replan_already_attempted')
        self.obs['entities'].insert(2,observed)
        self.c.state['preserved_foreign_assets']=[dict(self.foreign,owner='chemical_plastic-bar',world_id='w')]
        self.c.state['preserved_foreign_assets'][0]['direction']=4
        self.c.state['feed_replan']['phase']='adopted'
        self.obs['entities'][2]['unit_number']=778
        self.assertEqual(self.c.step(self.obs)['reason'],'engine_preserved_foreign_asset_changed')


if __name__=='__main__':unittest.main()
