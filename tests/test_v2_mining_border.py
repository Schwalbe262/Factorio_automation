"""Mine frames follow their patch; neighbouring patches of another ore; border lanes with a filter splitter."""

from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.board import entity
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.core.deterministic_game import run_config
from factorio_agent.mining_border import (
    MIXED_PAYBACK,
    block_rects,
    configure_action,
    decide_border_lane,
    device_proof,
    device_raw_cost,
    filter_device,
    neighbour_reservations,
    rect_tiles,
    tile_rects,
)
from factorio_agent.mining_layout_policy import candidate_sites, make_policy, patch_grid, prepare_new_bank
from factorio_agent.planning import overlap
from factorio_agent.runtime import WorldBoundGame

from test_v2_mining_site_exit import live_plan

NATIVE = dict(mining_speed=0.5, mining_radius=2.49, mining_time=1, belt_speed=0.03125, force_productivity_bonus=0)


def snapshot(patches, resource):
    """``patches``: {resource: cells}; the survey holds every resource (type=resource)."""
    tiles = [
        {"name": name, "position": {"x": x + 0.5, "y": y + 0.5}, "amount": 500}
        for name, cells in patches.items()
        for x, y in sorted(cells)
    ]
    return dict(
        ok=True,
        tick=100,
        world_id="w",
        catalog_fingerprint="c",
        resource=resource,
        area=[[-60, -60], [60, 60]],
        tiles=tiles,
        surface_index=1,
        native=NATIVE,
    )


def drills(grid):
    return [d for row in grid["rows"] for d in row["drills"]]


def area(d):
    return {(d["x"] + i, d["y"] + j) for i in range(-2, 3) for j in range(-2, 3)}


def body(d):
    return {(d["x"] + i, d["y"] + j) for i in (-1, 0, 1) for j in (-1, 0, 1)}


def centres(cells):
    return {(x + 0.5, y + 0.5) for x, y in cells}


def frame_tiles(rects):
    return set().union(*(rect_tiles(r) for r in rects)) if rects else set()


# Iron west, stone east, sharing the border x = 20 | 21 (rows 0..23).
IRON = {(x, y) for x in range(0, 21) for y in range(0, 24)}
STONE = {(x, y) for x in range(21, 40) for y in range(0, 24)}


class RowWiseFrame(unittest.TestCase):
    def test_tile_rects_cover_exactly_the_tiles(self):
        tiles = {(0.5, 0.5), (1.5, 0.5), (0.5, 1.5), (1.5, 1.5), (5.5, 0.5), (0.5, 4.5)}
        rects = tile_rects(tiles)
        self.assertEqual(frame_tiles(rects), tiles)
        self.assertEqual(len(rects), 3)

    def test_an_l_shaped_patch_frame_follows_the_patch_not_its_box(self):
        cells = {(x, y) for x in range(0, 32) for y in range(0, 10)} | {
            (x, y) for x in range(0, 10) for y in range(10, 34)
        }
        grid = patch_grid(snapshot({"coal": cells}, "coal"), make_policy("w", "c", "coal"))
        self.assertTrue(grid["ok"], grid)
        tiles = frame_tiles(grid["frame_rects"])
        for d in drills(grid):
            self.assertLessEqual(body(d), tiles)
        # The empty corner of the L holds no drill and no reservation but the short rows' collectors
        # running to the common edge (one lane each), while the bounding box holds all of it.
        corner = {(x + 0.5, y + 0.5) for x in range(16, 32) for y in range(16, 34)}
        lanes = {row["port"]["y"] for row in grid["rows"]}
        self.assertTrue(all(y in lanes for _, y in tiles & corner))
        self.assertFalse(any(body(d) & corner for d in drills(grid)))
        self.assertGreater(len(grid["frame_rects"]), 1)
        self.assertLess(len(tiles), 0.6 * 34 * 34)
        # Per row the extent is the row's own span: the long rows have more columns than the short ones.
        columns = [row["columns"] for row in grid["rows"]]
        self.assertGreater(max(columns), min(columns))

    def test_a_notch_of_water_is_never_under_a_drill(self):
        cells = {(x, y) for x in range(0, 30) for y in range(0, 18)}
        water = {(x + 0.5, y + 0.5) for x in range(10, 16) for y in range(6, 12)}
        grid = patch_grid(snapshot({"coal": cells}, "coal"), make_policy("w", "c", "coal"), forbidden=sorted(water))
        self.assertTrue(grid["ok"], grid)
        self.assertTrue(drills(grid))
        self.assertFalse(any(body(d) & water for d in drills(grid)))

    def test_old_banks_keep_their_rectangle_as_a_legal_reservation(self):
        old = {"reserved_area": [[0, 0], [10, 10]]}
        self.assertEqual(block_rects(old), [[[0, 0], [10, 10]]])
        new = {"reserved_area": [[0, 0], [10, 20]], "reserved_rects": [[[0, 0], [10, 9]], [[4, 9], [5, 20]]]}
        self.assertEqual(block_rects(new), new["reserved_rects"])


