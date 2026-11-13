"""Science-capacity sizing: planner, multi-machine startup cell, multi-lab courier and wiring."""

from copy import deepcopy
import json
import math
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

from factorio_agent import progression as pg
from factorio_agent import research_capacity as rc
from factorio_agent.board import (
    LAB_SLOT_OFFSETS,
    lab_slot_entities,
    lab_slot_plan,
    science_machine_count,
    startup_science_cell,
)
from factorio_agent.core.deterministic_builder import FactoryBuilder
from factorio_agent.industry import CAPACITY_FILE, SCIENCE_COURIER_BATCH, BootstrapLogistics
from factorio_agent.jobs import CAPABILITIES, summary

RED = rc.RED_PACK


def item(name, amount):
    return dict(name=name, type="item", amount=amount)


def recipe(name, energy, ingredients, amount=1):
    return dict(name=name, energy=energy, ingredients=ingredients, products=[dict(name=name, amount=amount)])


# Vanilla 2.x recipes and technologies the planner reads (unit_energy in ticks).
RECIPES = {
    RED: recipe(RED, 5, [item("copper-plate", 1), item("iron-gear-wheel", 1)]),
    "iron-gear-wheel": recipe("iron-gear-wheel", 0.5, [item("iron-plate", 2)]),
    "copper-cable": recipe("copper-cable", 0.5, [item("copper-plate", 1)], amount=2),
    "electronic-circuit": recipe("electronic-circuit", 0.5, [item("iron-plate", 1), item("copper-cable", 3)]),
    "inserter": recipe(
        "inserter", 0.5, [item("iron-plate", 1), item("iron-gear-wheel", 1), item("electronic-circuit", 1)]
    ),
    "transport-belt": recipe("transport-belt", 0.5, [item("iron-plate", 1), item("iron-gear-wheel", 1)], amount=2),
    "assembling-machine-1": recipe(
        "assembling-machine-1",
        0.5,
        [item("iron-plate", 9), item("iron-gear-wheel", 5), item("electronic-circuit", 3)],
    ),
    "lab": recipe("lab", 2, [item("iron-gear-wheel", 10), item("electronic-circuit", 10), item("transport-belt", 4)]),
}
TECHNOLOGIES = {
    name: dict(
        prerequisites=["automation-science-pack"],
        enabled=True,
        unit_count=units,
        unit_energy=seconds * 60,
        ingredients=[item(RED, 1)],
        count_formula=None,
        research_trigger=None,
    )
    for name, units, seconds in [
        ("automation", 10, 10),
        ("electric-mining-drill", 25, 10),
        ("logistics", 20, 15),
        ("fast-inserter", 30, 15),
        ("logistic-science-pack", 75, 5),
        ("steel-processing", 50, 5),
        ("gun-turret", 10, 10),
        ("military", 10, 15),
        ("stone-wall", 10, 10),
    ]
}


def catalog():
    recipes = deepcopy(RECIPES)
    return SimpleNamespace(
        fingerprint="catalog",
        technologies=deepcopy(TECHNOLOGIES),
        recipes=recipes,
        entities={"assembling-machine-1": {"crafting_speed": 0.5}},
        recipe_for_product=lambda name: recipes.get(name),
    )


AMPLE = {
    "iron-plate": {"available": 5000, "produced_per_minute": 300, "net_per_minute": 200},
    "copper-plate": {"available": 5000, "produced_per_minute": 300, "net_per_minute": 200},
}


def materials(evidence=None):
    costs = rc.expansion_costs(catalog(), RED)
    return rc.ResearchMaterials(
        evidence=deepcopy(AMPLE if evidence is None else evidence),
        assembler_cost={RED: costs["assembler"]},
        lab_cost=costs["lab"],
        pack_cost={RED: costs["pack"]},
        machines_per_assembler={RED: costs["machines_per_assembler"]},
    )


def hardware(assemblers=1, labs=1, stock=0):
    return rc.ResearchHardware({RED: assemblers}, {RED: 6.0}, labs, pack_stock={RED: stock})


def target(name):
    return rc.research_target(catalog(), name)


