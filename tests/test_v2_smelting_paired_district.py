"""Fresh-map smelting district: paired banks whose reserved growth slots stay free."""

import json
import unittest
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from factorio_agent import progression as pg
from factorio_agent.board import planned_unbuilt_hardware
from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.diagnostics import smelting_throughput_bounds
from factorio_agent.planned_bus import feed_branch
from factorio_agent.plate_supply import plate_sources
from factorio_agent.power_grid import _foreign_block_points
from factorio_agent.site.fakeworld import make_world
from factorio_agent.site.masterplan import plan_site
from factorio_agent.site.transform import apply, variants
from factorio_agent.smelting import (
    BANK_SLOTS,
    SmeltingDistrict,
    bank_capacities,
    feed_lane_positions,
    grown_bank,
    growth_tiles,
    paired_furnace_bank,
)
from factorio_agent.smelting_site import DISTRICT_SIZE, district_candidate, oriented, plan_candidates
from factorio_agent.transport import _production_keepouts, xy

GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)
COUNTS = {"iron-ore": 4, "copper-ore": 2}
SEEDS = (1, 2, 4, 7)  # all four bus directions, plain and mirrored districts (test_v2_planned_bus)
PLANS = {seed: plan_site(make_world(seed, size=512)) for seed in SEEDS}
VECTORS = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}
CATALOG = SimpleNamespace(
    fingerprint="c",
    recipes={
        "iron-plate": {"energy": 3.2, "products": [{"name": "iron-plate", "amount": 1}]},
        "copper-plate": {"energy": 3.2, "products": [{"name": "copper-plate", "amount": 1}]},
    },
    entities={"stone-furnace": {"crafting_speed": 1, "type": "furnace"}},
)


def occupied(entities):
    return GEOMETRY._occupied_by_plan(list(entities))


def every_candidate():
    """Every district candidate the plan offers on every seed, plus all 8 turns of the canonical one."""
    for seed, master in PLANS.items():
        for candidate in plan_candidates(master, COUNTS):
            yield f"seed {seed} {candidate['site_transform']}", candidate
    for t in variants((24, 16)):
        yield f"variant {t}", oriented(apply(t, district_candidate(8, 8, COUNTS)), t)


def lane_side(belt, point):
    """+1 when ``point`` is right of the belt's travel direction, -1 when left."""
    vx, vy = VECTORS[belt["direction"]]
    dx, dy = point[0] - belt["position"]["x"], point[1] - belt["position"]["y"]
    return 1 if vx * dy - vy * dx > 0 else -1


def bfs_route(game, builder, obs, source, destination, reserved, guide=None):
    """A plain shortest belt path (stand-in for the router).

    Reserved non-belts are hard; reserved belts may be crossed and are left
    out of the result (the real router passes them underground).
    """
    a, b = xy(source), xy(destination)
    crossed = {xy(e) for e in reserved if e["name"] == "transport-belt"} - {a, b}
    hard = occupied([e for e in reserved if e["name"] != "transport-belt"]) - {a, b}
    x0, x1 = min(a[0], b[0]) - 48, max(a[0], b[0]) + 48
    y0, y1 = min(a[1], b[1]) - 48, max(a[1], b[1]) + 48
    parents, queue = {a: None}, deque([a])
    while queue:
        cell = queue.popleft()
        if cell == b:
            break
        for dx, dy in VECTORS.values():
            nxt = (cell[0] + dx, cell[1] + dy)
            if nxt in parents or nxt in hard or not (x0 <= nxt[0] <= x1 and y0 <= nxt[1] <= y1):
                continue
            parents[nxt] = cell
            queue.append(nxt)
    if b not in parents:
        return {"ok": False, "reason": "fake_route_blocked"}
    path = [b]
    while parents[path[-1]] is not None:
        path.append(parents[path[-1]])
    path.reverse()
    direction = {v: k for k, v in VECTORS.items()}
    belts = []
    for here, there in zip(path, path[1:]):
        belts.append({"name": "transport-belt", "position": {"x": here[0], "y": here[1]}, "direction": 0})
        belts[-1]["direction"] = direction[(there[0] - here[0], there[1] - here[1])]
    belts.append({"name": "transport-belt", "position": dict(destination["position"]), "direction": 4})
    belts[-1]["direction"] = destination["facing"]
    belts = [e for e in belts[1:] if xy(e) not in crossed]
    return _plan(belts, [], underground_pairs=[], route_metrics={"length": len(belts)})


