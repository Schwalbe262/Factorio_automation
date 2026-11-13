"""Green science plans from the site plan on fresh maps in every orientation."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from site_transforms import transform_world
from test_v2_site_service import FakeGame

from factorio_agent.compact_green import _recipe_catalog
from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.green_science import GREEN_BLOCK, LogisticScience, planned_green_cores
from factorio_agent.manufacturing import GREEN_CORE_DEPTH, lab_green_entry_outside
from factorio_agent.science import lab_row, planned_science_candidates
from factorio_agent.site.fakeworld import make_world
from factorio_agent.site.placement import settle
from factorio_agent.site.planmodel import SiteConfig
from factorio_agent.site.service import SiteService
from factorio_agent.site.transform import Transform, apply, in_canonical, plan_transform, variants
from factorio_agent.smelting import merge_entities
from factorio_agent.smelting_site import plan_candidates
from factorio_agent.transport import plan_connection, xy

WORLD = "w1"
SEED = 3
COUNTS = {"iron-ore": 4, "copper-ore": 2}
KINDS = (None, "flip_x", "flip_y", "rotate")
GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)
BUILDER = SimpleNamespace(_occupied_by_plan=GEOMETRY._occupied_by_plan, can_place=lambda entities: {"ok": True})
CLEAR = {"ok": True, "blocked": [], "max_span": 5}


def catalog():
    base = _recipe_catalog()
    recipes = dict(base.recipes)
    recipes["automation-science-pack"] = {
        "energy": 5,
        "ingredients": [{"name": "copper-plate", "amount": 1}, {"name": "iron-gear-wheel", "amount": 1}],
        "products": [{"name": "automation-science-pack", "amount": 1}],
    }
    for plate in ("iron-plate", "copper-plate"):
        recipes[plate] = {"energy": 3.2, "ingredients": [], "products": [{"name": plate, "amount": 1}]}
    entities = {"assembling-machine-1": {"crafting_speed": 0.5}, "stone-furnace": {"crafting_speed": 1}}
    return SimpleNamespace(recipes=recipes, entities=entities, recipe_for_product=recipes.get, fingerprint="cat")


def site_service(root, kind, **kwargs):
    def survey(game, rect):
        world = make_world(SEED, size=512)
        return world if kind is None else transform_world(world, kind)

    return SiteService(FakeGame(root, WORLD), half_size=256, surveyor=survey, **kwargs)


def inside(area, rect) -> bool:
    (x0, y0), (x1, y1) = area
    return rect.x0 <= x0 and rect.y0 <= y0 and x1 <= rect.x1 and y1 <= rect.y1


def tiles(entities):
    return set().union(set(), *(GEOMETRY._occupied_by_plan([e]) for e in entities))


def overlaps(entities):
    return sum(len(GEOMETRY._occupied_by_plan([e])) for e in entities) - len(tiles(entities))


class World:
    """A fresh map once red science is planned: district, red row, labs and red routes on plan sites."""

    def __init__(self, root, kind, *, iron_ore=60, copper_ore=30, red_rate=30):
        self.root = root
        self.site = site_service(root, kind)
        district = plan_candidates(self.site.plan(WORLD), COUNTS)[0]
        ports = [p for b in district["banks"] for p in b["ports"] if p["direction"] == "output"]
        self.district = _plan(
            district["entities"], ports, reserved_area=district["area"], site_transform=district["site_transform"]
        )
        block = planned_science_candidates(self.site, WORLD, 3)[0]
        settle(self.site, WORLD, "automation_science", block["allocation"])
        self.science_block, self.labs = block["science"], block["labs"]
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root), query=lambda _body: CLEAR)
        self.write_board({"smelting_district": self.district})
        entities = merge_entities(self.district["entities"], self.science_block["entities"], self.labs["entities"])
        self.obs = {
            "world_id": WORLD,
            "tick": 100,
            "enabled_recipes": {"logistic-science-pack": True, "fast-inserter": True},
            "entities": self.built(entities),
        }
        # Red science's own routes, planned like AutomaticScience.prepare does.
        sources = {p["item"]: p for p in ports}
        connections = [(sources[p["item"]], p) for p in self.science_block["ports"] if p["direction"] == "input"]
        connections.append((self.science_block["ports"][-1], self.labs["ports"][0]))
        self.red_routes = []
        for source, destination in connections:
            route = plan_connection(self.game, BUILDER, self.obs, source, destination, entities)
            assert route["ok"], (kind, source["item"], route.get("reason"))
            entities = merge_entities(entities, route["entities"])
            self.red_routes += route["entities"]
        self.obs["entities"] = self.built(entities)
        self.write_board(
            {
                "smelting_district": self.district,
                "automation_science": self.science_block,
                "laboratories": self.labs,
            }
        )
        mining = {
            "world_id": WORLD,
            "banks": [
                {"ready": True, "plan": {"resource": "iron-ore"}, "nominal_rate_per_minute": iron_ore},
                {"ready": True, "plan": {"resource": "copper-ore"}, "nominal_rate_per_minute": copper_ore},
            ],
        }
        (root / "mining-outposts.json").write_text(json.dumps(mining), encoding="utf-8")
        smelting = SimpleNamespace(state={"world_id": WORLD, "plan": self.district, "banks": district["banks"]})
        science = SimpleNamespace(proof=lambda obs: True, state={"labs": self.labs, "nominal_machine_rate": red_rate})
        self.green = LogisticScience(self.game, BUILDER, catalog(), smelting, science, site=self.site)

    @staticmethod
    def built(entities):
        return [dict(e, unit_number=i + 1) for i, e in enumerate(entities)]

    def write_board(self, blocks):
        board = {"world_id": WORLD, "catalog_fingerprint": "cat", "blocks": blocks}
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")

    def prepare(self, rate=30):
        def legal(game, slots):
            return {"ok": True, "legal": list(range(1, len(slots) + 1))}

        with (
            patch("factorio_agent.green_science.preflight", return_value={"ok": True, "clear_obstacles": []}),
            patch("factorio_agent.green_science.survey_slots", side_effect=legal),
            patch("factorio_agent.green_science.connect_power_groups", return_value={"ok": True, "entities": []}),
        ):
            return self.green.prepare(self.obs, {"rate_per_minute": rate})


class GreenSitePlanTests(unittest.TestCase):
    def test_narrow_core_fits_a_district_block_with_plates_on_the_bus_edge(self):
        with TemporaryDirectory() as directory:
            site = site_service(Path(directory), None)
            self.assertGreaterEqual(site.plan(WORLD).district_depth, GREEN_CORE_DEPTH)
            cores = planned_green_cores(site, WORLD, 7)
            self.assertTrue(cores)
            for core in cores:
                allocation = site.allocation(core["allocation"], WORLD)
                self.assertEqual((allocation.along1 - allocation.along0, allocation.depth), GREEN_BLOCK)
                self.assertTrue(inside(core["reserved_area"], allocation.rect))
                self.assertEqual(overlaps(core["entities"]), 0)
                # Both plate inputs sit near the bus edge, facing away from it.
                tap = allocation.tap_rect
                normal = {"N": (0, -1), "E": (1, 0), "S": (0, 1), "W": (-1, 0)}[allocation.input_side]
                for port in core["ports"][:2]:
                    p = port["position"]
                    gap = max(tap.x0 - p["x"], p["x"] - tap.x1, tap.y0 - p["y"], p["y"] - tap.y1, 0)
                    self.assertLessEqual(gap, 8, port)
                    facing = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}[port["facing"]]
                    self.assertEqual(facing, (-normal[0], -normal[1]), port)

    def test_every_orientation_plans_branch_core_and_lab_delivery(self):
        seen = set()
        for kind in KINDS:
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                world = World(Path(directory), kind)
                result = world.prepare()
                self.assertTrue(result["ok"], (kind, result.get("reason"), result.get("failures", [])[:2]))
                district = Transform.from_dict(world.district["site_transform"])
                seen.add(("district", district.rotation, district.mirror))
                labs = plan_transform(world.labs)
                seen.add(("labs", labs.rotation, labs.mirror))
                core = result["core"]
                self.assertTrue(inside(core["reserved_area"], world.site.allocation(core["allocation"], WORLD).rect))
                # One splitter per plate port replaces the belt just upstream of it.
                self.assertEqual(len(result["remove"]), 2)
                splitters = [e for e in result["source_plan"]["entities"] if e["name"] == "splitter"]
                for removed in result["remove"]:
                    self.assertEqual(removed["direction"], district.direction(4))
                    self.assertTrue(any(xy(removed) in GEOMETRY._occupied_by_plan([s]) for s in splitters))
                self.assertEqual(overlaps(result["source_plan"]["entities"]), 0)
                # The new hardware never lands on existing hardware except the replaced belts.
                existing = tiles(e for e in world.obs["entities"] if e not in result["remove"])
                fresh = [
                    e
                    for e in result["plan"]["entities"]
                    if not any(e["name"] == o["name"] and xy(e) == xy(o) for o in world.obs["entities"])
                ]
                self.assertEqual(overlaps(result["plan"]["entities"]), 0)
                self.assertFalse(tiles(fresh) & existing, kind)
        self.assertTrue(any(r or m for part, r, m in seen if part == "district"), seen)
        self.assertTrue(any(m for part, r, m in seen if part == "district"), seen)
        self.assertTrue(any(r for part, r, m in seen if part == "labs"), seen)

    def test_a_site_service_never_falls_back_to_development_anchors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            world = World(root, None)
            with patch("factorio_agent.green_science.planned_green_cores", return_value=[]):
                full = world.prepare()
            self.assertEqual(full["reason"], "no_green_science_district_block")
            (root / "shallow").mkdir()
            config = SiteConfig(district_depth=32, district_depth_fallbacks=(24,))
            world.green.site = site_service(root / "shallow", None, config=config)
            with patch("factorio_agent.green_science.green_core", side_effect=AssertionError("legacy anchor")):
                shallow = world.prepare()
        self.assertFalse(shallow["ok"])
        self.assertEqual(shallow["reason"], "green_science_needs_deeper_district")
        self.assertEqual((shallow["district_depth"], shallow["block_depth"]), (32, GREEN_CORE_DEPTH))


class GreenLabEntryTests(unittest.TestCase):
    def test_lab_entry_port_lies_outside_the_lab_block_in_every_orientation(self):
        canonical = lab_row(1.5, 8.5)
        for base in variants():
            turn = Transform(base.rotation, base.mirror, (16, 8))
            labs = dict(apply(turn, canonical), site_transform=turn.to_dict())
            with self.subTest(rotation=turn.rotation, mirror=turn.mirror):
                entry = in_canonical(labs, lab_green_entry_outside)
                self.assertEqual(overlaps(labs["entities"] + entry["entities"]), 0)
                (x0, y0), (x1, y1) = labs["reserved_area"]
                p = entry["port"]["position"]
                self.assertFalse(x0 < p["x"] < x1 and y0 < p["y"] < y1)
                belt = next(e for e in entry["entities"] if xy(e) == xy(entry["port"]))
                self.assertEqual(belt["direction"], entry["port"]["facing"])
                # The last chain belt side-loads the lab belt head from the canonical south.
                jx, jy = entry["join"]["x"], entry["join"]["y"]
                last = next(e for e in entry["entities"] if abs(xy(e)[0] - jx) + abs(xy(e)[1] - jy) == 1)
                self.assertEqual(last["direction"], turn.direction(0))
                self.assertEqual(len(entry["pairs"]), 2)


class GreenSupplySizingTests(unittest.TestCase):
    def sized(self, iron_ore, copper_ore, red_rate, count=7):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            mining = {
                "world_id": WORLD,
                "banks": [
                    {"ready": True, "plan": {"resource": "iron-ore"}, "nominal_rate_per_minute": iron_ore},
                    {"ready": True, "plan": {"resource": "copper-ore"}, "nominal_rate_per_minute": copper_ore},
                ],
            }
            (root / "mining-outposts.json").write_text(json.dumps(mining), encoding="utf-8")
            banks = [
                {"product": "iron-plate", "resource": "iron-ore", "count": 8},
                {"product": "copper-plate", "resource": "copper-ore", "count": 4},
            ]
            game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root))
            smelting = SimpleNamespace(state={"world_id": WORLD, "banks": banks})
            science = SimpleNamespace(state={"nominal_machine_rate": red_rate})
            green = LogisticScience(game, BUILDER, catalog(), smelting, science)
            return green.supply_sized_count(count, 5)

    def test_a_single_shared_iron_belt_still_feeds_one_machine_through_its_splitter_half(self):
        # Red's 5-machine row takes all 60 iron/min; the splitter still hands green half.
        self.assertEqual(self.sized(60, 30, 30), {"ok": True, "count": 1})

    def test_headroom_after_red_sizes_the_row_down(self):
        # 150 iron/min (8 furnaces) minus red's 60 leaves 90: 3 machines at 27.5/min.
        self.assertEqual(self.sized(400, 200, 30), {"ok": True, "count": 3})
        self.assertEqual(self.sized(400, 200, 0, count=2), {"ok": True, "count": 2})

    def test_too_little_plate_supply_blocks_with_the_short_item(self):
        result = self.sized(40, 30, 30)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "green_science_needs_connected_supply_headroom")
        self.assertEqual(result["item"], "iron-plate")
        self.assertEqual(result["needed"], 27.5)


if __name__ == "__main__":
    unittest.main()
