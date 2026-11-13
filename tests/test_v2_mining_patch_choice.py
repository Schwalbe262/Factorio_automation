"""A mine is planned whenever its patch has minable ore reachable off the ore.

Fleet all-1113-2155 (commit e9b4144) lost 3 of 12 seeds to a mining bank that never found a site:

* 1541632234 copper: the plan's copper zone was a 720-tile patch 298 tiles out (the near
  333-tile patch overlapped the iron zone and was dropped from the plan), the bank surveyed the
  near patch, the far zone rect dropped every site on all four sides (0 candidates each) and the
  near patch was then tried only with the historical east flow, which faced the iron patch;
* 3442106081 iron: the observation's "nearest" iron tile is the nearest of a 2000-tile capped
  search; with 2995 iron tiles in range it lay on a far uncharted patch, the survey came back
  empty (a JSON object) and the bank blocked as ``mining_patch_native_receipt_changed`` for good;
* 4278523554 coal: a 217-tile coal patch squeezed between iron and copper had 0 candidates on
  every side: a bank whose port already stands off the ore was dropped, and no row without a
  reserved growth pair was ever tried.

The geometry is synthetic and small; nothing reads ``runtime/``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.mining_layout_policy import (
    NO_COMPLIANT_BANK,
    SURVEY_EMPTY,
    _exit_cells,
    candidate_sites,
    make_policy,
    prepare_new_bank,
    survey_patch,
)
from factorio_agent.mining_site import patch_exits, seed_in_zone
from factorio_agent.outposts import MiningOutposts
from factorio_agent.site.geometry import Rect
from factorio_agent.site.planmodel import PatchRef, ResourcePlan
from factorio_agent.site.planzones import Corridor, Port, Segment, Zone
from factorio_agent.site.worldmap import WorldMap

from test_v2_mining_site_exit import LuaRuntime, LuaSurfaceGame, catalog, map_preflight, snapshot


def copper_plan(primary_distance):
    """Seed 1541632234's shape: smelting north of the start, the copper zone around ``primary``."""
    far = (137.0, -264.3) if primary_distance > 200 else (-2.5, -55.8)
    zone_rect = (115, -285, 160, -241) if primary_distance > 200 else (-20, -73, 13, -38)
    zones = (
        Zone(
            "smelting",
            "smelting",
            Rect(12, -136, 60, -72),
            "W",
            "W",
            "E",
            (Port("copper-ore", "in", "N", (58.0, -136.0)),),
        ),
        Zone(
            "mining",
            "mining:copper-ore",
            Rect(*zone_rect),
            "N",
            "",
            "W",
            (Port("copper-ore", "out", "W", (zone_rect[0], zone_rect[1] + 4.0)),),
        ),
    )
    corridor = Corridor(
        "corridor:copper-ore->smelting",
        "feeder",
        "copper-ore",
        "mining:copper-ore",
        "smelting",
        4,
        "W",
        "N",
        (Segment(Rect(56, -245, 115, -241), "W"),),
    )
    primary = PatchRef("copper-ore", 720, 2485275, Rect(*zone_rect), far, primary_distance, primary_distance)
    near = PatchRef("copper-ore", 333, 240191, Rect(-14, -67, 7, -44), (-2.5, -55.8), 56.4, 56.4)
    others = (near,) if primary_distance > 200 else ()
    row = ResourcePlan("copper-ore", primary, others)

    class Plan:
        def zone(self, name):
            return next((z for z in zones if z.name == name), None)

        def corridor(self, name):
            return corridor if name == corridor.name else None

        def resource(self, name):
            return row if name == "copper-ore" else None

    plan = Plan()
    plan.zones = zones
    plan.corridors = (corridor,)
    world_zones = [(z.name, z.rect) for z in zones]
    return SimpleNamespace(
        plan=lambda world_id: plan, world=lambda world_id: SimpleNamespace(zones=lambda: world_zones)
    )