class BankTemplateTests(unittest.TestCase):
    def test_full_bank_fills_both_output_lanes_and_feeds_every_inserter(self):
        bank = paired_furnace_bank(0, 0, BANK_SLOTS, "iron-ore")
        output = {xy(e): e for e in bank["entities"] if e["name"] == "transport-belt" and xy(e)[1] == 6.5}
        feeds = {(p["x"], p["y"]) for p in bank["feed_belts"]}
        inserters = {xy(e): e for e in bank["entities"] if e["name"] == "inserter"}
        sides = {}
        for row, furnaces in bank["producer_rows"].items():
            self.assertEqual(len(furnaces), BANK_SLOTS)
            for furnace in furnaces:
                fx, fy = xy(furnace)
                for (ix, iy), ins in inserters.items():
                    if ix != fx + 0.5 or abs(iy - fy) != 1.5:
                        continue
                    vx, vy = VECTORS[ins["direction"]]
                    pickup, drop = (ix + vx, iy + vy), (ix - vx, iy - vy)
                    if pickup in feeds:  # input inserter: feed belt -> furnace
                        self.assertIn(drop, occupied([furnace]))
                    else:  # output inserter: furnace -> shared output belt
                        self.assertIn(pickup, occupied([furnace]))
                        self.assertIn(drop, output)
                        sides.setdefault(row, set()).add(lane_side(output[drop], (ix, iy)))
        # An inserter drops onto the far lane: the two rows sit on opposite sides.
        self.assertEqual(len(sides), 2)
        self.assertTrue(all(len(s) == 1 for s in sides.values()))
        self.assertEqual(sides["right"] | sides["left"], {1, -1})
        self.assertEqual(bank_capacities(CATALOG, [bank])["iron-ore"], 450)

    def test_bank_overlap_free_ports_on_edges_and_room_beside_the_plate_port(self):
        for per_row in (1, 2, BANK_SLOTS):
            bank = paired_furnace_bank(0, 0, per_row, "copper-ore")
            everything = bank["entities"] + bank["growth_entities"]
            boxes = [occupied([e]) for e in everything]
            self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))
            self.assertEqual(xy(bank["ports"][0]), (0.5, 0.5))
            self.assertEqual(xy(bank["ports"][-1]), (47.5, 6.5))
            # Bus feed splitter and branch (planned_bus.feed_branch) need the last four columns.
            self.assertFalse({(x, y) for x in (44.5, 45.5, 46.5, 47.5) for y in (5.5, 7.5)} & set().union(*boxes))
            self.assertEqual(len(feed_lane_positions(bank)), 2 * (3 * per_row - 1))

    def test_growth_builds_only_reserved_slots_and_keeps_ports(self):
        bank = paired_furnace_bank(0, 0, 2, "iron-ore")
        grown = grown_bank(bank, 5)
        added = [e for e in grown["entities"] if e not in bank["entities"]]
        self.assertTrue(added)
        self.assertTrue(all(e in bank["growth_entities"] for e in added))
        self.assertTrue(occupied(added) <= growth_tiles(GEOMETRY, [bank]))
        self.assertEqual(grown["ports"], bank["ports"])
        self.assertEqual((grown["per_side_count"], grown["count"]), (5, 10))
        self.assertEqual(len(grown["growth_entities"]) + len(added), len(bank["growth_entities"]))
        self.assertEqual(grown_bank(grown, 3)["entities"], grown["entities"])
        with self.assertRaises(ValueError):
            grown_bank(bank, BANK_SLOTS + 1)


