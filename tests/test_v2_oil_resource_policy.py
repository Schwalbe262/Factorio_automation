from copy import deepcopy
import unittest

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.board import entity
from factorio_agent.resource_policy import (
    mark_pumpjack,
    validate_annotations,
    RESOURCE_CHECK_LUA,
    RESOURCES,
    MINING_NAMES,
)

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


class OilResourceScopeTests(unittest.TestCase):
    def test_only_exact_pumpjack_gets_crude_exemption_and_solids_are_unchanged(self):
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        pump = entity("pumpjack", 0.5, 0.5)
        mark_pumpjack(pump)
        validate_annotations([pump], geometry)
        for name in (
            "pipe",
            "small-electric-pole",
            "transport-belt",
            "burner-inserter",
            "oil-refinery",
            "electric-mining-drill",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_annotations([dict(pump, name=name)], geometry)
        wrong = deepcopy(pump)
        wrong["mining_footprint"][0][0] -= 1
        with self.assertRaises(ValueError):
            validate_annotations([wrong], geometry)
        self.assertNotIn("crude-oil", RESOURCES)
        self.assertNotIn("pumpjack", MINING_NAMES)
        self.assertNotIn("--", RESOURCE_CHECK_LUA)
        for resource in RESOURCES:
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                validate_annotations([dict(pump, mining_resource=resource)], geometry)


@unittest.skipUnless(LuaRuntime, "optional Lua runtime: pip install lupa")
class NativeOilResourceTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute("""f={};s={};ores={};function target(p,name)
for _,e in ipairs(ores)do if e.name==name and e.position.x==p.x and e.position.y==p.y then return e end end end
function s.find_entities_filtered(args)local found={};for _,e in ipairs(ores)do local p=e.position
 if p.x>=args.area[1][1]and p.x<=args.area[2][1]and p.y>=args.area[1][2]and p.y<=args.area[2][2]then found[#found+1]=e end end;return found end
function add(name,x,y)ores[#ores+1]={name=name,position={x=x,y=y},amount=1000}end
add("crude-oil",.5,.5)""")
        self.check = self.lua.execute(
            RESOURCE_CHECK_LUA + "return function(spec,zones)return resource_violation(spec,zones,{}, {spec})end"
        )
        self.pump = entity("pumpjack", 0.5, 0.5)
        mark_pumpjack(self.pump)
        self.pump["survey_area"] = [[-0.99, -0.99], [1.99, 1.99]]

    def table(self, v):
        if isinstance(v, dict):
            return self.lua.table_from({k: self.table(a) for k, a in v.items()})
        if isinstance(v, list):
            return self.lua.table_from([self.table(a) for a in v])
        return v

    def reason(self, spec=None, zones=None):
        result = self.check(self.table(spec or self.pump), self.table(zones or []))
        return result[0] if isinstance(result, tuple) else result

    def test_native_exact_live_crude_well_is_accepted(self):
        self.assertIsNone(self.reason())

    def test_offset_or_missing_well_is_rejected_even_without_other_resources(self):
        moved = deepcopy(self.pump)
        moved["position"]["x"] = 3.5
        self.assertEqual(self.reason(moved), "pumpjack_requires_exact_live_crude_well")
        self.lua.execute("ores={}")
        self.assertEqual(self.reason(), "pumpjack_requires_exact_live_crude_well")

    def test_foreign_solid_resource_and_reserved_mining_bank_are_rejected(self):
        self.lua.globals().add("iron-ore", 1.5, 0.5)
        self.assertEqual(self.reason(), "pumpjack_cannot_overlap_solid_resource")
        self.lua.execute("table.remove(ores,2)")
        self.assertEqual(
            self.reason(zones=[{"area": [[-1, -1], [2, 2]], "resource": "coal"}]),
            "pumpjack_cannot_overlap_solid_mining_reservation",
        )

    def test_pipe_pole_or_factory_still_cannot_build_on_crude(self):
        for name in ("pipe", "small-electric-pole", "oil-refinery"):
            spec = entity(name, 0.5, 0.5)
            spec["survey_area"] = [[-0.49, -0.49], [1.49, 1.49]]
            self.assertEqual(self.reason(spec), "new_entity_over_resource_or_mining_reservation")


if __name__ == "__main__":
    unittest.main()
