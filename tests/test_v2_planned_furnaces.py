"""Site-planned steel and brick furnace blocks (planned_furnaces.py) on fresh maps in every orientation."""

import json
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

from test_v2_green_site import KINDS as WORLD_KINDS
from test_v2_green_site import WORLD, World, feed_route
from test_v2_smelting_paired_district import bfs_route

from factorio_agent import bus_lane
from factorio_agent import plan_needs
from factorio_agent import planned_bus as pb
from factorio_agent import planned_furnaces as pf
from factorio_agent import planned_tap as pt
from factorio_agent import progression as pg
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.industrial import IndustrialOperations
from factorio_agent.jobs import CAPABILITIES
from factorio_agent.planned_furnaces import (
    KINDS,
    LENGTH,
    SLOTS,
    START_PER_ROW,
    PlannedFurnaces,
    furnace_block,
    grown,
    growth_evidence,
)
from factorio_agent.smelting import BANK_DEPTH, growth_tiles
from factorio_agent.transport import xy

GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)
OK = {"ok": True, "clear_obstacles": []}
FACING = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}
NORMAL = {"N": (0, -1), "E": (1, 0), "S": (0, 1), "W": (-1, 0)}


def catalog():
    def smelt(product, raw, amount, energy):
        return {
            "energy": energy,
            "categories": ["smelting"],
            "ingredients": [{"name": raw, "type": "item", "amount": amount}],
            "products": [{"name": product, "type": "item", "amount": 1}],
        }

    return SimpleNamespace(
        fingerprint="cat",
        recipes={
            "steel-plate": smelt("steel-plate", "iron-plate", 5, 16),
            "stone-brick": smelt("stone-brick", "stone", 2, 3.2),
        },
        entities={
            "stone-furnace": {
                "burner": True,
                "crafting_speed": 1,
                "crafting_categories": ["smelting"],
                "energy_usage": 1500,
            }
        },
        items={"coal": {"fuel_value": 4000000}},
    )


def route(game, builder, obs, source, destination, reserved=(), **kwargs):
    """Shortest-path stand-in for the router; reserved non-belts (furnaces, growth keepouts) are hard."""
    return bfs_route(game, builder, obs, source, destination, reserved)


def tiles(entities):
    return set().union(set(), *(GEOMETRY._occupied_by_plan([e]) for e in entities))


def overlaps(entities):
    return sum(len(GEOMETRY._occupied_by_plan([e])) for e in entities) - len(tiles(entities))


def inside(area, rect):
    (x0, y0), (x1, y1) = area
    return rect.x0 <= x0 and rect.y0 <= y0 and x1 <= rect.x1 and y1 <= rect.y1


def material_sources(world, far=None):
    """Coal and stone outlets of the dedicated banks beside the bus's left edge, near the first
    district blocks (short private belts, planned_furnaces.PRIVATE_ROUTE_TILES).

    ``far``: ``{key: (along, lateral)}`` moves outlets (a patch across the base)."""
    view = pt.BusView(world.board()["shared_main_bus"])
    half = view.bus.half

    def source(item, along, lateral, key):
        return {
            "item": item,
            "direction": "output",
            "facing": 4,
            "kind": "item",
            "name": "transport-belt",
            "position": view.world(along, lateral),
            "unit_number": 9000 + along,
            "world_id": WORLD,
            "material_supply_id": f"dedicated-material:{WORLD}:{item}",
        }

    where = {"coal": (24, -(half + 22)), "brick-coal": (26, -(half + 22)), "stone": (40, -(half + 22))}
    where.update(far or {})
    sources = {
        key: source("stone" if key == "stone" else "coal", along, lateral, key)
        for key, (along, lateral) in where.items()
    }
    data = {"world_id": WORLD, "catalog_fingerprint": "cat", "sources": sources}
    (world.root / "material-sources.json").write_text(json.dumps(data), encoding="utf-8")
    return sources


