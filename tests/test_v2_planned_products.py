"""Engines and magazines as generic produce rows on a planned world (planned_products.py), steel growth.

Fresh fake maps in every orientation (``test_v2_green_site.World``: district,
planned bus, red science on taps), a site-planned steel block
(``planned_furnaces``), then the engine row: twin sub-rows (gears + pipes)
from one planned-bus iron tap, steel from the steel block's board port.
"""

import json
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_v2_green_site import KINDS as WORLD_KINDS
from test_v2_green_site import WORLD, World
from test_v2_planned_furnaces import GEOMETRY, OK, material_sources
from test_v2_planned_furnaces import route as furnace_route
from test_v2_production_layout import catalog as layout_catalog
from test_v2_smelting_paired_district import bfs_route
from v2_blocks_fixture import _entity, _recipe

from factorio_agent import planned_products
from factorio_agent import progression as pg
from factorio_agent.ammo_production import AmmoProduction
from factorio_agent.core.world_catalog import WorldCatalog
from factorio_agent.engine_production import EngineProduction
from factorio_agent.jobs import CAPABILITIES
from factorio_agent.planned_furnaces import PlannedFurnaces, built_rate, furnace_block_claims, per_row_for
from factorio_agent.production_layout import audit_line, rows
from factorio_agent.production_rows import BLOCK_PREFIX, ProductionRows
from factorio_agent.site.transform import Transform
from factorio_agent.site.transform import apply as apply_transform
from factorio_agent.steel_capacity import SteelCapacity
from factorio_agent.transport import xy

SITE = "factorio_agent.production_site."
ENGINE_OWNER = BLOCK_PREFIX + "engine-unit"


class Catalog(WorldCatalog):
    fingerprint = "cat"  # the fake worlds' board fingerprint


def catalog():
    """Layout fake catalog plus engines, magazines, steel smelting and a burner furnace."""
    data = layout_catalog().to_dict()
    data["recipes"]["engine-unit"] = _recipe(
        10, {"steel-plate": 1, "iron-gear-wheel": 1, "pipe": 2}, {"engine-unit": 1}, "advanced-crafting"
    )
    data["recipes"]["firearm-magazine"] = _recipe(1, {"iron-plate": 4}, {"firearm-magazine": 1})
    data["recipes"]["steel-plate"] = _recipe(16, {"iron-plate": 5}, {"steel-plate": 1}, "smelting")
    data["entities"]["stone-furnace"] = _entity(
        "furnace", 2, electric=False, burner=True, crafting_speed=1, crafting_categories=["smelting"], energy_usage=1500
    )
    data["items"] = {"coal": {"fuel_value": 4000000, "stack_size": 50}}
    return Catalog.from_dict(data)


def enabled(c):
    names = dict.fromkeys(c.recipes, True)
    names.update({"small-electric-pole": True, "iron-chest": True, "long-handed-inserter": True})
    return names


def keepout_route(world):
    """Shortest belt path that stays out of the bus band and every foreign board block."""

    def route(game, builder, obs, source, destination, reserved=(), **kwargs):
        board = world.board()
        start = xy(source)
        hard = []
        rects = [b["reserved_area"] for b in board["blocks"].values() if isinstance(b, dict) and b.get("reserved_area")]
        bus = world.bus_rect
        rects.append([[bus.x0, bus.y0], [bus.x1, bus.y1]])
        # bfs_route searches 48 tiles around both ends; make every keepout tile of that window hard.
        lo = (math.floor(min(start[0], xy(destination)[0])) - 50, math.floor(min(start[1], xy(destination)[1])) - 50)
        hi = (math.ceil(max(start[0], xy(destination)[0])) + 50, math.ceil(max(start[1], xy(destination)[1])) + 50)
        for (x0, y0), (x1, y1) in rects:
            for x in range(math.floor(max(x0, lo[0])), math.ceil(min(x1, hi[0]))):
                for y in range(math.floor(max(y0, lo[1])), math.ceil(min(y1, hi[1]))):
                    tile = (x + 0.5, y + 0.5)
                    if tile not in (start, xy(destination)):
                        hard.append({"name": "wooden-chest", "position": {"x": tile[0], "y": tile[1]}})
        found = bfs_route(game, builder, obs, source, destination, list(reserved) + hard)
        manhattan = abs(start[0] - xy(destination)[0]) + abs(start[1] - xy(destination)[1])
        if found.get("ok") and len(found["entities"]) > 1.35 * manhattan + 4:  # the router's length gate
            return {"ok": False, "reason": "route_too_long"}
        return dict(found, clear_obstacles=found.get("clear_obstacles", []))

    return route


