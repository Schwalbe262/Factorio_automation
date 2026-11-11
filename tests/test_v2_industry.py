from types import SimpleNamespace
import unittest

from factorio_agent.industry import BootstrapLogistics


class MachineFundingTests(unittest.TestCase):
    def test_finite_science_budget_comes_from_recipe_yields_not_handcrafting(self):
        recipes={
            'automation-science-pack':{'products':[{'name':'automation-science-pack','amount':1}],
                 'ingredients':[{'name':'copper-plate','amount':1},{'name':'iron-gear-wheel','amount':1}]},
            'iron-gear-wheel':{'products':[{'name':'iron-gear-wheel','amount':1}],
                 'ingredients':[{'name':'iron-plate','amount':2}]}}
        routine=BootstrapLogistics.__new__(BootstrapLogistics)
        routine.catalog=SimpleNamespace(technologies={'logistics':{'unit_count':20,
             'ingredients':[{'name':'automation-science-pack','type':'item','amount':1}]}},
             recipe_for_product=lambda name:recipes[name])
        self.assertEqual(routine.requirements(),(20,{'iron-plate':40,'copper-plate':20}))
        recipes['iron-gear-wheel']['products'][0]['amount']=2
        self.assertEqual(routine.requirements(),(20,{'iron-plate':20,'copper-plate':20}))


if __name__=='__main__':unittest.main()
