"""Live stall (seed 1000868986): a science cell inserter stood on a mining bank's planned bridge pole.

The bank's saved plan blocked with ``reserved block placement is obstructed``
on that pole forever. These tests pin the recovery (re-plan only the unbuilt
bridge, bounded) and the prevention (sites avoid other blocks' planned but
unbuilt hardware).
"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.board import (
    Board,
    entity,
    external_hardware,
    hardware_conflicts,
    planned_unbuilt_hardware,
)
from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.mining_layout_policy import layout_contract, make_policy
from factorio_agent.outposts import MAX_BRIDGE_REPLANS, OBSTRUCTED, MiningOutposts, bridge_poles
from factorio_agent.paired_mining import mining_pair

GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)
OK_PREFLIGHT = {"ok": True, "clear_obstacles": [], "clearance_world_id": "w"}


def blocked(e):
    """The builder's obstructed report for planned entity ``e``."""
    return {
        "status": "blocked",
        "reason": OBSTRUCTED,
        "evidence": {
            "blockers": [{"name": e["name"], "position": e["position"], "reason": "terrain_or_entity_collision"}],
            "entity": e,
        },
    }


def pending_bank():
    """A pending paired iron bank whose plan carries a two-pole bridge north of its power port."""
    core = mining_pair(0.5, 0.5, 1, "iron-ore")
    bank = dict(plan=core, reserved_pair_slots=2, reserved_area=[[-9, -2], [6, 6]])
    contract = layout_contract(bank, make_policy("w", "c", "iron-ore"))
    port = contract["fixed_power_port"]
    bridge = [
        entity("small-electric-pole", port["x"], port["y"] - 6),
        entity("small-electric-pole", port["x"], port["y"] - 12),
    ]
    metadata = {k: v for k, v in core.items() if k not in ("ok", "reason", "entities", "ports", "required_items")}
    plan = _plan(core["entities"] + bridge, core["ports"], **metadata, layout_contract=deepcopy(contract))
    return dict(bank, plan=plan, ready=False, layout_contract=contract, wire_hops=3, nominal_rate_per_minute=60)


class OutpostBridgeReplanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bank = pending_bank()
        self.port = self.bank["layout_contract"]["fixed_power_port"]
        self.near, self.far = bridge_poles(self.bank)
        board = {
            "world_id": "w",
            "blocks": {
                "startup_science": {
                    "reserved_area": [[30, -40], [46, -24]],
                    "entities": [entity("inserter", 40.5, -30.5)],
                }
            },
        }
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        outposts = MiningOutposts.__new__(MiningOutposts)
        outposts.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root))
        outposts.builder = SimpleNamespace(_occupied_by_plan=GEOMETRY._occupied_by_plan, ensure_plan=Mock())
        outposts.perimeter = None
        outposts.site = None
        outposts.path = self.root / "mining-outposts.json"
        outposts.state = {"world_id": "w", "banks": [self.bank]}
        self.outposts = outposts
        # The far pole (next to the supplied network) is built; an inserter stands on the near one.
        self.obs = {
            "world_id": "w",
            "tick": 5000,
            "technologies": {"electric-mining-drill": True},
            "entities": [
                dict(self.far, unit_number=11),
                dict(entity("inserter", self.near["position"]["x"], self.near["position"]["y"], 4), unit_number=12),
            ],
        }

    def connection(self, x, y):
        """A plan_connection result that reaches the power port through one new pole at (x, y)."""
        goal = entity("small-electric-pole", self.port["x"], self.port["y"])
        return _plan([entity("small-electric-pole", x, y), goal], [], template="power_grid_extension", wire_hops=2)

    def saved(self):
        return json.loads(self.outposts.path.read_text(encoding="utf-8"))["banks"][0]

    def step(self):
        return self.outposts.step(self.obs, {"resource": "iron-ore", "rate_per_minute": 60})

    def test_obstructed_bridge_pole_is_replanned_around_the_inserter_and_building_continues(self):
        before = deepcopy(self.bank)
        build = {"type": "build", "name": "small-electric-pole", "position": {"x": self.port["x"] - 3, "y": -6.0}}
        self.outposts.builder.ensure_plan.side_effect = [blocked(self.near), build]
        new_pole = (self.port["x"] - 3, self.port["y"] - 5)
        with (
            patch("factorio_agent.power_grid.plan_connection", return_value=self.connection(*new_pole)) as power,
            patch("factorio_agent.outposts.preflight", return_value=OK_PREFLIGHT) as check,
        ):
            result = self.step()
        self.assertEqual(result, build)
        saved = self.saved()
        positions = {(e["name"], e["position"]["x"], e["position"]["y"]) for e in saved["plan"]["entities"]}
        self.assertIn(("small-electric-pole", *new_pole), positions)
        self.assertIn(("small-electric-pole", self.far["position"]["x"], self.far["position"]["y"]), positions)
        self.assertNotIn(("small-electric-pole", self.near["position"]["x"], self.near["position"]["y"]), positions)
        for e in before["plan"]["entities"]:
            if e != self.near:
                self.assertIn(e, saved["plan"]["entities"])
        # The second ensure_plan call builds from the persisted plan.
        self.assertEqual(self.outposts.builder.ensure_plan.call_args.args[1], saved["plan"])
        # Derived data: required items, contract, defense geometry, counter and board publication.
        self.assertEqual(
            saved["plan"]["required_items"]["small-electric-pole"],
            sum(e["name"] == "small-electric-pole" for e in saved["plan"]["entities"]),
        )
        self.assertEqual(saved["plan"]["layout_contract"], before["layout_contract"])
        self.assertEqual(saved["layout_contract"], before["layout_contract"])
        self.assertEqual(saved["reserved_area"], before["reserved_area"])
        self.assertEqual(saved["plan"]["ports"], before["plan"]["ports"])
        self.assertEqual(saved["plan"]["clearance_world_id"], "w")
        self.assertEqual(saved["power_bridge_replans"], 1)
        self.assertEqual(saved["wire_hops"], 2)
        history = saved["power_bridge_history"][0]
        self.assertEqual(history["obstructed"], self.near["position"])
        self.assertEqual(history["obstruction"], ["inserter"])
        self.assertEqual(history["kept"], [self.far["position"]])
        board = json.loads((self.root / "pcb-board.json").read_text(encoding="utf-8"))
        self.assertEqual(board["blocks"]["iron-ore_mining"]["entities"], saved["plan"]["entities"])
        self.assertIn("startup_science", board["blocks"])
        # The new chain goes to the fixed power port and keeps off bank hardware and the output access.
        destination, reserved = power.call_args.args[3], power.call_args.args[4]
        self.assertEqual(destination, self.port)
        self.assertFalse(any(e["name"] == "small-electric-pole" for e in reserved))
        for cell in before["layout_contract"]["output_access"]:
            self.assertTrue(any(e["position"] == cell for e in reserved))
        # Whole future footprint first, then the current plan (its clearance is stored).
        self.assertEqual(check.call_count, 2)
        self.assertEqual(check.call_args.args[3], saved["plan"]["entities"])

    def test_second_obstruction_is_replanned_and_the_third_returns_the_blocker(self):
        replies = [self.connection(self.port["x"] - 3, -5.5), self.connection(self.port["x"] + 3, -5.5)]
        with (
            patch("factorio_agent.power_grid.plan_connection", side_effect=replies) as power,
            patch("factorio_agent.outposts.preflight", return_value=OK_PREFLIGHT),
        ):
            for attempt in range(MAX_BRIDGE_REPLANS + 1):
                current = [e for e in bridge_poles(self.outposts.state["banks"][0]) if e != self.far][0]
                self.obs["entities"][1] = dict(
                    entity("inserter", current["position"]["x"], current["position"]["y"]), unit_number=20 + attempt
                )
                report = blocked(current)
                self.outposts.builder.ensure_plan.side_effect = [report, {"type": "build"}]
                result = self.step()
                if attempt < MAX_BRIDGE_REPLANS:
                    self.assertEqual(result, {"type": "build"})
                    self.assertEqual(self.saved()["power_bridge_replans"], attempt + 1)
                else:
                    self.assertIs(result, report)
        self.assertEqual(power.call_count, MAX_BRIDGE_REPLANS)
        self.assertEqual(self.saved()["power_bridge_replans"], MAX_BRIDGE_REPLANS)
        self.assertEqual(len(self.saved()["power_bridge_history"]), MAX_BRIDGE_REPLANS)

    def test_obstructed_drill_returns_the_blocker_unchanged(self):
        drill = next(e for e in self.bank["plan"]["entities"] if e["name"] == "electric-mining-drill")
        self.obs["entities"][1] = dict(
            entity("inserter", drill["position"]["x"], drill["position"]["y"]), unit_number=12
        )
        report = blocked(drill)
        self.outposts.builder.ensure_plan.side_effect = [report]
        with patch("factorio_agent.power_grid.plan_connection") as power:
            self.assertIs(self.step(), report)
        power.assert_not_called()
        self.assertNotIn("power_bridge_replans", self.outposts.state["banks"][0])

    def test_bank_pole_and_obstruction_without_an_entity_are_not_replanned(self):
        core_pole = next(e for e in self.bank["plan"]["entities"] if e["name"] == "small-electric-pole")
        self.assertIn("mining_resource", core_pole)
        self.assertNotIn(core_pole, bridge_poles(self.bank))
        self.obs["entities"].pop()
        report = blocked(self.near)
        self.outposts.builder.ensure_plan.side_effect = [report]
        with patch("factorio_agent.power_grid.plan_connection") as power:
            self.assertIs(self.step(), report)
        power.assert_not_called()

    def test_failed_replan_keeps_the_saved_plan_and_explains_why(self):
        before = deepcopy(self.bank["plan"])
        self.outposts.builder.ensure_plan.side_effect = [blocked(self.near)]
        with patch(
            "factorio_agent.power_grid.plan_connection",
            return_value={"ok": False, "reason": "no_bounded_pole_hop_connection"},
        ):
            result = self.step()
        self.assertEqual(result["reason"], OBSTRUCTED)
        self.assertEqual(result["evidence"]["power_bridge_replan"], "no_bounded_pole_hop_connection")
        self.assertEqual(self.outposts.state["banks"][0]["plan"], before)
        self.assertNotIn("power_bridge_replans", self.outposts.state["banks"][0])