def controller(world, kind, builder=None):
    return PlannedFurnaces(
        world.game,
        builder or Mock(_occupied_by_plan=GEOMETRY._occupied_by_plan),
        catalog(),
        site=world.site,
        kind=kind,
        route=route,
        power=lambda *a, **k: {"ok": True, "entities": []},
        check=lambda *a, **k: OK,
    )


class TemplateTests(unittest.TestCase):
    def test_block_reserves_every_slot_and_builds_two_furnaces(self):
        for kind in KINDS.values():
            with self.subTest(kind=kind.name):
                bank = furnace_block(kind, 0, 0)
                self.assertEqual(bank["reserved_area"], [[0, 0], [LENGTH, BANK_DEPTH]])
                self.assertEqual((bank["resource"], bank["product"]), (kind.raw, kind.product))
                furnaces = [e for e in bank["entities"] if e["name"] == "stone-furnace"]
                self.assertEqual(len(furnaces), 2 * START_PER_ROW)
                self.assertEqual(len(bank["growth_slots"]), 2 * (SLOTS - START_PER_ROW))
                self.assertEqual(overlaps(bank["entities"] + bank["growth_entities"]), 0)
                self.assertTrue(all(0 <= x < LENGTH and 0 <= y < BANK_DEPTH for x, y in tiles(bank["entities"])))
                raw, coal, product = bank["ports"]
                self.assertEqual((raw["item"], coal["item"], product["item"]), (kind.raw, "coal", kind.product))
                self.assertEqual(product["direction"], "output")

    def test_growth_adds_reserved_slots_in_place(self):
        bank = furnace_block(KINDS["steel"], 0, 0)
        state = {"bank": bank, "plan": {"entities": list(bank["entities"]), "growth_entities": bank["growth_entities"]}}
        bigger = grown(state, 3)
        old = {(e["name"], *xy(e)) for e in bank["entities"]}
        new = {(e["name"], *xy(e)) for e in bigger["plan"]["entities"]}
        self.assertTrue(old < new)
        reserved = {(e["name"], *xy(e)) for e in bank["growth_entities"]}
        self.assertTrue(new - old <= reserved)
        self.assertEqual(sum(e["name"] == "stone-furnace" for e in bigger["bank"]["entities"]), 6)
        self.assertEqual(bigger["bank"]["ports"], bank["ports"])
        self.assertEqual(
            len(bigger["plan"]["growth_entities"]), len(bank["growth_entities"]) - 4 * 7
        )  # 4 slots: furnace, 2 inserters, pole, 3 belts
        with self.assertRaises(ValueError):
            grown(state, SLOTS + 1)

    def test_growth_waits_for_busy_furnaces_and_a_taken_product(self):
        bank = furnace_block(KINDS["steel"], 0, 0)
        state = {"bank": bank, "raw_per_craft": 5}
        raw, _coal, product = bank["ports"]

        def obs(raw_in_furnace, raw_on_belt, product_on_belt):
            rows = [
                dict(e, inventory={"iron-plate": raw_in_furnace}) for row in bank["producer_rows"].values() for e in row
            ]
            rows.append(
                dict(name="transport-belt", position=raw["position"], belt_inventory={"iron-plate": raw_on_belt})
            )
            rows.append(
                dict(
                    name="transport-belt", position=product["position"], belt_inventory={"steel-plate": product_on_belt}
                )
            )
            return {"entities": rows}

        self.assertTrue(growth_evidence(state, obs(10, 8, 0))["grow"])
        self.assertEqual(growth_evidence(state, obs(4, 8, 0))["reason"], "a_built_furnace_waits_for_raw")
        self.assertEqual(growth_evidence(state, obs(10, 2, 0))["reason"], "raw_supply_not_backed_up")
        self.assertEqual(growth_evidence(state, obs(10, 8, 8))["reason"], "product_not_taken")