def steel_block(world, c):
    """The planned steel block, planned, published and verified (built furnaces made steel)."""
    material_sources(world)
    steel = PlannedFurnaces(
        world.game,
        Mock(_occupied_by_plan=GEOMETRY._occupied_by_plan),
        c,
        site=world.site,
        kind="steel",
        route=furnace_route,
        power=lambda *a, **k: {"ok": True, "entities": []},
        check=lambda *a, **k: OK,
    )
    steel.state = steel.prepare(world.obs)
    assert steel.state["ok"], steel.state
    steel.state["phase"] = "verified"
    for tap in steel.state["taps"]:
        tap["built"] = True
    steel._save()
    assert steel._publish(world.obs) is None
    return steel


def engine_rows(world, c):
    game = SimpleNamespace(cfg=world.game.cfg, query=Mock(return_value={"ok": True, "max_span": 5}))
    builder = Mock(_occupied_by_plan=GEOMETRY._occupied_by_plan)
    builder.ensure_plan.return_value = {"status": "succeeded"}
    builder.bootstrap.ensure_item.return_value = {"type": "craft", "recipe": "splitter", "count": 1}
    return ProductionRows(game, builder, c, site=world.site)


def used(line):
    """Declared demand basis: a stock row averages its requested rate, at most its nameplate."""
    return min(planned_products.ENGINE_ROW["rate_per_minute"], line["rated_per_minute"])


def planned_obs(world, c):
    return dict(world.obs, enabled_recipes=enabled(c))