class ResearchTimeTests(unittest.TestCase):
    def test_time_is_the_slower_of_labs_and_each_pack(self):
        # logistic-science-pack: labs 75*5 = 375 s, packs 75 / 6 per min = 750 s.
        self.assertEqual(rc.research_seconds(75, 5, 1, 1.0, {RED: 6.0}), 750)
        # fast-inserter: labs 30*15 = 450 s, packs 300 s.
        self.assertEqual(rc.research_seconds(30, 15, 1, 1.0, {RED: 6.0}), 450)
        self.assertEqual(rc.research_seconds(30, 15, 2, 1.0, {RED: 6.0}), 300)
        self.assertEqual(rc.research_seconds(30, 15, 1, 2.0, {RED: 6.0}), 300)

    def test_stock_shortens_only_the_pack_part_and_missing_production_is_infinite(self):
        self.assertEqual(rc.research_seconds(75, 5, 1, 1.0, {RED: 6.0}, pack_stock={RED: 45}), 375)
        self.assertEqual(rc.research_seconds(75, 5, 1, 1.0, {RED: 0.0}, pack_stock={RED: 75}), 375)
        self.assertTrue(math.isinf(rc.research_seconds(75, 5, 1, 1.0, {RED: 0.0})))
        self.assertTrue(math.isinf(rc.research_seconds(75, 5, 0, 1.0, {RED: 6.0})))
        self.assertEqual(rc.research_seconds(0, 5, 0, 1.0, {}), 0)

    def test_queue_sums_targets_and_uses_stock_first(self):
        queue = [target("automation"), target("gun-turret")]
        # Each: labs 100 s, packs 10 packs / 6 per min = 100 s + 10 s ramp.
        self.assertAlmostEqual(rc.research_queue_seconds(queue, hardware()), 220)
        # 10 packs in stock cover the whole first technology.
        self.assertAlmostEqual(rc.research_queue_seconds(queue, hardware(stock=10)), 210)


class PlannerTests(unittest.TestCase):
    def plan(self, names, **kwargs):
        config = kwargs.pop("config", rc.ResearchCapacityConfig())
        evidence = kwargs.pop("evidence", None)
        return rc.plan_research_capacity(
            [target(n) for n in names], hardware(**kwargs), materials(evidence), config=config
        )

    def test_pack_limited_research_adds_an_assembler(self):
        decision = self.plan(["logistic-science-pack"], labs=2)
        self.assertEqual(decision.bottleneck_before, "packs")
        self.assertGreater(decision.target_assemblers, decision.current_assemblers)
        self.assertEqual(decision.target_labs, 2)
        self.assertTrue(decision.expand)

    def test_lab_limited_research_adds_a_lab(self):
        decision = self.plan(["fast-inserter"], assemblers=3)
        self.assertEqual(decision.bottleneck_before, "labs")
        self.assertEqual(decision.target_assemblers, 3)
        self.assertGreater(decision.target_labs, 1)

    def test_one_assembler_one_lab_grows_both_and_reports_the_times(self):
        decision = self.plan(["logistic-science-pack"])
        self.assertEqual((decision.target_assemblers, decision.target_labs), (3, 2))
        self.assertAlmostEqual(decision.wait_seconds, 760)
        self.assertAlmostEqual(decision.chosen_seconds, 320)
        self.assertIn("wait 760s", decision.reason)
        self.assertIn("= 320s", decision.reason)
        self.assertIn("saves 440s", decision.reason)
        self.assertEqual(len(decision.options), 9)
        steel = self.plan(["steel-processing"])
        self.assertEqual((steel.target_assemblers, steel.target_labs), (3, 2))
        fast = self.plan(["fast-inserter"])
        self.assertEqual((fast.target_assemblers, fast.target_labs), (2, 3))

    def test_cell_limits_bound_the_options(self):
        config = rc.ResearchCapacityConfig(max_assemblers=2, max_labs=1)
        decision = self.plan(["logistic-science-pack"], config=config)
        self.assertEqual((decision.target_assemblers, decision.target_labs), (2, 1))
        self.assertEqual(len(decision.options), 2)

    def test_short_research_never_expands(self):
        decision = self.plan(["automation"])
        self.assertFalse(decision.expand)
        self.assertIn("< 120s anyway", decision.reason)

    def test_unfundable_or_unknown_cost_never_expands(self):
        empty = self.plan(["logistic-science-pack"], evidence={})
        self.assertFalse(empty.expand)
        self.assertIn("no fundable expansion", empty.reason)
        poor = {
            "iron-plate": {"available": 0, "produced_per_minute": 0, "net_per_minute": 0},
            "copper-plate": {"available": 0, "produced_per_minute": 0, "net_per_minute": 0},
        }
        self.assertFalse(self.plan(["logistic-science-pack"], evidence=poor).expand)
        unknown = rc.plan_research_capacity(
            [target("logistic-science-pack")], hardware(), rc.ResearchMaterials(evidence=deepcopy(AMPLE))
        )
        self.assertFalse(unknown.expand)

    def test_slow_funding_counts_against_expansion(self):
        slow = {
            "iron-plate": {"available": 0, "produced_per_minute": 6, "net_per_minute": 6},
            "copper-plate": {"available": 0, "produced_per_minute": 6, "net_per_minute": 6},
        }
        decision = self.plan(["logistic-science-pack"], evidence=slow)
        self.assertFalse(decision.expand)
        self.assertTrue(all(row["fund_seconds"] >= 300 for row in decision.options[1:]))

    def test_plate_supply_caps_the_extra_pack_rate(self):
        # Stock pays for machines; no plate production left for more packs.
        tight = {
            "iron-plate": {"available": 100, "produced_per_minute": 10, "net_per_minute": 0},
            "copper-plate": {"available": 100, "produced_per_minute": 10, "net_per_minute": 0},
        }
        decision = self.plan(["logistic-science-pack"], labs=2, evidence=tight)
        one = next(r for r in decision.options if r["extra_assemblers"] == 1 and r["extra_labs"] == 0)
        self.assertTrue(one["materials_capped"])
        self.assertEqual(one["bottleneck"], "materials")
        self.assertLess(one["pack_rate_per_minute"], 12)
        roomy = self.plan(["logistic-science-pack"], labs=2)
        free = next(r for r in roomy.options if r["extra_assemblers"] == 1 and r["extra_labs"] == 0)
        self.assertFalse(free["materials_capped"])
        self.assertEqual(free["pack_rate_per_minute"], 12)

    def test_a_queue_of_short_technologies_justifies_one_expansion(self):
        names = ["automation", "gun-turret", "military", "stone-wall", "electric-mining-drill"]
        alone = self.plan(names[:1])
        self.assertFalse(alone.expand)
        queue = self.plan(names)
        self.assertTrue(queue.expand)
        self.assertEqual(queue.technologies, tuple(names))
        self.assertGreater(queue.wait_seconds, 600)

    def test_decision_dict_is_json_safe(self):
        row = self.plan(["logistic-science-pack"], evidence={}).as_dict()
        json.dumps(row, allow_nan=False)
        self.assertFalse(row["expand"])