class NeighbouringPatches(unittest.TestCase):
    def test_both_patches_get_a_clean_grid_and_the_frames_do_not_overlap(self):
        patches = {"iron-ore": IRON, "stone": STONE}
        iron = patch_grid(snapshot(patches, "iron-ore"), make_policy("w", "c", "iron-ore", flow="east_to_west"))
        reserved = iron["frame_rects"]
        stone = patch_grid(snapshot(patches, "stone"), make_policy("w", "c", "stone"), reserved)
        for grid, foreign in ((iron, centres(STONE)), (stone, centres(IRON))):
            self.assertTrue(grid["ok"], grid)
            self.assertTrue(drills(grid))
            # No drill's mining area covers the other ore (a single-item belt stays single-item).
            self.assertFalse(any(area(d) & foreign for d in drills(grid)))
            self.assertFalse(frame_tiles(grid["frame_rects"]) & foreign)  # the frame keeps off the other ore
        self.assertFalse(frame_tiles(iron["frame_rects"]) & frame_tiles(stone["frame_rects"]))
        # The ports leave on opposite edges, away from each other.
        self.assertLess(iron["rows"][0]["port"]["x"], 21)
        self.assertGreater(stone["rows"][0]["port"]["x"], 21)

    def test_a_port_facing_the_other_patch_ranks_after_one_that_does_not(self):
        # Iron flows east toward stone touching its upper rows only: the lower rows' ports are clear.
        stone = {(x, y) for x in range(25, 40) for y in range(0, 10)}
        iron = {(x, y) for x in range(0, 21) for y in range(0, 26)}
        s = snapshot({"iron-ore": iron, "stone": stone}, "iron-ore")
        grid = patch_grid(s, make_policy("w", "c", "iron-ore"))
        policy = make_policy("w", "c", "iron-ore", grid_origin=grid["origin"])
        sites = candidate_sites(s, policy, 1, [], grid=grid)
        self.assertTrue(any(q["faces_neighbour"] for q in sites))
        self.assertFalse(sites[0]["faces_neighbour"])

    def test_a_planned_neighbour_frame_is_a_keep_out_only_for_another_resource(self):
        frame = [[[0, 0], [8, 8]]]
        blocks = [{"resource": "iron-ore", "reserved_area": [[0, 0], [4, 4]], "patch_frame": frame}]
        self.assertIn(frame[0], neighbour_reservations(blocks, (), "stone"))
        self.assertNotIn(frame[0], neighbour_reservations(blocks, (), "iron-ore"))

    def test_the_outpost_turns_away_from_an_edge_facing_the_neighbour(self):
        from factorio_agent.outposts import MiningOutposts

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(temp.name)))
        outposts = MiningOutposts(game, None, None, site=SimpleNamespace(plan=lambda world_id: live_plan()))
        facing = {"ok": True, "patch_diagnostic": {"faces_neighbour": True}}
        clear = {"ok": True, "patch_diagnostic": {"faces_neighbour": False}}
        with patch("factorio_agent.paired_mining.prepare_new_bank", side_effect=[facing, clear]) as prepare:
            result = outposts.prepare({"world_id": "w"}, "coal", 2)
        self.assertEqual(prepare.call_count, 2)
        self.assertFalse(result["patch_diagnostic"]["faces_neighbour"])
        self.assertIn("edge_turned_from_neighbour", result["patch_diagnostic"])


