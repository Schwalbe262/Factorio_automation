from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from factorio_agent.normal_research import NormalResearch


class EngineResearchTests(unittest.TestCase):
    def test_research_emits_no_pack_crafting_or_insertion_and_requires_actual_consumption(self):
        with TemporaryDirectory() as directory:
            obj=NormalResearch.__new__(NormalResearch);obj.path=Path(directory)/'research.json';obj.state={}
            obj.catalog=SimpleNamespace(fingerprint='f',technologies={'gun-turret':{
                'prerequisites':[],'ingredients':[{'name':'automation-science-pack','amount':1}]}})
            obj.science=SimpleNamespace(proof=lambda o:{'science_units':[1,2,3]})
            obs={'world_id':'w','tick':10,'technologies':{},'research':None,
                 'production':{'automation-science-pack':{'consumed':55}}}
            self.assertEqual(obj.step(obs,{'technology':'gun-turret'})['type'],'research')
            obs['technologies']['gun-turret']=True
            self.assertIsNone(obj.proof(obs,{'technology':'gun-turret'}))
            obs['production']['automation-science-pack']['consumed']=65
            self.assertEqual(obj.proof(obs,{'technology':'gun-turret'})['science_consumed_since_first_request'],{'automation-science-pack':10})

    def test_green_technology_requires_commissioned_machine_green_supply(self):
        with TemporaryDirectory() as directory:
            obj=NormalResearch.__new__(NormalResearch);obj.path=Path(directory)/'research.json';obj.state={}
            obj.catalog=SimpleNamespace(fingerprint='f',technologies={'advanced-material-processing':{
                'prerequisites':[],'ingredients':[{'name':'automation-science-pack','amount':1},{'name':'logistic-science-pack','amount':1}]}})
            obj.science=SimpleNamespace(proof=lambda o:{'science_units':[1]})
            obj.green=SimpleNamespace(proof=lambda o:None)
            obs={'world_id':'w','tick':10,'technologies':{},'research':None,
                 'production':{'automation-science-pack':{'consumed':10},'logistic-science-pack':{'consumed':0}}}
            request={'technology':'advanced-material-processing'}
            self.assertEqual(obj.step(obs,request)['reason'],'continuous_science_supply_not_implemented')
            obj.green=SimpleNamespace(proof=lambda o:{'science_units':[2]})
            self.assertEqual(obj.step(obs,request)['type'],'research')


if __name__=='__main__':unittest.main()