class CatalogHelperTests(unittest.TestCase):
    def test_expansion_cost_counts_machine_inserters_and_lab(self):
        costs = rc.expansion_costs(catalog(), RED)
        # assembler 22 Fe + 4.5 Cu, two inserters 8 Fe + 3 Cu; lab 36 Fe + 15 Cu.
        self.assertEqual(costs["assembler"], {"iron-plate": 30.0, "copper-plate": 7.5})
        self.assertEqual(costs["lab"], {"iron-plate": 36.0, "copper-plate": 15.0})
        self.assertEqual(costs["pack"], {"copper-plate": 1.0, "iron-plate": 2.0})
        self.assertEqual(costs["pack_rate_per_assembler"], 6.0)
        self.assertEqual(costs["machines_per_assembler"], 1)

    def test_one_gear_machine_feeds_ten_science_machines(self):
        for current, extra in ((1, 0), (9, 0), (10, 1), (19, 0), (20, 1)):
            with self.subTest(current=current):
                costs = rc.expansion_costs(catalog(), RED, current_assemblers=current)
                self.assertEqual(costs["extra_upstream_machines"].get("iron-gear-wheel", 0), extra)
                self.assertEqual(costs["machines_per_assembler"], 1 + extra)

    def test_target_converts_ticks_and_rejects_formulas(self):
        row = target("fast-inserter")
        self.assertEqual((row.units, row.unit_seconds, row.packs_per_unit), (30, 15, {RED: 1}))
        half = rc.research_target(catalog(), "fast-inserter", progress=0.5)
        self.assertEqual(half.units, 15)
        cat = catalog()
        cat.technologies["fast-inserter"]["count_formula"] = "L*30"
        self.assertIsNone(rc.research_target(cat, "fast-inserter"))
        self.assertIsNone(rc.research_target(cat, "missing"))

    def test_recommended_rate_follows_plate_supply(self):
        queue = ["logistic-science-pack", "steel-processing", "fast-inserter"]
        full = rc.recommended_science_rate(catalog(), queue, AMPLE)
        self.assertEqual(full["rate_per_minute"], 30)
        # 155 units over 1075 lab-seconds: 30/min needs 30*1075/(60*155) = 3.5 -> 4 labs.
        self.assertEqual(full["labs"], 4)
        tight = {name: dict(row, net_per_minute=24) for name, row in AMPLE.items()}
        self.assertEqual(rc.recommended_science_rate(catalog(), queue, tight)["rate_per_minute"], 12)
        self.assertIsNone(rc.recommended_science_rate(catalog(), queue, {}))
        self.assertIsNone(rc.recommended_science_rate(catalog(), [], AMPLE))