class NewBankReservation(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(temp.name)), query=Mock())
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        products = {r: {"mining_products": [{"name": r, "amount": 1, "type": "item"}]} for r in ("iron-ore", "stone")}
        catalog = SimpleNamespace(fingerprint="c", entities=products)
        self.builder = SimpleNamespace(catalog=catalog, _occupied_by_plan=geometry._occupied_by_plan)

    def prepare(self, resource, seed, reservations, **options):
        obs = {"world_id": "w", "tick": 100, "entities": [], "resources": {resource: {"position": seed}}}

        def outlet(game, builder, obs, core, future, reservations):
            source = core["ports"][0]
            ux = 1 if source.get("facing", 4) == 4 else -1
            path = [{"x": source["position"]["x"] + ux * i, "y": source["position"]["y"]} for i in range(5)]
            p = dict(deepcopy(source), position=dict(path[-1]))
            p.pop("mining_resource", None)
            es = core["entities"] + [
                entity("transport-belt", q["x"], q["y"], source.get("facing", 4)) for q in path[1:]
            ]
            access = [entity("transport-belt", path[-1]["x"] + ux * i, path[-1]["y"], 4) for i in range(1, 4)]
            return dict(ok=True, entities=es, port=p, collector_port=source, path=path, output_access=access)

        with (
            patch(
                "factorio_agent.mining_layout_policy.survey_patch",
                return_value=snapshot({"iron-ore": IRON, "stone": STONE}, resource),
            ),
            patch("factorio_agent.mining_layout_policy.forbidden_tiles", return_value=[]),
            patch("factorio_agent.mining_layout_policy.preflight", return_value={"ok": True, "clear_obstacles": []}),
            patch("factorio_agent.mining_layout_policy.plan_offore_exit", side_effect=outlet),
            patch(
                "factorio_agent.mining_layout_policy.resource_survey",
                return_value={"ok": True, "unique_remaining_resource": 5000},
            ),
            patch(
                "factorio_agent.mining_layout_policy.power_connection",
                return_value={"ok": True, "entities": [], "wire_hops": 1},
            ),
        ):
            return prepare_new_bank(self.game, self.builder, obs, resource, 2, reservations, **options)

    def test_adjacent_banks_reserve_row_wise_frames_that_do_not_overlap(self):
        iron = self.prepare("iron-ore", {"x": 10.5, "y": 10.5}, [], policy=None)
        self.assertTrue(iron["ok"], iron)
        self.assertGreater(len(iron["reserved_rects"]), 1)  # footprint + exit + output access, not one box
        self.assertTrue(iron["patch_frame"])
        held = neighbour_reservations([dict(iron["plan"], **iron)], (), "stone")
        stone = self.prepare("stone", {"x": 30.5, "y": 10.5}, held)
        self.assertTrue(stone["ok"], stone)
        for a in iron["reserved_rects"]:
            for b in stone["reserved_rects"]:
                self.assertFalse(overlap(a, b), (a, b))
        foreign = {"iron-ore": centres(STONE), "stone": centres(IRON)}
        for bank in (iron, stone):
            mined = [e["position"] for e in bank["plan"]["entities"] if e["name"] == "electric-mining-drill"]
            self.assertFalse(any(area(d) & foreign[bank["plan"]["resource"]] for d in mined))


class MixedZone(unittest.TestCase):
    def test_interleaved_ore_is_reported_and_a_clean_border_is_not(self):
        coal = {(x, y) for x in range(0, 30) for y in range(0, 20)}
        speckle = {(x, y) for x in range(10, 18) for y in range(4, 12) if (x + y) % 2 == 0}
        grid = patch_grid(snapshot({"coal": coal - speckle, "stone": speckle}, "coal"), make_policy("w", "c", "coal"))
        zone = grid["mixed_ore_zone"]
        self.assertGreaterEqual(zone["tiles"], len(speckle))
        self.assertEqual(zone["resources"], ["stone"])
        self.assertFalse(zone["handled"])
        (x0, y0), (x1, y1) = zone["bbox"]
        self.assertTrue(x0 <= 10 and x1 >= 18 and y0 <= 4 and y1 >= 12)
        # No drill stands where its area would take the speckled stone.
        self.assertFalse(any(area(d) & centres(speckle) for d in drills(grid)))
        clean = patch_grid(snapshot({"iron-ore": IRON, "stone": STONE}, "iron-ore"), make_policy("w", "c", "iron-ore"))
        self.assertNotIn("mixed_ore_zone", clean)


