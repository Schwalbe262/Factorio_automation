"""Fourth-family geometry and the opt-in pre-expansion straight-prefix gate."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from factorio_agent.routing import (
    VECTORS,
    clear_skeleton,
    expand,
    forward_jog_skeletons,
    obstacle_index,
    route_trunks,
)


def forced_forward_case(transform=lambda p: p, escape=5):
    vertices = [
        (0.5, 0.5),
        (escape + 0.5, 0.5),
        (escape + 0.5, -2.5),
        (96.5, -2.5),
        (96.5, -47.5),
        (68.5, -47.5),
        (68.5, -80.5),
        (69.5, -80.5),
    ]
    corridor = set(expand(vertices))
    xs = [i + 0.5 for i in range(-4, 101)]
    ys = [i + 0.5 for i in range(-85, 5)]
    hard = {transform((x, y)) for x in xs for y in ys if (x, y) not in corridor}
    corners = [transform((x, y)) for x in (xs[0], xs[-1]) for y in (ys[0], ys[-1])]
    bounds = {
        "min_x": min(p[0] for p in corners),
        "max_x": max(p[0] for p in corners),
        "min_y": min(p[1] for p in corners),
        "max_y": max(p[1] for p in corners),
    }
    a, b = transform(vertices[0]), transform(vertices[-1])
    next_ = transform((1.5, 0.5))
    heading = next(d for d, v in VECTORS.items() if v == (next_[0] - a[0], next_[1] - a[1]))
    return a, b, hard, bounds, heading, [transform(p) for p in vertices]


def captured_water_case(capture_path, runtime_dir, vertical_shift=-28):
    """Manual benchmark helper only; unittest does not depend on ignored runtime files."""
    from factorio_agent.core.deterministic_builder import FactoryBuilder
    from factorio_agent.core.world_catalog import WorldCatalog
    from factorio_agent.chemical_production import chemical_core
    from factorio_agent.petroleum import compile_pipe_crossings
    from factorio_agent.transport import unbuilt_external_keepouts, xy

    d = json.loads(Path(capture_path).read_text())
    root = Path(runtime_dir)
    catalog = WorldCatalog.from_dict(json.loads((root / "catalog.json").read_text()))
    board = json.loads((root / "pcb-board.json").read_text())
    builder = FactoryBuilder.__new__(FactoryBuilder)
    machine = d["core"]["machine"]["position"]
    core = chemical_core(catalog, "sulfur", machine["x"], machine["y"] + vertical_shift)
    a = xy(d["source"])
    b = xy(core["fluid_ports"]["water"])
    area = core["reserved_area"]
    hard = {(q["x"], q["y"]) for q in d["survey"]["hard"]}
    hard |= unbuilt_external_keepouts(builder, {"entities": d["observed_assets"]}, board["blocks"])
    hard |= builder._occupied_by_plan(core["entities"] + core["output_access"])
    cells = {
        (area[0][0] + 0.5 + i, area[0][1] + 0.5 + j)
        for i in range(int(area[1][0] - area[0][0]))
        for j in range(int(area[1][1] - area[0][1]))
    }
    hard |= {q for q in cells if not (q[1] == b[1] and q[0] <= b[0])}
    for e in core["fluid_groups"]["petroleum-gas"]:
        p = xy(e)
        hard.add(p)
        hard.update((p[0] + dx, p[1] + dy) for dx, dy in VECTORS.values())
    hard -= {a, b}
    soft = {(p["x"], p["y"]) for p in d["survey"]["soft"]} - hard
    apron = d["survey_args"]["apron_path"]
    facing = d["source"]["facing"]

    def layers(row):
        if [s["position"] for s in row["segments"][: len(apron)]] != apron:
            return False
        if any(s["direction"] != facing for s in row["segments"][: len(apron) - 1]):
            return False
        compiled = compile_pipe_crossings(row, soft, 10)
        return compiled.get("ok") and all(
            any(e["name"] == "pipe" and xy(e) == xy({"position": p}) for e in compiled["entities"]) for p in apron[:2]
        )

    return {
        "start": a,
        "end": b,
        "occupied": hard,
        "bounds": d["survey_args"]["bounds"],
        "start_direction": facing,
        "end_direction": core["fluid_ports"]["water"]["facing"],
        "max_nodes": 200000,
        "max_turns": 6,
        "max_detour_ratio": 1.35,
        "minimum_start_run": 5,
        "candidate_validator": layers,
    }


class ForwardJogTests(unittest.TestCase):
    def test_fourth_family_finds_whole_forward_trunk_and_preserves_all_quality_limits(self):
        a, b, hard, bounds, direction, vertices = forced_forward_case()
        result = route_trunks(
            a,
            b,
            occupied=hard,
            bounds=bounds,
            start_direction=direction,
            end_direction=direction,
            minimum_start_run=5,
            max_nodes=200000,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["metrics"], {"length": 206, "turns": 6, "manhattan_distance": 150, "detour_ratio": 206 / 150}
        )
        self.assertLessEqual(result["visited"], 200000)
        self.assertEqual([(p["x"], p["y"]) for p in result["path"]], expand(vertices))

    def test_old_three_families_fail_fixture_and_remain_preferred_when_accepted(self):
        a, b, hard, bounds, direction, _ = forced_forward_case()
        with patch("factorio_agent.routing.forward_jog_skeletons", return_value=[]):
            old = route_trunks(
                a,
                b,
                occupied=hard,
                bounds=bounds,
                start_direction=direction,
                end_direction=direction,
                minimum_start_run=5,
                max_nodes=200000,
            )
        self.assertFalse(old["ok"])
        self.assertEqual(old["visited"], 0)
        with patch(
            "factorio_agent.routing.forward_jog_skeletons", side_effect=AssertionError("fourth family must remain lazy")
        ):
            ordinary = route_trunks((0.5, 0.5), (12.5, 0.5), start_direction=4, end_direction=4)
            explicit = route_trunks((0.5, 0.5), (12.5, 0.5), start_direction=4, end_direction=4, minimum_start_run=None)
        self.assertEqual(ordinary, explicit)
        self.assertTrue(ordinary["ok"])

    def test_rotation_and_mirror_covariance(self):
        for mirror in (-1, 1):
            for turns in range(4):

                def transform(p):
                    x, y = (p[0] - 0.5) * mirror, p[1] - 0.5
                    for _ in range(turns):
                        x, y = -y, x
                    return x + 0.5, y + 0.5

                a, b, hard, bounds, direction, vertices = forced_forward_case(transform)
                result = route_trunks(
                    a,
                    b,
                    occupied=hard,
                    bounds=bounds,
                    start_direction=direction,
                    end_direction=direction,
                    minimum_start_run=5,
                    max_nodes=200000,
                )
                self.assertTrue(result["ok"], (mirror, turns, result))
                self.assertEqual([(p["x"], p["y"]) for p in result["path"]], expand(vertices))

    def test_length_turn_bounds_layer_and_shared_expansion_budget_are_not_relaxed(self):
        a, b, hard, bounds, direction, _ = forced_forward_case()
        base = dict(
            occupied=hard,
            bounds=bounds,
            start_direction=direction,
            end_direction=direction,
            minimum_start_run=5,
            max_nodes=200000,
        )
        for extra in (
            {"max_turns": 5},
            {"max_detour_ratio": 1.0},
            {"candidate_validator": lambda row: False},
            {"max_nodes": 206},
            {"minimum_start_run": 6},
        ):
            result = route_trunks(a, b, **{**base, **extra})
            self.assertFalse(result["ok"], extra)
            self.assertLessEqual(result["visited"], extra.get("max_nodes", 200000))
        narrow = {**bounds, "max_x": bounds["max_x"] - 6}
        self.assertFalse(route_trunks(a, b, **{**base, "bounds": narrow})["ok"])

    def test_strict_optional_prefix_validation_and_zero_length(self):
        for value in (0, -1, 9, True, False, 1.0, "5"):
            result = route_trunks((0, 0), (10, 0), start_direction=4, end_direction=4, minimum_start_run=value)
            self.assertFalse(result["ok"])
            self.assertEqual(result["visited"], 0)
        self.assertFalse(route_trunks((0, 0), (10, 0), minimum_start_run=1)["ok"])
        self.assertFalse(route_trunks((0, 0), (0, 0), start_direction=4, minimum_start_run=1)["ok"])
        self.assertTrue(route_trunks((0, 0), (10, 0), start_direction=4, end_direction=4, minimum_start_run=8)["ok"])

    def test_invalid_prefixes_are_not_expanded_in_any_family(self):
        short = [(0, 0), (1, 0), (1, 2), (9, 2), (9, 0), (10, 0)]
        for minimum in (2, 5):
            with patch(
                "factorio_agent.routing.expand", side_effect=AssertionError("prefix must be rejected before expansion")
            ):
                with (
                    patch("factorio_agent.routing.return_jog_skeletons", return_value=[short]),
                    patch("factorio_agent.routing.forward_jog_skeletons", return_value=[short]),
                ):
                    result = route_trunks(
                        (0, 0),
                        (10, 0),
                        occupied=[(2, 0)],
                        bounds={"min_x": 0, "max_x": 10, "min_y": 0, "max_y": 2},
                        start_direction=4,
                        end_direction=4,
                        minimum_start_run=minimum,
                        max_nodes=200000,
                    )
            self.assertFalse(result["ok"])
            self.assertEqual(result["visited"], 0)

    def test_six_cell_source_escape_requires_explicit_native_prefix_requirement(self):
        a, b, hard, bounds, direction, vertices = forced_forward_case(escape=6)
        args = dict(occupied=hard, bounds=bounds, start_direction=direction, end_direction=direction, max_nodes=200000)
        self.assertFalse(route_trunks(a, b, **args)["ok"])
        result = route_trunks(a, b, **args, minimum_start_run=6)
        self.assertTrue(result["ok"], result)
        self.assertEqual([(p["x"], p["y"]) for p in result["path"]], expand(vertices))

    def test_clear_crossbar_is_required_before_limiting_sixteen_end_columns(self):
        rows, columns = obstacle_index({(30, 10)})
        candidates = forward_jog_skeletons(
            (0, 0),
            (50, 20),
            {"min_x": 0, "max_x": 60, "min_y": -5, "max_y": 25},
            4,
            4,
            rows,
            columns,
            [[20], [10]],
            [set(), set()],
            6,
            1000,
            5,
        )
        self.assertTrue(candidates)
        self.assertTrue(any(v[-2] == (29, 20) for v in candidates))
        self.assertTrue(
            all(
                clear_skeleton(v, rows, columns, {"min_x": 0, "max_x": 60, "min_y": -5, "max_y": 25}, 6)
                for v in candidates
            )
        )

    def test_middle_columns_are_not_limited_before_whole_vertical_clearance(self):
        rows, columns = obstacle_index({(x, 5) for x in range(6, 41)})
        candidates = forward_jog_skeletons(
            (0, 0),
            (20, 20),
            {"min_x": 0, "max_x": 105, "min_y": -5, "max_y": 25},
            4,
            4,
            rows,
            columns,
            [list(range(6, 41)) + [100], [10]],
            [set(), set()],
            6,
            1000,
            5,
        )
        self.assertTrue(candidates)
        self.assertTrue(all(any(p[0] == 100 for p in v) for v in candidates))


if __name__ == "__main__":
    unittest.main()
