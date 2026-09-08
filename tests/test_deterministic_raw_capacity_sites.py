from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from factorio_ai import deterministic_game
from factorio_ai.deterministic_game import DeterministicGame, run_config
from tests import test_deterministic_factory as fixtures


class RawCapacitySiteTests(unittest.TestCase):
    def setUp(self):
        fixtures.FactoryTests.setUp(self)
        self.key = "source:iron-plate:capacity:8"
        self.near, self.far = {"x": 8, "y": 8}, {"x": 200, "y": 100}
        self.factory.state["blocks"]["source:iron-plate"] = {
            "ok": True, "entities": [], "ports": [fixtures.port("iron-plate")]}
        self.game.query.return_value = {"ok": True, "sites": [self.near, self.far]}

    def choose(self):
        return self.factory._raw_capacity_site(self.obs, "iron-plate", self.key)

    def test_reserved_site_and_paid_routes_are_not_migrated_or_resurveyed(self):
        saved = self.factory._electric_source_plan("iron-plate", 240, -49)
        saved["key"] = self.key
        self.factory.state["blocks"][self.key] = saved
        self.factory.state["links"][self.key + ":output"] = {"entities": [{
            "name": "transport-belt", "position": {"x": 236.5, "y": -50.5}, "direction": 12}]}
        self.factory._save()
        before = deepcopy(self.factory.state), self.factory.path.read_bytes()
        self.assertIs(self.choose(), saved)
        self.game.query.assert_not_called()
        self.builder.can_place.assert_not_called()
        self.assertEqual((self.factory.state, self.factory.path.read_bytes()), before)

    def test_nearby_reserved_footprint_falls_back_without_releasing_owner(self):
        owner = {"entities": [{"name": "wooden-chest", "position": {"x": 8.5, "y": 8.5}}]}
        self.factory.state["blocks"]["other-owner"] = deepcopy(owner)
        chosen = self.choose()
        self.assertEqual(chosen["entities"][0]["position"], {"x": 200.5, "y": 100.5})
        self.assertEqual(self.factory.state["blocks"]["other-owner"], owner)
        self.builder.can_place.assert_called_once()

    def test_nearby_reserved_input_approach_falls_back(self):
        self.factory.state["blocks"]["other-owner"] = {"entities": [{
            "name": "transport-belt", "position": {"x": 12.5, "y": 5.5}, "direction": 4}]}
        self.assertEqual(self.choose()["entities"][0]["position"], {"x": 200.5, "y": 100.5})
        self.builder.can_place.assert_called_once()

    def test_live_collision_at_nearby_site_falls_back_with_all_normal_cell_hardware(self):
        self.builder.can_place.side_effect = [{"ok": False}, {"ok": True}]
        plan = self.choose()
        self.assertEqual(plan, self.factory.state["blocks"][self.key])
        self.assertEqual(len(plan["entities"]), 10)
        self.assertTrue(plan["resource_cell"])
        self.assertEqual(self.builder.can_place.call_count, 2)
        self.builder.ensure_plan.assert_not_called()
        self.factory.ensure_power_connection.assert_not_called()

    def test_exhausted_eligible_list_does_not_claim_or_reserve_capacity(self):
        self.game.query.return_value = {"ok": True, "sites": []}
        before = deepcopy(self.factory.state)
        self.assertFalse(self.choose()["ok"])
        self.assertEqual(self.factory.state, before)
        self.builder.can_place.assert_not_called()


SHADOW = '''
local options=helpers.json_to_table(OPTIONS);local f={};local s={};local cases={};local ores={}
for _,row in ipairs(options) do
 cases[row.x..","..row.y]=row
 ores[#ores+1]={position={x=row.x+.5,y=row.y+.5}}
end
function s.can_place_entity(args)
 local row=cases[math.floor(args.position.x)..","..math.floor(args.position.y)]
 return row and not row.blocked
end
function s.find_entities_filtered(args)
 if args.radius then return ores end
 local row=cases[(args.area[1][1]+2)..","..(args.area[1][2]+2)];local out={}
 for i=1,row.count do out[#out+1]={name=row.mixed and i==1 and "copper-ore" or "iron-ore",amount=row.amount/row.count} end
 return out
end
'''


@unittest.skipUnless(os.environ.get("FACTORIO_RAW_SITE_RANKING_LIVE_TEST") == "1", "requires opt-in inert RCON test")
class RawCapacityRankingLuaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime = Path(os.environ["FACTORIO_RAW_SITE_RANKING_RUNTIME"])
        cls.live = DeterministicGame(run_config(runtime=runtime, server_port=34210, rcon_port=27025))

    def ranked(self, sites, reference=None):
        case = RawCapacitySiteTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        if reference is not None:
            case.factory.state["blocks"]["source:iron-plate"]["ports"][0]["position"] = reference
        case.game.query.return_value = {"ok": True, "sites": []}
        case.choose()
        body = case.game.query.call_args.args[0]
        with patch.object(deterministic_game, "_HELPERS", ""):
            result = self.live.query(SHADOW.replace("OPTIONS", json.dumps(json.dumps(sites))) + body)
        self.assertTrue(result.get("ok"), result)
        return result.get("sites") or []

    def test_more_than_256_rich_distant_sites_cannot_exclude_a_viable_near_site(self):
        distant = [{"x": 200+i%20, "y": 100+i//20, "count": 25, "amount": 100000} for i in range(260)]
        near = {"x": 4, "y": 4, "count": 12, "amount": 2761}
        ranked = self.ranked([*distant, near])
        self.assertEqual(len(ranked), 256)
        self.assertEqual((ranked[0]["x"], ranked[0]["y"]), (4, 4))

    def test_connection_reference_precedes_origin_and_richness_breaks_distance_ties(self):
        sites = [{"x": 0, "y": 0, "count": 25, "amount": 100000},
                 {"x": 99, "y": 100, "count": 12, "amount": 1000},
                 {"x": 101, "y": 100, "count": 4, "amount": 2000}]
        ranked = self.ranked(sites, {"x": 100.5, "y": 100.5})
        self.assertEqual([(r["x"], r["y"]) for r in ranked], [(101, 100), (99, 100), (0, 0)])

    def test_depletion_mixed_ore_insufficient_tiles_and_drill_collision_remain_ineligible(self):
        sites = [{"x": 1, "y": 1, "count": 0, "amount": 0},
                 {"x": 2, "y": 2, "count": 3, "amount": 1000},
                 {"x": 3, "y": 3, "count": 25, "amount": 100000, "mixed": True},
                 {"x": 4, "y": 4, "count": 25, "amount": 100000, "blocked": True},
                 {"x": 10, "y": 10, "count": 4, "amount": 2761}]
        ranked = self.ranked(sites)
        self.assertEqual([(r["x"], r["y"]) for r in ranked], [(10, 10)])

    def test_equal_distance_richness_and_tile_count_have_deterministic_coordinate_ties(self):
        sites = [{"x": x, "y": y, "count": 4, "amount": 1000} for x, y in ((1, 0), (0, 1), (0, -1), (-1, 0))]
        ranked = self.ranked(sites)
        self.assertEqual([(r["x"], r["y"]) for r in ranked], [(-1, 0), (0, -1), (0, 1), (1, 0)])


if __name__ == "__main__":
    unittest.main()