class BorderLane(unittest.TestCase):
    def device(self, sink="chest"):
        return filter_device((10.5, 2.5), "iron-ore", "stone", sink=sink)

    def test_border_cells_stay_excluded_until_the_filter_pays_and_is_researched(self):
        device = self.device()["entities"]
        cost = device_raw_cost(device)
        rich = MIXED_PAYBACK * cost + 1
        self.assertEqual(decide_border_lane(rich, 30, ["stone"], device, filter_ready=True)["decision"], "mixed")
        self.assertEqual(decide_border_lane(rich, 30, ["stone"], device, filter_ready=False)["decision"], "exclude")
        self.assertEqual(decide_border_lane(cost, 30, ["stone"], device, filter_ready=True)["decision"], "exclude")
        two = decide_border_lane(rich, 30, ["coal", "stone"], device, filter_ready=True)
        self.assertEqual(two["decision"], "exclude")

    def test_the_grid_marks_a_paying_border_lane_mixed_with_its_filter_device(self):
        # A stone island inside the iron: the drills whose areas reach it would win ore no clean drill gets.
        island = {(10, 6), (11, 6), (10, 7), (11, 7)}
        iron = {(x, y) for x in range(0, 30) for y in range(0, 24)} - island
        s = snapshot({"iron-ore": iron, "stone": island}, "iron-ore")
        policy = make_policy("w", "c", "iron-ore")
        fixed = {"x": 0.5, "y": 0.5}
        before = patch_grid(s, policy, origin=fixed)
        after = patch_grid(s, policy, origin=fixed, filter_ready=True)
        lanes = [r["border_lane"] for r in after["rows"] if "border_lane" in r]
        self.assertTrue(lanes)
        self.assertTrue(all(r["border_lane"]["decision"] == "exclude" for r in before["rows"] if "border_lane" in r))
        self.assertTrue(any(lane["decision"] == "mixed" for lane in lanes))
        for lane in lanes:
            self.assertGreater(lane["gained_tiles"], 0)
            if lane["decision"] == "mixed":
                self.assertGreaterEqual(lane["gained_amount"], MIXED_PAYBACK * lane["device_raw_ore"])
                self.assertEqual(lane["mixed"], ["iron-ore", "stone"])
                splitter = next(e for e in lane["device"] if e["name"] == "splitter")
                self.assertEqual(splitter["splitter_filter"], "stone")
                self.assertEqual(splitter["splitter_output_priority"], "right")
        # The placed grid drills are unchanged: border drills are a recorded lane, not hidden in the bank.
        self.assertEqual(before["drills"], after["drills"])

    def observed(self, device, main_items, chest_items):
        splitter = device["splitter"]
        rows = [
            dict(
                splitter,
                unit_number=5,
                splitter_filter=splitter["splitter_filter"],
            ),
            dict(device["main_output"], belt_inventory=main_items),
            dict(device["foreign_output"], belt_inventory={"stone": 8}),
        ]
        if device["chest"]:
            rows.append(dict(device["chest"], inventory={"stone": chest_items}))
        return {"world_id": "w", "entities": rows}

    def test_the_trunk_carries_only_the_main_item_and_a_full_sink_stops_the_border_drills(self):
        device = self.device()
        lane = [entity("transport-belt", x + 0.5, 2.5, 4) for x in range(0, 10)]
        border = [{"x": 2.5, "y": 0.5}]
        ok = device_proof(self.observed(device, {"iron-ore": 6}, 10), device, lane, border)
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(ok["main_items"], ["iron-ore"])
        self.assertFalse(ok["retire_border"])
        dirty = device_proof(self.observed(device, {"iron-ore": 6, "stone": 1}, 10), device, lane, border)
        self.assertEqual(dirty["reason"], "border_trunk_contaminated")
        # Sink nearly full: the main item still flows and the border drills are retired before the
        # foreign item can back up into the splitter.
        full = device_proof(self.observed(device, {"iron-ore": 6}, 16 * 50 - 20), device, lane, border)
        self.assertTrue(full["ok"])
        self.assertTrue(full["retire_border"])
        self.assertEqual(full["retire"], border)

    def test_the_filter_is_set_by_a_guarded_paid_configure_splitter(self):
        device = self.device()
        obs = self.observed(device, {}, 0)
        obs["entities"][0] = dict(obs["entities"][0], splitter_filter=None, splitter_output_priority="none")
        action = configure_action(SimpleNamespace(), obs, device["splitter"])
        self.assertEqual(action["filter"], "stone")
        self.assertEqual(action["output_priority"], "right")
        self.assertIsNone(configure_action(SimpleNamespace(), self.observed(device, {}, 0), device["splitter"]))
        with tempfile.TemporaryDirectory() as directory:
            game = WorldBoundGame(run_config(runtime=Path(directory)), "w")
            receipt = {"ok": True, "status": "succeeded", "filter": "stone"}
            with patch.object(game, "query", return_value=receipt) as query:
                self.assertEqual(game.act(action), receipt)
            script = query.call_args.args[0]
            self.assertIn("e.splitter_filter=", script)
            self.assertIn("e.unit_number~=x.expected_splitter_unit", script)
            for bad in ({"filter": ""}, {"filter": 3}, {"output_priority": "none"}):
                with patch.object(game, "query") as query, self.assertRaises(ValueError):
                    game.act(dict(action, **bad))
                query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
