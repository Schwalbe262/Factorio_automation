"""Execute the shared native selector with an optional local Lua test runtime."""
import unittest

from factorio_agent.resource_policy import MINING_EXIT_LUA, mining_exit_candidates

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


@unittest.skipUnless(LuaRuntime, 'optional Lua runtime: pip install lupa')
class NativeMiningExitTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(r'''
f={name="player"};a=nil;entities={};terrain={}
defines={build_check_type={manual_ghost=1}}
prototypes={entity={["transport-belt"]={collision_mask={layers={ground=true}}}}}
local function overlaps(e,area)
 local size=e.type=="mining-drill" and 3 or 1
 return e.position.x+size/2>area[1][1] and e.position.x-size/2<area[2][1]
  and e.position.y+size/2>area[1][2] and e.position.y-size/2<area[2][2]
end
function target(p,name)
 for _,e in ipairs(entities) do if e.name==name and e.position.x==p.x and e.position.y==p.y then return e end end
end
s={}
function s.find_entities_filtered(args)
 local found={}
 for _,e in ipairs(entities) do if (not args.type or e.type==args.type) and (not args.force or e.force==args.force)
  and overlaps(e,args.area) then found[#found+1]=e end end
 return found
end
function s.can_place_entity(args)
 if terrain[args.position.x..":"..args.position.y] then return false end
 for _,e in ipairs(s.find_entities_filtered{area={{args.position.x-.4,args.position.y-.4},{args.position.x+.4,args.position.y+.4}}}) do
  if e.type~="resource" then return false end
 end
 return true
end
function s.get_tile(p)return {collides_with=function()return terrain[p.x..":"..p.y] or false end}end
function add(name,x,y,kind,unit,direction,foreign)
 local e={name=name,position={x=x,y=y},type=kind,unit_number=unit,direction=direction or 4,
  force=foreign and {name="other"} or f,prototype={collision_mask={colliding_with_tiles_only=false}}}
 e.get_transport_line=function()return {get_contents=function()return {} end}end
 entities[#entities+1]=e;return e
end
add("transport-belt",.5,.5,"transport-belt",1)
add("electric-mining-drill",-1.5,-1.5,"mining-drill",2)
''')
        self.choose = self.lua.execute(MINING_EXIT_LUA + '''
return function(args)
 local accept=nil
 if args.apron then accept=function(p)
  return #s.find_entities_filtered{area={{p.x+.5,p.y-.5},{p.x+3.5,p.y+1.5}},type="resource"}==0
 end end
 return select_mining_exit(args.source,args.zones,args.ignored,args.planned,args.candidates,accept,false,args.extension)
end
''')
        self.source = {'position': {'x': .5, 'y': .5}, 'facing': 4, 'item': 'coal', 'mining_resource': 'coal'}

    def table(self, value):
        if isinstance(value, dict): return self.lua.table_from({k: self.table(v) for k,v in value.items()})
        if isinstance(value, list): return self.lua.table_from([self.table(v) for v in value])
        return value

    def ore(self, x, y, name='coal'):
        self.lua.globals().add(name, x, y, 'resource', None, 0, False)

    def block(self, x, y, name='transport-belt', unit=80):
        self.lua.globals().add(name, x, y, name, unit, 4, False)

    def run_selector(self, *, zones=None, ignored=None, planned=None, apron=False, extension=0):
        return self.choose(self.table({'source': self.source, 'zones': zones or [], 'ignored': ignored or {},
            'planned': planned or [], 'apron': apron, 'extension': extension,
            'candidates': mining_exit_candidates(self.source['position'], extra_clear_steps=extension)}))

    def south_patch(self):
        for i in range(13): self.ore(i+.5, .5)
        for i in range(1, 5): self.ore(1.5, i+.5)
        self.block(1.5, -.5)

    def test_clear_east_is_preferred_over_shorter_south(self):
        self.ore(.5, .5); self.ore(1.5, .5); self.ore(2.5, .5)
        result = self.run_selector()
        self.assertTrue(result['ok'])
        self.assertEqual((result['clear_position']['x'], result['clear_position']['y']), (3.5, .5))
        self.assertEqual(result['exit_length'], 2)

    def test_safe_south_l_ends_at_first_resource_and_reservation_free_cell(self):
        self.south_patch()
        result = self.run_selector()
        self.assertTrue(result['ok'])
        self.assertEqual((result['clear_position']['x'], result['clear_position']['y']), (1.5, 5.5))
        self.assertEqual(result['exit_length'], 5)
        self.assertEqual(len(result['exit_prefix']), 7)

    def test_hub_advances_off_patch_when_first_clear_anchor_has_ore_on_east_apron(self):
        self.south_patch(); self.ore(4.5, 5.5)
        result = self.run_selector(apron=True, extension=4)
        self.assertTrue(result['ok'])
        self.assertEqual(result['clear_position']['y'], 5.5)
        self.assertEqual(result['exit_length'], 5)
        self.assertEqual(len(result['exit_prefix']), 7)
        self.assertEqual(result['hub_position']['y'], 6.5)
        self.assertEqual(len(result['hub_connector']), 1)
        self.assertEqual(result['hub_connector'][1]['y'], 6.5)

    def test_hub_connector_never_reenters_ore_or_exceeds_four_additional_tiles(self):
        self.south_patch(); self.ore(4.5, 5.5); self.ore(1.5, 6.5)
        self.assertFalse(self.run_selector(apron=True, extension=4)['ok'])
        self.setUp(); self.south_patch()
        for i in range(6): self.ore(4.5, 5.5+i)
        self.assertFalse(self.run_selector(apron=True, extension=4)['ok'])

    def test_hub_connector_native_collision_or_reservation_is_rejected(self):
        self.south_patch(); self.ore(4.5, 5.5); self.block(1.5, 6.5)
        self.assertFalse(self.run_selector(apron=True, extension=4)['ok'])
        self.setUp(); self.south_patch(); self.ore(4.5, 5.5)
        zones = [{'area': [[1, 6], [2, 7]], 'resource': 'coal'}]
        self.assertFalse(self.run_selector(apron=True, extension=4, zones=zones)['ok'])

    def test_foreign_belt_drill_or_terrain_blocks_l_even_before_first_clear_cell(self):
        for kind in ('transport-belt', 'underground-belt', 'electric-mining-drill', 'terrain'):
            with self.subTest(kind=kind):
                self.setUp(); self.south_patch()
                if kind == 'terrain': self.lua.execute('terrain["1.5:2.5"]=true')
                else: self.block(1.5, 2.5, kind)
                self.assertFalse(self.run_selector()['ok'])

    def test_recovery_ignores_only_exact_owned_target_units(self):
        self.south_patch(); self.block(1.5, 2.5, unit=99)
        self.assertFalse(self.run_selector()['ok'])
        self.assertTrue(self.run_selector(ignored={99: True})['ok'])
        self.lua.execute('entities[#entities].force={name="other"}')
        self.assertFalse(self.run_selector(ignored={99: True})['ok'])

    def test_foreign_resource_or_reservation_on_l_is_rejected(self):
        self.south_patch(); self.ore(1.5, 2.5, 'iron-ore')
        self.assertFalse(self.run_selector()['ok'])
        self.setUp(); self.south_patch()
        self.assertFalse(self.run_selector(zones=[{'area': [[1, 2], [2, 3]], 'resource': 'stone'}])['ok'])

    def test_reservation_continues_prefix_past_first_ore_hole(self):
        self.south_patch()
        result = self.run_selector(zones=[{'area': [[1, 5], [2, 7]], 'resource': 'coal'}])
        self.assertTrue(result['ok'])
        self.assertEqual(result['clear_position']['y'], 7.5)

    def test_over_twelve_steps_and_missing_source_drill_fail_closed(self):
        for path in mining_exit_candidates(self.source['position']):
            for p in path: self.ore(p['x'], p['y'])
        self.assertFalse(self.run_selector()['ok'])
        self.setUp(); self.ore(.5, .5)
        self.lua.execute('table.remove(entities,2)')
        self.assertEqual(self.run_selector()['reason'], 'mining_exit_without_nearby_source_drill')

    def test_owned_or_planned_mining_source_is_required(self):
        self.ore(.5, .5); self.lua.execute('table.remove(entities,1)')
        self.assertEqual(self.run_selector()['reason'], 'mining_exit_requires_owned_source')
        self.assertTrue(self.run_selector(planned=[{'name': 'transport-belt', 'position': self.source['position'],
            'direction': 4, 'mining_resource': 'coal'}])['ok'])


if __name__ == '__main__':
    unittest.main()
