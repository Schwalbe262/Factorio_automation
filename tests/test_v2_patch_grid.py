"""Whole-patch drill grid (Mining Patch Planner "simple" principles) in mining_layout_policy."""

from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.board import entity
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.mining_layout_policy import (
    BANK_PITCH,
    PAIR_PITCH,
    candidate_sites,
    make_policy,
    patch_grid,
    prepare_new_bank,
)


def snapshot(cells, resource="coal", other=(), amount=500):
    tiles = [{"name": resource, "position": {"x": x + 0.5, "y": y + 0.5}, "amount": amount} for x, y in cells]
    tiles += [{"name": "stone", "position": {"x": x + 0.5, "y": y + 0.5}, "amount": 500} for x, y in other]
    return dict(
        ok=True,
        tick=100,
        world_id="w",
        catalog_fingerprint="c",
        resource=resource,
        area=[[-60, -60], [60, 60]],
        tiles=tiles,
        surface_index=1,
        native=dict(
            mining_speed=0.5, mining_radius=2.49, mining_time=1, belt_speed=0.03125, force_productivity_bonus=0
        ),
    )


def footprint(drill):
    return {(drill["x"] + i, drill["y"] + j) for i in (-1, 0, 1) for j in (-1, 0, 1)}


class PatchGrid(unittest.TestCase):
    def test_the_grid_covers_an_irregular_patch_with_every_lane_at_one_edge(self):
        # An L-shaped patch with a notch: rows of different length.
        cells = {(x, y) for x in range(0, 30) for y in range(0, 10)}
        cells |= {(x, y) for x in range(0, 12) for y in range(10, 34)}
        cells -= {(x, y) for x in range(4, 8) for y in range(16, 20)}
        grid = patch_grid(snapshot(cells), make_policy("w", "c", "coal"))
        self.assertTrue(grid["ok"], grid)
        self.assertGreaterEqual(len(grid["rows"]), 4)
        self.assertEqual(grid["covered_tiles"] + grid["unreachable_tiles"], len(cells))
        self.assertLess(grid["unreachable_tiles"], len(cells) * 0.05)
        # Every collector reaches the same patch edge (EAST flow: one port column).
        self.assertEqual(len({row["port"]["x"] for row in grid["rows"]}), 1)
        # Rows are pairs of the paired-bank shape, BANK_PITCH apart, columns PAIR_PITCH apart.
        tops = sorted(row["anchor"]["y"] for row in grid["rows"])
        self.assertTrue(all((b - a) % BANK_PITCH == 0 for a, b in zip(tops, tops[1:])))
        self.assertTrue(all((row["anchor"]["x"] - grid["origin"]["x"]) % PAIR_PITCH == 0 for row in grid["rows"]))
        self.assertGreater(grid["rows"][0]["columns"], grid["rows"][-1]["columns"])  # long and short rows

    def test_the_shift_search_picks_the_better_offset(self):
        cells = [(x, y) for x in range(3, 16) for y in range(5, 14)]
        policy = make_policy("w", "c", "coal")
        best = patch_grid(snapshot(cells), policy)
        base = patch_grid(snapshot(cells), policy, origin=policy["grid_origin"])
        self.assertEqual(best["shifts_scored"], PAIR_PITCH * BANK_PITCH)
        self.assertEqual(base["shifts_scored"], 1)
        self.assertEqual(best["covered_tiles"], base["covered_tiles"])
        self.assertLess(best["drills"], base["drills"])  # the same ore with fewer drills
        self.assertNotEqual(best["origin"], policy["grid_origin"])
        for dx in range(PAIR_PITCH):
            for dy in range(BANK_PITCH):
                other = patch_grid(snapshot(cells), policy, origin={"x": 0.5 + dx, "y": 0.5 + dy})
                self.assertLessEqual(other["score"], best["score"])

    def test_water_cliffs_and_other_ore_are_never_under_a_drill(self):
        cells = [(x, y) for x in range(0, 24) for y in range(0, 12)]
        water = {(x + 0.5, y + 0.5) for x in range(8, 12) for y in range(0, 12)}
        grid = patch_grid(snapshot(cells), make_policy("w", "c", "coal"), forbidden=sorted(water))
        drills = [d for row in grid["rows"] for d in row["drills"]]
        self.assertTrue(drills)
        self.assertFalse(any(footprint(d) & water for d in drills))
        mixed = patch_grid(snapshot(cells, other=[(30, 30)]), make_policy("w", "c", "coal"))
        self.assertTrue(mixed["ok"])

    def test_the_staged_start_is_a_row_pair_at_the_output_edge_with_its_row_reserved(self):
        cells = [(x, y) for x in range(0, 40) for y in range(0, 8)]
        s = snapshot(cells)
        grid = patch_grid(s, make_policy("w", "c", "coal"))
        policy = make_policy("w", "c", "coal", grid_origin=grid["origin"])
        sites = candidate_sites(s, policy, 1, [], grid=grid)
        self.assertTrue(sites)
        anchors = {(row["anchor"]["x"], row["anchor"]["y"]) for row in grid["rows"]}
        for site in sites:
            self.assertIn((site["x"], site["y"]), anchors)  # the edge-most columns of a grid row
        self.assertGreater(max(site["reserved_pair_slots"] for site in sites), 3)  # the row is reserved, not +2
        plain = candidate_sites(s, make_policy("w", "c", "coal"), 1, [])
        self.assertLessEqual(max(site["reserved_pair_slots"] for site in plain), 3)

    def test_a_thin_edge_column_moves_the_start_inward_to_the_first_full_pair(self):
        # Fleet all-1113-2155: every bank fell back to the lattice, the row's edge-most column was
        # too thin for a full pair. Here a 6 x 4 tongue at the output edge holds one drill only.
        cells = {(x, y) for x in range(0, 40) for y in range(0, 14)}
        cells |= {(x, y) for x in range(40, 46) for y in range(0, 4)}
        s = snapshot(cells)
        grid = patch_grid(s, make_policy("w", "c", "coal"))
        policy = make_policy("w", "c", "coal", grid_origin=grid["origin"])
        thin = grid["rows"][0]
        self.assertNotEqual(thin["anchor"], thin["pair_anchors"][0])  # the edge column is no full pair
        edge_only = dict(grid, rows=[{k: v for k, v in r.items() if k != "pair_anchors"} for r in grid["rows"]])
        self.assertFalse(
            [q for q in candidate_sites(s, policy, 1, [], grid=edge_only) if (q["x"], q["y"]) == (45.5, 1.5)]
        )
        sites = candidate_sites(s, policy, 1, [], grid=grid)
        self.assertEqual(len(sites), len(grid["rows"]))  # one start per row
        starts = {(q["x"], q["y"]) for q in sites}
        self.assertIn((thin["pair_anchors"][0]["x"], thin["pair_anchors"][0]["y"]), starts)
        self.assertTrue(all(q["reserved_pair_slots"] > 3 for q in sites))  # the row's growth is still reserved

    def test_a_reserved_output_edge_walks_the_row_inward(self):
        # A starter cell on the edge columns (the usual reservation at a patch edge near the base).
        cells = [(x, y) for x in range(0, 40) for y in range(0, 8)]
        s = snapshot(cells)
        grid = patch_grid(s, make_policy("w", "c", "coal"))
        policy = make_policy("w", "c", "coal", grid_origin=grid["origin"])
        edge = max(a["x"] for row in grid["rows"] for a in row["pair_anchors"])
        starter = [[[edge - 3, -10], [edge + 4, 20]]]  # covers the edge-most pair column
        sites = candidate_sites(s, policy, 1, starter, grid=grid)
        self.assertTrue(sites)
        self.assertTrue(all(q["x"] <= edge - PAIR_PITCH * 2 for q in sites))
        self.assertTrue(all(q["inward_columns"] >= 1 for q in sites))