class PlannedHardwareTests(unittest.TestCase):
    def blocks(self):
        return {
            "iron-ore_mining": {
                "reserved_area": [[-10, -2], [6, 6]],
                "entities": [
                    entity("electric-mining-drill", 0.5, 0.5, 8),
                    entity("small-electric-pole", 2.5, -5.5),
                    entity("small-electric-pole", 2.5, -11.5),
                ],
            },
            "startup_science": {"reserved_area": [[30, 4], [46, 20]], "entities": [entity("inserter", 40.5, 14.5)]},
        }

    def test_bridge_poles_outside_reserved_areas_are_planned_hardware_until_built(self):
        obs = {"entities": [dict(entity("small-electric-pole", 2.5, -11.5), unit_number=7)]}
        tiles = planned_unbuilt_hardware(GEOMETRY, obs, self.blocks())
        self.assertEqual(tiles[(2.5, -5.5)]["block"], "iron-ore_mining")
        self.assertNotIn((2.5, -11.5), tiles)
        self.assertIn((0.5, 0.5), tiles)
        self.assertEqual(len([t for t, row in tiles.items() if row["name"] == "electric-mining-drill"]), 9)
        self.assertEqual(tiles[(40.5, 14.5)]["block"], "startup_science")
        mine = planned_unbuilt_hardware(GEOMETRY, obs, self.blocks(), exclude=("startup_science",))
        self.assertNotIn((40.5, 14.5), mine)
        outside = [(name, e["position"]) for name, e in external_hardware(self.blocks())]
        self.assertEqual(
            outside, [("iron-ore_mining", {"x": 2.5, "y": -5.5}), ("iron-ore_mining", {"x": 2.5, "y": -11.5})]
        )

    def test_a_shared_pole_is_not_a_conflict_but_any_other_overlap_is(self):
        tiles = planned_unbuilt_hardware(GEOMETRY, {"entities": []}, self.blocks())
        self.assertEqual(hardware_conflicts(GEOMETRY, [entity("small-electric-pole", 2.5, -5.5)], tiles), [])
        conflicts = hardware_conflicts(GEOMETRY, [entity("inserter", 2.5, -5.5)], tiles)
        self.assertEqual(conflicts, [{"entity": "inserter", "tile": (2.5, -5.5), "block": "iron-ore_mining"}])


