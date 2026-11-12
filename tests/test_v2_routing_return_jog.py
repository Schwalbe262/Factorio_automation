import unittest
from unittest.mock import patch

from factorio_agent.board import entity
from factorio_agent import routing
from factorio_agent.transport import compile_crossings


def fixture(transform=lambda p: p):
    start, end = (0.5, 0.5), (60.5, -2.5)
    hard = {(x + 0.5, y + 0.5) for x in range(4, 31) for y in range(-9, 19)}
    hard |= {(x + 0.5, y + 0.5) for x in range(44, 71) for y in range(-22, -5)}
    foreign = []
    for x in (1, 2):
        for y in range(-10, 1):
            if y in (-4, -6):
                hard.add((x + 0.5, y + 0.5))
                foreign.append(
                    dict(
                        entity("underground-belt", x + 0.5, y + 0.5, 0),
                        belt_to_ground_type="input" if y == -4 else "output",
                    )
                )
            elif x == 2 or y <= -2:
                foreign.append(entity("transport-belt", x + 0.5, y + 0.5, 0))
    foreign += [entity("transport-belt", x + 0.5, y, 4) for y in (-2.5, -5.5) for x in range(-6, 31) if x not in (1, 2)]

    def direction(d):
        vector = transform(routing.VECTORS[d])
        origin = transform((0, 0))
        return next(k for k, v in routing.VECTORS.items() if v == (vector[0] - origin[0], vector[1] - origin[1]))

    transformed = []
    for e in foreign:
        p = transform(tuple(e["position"].values()))
        transformed.append(dict(e, position=dict(zip(("x", "y"), p)), direction=direction(e["direction"])))
    corners = [transform(p) for p in ((-15.5, -25.5), (-15.5, 20.5), (76.5, -25.5), (76.5, 20.5))]
    bounds = {
        k: fn(p[i] for p in corners)
        for k, fn, i in (("min_x", min, 0), ("max_x", max, 0), ("min_y", min, 1), ("max_y", max, 1))
    }
    return (
        transform(start),
        transform(end),
        dict(
            occupied={transform(p) for p in hard},
            bounds=bounds,
            start_direction=direction(4),
            end_direction=direction(4),
            max_nodes=150000,
            candidate_validator=lambda c: compile_crossings(c, transformed)["ok"],
        ),
    )


class ReturnJogRoutingTests(unittest.TestCase):
    def test_whole_return_jog_routes_rotations_and_mirror_without_changing_layer_rules(self):
        transforms = (
            lambda p: p,
            lambda p: (-p[1], p[0]),
            lambda p: (-p[0], -p[1]),
            lambda p: (p[1], -p[0]),
            lambda p: (-p[0], p[1]),
        )
        for transform in transforms:
            with self.subTest(transform=transform):
                a, b, kwargs = fixture(transform)
                expanded = []
                original = routing.expand

                def track(vertices):
                    path = original(vertices)
                    self.assertFalse(set(path) & kwargs["occupied"])
                    expanded.append(len(path))
                    return path

                with patch.object(routing, "expand", side_effect=track):
                    result = routing.route_trunks(a, b, **kwargs)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["metrics"]["turns"], 6)
                self.assertEqual(result["metrics"]["length"], 81)
                self.assertLessEqual(result["metrics"]["length"], 1.35 * result["metrics"]["manhattan_distance"] + 4)
                self.assertEqual(result["visited"], sum(expanded))
                self.assertLessEqual(sum(expanded), kwargs["max_nodes"])
                self.assertEqual(result["segments"][0]["direction"], kwargs["start_direction"])
                self.assertEqual(result["segments"][-1]["direction"], kwargs["end_direction"])
                self.assertTrue(kwargs["candidate_validator"](result))

    def test_existing_families_cannot_generate_the_required_source_return(self):
        a, b, kwargs = fixture()
        with (
            patch.object(routing, "return_jog_skeletons", return_value=[]),
            patch.object(routing, "forward_jog_skeletons", return_value=[]),
        ):
            old = routing.route_trunks(a, b, **kwargs)
        self.assertFalse(old["ok"])
        self.assertEqual(old["visited"], 0)

    def test_first_step_turn_detour_and_budget_constraints_still_fail_closed(self):
        a, b, kwargs = fixture()
        self.assertFalse(routing.route_trunks(a, b, **kwargs, max_turns=5)["ok"])
        self.assertFalse(routing.route_trunks(a, b, **kwargs, max_detour_ratio=1.1)["ok"])
        kwargs["occupied"].add((1.5, 0.5))
        result = routing.route_trunks(a, b, **kwargs)
        self.assertFalse(result["ok"])
        self.assertEqual(result["visited"], 0)

    def test_old_family_expansions_are_not_reset_for_the_new_family(self):
        expanded = []
        entered = []
        generated = []
        original_expand = routing.expand
        original_jog = routing.return_jog_skeletons

        def track(vertices):
            path = original_expand(vertices)
            expanded.append(len(path))
            return path

        def jog(*args):
            entered.append(sum(expanded))
            candidates = original_jog(*args)
            generated.append(len(candidates))
            return candidates

        with (
            patch.object(routing, "expand", side_effect=track),
            patch.object(routing, "return_jog_skeletons", side_effect=jog),
        ):
            result = routing.route_trunks(
                (0.5, 0.5),
                (64.5, 0.5),
                bounds={"min_x": -4.5, "max_x": 68.5, "min_y": -9.5, "max_y": 9.5},
                start_direction=4,
                end_direction=4,
                max_nodes=3000,
                candidate_validator=lambda c: bool(entered),
            )
        self.assertFalse(result["ok"])
        self.assertTrue(entered)
        self.assertGreater(entered[0], 0)
        self.assertGreater(generated[0], 0)
        self.assertEqual(sum(expanded), result["visited"])
        self.assertLessEqual(sum(expanded), 3000)

    def test_accepted_legacy_route_never_invokes_the_additional_family(self):
        kwargs = dict(occupied={(20.5, 0.5)}, start_direction=4, end_direction=4, max_nodes=25000)
        with patch.object(routing, "return_jog_skeletons", return_value=[]) as helper:
            baseline = routing.route_trunks((0.5, 0.5), (60.5, 0.5), **kwargs)
            helper.assert_not_called()
        with patch.object(routing, "return_jog_skeletons", side_effect=AssertionError("legacy route changed")):
            actual = routing.route_trunks((0.5, 0.5), (60.5, 0.5), **kwargs)
        self.assertTrue(actual["ok"])
        self.assertEqual(actual["path"], baseline["path"])


if __name__ == "__main__":
    unittest.main()
