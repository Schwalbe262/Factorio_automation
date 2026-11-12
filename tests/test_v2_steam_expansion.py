from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from factorio_ai.deterministic_builder import FactoryBuilder,_plan
from factorio_agent.board import entity
from factorio_agent.smelting import port
from factorio_agent.steam_expansion import steam_pair,SteamExpansion


class SteamExpansionTests(unittest.TestCase):
    def fixture(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
        obj=SteamExpansion.__new__(SteamExpansion);obj.path=root/'steam-expansion.json'
        obj.catalog=SimpleNamespace(fingerprint='c',entities={'steam-engine':{'energy_production':15000}})
        obj.game=Mock();obj.builder=SimpleNamespace(ensure_plan=Mock(return_value={'type':'build','name':'boiler'}))
        source=port('coal',-7.5,50.5,4)
        bank={'ok':True,'plan':_plan([entity('transport-belt',-7.5,50.5,4)],[source],
                mining_reserved_area=[[-25,47],[-13,54]],layout_contract={'fixed_output_port':source}),
              'layout_contract':{'fixed_output_port':source},'reserved_area':[[-25,47],[-4,54]],
              'mining_reserved_area':[[-25,47],[-13,54]],'nominal_rate_per_minute':66}
        obj.outposts=SimpleNamespace(path=root/'mining-outposts.json',
            state={'world_id':'w','catalog_fingerprint':'c','banks':[]},publish_reservations=Mock(return_value=None))
        obj.state={'world_id':'w','catalog_fingerprint':'c','bank':deepcopy(bank),'core':steam_pair(),
                   'plan':_plan(bank['plan']['entities'],[]),'samples':[]}
        (root/'pcb-board.json').write_text(json.dumps({'world_id':'w','blocks':{'steam_expansion':steam_pair()}}))
        obs={'world_id':'w','tick':1,'entities':[{'name':'steam-engine','unit_number':10,'energy':100,
                                              'position':{'x':0,'y':0}}]}
        return obj,bank,obs

    def test_prepare_passes_the_ordinary_fixed_source_without_mining_annotation(self):
        obj,bank,obs=self.fixture();obj.state={}
        with patch('factorio_agent.steam_expansion.preflight',return_value={'ok':True,'clear_obstacles':[]}),\
             patch('factorio_agent.steam_expansion.prepare_new_bank',return_value=bank),\
             patch('factorio_agent.steam_expansion.plan_connection',return_value={'ok':True,'entities':[],
                         'underground_pairs':[]})as route:
            result=obj.prepare(obs)
        self.assertTrue(result['ok'],result)
        self.assertEqual(route.call_args.args[3],bank['plan']['ports'][0])
        self.assertNotIn('mining_resource',route.call_args.args[3])
        self.assertEqual(result['bank']['plan']['mining_reserved_area'],bank['mining_reserved_area'])
        self.assertEqual(result['bank']['layout_contract'],bank['layout_contract'])
        self.assertFalse(obj.outposts.path.exists())

    def test_contracted_bank_is_reserved_before_first_construction_and_resume_is_idempotent(self):
        obj,bank,obs=self.fixture()
        def build(*_):
            saved=json.loads(obj.outposts.path.read_text())
            self.assertEqual(len(saved['banks']),1)
            self.assertFalse(saved['banks'][0]['ready'])
            self.assertEqual(saved['banks'][0]['plan']['ports'],bank['plan']['ports'])
            obj.outposts.publish_reservations.assert_called_with(obs)
            return {'type':'build','name':'boiler'}
        obj.builder.ensure_plan.side_effect=build
        self.assertEqual(obj.step(obs)['type'],'build')
        self.assertEqual(obj.step(obs)['type'],'build')
        self.assertEqual(len(obj.outposts.state['banks']),1)

    def test_same_fixed_port_with_changed_canonical_plan_blocks_before_build(self):
        obj,bank,obs=self.fixture();changed=deepcopy(bank)
        changed['plan']['entities'][0]['direction']=8
        obj.outposts.state['banks']=[changed]
        result=obj.step(obs)
        self.assertEqual(result['reason'],'steam_fixed_coal_bank_plan_changed')
        obj.builder.ensure_plan.assert_not_called()

    def test_new_pair_preserves_a_tunnel_under_existing_lab(self):
        plan=steam_pair();geometry=FactoryBuilder.__new__(FactoryBuilder)
        boxes=[geometry._occupied_by_plan([e]) for e in plan['entities']]
        self.assertEqual(sum(map(len,boxes)),len(set().union(*boxes)))
        pipe=[e for e in plan['entities'] if e['name']=='pipe-to-ground']
        self.assertEqual([(e['position']['x'],e['direction']) for e in pipe],[(40.5,12),(44.5,4)])
        self.assertFalse(any(e['position']=={'x':42.5,'y':1.5} for e in plan['entities']))

    def test_history_cannot_replace_a_fresh_native_generation_check(self):
        obj=SteamExpansion.__new__(SteamExpansion)
        obj.state={'world_id':'w','samples':[{'tick':100,'engines':[{'unit_number':2}]},{'tick':1900,'engines':[{'unit_number':2}]}],
                   'preserved_engine_units':[1],'added_nominal_kw':1800,'bank':{'nominal_rate_per_minute':60}}
        obs={'world_id':'w','tick':1901,'entities':[{'name':'steam-engine','unit_number':1}]}
        obj.sample=lambda _:None
        self.assertIsNone(obj.proof(obs))
        obj.sample=lambda _:{'ok':True,'engines':[{'unit_number':3}]}
        self.assertIsNone(obj.proof(obs))
        obj.sample=lambda _:{'ok':True,'tick':1902,'engines':[{'unit_number':2}]}
        self.assertIsNotNone(obj.proof(obs))
        obs['tick']=1899
        self.assertIsNotNone(obj.proof(obs))


if __name__=='__main__':unittest.main()