# --- generalised startup cell ------------------------------------------------

PICKUP = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}


def footprint(entity):
    return FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan([entity])


def owner(entities, tile):
    return next((e for e in entities if tile in footprint(e)), None)


class HubCellGeometryTests(unittest.TestCase):
    def setUp(self):
        self.plan = startup_science_cell(32.5, 0.5, science_machines=2)
        self.entities = self.plan["entities"]

    def test_original_cell_is_unchanged_and_counts_as_one_machine(self):
        legacy = startup_science_cell(32.5, 0.5)
        self.assertNotIn("science_machines", legacy)
        self.assertEqual(science_machine_count(legacy), 1)
        self.assertEqual(science_machine_count(self.plan), 2)
        self.assertIsNone(science_machine_count({"science_machines": 5}))
        with self.assertRaises(ValueError):
            startup_science_cell(32.5, 0.5, science_machines=3)

    def test_no_overlaps_and_same_three_ports(self):
        boxes = [footprint(e) for e in self.entities]
        self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))
        self.assertEqual(
            self.plan["required_items"],
            {"assembling-machine-1": 3, "wooden-chest": 3, "inserter": 6, "small-electric-pole": 4},
        )
        self.assertEqual([p["item"] for p in self.plan["ports"]], ["iron-plate", "copper-plate", RED])

    def test_every_inserter_moves_from_its_source_to_its_destination(self):
        def at(name, x, y):
            return next(e for e in self.entities if e["name"] == name and e["position"] == {"x": x, "y": y})

        x, y = 32.5, 0.5
        gear = at("assembling-machine-1", x - 3, y)
        top = at("assembling-machine-1", x, y - 3)
        bottom = at("assembling-machine-1", x, y + 3)
        iron, hub, out = (at("wooden-chest", x + dx, y) for dx in (-6, 0, 1))
        expected = {
            (x - 5, y): (iron, gear),
            (x - 1, y): (gear, hub),
            (x, y - 1): (hub, top),
            (x, y + 1): (hub, bottom),
            (x + 1, y - 1): (top, out),
            (x + 1, y + 1): (bottom, out),
        }
        inserters = [e for e in self.entities if e["name"] == "inserter"]
        self.assertEqual(len(inserters), len(expected))
        for ins in inserters:
            px, py = ins["position"]["x"], ins["position"]["y"]
            dx, dy = PICKUP[ins["direction"]]
            source, destination = expected[(px, py)]
            self.assertIs(owner(self.entities, (px + dx, py + dy)), source, ins)
            self.assertIs(owner(self.entities, (px - dx, py - dy)), destination, ins)
        self.assertEqual(top["recipe"], RED)
        self.assertEqual(bottom["recipe"], RED)
        self.assertEqual(gear["recipe"], "iron-gear-wheel")

    def assert_powered(self, entities):
        poles = [e for e in entities if e["name"] == "small-electric-pole"]
        for e in entities:
            if e["name"] in ("small-electric-pole", "wooden-chest"):
                continue
            covered = any(
                abs(tx - p["position"]["x"]) < 3 and abs(ty - p["position"]["y"]) < 3
                for tx, ty in footprint(e)
                for p in poles
            )
            self.assertTrue(covered, e)
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
        self.assertEqual(len(reached), len(poles))

    def test_power_covers_every_machine_and_inserter_with_one_pole_network(self):
        self.assert_powered(self.entities)

    def test_lab_slots_are_free_powered_and_inside_the_reservation(self):
        for plan in (self.plan, startup_science_cell(32.5, 0.5)):
            with self.subTest(machines=science_machine_count(plan)):
                plan = dict(plan, reserved_area=[[24.0, -8.0], [40.0, 8.0]])
                slots = lab_slot_entities(plan)
                self.assertEqual(len(slots), len(LAB_SLOT_OFFSETS))
                labs = lab_slot_plan(plan, 3)["entities"]
                everything = plan["entities"] + labs
                boxes = [footprint(e) for e in everything]
                self.assertEqual(sum(map(len, boxes)), len(set().union(*boxes)))
                self.assert_powered(everything)
                self.assertEqual(sum(e["name"] == "lab" for e in lab_slot_plan(plan, 1)["entities"]), 1)

    def test_lab_slots_skip_collisions_and_a_shrunk_reservation(self):
        plan = dict(self.plan, reserved_area=[[24.0, -8.0], [40.0, 8.0]])
        blocked = deepcopy(plan)
        blocked["entities"].append({"name": "small-electric-pole", "position": {"x": 36.5, "y": 0.5}, "direction": 0})
        self.assertEqual(len(lab_slot_entities(blocked)), 2)
        shrunk = dict(plan, reserved_area=[[24.0, -8.0], [35.0, 8.0]])
        self.assertEqual(lab_slot_entities(shrunk), [])
        self.assertIsNone(lab_slot_plan(shrunk, 2))