class SitePlanTests(unittest.TestCase):
    def check_plan(self, world, state, kind):
        allocation = world.site.allocation(state["allocation"], WORLD)
        self.assertIsNotNone(allocation)
        self.assertEqual(allocation.name, kind.owner + "#" + state["allocation"].split("#")[1])
        # Only the kept candidate stays allocated.
        names = [a.name for a in world.site.plan(WORLD).allocations if a.name.startswith(kind.owner)]
        self.assertEqual(names, [allocation.name])
        bank, plan = state["bank"], state["plan"]
        self.assertTrue(inside(bank["reserved_area"], allocation.rect))
        self.assertEqual(overlaps(plan["entities"]), 0)
        # Nothing of the plan (routes, taps, power) stands on a reserved growth slot.
        self.assertFalse(tiles(plan["entities"]) & growth_tiles(GEOMETRY, [bank]))
        self.assertEqual(plan["growth_entities"], bank["growth_entities"])
        # The raw port never runs out through the bus edge; the product leaves on the bus edge side.
        raw, _coal, product = bank["ports"]
        normal = NORMAL[allocation.input_side]
        vx, vy = FACING[raw["facing"]]
        self.assertLessEqual(vx * normal[0] + vy * normal[1], 0)
        self.assertEqual(state["owner"], kind.owner)
        self.assertEqual([c["owner"] for c in state["connections"]], [kind.owner, kind.owner])
        # The flow model chose the split feed head (each row a full lane); its tunnel is a plan pair.
        self.assertEqual(
            (bank["feed_head"], state["bandwidth"]["head"], state["bandwidth"]["findings"]), ("split", "split", [])
        )
        self.assertTrue(all(p in plan["underground_pairs"] for p in bank["underground_pairs"]))
        return allocation

    def test_steel_block_taps_bus_iron_and_routes_dedicated_coal_on_every_orientation(self):
        seen = set()
        for world_kind in WORLD_KINDS:
            with self.subTest(world=world_kind), TemporaryDirectory() as directory:
                world = World(Path(directory), world_kind)
                sources = material_sources(world)
                steel = controller(world, "steel")
                state = steel.prepare(world.obs)
                self.assertTrue(state["ok"], (world_kind, state.get("reason"), state.get("failures")))
                allocation = self.check_plan(world, state, KINDS["steel"])
                taps = state["taps"]
                self.assertEqual([(t["item"], t["owner"]) for t in taps], [("iron-plate", "steel_production")])
                self.assertEqual(taps[0]["side"], allocation.side)
                self.assertEqual(taps[0]["demand_per_minute"], 37.5)
                coal = state["connections"][1]
                self.assertEqual(coal["source"], sources["coal"]["position"])
                self.assertEqual(state["sources"]["coal"]["unit_number"], sources["coal"]["unit_number"])
                # The supply phase builds the iron route only (no furnace, no coal).
                names = {e["name"] for e in state["supply_plan"]["entities"]}
                self.assertEqual(names, {"transport-belt"})
                self.assertTrue(tiles(state["supply_plan"]["entities"]) <= tiles(state["plan"]["entities"]))
                seen.add(
                    (world.site.plan(WORLD).bus.direction, state["transform"]["rotation"], state["transform"]["mirror"])
                )
        self.assertGreaterEqual(len({s[0] for s in seen}), 2, seen)
        self.assertGreaterEqual(len({s[1:] for s in seen}), 2, seen)

    def test_brick_block_routes_stone_and_brick_coal_without_a_bus_tap(self):
        for world_kind in (None, "rotate"):
            with self.subTest(world=world_kind), TemporaryDirectory() as directory:
                world = World(Path(directory), world_kind)
                sources = material_sources(world)
                brick = controller(world, "brick")
                state = brick.prepare(world.obs)
                self.assertTrue(state["ok"], (world_kind, state.get("reason"), state.get("failures")))
                self.check_plan(world, state, KINDS["brick"])
                self.assertEqual(state["taps"], [])
                self.assertEqual(
                    [c["source"] for c in state["connections"]],
                    [sources["stone"]["position"], sources["brick-coal"]["position"]],
                )
                self.assertEqual(state["rated"]["product_per_minute"], 37.5)

    def test_a_taken_or_missing_material_port_is_a_precise_blocker(self):
        with TemporaryDirectory() as directory:
            world = World(Path(directory), None)
            steel = controller(world, "steel")
            self.assertEqual(steel.prepare(world.obs)["reason"], "dedicated_material_sources_missing")
            sources = material_sources(world)
            board = world.board()
            board["connections"] = [{"item": "coal", "source": sources["coal"]["position"], "owner": "other"}]
            world.write_board(board["blocks"], bus=board["shared_main_bus"])
            self.write_connections(world, board["connections"])
            self.assertEqual(steel.prepare(world.obs)["reason"], "dedicated_material_coal_output_taken")

    def test_every_candidate_failure_reaches_the_job_evidence(self):
        # eval-4177156614: the ledger kept only {"candidates": 4, "kind": "brick"}; the failing
        # connection (coal outlet sealed by the unbuilt steel route) was invisible.
        with TemporaryDirectory() as directory:
            world = World(Path(directory), None)
            sources = material_sources(world)

            def sealed_coal(game, builder, obs, source, destination, reserved=(), **kwargs):
                if source["item"] == "coal":
                    return {
                        "ok": False,
                        "reason": "route_port_escape_reserved_by_unbuilt_plan",
                        "port": "source",
                        "escape": {"x": 1.5, "y": 2.5},
                    }
                return route(game, builder, obs, source, destination, reserved)

            brick = controller(world, "brick")
            brick.route = sealed_coal
            result = brick.production_step(dict(world.obs, enabled_recipes={"stone-brick": True}))
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["reason"], "no_brick_furnace_block_with_complete_connections")
            evidence = result["evidence"]
            self.assertEqual(evidence["candidates"], result["candidates"])
            self.assertTrue(evidence["failures"])
            sealed = [f for f in evidence["failures"] if f["item"] == "coal"]
            self.assertTrue(sealed)
            self.assertEqual(
                {(f["reason"], f["port"]) for f in sealed}, {("route_port_escape_reserved_by_unbuilt_plan", "source")}
            )
            # Candidates across the bus from the stone outlet fail on its long private belt instead.
            self.assertLessEqual(
                {f["reason"] for f in evidence["failures"] if f["item"] != "coal"}, {pf.PRIVATE_ROUTE_TOO_LONG}
            )
            # One row per candidate and reason, not one per orientation.
            self.assertEqual(
                len(evidence["failures"]), len({(f["allocation"], f["item"]) for f in evidence["failures"]})
            )
            self.assertEqual(evidence["sources"]["coal"]["key"], "brick-coal")
            self.assertEqual(evidence["sources"]["raw"]["x"], sources["stone"]["position"]["x"])

    @staticmethod
    def write_connections(world, connections):
        board = world.board()
        board["connections"] = connections
        (world.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")


CORRIDOR_TILES = 100  # longest corridor the stand-in router finds through a built base


def base_route(world):
    """The router as on the fleet seeds: the bus band is closed to every route that is not the
    bus's own (``transport.bus_band``), and a corridor longer than ``CORRIDOR_TILES`` through the
    built base finds no clean trunk corridor (fleet long-1114-0612: every 100-250 tile coal/stone
    route of the failing candidates, ``no_clean_trunk_corridor``)."""
    band = world.bus_rect

    def plan(game, builder, obs, source, destination, reserved=(), **kwargs):
        ay, by = xy(source)[1], xy(destination)[1]
        lo, hi = math.floor(min(ay, by)) - 49, math.ceil(max(ay, by)) + 49  # the stand-in's search box
        walls = [
            {"name": "stone-wall", "position": {"x": x + 0.5, "y": y + 0.5}}
            for x in range(band.x0, band.x1)
            for y in range(max(band.y0, lo), min(band.y1, hi))
        ]
        found = route(
            game, builder, obs, source, destination, list(reserved) + ([] if kwargs.get("bus_route") else walls)
        )
        if not found.get("ok") or found["route_metrics"]["length"] > CORRIDOR_TILES:
            return {"ok": False, "reason": "no_clean_trunk_corridor"}
        return found

    return plan


class SharedMaterialsOnTheBusTests(unittest.TestCase):
    """Fleet long-1114-0612 (4 seeds, 240 game minutes): no seed got past steel/brick. Each block
    routed a private belt from its patch's outlet across the base (coal 90+ tiles west of the bus
    on 649402784 and 3371980627; stone 190-250 tiles away on 2121061131): every candidate failed
    ``no_clean_trunk_corridor`` / a sealed outlet, and the plans that fitted needed 355-482 belts
    and stalled on belt supply. Shared raw materials travel on the main bus instead."""

    FAR = {"coal": (-40, -110), "brick-coal": (-38, -110), "stone": (-40, 110)}

    def world(self, directory):
        world = World(Path(directory), None)
        sources = material_sources(world, far=self.FAR)
        # The dedicated banks publish their outlets as board output ports (bus_lane feed sources).
        board = world.board()
        for key in ("coal", "stone"):
            port = {k: sources[key][k] for k in ("item", "direction", "facing", "kind", "position")}
            board["blocks"][f"{key}_mining"] = {"reserved_area": None, "entities": [], "ports": [port]}
        world.write_board(board["blocks"], bus=board["shared_main_bus"])
        return world, sources

    def planner(self, world, kind):
        planner = controller(world, kind)
        planner.route = base_route(world)
        return planner

    def provide(self, world, result):
        """The provider job the block's needs propose (``bus_lane_nodes``), run to success."""
        needs = plan_needs.from_result(KINDS[result["kind"]].owner, result)
        job = {"capability": KINDS[result["kind"]].owner, "status": "blocked", "needs": needs}
        lanes = bus_lane.receipt(world.root, world.obs)
        nodes = bus_lane.bus_lane_nodes([job], [], {"bus_lanes": lanes}, {bus_lane.CAPABILITY: {}})
        self.assertEqual(len(nodes), 1, (needs, lanes))
        self.build_lane(world, nodes[0]["parameters"])
        return nodes[0]["parameters"]

    def build_lane(self, world, parameters):
        """bus_lane for ``parameters`` planned, built (stand-in feed route) and joined to the bus."""
        item = parameters["item"]

        def feed(game, builder, obs, source, destination, reserved=(), **kwargs):
            return feed_route(game, builder, obs, source, destination, reserved)

        builder = Mock(_occupied_by_plan=GEOMETRY._occupied_by_plan)
        builder.ensure_plan.return_value = {"status": "succeeded"}
        lane = bus_lane.BusLane(
            world.game,
            builder,
            SimpleNamespace(fingerprint="cat"),
            outposts=SimpleNamespace(state={"world_id": WORLD, "banks": []}),
            item=item,
            route=feed,
            check=lambda *a, **k: OK,
        )
        self.assertEqual(lane.step(world.obs, parameters)["status"], "waiting")
        tail = lane.state["trunk"]["tail"]
        built = [e for e in world.obs["entities"]]
        for e in lane.state["plan"]["entities"]:
            built.append(dict(e, belt_inventory={item: 3} if e["position"] == tail else {}))
        world.obs["entities"] = world.built(built)
        result = lane.step(world.obs, parameters)
        self.assertEqual(result["status"], "succeeded", (result, lane.last_proof_failure))
        self.assertTrue(pt.carries(world.board()["shared_main_bus"], item))

    def test_far_coal_is_no_dead_end_but_a_bus_lane_need(self):
        with TemporaryDirectory() as directory:
            world, _sources = self.world(directory)
            result = self.planner(world, "steel").prepare(world.obs)
            # The fleet's failure, reproduced: every candidate's coal belt finds no corridor.
            self.assertEqual(result["reason"], "no_steel_furnace_block_with_complete_connections")
            self.assertEqual(
                {(f["item"], f["reason"]) for f in result["failures"]}, {("coal", "no_clean_trunk_corridor")}
            )
            # The missing connection is a need with a provider job: a coal lane on the bus.
            needs = plan_needs.from_result("steel_supply", result)
            self.assertEqual((needs["kind"], needs["reason"], needs["item"]), ("route", bus_lane.OFF_BUS, "coal"))
            self.assertEqual(self.provide(world, result)["item"], "coal")

    def test_a_long_private_belt_waits_for_the_bus_lane_instead(self):
        # The plans that fitted on the fleet ran 190-250 tile belts (355-482 belts per brick
        # block) and stalled on belt supply: with a lane free on the bus, they are refused.
        with TemporaryDirectory() as directory:
            world, _sources = self.world(directory)
            steel = self.planner(world, "steel")
            steel.route = route  # every corridor found, however long
            result = steel.prepare(world.obs)
            self.assertEqual({f["reason"] for f in result["failures"]}, {pf.PRIVATE_ROUTE_TOO_LONG})
            self.assertEqual(result["plan_needs"]["item"], "coal")

    def test_steel_then_brick_take_stone_and_coal_from_bus_taps_at_their_slot(self):
        with TemporaryDirectory() as directory:
            world, sources = self.world(directory)
            for kind, items, provided in (
                ("steel", ["iron-plate", "coal"], ["coal"]),
                ("brick", ["stone", "coal"], ["stone"]),  # brick: stone first (both failed; raw first)
            ):
                with self.subTest(kind=kind):
                    state = self.planner(world, kind).prepare(world.obs)
                    lanes = []
                    while not state["ok"] and len(lanes) < 3:
                        lanes.append(self.provide(world, state)["item"])
                        state = self.planner(world, kind).prepare(world.obs)
                    self.assertEqual(lanes, provided)
                    self.assertTrue(state["ok"], (state.get("reason"), state.get("failures")))
                    owner = KINDS[kind].owner
                    self.assertEqual(sorted(t["item"] for t in state["taps"]), sorted(items))
                    self.assertTrue(all(t["owner"] == owner for t in state["taps"]))
                    self.assertEqual({s["kind"] for s in state["sources"].values()}, {pf.BUS_TAP})
                    # No belt from a patch outlet: every connection starts at one of the block's taps.
                    far = {(s["position"]["x"], s["position"]["y"]) for s in sources.values()}
                    self.assertFalse({(c["source"]["x"], c["source"]["y"]) for c in state["connections"]} & far)
                    self.assertTrue(all(length <= 40 for length in _lengths(state)), _lengths(state))
                    self.assertEqual(state["taps"][0]["side"], world.site.allocation(state["allocation"], WORLD).side)

    def test_a_built_coal_tap_proves_the_coal_supply(self):
        with TemporaryDirectory() as directory:
            world, _sources = self.world(directory)
            self.build_lane(world, {"item": "coal", "side": "left"})
            steel = self.planner(world, "steel")
            steel.state = dict(steel.prepare(world.obs), phase="verified")
            port = steel.state["bank"]["ports"][1]
            obs = dict(world.obs, entities=[{"name": "transport-belt", "position": port["position"]}])
            self.assertIsNone(steel.coal_proof(obs))
            self.assertEqual(steel.last_proof_failure["reason"], "planned_coal_tap_not_built")
            for tap in steel.state["taps"]:
                tap["built"] = True
            obs["entities"][0]["belt_inventory"] = {"coal": 2}
            evidence = steel.coal_proof(obs)
            self.assertEqual(evidence["coal_source"]["kind"], pf.BUS_TAP)


def _lengths(state):
    return [m.get("length", 0) for m in state["route_metrics"] if m]


class StepTests(unittest.TestCase):
    def test_steel_supply_publishes_the_whole_plan_before_building_the_tap(self):
        with TemporaryDirectory() as directory:
            world = World(Path(directory), None)
            material_sources(world)
            builder = Mock(_occupied_by_plan=GEOMETRY._occupied_by_plan)
            builder.ensure_plan.return_value = {"status": "succeeded"}
            builder.bootstrap.ensure_item.return_value = {"type": "craft", "recipe": "splitter", "count": 1}
            steel = controller(world, "steel", builder)
            action = steel.supply_step(world.obs)
            self.assertEqual(action["type"], "craft")  # tap stock first (planned_tap.cutover)
            board = world.board()
            block = board["blocks"]["steel_production"]
            self.assertEqual(block, steel.state["plan"])
            # Routes leave the reserved area and are published with it; growth slots too.
            outside = [e for e in block["entities"] if not _inside_area(block["reserved_area"], e["position"])]
            self.assertTrue(outside)
            self.assertTrue(block["growth_entities"])
            for connection in steel.state["connections"]:
                self.assertIn(connection, board["connections"])
            names = [s["name"] for s in board["shared_main_bus"]["stations"]]
            self.assertIn("steel_production:iron-plate", names)
            bus_state = json.loads((world.root / pb.STATE_FILE).read_text(encoding="utf-8"))
            self.assertIn("steel_production:iron-plate", [t["name"] for t in bus_state["taps"]])
            builder.ensure_plan.assert_not_called()
            # Production waits for the native steel recipe.
            self.assertEqual(steel.production_step(world.obs)["reason"], "native_furnace_recipe_locked")

    def built_obs(self, world, steel, *, output=None):
        state = steel.state
        kind = steel.kind
        units = iter(range(20000, 90000))
        rows = []
        furnaces = {xy(e) for row in state["bank"]["producer_rows"].values() for e in row}
        out_port, coal_port, raw_port = (state["bank"]["ports"][i]["position"] for i in (2, 1, 0))
        for e in state["plan"]["entities"]:
            row = dict(e, unit_number=next(units))
            if e["name"] in ("inserter", "fast-inserter"):
                row.update(electric_network_connected=True, energy=10)
            if xy(e) in furnaces:
                row.update(
                    products_finished=3,
                    recipe=kind.product,
                    inventory={kind.raw: 5, "coal": 2},
                    output_inventory={kind.product: 1},
                    remaining_burning_fuel=1,
                )
            if e["name"] == "transport-belt" and xy(e) == xy({"position": out_port}):
                row["belt_inventory"] = output or {kind.product: 4}
            if e["name"] == "transport-belt" and xy(e) == xy({"position": coal_port}):
                row["belt_inventory"] = {"coal": 3}
            if e["name"] == "transport-belt" and xy(e) == xy({"position": raw_port}):
                row["belt_inventory"] = {kind.raw: 3}
            rows.append(row)
        by_tile = {xy(r): r for r in rows if r["name"] == "underground-belt"}
        for pair in state["plan"]["underground_pairs"]:
            first, last = by_tile[tuple(pair["input"])], by_tile[tuple(pair["output"])]
            first.update(underground_pair_verified=True, underground_neighbour={"unit_number": last["unit_number"]})
            last["underground_pair_verified"] = True
        source = json.loads((world.root / "material-sources.json").read_text(encoding="utf-8"))["sources"]
        coal = source[kind.coal_key]
        rows.append(
            dict(name="transport-belt", position=coal["position"], direction=4, unit_number=coal["unit_number"])
        )
        return {"world_id": WORLD, "tick": 500, "entities": rows, "enabled_recipes": {kind.product: True}}

    def test_steel_production_proof_and_coal_supply(self):
        with TemporaryDirectory() as directory:
            world = World(Path(directory), None)
            material_sources(world)
            builder = Mock(_occupied_by_plan=GEOMETRY._occupied_by_plan)
            builder.ensure_plan.return_value = {"status": "succeeded"}
            steel = controller(world, "steel", builder)
            steel.state = steel.prepare(world.obs)
            for tap in steel.state["taps"]:
                tap["built"] = True
            steel._save()
            lanes = {"ok": True, "banks": [{"resource": "iron-plate", "lanes": [{"iron-plate": 4}, {"coal": 2}]}]}
            steel.game = SimpleNamespace(cfg=world.game.cfg, query=lambda body: lanes)
            obs = self.built_obs(world, steel)
            self.assertEqual(steel.supply_step(obs)["status"], "succeeded")
            early = steel.coal_supply_step(obs)  # before steel_production's proof: a wait, never a block
            self.assertEqual((early["status"], early["reason"]), ("waiting", "planned_furnace_production_not_verified"))
            done = steel.production_step(obs)
            self.assertEqual(done["status"], "succeeded", (done, steel.last_proof_failure))
            self.assertEqual(done["evidence"]["product_on_port_belt"], 4)
            self.assertEqual(steel.coal_supply_step(obs)["status"], "succeeded")
            # Contaminated output or mixed feed lanes fail the proof.
            self.assertIsNone(steel.proof(self.built_obs(world, steel, output={"steel-plate": 2, "iron-plate": 1})))
            mixed = {
                "ok": True,
                "banks": [{"resource": "iron-plate", "lanes": [{"iron-plate": 4, "coal": 1}, {"coal": 2}]}],
            }
            steel.game = SimpleNamespace(cfg=world.game.cfg, query=lambda body: mixed)
            self.assertIsNone(steel.proof(obs))
            self.assertEqual(steel.last_proof_failure["reason"], "feed_lanes_not_raw_and_coal")


class WiringTests(unittest.TestCase):
    def test_industrial_operations_delegate_on_a_planned_world_only(self):
        with TemporaryDirectory() as directory:
            world = World(Path(directory), None)
            game = SimpleNamespace(cfg=world.game.cfg, query=world.game.query)
            builder = Mock(_occupied_by_plan=GEOMETRY._occupied_by_plan)
            tap = Mock()
            planned = IndustrialOperations(game, builder, catalog(), None, tap=tap, site=world.site)
            self.assertIsNotNone(planned.planned("steel", world.obs))
            self.assertEqual(planned.steel_supply(world.obs)["reason"], "dedicated_material_sources_missing")
            self.assertEqual(planned.brick_production(world.obs)["reason"], "native_furnace_recipe_locked")
            legacy = IndustrialOperations(game, builder, catalog(), None, tap=tap)
            self.assertIsNone(legacy.planned("steel", world.obs))

    def test_roadmap_reaches_steel_after_the_dedicated_coal_bank(self):
        steps = {s.id: s for s in pg.ROADMAP}
        self.assertIn("dedicated_materials_coal", steps["steel_supply"].after)
        self.assertIn("dedicated_materials_coal", steps["brick_production"].after)

        def done(skip=()):
            jobs = []
            for step in pg.ROADMAP:
                if step.id == "steel_supply":
                    return jobs
                if step.id not in skip:
                    jobs.append(
                        {"capability": step.capability, "parameters": dict(step.parameters), "status": "succeeded"}
                    )
            raise AssertionError("steel_supply missing")

        observation = {"enabled_recipes": {"steel-plate": True, "stone-brick": True}}
        ids = [n["id"] for n in pg.autopilot_nodes(done(), [], observation, CAPABILITIES, limit=10)]
        self.assertIn("auto_steel_supply", ids)
        ids = [
            n["id"]
            for n in pg.autopilot_nodes(
                done(skip=("dedicated_materials_coal",)), [], observation, CAPABILITIES, limit=10
            )
        ]
        self.assertNotIn("auto_steel_supply", ids)
        self.assertNotIn("auto_brick_production", ids)


def _inside_area(area, position):
    (x0, y0), (x1, y1) = area
    return x0 <= position["x"] <= x1 and y0 <= position["y"] <= y1


if __name__ == "__main__":
    unittest.main()