class StartupScienceAvoidsPlannedHardwareTests(unittest.TestCase):
    def reserve(self, blocks):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        if blocks:
            board = {"schema_version": 1, "world_id": "w", "catalog_fingerprint": "c", "blocks": blocks}
            (root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        payloads = []

        def query(body):
            plans = json.loads(json.loads(body.split("helpers.json_to_table(", 1)[1].split(");", 1)[0]))
            payloads.append(plans)
            return {"ok": True, "legal": list(range(1, len(plans) + 1)), "roomy": []}

        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root), query=query)
        board = Board(game, GEOMETRY, SimpleNamespace(fingerprint="c"))
        obs = {
            "world_id": "w",
            "entities": [
                {"name": "steam-engine", "position": {"x": 20.5, "y": 10.5}, "energy": 5, "electric_network_id": 1},
                {"name": "small-electric-pole", "position": {"x": 24.5, "y": 10.5}, "electric_network_id": 1},
            ],
        }
        return board.reserve_startup_science(obs), payloads[0]

    def test_candidate_on_another_blocks_planned_bridge_pole_is_refused(self):
        first, every = self.reserve({})
        target = next(e for e in first["entities"] if e["name"] == "inserter")
        pole = entity("small-electric-pole", target["position"]["x"], target["position"]["y"])
        blocks = {"iron-ore_mining": {"reserved_area": [[200, 200], [220, 220]], "entities": [pole]}}
        chosen, offered = self.reserve(blocks)
        self.assertTrue(chosen["ok"])
        self.assertNotEqual(chosen["entities"], first["entities"])
        self.assertLess(len(offered), len(every))
        tiles = planned_unbuilt_hardware(GEOMETRY, {"entities": []}, blocks)
        self.assertEqual(hardware_conflicts(GEOMETRY, chosen["entities"], tiles), [])
        for plan in offered:
            self.assertEqual(hardware_conflicts(GEOMETRY, plan["entities"], tiles), [])


class SiteAllocationAvoidsPlannedHardwareTests(unittest.TestCase):
    def test_allocation_avoids_a_planned_bridge_pole_and_leaves_no_marks(self):
        from test_v2_site_service import service

        from factorio_agent.site.service import BOARD_HARDWARE_OWNER, MAP_FILE

        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            free, _, _ = service(Path(first_dir))
            block = free.allocate("science", 32, 32, name="red", returns_to_bus=False)
            x = block.rect.x0 + 10.5
            y = block.rect.y0 + 10.5
            board = {
                "world_id": "w1",
                "blocks": {
                    "iron-ore_mining": {
                        "reserved_area": [[x + 400, y + 400], [x + 420, y + 420]],
                        "entities": [entity("small-electric-pole", x, y)],
                    }
                },
            }
            (Path(second_dir) / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
            site, _, _ = service(Path(second_dir))
            moved = site.allocate("science", 32, 32, name="red", returns_to_bus=False)
            self.assertNotEqual(moved.rect, block.rect)
            self.assertFalse(moved.rect.x0 <= x < moved.rect.x1 and moved.rect.y0 <= y < moved.rect.y1)
            saved = (Path(second_dir) / MAP_FILE).read_text(encoding="utf-8")
            self.assertNotIn(BOARD_HARDWARE_OWNER, saved)
            self.assertFalse([f for f in site.world().features("plan") if f.owner == BOARD_HARDWARE_OWNER])


if __name__ == "__main__":
    unittest.main()