# --- startup batch with several machines and labs ------------------------------


class MultiMachineBatchTests(unittest.TestCase):
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
        self.builder = SimpleNamespace(ensure_plan=Mock(return_value={"status": "succeeded"}), _sync=Mock())
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

    def board(self, plan):
        plan = dict(plan, reserved_area=[[2.0, 2.0], [18.0, 18.0]])
        board = dict(world_id="w", catalog_fingerprint="catalog", blocks={"startup_science": plan})
        (self.root / "pcb-board.json").write_text(json.dumps(board), encoding="utf-8")
        return plan

    def place(self, plan, labs=()):
        self.obs["entities"] = []
        for i, spec in enumerate(plan["entities"] + list(labs), 1):
            self.obs["entities"].append(
                dict(
                    deepcopy(spec),
                    unit_number=i,
                    electric_network_connected=True,
                    energy=1000,
                    products_finished=100,
                    inventory={},
                )
            )
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

    def make(self, technology="fast-inserter"):
        return BootstrapLogistics(self.game, self.bootstrap, self.builder, self.catalog, technology=technology)

    def fund(self, controller):
        for name in ("iron-plate", "copper-plate"):
            action = controller.step(self.obs)
            self.assertEqual((action["type"], action["item"]), ("insert", name))
            controller.record_action(action, {"ok": True, "moved": action["count"]})

    def reds(self):
        return [e for e in self.obs["entities"] if e.get("recipe") == RED]

    def test_new_cell_is_sized_from_the_queue_and_reserved_with_two_machines(self):
        hub = self.board(startup_science_cell(10.5, 10.5, science_machines=2))
        controller = self.make("logistic-science-pack")
        controller.board.state["blocks"].clear()

        def reserve(obs, science_machines=1):
            controller.board.state["blocks"]["startup_science"] = deepcopy(hub)
            return deepcopy(hub)

        controller.board.reserve_startup_science = Mock(side_effect=reserve)
        self.builder.ensure_plan.return_value = {"type": "build", "name": "assembling-machine-1"}
        self.assertEqual(controller.step(self.obs)["type"], "build")
        controller.board.reserve_startup_science.assert_called_once_with(self.obs, science_machines=2)
        saved = json.loads((self.root / CAPACITY_FILE).read_text(encoding="utf-8"))
        decision = saved["decisions"]["logistic-science-pack"]
        self.assertEqual(decision["target_assemblers"], 2)
        self.assertGreaterEqual(decision["target_labs"], 1)
        self.assertIn("wait", decision["reason"])

    def test_two_machines_two_labs_bind_sum_and_distribute_packs(self):
        plan = self.board(startup_science_cell(10.5, 10.5, science_machines=2))
        slots = lab_slot_entities(plan)
        self.place(plan, [slots[0]["lab"]])
        (self.root / CAPACITY_FILE).write_text(
            json.dumps(dict(world_id="w", decisions={"fast-inserter": {"target_labs": 2}})), encoding="utf-8"
        )
        controller = self.make()
        self.fund(controller)
        state = controller.state
        self.assertEqual(len(state["red_machines"]), 2)
        self.assertEqual(len(state["labs"]), 2)
        self.assertEqual(state["lab"]["unit_number"], 500)
        self.assertEqual(state["baseline_products_finished"], 200)
        self.builder.ensure_plan.assert_called()
        # The lab slot is already built; no lab-extension action was needed.
        self.obs["research"] = "fast-inserter"
        output = next(e for e in self.obs["entities"] if e["position"] == plan["ports"][-1]["position"])
        output["inventory"][RED] = SCIENCE_COURIER_BATCH
        self.assertEqual(controller.step(self.obs)["status"], "waiting")  # waits for one batch per lab
        output["inventory"][RED] = 2 * SCIENCE_COURIER_BATCH
        action = controller.step(self.obs)
        self.assertEqual((action["type"], action["count"]), ("take", 10))
        controller.record_action(action, {"ok": True, "moved": 10})
        output["inventory"][RED] = 0
        self.obs["inventory"][RED] = 10
        first = controller.step(self.obs)
        self.assertEqual((first["name"], first["count"]), ("lab", 5))
        controller.record_action(first, {"ok": True, "moved": 5})
        lab = next(e for e in self.obs["entities"] if e["position"] == first["position"] and e["name"] == "lab")
        lab["inventory"][RED] = 5
        self.obs["inventory"][RED] = 5
        second = controller.step(self.obs)
        self.assertEqual(second["count"], 5)
        self.assertNotEqual(second["position"], first["position"])
        controller.record_action(second, {"ok": True, "moved": 5})
        self.assertEqual(controller.state["supplied_packs"], 10)
        # Proof sums both science machines.
        self.reds()[0]["products_finished"] += 15
        self.reds()[1]["products_finished"] += 15
        self.obs["production"][RED]["consumed"] += 30
        self.obs["technologies"]["fast-inserter"] = True
        proof = controller.proof(self.obs)
        self.assertEqual(proof["normally_assembled_packs"], 30)
        # A replaced second science machine invalidates the binding.
        self.reds()[1]["unit_number"] += 100
        self.assertIsNone(controller.proof(self.obs))
        self.assertEqual(controller.step(self.obs)["reason"], "bootstrap_research_red_machine_unit_changed")

    def test_a_planned_lab_is_built_before_binding_and_a_blocked_slot_is_not_retried(self):
        plan = self.board(startup_science_cell(10.5, 10.5))
        self.place(plan)
        (self.root / CAPACITY_FILE).write_text(
            json.dumps(dict(world_id="w", decisions={"fast-inserter": {"target_labs": 3}})), encoding="utf-8"
        )
        build = {"type": "build", "name": "lab", "position": lab_slot_entities(plan)[0]["lab"]["position"]}
        self.builder.ensure_plan.side_effect = [{"status": "succeeded"}, build]
        controller = self.make()
        self.assertEqual(controller.step(self.obs), build)
        lab_plan = self.builder.ensure_plan.call_args[0][1]
        self.assertEqual(sum(e["name"] == "lab" for e in lab_plan["entities"]), 2)
        self.assertFalse(controller.path.exists())
        self.builder.ensure_plan.side_effect = [{"status": "succeeded"}, {"status": "blocked", "reason": "tree"}]
        action = controller.step(self.obs)
        self.assertEqual((action["type"], action["item"]), ("insert", "iron-plate"))
        self.assertNotIn("labs", controller.state)
        saved = json.loads((self.root / CAPACITY_FILE).read_text(encoding="utf-8"))
        self.assertEqual(saved["decisions"]["fast-inserter"]["lab_extension_blocked"], "tree")

    def test_single_machine_state_without_new_fields_keeps_its_shape_and_courier(self):
        plan = self.board(startup_science_cell(10.5, 10.5))
        self.place(plan)
        controller = self.make()
        self.fund(controller)
        self.assertNotIn("red_machines", controller.state)
        self.assertNotIn("labs", controller.state)
        self.obs["research"] = "fast-inserter"
        output = next(e for e in self.obs["entities"] if e["position"] == plan["ports"][-1]["position"])
        output["inventory"][RED] = 7
        action = controller.step(self.obs)
        self.assertEqual(action["count"], 7)
        controller.record_action(action, {"ok": True, "moved": 7})
        self.obs["inventory"][RED] = 7
        action = controller.step(self.obs)
        self.assertEqual((action["position"], action["count"]), ({"x": 0.5, "y": 0.5}, 7))