class DistrictReservationTests(unittest.TestCase):
    def test_every_orientation_keeps_growth_free_and_bus_feeds_beside_the_ports(self):
        count = 0
        for label, candidate in every_candidate():
            count += 1
            built = occupied(candidate["entities"])
            growth = growth_tiles(GEOMETRY, candidate["banks"])
            (x0, y0), (x1, y1) = candidate["area"]
            self.assertEqual(sorted((x1 - x0, y1 - y0)), sorted(DISTRICT_SIZE), label)
            self.assertFalse(built & growth, label)
            self.assertTrue(all(x0 < x < x1 and y0 < y < y1 for x, y in growth | built), label)
            for bank in candidate["banks"]:
                self.assertTrue(occupied(bank["growth_entities"]) <= growth, label)
            plan = _plan(
                candidate["entities"],
                [b["ports"][-1] for b in candidate["banks"]],
                reserved_area=candidate["area"],
                site_transform=candidate["site_transform"],
            )
            for port in plan["ports"]:
                feed = feed_branch(plan, port)
                cells = occupied([feed["splitter"], *feed["belts"]])
                self.assertFalse(cells & growth, label)
                self.assertFalse((cells - {xy({"position": feed["anchor"]})}) & built - {xy(port)}, label)
            with TemporaryDirectory() as root:
                board = {"world_id": "w", "blocks": {"smelting_district": plan}, "connections": []}
                found = plate_sources(root, board, "w", "c", owner="automation_science")
            self.assertTrue(found["ok"], label)
        self.assertGreater(count, 8)

    def test_district_feed_routes_and_poles_avoid_growth_on_every_seed(self):
        for seed, master in PLANS.items():
            with TemporaryDirectory() as directory:
                result, calls = prepare(Path(directory), master)
            self.assertTrue(result["ok"], (seed, result))
            growth = growth_tiles(GEOMETRY, result["banks"])
            self.assertTrue(growth)
            self.assertFalse(occupied(result["plan"]["entities"]) & growth, seed)
            self.assertEqual(len(result["plan"]["growth_entities"]), 2 * 2 * BANK_SLOTS * 7 - 6 * 7, seed)
            # Split feed heads (flow-model choice): their undergrounds are plan pairs (underground_proof).
            self.assertEqual(result["bandwidth"]["head"], "split", seed)
            head_pairs = [p for b in result["banks"] for p in b["underground_pairs"]]
            self.assertEqual(len(head_pairs), 2, seed)
            self.assertTrue(all(p in result["plan"]["underground_pairs"] for p in head_pairs), seed)
            for reserved in calls["belt"] + calls["power"]:
                self.assertTrue(growth <= {xy(e) for e in reserved} | occupied(reserved), seed)
            # The district -> bus head corridor is hard for the district's routes and poles and
            # holds nothing of the district (all-1113-2332 seeds 3872030632, 2304149747).
            from factorio_agent.smelting_site import bus_header_tiles

            header = bus_header_tiles(master)
            self.assertTrue(header, seed)
            for reserved in calls["belt"] + calls["power"]:
                self.assertTrue(header <= {xy(e) for e in reserved}, seed)
            self.assertFalse(occupied(result["plan"]["entities"]) & header, seed)

    def test_later_routes_poles_and_sites_of_other_blocks_avoid_the_growth_footprint(self):
        site = district_candidate(8, 8, COUNTS)
        plan = _plan(
            site["entities"],
            [b["ports"][-1] for b in site["banks"]],
            reserved_area=site["area"],
            growth_entities=[e for b in site["banks"] for e in b["growth_entities"]],
        )
        growth = growth_tiles(GEOMETRY, site["banks"])
        obs = {"world_id": "w", "entities": []}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            board = {"world_id": "w", "blocks": {"smelting_district": plan}, "connections": []}
            (root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
            game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root))
            # A belt from east of the district to its west: the whole reservation is hard.
            source = {"kind": "item", "item": "stone", "position": {"x": 70.5, "y": 20.5}, "facing": 12}
            destination = dict(source, position={"x": -20.5, "y": 20.5}, direction="input")
            bounds = {"min_x": -30, "max_x": 80, "min_y": -20, "max_y": 60}
            keepouts = _production_keepouts(
                game, GEOMETRY, obs, dict(source, direction="output"), destination, [], bounds
            )
            self.assertTrue(keepouts["ok"], keepouts)
            self.assertTrue(growth <= keepouts["hard"])
            # Foreign power poles stay out of the reservation.
            self.assertEqual(_foreign_block_points(game, GEOMETRY, (100.5, 100.5), growth), growth)
        # Concurrent site choices see the reserved slots as planned hardware.
        planned = planned_unbuilt_hardware(GEOMETRY, obs, {"smelting_district": plan})
        self.assertTrue(growth & occupied(plan["growth_entities"]) <= set(planned))


