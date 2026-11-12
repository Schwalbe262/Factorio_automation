from types import SimpleNamespace
import unittest

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.board import startup_science_cell,pole_chain


class BoardGeometryTests(unittest.TestCase):
    def test_cell_has_no_overlaps_and_uses_unlocked_starter_equipment(self):
        plan=startup_science_cell(32.5,.5)
        geometry=FactoryBuilder.__new__(FactoryBuilder)
        boxes=[geometry._occupied_by_plan([e]) for e in plan['entities']]
        self.assertEqual(sum(map(len,boxes)),len(set().union(*boxes)))
        self.assertEqual(plan['required_items'],{'assembling-machine-1':2,'wooden-chest':3,'inserter':4,'small-electric-pole':2})
        self.assertEqual({p['item'] for p in plan['ports']},{'iron-plate','copper-plate','automation-science-pack'})

    def test_wire_hops_are_bounded_and_orthogonal(self):
        source={'x':44.5,'y':1.5};destination={'x':34.5,'y':1.5}
        chain=pole_chain(source,destination)
        self.assertEqual([e['position'] for e in chain],[{'x':39.5,'y':1.5},destination])


if __name__=='__main__':unittest.main()