# --- observe receipt and roadmap ---------------------------------------------


class ReceiptAndRoadmapTests(unittest.TestCase):
    def observation(self):
        return dict(
            world_id="w",
            tick=1,
            research="logistic-science-pack",
            research_progress=0.0,
            technologies={"automation": True},
            inventory={},
            entities=[
                {"name": "assembling-machine-1", "recipe": RED, "position": {"x": 0, "y": 0}},
                {"name": "lab", "position": {"x": 5, "y": 5}, "electric_network_connected": True, "inventory": {}},
            ],
            resource_supply_options={name: {"evidence": row} for name, row in AMPLE.items()},
        )

    def test_receipt_explains_the_running_research_and_recommends_rates(self):
        receipt = rc.research_capacity_receipt(catalog(), [], self.observation())
        json.dumps(receipt, allow_nan=False)
        decision = receipt["decision"]
        self.assertEqual(decision["technologies"][0], "logistic-science-pack")
        self.assertEqual(decision["current_assemblers"], 1)
        self.assertEqual(decision["current_labs"], 1)
        self.assertTrue(decision["expand"])
        self.assertIn(RED, receipt["recommended_rates"])
        row = summary(dict(self.observation(), research_capacity=receipt), SimpleNamespace(data={"jobs": {}}))
        self.assertEqual(row["research_capacity"], receipt)
        self.assertIsNone(row["research_capacity_error"])

    def test_receipt_without_research_or_evidence_does_not_expand(self):
        obs = self.observation()
        obs.pop("resource_supply_options")
        receipt = rc.research_capacity_receipt(catalog(), [], obs)
        self.assertFalse(receipt["decision"]["expand"])
        self.assertEqual(receipt["recommended_rates"], {})

    def test_autopilot_takes_the_recommended_rate_and_old_jobs_still_match(self):
        jobs = []
        for step in pg.ROADMAP:
            if step.id == "automation_science":
                break
            jobs.append({"capability": step.capability, "parameters": dict(step.parameters), "status": "succeeded"})
        observation = {"research_capacity": {"recommended_rates": {RED: {"rate_per_minute": 12, "reason": "plates"}}}}
        nodes = pg.autopilot_nodes(jobs, [], observation, CAPABILITIES, limit=10)
        node = next(n for n in nodes if n["id"] == "auto_automation_science")
        self.assertEqual(node["parameters"], {"rate_per_minute": 12})
        self.assertIn("12/min", node["reason"])
        plain = pg.autopilot_nodes(jobs, [], {}, CAPABILITIES, limit=10)
        self.assertEqual(
            next(n for n in plain if n["id"] == "auto_automation_science")["parameters"]["rate_per_minute"], 30
        )
        done = jobs + [
            {"capability": "automation_science", "parameters": {"rate_per_minute": 30}, "status": "succeeded"}
        ]
        self.assertNotIn(
            "auto_automation_science",
            [n["id"] for n in pg.autopilot_nodes(done, [], observation, CAPABILITIES, limit=10)],
        )
        for bad in (0, 31, True, "12"):
            observation["research_capacity"]["recommended_rates"][RED]["rate_per_minute"] = bad
            nodes = pg.autopilot_nodes(jobs, [], observation, CAPABILITIES, limit=10)
            self.assertEqual(
                next(n for n in nodes if n["id"] == "auto_automation_science")["parameters"]["rate_per_minute"], 30
            )


if __name__ == "__main__":
    unittest.main()


class HandGatheredFundingTests(unittest.TestCase):
    def test_wood_without_stock_or_producer_is_gathered_not_unfundable(self):
        from factorio_agent import research_capacity as rc

        evidence = {"iron-plate": {"available": 100.0, "produced_per_minute": 60.0}}
        # 0.5 wood for a pole, none carried, nothing produces wood: one second of gathering, not infinity.
        self.assertEqual(rc._fund({"wood": 0.5, "iron-plate": 30.0}, evidence), 1.0)
        held = dict(evidence, wood={"available": 4.0, "produced_per_minute": 0.0})
        self.assertEqual(rc._fund({"wood": 0.5}, held), 0.0)
        self.assertEqual(rc._fund({"plastic-bar": 1.0}, evidence), float("inf"))  # other unknown items stay unfundable