class EngineRowTests(unittest.TestCase):
    def prepare(self, world, c):
        steel = steel_block(world, c)
        rows_ = engine_rows(world, c)
        with (
            patch(SITE + "plan_connection", side_effect=keepout_route(world)),
            patch(SITE + "connect_power_groups", return_value={"ok": True, "entities": []}),
            patch(SITE + "preflight", return_value=OK),
        ):
            state = rows_.prepare(planned_obs(world, c), dict(planned_products.ENGINE_ROW))
        return steel, rows_, state

    def test_engine_row_on_every_orientation_taps_iron_and_routes_steel_from_the_steel_port(self):
        c = catalog()
        seen = set()
        for kind in WORLD_KINDS:
            with self.subTest(world=kind), TemporaryDirectory() as directory:
                world = World(Path(directory), kind)
                steel, _rows, state = self.prepare(world, c)
                self.assertTrue(state.get("ok"), (kind, state.get("reason"), state.get("failures")))
                line = state["line"]
                # Twin sub-rows: gears and pipes made in-line, one mixed belt, audit clean when turned.
                self.assertEqual({line["spec"]["sub_item"], line["spec"]["twin_item"]}, {"pipe", "iron-gear-wheel"})
                self.assertEqual(audit_line(c, line), [])
                # Site from the allocation: the whole reserved block lies inside it.
                allocation = world.site.allocation(state["allocation"], WORLD)
                (x0, y0), (x1, y1) = line["reserved_area"]
                rect = allocation.rect
                self.assertTrue(rect.x0 <= x0 and rect.y0 <= y0 and x1 <= rect.x1 and y1 <= rect.y1)
                # Inputs: one planned-bus iron tap for both sub-rows, steel from the steel block's port.
                self.assertEqual([(t["item"], t["owner"]) for t in state["taps"]], [("iron-plate", ENGINE_OWNER)])
                self.assertAlmostEqual(state["taps"][0]["demand_per_minute"], 4 * used(line))
                kinds = {s["item"]: (s["kind"], s["block"]) for s in state["sources"]}
                self.assertEqual(kinds["steel-plate"], ("board_port", "steel_production"))
                self.assertEqual(kinds["iron-plate"], ("planned_bus_tap", "shared_main_bus"))
                steel_out = steel.state["bank"]["ports"][2]["position"]
                claim = next(x for x in state["connections"] if x["item"] == "steel-plate")
                self.assertEqual(claim["source"], steel_out)
                self.assertAlmostEqual(claim["demand_per_minute"], used(line))
                # Growth slots stay free: no plan entity on a reserved main-row slot.
                taken = {t for e in state["plan"]["entities"] for t in GEOMETRY._occupied_by_plan([e])}
                for slot in line["main"]["expansion_slots"]:
                    self.assertFalse(GEOMETRY._occupied_by_plan([dict(slot, direction=0)]) & taken)
                # Only the tap itself stands in the bus band; the block and the steel route stay out.
                bus = world.bus_rect
                tap = {t for e in state["taps"][0]["entities"] for t in GEOMETRY._occupied_by_plan([e])}
                self.assertFalse(any(bus.x0 < x < bus.x1 and bus.y0 < y < bus.y1 for x, y in taken - tap))
                seen.add((world.site.plan(WORLD).bus.direction, state["transform"]["rotation"]))
        self.assertGreaterEqual(len({s[0] for s in seen}), 2, seen)
        self.assertGreaterEqual(len({s[1] for s in seen}), 2, seen)

    def test_step_publishes_the_whole_plan_and_tap_before_building(self):
        c = catalog()
        with TemporaryDirectory() as directory:
            world = World(Path(directory), None)
            steel_block(world, c)
            rows_ = engine_rows(world, c)
            engines = EngineProduction(rows_.game, rows_.builder, c, None, tap=Mock(), steel=Mock(), rows=rows_)
            obs = planned_obs(world, c)
            with (
                patch(SITE + "plan_connection", side_effect=keepout_route(world)),
                patch(SITE + "connect_power_groups", return_value={"ok": True, "entities": []}),
                patch(SITE + "preflight", return_value=OK),
            ):
                action = engines.step(obs)
            self.assertEqual(action["type"], "craft")  # tap stock first (planned_tap.cutover)
            state = rows_.state("engine-unit")
            board = world.board()
            self.assertEqual(board["blocks"][ENGINE_OWNER], state["plan"])
            for connection in state["connections"]:
                self.assertIn(connection, board["connections"])
            self.assertIn(ENGINE_OWNER + ":iron-plate", [s["name"] for s in board["shared_main_bus"]["stations"]])
            rows_.builder.ensure_plan.assert_not_called()
            # The steel block's claims now count the engine row.
            claims = furnace_block_claims(world.root, obs)["blocks"][0]
            self.assertEqual(claims["kind"], "steel")
            self.assertAlmostEqual(claims["claimed_port_demand_per_minute"], used(state["line"]))
            # A development-map world (no site service) keeps the old controller.
            legacy = EngineProduction(rows_.game, rows_.builder, c, None, tap=Mock(), steel=Mock())
            self.assertIsNone(legacy.planned(obs))

    def test_engine_proof_needs_every_row_machine_and_chest_stock(self):
        c = catalog()
        with TemporaryDirectory() as directory:
            world = World(Path(directory), None)
            _steel, rows_, state = self.prepare(world, c)
            for tap in state["taps"]:
                tap["built"] = True
            rows_.save("engine-unit", dict(state, bar_set=True))
            engines = EngineProduction(rows_.game, rows_.builder, c, None, tap=Mock(), steel=Mock(), rows=rows_)
            live = []
            for index, e in enumerate(state["plan"]["entities"], 1):
                row = dict(e, unit_number=index, electric_network_connected=True, energy=1, products_finished=2)
                if e["name"] == "iron-chest":
                    row["inventory"] = {"engine-unit": 3}
                live.append(row)
            by_tile = {xy(r): r for r in live if r["name"] == "underground-belt"}
            for pair in state["plan"]["underground_pairs"]:
                first, last = by_tile[tuple(pair["input"])], by_tile[tuple(pair["output"])]
                first.update(underground_pair_verified=True, underground_neighbour={"unit_number": last["unit_number"]})
                last["underground_pair_verified"] = True
            obs = dict(planned_obs(world, c), entities=live)
            proof = engines.proof(obs)
            self.assertIsNotNone(proof, rows_.last_proof_failure)
            machines = [e for r in rows(state["line"]) for e in r["entities"] if e.get("recipe")]
            self.assertEqual(len(proof["machine_units"]), len(machines))
            gear = next(e for e in machines if e["recipe"] == "iron-gear-wheel")
            idle = [
                dict(r, products_finished=0) if xy(r) == xy(gear) and r["name"] == gear["name"] else r for r in live
            ]
            self.assertIsNone(engines.proof(dict(obs, entities=idle)))
            self.assertEqual(rows_.last_proof_failure["reason"], "machine_has_not_produced")

    def test_locked_engine_recipe_waits_instead_of_blocking(self):
        c = catalog()
        with TemporaryDirectory() as directory:
            world = World(Path(directory), None)
            steel_block(world, c)
            rows_ = engine_rows(world, c)
            engines = EngineProduction(rows_.game, rows_.builder, c, None, tap=Mock(), steel=Mock(), rows=rows_)
            locked = dict(enabled(c), **{"engine-unit": False})
            result = engines.step(dict(world.obs, enabled_recipes=locked))
            self.assertEqual(result["status"], "waiting")
            self.assertIn("engine-unit", result["missing"])

    def test_mirrored_twin_line_in_canonical_frame_turns_cleanly(self):
        c = catalog()
        with TemporaryDirectory() as directory:
            world = World(Path(directory), None)
            _steel, _rows, state = self.prepare(world, c)
            for rotation in (0, 4, 8, 12):
                turned = apply_transform(Transform(rotation, None, (0.5, 0.5)), state["canonical"])
                self.assertEqual(audit_line(c, turned), [])


