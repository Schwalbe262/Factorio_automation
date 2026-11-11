import json
from types import SimpleNamespace
import unittest

from factorio_agent.material_supply import DedicatedMaterials
try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime=None


@unittest.skipUnless(LuaRuntime,'optional Lua runtime: pip install lupa')
class NativeMaterialOutputAccessTests(unittest.TestCase):
    def setUp(self):
        from tests.test_v2_mining_exit_native import NativeMiningExitTests
        fixture=NativeMiningExitTests();fixture.setUp();self.fixture=fixture;self.lua=fixture.lua
        fixture.ore(.5,.5)
        self.lua.execute('''function s.count_entities_filtered(args)return #s.find_entities_filtered(args)end
prototypes.entity["transport-belt"].tile_width=1;prototypes.entity["transport-belt"].tile_height=1
prototypes.entity["small-electric-pole"]={tile_width=1,tile_height=1}''')
        self.lua.globals().helpers=self.lua.table_from({'json_to_table':lambda raw:fixture.table(json.loads(raw))})
        def decode(v):
            if not hasattr(v,'items'):return v
            keys=list(v.keys())
            if keys and all(type(k)is int for k in keys)and sorted(keys)==list(range(1,len(keys)+1)):
                return [decode(v[i])for i in range(1,len(keys)+1)]
            return {k:decode(a)for k,a in v.items()}
        self.c=DedicatedMaterials.__new__(DedicatedMaterials)
        self.c.game=SimpleNamespace(query=lambda body:decode(self.lua.execute('return(function() '+' '.join(body.splitlines())+' end)()')))
        self.source=fixture.source

    def survey(self,planned=()):return self.c._coal_hub(self.source,[],planned)

    def test_planned_power_in_first_output_apron_rejects_the_unusable_hub(self):
        baseline=self.survey();self.assertTrue(baseline['ok'],baseline)
        anchor=baseline['hub_position']
        pole={'name':'small-electric-pole','position':{'x':anchor['x']+3,'y':anchor['y']},'direction':0}
        selected=self.survey([pole])
        self.assertEqual(selected['reason'],'no_safe_bounded_mining_exit')

    def test_planned_power_cannot_be_ignored_at_the_initial_exit(self):
        pole={'name':'small-electric-pole','position':{'x':1.5,'y':.5},'direction':0}
        self.assertEqual(self.survey([pole])['reason'],'no_safe_bounded_mining_exit')

    def test_resource_beyond_first_empty_tile_cannot_leave_output_boxed_in(self):
        baseline=self.survey();p=baseline['hub_position']
        self.fixture.ore(p['x']+4,p['y'])
        self.assertEqual(self.survey()['reason'],'no_safe_bounded_mining_exit')


if __name__=='__main__':unittest.main()
