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


def snapshot(cells, resource="coal", other=()):
    tiles = [{"name": resource, "position": {"x": x + 0.5, "y": y + 0.5}, "amount": 500} for x, y in cells]
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

    def prepare(self, **options):
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
            return prepare_new_bank(self.game, self.builder, self.obs, "coal", 2, [], **options)

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

    def test_a_later_bank_keeps_the_patch_lattice(self):
        first = self.prepare()
        origin = first["layout_contract"]["policy"]["grid_origin"]
        later = self.prepare(grid_origin={"origin": origin, "flow": "west_to_east"})
        self.assertEqual(later["patch_grid"]["shifts_scored"], 1)
        self.assertEqual(later["patch_grid"]["origin"], origin)


if __name__ == "__main__":
    unittest.main()
