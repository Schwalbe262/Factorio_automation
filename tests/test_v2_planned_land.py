"""One reservation rule: what is planned is published, and every planner reads it.

Fleet ``pace1`` (8 seeds, 45 min): three failure families were two planners
claiming the same land. Fixtures below are the saved geometry reduced:

* seed 2005199778 / 1590659659: the coal bank's exit and output access lay on
  the reserved power coal feed's belts; its board publish blocked, and the
  shared publish blocked the iron and copper lanes as well;
* seed 2005199778: the starter lab took a feed route tile; seeds 2212603376 /
  1590659659: the feed drill was planned on a pending starter cell's drill tile;
* seeds 3794581402 / 1634401926: starter cells took the electric drill site
  next to the feed's burner drill (``electric_upgrade_footprint_obstructed``).
"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.board import (
    Board,
    entity,
    keepout_tiles,
    pending_starter_cells,
    starter_keepouts,
)
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.mining_layout_policy import prepare_new_bank
from factorio_agent.outposts import MiningOutposts
from factorio_agent.power_coal_feed import feed_block
from factorio_agent.power_layout import feed_keepouts, keepout_entities, near
from factorio_agent.research import StarterResearch

# Seed 2005199778, reduced: the reserved feed (drill east of the coal patch, belt north).
FEED_DRILL = {"name": "burner-mining-drill", "position": {"x": 28, "y": 33}, "direction": 4}
FEED_PLAN = {
    "ok": True,
    "drill": FEED_DRILL,
    "entities": [
        FEED_DRILL,
        entity("transport-belt", 29.5, 32.5, 4),
        entity("burner-inserter", 29.5, 33.5, 4),
        entity("transport-belt", 30.5, 32.5, 8),
        entity("transport-belt", 30.5, 33.5, 8),
        entity("transport-belt", 30.5, 34.5, 4),
        entity("transport-belt", 31.5, 34.5, 4),
        entity("transport-belt", 32.5, 34.5, 0),
    ],
    "ports": [],
    "required_items": {},
}


def write(root, name, value):
    (Path(root) / name).write_text(json.dumps(value), encoding="utf-8")


def board(root, blocks):
    write(root, "pcb-board.json", {"schema_version": 1, "world_id": "w", "catalog_fingerprint": "c", "blocks": blocks})


GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)


class SharedKeepoutTests(unittest.TestCase):
    def test_pending_starter_cells_are_planned_land_until_built(self):
        with tempfile.TemporaryDirectory() as tmp:
            drill = {"name": "burner-mining-drill", "position": {"x": -71, "y": -45}, "direction": 0}
            chest = {"name": "wooden-chest", "position": {"x": -71.5, "y": -46.5}, "direction": 0}
            write(
                tmp,
                "starter-capacity.json",
                {"world_id": "w", "pending": {"coal": {"drill": drill, "receiver": chest}}},
            )
            built_chest = dict(chest, unit_number=5)
            obs = {"world_id": "w", "entities": [built_chest]}
            self.assertEqual(set(pending_starter_cells(tmp, obs)), {"starter_capacity:coal"})
            keepouts = starter_keepouts(tmp, obs)
            self.assertIn("-72,-46", keepouts["tiles"])  # the unbuilt drill (2x2)
            self.assertNotIn("-72,-47", keepouts["tiles"])  # the chest stands: built
            self.assertEqual(starter_keepouts(tmp, obs, exclude=("starter_capacity:coal",))["tiles"], [])
            self.assertEqual(starter_keepouts(tmp, {"world_id": "x", "entities": []})["tiles"], [])

    def test_keepout_tiles_cover_areas_and_tiles(self):
        tiles = keepout_tiles({"areas": [[26, 31, 29, 34]], "tiles": ["40,2"]})
        self.assertEqual(len(tiles), 10)
        self.assertIn((26.5, 31.5), tiles)
        self.assertNotIn((29.5, 31.5), tiles)
        self.assertIn((40.5, 2.5), tiles)

    def test_feed_block_reserves_the_electric_upgrade_site(self):
        block = feed_block(FEED_PLAN)
        self.assertEqual(block["reserved_area"], [[26, 31], [29, 34]])
        # electric_mining.replacement_for: 3x3 centred at (27.5, 32.5) lies inside.
        footprint = GEOMETRY._occupied_by_plan([entity("electric-mining-drill", 27.5, 32.5, 4)])
        self.assertTrue(all(26 < x < 29 and 31 < y < 34 for x, y in footprint))
        self.assertEqual(feed_block(FEED_PLAN, upgrade_site=False)["reserved_area"], [[27, 32], [29, 34]])


class CoalFeedSiteTests(unittest.TestCase):
    def test_feed_keepouts_skip_the_feed_block_but_hold_every_other_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            science = {"entities": [], "reserved_area": [[8.0, -40.0], [24.0, -24.0]]}
            board(tmp, {"power_coal_feed": feed_block(FEED_PLAN), "startup_science": science})
            b = SimpleNamespace(path=Path(tmp) / "factory-builder.json")
            keep = feed_keepouts(b, {"world_id": "w", "entities": []})
            self.assertIn((23.5, -27.5), keep)  # inside the science cell
            self.assertNotIn((30.5, 34.5), keep)  # the feed's own belt
            self.assertEqual(feed_keepouts(SimpleNamespace(), {"world_id": "w"}), set())
            rows = keepout_entities(near(keep, {(20.5, -30.5)}, margin=2))
            self.assertTrue(rows and all(r["name"] == "wooden-chest" for r in rows))
            self.assertTrue(all(18 <= r["position"]["x"] <= 23 for r in rows))


class StarterLabTests(unittest.TestCase):
    def test_lab_site_search_reads_the_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            board(tmp, {"power_coal_feed": feed_block(dict(FEED_PLAN))})
            game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=tmp), query=Mock(return_value={"ok": False}))
            research = StarterResearch(game, None, None)
            self.assertEqual(research.lab({"world_id": "w", "entities": []})["status"], "blocked")
            script = game.query.call_args.args[0]
            self.assertIn("keepout_hit(p.x-1.5,p.y-1.5,p.x+1.5,p.y+1.5)", script)
            self.assertIn('\\"30,34\\"', script)  # a planned feed belt tile is in the keepouts


class StartupScienceTests(unittest.TestCase):
    def test_science_cell_never_takes_another_blocks_reserved_area(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A bank reserved on every candidate cell except the far west ones.
            bank = {"entities": [], "reserved_area": [[-24.0, -60.0], [60.0, 60.0]]}
            board(tmp, {"coal_mining": bank})
            game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=tmp))
            surveyed = []

            def query(script):
                plans = json.loads(json.loads(script.split("helpers.json_to_table(", 1)[1].split(");", 1)[0]))
                surveyed.extend(plans)
                return {"ok": True, "legal": [1], "roomy": [], "clearable": []}

            game.query = query
            b = Board(game, GEOMETRY, SimpleNamespace(fingerprint="c"))
            obs = {
                "world_id": "w",
                "entities": [
                    {"name": "steam-engine", "position": {"x": 0, "y": 0}, "energy": 1, "electric_network_id": 1},
                    {"name": "small-electric-pole", "position": {"x": 0.5, "y": 0.5}, "electric_network_id": 1},
                ],
            }
            plan = b.reserve_startup_science(obs)
            self.assertTrue(surveyed)
            from factorio_agent.planning import overlap

            self.assertFalse(any(overlap(p["reserved_area"], bank["reserved_area"]) for p in surveyed))
            self.assertFalse(overlap(plan["reserved_area"], bank["reserved_area"]))


def snapshot():
    return dict(
        ok=True,
        tick=100,
        world_id="w",
        catalog_fingerprint="c",
        resource="coal",
        area=[[-32, -16], [16, 24]],
        tiles=[
            {
                "name": "coal",
                "position": {"x": x + 0.5, "y": y + 0.5},
                "amount": 100,
                "world_id": "w",
                "surface_index": 1,
            }
            for x in range(-20, 10)
            for y in range(-8, 16)
        ],
        surface_index=1,
        native=dict(
            mining_speed=0.5, mining_radius=2.49, mining_time=1, belt_speed=0.03125, force_productivity_bonus=0.1
        ),
    )


def catalog():
    return SimpleNamespace(
        fingerprint="c",
        entities={
            "coal": {"mining_products": [{"name": "coal", "amount": 1, "type": "item"}]},
            "electric-mining-drill": {"mining_speed": 0.5},
            "transport-belt": {"belt_speed": 0.03125},
        },
    )


def outlet(game, builder, obs, core, future, reservations):
    p = deepcopy(core["ports"][0])
    p["position"]["x"] += 4
    p.pop("mining_resource", None)
    path = [{"x": core["ports"][0]["position"]["x"] + i, "y": p["position"]["y"]} for i in range(5)]
    es = core["entities"] + [entity("transport-belt", q["x"], q["y"], 4) for q in path[1:]]
    access = [entity("transport-belt", p["position"]["x"] + i, p["position"]["y"], 4) for i in range(1, 4)]
    return dict(ok=True, entities=es, port=p, collector_port=core["ports"][0], path=path, output_access=access)


class BankCandidateTests(unittest.TestCase):
    def plan(self, **options):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        g = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(temp.name)), query=Mock())
        builder = SimpleNamespace(catalog=catalog(), _occupied_by_plan=GEOMETRY._occupied_by_plan)
        obs = {
            "ok": True,
            "world_id": "w",
            "tick": 100,
            "entities": [],
            "resources": {"coal": {"position": {"x": 0.5, "y": 0.5}}},
        }
        with (
            patch("factorio_agent.mining_layout_policy.survey_patch", return_value=snapshot()),
            patch("factorio_agent.mining_layout_policy.preflight", return_value={"ok": True, "clear_obstacles": []}),
            patch("factorio_agent.mining_layout_policy.plan_offore_exit", side_effect=outlet),
            patch(
                "factorio_agent.mining_layout_policy.resource_survey",
                return_value={"ok": True, "rate_per_drill": 30, "unique_remaining_resource": 5000},
            ),
            patch(
                "factorio_agent.mining_layout_policy.power_connection",
                return_value={"ok": True, "entities": [], "wire_hops": 1},
            ),
        ):
            return prepare_new_bank(g, builder, obs, "coal", 2, [], **options)

    def test_candidate_meeting_planned_hardware_or_a_registered_area_yields_to_the_next(self):
        first = self.plan()
        self.assertTrue(first["ok"], first)
        access = first["layout_contract"]["output_access"][0]
        tile = (access["x"], access["y"])
        # The reserved feed's belt stands on the first candidate's output access.
        planned = {tile: {"block": "power_coal_feed", "name": "transport-belt", "position": dict(access)}}
        moved = self.plan(planned=planned)
        self.assertTrue(moved["ok"], moved)
        self.assertNotEqual(moved["reserved_area"], first["reserved_area"])
        everything = moved["plan"]["entities"] + [
            entity("transport-belt", p["x"], p["y"], 4) for p in moved["layout_contract"]["output_access"]
        ]
        self.assertNotIn(tile, GEOMETRY._occupied_by_plan(everything))
        # A registered block's reserved area on the first candidate's exit.
        other = self.plan(
            registered=[("power_coal_feed", [[tile[0] - 0.5, tile[1] - 0.5], [tile[0] + 0.5, tile[1] + 0.5]])]
        )
        self.assertTrue(other["ok"], other)
        self.assertNotEqual(other["reserved_area"], first["reserved_area"])

    def test_no_fitting_candidate_blocks_with_evidence_naming_the_block(self):
        everywhere = [("startup_science", [[-200, -200], [200, 200]])]
        result = self.plan(registered=everywhere)
        self.assertFalse(result["ok"])
        self.assertIn("bank_reserved_area_meets_registered_block:startup_science", result["candidate_failures"])


def paired_bank(resource, area, bridge=()):
    plan = {
        "paired": True,
        "resource": resource,
        "entities": [entity("transport-belt", area[0][0] + 0.5, area[0][1] + 0.5, 4)] + list(bridge),
        "ports": [],
    }
    return {"plan": plan, "reserved_area": area, "ready": False}


class OutpostPublishTests(unittest.TestCase):
    def outposts(self, tmp, banks):
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(tmp)))
        o = MiningOutposts(game, None, None)
        o.state = {"world_id": "w", "banks": banks}
        return o

    def test_one_conflicting_bank_blocks_only_its_own_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            board(tmp, {"power_coal_feed": feed_block(FEED_PLAN)})
            coal = paired_bank("coal", [[15.0, 31.0], [33.0, 38.0]])  # seed 2005199778's coal bank
            iron = paired_bank("iron-ore", [[-33.0, -33.0], [-8.0, -26.0]])
            o = self.outposts(tmp, [iron, coal])
            obs = {"world_id": "w", "entities": []}
            self.assertIsNone(o.publish_reservations(obs, "iron-ore"))
            blocks = json.loads((Path(tmp) / "pcb-board.json").read_text(encoding="utf-8"))["blocks"]
            self.assertIn("iron-ore_mining", blocks)
            self.assertNotIn("coal_mining", blocks)
            conflict = o.publish_reservations(obs, "coal")
            self.assertEqual(conflict["reason"], "paired_mining_reserved_area_conflicts_with_registered_block")
            self.assertEqual((conflict["block"], conflict["conflicting_block"]), ("coal_mining", "power_coal_feed"))
            self.assertIsNotNone(o.publish_reservations(obs))  # other callers: any conflict

    def test_unbuilt_conflicting_bank_is_planned_again_a_bounded_number_of_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            board(tmp, {"power_coal_feed": feed_block(FEED_PLAN)})
            coal = paired_bank("coal", [[15.0, 31.0], [33.0, 38.0]])
            o = self.outposts(tmp, [coal])
            obs = {"world_id": "w", "entities": []}
            conflict = o.publish_reservations(obs, "coal")
            result = o._replan_conflicting_bank(obs, "coal", conflict)
            self.assertEqual(result["status"], "waiting")
            self.assertEqual(result["evidence"]["conflicting_block"], "power_coal_feed")
            self.assertEqual(o.state["banks"], [])
            # Something of a bank built: it stays and the lane blocks.
            o.state["banks"] = [coal]
            built = dict(coal["plan"]["entities"][0], unit_number=9)
            self.assertIsNone(o._replan_conflicting_bank({"world_id": "w", "entities": [built]}, "coal", conflict))
            o.state["bank_replans"] = {"coal": 2}
            self.assertIsNone(o._replan_conflicting_bank(obs, "coal", conflict))

    def test_parallel_banks_see_each_others_unpublished_bridge_poles(self):
        with tempfile.TemporaryDirectory() as tmp:
            board(tmp, {"power_coal_feed": feed_block(FEED_PLAN)})
            pole = entity("small-electric-pole", 30.5, 20.5)  # bridge pole outside the iron bank's area
            iron = paired_bank("iron-ore", [[-33.0, -33.0], [-8.0, -26.0]], bridge=[pole])
            o = self.outposts(tmp, [iron])
            obs = {"world_id": "w", "entities": [], "resources": {"coal": {"position": {"x": 20, "y": 34}}}}
            shared = o.planned_keepouts(obs, json.loads((Path(tmp) / "pcb-board.json").read_text()), "coal")
            self.assertEqual(shared["planned"][(30.5, 20.5)]["block"], "mining_outposts:iron-ore:0")
            self.assertEqual(shared["planned"][(30.5, 34.5)]["block"], "power_coal_feed")
            self.assertIn(("power_coal_feed", feed_block(FEED_PLAN)["reserved_area"]), shared["registered"])
            self.assertIn(("mining_outposts:iron-ore:0", iron["reserved_area"]), shared["registered"])
            self.assertIn([[30.0, 34.0], [31.0, 35.0]], shared["tile_areas"])
            far = dict(obs, resources={"coal": {"position": {"x": 500, "y": 500}}})
            self.assertEqual(o.planned_keepouts(far, {"blocks": {}}, "coal")["planned"], {})


class ElectricCoalEvidenceTests(unittest.TestCase):
    def test_obstructed_upgrade_names_the_entity(self):
        from factorio_agent.electric_mining import ElectricCoal

        with tempfile.TemporaryDirectory() as tmp:
            game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=tmp))
            coal = ElectricCoal(game, None, None)
            coal.prepare = Mock(
                return_value={
                    "ok": False,
                    "reason": "electric_upgrade_footprint_obstructed",
                    "entity": "wooden-chest",
                    "position": {"x": -1.5, "y": -86.5},
                }
            )
            result = coal.step({"world_id": "w", "technologies": {"electric-mining-drill": True}})
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["evidence"]["entity"], "wooden-chest")


if __name__ == "__main__":
    unittest.main()
