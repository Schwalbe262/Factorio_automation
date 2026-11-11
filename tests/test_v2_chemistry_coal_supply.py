from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_agent.material_supply import DedicatedMaterials
from factorio_agent.outposts import MiningOutposts
from factorio_agent.paired_mining import mining_pair


class ChemistryCoalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.catalog=SimpleNamespace(fingerprint='c');self.game=SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root),
            query=Mock(return_value={'ok':True,'offset':0,'coal':1}))
        self.builder=SimpleNamespace(_occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,
            ensure_plan=Mock(return_value={'status':'waiting'}))
        self.outposts=MiningOutposts.__new__(MiningOutposts);self.outposts.path=self.root/'mining-outposts.json'
        self.outposts.state={'world_id':'w','catalog_fingerprint':'c','banks':[]}
        self.outposts.bank_proof=Mock(return_value={'drill_units':[100,101],
            'native_producer_lanes':{'ok':True,'left':{'coal':1},'right':{'coal':1}}})
        self.defense=SimpleNamespace(prepare_coverage=Mock(return_value={'ok':True,'new_posts':[]}),
            ensure_coverage=Mock(return_value=None),coverage_proof=Mock(return_value={'covered':True}))
        self.board={'world_id':'w','catalog_fingerprint':'c','blocks':{}}
        (self.root/'pcb-board.json').write_text(json.dumps(self.board))
        self.legacy_sources={name:{'material_supply_id':'old-bank','unit_number':unit}for name,unit in
            (('coal',5277),('brick-coal',5278),('steel-coal',6100),('stone',5457))}
        (self.root/'material-sources.json').write_text(json.dumps(dict(self.board,sources=self.legacy_sources)))
        def bank(*args):return {'ok':True,'ready':False,'plan':mining_pair(30.5,20.5,1,'coal'),
            'reserved_area':[[29,19],[33,26]],'reserved_pair_slots':1,'nominal_rate_per_minute':60,'wire_hops':0}
        self.prepare_bank=patch('factorio_agent.material_supply.prepare_new_bank',side_effect=bank).start()
        self.preflight=patch('factorio_agent.material_supply.preflight',return_value={'ok':True,'clear_obstacles':[]}).start()
        self.power=patch('factorio_agent.material_supply.power_connection',return_value={'ok':True,'entities':[],'wire_hops':0}).start()
        self.addCleanup(patch.stopall)
        self.c=DedicatedMaterials(self.game,self.builder,self.catalog,self.outposts,perimeter=self.defense,purpose='chemistry')
        self.obs={'world_id':'w','tick':100,'technologies':{'electric-mining-drill':True},'entities':[]}

    def install(self):
        self.c.step(self.obs,{'resource':'coal'})
        es=self.c.state['supplies']['coal']['bank']['plan']['entities']
        self.obs['entities']=[dict(e,unit_number=100+i,belt_inventory={'coal':1})for i,e in enumerate(es)]
        self.builder.ensure_plan.return_value={'status':'succeeded'}

    def test_new_purpose_is_readonly_independent_identity_and_legacy_default_is_unchanged(self):
        legacy=DedicatedMaterials(self.game,self.builder,self.catalog,self.outposts)
        prepared=legacy.prepare(self.obs,{'resource':'coal'})
        legacy_state={'world_id':'w','catalog_fingerprint':'c','supplies':{'coal':prepared}}
        legacy.path.write_text(json.dumps(legacy_state));before=legacy.path.read_bytes()
        with patch('factorio_agent.material_supply._atomic_json')as write:
            result=self.c.prepare(self.obs,{'resource':'coal'});write.assert_not_called()
        self.assertTrue(result['ok'],result);self.assertEqual(legacy.path.name,'material-supply.json')
        self.assertEqual(self.c.path.name,'material-supply-chemistry.json');self.assertFalse(self.c.path.exists())
        self.assertEqual(prepared['material_supply_id'],'dedicated-material:w:coal')
        self.assertEqual(result['material_supply_id'],'dedicated-material:w:coal:chemistry')
        self.assertEqual(legacy.path.read_bytes(),before)
        self.assertEqual(set(result['bank']['material_distribution']['ports']),{'plastic-coal','chemical-coal-reserve'})
        self.assertEqual(self.prepare_bank.call_args.args[4],2);self.defense.prepare_coverage.assert_called_once()

    def test_copying_default_checkpoint_or_wrong_purpose_identity_fails_closed(self):
        self.c.state={'world_id':'w','catalog_fingerprint':'c','supplies':{}}
        self.assertEqual(self.c.prepare(self.obs,{'resource':'coal'})['reason'],'material_supply_purpose_or_identity_changed')
        self.assertEqual(self.c.step(self.obs,{'resource':'coal'})['reason'],'material_supply_purpose_or_identity_changed')
        self.assertIsNone(self.c.proof(self.obs,{'resource':'coal'}));self.builder.ensure_plan.assert_not_called()
        self.c.state={};self.assertEqual(self.c.prepare(self.obs,{'resource':'stone'})['reason'],'chemistry_material_supply_requires_coal')
        with self.assertRaises(ValueError):DedicatedMaterials(self.game,self.builder,self.catalog,self.outposts,purpose='steel')

    def test_publication_is_only_plastic_coal_and_preserves_every_existing_registry_entry(self):
        self.install();result=self.c.step(self.obs,{'resource':'coal'});self.assertEqual(result['status'],'succeeded',result)
        saved=json.loads(self.c.sources_path.read_text())['sources']
        self.assertEqual(set(saved),set(self.legacy_sources)|{'plastic-coal'})
        self.assertEqual({k:saved[k]for k in self.legacy_sources},self.legacy_sources)
        self.assertEqual(saved['plastic-coal']['nominal_shared_rate_per_minute'],60)
        self.assertEqual(saved['plastic-coal']['material_supply_id'],'dedicated-material:w:coal:chemistry')
        self.assertNotIn('chemical-coal-reserve',saved)
        self.assertEqual(self.outposts.state['banks'][0]['material_supply_purpose'],'chemistry')

    def test_empty_or_contaminated_actual_output_does_not_publish_or_forge_items(self):
        self.install();before=self.c.sources_path.read_bytes()
        for reason in('chemistry_coal_output_contaminated','chemistry_coal_output_identity_changed','empty'):
            self.game.query.return_value={'ok':False,'reason':reason}
            self.assertEqual(self.c.step(self.obs,{'resource':'coal'})['status'],'waiting')
            self.assertEqual(self.c.sources_path.read_bytes(),before)
        script=self.game.query.call_args.args[0]
        self.assertIn('e.unit_number~=q.unit_number',script);self.assertIn('row.quality~="normal"',script)
        for forbidden in('create_entity','insert(','remove(','set_recipe'):
            self.assertNotIn(forbidden,script)

    def test_defense_prepare_and_deployment_precede_mining_and_poles_are_exactly_bound(self):
        self.defense.prepare_coverage.return_value={'ok':False,'reason':'no_defended_site'}
        self.assertEqual(self.c.prepare(self.obs,{'resource':'coal'})['reason'],'chemistry_coal_defense_preflight_failed')
        self.assertFalse(self.c.path.exists());self.builder.ensure_plan.assert_not_called()
        self.defense.prepare_coverage.return_value={'ok':True}
        self.defense.ensure_coverage.return_value={'type':'craft','recipe':'firearm-magazine','count':10}
        self.assertEqual(self.c.step(self.obs,{'resource':'coal'})['recipe'],'firearm-magazine')
        self.builder.ensure_plan.assert_not_called();self.defense.ensure_coverage.return_value=None
        self.install();self.c.step(self.obs,{'resource':'coal'})
        pole=next(e for e in self.obs['entities']if e['name']=='small-electric-pole');pole['unit_number']+=900
        self.assertEqual(self.c.step(self.obs,{'resource':'coal'})['reason'],'dedicated_material_entity_identity_changed')
        self.c.perimeter=None
        self.assertEqual(self.c.step(self.obs,{'resource':'coal'})['reason'],'chemistry_material_supply_requires_perimeter')
        self.assertIsNone(self.c.proof(self.obs,{'resource':'coal'}))

    def test_existing_legacy_bank_is_reserved_and_never_relabelled_for_chemistry(self):
        old={'material_supply_id':'dedicated-material:w:coal','plan':mining_pair(-40.5,-40.5,1,'coal'),
            'reserved_area':[[-42,-42],[-38,-35]],'nominal_rate_per_minute':60,'ready':True}
        self.outposts.state['banks']=[deepcopy(old)]
        result=self.c.prepare(self.obs,{'resource':'coal'})
        self.assertTrue(result['ok'],result);self.assertNotEqual(result['bank']['plan']['ports'],old['plan']['ports'])
        self.assertEqual(self.outposts.state['banks'],[old]);self.assertTrue(self.prepare_bank.call_args.args[5])

    def test_chemistry_planning_budget_blocks_without_persisting_or_touching_old_outputs(self):
        with patch('factorio_agent.material_supply.PlanningBudget')as budget_type:
            budget_type.return_value=SimpleNamespace(game=self.game,expired=lambda:True,
                failure=lambda **extra:dict(ok=False,reason='industrial_planning_budget_exhausted',**extra))
            result=self.c.prepare(self.obs,{'resource':'coal'})
        self.assertEqual(result['reason'],'industrial_planning_budget_exhausted')
        self.assertEqual(result['attempted_banks'],0);self.prepare_bank.assert_not_called();self.assertFalse(self.c.path.exists())


if __name__=='__main__':unittest.main()
