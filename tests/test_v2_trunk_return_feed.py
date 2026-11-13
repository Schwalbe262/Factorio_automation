"""Return feed on a patch trunk: surplus back onto the trunk ahead of mined items (trunk_return_feed, return_feed)."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from factorio_agent import carried_surplus as cs
from factorio_agent import mine_trunk, return_feed
from factorio_agent import trunk_return_feed as trf
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.jobs import CAPABILITIES
from factorio_agent.transport import xy
from test_v2_mine_trunk import patch_bank, trunk_belts

WORLD = "w"
CATALOG = SimpleNamespace(
    fingerprint="c",
    items={"coal": {"stack_size": 50}, "iron-ore": {"stack_size": 50}, "wood": {"stack_size": 100}},
    entities={},
)
ALLOW = cs.allowance("coal", CATALOG)
PREFLIGHT_OK = {"ok": True, "clear_obstacles": []}


def chest(unit, x=4.5, y=4.5, **inventory):
    return {"name": "wooden-chest", "position": {"x": x, "y": y}, "unit_number": unit, "inventory": inventory}


class FeedsStub:
    """A return feed for ``coal`` only (``TrunkReturnFeed.chest`` contract)."""

    def __init__(self, coal_chest):
        self.coal_chest = coal_chest

    def chest(self, obs, item):
        return self.coal_chest if item == "coal" else None


class Trunk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bank = patch_bank()
        (self.root / "mining-outposts.json").write_text(json.dumps({"world_id": WORLD, "banks": [self.bank]}))
        self.belts = trunk_belts(self.bank)
        district = {"ok": True, "entities": [{k: e[k] for k in ("name", "position", "direction")} for e in self.belts]}
        (self.root / "smelting-district.json").write_text(json.dumps({"world_id": WORLD, "plan": district}))
        board = {"world_id": WORLD, "catalog_fingerprint": "c", "blocks": {"smelting_district": district}}
        (self.root / "pcb-board.json").write_text(json.dumps(board))
        plan = self.bank["plan"]["entities"]
        self.poles = [dict(e, unit_number=40 + i) for i, e in enumerate(plan) if e["name"] == "small-electric-pole"]
        self.drills = [dict(e, unit_number=10 + i) for i, e in enumerate(plan) if e["name"] == "electric-mining-drill"]
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        self.builder = SimpleNamespace(
            state={},
            _occupied_by_plan=geometry._occupied_by_plan,
            _move=lambda o, p: None,
            ensure_plan=Mock(return_value={"status": "succeeded"}),
            bootstrap=SimpleNamespace(ensure_item=Mock(return_value={"status": "succeeded"})),
        )
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root))
        self.feed = trf.TrunkReturnFeed(game, self.builder, CATALOG)
        self.entities = self.drills + self.poles + self.belts
        patch("factorio_agent.planning.preflight", return_value=PREFLIGHT_OK).start()
        self.addCleanup(patch.stopall)

    def obs(self, coal=0, **inventory):
        return {
            "world_id": WORLD,
            "tick": 100,
            "position": {"x": -30, "y": -38},
            "inventory": dict({"coal": coal, "splitter": 1}, **inventory),
            "entities": self.entities,
        }

    def build(self, layout):
        """The world after the side build and the splice: trunk belt mined, splitter and side hardware standing."""
        self.entities = [e for e in self.entities if xy(e) != xy(layout["belt"])]
        self.entities += [dict(deepcopy(e), unit_number=900 + i) for i, e in enumerate(layout["entities"])]
        self.entities[-1]["inventory"] = {}  # the chest
        self.entities.append(dict(deepcopy(layout["splitter"]), unit_number=950))


class LayoutTests(Trunk):
    def test_head_feed_has_side_input_priority_and_trunk_output_priority(self):
        prepared = self.feed.prepare(self.obs(), "coal")
        self.assertTrue(prepared["ok"], prepared)
        layout, r = prepared["layout"], prepared["row"]
        self.assertGreaterEqual(r, 1)
        self.assertEqual(xy(layout["belt"]), xy(self.belts[r]))  # one trunk belt of the first run
        split = layout["splitter"]
        self.assertEqual(split["splitter_input_priority"], return_feed.splitter_side(split, layout["side"]))
        self.assertEqual(
            split["splitter_output_priority"], return_feed.splitter_side(split, layout["belt"]["position"])
        )
        self.assertNotEqual(split["splitter_input_priority"], split["splitter_output_priority"])
        # Nearest the head first: the elbow beside the bank's pole, powered, so an electric inserter.
        self.assertEqual(layout.get("shape"), "elbow")
        self.assertEqual(return_feed.arm_spec(layout)["name"], return_feed.ELECTRIC_ARM)
        side_belt, arm, box = layout["entities"]
        self.assertEqual((side_belt["direction"], box["name"]), (4, return_feed.CHEST))

    def test_placed_upstream_of_every_tap_and_later_taps_keep_clear(self):
        tap = {"name": "a:coal", "trunk": mine_trunk.trunk_key(self.bank), "along": 6, "side": "right", "segment": 0}
        mine_trunk.record_tap(self.root, WORLD, dict(tap, entities=[], remove=[]))
        prepared = self.feed.prepare(self.obs(), "coal")
        self.assertTrue(prepared["ok"], prepared)
        self.assertLess(prepared["row"], tap["along"])
        self.feed.state = {"world_id": WORLD, "feeds": {"coal": prepared}}
        mine_trunk.record_return(self.root, WORLD, self.feed.station("coal", prepared))
        stations = mine_trunk.stations(self.root, WORLD, mine_trunk.trunk_key(self.bank), exclude="a:coal")
        later = mine_trunk.plan_tap(self.bank, self.obs(), owner="b", name="b:coal", demand=30, stations=stations)
        self.assertTrue(later["ok"], later)
        self.assertTrue(all(t["along"] >= prepared["row"] + 3 for t in later["taps"]))
        # A tap right behind the head leaves no row: a precise failure, never a feed downstream of a tap.
        mine_trunk.record_tap(self.root, WORLD, dict(tap, name="z:coal", along=1, entities=[], remove=[]))
        self.assertEqual(self.feed.prepare(self.obs(), "coal")["reason"], "no_return_feed_site_at_the_trunk_head")

    def test_ore_without_a_pole_has_no_site_and_coal_gets_a_burner_arm(self):
        self.entities = self.drills + self.belts  # the bank's poles are not observed
        self.bank["plan"]["resource"] = "iron-ore"
        (self.root / "mining-outposts.json").write_text(json.dumps({"world_id": WORLD, "banks": [self.bank]}))
        refused = self.feed.prepare(self.obs(), "iron-ore")
        self.assertFalse(refused["ok"])
        self.assertIn("return_feed_inserter_unpowered", refused["rejected"])
        self.bank["plan"]["resource"] = "coal"
        (self.root / "mining-outposts.json").write_text(json.dumps({"world_id": WORLD, "banks": [self.bank]}))
        layout = self.feed.prepare(self.obs(), "coal")["layout"]
        self.assertEqual(return_feed.arm_spec(layout)["name"], return_feed.BURNER_ARM)

    def test_wood_feed_shape_is_the_column_and_unchanged(self):
        from factorio_agent import wood_burner_feed as wbf

        belts = [{"name": "transport-belt", "position": {"x": x + 0.5, "y": -0.5}, "direction": 4} for x in range(5)]
        rows = wbf.layouts({"entities": belts})
        self.assertTrue(rows and all("shape" not in r for r in rows))
        self.assertEqual(rows, return_feed.layouts(belts))


class BuildTests(Trunk):
    def test_release_splice_seed_register_stock_and_proof(self):
        first = self.feed.step(self.obs(coal=ALLOW + 300), {"resource": "coal"})
        entry = self.feed.state["feeds"]["coal"]
        layout = entry["layout"]
        self.assertEqual((first["type"], first["expected_entity_unit"]), ("mine", layout["belt_unit"]))
        # The owner's plan and board copy dropped the trunk belt before the mine; the feed is on the ledger.
        district = json.loads((self.root / "smelting-district.json").read_text())["plan"]
        self.assertNotIn(xy(layout["belt"]), {xy(e) for e in district["entities"]})
        board = json.loads((self.root / "pcb-board.json").read_text())
        self.assertEqual(board["blocks"]["smelting_district"], district)
        self.assertEqual(board["blocks"]["trunk_return_feed:coal"]["owner"], "mine_trunk")
        ledger = mine_trunk.ledger(self.root, WORLD)["trunks"][mine_trunk.trunk_key(self.bank)]
        self.assertEqual(ledger["returns"]["return:coal"]["kind"], "return")
        self.assertEqual(ledger.get("taps"), {})  # no consumer demand recorded for the feed
        passthrough = mine_trunk.tap_passthrough(self.root, WORLD, self.bank)
        self.assertEqual(passthrough[xy(layout["belt"])]["name"], "splitter")
        self.build(layout)
        if return_feed.arm_spec(layout)["name"] == return_feed.BURNER_ARM:
            seed = self.feed.step(self.obs(coal=ALLOW + 300), {"resource": "coal"})
            self.assertEqual((seed["type"], seed["item"], seed["count"]), ("insert", "coal", 2))
            self.entities[-3]["inventory"] = {"coal": 2}
        deposit = self.feed.step(self.obs(coal=ALLOW + 300), {"resource": "coal"})
        self.assertEqual((deposit["type"], deposit["item"], deposit["count"]), ("deposit", "coal", 300))
        self.assertEqual(deposit["expected_entity_unit"], self.entities[-2]["unit_number"])
        done = self.feed.step(self.obs(coal=ALLOW), {"resource": "coal"})
        self.assertEqual(done["status"], "succeeded", done)
        # The trunk trace passes the splitter and runs on to its end.
        self.assertEqual(done["evidence"]["trunk_tiles_traced"], len(self.belts))
        self.assertEqual(self.feed.chest(self.obs(), "coal")["unit_number"], self.entities[-2]["unit_number"])
        self.entities[-1]["splitter_input_priority"] = "none"  # wrong live priority: no proof
        self.assertIsNone(self.feed.proof(self.obs(), {"resource": "coal"}))

    def test_parameters(self):
        trf.validate_parameters({"resource": "coal"})
        for bad in ({}, {"resource": "stone"}, {"resource": "coal", "x": 1}):
            with self.assertRaises(ValueError):
                trf.validate_parameters(bad)


class TriggerTests(unittest.TestCase):
    def decide(self, surplus=0, trips=0, distance=20.0, spare=800.0):
        return trf.decide(surplus=surplus, trips=trips, stack=50, distance=distance, spare_per_minute=spare)

    def test_small_surplus_keeps_the_trips_large_or_recurring_builds_the_feed(self):
        self.assertFalse(self.decide(surplus=50)["ready"])  # one stack: one trip of 12 s < 40 s + walk
        self.assertEqual(self.decide(surplus=50)["reason"], "surplus_trips_cheaper_than_the_feed")
        self.assertTrue(self.decide(surplus=300)["ready"])  # six stacks: 72 s > 44.5 s
        self.assertTrue(self.decide(surplus=0, trips=5)["ready"])  # recurring: 60 s > 44.5 s
        self.assertFalse(self.decide(surplus=0, trips=5, distance=200)["ready"])  # far trunk: walk dominates
        self.assertEqual(self.decide(surplus=900, spare=0)["reason"], "trunk_has_no_spare_belt_capacity")

    def nodes(self, ready=True, jobs=None, recipes=("splitter",), built=False, queue=()):
        jobs = jobs if jobs is not None else [{"capability": "mining_outpost", "status": "succeeded"}]
        row = {"ready": ready, "observed": True, "built": built, "trunk": "coal@1,1", "surplus": 400}
        observation = {"trunk_return_feeds": {"items": {"coal": row}}, "enabled_recipes": list(recipes)}
        return trf.trunk_return_feed_nodes(jobs, list(queue), observation, CAPABILITIES)

    def test_proposed_once_per_trunk_with_splitters(self):
        nodes = self.nodes()
        self.assertEqual([(n["capability"], n["parameters"]) for n in nodes], [(trf.CAPABILITY, {"resource": "coal"})])
        self.assertEqual(self.nodes(ready=False), [])
        self.assertEqual(self.nodes(recipes=()), [])  # splitters need logistics
        self.assertEqual(self.nodes(jobs=[]), [])  # a patch bank first
        self.assertEqual(self.nodes(built=True), [])
        self.assertEqual(self.nodes(queue=nodes), [])
        stalled = {"capability": trf.CAPABILITY, "parameters": {"resource": "coal"}, "status": "blocked"}
        self.assertEqual(self.nodes(jobs=[{"capability": "mining_outpost", "status": "succeeded"}, stalled]), [])

    def test_receipt_counts_carried_excess_depots_and_recent_trips(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            bank = patch_bank()
            (root / "mining-outposts.json").write_text(json.dumps({"world_id": WORLD, "banks": [bank]}))
            storage = {"world_id": WORLD, "chests": [{"name": "iron-chest", "unit_number": 4}]}
            (root / "inventory-storage.json").write_text(json.dumps(storage))
            depot = {"name": "iron-chest", "unit_number": 4, "inventory": {"coal": 30}}
            obs = {
                "world_id": WORLD,
                "tick": 1000,
                "position": bank["plan"]["ports"][0]["position"],
                "inventory": {"coal": ALLOW + 20},
                "entities": [depot, *trunk_belts(bank)],
            }
            small = trf.receipt(root, obs, CATALOG)["items"]["coal"]
            self.assertEqual((small["surplus"], small["recent_trips"], small["ready"]), (50, 0, False))
            self.assertTrue(small["observed"])
            self.assertEqual(small["spare_per_minute"], 900.0 - 60.0)
            for tick in (100, 200, 300, 400):
                cs.log_trip(root, dict(obs, tick=tick), "coal")
            busy = trf.receipt(root, obs, CATALOG)["items"]["coal"]
            self.assertEqual((busy["recent_trips"], busy["expected_trips"], busy["ready"]), (4, 5, True))
            later = dict(obs, tick=1000 + cs.TRIP_WINDOW_TICKS + 500)  # the trips left the window
            self.assertEqual(trf.receipt(root, later, CATALOG)["items"]["coal"]["recent_trips"], 0)


class SinkTests(unittest.TestCase):
    def setUp(self):
        self.feed_chest = chest(9, x=40.5, y=0.5)
        self.depot = chest(30, x=2.5, y=0.5)
        self.storage = SimpleNamespace(
            state={"world_id": WORLD, "chests": [{"name": "wooden-chest", "unit_number": 30}]}
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.surplus = cs.CarriedSurplus(
            CATALOG, storage=self.storage, return_feeds=FeedsStub(self.feed_chest), root=self.root
        )

    def obs(self, inventory, *entities, **extra):
        return {
            "world_id": WORLD,
            "tick": 50,
            "position": {"x": 0, "y": 0},
            "inventory": inventory,
            "entities": list(entities),
            **extra,
        }

    def test_return_feed_chest_first_with_the_whole_surplus(self):
        boiler = {"name": "boiler", "position": {"x": 1, "y": 0}, "unit_number": 1, "inventory": {}}
        state = self.obs({"coal": ALLOW + 400}, boiler, self.feed_chest, self.depot)
        action = self.surplus.step(state)
        self.assertEqual((action["type"], action["expected_entity_unit"], action["count"]), ("deposit", 9, 400))
        self.assertEqual(cs.recent_trips(self.root, state, "coal"), 0)  # the feed is not a disposal trip
        # Without the feed: the nearer consumer, a logged trip.
        self.surplus.return_feeds = FeedsStub(None)
        action = self.surplus.step(state)
        self.assertEqual((action["type"], action["name"]), ("insert", "boiler"))
        self.assertEqual(cs.recent_trips(self.root, state, "coal"), 1)

    def test_feed_chest_only_receives_its_own_item_and_is_never_a_depot(self):
        self.storage.state["chests"].append({"name": "wooden-chest", "unit_number": 9})  # even if registered
        state = self.obs({"iron-ore": cs.allowance("iron-ore", CATALOG) + 100}, self.feed_chest, self.depot)
        action = self.surplus.step(state)
        self.assertEqual((action["type"], action["item"], action["expected_entity_unit"]), ("deposit", "iron-ore", 30))
        self.assertEqual([c["unit_number"] for c in self.surplus.depots(state)], [30])

    def test_depot_coal_drains_towards_the_feed_only_when_idle_or_passing(self):
        far = chest(30, x=60.5, y=0.5, coal=120)
        state = self.obs({"coal": ALLOW}, self.feed_chest, far)
        self.assertIsNone(self.surplus.step(state))  # busy pass, depot far away: no shuttle
        take = self.surplus.step(state, idle=True)
        self.assertEqual((take["type"], take["position"]), ("take", far["position"]))
        self.assertEqual((take["item"], take["count"]), ("coal", 50))
        near = chest(30, x=5.5, y=0.5, coal=120)
        self.assertEqual(self.surplus.step(self.obs({"coal": ALLOW}, self.feed_chest, near))["type"], "take")
        # The taken coal is surplus and goes whole into the feed chest next.
        after = self.surplus.step(self.obs({"coal": ALLOW + 50}, self.feed_chest, near))
        self.assertEqual((after["type"], after["expected_entity_unit"]), ("deposit", 9))
        # A full inventory or a full feed chest takes nothing.
        self.assertIsNone(self.surplus.step(dict(state, inventory_empty_slots=0), idle=True))
        self.feed_chest["inventory"] = {"stone": 16 * 50}
        self.assertIsNone(self.surplus.step(state, idle=True))

    def test_starter_retirement_sinks_into_the_feed_chest(self):
        # Retirement calls sink_action for its previous trip and sizes the next by trip_room.
        state = self.obs({"coal": ALLOW + 200}, self.feed_chest)
        action = self.surplus.sink_action(state, "coal", 200, "put retiring coal starter stock to use")
        self.assertEqual((action["type"], action["expected_entity_unit"], action["count"]), ("deposit", 9, 200))
        self.assertEqual(self.surplus.trip_room(self.obs({"coal": ALLOW}, self.feed_chest), "coal"), 16 * 50)

    def test_wood_order_is_unchanged(self):
        wood_chest = chest(8, x=6.5, y=0.5)
        self.surplus.wood_feed = SimpleNamespace(chest=lambda o: wood_chest)
        boiler = {"name": "boiler", "position": {"x": 1, "y": 0}, "unit_number": 1, "inventory": {"coal": 40}}
        action = self.surplus.step(self.obs({"wood": 300}, boiler, wood_chest, self.feed_chest), idle=True)
        self.assertEqual((action["item"], action["expected_entity_unit"]), ("wood", 8))


if __name__ == "__main__":
    unittest.main()
