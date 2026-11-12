"""Growing a built one-machine startup science cell to two science machines between batches."""

from copy import deepcopy
import json
import math
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from factorio_agent import research_capacity as rc
from factorio_agent.board import (
    LAB_SLOT_OFFSETS,
    SCIENCE_EXTENSION_LAYOUT,
    entity,
    extended_science_cell,
    is_cell_growth,
    lab_slot_entities,
    plan_hash,
    science_extension_entities,
    science_extension_error,
    science_machine_count,
    startup_science_cell,
)
from factorio_agent.industry import CAPACITY_FILE, BootstrapLogistics, _hash

from test_v2_research_capacity import PICKUP, catalog, footprint, owner

RED = rc.RED_PACK
AREA = [[24.0, -8.0], [40.0, 8.0]]


def one_machine(x=32.5, y=0.5, area=AREA):
    """The original cell as older versions saved it (no science_machines or cell_origin)."""
    return dict(startup_science_cell(x, y), reserved_area=deepcopy(area))


def assert_powered(test, entities):
    """Every machine, inserter and lab touches a pole's 5x5 area; all poles form one wire network."""
    poles = [e for e in entities if e["name"] == "small-electric-pole"]
    for e in entities:
        if e["name"] in ("small-electric-pole", "wooden-chest"):
            continue
        covered = any(
            abs(tx - p["position"]["x"]) < 3 and abs(ty - p["position"]["y"]) < 3
            for tx, ty in footprint(e)
            for p in poles
        )
        test.assertTrue(covered, e)
    reached = {0}
    while True:
        more = {
            j
            for i in reached
            for j, q in enumerate(poles)
            if math.dist(
                (poles[i]["position"]["x"], poles[i]["position"]["y"]), (q["position"]["x"], q["position"]["y"])
            )
            <= 7.5
        }
        if more <= reached:
            break
        reached |= more
    test.assertEqual(len(reached), len(poles))