class RecordedPatches(unittest.TestCase):
    def test_every_recorded_patch_and_flow_gets_a_grid_row(self):
        # Seed 2150100594's three patches with the burner hardware between them as reservations:
        # before, two of the twelve (patch, flow) cases had no grid row whose edge column fitted.
        from test_v2_mining_site_exit import LIVE_ENTITIES, live_world, patch_seed, snapshot as live_snapshot

        world = live_world()
        burner = []
        for item in LIVE_ENTITIES.split():
            x0, y0, x1, y1 = map(int, item.split(":"))
            burner.append([[x0, y0], [x1, y1]])
        for resource in ("coal", "copper-ore", "iron-ore"):
            seed = patch_seed(world, resource)
            area = [[int(seed[k] // 1) - 48 for k in ("x", "y")], [int(seed[k] // 1) + 48 for k in ("x", "y")]]
            s = live_snapshot(world, resource, area)
            for flow in ("west_to_east", "east_to_west", "north_to_south", "south_to_north"):
                grid = patch_grid(s, make_policy("w", "c", resource, flow=flow, site_oriented=True), burner)
                policy = make_policy("w", "c", resource, flow=flow, grid_origin=grid["origin"], site_oriented=True)
                with self.subTest(resource=resource, flow=flow):
                    self.assertTrue(candidate_sites(s, policy, 1, burner, grid=grid))


class PatchGridOnNewBank(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(temp.name)), query=Mock())
        geometry = FactoryBuilder.__new__(FactoryBuilder)
        catalog = SimpleNamespace(
            fingerprint="c",
            entities={"coal": {"mining_products": [{"name": "coal", "amount": 1, "type": "item"}]}},
        )
        self.builder = SimpleNamespace(catalog=catalog, _occupied_by_plan=geometry._occupied_by_plan)
        self.obs = {
            "world_id": "w",
            "tick": 100,
            "entities": [],
            "resources": {"coal": {"position": {"x": 10.5, "y": 6.5}}},
        }
        cells = [(x, y) for x in range(-4, 30) for y in range(0, 14)]
        self.snapshot = snapshot(cells)

    def prepare(self, reservations=(), **options):
        def outlet(game, builder, obs, core, future, reservations):
            p = deepcopy(core["ports"][0])
            p["position"]["x"] += 4
            p.pop("mining_resource", None)
            path = [{"x": core["ports"][0]["position"]["x"] + i, "y": p["position"]["y"]} for i in range(5)]
            es = core["entities"] + [entity("transport-belt", q["x"], q["y"], 4) for q in path[1:]]
            access = [entity("transport-belt", p["position"]["x"] + i, p["position"]["y"], 4) for i in range(1, 4)]
            return dict(ok=True, entities=es, port=p, collector_port=core["ports"][0], path=path, output_access=access)

        with (
            patch("factorio_agent.mining_layout_policy.survey_patch", return_value=self.snapshot),
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
            return prepare_new_bank(self.game, self.builder, self.obs, "coal", 2, list(reservations), **options)

    def test_a_new_bank_is_one_row_pair_of_the_recorded_grid(self):
        bank = self.prepare()
        self.assertTrue(bank["ok"], bank)
        grid = bank["patch_grid"]
        self.assertEqual(bank["layout_contract"]["policy"]["grid_origin"], grid["origin"])
        port = bank["plan"]["collector_port"]["position"]
        self.assertIn(
            (port["x"] - 2, port["y"] - 2), {(row["anchor"]["x"], row["anchor"]["y"]) for row in grid["rows"]}
        )
        self.assertIsNone(grid["fallback"])

    def test_a_thin_output_edge_still_gives_a_grid_bank(self):
        cells = {(x, y) for x in range(0, 40) for y in range(0, 14)}
        cells |= {(x, y) for x in range(40, 46) for y in range(0, 4)}
        self.snapshot = snapshot(cells)
        bank = self.prepare()
        self.assertTrue(bank["ok"], bank)
        grid = bank["patch_grid"]
        self.assertIsNone(grid["fallback"])
        row = grid["rows"][grid["row"]]
        port = bank["plan"]["collector_port"]["position"]
        self.assertEqual((port["x"] - 2, port["y"] - 2), (row["start"]["x"], row["start"]["y"]))
        self.assertTrue(all(r["full_pairs"] >= 1 for r in grid["rows"]))

    def test_relaxation_takes_a_grid_row_before_the_lattice(self):
        # The west part is another block's reservation: the one pair column left has no growth pair,
        # so the first pass finds nothing (tests/test_v2_mining_patch_choice.py, on the lattice).
        self.snapshot = snapshot([(x, y) for x in range(0, 12) for y in range(0, 16)], amount=300)
        bank = self.prepare([[[-20, -20], [8, 40]]])
        self.assertTrue(bank["ok"], bank)
        self.assertEqual(bank["patch_diagnostic"]["relaxed"], "no_growth_pair")
        self.assertIsNone(bank["patch_grid"]["fallback"])
        self.assertEqual(bank["layout_contract"]["policy"]["grid_origin"], bank["patch_grid"]["origin"])

    def test_a_later_bank_keeps_the_patch_lattice(self):
        first = self.prepare()
        origin = first["layout_contract"]["policy"]["grid_origin"]
        later = self.prepare(grid_origin={"origin": origin, "flow": "west_to_east"})
        self.assertEqual(later["patch_grid"]["shifts_scored"], 1)
        self.assertEqual(later["patch_grid"]["origin"], origin)


if __name__ == "__main__":
    unittest.main()
