"""Staged red science block: small start, reserved slots, evidence-driven growth, published routes."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

from factorio_agent import progression as pg
from factorio_agent import research_capacity as rc
from factorio_agent import science_growth as sg
from factorio_agent.board import foreign_belts, hardware_conflicts, planned_unbuilt_hardware, surface_conflict
from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.jobs import CAPABILITIES
from factorio_agent.planned_bus import trunk_plan
from factorio_agent.science import ROUTES_BLOCK, AutomaticScience, routes_block, validate_growth
from factorio_agent.science import science_block
from factorio_agent.site.busframe import Bus
from factorio_agent.site.planmodel import Lane
from factorio_agent.site.transform import Transform, apply
from factorio_agent.smelting import merge_entities
from factorio_agent.transport import unbuilt_external_keepouts

from test_v2_research_capacity import AMPLE, catalog
from test_v2_template_power import pole_groups

RED = rc.RED_PACK
GEOMETRY = FactoryBuilder.__new__(FactoryBuilder)
# A route belt and a power-link pole outside both blocks (what prepare merges in).
ROUTE = [
    {"name": "transport-belt", "position": {"x": 40.5, "y": 5.5}, "direction": 0},
    {"name": "transport-belt", "position": {"x": 40.5, "y": 4.5}, "direction": 0},
    {"name": "small-electric-pole", "position": {"x": 42.5, "y": 8.5}, "direction": 0},
]


def key(e):
    return (e["name"], e["position"]["x"], e["position"]["y"])


def state(count=4, transform=None, labs=sg.START_LABS, machines=2, machine_limit=None):
    block = science_block(0, 0, count)
    if transform is not None:
        block = {name: dict(apply(transform, plan), site_transform=transform.to_dict()) for name, plan in block.items()}
    row = {
        "world_id": "w",
        "catalog_fingerprint": "catalog",
        "science": block["science"],
        "labs": block["labs"],
        "plan": _plan(merge_entities(block["science"]["entities"], block["labs"]["entities"], ROUTE), []),
        "nominal_machine_rate": 12.0,
        "requested_rate": 30,
        "connections": [],
    }
    row["stage"] = sg.new_stage(row, labs=labs, machines=machines, machine_limit=machine_limit or count)
    return row


TRANSFORMS = [None, Transform(4, None, (16, 16)), Transform(8, "x", (16, 16)), Transform(12, "y", (16, 16))]


class StagedLayoutTests(unittest.TestCase):
    def test_first_build_is_two_labs_and_the_start_machines(self):
        for transform in TRANSFORMS:
            with self.subTest(transform=transform):
                row = state(transform=transform)
                plan = sg.build_plan(row)
                names = [e["name"] for e in plan["entities"]]
                self.assertEqual(names.count("lab"), 2)
                self.assertEqual(sum(e.get("recipe") == RED for e in plan["entities"]), 2)
                # Reserved belts are built in full so growth needs no rerouting.
                belts = [e for e in row["plan"]["entities"] if e["name"] == "transport-belt"]
                self.assertTrue(all(key(e) in {key(x) for x in plan["entities"]} for e in belts))
                deferred = {key(e) for e in row["stage"]["growth_entities"]}
                self.assertEqual(sum(k[0] == "lab" for k in deferred), 6)
                self.assertEqual(sum(k[0] == "assembling-machine-1" for k in deferred), 2)
                self.assertFalse(deferred & {key(e) for e in plan["entities"]})
                self.assertEqual(len(plan["entities"]) + len(deferred), len(row["plan"]["entities"]))

    def test_every_staged_subset_is_one_pole_network_with_the_grid_pole(self):
        for transform in TRANSFORMS:
            for labs in range(1, 9):
                for machines in range(1, 5):
                    row = state(transform=transform)
                    plan = sg.build_plan(row, labs=labs, machines=machines)
                    for name in ("science", "labs"):
                        keys = {key(e) for e in row[name]["entities"]}
                        own = [e for e in plan["entities"] if key(e) in keys]
                        self.assertEqual(len(pole_groups(own)), 1, (transform, name, labs, machines))
                        # The block's first pole is what prepare wires to the grid.
                        first = next(e for e in row[name]["entities"] if e["name"] == "small-electric-pole")
                        self.assertIn(key(first), {key(e) for e in own})

    def test_unbuilt_slots_stay_occupied_for_other_planners(self):
        row = state()
        blocks = {"automation_science": row["science"], "laboratories": row["labs"]}
        obs = {"entities": [dict(e, unit_number=i) for i, e in enumerate(sg.build_plan(row)["entities"], 1)]}
        planned = planned_unbuilt_hardware(GEOMETRY, obs, blocks)
        free_lab = next(e for e in row["stage"]["growth_entities"] if e["name"] == "lab")
        intruder = {"name": "wooden-chest", "position": dict(free_lab["position"]), "direction": 0}
        self.assertTrue(hardware_conflicts(GEOMETRY, [intruder], planned))
        built_lab = sg.built_labs(row)[0]
        self.assertFalse(GEOMETRY._occupied_by_plan([built_lab]) & set(planned))

    def test_start_machines_follow_lab_consumption_and_plates(self):
        cat = catalog()
        fast = [rc.research_target(cat, "automation")]  # 10 s units: two labs eat 12 packs/min
        slow = [rc.research_target(cat, "military")]  # 15 s units: 8 packs/min
        self.assertEqual(sg.start_machines(fast, 6.0, 6), 2)
        self.assertEqual(sg.start_machines(slow, 6.0, 6), 2)
        self.assertEqual(sg.start_machines(fast, 6.0, 1), 1)
        self.assertEqual(sg.start_machines([rc.research_target(cat, "logistic-science-pack")], 6.0, 6), 2)
        self.assertEqual(sg.start_machines([], 6.0, 6), 1)
        thirty = rc.ResearchTarget("t", 10, 30.0, {RED: 1})  # 4 packs/min for two labs
        self.assertEqual(sg.start_machines([thirty], 6.0, 6), 1)

    def test_unstaged_state_keeps_the_full_plan(self):
        row = state()
        row.pop("stage")
        self.assertIs(sg.build_plan(row), row["plan"])
        self.assertEqual(len(sg.built_labs(row)), 8)


def observed(
    row, *, lab_status="working", lab_packs=3, belt_packs=0, machine_status="working", research="fast-inserter"
):
    entities = []
    belt_done = False
    for i, e in enumerate(sg.build_plan(row)["entities"], 1):
        live = dict(deepcopy(e), unit_number=i, electric_network_connected=True, energy=1000, inventory={})
        if e["name"] == "lab":
            live.update(status_name=lab_status, inventory={RED: lab_packs} if lab_packs else {})
        if e.get("recipe") == RED:
            live.update(status_name=machine_status, products_finished=5)
        if e["name"] == "transport-belt" and not belt_done and key(e) in {key(x) for x in row["labs"]["entities"]}:
            live["belt_inventory"] = {RED: belt_packs} if belt_packs else {}
            belt_done = True
        entities.append(live)
    return {"world_id": "w", "tick": 100, "entities": entities, "research": research, "inventory": {}}


def queue():
    cat = catalog()
    return [rc.research_target(cat, name) for name in ("fast-inserter", "logistics", "military")]


class GrowthRuleTests(unittest.TestCase):
    def decide(self, row, obs):
        return sg.growth_decision(catalog(), row, obs, queue(), deepcopy(AMPLE))

    def test_busy_labs_with_backed_up_packs_add_labs_only(self):
        row = state(machines=2)
        decision = self.decide(row, observed(row, lab_packs=3, belt_packs=12))
        self.assertTrue(decision["grow"], decision["reason"])
        self.assertGreater(decision["target_labs"], 2)
        self.assertLessEqual(decision["target_labs"], 4)  # one step adds at most max_step labs
        self.assertEqual(decision["target_machines"], 2)
        json.dumps(decision, allow_nan=False)

    def test_idle_labs_never_get_more_labs(self):
        row = state(machines=2)
        for status in ("missing_science_packs", "no_research_in_progress", "low_power"):
            decision = self.decide(row, observed(row, lab_status=status, lab_packs=0, belt_packs=12))
            self.assertEqual(decision["target_labs"], 2, status)

    def test_starving_labs_add_machines_when_plates_arrive(self):
        row = state(machines=1)
        decision = self.decide(row, observed(row, lab_status="missing_science_packs", lab_packs=0))
        self.assertTrue(decision["grow"], decision["reason"])
        self.assertGreater(decision["target_machines"], 1)
        self.assertEqual(decision["target_labs"], 2)

    def test_starving_labs_with_plate_short_machines_wait(self):
        row = state(machines=1)
        obs = observed(row, lab_status="missing_science_packs", lab_packs=0, machine_status="item_ingredient_shortage")
        decision = self.decide(row, obs)
        self.assertFalse(decision["grow"])
        self.assertIn("plates", decision["reason"])

    def test_busy_labs_without_backlog_and_no_research_do_not_grow(self):
        row = state(machines=2)
        self.assertFalse(self.decide(row, observed(row, lab_packs=1, belt_packs=0))["grow"])
        self.assertFalse(self.decide(row, observed(row, belt_packs=12, research=None))["grow"])

    def test_unpowered_or_missing_built_lab_blocks_growth(self):
        row = state(machines=2)
        obs = observed(row, belt_packs=12)
        next(e for e in obs["entities"] if e["name"] == "lab")["energy"] = 0
        self.assertEqual(self.decide(row, obs)["reason"], "built stage not fully observed and powered")

    def test_machine_growth_stops_at_the_plate_limit(self):
        row = state(machines=2, machine_limit=2)
        decision = self.decide(row, observed(row, lab_status="missing_science_packs", lab_packs=0))
        self.assertFalse(decision["grow"])


class ControllerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root))
        self.builder = SimpleNamespace(ensure_plan=Mock(return_value={"status": "succeeded"}))
        self.science = AutomaticScience(game, self.builder, catalog())

    def test_proof_passes_on_the_built_start(self):
        self.science.state = state(machines=2)
        obs = observed(self.science.state)
        proof = self.science.proof(obs)
        self.assertIsNotNone(proof)
        self.assertEqual(len(proof["lab_units"]), 2)
        self.assertEqual(proof["stage"]["labs"], 2)
        self.science.state.pop("stage")  # the old rule demanded all eight labs
        self.assertIsNone(self.science.proof(obs))

    def test_growth_builds_only_reserved_slots_and_commits_when_observed(self):
        self.science.state = state(machines=2)
        (self.root / "automation-science.json").write_text(json.dumps(self.science.state), encoding="utf-8")
        before = observed(self.science.state)
        result = self.science.grow(before, {"labs": 4, "machines": 2})
        plan = self.builder.ensure_plan.call_args[0][1]
        reserved = {key(e) for e in self.science.state["plan"]["entities"]}
        self.assertTrue({key(e) for e in plan["entities"]} <= reserved)
        new = {key(e) for e in plan["entities"]} - {key(e) for e in sg.build_plan(self.science.state)["entities"]}
        self.assertEqual(
            sorted(k[0] for k in new),
            ["inserter", "inserter", "lab", "lab", "small-electric-pole", "small-electric-pole"],
        )
        # Not observed yet: the stage is not committed.
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(self.science.state["stage"]["labs"], 2)
        grown = dict(self.science.state, stage=dict(self.science.state["stage"], labs=4))
        after = observed(grown)
        result = self.science.grow(after, {"labs": 4, "machines": 2})
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.science.state["stage"]["labs"], 4)
        self.assertEqual(len(self.science.state["stage"]["growth_entities"]), 4 * 3 + 2 * 4)
        saved = json.loads((self.root / "automation-science.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["stage"]["labs"], 4)
        self.assertIsNotNone(self.science.growth_proof(after, {"labs": 4, "machines": 2}))
        self.assertIsNone(self.science.growth_proof(after, {"labs": 5, "machines": 2}))

    def test_step_publishes_routes_with_the_plan_and_retires_them_once_built(self):
        row = state(machines=2)
        self.science.state = row
        board = {"world_id": "w", "catalog_fingerprint": "catalog", "blocks": {}}
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        self.builder.ensure_plan.return_value = {"type": "build", "name": "lab"}
        obs = {"world_id": "w", "tick": 1, "entities": []}
        self.assertEqual(self.science.step(obs, {"rate_per_minute": 30})["type"], "build")
        blocks = json.loads((self.root / "pcb-board.json").read_text(encoding="utf-8"))["blocks"]
        self.assertEqual(set(blocks), {"automation_science", "laboratories", ROUTES_BLOCK})
        self.assertEqual({key(e) for e in blocks[ROUTES_BLOCK]["entities"]}, {key(e) for e in ROUTE})
        # The builder only gets the built stage.
        plan = self.builder.ensure_plan.call_args[0][1]
        self.assertEqual(sum(e["name"] == "lab" for e in plan["entities"]), 2)
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        result = self.science.step(observed(row), {"rate_per_minute": 30})
        self.assertEqual(result["status"], "succeeded")
        blocks = json.loads((self.root / "pcb-board.json").read_text(encoding="utf-8"))["blocks"]
        self.assertNotIn(ROUTES_BLOCK, blocks)

    def test_startup_cell_adds_no_lab_while_a_built_lab_is_idle(self):
        from factorio_agent.industry import _idle_powered_labs

        lab = {"name": "lab", "position": {"x": 0.5, "y": 0.5}, "electric_network_connected": True, "energy": 9}
        obs = {"research": "logistics", "entities": [dict(lab, status_name="missing_science_packs", inventory={})]}
        self.assertTrue(_idle_powered_labs(obs))
        self.assertFalse(_idle_powered_labs(dict(obs, research=None)))  # between batches the time model decides
        obs["entities"][0]["status_name"] = "working"
        self.assertFalse(_idle_powered_labs(obs))

    def test_growth_never_exceeds_slots_and_validates(self):
        self.science.state = state(machines=2, machine_limit=2)
        obs = observed(self.science.state)
        self.assertEqual(
            self.science.grow(obs, {"labs": 2, "machines": 3})["reason"], "science_growth_exceeds_reserved_slots"
        )
        for bad in ({"labs": 0, "machines": 1}, {"labs": 2}, {"labs": 2, "machines": 1.0}):
            with self.assertRaises(ValueError):
                validate_growth(bad)
        self.assertIn("science_growth", CAPABILITIES)


class AutopilotTests(unittest.TestCase):
    capabilities = {"science_growth": {"requires": ["automation_science"]}}
    done = [{"capability": "automation_science", "status": "succeeded"}]

    def obs(self, grow=True):
        return {"science_growth": {"grow": grow, "target_labs": 4, "target_machines": 2, "reason": "r"}}

    def test_growth_node_is_low_priority_and_single(self):
        nodes = pg.science_growth_nodes(self.done, [], self.obs(), self.capabilities)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["parameters"], {"labs": 4, "machines": 2})
        self.assertLess(nodes[0]["priority"], pg.TOP_PRIORITY - len(pg.ROADMAP))
        validate_growth(nodes[0]["parameters"])
        self.assertEqual(pg.science_growth_nodes(self.done, [], self.obs(False), self.capabilities), [])
        self.assertEqual(pg.science_growth_nodes([], [], self.obs(), self.capabilities), [])
        open_job = self.done + [{"capability": "science_growth", "status": "running", "parameters": {}}]
        self.assertEqual(pg.science_growth_nodes(open_job, [], self.obs(), self.capabilities), [])
        stalled = self.done + [
            {"capability": "science_growth", "status": "blocked", "parameters": {"labs": 4, "machines": 2}}
        ]
        self.assertEqual(pg.science_growth_nodes(stalled, [], self.obs(), self.capabilities), [])


class PublishedRoutesTests(unittest.TestCase):
    """Two capabilities planned back to back before either is built (seed 283895142, iron onto copper)."""

    def bus(self):
        return Bus((30, 0), "E", 6, 32, (Lane(0, 0, 0, "copper-plate"), Lane(1, 0, 1, "iron-plate")))

    def routes(self):
        # Science's planned northbound iron line crossing the copper lane row (y = 0.5).
        belts = [
            {"name": "transport-belt", "position": {"x": 45.5, "y": y + 0.5}, "direction": 0} for y in range(3, -4, -1)
        ]
        row = state()
        row["plan"] = _plan(merge_entities(row["plan"]["entities"], belts), [])
        return row, belts

    def test_routes_block_lists_routes_outside_both_blocks_with_a_one_tile_area(self):
        row, belts = self.routes()
        block = routes_block(row)
        keys = {key(e) for e in block["entities"]}
        self.assertTrue({key(e) for e in belts + ROUTE} <= keys)
        self.assertFalse(keys & {key(e) for e in row["science"]["entities"] + row["labs"]["entities"]})
        (x0, y0), (x1, y1) = block["reserved_area"]
        self.assertEqual((x1 - x0, y1 - y0), (1.0, 1.0))
        hard = unbuilt_external_keepouts(GEOMETRY, {"entities": []}, {ROUTES_BLOCK: block})
        self.assertIn((45.5, 0.5), hard)

    def test_trunk_planned_after_science_crosses_its_unbuilt_route_underground(self):
        row, belts = self.routes()
        bus = self.bus()
        lane = bus.lanes[0]
        blocks = {ROUTES_BLOCK: routes_block(row)}
        obs = {"entities": []}
        # The old rule (observed belts only) put a trunk belt on the route's tile.
        old = trunk_plan(bus, lane, 32, [], 4)
        tiles = {(e["position"]["x"], e["position"]["y"]) for e in old["entities"]}
        self.assertIn((45.5, 0.5), tiles)
        new = trunk_plan(bus, lane, 32, foreign_belts(obs, blocks), 4)
        self.assertTrue(new["ok"], new)
        surface = {(e["position"]["x"], e["position"]["y"]) for e in new["entities"]}
        self.assertNotIn((45.5, 0.5), surface)
        self.assertTrue(new["underground_pairs"])
        self.assertIsNone(surface_conflict(GEOMETRY, obs, blocks, new["entities"]))

    def test_a_planned_belt_ending_beside_the_trunk_is_a_side_load(self):
        bus = self.bus()
        feeder = [
            {"name": "transport-belt", "position": {"x": 45.5, "y": 2.5}, "direction": 0},
            {"name": "transport-belt", "position": {"x": 45.5, "y": 1.5}, "direction": 0},
        ]
        blocks = {"other": _plan(feeder, [], reserved_area=[[60, 60], [61, 61]])}
        obs = {"entities": []}
        # With planned belts as foreign belts, the crossing compiler refuses the side-load itself.
        refused = trunk_plan(bus, bus.lanes[0], 32, foreign_belts(obs, blocks), 4)
        self.assertEqual(refused["reason"], "foreign_surface_belt_would_feed_route")
        # The board check catches it for any line compiled without them.
        trunk = trunk_plan(bus, bus.lanes[0], 32, [], 4)
        clash = surface_conflict(GEOMETRY, obs, blocks, trunk["entities"])
        self.assertEqual(clash["reason"], "planned_belt_would_side_load_line")
        built = {"entities": [dict(e, unit_number=i) for i, e in enumerate(feeder, 1)]}
        self.assertIsNone(surface_conflict(GEOMETRY, built, blocks, trunk["entities"]))


if __name__ == "__main__":
    unittest.main()
