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


# Packs-limited like eval-4177156614 at 82 min (research_capacity: "packs-limited: labs 524s, packs 727s"):
# 7.2 s units, two labs eat 16.7 packs/min (packs/labs = 1.39 as there), two machines make 12.
FAST_QUEUE = [rc.ResearchTarget("fast", 300, 7.2, {RED: 1})]


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

    def test_busy_labs_without_backlog_grow_labs_only_when_labs_limit(self):
        row = state(machines=2)
        # The fixture queue is labs-limited (research_capacity): every lab works, so labs may follow.
        labs_limited = self.decide(row, observed(row, lab_packs=1, belt_packs=0))
        self.assertGreaterEqual(labs_limited["target_labs"], 2)
        self.assertEqual(labs_limited["target_machines"], 2)
        # A packs-limited queue adds no lab without a backlog.
        packs_limited = sg.growth_decision(
            catalog(), row, observed(row, lab_packs=1, belt_packs=0), FAST_QUEUE, deepcopy(AMPLE)
        )
        self.assertEqual(packs_limited["target_labs"], 2)
        self.assertFalse(self.decide(row, observed(row, belt_packs=12, research=None))["grow"])

    def test_unpowered_or_missing_built_lab_blocks_growth(self):
        row = state(machines=2)
        obs = observed(row, belt_packs=12)
        next(e for e in obs["entities"] if e["name"] == "lab")["energy"] = 0
        self.assertEqual(self.decide(row, obs)["reason"], "built stage not fully observed and powered")

    def live(self, row, statuses, packs, belt):
        """eval-4177156614 at 82 min: lab_status [missing_science_packs, working], lab_packs [0, 2], belt 2."""
        obs = observed(row, belt_packs=belt)
        labs = [e for e in obs["entities"] if e["name"] == "lab"]
        for lab, status, n in zip(labs, statuses, packs):
            lab.update(status_name=status, inventory={RED: n} if n else {})
        return obs

    def test_a_lab_idle_for_packs_grows_the_pack_machines_not_the_labs(self):
        row = state(machines=2)
        obs = self.live(row, ["missing_science_packs", "working"], [0, 2], 2)
        decision = sg.growth_decision(catalog(), row, obs, FAST_QUEUE, deepcopy(AMPLE))
        self.assertTrue(decision["grow"], decision["reason"])
        self.assertEqual(decision["chain"], "packs")
        self.assertEqual(decision["target_machines"], 3)
        self.assertEqual(decision["target_labs"], 2)
        self.assertEqual(decision["permitted"], {"labs": False, "machines": True})

    def test_labs_follow_once_packs_are_no_longer_the_limit(self):
        row = state(machines=3)
        decision = self.decide(row, self.live(row, ["working", "working"], [3, 3], 12))
        self.assertTrue(decision["grow"], decision["reason"])
        self.assertEqual(decision["chain"], "labs")
        self.assertGreater(decision["target_labs"], 2)

    def test_pack_shortage_with_plate_short_machines_points_at_the_plate_supply(self):
        row = state(machines=2)
        obs = self.live(row, ["missing_science_packs", "working"], [0, 2], 2)
        for e in obs["entities"]:
            if e.get("recipe") == RED:
                e["status_name"] = "item_ingredient_shortage"
        decision = self.decide(row, obs)
        self.assertFalse(decision["grow"])
        self.assertEqual(decision["chain"], "plates")
        self.assertIn("throughput_growth", decision["reason"])

    def test_labs_idle_for_demand_hold_every_growth(self):
        row = state(machines=2)
        decision = self.decide(row, self.live(row, ["no_research_in_progress", "working"], [0, 2], 2))
        self.assertFalse(decision["grow"])
        self.assertEqual(decision["chain"], "demand")

    def test_machine_growth_stops_at_the_plate_limit(self):
        row = state(machines=2, machine_limit=2)
        obs = self.live(row, ["missing_science_packs", "working"], [0, 2], 2)
        # No net plates and no stock: the live plate limit is the built count.
        thin = {item: {"available": 0, "produced_per_minute": 30, "net_per_minute": 0} for item in AMPLE}
        decision = sg.growth_decision(catalog(), row, obs, FAST_QUEUE, thin)
        self.assertFalse(decision["grow"])
        self.assertEqual(decision["frame"]["plate_limit"], 2)
        self.assertIn("plate supply carries no further machine", decision["reason"])