def prepare(root, master):
    """``SmeltingDistrict.prepare`` on ``master`` with the BFS stand-in router; records reserved args."""
    mining = {"world_id": "w", "banks": []}
    for resource in ("iron-ore", "copper-ore", "coal"):
        zone = master.zone("mining:" + resource)
        cx, cy = zone.rect.centre() if zone else master.zone("smelting").rect.centre()
        position = {"x": float(int(cx)) + 0.5, "y": float(int(cy)) + 0.5}
        port = {"item": resource, "kind": "item", "direction": "output", "facing": 4, "position": position}
        mining["banks"].append(
            {"ready": True, "nominal_rate_per_minute": 60, "plan": {"resource": resource, "ports": [port]}}
        )
    (root / "mining-outposts.json").write_text(json.dumps(mining), encoding="utf-8")
    board = {"world_id": "w", "catalog_fingerprint": "c", "blocks": {}}
    (root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
    game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root), query=Mock(return_value={"ok": True, "legal": [1]}))
    routine = SmeltingDistrict(game, None, GEOMETRY, CATALOG, site=SimpleNamespace(plan=Mock(return_value=master)))
    calls = {"belt": [], "power": []}

    def belts(game, builder, obs, source, destination, reserved, **kwargs):
        calls["belt"].append(list(reserved))
        return bfs_route(game, builder, obs, source, destination, reserved)

    def power(game, builder, obs, goal, reserved):
        calls["power"].append(list(reserved))
        return _plan([], [])

    with (
        patch("factorio_agent.smelting.belt_connection", side_effect=belts),
        patch("factorio_agent.smelting.power_connection", side_effect=power),
        patch("factorio_agent.smelting.preflight", return_value={"ok": True, "clear_obstacles": []}),
    ):
        result = routine.prepare({"world_id": "w", "technologies": {"logistics": True}, "entities": []})
    return result, calls


