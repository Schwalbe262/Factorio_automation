import unittest
from factorio_ai.deterministic_builder import FactoryBuilder
from factorio_agent.steam_expansion import steam_pair,SteamExpansion


class SteamExpansionTests(unittest.TestCase):
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