class PatchChoiceTests(unittest.TestCase):
    def outposts(self, site):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(temp.name)))
        return MiningOutposts(game, None, None, site=site)

    def test_the_near_patch_is_mined_on_any_side_when_the_plan_zone_is_a_far_patch(self):
        outposts = self.outposts(copper_plan(297.94))
        obs = {"world_id": "w", "resources": {"copper-ore": {"position": {"x": -1.5, "y": -44.5}}}}
        calls = []

        def prepare(game, builder, obs, resource, count, reservations, **options):
            side = (options.get("site_exit") or {}).get("side")
            calls.append(
                (obs["resources"][resource]["position"], side, (options.get("site_exit") or {}).get("zone_rect"))
            )
            if side == "N":
                return {"ok": True, "plan": {}, "patch_diagnostic": {}}
            return {"ok": False, "reason": NO_COMPLIANT_BANK, "candidate_count": 0, "candidate_failures": []}

        with patch("factorio_agent.paired_mining.prepare_new_bank", side_effect=prepare):
            result = outposts.prepare(obs, "copper-ore", 2)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["plan"]["other_patch"]["bbox"], [-14, -67, 7, -44])
        # The far plan zone (298 tiles) is never surveyed; the near patch is tried with an exit
        # toward the smelting entry, nearest side first, inside its own zone (bbox + margin).
        self.assertTrue(all(position == {"x": -2.5, "y": -55.8} for position, _, _ in calls))
        self.assertEqual(calls[0][1], "N")
        self.assertEqual(calls[0][2], [-20, -73, 13, -38])

    def test_a_failure_names_every_attempt_and_what_would_have_to_change(self):
        outposts = self.outposts(copper_plan(297.94))
        obs = {"world_id": "w", "resources": {"copper-ore": {"position": {"x": -1.5, "y": -44.5}}}}

        def prepare(game, builder, obs, resource, count, reservations, **options):
            return {
                "ok": False,
                "reason": NO_COMPLIANT_BANK,
                "candidate_count": 3,
                "candidate_failures": ["bank_reserved_area_meets_registered_block:plan:mining:iron-ore"],
            }

        with patch("factorio_agent.paired_mining.prepare_new_bank", side_effect=prepare):
            result = outposts.prepare(obs, "copper-ore", 2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["planned_patch_skipped"]["distance"], 297.94)
        self.assertEqual([row["side"] for row in result["attempts"]], ["N", "E", "W", "S"])
        self.assertEqual(result["needs"], "registered_block_moved:plan:mining:iron-ore")

    def test_a_seed_outside_the_plan_zone_is_moved_onto_the_planned_patch(self):
        # Seed 3442106081: the observed iron seed lay on a far patch; the plan's iron patch is near.
        outposts = self.outposts(copper_plan(39.0))
        obs = {"world_id": "w", "resources": {"copper-ore": {"position": {"x": 190.5, "y": -242.5}}}}
        seen = []

        def prepare(game, builder, obs, resource, count, reservations, **options):
            seen.append((obs["resources"][resource]["position"], options["site_exit"]["side"]))
            return {"ok": True, "plan": {}, "patch_diagnostic": {}}

        with patch("factorio_agent.paired_mining.prepare_new_bank", side_effect=prepare):
            self.assertTrue(outposts.prepare(obs, "copper-ore", 2)["ok"])
        self.assertEqual(seen, [({"x": -2.5, "y": -55.8}, "W")])

    def test_patch_exits_cover_every_side_of_the_patch_zone(self):
        record = {
            "resource": "copper-ore",
            "zone": "mining:copper-ore",
            "zone_rect": [115, -285, 160, -241],
            "side": "W",
            "flow": "east_to_west",
            "facing": 12,
            "corridor": "corridor:copper-ore->smelting",
            "corridor_start": [115.0, -281.0],
            "entry_port": {"zone": "smelting", "side": "N", "point": [58.0, -136.0]},
            "guide": [56, -245, 115, -136],
        }
        exits = patch_exits(record, [-14, -67, 7, -44])
        self.assertEqual(sorted(e["side"] for e in exits), ["E", "N", "S", "W"])
        self.assertEqual(exits[0]["side"], "N")  # the smelting entry is north
        self.assertTrue(all(e["zone_rect"] == [-20, -73, 13, -38] and e["planned_side"] == "W" for e in exits))
        self.assertFalse(seed_in_zone(record, {"x": -1.5, "y": -44.5}))
        self.assertEqual(patch_exits(None, [0, 0, 1, 1]), [])


class SurveyTests(unittest.TestCase):
    def test_an_empty_survey_is_an_answer_not_a_changed_receipt(self):
        native = dict(
            mining_speed=0.5, mining_radius=2.49, mining_time=1, belt_speed=0.03125, force_productivity_bonus=0
        )
        reply = dict(
            ok=True, tick=5, world_id="w", surface_index=1, area=[[0, 0], [96, 96]], tiles={}, charted_chunks=0
        )
        game = SimpleNamespace(query=lambda body: dict(reply, native=native))
        result = survey_patch(game, {"world_id": "w"}, catalog(), "iron-ore", [[0, 0], [96, 96]])
        self.assertEqual(result["reason"], SURVEY_EMPTY)
        self.assertEqual(result["charted_chunks"], 0)


def rectangle_world(x0, y0, x1, y1, resource="coal", amount=300):
    world = WorldMap()
    world.mark_surveyed(Rect(-64, -64, 64, 64))
    for x in range(x0, x1):
        for y in range(y0, y1):
            world.set_ore(x, y, resource, amount)
    return world


