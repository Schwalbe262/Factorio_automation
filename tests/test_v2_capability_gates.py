from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.capability_gates import native_recipe_gates,reject_native_recipe_cycles
from factorio_agent.supervisor import WorkQueue


CAPS={'logistic_science':{'parameters':{},'requires':[],
    'native_recipes':['logistic-science-pack','fast-inserter']},
    'research':{'parameters':{'technology':'name'},'requires':[]},
    'starter':{'parameters':{},'requires':[]}}

def task(name,cap='starter',deps=(),technology=None):
    return dict(id=name,capability=cap,parameters={}if technology is None else{'technology':technology},
        depends_on=list(deps),priority=1,reason='ordinary native progression')

class NativeRecipeGateTests(unittest.TestCase):
    def setUp(self):
        self.catalog=SimpleNamespace(technologies={
            'logistic-science-pack':{'unlocks':['logistic-science-pack'],'researched':True},
            'fast-inserter':{'unlocks':['fast-inserter'],'researched':True}})
        self.obs={'enabled_recipes':{'logistic-science-pack':False,'fast-inserter':False}}

    def gates(self):return native_recipe_gates(self.catalog,self.obs,CAPS)

    def test_catalog_prototypes_and_old_research_flags_cannot_unlock_fresh_factories(self):
        gate=self.gates()['logistic_science']
        self.assertFalse(gate['native_recipes_ready'])
        self.assertEqual(gate['required_technologies'],['fast-inserter','logistic-science-pack'])
        self.assertEqual(len(gate['missing_recipes']),2)

    def test_only_current_native_recipe_unlocks_clear_the_gate(self):
        self.obs['enabled_recipes']['logistic-science-pack']=True
        self.assertEqual(self.gates()['logistic_science']['missing_recipes'],['fast-inserter'])
        self.obs['enabled_recipes']['fast-inserter']=True
        self.assertTrue(self.gates()['logistic_science']['native_recipes_ready'])

    def test_missing_unlock_metadata_does_not_assume_a_recipe_is_available(self):
        self.catalog.technologies={}
        gate=self.gates()['logistic_science']
        self.assertFalse(gate['native_recipes_ready']);self.assertEqual(len(gate['unresolved_unlock_recipes']),2)

    def test_green_factory_before_its_own_unlock_is_rejected_atomically(self):
        with TemporaryDirectory()as root:
            work=WorkQueue(Path(root)/'queue.json','world');validator=Mock()
            nodes=[task('red'),task('green','logistic_science',['red']),
                task('green_unlock','research',['green'],'logistic-science-pack')]
            before=deepcopy(work.data)
            with self.assertRaisesRegex(ValueError,'research logistic-science-pack must precede green'):
                work.merge(nodes,CAPS,validator,1,native_gates=self.gates())
            self.assertEqual(work.data,before);self.assertFalse(work.path.exists());validator.assert_not_called()

    def test_unlock_with_existing_red_then_green_is_legal(self):
        nodes=[task('red'),task('green_unlock','research',['red'],'logistic-science-pack'),
            task('green','logistic_science',['green_unlock'])]
        reject_native_recipe_cycles(nodes,self.gates(),{'green','green_unlock'})

    def test_changing_an_intermediate_dependency_cannot_hide_the_cycle(self):
        nodes=[task('green','logistic_science'),task('bridge','starter',['green']),
            task('green_unlock','research',['bridge'],'logistic-science-pack')]
        with self.assertRaisesRegex(ValueError,'native recipe dependency cycle'):
            reject_native_recipe_cycles(nodes,self.gates(),{'bridge'})
        reject_native_recipe_cycles(nodes,self.gates(),{'unrelated'})

    def test_alternative_research_unlocks_are_choices_not_a_proven_cycle(self):
        self.catalog.technologies['alternative']={'unlocks':['logistic-science-pack']}
        nodes=[task('green','logistic_science'),task('study','research',['green'],'logistic-science-pack')]
        reject_native_recipe_cycles(nodes,self.gates(),{'study'})

    def test_already_enabled_recipe_does_not_create_a_future_unlock_edge(self):
        self.obs['enabled_recipes']['logistic-science-pack']=True
        nodes=[task('green','logistic_science'),task('study','research',['green'],'logistic-science-pack')]
        reject_native_recipe_cycles(nodes,self.gates(),{'study'})