class FrameTests(unittest.TestCase):
    """The reserved frame: sized from the research bill, extended for legacy blocks, limit read live."""

    def test_plan_reserves_the_frame_from_the_research_bill_and_builds_the_start(self):
        cat = catalog()
        bill = [rc.research_target(cat, name) for name in ("fast-inserter", "logistics", "military")] + FAST_QUEUE
        slots = sg.frame_size(cat, bill, requested=2)
        self.assertGreater(slots, 2)
        self.assertLessEqual(slots, sg.SITE_MACHINE_SLOTS)
        self.assertEqual(sg.frame_size(cat, [], requested=2), 2)  # nothing queued: the requested row
        self.assertEqual(sg.frame_size(cat, bill, requested=6), 6)
        row = state(count=slots)
        built = sg.build_plan(row)
        self.assertEqual(sum(e.get("recipe") == RED for e in built["entities"]), 2)
        self.assertEqual(sum(e["name"] == "lab" for e in built["entities"]), sg.START_LABS)
        self.assertEqual(row["stage"]["machine_slots"], slots)

    def test_a_legacy_two_slot_block_has_a_six_slot_frame_in_its_reserved_square(self):
        for transform in TRANSFORMS:
            with self.subTest(transform=transform):
                row = state(count=2, transform=transform, machines=2, machine_limit=2)
                self.assertEqual(sg.frame_capacity(row), sg.SITE_MACHINE_SLOTS)
                grown = sg.extended_state(row, sg.SITE_MACHINE_SLOTS)
                full = state(count=sg.SITE_MACHINE_SLOTS, transform=transform)
                # The extension is exactly the six-slot row of the same square.
                self.assertEqual(
                    {key(e) for e in grown["science"]["entities"]}, {key(e) for e in full["science"]["entities"]}
                )
                self.assertEqual(grown["science"]["count"], 6)
                self.assertEqual(grown["stage"]["machine_slots"], 6)
                self.assertEqual(grown["stage"]["machines"], 2)  # nothing new is built by the reservation
                self.assertEqual(
                    {key(e) for e in sg.build_plan(grown)["entities"]}, {key(e) for e in sg.build_plan(row)["entities"]}
                )
                self.assertEqual(len(grown["frame"]["replaces"]), 1)
                for machines in range(1, 7):
                    plan = sg.build_plan(grown, machines=machines)
                    keys = {key(e) for e in grown["science"]["entities"]}
                    self.assertEqual(len(pole_groups([e for e in plan["entities"] if key(e) in keys])), 1)

    def test_a_blocked_extension_falls_back_to_the_reserved_slots(self):
        row = state(count=2, machines=2, machine_limit=2)
        row["frame"] = {"extension_blocked": "ore_under_slot"}
        self.assertEqual(sg.frame_capacity(row), 2)

    def test_limit_follows_the_plate_supply_now(self):
        per = {"iron-plate": 12.0, "copper-plate": 6.0}
        none = {
            "iron-plate": {"available": 0, "net_per_minute": 0},
            "copper-plate": {"available": 0, "net_per_minute": 0},
        }
        self.assertEqual(sg.plate_machine_limit(2, per, none, 600), 2)
        some = {
            "iron-plate": {"available": 0, "net_per_minute": 25},
            "copper-plate": {"available": 0, "net_per_minute": 40},
        }
        self.assertEqual(sg.plate_machine_limit(2, per, some, 600), 4)
        # Stock spread over the remaining research: 120 iron over 600 s = 12/min, one more machine.
        stock = {
            "iron-plate": {"available": 120, "net_per_minute": 0},
            "copper-plate": {"available": 60, "net_per_minute": 0},
        }
        self.assertEqual(sg.plate_machine_limit(2, per, stock, 600), 3)
        self.assertEqual(sg.plate_machine_limit(2, per, {}, 600), 2)  # unknown supply adds nothing

    def live_82(self, row):
        """eval-4177156614 at 82 min: lab_status [missing_science_packs, working], lab_packs [0, 2], belt 2."""
        obs = observed(row, belt_packs=2)
        for lab, status, n in zip([e for e in obs["entities"] if e["name"] == "lab"], PACK_STATUSES, (0, 2)):
            lab.update(status_name=status, inventory={RED: n} if n else {})
        return obs

    def test_the_82_minute_receipt_grows_a_third_machine_into_the_legacy_frame(self):
        row = state(count=2, machines=2, machine_limit=2)  # the live block: 2 slots, limit 2
        decision = sg.growth_decision(catalog(), row, self.live_82(row), FAST_QUEUE, deepcopy(AMPLE))
        self.assertTrue(decision["grow"], decision["reason"])
        self.assertEqual(decision["target_machines"], 3)
        self.assertEqual(decision["target_labs"], 2)
        self.assertEqual(decision["frame"]["extend_to"], 6)
        json.dumps(decision, allow_nan=False)

    def test_frame_full_turns_to_the_second_block_or_rebuild_decision(self):
        row = state(count=6, machines=6, machine_limit=6)
        decision = sg.growth_decision(catalog(), row, self.live_82(row), [FAST_QUEUE[0]] * 4, deepcopy(AMPLE))
        self.assertFalse(decision["grow"])
        step = decision["next_step"]
        self.assertIn(step["choice"], ("wait", "second_block_1", "second_block_2"))
        self.assertFalse(step["implemented"])
        self.assertEqual([o["option"] for o in step["options"]], ["second_block_1", "second_block_2"])
        self.assertIn("frame is built", decision["reason"])
        json.dumps(decision, allow_nan=False)
        # A long packs-limited bill pays for the second row.
        long = [rc.ResearchTarget("long", 3000, 1.0, {RED: 1})]  # two labs eat 120/min, six machines make 36
        step = sg.growth_decision(catalog(), row, self.live_82(row), long, deepcopy(AMPLE))["next_step"]
        self.assertEqual(step["choice"], "second_block_2", step["reason"])

    def test_rebuild_is_an_option_once_faster_machines_are_unlocked(self):
        cat = catalog()
        cat.entities["assembling-machine-2"] = {"crafting_speed": 0.75}
        cat.recipes["assembling-machine-2"] = {
            "name": "assembling-machine-2",
            "energy": 0.5,
            "ingredients": [{"name": "iron-plate", "amount": 20}],
            "products": [{"name": "assembling-machine-2", "amount": 1}],
        }
        row = state(count=6, machines=6, machine_limit=6)
        obs = dict(self.live_82(row), enabled_recipes={"assembling-machine-2": True})
        long = [rc.ResearchTarget("long", 3000, 1.0, {RED: 1})]
        step = sg.growth_decision(cat, row, obs, long, deepcopy(AMPLE))["next_step"]
        rebuild = next(o for o in step["options"] if o["option"] == "rebuild")
        self.assertEqual(rebuild["pack_rate_per_minute"], 54.0)  # 6 machines x 6/min x 0.75/0.5
        self.assertEqual(step["choice"], "rebuild", step["reason"])

    def test_declared_draw_rises_with_the_growth(self):
        row = state(count=2, machines=2, machine_limit=2)
        row["taps"] = [
            {"name": "automation_science:iron-plate", "item": "iron-plate", "demand_per_minute": 24.0},
            {"name": "automation_science:copper-plate", "item": "copper-plate", "demand_per_minute": 12.0},
        ]
        self.assertEqual(sg.undeclared_plate_draw(row), {"iron-plate": 0.0, "copper-plate": 0.0})
        receipt = {"grow": True, "target_machines": 3}
        self.assertEqual(sg.undeclared_plate_draw(row, receipt), {"iron-plate": 12.0, "copper-plate": 6.0})
        grown = dict(row, stage=dict(row["stage"], machines=4))
        self.assertEqual(sg.undeclared_plate_draw(grown), {"iron-plate": 24.0, "copper-plate": 12.0})
        self.assertEqual(sg.undeclared_plate_draw(dict(row, taps=[]), receipt), {})