class AmmoRowTests(unittest.TestCase):
    def test_magazine_row_on_a_planned_tap_and_refill_from_its_chest(self):
        c = catalog()
        with TemporaryDirectory() as directory:
            world = World(Path(directory), "rotate")
            rows_ = engine_rows(world, c)
            perimeter = Mock()
            ammo = AmmoProduction(rows_.game, rows_.builder, c, perimeter, tap=Mock(), rows=rows_)
            obs = planned_obs(world, c)
            self.assertFalse(ammo.commissioned)
            with (
                patch(SITE + "plan_connection", side_effect=keepout_route(world)),
                patch(SITE + "connect_power_groups", return_value={"ok": True, "entities": []}),
                patch(SITE + "preflight", return_value=OK),
            ):
                action = ammo.step(obs)
            self.assertEqual(action["type"], "craft")
            state = rows_.state("firearm-magazine")
            self.assertEqual(
                [(t["item"], t["owner"]) for t in state["taps"]], [("iron-plate", BLOCK_PREFIX + "firearm-magazine")]
            )
            self.assertIn(BLOCK_PREFIX + "firearm-magazine", world.board()["blocks"])
            perimeter.ensure_coverage.assert_not_called()
            # Commissioned: the turret refill takes full magazines from the row's stock chest.
            rows_.save("firearm-magazine", dict(state, phase="verified"))
            self.assertTrue(ammo.commissioned)
            chest = state["line"]["tail"]["chest"]
            live = dict(chest, unit_number=77)
            rows_.game.query = Mock(
                return_value={
                    "ok": True,
                    "unit_number": 77,
                    "actor_unit": 1,
                    "full_magazines": 30,
                    "source_slots": [1],
                    "actor_full_magazines": 5,
                    "insertable": 400,
                    "bar_slots": 2,
                }
            )
            action = ammo.ensure_full_magazines(dict(obs, entities=[live]), 20)
            self.assertEqual(action["type"], "take_full_magazines")
            self.assertEqual((action["position"], action["count"]), (chest["position"], 15))
            self.assertEqual(action["expected_entity_unit"], 77)