class CapacityGrowthTests(unittest.TestCase):
    def district(self, root):
        result, _calls = prepare(root, PLANS[1])
        self.assertTrue(result["ok"], result)
        state = dict(result, automatic_feed_baseline={"tick": 0})
        (root / "smelting-district.json").write_text(json.dumps(state), encoding="utf-8")
        board = {"world_id": "w", "catalog_fingerprint": "c", "blocks": {"smelting_district": state["plan"]}}
        (root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        mining = json.loads((root / "mining-outposts.json").read_text(encoding="utf-8"))
        for bank in mining["banks"]:
            bank["nominal_rate_per_minute"] = 450
        (root / "mining-outposts.json").write_text(json.dumps(mining), encoding="utf-8")
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root), query=Mock(return_value={"ok": False}))
        builder = FactoryBuilder.__new__(FactoryBuilder)
        builder.ensure_plan = Mock(return_value={"status": "waiting", "reason": "building"})
        return SmeltingDistrict(game, None, builder, CATALOG), state

    def test_capacity_step_adds_reserved_slots_in_place(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            routine, before = self.district(root)
            with patch("factorio_agent.smelting.preflight", return_value={"ok": True, "clear_obstacles": []}):
                result = routine.capacity_step(
                    {"world_id": "w", "entities": []}, {"resource": "iron-ore", "rate_per_minute": 150}
                )
            self.assertEqual(result["status"], "waiting", result)
            old_bank = before["banks"][0]
            new_bank = routine.state["banks"][0]
            self.assertEqual(new_bank["per_side_count"], 4)
            old = {(e["name"], *xy(e)) for e in before["plan"]["entities"]}
            added = [e for e in routine.state["plan"]["entities"] if (e["name"], *xy(e)) not in old]
            self.assertEqual(sum(e["name"] == "stone-furnace" for e in added), 4)
            self.assertTrue(all(e in old_bank["growth_entities"] for e in added))
            self.assertEqual(routine.state["plan"]["ports"], before["plan"]["ports"])
            self.assertEqual(routine.state["nominal_smelting_capacity_per_minute"]["iron-ore"], 150)
            board = json.loads((root / "pcb-board.json").read_text(encoding="utf-8"))
            self.assertEqual(board["blocks"]["smelting_district"], routine.state["plan"])
            self.assertEqual(len(routine.state["plan"]["growth_entities"]), len(before["plan"]["growth_entities"]) - 28)

    def test_a_full_bank_asks_for_another_bank(self):
        with TemporaryDirectory() as directory:
            routine, _ = self.district(Path(directory))
            routine.catalog = SimpleNamespace(**{**vars(CATALOG), "fingerprint": "c"})
            routine.catalog.entities = {"stone-furnace": {"crafting_speed": 0.5, "type": "furnace"}}
            result = routine.capacity_step(
                {"world_id": "w", "entities": []}, {"resource": "copper-ore", "rate_per_minute": 300}
            )
        self.assertEqual(result["reason"], "paired_bank_full_next_growth_is_another_bank")


class GrowthProposalTests(unittest.TestCase):
    CAPABILITIES = {"smelting_capacity": {"requires": ["smelting_district"]}}
    JOBS = [{"capability": "smelting_district", "status": "succeeded"}]

    def row(self, claimed, nominal=75.0, ore=450.0):
        return {
            "product": "iron-plate",
            "resource": "iron-ore",
            "nominal_capacity_per_minute": nominal,
            "full_bank_capacity_per_minute": 450.0,
            "claimed_bus_demand_per_minute": claimed,
            "connected_nominal_ore_supply": ore,
        }

    def nodes(self, row, jobs=JOBS, queue=()):
        observation = {"smelting_throughput_bounds": {"banks": [row]}}
        return pg.smelting_capacity_nodes(jobs, list(queue), observation, self.CAPABILITIES)

    def test_claims_near_the_bank_grow_it_within_ore_and_slots(self):
        self.assertEqual(self.nodes(self.row(40)), [])
        node = self.nodes(self.row(70))[0]
        self.assertEqual(node["parameters"], {"resource": "iron-ore", "rate_per_minute": 87.5})
        self.assertEqual(self.nodes(self.row(900))[0]["parameters"]["rate_per_minute"], 450.0)
        self.assertEqual(self.nodes(self.row(900, ore=120))[0]["parameters"]["rate_per_minute"], 120.0)
        self.assertEqual(self.nodes(self.row(900, nominal=450)), [])
        self.assertEqual(self.nodes(self.row(900, ore=60)), [])

    def test_one_growth_row_per_resource_and_autopilot_wiring(self):
        open_job = {
            "capability": "smelting_capacity",
            "status": "blocked",
            "parameters": {"resource": "iron-ore", "rate_per_minute": 90},
        }
        self.assertEqual(self.nodes(self.row(70), jobs=self.JOBS + [open_job]), [])
        observation = {"smelting_throughput_bounds": {"banks": [self.row(70)]}}
        proposed = pg.autopilot_nodes(self.JOBS, [], observation, self.CAPABILITIES)
        self.assertEqual([n["capability"] for n in proposed], ["smelting_capacity"])

    def test_receipt_rows_carry_nominal_full_and_claimed_rates(self):
        bank = paired_furnace_bank(0, 0, 2, "iron-ore")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "smelting-district.json").write_text(
                json.dumps({"world_id": "w", "banks": [bank]}), encoding="utf-8"
            )
            mining = {"world_id": "w", "banks": []}
            (root / "mining-outposts.json").write_text(json.dumps(mining), encoding="utf-8")
            stations = [
                {"item": "iron-plate", "demand_per_minute": 30},
                {"item": "copper-plate", "demand_per_minute": 9},
            ]
            board = {"world_id": "w", "shared_main_bus": {"stations": stations}}
            (root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
            bounds = smelting_throughput_bounds(root, {"world_id": "w", "entities": []}, CATALOG)
        row = bounds["banks"][0]
        self.assertEqual(
            (
                row["nominal_capacity_per_minute"],
                row["full_bank_capacity_per_minute"],
                row["claimed_bus_demand_per_minute"],
            ),
            (75.0, 450.0, 30.0),
        )


if __name__ == "__main__":
    unittest.main()