class CandidateTests(unittest.TestCase):
    def test_a_bank_whose_port_already_stands_off_the_ore_is_a_candidate(self):
        world = rectangle_world(0, 0, 12, 16, amount=300)
        area = [[-48, -48], [48, 48]]
        record = {
            "resource": "coal",
            "flow": "west_to_east",
            "zone_rect": [-10, -10, 30, 30],
            "corridor_start": [20.0, 2.5],
        }
        sites = candidate_sites(snapshot(world, "coal", area), make_policy("w", "c", "coal"), 1, [], site_exit=record)
        tiles = {(t["position"]["x"], t["position"]["y"]): t for t in snapshot(world, "coal", area)["tiles"]}
        cells = [_exit_cells(tiles, q["plan"]["ports"][0]) for q in sites]
        self.assertIn(0, cells)  # dropped before: the edge bank, the cheapest exit of all


@unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
class RelaxationTests(unittest.TestCase):
    def test_a_patch_one_pair_wide_gets_a_bank_without_a_growth_pair(self):
        # A 12 x 16 patch whose west part is another block's reservation: the only pair column
        # (drills at x 10.5) has no room for a growth pair behind it.
        world = rectangle_world(0, 0, 12, 16, amount=300)
        reserved = [[[-20, -20], [8, 40]]]
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        builder = SimpleNamespace(catalog=catalog(), _occupied_by_plan=geometry._occupied_by_plan)
        obs = {
            "ok": True,
            "world_id": "w",
            "tick": 1,
            "entities": [],
            "resources": {"coal": {"position": {"x": 10.5, "y": 4.5}}},
        }

        def survey(planning, obs, catalog, resource, area, **_):
            return snapshot(world, resource, area)

        with (
            patch("factorio_agent.mining_layout_policy.survey_patch", side_effect=survey),
            patch("factorio_agent.mining_layout_policy.forbidden_tiles", return_value=[]),
            patch("factorio_agent.mining_layout_policy.preflight", side_effect=map_preflight(world)),
            patch(
                "factorio_agent.mining_layout_policy.resource_survey",
                return_value={"ok": True, "rate_per_drill": 30, "unique_remaining_resource": 5000},
            ),
            patch(
                "factorio_agent.mining_layout_policy.power_connection",
                return_value={"ok": True, "entities": [], "wire_hops": 1},
            ),
        ):
            bank = prepare_new_bank(LuaSurfaceGame(world), builder, obs, "coal", 2, reserved)
        self.assertTrue(bank["ok"], bank)
        self.assertEqual(
            bank["patch_diagnostic"].get("relaxed"),
            "no_growth_pair",
            (bank["reserved_pair_slots"], bank["mining_reserved_area"], bank["patch_diagnostic"]),
        )
        drills = [e["position"] for e in bank["plan"]["entities"] if e["name"] == "electric-mining-drill"]
        self.assertTrue(drills and all(p["x"] > 8 for p in drills))
        self.assertEqual(bank["reserved_pair_slots"], 1)
        self.assertEqual(bank["layout_contract"]["policy"]["minimum_growth_pairs"], 0)


@unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
class ObservedSeedTests(unittest.TestCase):
    def test_the_observed_patch_seed_is_the_nearest_ore_even_past_the_search_cap(self):
        # Seed 3442106081: 2995 iron tiles in range; the capped search returned the far patches first.
        from factorio_agent.core.deterministic_game import DeterministicGame

        bodies = []

        class Stop(Exception):
            pass

        def capture(body):
            bodies.append(body)
            raise Stop

        game = DeterministicGame.__new__(DeterministicGame)
        game.query = capture
        with self.assertRaises(Stop):
            game.observe()
        body = bodies[0]
        snippet = body[body.index("local resources={}") : body.index("local stats=")]
        lua = LuaRuntime(unpack_returned_tuples=True)
        lua.execute(
            """
s={};local tiles={}
-- 2400 far tiles listed first (as chunk order may), then the near patch.
for i=1,2400 do tiles[#tiles+1]={name="iron-ore",position={x=180+i%40,y=-250-math.floor(i/40)},amount=500} end
for x=25,40 do for y=-10,10 do tiles[#tiles+1]={name="iron-ore",position={x=x+.5,y=y+.5},amount=500} end end
function s.find_entities_filtered(q)
 local rows={}
 for _,e in ipairs(tiles) do
  if e.name==q.name and e.position.x^2+e.position.y^2<=q.radius^2 then rows[#rows+1]=e;if #rows>=q.limit then break end end
 end
 return rows
end
function pos(p) return {x=p.x,y=p.y} end
"""
        )
        found = lua.execute(snippet + " return resources")
        position = found["iron-ore"]["position"]
        self.assertEqual((position["x"], abs(position["y"])), (25.5, 0.5))


if __name__ == "__main__":
    unittest.main()