class SteelGrowthTests(unittest.TestCase):
    def test_claims_trigger_growth_in_reserved_slots_and_the_capacity_proof(self):
        c = catalog()
        with TemporaryDirectory() as directory:
            world = World(Path(directory), None)
            steel = steel_block(world, c)
            nominal = built_rate(steel.state)
            out = steel.state["bank"]["ports"][2]["position"]
            board = world.board()
            board["connections"].append(
                {
                    "item": "steel-plate",
                    "source": out,
                    "destination": {"x": 0, "y": 0},
                    "owner": "x",
                    "demand_per_minute": 7,
                }
            )
            (world.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
            claims = furnace_block_claims(world.root, world.obs)
            row = claims["blocks"][0]
            self.assertEqual((row["nominal_capacity_per_minute"], row["claimed_port_demand_per_minute"]), (nominal, 7))
            jobs = [{"capability": "steel_production", "status": "succeeded", "parameters": {}}]
            nodes = pg.steel_capacity_nodes(jobs, [], {"furnace_block_claims": claims}, CAPABILITIES)
            self.assertEqual(len(nodes), 1)
            target = nodes[0]["parameters"]["rate_per_minute"]
            self.assertEqual(target, round(7 * pg.SMELTING_HEADROOM, 1))
            # Below the trigger, with a node open, or when the block is full: nothing.
            low = json.loads(json.dumps(claims))
            low["blocks"][0]["claimed_port_demand_per_minute"] = 2
            self.assertEqual(pg.steel_capacity_nodes(jobs, [], {"furnace_block_claims": low}, CAPABILITIES), [])
            self.assertEqual(pg.steel_capacity_nodes(jobs, nodes, {"furnace_block_claims": claims}, CAPABILITIES), [])
            self.assertEqual(pg.steel_capacity_nodes([], [], {"furnace_block_claims": claims}, CAPABILITIES), [])
            # steel_capacity on a planned world fills reserved slots in place, then proves them.
            industrial = SimpleNamespace(planned=lambda kind, obs: steel)
            capacity = SteelCapacity(world.game, Mock(), c, Mock(), steel=Mock(), industrial=industrial)
            old = {(e["name"], *xy(e)) for e in steel.state["plan"]["entities"]}
            steel.builder.ensure_plan.return_value = {"status": "running", "reason": "building"}
            result = capacity.step(dict(world.obs, enabled_recipes={"steel-plate": True}), {"rate_per_minute": target})
            self.assertEqual(result["reason"], "building")
            self.assertEqual(steel.state["bank"]["per_side_count"], per_row_for(steel.state, target))
            new = {(e["name"], *xy(e)) for e in steel.state["plan"]["entities"]}
            reserved = {
                (e["name"], *xy(e))
                for e in json.loads(json.dumps(world.board()))["blocks"]["steel_production"]["entities"]
            }
            self.assertTrue(old < new)
            self.assertEqual(new, reserved)  # the grown plan is published on the board before building
            self.assertGreaterEqual(built_rate(steel.state), target)
            self.assertIsNone(capacity.proof(world.obs))
            too_much = capacity.step(world.obs, {"rate_per_minute": 1000})
            self.assertEqual(too_much["reason"], "furnace_capacity_exceeds_reserved_slots")


class RoadmapTests(unittest.TestCase):
    def done_until(self, stop, skip=()):
        jobs = []
        for step in pg.ROADMAP:
            if step.id == stop:
                return jobs
            if step.id not in skip:
                jobs.append({"capability": step.capability, "parameters": dict(step.parameters), "status": "succeeded"})
        raise AssertionError(stop)

    def test_roadmap_reaches_engines_and_ammo_on_a_planned_world(self):
        observation = {"technologies": {}}
        for stop, node in (
            ("engine_production", "auto_engine_production"),
            ("ammo_production", "auto_ammo_production"),
        ):
            with self.subTest(step=stop):
                jobs = self.done_until(stop)
                ids = [n["id"] for n in pg.autopilot_nodes(jobs, [], observation, CAPABILITIES, limit=20)]
                self.assertIn(node, ids)
        # Engines wait for their research step (never a blocked job).
        jobs = self.done_until("engine_production", skip=("research_engine",))
        ids = [n["id"] for n in pg.autopilot_nodes(jobs, [], observation, CAPABILITIES, limit=20)]
        self.assertNotIn("auto_engine_production", ids)
        self.assertIn("steel_production", CAPABILITIES["engine_production"]["requires"])


if __name__ == "__main__":
    unittest.main()