class ExtensionGeometryTests(unittest.TestCase):
    def setUp(self):
        self.old = one_machine()
        self.grown = extended_science_cell(self.old)
        self.entities = self.grown["entities"]

    def test_growth_is_a_strict_superset_with_one_more_output_port(self):
        self.assertIsNone(science_extension_error(self.old))
        self.assertEqual(self.entities[: len(self.old["entities"])], self.old["entities"])
        self.assertEqual(self.grown["ports"][:3], self.old["ports"])
        self.assertEqual([p["direction"] for p in self.grown["ports"]], ["input", "input", "output", "output"])
        self.assertTrue(is_cell_growth(self.old, self.grown))
        self.assertFalse(is_cell_growth(self.grown, self.old))
        self.assertEqual(science_machine_count(self.grown), 2)
        self.assertEqual(self.grown["science_extension"], SCIENCE_EXTENSION_LAYOUT)
        self.assertEqual(self.grown["cell_origin"], {"x": 32.5, "y": 0.5})
        self.assertEqual(self.grown["reserved_area"], AREA)
        added = self.grown["required_items"]
        self.assertEqual(added, {"assembling-machine-1": 3, "wooden-chest": 6, "inserter": 9, "small-electric-pole": 3})
        # The hub layout and an already grown cell do not grow again.
        self.assertIsNotNone(science_extension_error(self.grown))
        self.assertIsNotNone(science_extension_error(startup_science_cell(32.5, 0.5, science_machines=2)))

    def test_no_overlaps_inside_the_reservation_and_lab_slots_unchanged(self):
        slots = lab_slot_entities(self.grown)
        self.assertEqual(
            [s["lab"]["position"] for s in slots], [s["lab"]["position"] for s in lab_slot_entities(self.old)]
        )
        self.assertEqual(len(slots), len(LAB_SLOT_OFFSETS))
        # The one-machine slot pole is still planned for the grown cell.
        self.assertEqual(slots[0]["poles"], lab_slot_entities(self.old)[0]["poles"])
        everything = self.entities + slots[0]["poles"] + [s["lab"] for s in slots]
        boxes = [footprint(e) for e in everything]
        self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))
        (x0, y0), (x1, y1) = AREA
        for tx, ty in set().union(*boxes):
            self.assertTrue(x0 <= tx - 0.5 and tx + 0.5 <= x1 and y0 <= ty - 0.5 and ty + 0.5 <= y1)
        assert_powered(self, everything)

    def test_every_inserter_moves_from_its_source_to_its_destination(self):
        def at(name, dx, dy):
            return next(
                e for e in self.entities if e["name"] == name and e["position"] == {"x": 32.5 + dx, "y": 0.5 + dy}
            )

        gear = at("assembling-machine-1", 0, -4)
        first = at("assembling-machine-1", 0, 0)
        second = at("assembling-machine-1", -6, -3)
        iron, copper, out = at("wooden-chest", -3, -4), at("wooden-chest", -3, 0), at("wooden-chest", 0, 3)
        relay_gear, relay_copper, out2 = (
            at("wooden-chest", -3, -3),
            at("wooden-chest", -5, 0),
            at("wooden-chest", -6, 0),
        )
        expected = {
            (-2, -4): (iron, gear),
            (-2, 0): (copper, first),
            (0, -2): (gear, first),
            (0, 2): (first, out),
            (-2, -3): (gear, relay_gear),
            (-4, -3): (relay_gear, second),
            (-4, 0): (copper, relay_copper),
            (-5, -1): (relay_copper, second),
            (-6, -1): (second, out2),
        }
        inserters = [e for e in self.entities if e["name"] == "inserter"]
        self.assertEqual(len(inserters), len(expected))
        for ins in inserters:
            px, py = ins["position"]["x"], ins["position"]["y"]
            dx, dy = PICKUP[ins["direction"]]
            source, destination = expected[(px - 32.5, py - 0.5)]
            self.assertIs(owner(self.entities, (px + dx, py + dy)), source, ins)
            self.assertIs(owner(self.entities, (px - dx, py - dy)), destination, ins)
        self.assertEqual(second["recipe"], RED)
        self.assertEqual(self.grown["ports"][3]["position"], out2["position"])

    def test_collisions_area_and_moved_entities_refuse_growth(self):
        pole = entity("small-electric-pole", 32.5 - 7, 0.5 - 1)
        identical = deepcopy(self.old)
        identical["entities"].append(pole)
        self.assertIsNone(science_extension_error(identical))
        chain = deepcopy(self.old)
        chain["entities"].append(entity("small-electric-pole", 32.5 - 5, 0.5 - 3))
        self.assertEqual(science_extension_error(chain), "science_cell_extension_layout_collides")
        shrunk = dict(self.old, reserved_area=[[27.0, -8.0], [40.0, 8.0]])
        self.assertEqual(science_extension_error(shrunk), "science_cell_extension_outside_reserved_area")
        moved = deepcopy(self.old)
        moved["entities"][2]["position"]["x"] += 1
        self.assertEqual(science_extension_error(moved), "science_cell_extension_original_layout_differs")
        self.assertIsNone(extended_science_cell(moved))
        self.assertTrue(science_extension_entities(self.old))


class LabFallbackTests(unittest.TestCase):
    def decide(self, name):
        costs = rc.expansion_costs(catalog(), RED)
        materials = rc.ResearchMaterials(
            evidence={"iron-plate": {"available": 5000}, "copper-plate": {"available": 5000}},
            assembler_cost={RED: costs["assembler"]},
            lab_cost=costs["lab"],
            machines_per_assembler={RED: 1},
        )
        hardware = rc.ResearchHardware({RED: 1}, {RED: 6.0}, 1)
        config = rc.ResearchCapacityConfig(max_assemblers=2, max_labs=4)
        target = rc.research_target(catalog(), name)
        return rc.plan_research_capacity([target], hardware, materials, config=config).as_dict()

    def test_labs_alone_never_follow_a_pack_limited_decision(self):
        packs = self.decide("logistic-science-pack")
        self.assertEqual((packs["target_assemblers"], packs["target_labs"]), (2, 1))
        self.assertEqual(rc.labs_without_assemblers(packs), 1)
        labs = self.decide("fast-inserter")
        self.assertEqual((labs["target_assemblers"], labs["target_labs"]), (2, 3))
        self.assertEqual(rc.labs_without_assemblers(labs), 2)
        self.assertEqual(rc.labs_without_assemblers({"current_labs": 2}), 2)


class ExtensionBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.game = SimpleNamespace(
            cfg=SimpleNamespace(runtime_dir=self.root), query=Mock(side_effect=AssertionError("no native query"))
        )
        self.catalog = catalog()
        self.bootstrap = SimpleNamespace(
            ensure_item=Mock(side_effect=lambda obs, name, count: dict(type="take", item=name, count=count))
        )
        self.builder = SimpleNamespace(ensure_plan=Mock(side_effect=self.ensure), _sync=Mock())
        self.blocked = None
        self.units = 0
        self.obs = dict(
            ok=True,
            world_id="w",
            tick=1000,
            entities=[],
            inventory={"iron-plate": 2000, "copper-plate": 1000},
            production={RED: {"produced": 100, "consumed": 30}},
            research=None,
            technologies={"automation": True, "automation-science-pack": True},
            enabled_recipes={
                name: True for name in ("assembling-machine-1", "inserter", "lab", RED, "iron-gear-wheel")
            },
        )
        self.old = one_machine(10.5, 10.5, [[2.0, 2.0], [18.0, 18.0]])
        board = dict(world_id="w", catalog_fingerprint="catalog", blocks={"startup_science": self.old})
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        self.add(self.old["entities"])
        self.obs["entities"].append(
            dict(
                name="lab",
                unit_number=500,
                position={"x": 0.5, "y": 0.5},
                electric_network_connected=True,
                energy=1000,
                inventory={},
            )
        )
        roadmap = patch("factorio_agent.progression.roadmap_wanted_technologies", return_value=[])
        roadmap.start()
        self.addCleanup(roadmap.stop)

    def add(self, specs):
        for spec in specs:
            self.units += 1
            self.obs["entities"].append(
                dict(
                    deepcopy(spec),
                    unit_number=self.units,
                    electric_network_connected=True,
                    energy=1000,
                    products_finished=100,
                    inventory={},
                )
            )

    def find(self, spec):
        return next(
            (e for e in self.obs["entities"] if e["name"] == spec["name"] and e["position"] == spec["position"]),
            None,
        )

    def ensure(self, obs, plan):
        """A builder stand-in: one build action per missing entity of an extension plan."""
        if plan.get("template") != "startup_science_extension":
            return {"status": "succeeded"}
        if self.blocked:
            return {"status": "blocked", "reason": self.blocked}
        missing = [e for e in plan["entities"] if self.find(e) is None]
        if missing:
            return {"type": "build", "name": missing[0]["name"], "position": missing[0]["position"]}
        return {"status": "succeeded"}

    def build_requested(self):
        """Place everything the last extension plan asked for."""
        plan = next(
            c.args[1]
            for c in reversed(self.builder.ensure_plan.call_args_list)
            if c.args[1].get("template") == "startup_science_extension"
        )
        self.add([e for e in plan["entities"] if self.find(e) is None])
        return plan

    def extension_calls(self):
        return [
            c
            for c in self.builder.ensure_plan.call_args_list
            if c.args[1].get("template") == "startup_science_extension"
        ]

    def make(self, technology):
        return BootstrapLogistics(self.game, self.bootstrap, self.builder, self.catalog, technology=technology)

    def decide(self, technology, **row):
        saved = {}
        path = self.root / CAPACITY_FILE
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
        saved.setdefault("world_id", "w")
        saved.setdefault("decisions", {})[technology] = row
        path.write_text(json.dumps(saved), encoding="utf-8")

    def saved_decision(self, technology):
        return json.loads((self.root / CAPACITY_FILE).read_text(encoding="utf-8"))["decisions"][technology]

    def board(self):
        return json.loads((self.root / "pcb-board.json").read_text(encoding="utf-8"))

    def fund(self, controller):
        for name in ("iron-plate", "copper-plate"):
            action = controller.step(self.obs)
            self.assertEqual((action["type"], action["item"]), ("insert", name))
            controller.record_action(action, {"ok": True, "moved": action["count"]})

    def finish(self, controller, packs_per_machine):
        for pin in controller._red_pins():
            self.find(pin)["products_finished"] += packs_per_machine
        self.obs["production"][RED]["consumed"] += packs_per_machine
        self.obs["technologies"][controller.technology] = True

    def grow(self, controller):
        """Step until the batch binds, placing whatever the extension plan requests."""
        action = controller.step(self.obs)
        while action.get("type") == "build":
            self.build_requested()
            action = controller.step(self.obs)
        return action

    def test_pack_limited_example_ends_with_two_machines_and_no_extra_lab(self):
        controller = self.make("logistic-science-pack")
        first = controller.step(self.obs)
        self.assertEqual(first["type"], "build")
        decision = self.saved_decision("logistic-science-pack")
        self.assertEqual((decision["target_assemblers"], decision["target_labs"]), (2, 1))
        self.assertEqual(decision["bottleneck_before"], "packs")
        plan = self.build_requested()
        self.assertFalse(any(e["name"] == "lab" for e in plan["entities"]))
        action = controller.step(self.obs)
        self.assertEqual((action["type"], action["item"]), ("insert", "iron-plate"))
        board = self.board()
        grown = board["blocks"]["startup_science"]
        self.assertEqual(science_machine_count(grown), 2)
        self.assertEqual(board["lineage"]["startup_science"], [_hash(self.old)])
        self.assertEqual(plan_hash(self.old), _hash(self.old))
        state = controller.state
        self.assertEqual(state["plan_hash"], _hash(grown))
        self.assertEqual(len(state["red_machines"]), 2)
        self.assertEqual(len(state["ports"]), 4)
        self.assertNotIn("labs", state)
        self.assertEqual(sum(e["name"] == "lab" for e in self.obs["entities"]), 1)

    def test_lab_limited_example_builds_labs_and_assembler_together(self):
        controller = self.make("fast-inserter")
        controller.step(self.obs)
        decision = self.saved_decision("fast-inserter")
        self.assertEqual((decision["target_assemblers"], decision["target_labs"]), (2, 3))
        plan = self.build_requested()
        names = [e["name"] for e in plan["entities"]]
        self.assertEqual(names.count("lab"), 2)
        self.assertEqual(names.count("assembling-machine-1"), 3)
        self.fund(controller)
        self.assertEqual(len(controller.state["red_machines"]), 2)
        self.assertEqual(len(controller.state["labs"]), 3)
        self.assertEqual(len(self.extension_calls()), 2)

    def test_new_batch_after_growth_collects_both_outputs_and_proof_sums_both_machines(self):
        controller = self.make("logistic-science-pack")
        for name in ("iron-plate", "copper-plate"):
            action = self.grow(controller)
            self.assertEqual((action["type"], action["item"]), ("insert", name))
            controller.record_action(action, {"ok": True, "moved": action["count"]})
        self.obs["research"] = "logistic-science-pack"
        grown = self.board()["blocks"]["startup_science"]
        first, second = (self.find({"name": "wooden-chest", "position": p["position"]}) for p in grown["ports"][2:])
        first["inventory"][RED] = 2
        second["inventory"][RED] = 4
        action = controller.step(self.obs)
        self.assertEqual((action["type"], action["position"], action["count"]), ("take", second["position"], 4))
        controller.record_action(action, {"ok": True, "moved": 4})
        self.finish(controller, 38)
        proof = controller.proof(self.obs)
        self.assertEqual(proof["normally_assembled_packs"], 76)
        self.assertEqual(proof["plan_hash"], _hash(grown))

    def test_running_batch_blocks_growth_and_a_finished_proof_survives_it(self):
        self.decide("fast-inserter", target_assemblers=1, target_labs=1, current_labs=1)
        running = self.make("fast-inserter")
        self.fund(running)
        later = self.make("logistic-science-pack")
        self.assertEqual(later._extension_refusal(self.obs), "science_cell_extension_batch_unfinished")
        self.assertIsNone(later._grow_cell(self.obs, later._startup_plan(self.obs)))
        self.assertEqual(self.extension_calls(), [])
        self.assertNotIn("lineage", self.board())
        # The running batch is finished; the cell may now grow under it.
        self.finish(running, 30)
        before = running.proof(self.obs)
        self.assertIsNotNone(before)
        self.assertIsNone(later._extension_refusal(self.obs))
        self.grow(later)
        self.assertEqual(self.board()["lineage"]["startup_science"], [running.state["plan_hash"]])
        after = running.proof(self.obs)
        self.assertEqual(after["normally_assembled_packs"], before["normally_assembled_packs"])
        self.assertEqual(after["plan_hash"], _hash(self.old))
        self.assertEqual(running.step(self.obs)["status"], "succeeded")
        # Only a finished batch may use the lineage: unfinished, the old hash is refused.
        self.obs["technologies"]["fast-inserter"] = False
        self.assertEqual(
            running._binding_error(self.obs, running._startup_plan(self.obs)),
            "bootstrap_research_startup_plan_changed",
        )
        self.assertEqual(running.step(self.obs)["reason"], "bootstrap_research_startup_plan_changed")
        # A replaced bound machine still invalidates the historical proof.
        self.obs["technologies"]["fast-inserter"] = True
        self.find(running.state["red_machine"])["unit_number"] += 1000
        self.assertIsNone(running.proof(self.obs))

    def write_state(self, name, **state):
        (self.root / name).write_text(json.dumps(dict(world_id="w", **state)), encoding="utf-8")

    def test_refusals_pending_transfer_prewarm_other_world_and_unreadable(self):
        controller = self.make("logistic-science-pack")
        self.obs["technologies"]["gun-turret"] = True
        self.write_state(
            "bootstrap-research-gun-turret.json", technology="gun-turret", pending_transfer={"kind": "lab"}
        )
        self.assertEqual(controller._extension_refusal(self.obs), "science_cell_extension_transfer_unresolved")
        self.write_state("bootstrap-research-gun-turret.json", technology="gun-turret")
        self.assertIsNone(controller._extension_refusal(self.obs))
        self.write_state("bootstrap-logistics.json", technology="logistics", prewarm={"owner_technology": "x"})
        self.assertEqual(controller._extension_refusal(self.obs), "science_cell_extension_prewarm_in_progress")
        (self.root / "bootstrap-logistics.json").write_text(
            json.dumps(dict(world_id="other", technology="logistics", prewarm={})), encoding="utf-8"
        )
        self.assertIsNone(controller._extension_refusal(self.obs))
        (self.root / "bootstrap-research-stone-wall.json").write_text("{", encoding="utf-8")
        self.assertEqual(controller._extension_refusal(self.obs), "science_cell_extension_batch_state_unreadable")
        # A refused growth leaves the cell alone and the batch binds the old cell.
        action = controller.step(self.obs)
        self.assertEqual((action["type"], action["item"]), ("insert", "iron-plate"))
        self.assertEqual(self.extension_calls(), [])
        self.assertEqual(controller.state["plan_hash"], _hash(self.old))
        self.assertNotIn("red_machines", controller.state)

    def test_blocked_placement_is_recorded_once_and_the_batch_uses_the_old_cell(self):
        self.blocked = "tree"
        controller = self.make("logistic-science-pack")
        action = controller.step(self.obs)
        self.assertEqual((action["type"], action["item"]), ("insert", "iron-plate"))
        self.assertEqual(self.saved_decision("logistic-science-pack")["cell_extension_blocked"], "tree")
        self.assertEqual(controller.state["plan_hash"], _hash(self.old))
        self.assertNotIn("lineage", self.board())
        # Labs alone would make the pack-limited queue slower: none are built.
        self.assertEqual(sum(e["name"] == "lab" for e in self.obs["entities"]), 1)
        calls = len(self.extension_calls())
        self.assertEqual(calls, 1)
        self.assertIsNone(controller._grow_cell(self.obs, controller._startup_plan(self.obs)))
        self.assertEqual(len(self.extension_calls()), calls)

    def test_unobserved_build_is_recorded_and_never_committed(self):
        controller = self.make("logistic-science-pack")
        self.builder.ensure_plan = Mock(return_value={"status": "succeeded"})
        action = controller.step(self.obs)
        self.assertEqual((action["type"], action["item"]), ("insert", "iron-plate"))
        self.assertEqual(
            self.saved_decision("logistic-science-pack")["cell_extension_blocked"],
            "science_cell_extension_not_observed_after_build",
        )
        self.assertNotIn("lineage", self.board())

    def test_legacy_logistics_state_keeps_its_proof_and_the_cell_can_grow(self):
        legacy = dict(
            world_id="w",
            allocated_inputs={"iron-plate": 40, "copper-plate": 20},
            required_packs=20,
            baseline_produced=100,
            baseline_consumed=30,
        )
        (self.root / "bootstrap-logistics.json").write_text(json.dumps(legacy), encoding="utf-8")
        controller = self.make("logistic-science-pack")
        self.assertEqual(controller._extension_refusal(self.obs), "science_cell_extension_batch_unfinished")
        self.obs["technologies"]["logistics"] = True
        self.obs["production"][RED]["consumed"] = 50
        logistics = self.make("logistics")
        self.assertTrue(logistics._is_legacy())
        before = logistics.proof(self.obs)
        self.assertIsNotNone(before)
        self.assertIsNone(controller._extension_refusal(self.obs))
        self.grow(controller)
        self.assertEqual(science_machine_count(self.board()["blocks"]["startup_science"]), 2)
        again = self.make("logistics").proof(self.obs)
        self.assertEqual(again, before)
        self.assertEqual(self.make("logistics").step(self.obs)["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
