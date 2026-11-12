"""Next steam bank geometry derived from the first plant's saved plan."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factorio_agent.board import entity
from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.core.factory_templates import DIRECTIONS, build_template, coal_entry_detour, route_orthogonal
from factorio_agent.site.geometry import Rect
from factorio_agent.steam_layout import (
    adjacent_layouts,
    canonical_point,
    fluid_conflicts,
    overlaps,
    plant_frame,
    rank_variants,
    tiles,
    water_connected,
    world_point,
)
from factorio_agent.steam_siting import candidate_layouts

ANCHOR = {"x": 10.5, "y": 20.5}


def plant(rotation, entry="south", inlet="south"):
    """A first plant as ``_choose_power_plan`` saves it, with its water inlet line.

    ``inlet`` is the canonical side the pump line arrives from: "south" leaves
    both row ends free, "west" runs along the row and blocks the western end.
    """
    bank = build_template("steam_bank", anchor=ANCHOR, rotation=rotation, coal_entry=entry)
    frame = {"rotation": rotation, "anchor": ANCHOR}
    if inlet == "south":
        line = [(-4, y) for y in range(2, 7)]
    else:
        line = [(x, 1) for x in range(-13, -4)]
    pipes = [entity("pipe", *world_point(frame, x, y)) for x, y in line]
    return frame, _plan(bank["entities"] + pipes, bank["ports"], template="starter_power")


def feed_from_east(frame):
    """The first coal feed running along the stub row from the canonical east."""
    direction = (12 + frame["rotation"]) % 16
    belts = [entity("transport-belt", *world_point(frame, x, 3), direction) for x in range(5, 21)]
    return _plan(belts, [], template="dedicated_coal_feed")


def boiler(rows):
    return next(e for e in rows if e["name"] == "boiler")


class FrameTests(unittest.TestCase):
    def test_frame_recovers_rotation_and_anchor_for_every_rotation(self):
        for rotation in DIRECTIONS:
            frame, power = plant(rotation)
            found = plant_frame(power)
            self.assertEqual(found["rotation"], rotation)
            self.assertEqual(found["anchor"], ANCHOR)
            self.assertEqual(found["indices"], [0])

    def test_saved_live_map_plant_matches_the_template_frame(self):
        # factory-builder.json of eval-1000868986: boiler west-facing at (57.0, 23.5).
        power = {"entities": [entity("boiler", 57.0, 23.5, 12)]}
        found = plant_frame(power)
        self.assertEqual(found["anchor"], {"x": 56.5, "y": 23.5})
        self.assertEqual(world_point(found, -4, 1), (57.5, 27.5))

    def test_off_grid_boiler_is_not_a_template_plant(self):
        power = {"entities": [entity("boiler", 1.5, 1, 0), entity("boiler", 6.5, 1, 0)]}
        self.assertIsNone(plant_frame(power))

    def test_canonical_point_inverts_world_point(self):
        frame = {"rotation": 4, "anchor": ANCHOR}
        self.assertEqual(canonical_point(frame, *world_point(frame, 8, 0.5)), (8, 0.5))


class AdjacentBankTests(unittest.TestCase):
    def test_next_bank_on_both_sides_for_every_rotation(self):
        for rotation in DIRECTIONS:
            frame, power = plant(rotation)
            layouts = {
                layout["side"]: layout for layout in adjacent_layouts(plant_frame(power), power, power["entities"])
            }
            self.assertEqual(set(layouts), {1, -1}, rotation)
            old = boiler(power["entities"])
            for side, layout in layouts.items():
                self.assertEqual(layout["index"], side)
                for found in layout["variants"]:
                    new = found["boiler"]
                    self.assertEqual(new["direction"], rotation)
                    self.assertEqual((new["position"]["x"], new["position"]["y"]), world_point(frame, 8 * side, 0.5))
                    self.assertFalse(overlaps(found["entities"], power["entities"]))
                    self.assertFalse(tiles(found["entities"]) & tiles(power["entities"]))
                    self.assertTrue(water_connected(power["entities"] + found["entities"], old, new))
                    self.assertEqual(fluid_conflicts(found["entities"], power["entities"]), [])
            # The western end's east stub would cross the inlet line.
            self.assertNotIn("east", [v["entry"] for v in layouts[-1]["variants"]])
            self.assertEqual([v["entry"] for v in layouts[1]["variants"]], ["west", "east", "south"])

    def test_bridge_is_the_two_pipes_between_the_boilers(self):
        frame, power = plant(8)
        layout = next(x for x in adjacent_layouts(plant_frame(power), power, power["entities"]) if x["side"] == 1)
        pipes = {
            (e["position"]["x"], e["position"]["y"]) for e in layout["variants"][0]["entities"] if e["name"] == "pipe"
        }
        self.assertLessEqual({world_point(frame, 2, 1), world_point(frame, 3, 1)}, pipes)

    def test_without_the_bridge_the_audit_finds_no_shared_water(self):
        _, power = plant(0)
        layout = adjacent_layouts(plant_frame(power), power, power["entities"])[0]
        found = layout["variants"][0]
        cut = [e for e in found["entities"] if e["name"] != "pipe"]
        self.assertFalse(water_connected(power["entities"] + cut, boiler(power["entities"]), found["boiler"]))

    def test_coal_stub_faces_the_source(self):
        for rotation in DIRECTIONS:
            frame, power = plant(rotation)
            layouts = {x["side"]: x for x in adjacent_layouts(plant_frame(power), power, power["entities"])}
            cases = [(1, (40, 3), "east"), (1, (8, 40), "south"), (-1, (-40, 3), "west"), (-1, (-8, 40), "south")]
            for side, (cx, cy), wanted in cases:
                x, y = world_point(frame, cx, cy)
                origin = {"x": x, "y": y}
                best = rank_variants(layouts[side]["variants"], origin)[0]
                self.assertEqual(best["entry"], wanted, (rotation, side, wanted))
                port = best["coal_port"]
                manhattan = abs(port["position"]["x"] - x) + abs(port["position"]["y"] - y)
                self.assertEqual(coal_entry_detour(port, origin), manhattan)

    def test_coal_feed_on_one_end_leaves_the_other_end(self):
        frame, power = plant(4, entry="east")
        feed = feed_from_east(frame)
        layouts = adjacent_layouts(plant_frame(power), power, power["entities"] + feed["entities"])
        self.assertEqual([x["side"] for x in layouts], [-1])

    def test_pipe_touching_an_engine_is_a_fluid_conflict(self):
        frame, power = plant(0)
        engine = next(e for e in power["entities"] if e["name"] == "steam-engine")
        x, y = engine["position"]["x"] + 2, engine["position"]["y"]
        self.assertTrue(fluid_conflicts([entity("pipe", x, y)], power["entities"]))


def fake_route(source, destination, name, reserved, margin=12):
    occupied = FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan(reserved)
    occupied -= {(source["x"], source["y"]), (destination["x"], destination["y"])}
    bounds = {
        "min_x": min(source["x"], destination["x"]) - margin,
        "max_x": max(source["x"], destination["x"]) + margin,
        "min_y": min(source["y"], destination["y"]) - margin,
        "max_y": max(source["y"], destination["y"]) + margin,
    }
    return route_orthogonal(source, destination, occupied=occupied, bounds=bounds)


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.frame, self.power = plant(0, entry="east", inlet="west")
        self.feed = feed_from_east(self.frame)
        self.builder = SimpleNamespace(state={"power_plan": self.power, "coal_plan": self.feed}, route=fake_route)
        self.obs = {"world_id": "w", "entities": []}

    def layouts(self, site, rects):
        board = {"world_id": "w", "blocks": {}}
        with (
            patch("factorio_agent.steam_siting.preflight", return_value={"ok": True, "clear_obstacles": []}),
            patch("factorio_agent.steam_siting.free_sites", return_value=rects) as sites,
        ):
            found = list(candidate_layouts(None, self.builder, self.obs, site, board))
        return found, sites

    def test_both_ends_blocked_uses_a_free_site_with_its_own_water_run(self):
        site = SimpleNamespace(world=lambda world_id: object())
        found, sites = self.layouts(site, [Rect(40, 10, 49, 28)])
        self.assertEqual(len(found), 1)
        layout = found[0]
        self.assertEqual(layout["kind"], "degraded_free_site")
        self.assertEqual(sites.call_args.args[2:], (9, 18))
        old = boiler(self.power["entities"])
        known = self.power["entities"] + self.feed["entities"]
        for found in layout["variants"]:
            self.assertEqual(found["boiler"]["direction"], 0)
            self.assertFalse(overlaps(found["entities"], known))
            self.assertTrue(water_connected(known + found["entities"], old, found["boiler"]))
        self.assertLessEqual(layout["water_run_tiles"], 32)

    def test_without_a_site_service_nothing_is_offered(self):
        found, _ = self.layouts(None, [])
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