PACK_STATUSES = ("missing_science_packs", "working")


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

    def test_startup_cell_stops_growing_once_the_bus_science_block_takes_over(self):
        from factorio_agent.industry import permanent_science_due

        obs = {"world_id": "w"}
        self.assertIsNone(permanent_science_due(self.root, obs))
        ledger = {"world_id": "w", "jobs": {"a": {"capability": "planned_main_bus", "status": "running"}}}
        (self.root / "jobs.json").write_text(json.dumps(ledger), encoding="utf-8")
        self.assertIsNone(permanent_science_due(self.root, obs))
        ledger["jobs"]["a"]["status"] = "succeeded"
        (self.root / "jobs.json").write_text(json.dumps(ledger), encoding="utf-8")
        self.assertEqual(permanent_science_due(self.root, obs), "bus_science_fundable")
        (self.root / "jobs.json").unlink()
        (self.root / "automation-science.json").write_text(json.dumps({"world_id": "w"}), encoding="utf-8")
        self.assertEqual(permanent_science_due(self.root, obs), "permanent_science_block_planned")
        self.assertIsNone(permanent_science_due(self.root, {"world_id": "other"}))

    def test_startup_cell_adds_no_lab_while_a_built_lab_is_idle(self):
        from factorio_agent.industry import _idle_powered_labs

        lab = {"name": "lab", "position": {"x": 0.5, "y": 0.5}, "electric_network_connected": True, "energy": 9}
        obs = {"research": "logistics", "entities": [dict(lab, status_name="missing_science_packs", inventory={})]}
        self.assertTrue(_idle_powered_labs(obs))
        self.assertFalse(_idle_powered_labs(dict(obs, research=None)))  # between batches the time model decides
        obs["entities"][0]["status_name"] = "working"
        self.assertFalse(_idle_powered_labs(obs))

    def board(self, row):
        board = {
            "world_id": "w",
            "catalog_fingerprint": "catalog",
            "blocks": {"automation_science": row["science"], "laboratories": row["labs"]},
        }
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")

    def test_a_legacy_block_republishes_its_frame_then_grows_to_three_machines_and_four_labs(self):
        from unittest.mock import patch

        row = state(count=2, machines=2, machine_limit=2)
        self.science.state = row
        self.board(row)
        clear = {"ok": True, "clear_obstacles": [{"name": "tree-01", "position": {"x": 30.5, "y": 20.5}}]}
        with patch("factorio_agent.science.preflight", return_value=clear) as checked:
            result = self.science.grow(observed(row), {"labs": 2, "machines": 3})
        self.assertEqual(result["status"], "waiting")
        added = checked.call_args[0][3]
        self.assertEqual(sum(e["name"] == "assembling-machine-1" for e in added), 4)  # the whole frame reserved
        saved = json.loads((self.root / "automation-science.json").read_text(encoding="utf-8"))
        board = json.loads((self.root / "pcb-board.json").read_text(encoding="utf-8"))
        self.assertEqual(board["blocks"]["automation_science"], saved["science"])
        self.assertEqual(saved["stage"]["machine_slots"], 6)
        self.assertEqual(saved["stage"]["machines"], 2)  # not committed before it is observed
        self.assertIn("tree-01", [e["name"] for e in saved["plan"]["clear_obstacles"]])
        plan = self.builder.ensure_plan.call_args[0][1]
        self.assertEqual(sum(e.get("recipe") == RED for e in plan["entities"]), 3)
        grown = dict(self.science.state, stage=dict(self.science.state["stage"], machines=3))
        self.assertEqual(self.science.grow(observed(grown), {"labs": 2, "machines": 3})["status"], "succeeded")
        self.assertEqual(self.science.state["stage"]["machines"], 3)
        # Labs follow once every lab works with packs backing up: 2 -> 4 lab slots.
        busy = observed(self.science.state, lab_packs=3, belt_packs=12)
        decision = sg.growth_decision(catalog(), self.science.state, busy, queue(), deepcopy(AMPLE))
        self.assertEqual(decision["target_labs"], 4, decision["reason"])
        self.assertEqual(self.science.grow(busy, {"labs": 4, "machines": 3})["status"], "waiting")
        four = dict(self.science.state, stage=dict(self.science.state["stage"], labs=4))
        self.assertEqual(self.science.grow(observed(four), {"labs": 4, "machines": 3})["status"], "succeeded")
        self.assertEqual((self.science.state["stage"]["labs"], self.science.state["stage"]["machines"]), (4, 3))
        # step() accepts the republished block (the board holds the extended plan).
        self.builder.ensure_plan.return_value = {"type": "build", "name": "lab"}
        self.assertEqual(
            self.science.step({"world_id": "w", "tick": 2, "entities": []}, {"rate_per_minute": 10})["type"], "build"
        )

    def test_a_board_still_holding_the_replaced_block_is_republished(self):
        row = state(count=2, machines=2, machine_limit=2)
        self.board(row)
        self.science.state = sg.extended_state(row, 6)
        self.builder.ensure_plan.return_value = {"type": "build", "name": "lab"}
        self.assertEqual(
            self.science.step({"world_id": "w", "tick": 2, "entities": []}, {"rate_per_minute": 10})["type"], "build"
        )
        board = json.loads((self.root / "pcb-board.json").read_text(encoding="utf-8"))
        self.assertEqual(board["blocks"]["automation_science"]["count"], 6)
        # A block another state wrote is still refused.
        board["blocks"]["automation_science"] = state(count=3)["science"]
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        self.assertEqual(
            self.science.step({"world_id": "w", "tick": 3, "entities": []}, {"rate_per_minute": 10})["reason"],
            "science_reserved_block_changed",
        )

    def test_a_frame_that_cannot_be_placed_turns_to_the_second_block_path(self):
        from unittest.mock import patch

        row = state(count=2, machines=2, machine_limit=2)
        self.science.state = row
        self.board(row)
        with patch("factorio_agent.science.preflight", return_value={"ok": False, "reason": "ore_under_slot"}):
            result = self.science.grow(observed(row), {"labs": 2, "machines": 3})
        self.assertEqual(result["reason"], "science_frame_extension_blocked")
        self.assertEqual(sg.frame_capacity(self.science.state), 2)
        self.builder.ensure_plan.assert_not_called()

    def test_growth_never_exceeds_slots_and_validates(self):
        self.science.state = state(machines=2, machine_limit=2)
        obs = observed(self.science.state)
        self.assertEqual(
            self.science.grow(obs, {"labs": 2, "machines": 7})["reason"], "science_growth_exceeds_reserved_slots"
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
