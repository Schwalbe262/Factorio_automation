from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.bus_tap import signature
from factorio_agent.engine_capacity import (
    EngineCapacity,
    MODE,
    NEW_ENGINE_LUA,
    PIN_FIELDS,
    _bound,
    _hash,
    _key,
    additive_engine_layout,
    validate_parameters,
    verify_engine_capacity,
)
from factorio_agent.engine_production import engine_core, engine_rates

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


def catalog():
    recipes = {}
    for name, seconds, raw in (
        ("engine-unit", 10, {"steel-plate": 1, "iron-gear-wheel": 1, "pipe": 2}),
        ("iron-gear-wheel", 0.5, {"iron-plate": 2}),
        ("pipe", 0.5, {"iron-plate": 1}),
    ):
        recipes[name] = dict(
            energy=seconds,
            category="advanced-crafting" if name == "engine-unit" else "crafting",
            ingredients=[dict(name=k, type="item", amount=v) for k, v in raw.items()],
            products=[dict(name=name, type="item", amount=1)],
        )
    return SimpleNamespace(
        fingerprint="catalog",
        recipes=recipes,
        entities={
            "assembling-machine-1": dict(
                crafting_speed=0.5, crafting_categories=["crafting", "advanced-crafting"], energy_usage=1250
            ),
            "inserter": {"energy_usage": 220},
            "long-handed-inserter": {"energy_usage": 320},
        },
    )


def observed(plan, previous=None):
    previous = {signature(q): q for q in (previous or {}).get("entities", [])}
    next_unit = max([q["unit_number"] for q in previous.values()] + [999]) + 1
    rows = []
    for q in plan["entities"]:
        unit = previous.get(signature(q), {}).get("unit_number")
        if unit is None:
            unit = next_unit
            next_unit += 1
        rows.append(dict(deepcopy(q), unit_number=unit))
    return dict(world_id="w", tick=100, inventory={}, entities=rows)


def producer(c, count=2):
    core = engine_core(c, 118.5, -114.5, count)
    plan = _plan(
        core["entities"],
        core["ports"],
        reserved_area=core["reserved_area"],
        underground_pairs=core["underground_pairs"],
    )
    obs = observed(plan)
    return dict(
        world_id="w",
        catalog_fingerprint="catalog",
        phase="verified",
        core=core,
        plan=plan,
        rated=core["rated"],
        bindings={_key(q): q["unit_number"] for q in obs["entities"]},
        iron_source=dict(
            name="engine-iron",
            item="iron-plate",
            position={"x": 12.5, "y": -120.5},
            facing=4,
            unit_number=123,
            world_id="w",
        ),
        steel_source=dict(
            name="transport-belt",
            item="steel-plate",
            position={"x": 108.5, "y": -37.5},
            facing=4,
            unit_number=6213,
            world_id="w",
        ),
        steel_plan_revision="original-steel-hash",
        steel_furnace_units=[6172, 6176],
        shared_steel_nominal_per_minute=7.5,
        recipe_baselines={"old": {"products_finished": 23}},
        feed_replan={"phase": "published"},
    )


def source(state, obs):
    spec = state["core"]["ports"][-1]
    live = next(q for q in obs["entities"] if q["name"] == "transport-belt" and q["position"] == spec["position"])
    return dict(spec, name="transport-belt", unit_number=live["unit_number"], world_id=obs["world_id"])


def native_proof(state, obs):
    machine_positions = {tuple(q["position"].values()) for q in state["core"]["machines"]}
    return dict(
        output=source(state, obs),
        rated=state["rated"],
        native={
            "machines": [
                dict(unit_number=q["unit_number"], products_finished=23, recipe=q["recipe"])
                for q in obs["entities"]
                if tuple(q["position"].values()) in machine_positions and q["name"] == "assembling-machine-1"
            ]
        },
    )


def receipt(old, new, oldobs, newobs):
    old_units = [q["unit_number"] for q in oldobs["entities"] if q["name"] == "assembling-machine-1"]
    units = [q["unit_number"] for q in newobs["entities"] if q["name"] == "assembling-machine-1"]
    added = list(set(units) - set(old_units))
    row = dict(
        mode=MODE,
        world_id="w",
        catalog_fingerprint="catalog",
        old_plan_revision=_hash(old["plan"]),
        new_plan_revision=_hash(new["plan"]),
        old_plan=old["plan"],
        new_plan=new["plan"],
        retained_units=[_bound(q) for q in oldobs["entities"]],
        source=source(old, oldobs),
        old_machine_units=old_units,
        current_machine_units=units,
        added_machine_units=added,
        old_source_pins={k: deepcopy(old[k]) for k in PIN_FIELDS},
        new_source_pins={k: deepcopy(old[k]) for k in PIN_FIELDS},
        production_witness={
            "ok": True,
            "machines": [dict(unit_number=u, products_finished=1, empty_process=False) for u in added],
        },
    )
    row["receipt_id"] = _hash(row)
    return row


class LayoutTests(unittest.TestCase):
    def test_four_slots_keep_original_complete_entities_interfaces_and_pairs(self):
        c = catalog()
        old = producer(c)
        layout = additive_engine_layout(c, old, 4)
        self.assertEqual(layout["rated"]["engines_per_minute"], 12)
        self.assertEqual(layout["rated"]["steel_per_minute"], 12)
        self.assertEqual(layout["rated"]["iron_per_minute"], 48)
        self.assertEqual(layout["rated"]["component_nominal_iron_per_minute"], 180)
        self.assertEqual(len(layout["added"]), 24)
        self.assertEqual(len(layout["new_machines"]), 2)
        self.assertEqual(len(layout["cold_plan"]["entities"]), 20)
        self.assertEqual(len(layout["new_input_arms"]), 4)
        for q in old["plan"]["entities"]:
            self.assertIn(q, layout["plan"]["entities"])
        self.assertEqual(old["plan"]["ports"], layout["plan"]["ports"])
        self.assertEqual(old["plan"]["underground_pairs"], layout["plan"]["underground_pairs"])
        self.assertEqual(old["core"]["reserved_area"], layout["core"]["reserved_area"])
        self.assertFalse(any(q in layout["cold_plan"]["entities"] for q in layout["new_input_arms"]))
        self.assertFalse(any(q["name"] in ("underground-belt", "splitter") for q in layout["added"]))

    def test_existing_recipe_change_or_moved_header_and_invalid_requested_rates_fail(self):
        c = catalog()
        old = producer(c)
        old["core"]["machines"][0]["recipe"] = "pipe"
        with self.assertRaises(ValueError):
            additive_engine_layout(c, old, 4)
        old = producer(c)
        old["core"]["entities"][0]["position"]["x"] += 1
        with self.assertRaises(ValueError):
            additive_engine_layout(c, old, 4)
        for v in (True, 0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                validate_parameters({"rate_per_minute": v})


class ControllerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.c = catalog()
        self.old = producer(self.c)
        self.oldobs = observed(self.old["plan"])
        self.layout = additive_engine_layout(self.c, self.old, 4)
        self.obs = observed(self.layout["plan"], self.oldobs)
        self.coldobs = dict(
            self.obs,
            entities=[
                q
                for q in self.obs["entities"]
                if not any(
                    q["name"] == a["name"] and q["position"] == a["position"] for a in self.layout["new_input_arms"]
                )
            ],
        )
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=self.root), query=Mock())
        self.builder = SimpleNamespace(ensure_plan=Mock(return_value={"status": "succeeded"}))
        self.perimeter = SimpleNamespace(ensure_coverage=Mock(return_value=None))
        self.engine = SimpleNamespace(
            state=deepcopy(self.old), proof=Mock(return_value=native_proof(self.old, self.oldobs))
        )
        self.p = EngineCapacity(self.game, self.builder, self.c, self.perimeter, engine=self.engine)
        self.p.state = dict(
            deepcopy(self.layout),
            world_id="w",
            catalog_fingerprint="catalog",
            phase="cold",
            old_state=deepcopy(self.old),
            parameters={"rate_per_minute": 12},
            retained_units=[_bound(q) for q in self.oldobs["entities"]],
            bindings=deepcopy(self.old["bindings"]),
            baseline={},
            source=source(self.old, self.oldobs),
            budget={"ok": True, "iron_additive_delta": 24, "iron_declared_before": 24, "iron_declared_after": 48},
            scope_id="engine-production",
            post_defense_checked=True,
        )
        self.p._budget = Mock(return_value={"ok": True, "steel_required": 12, "shared_steel_nameplate": 15})
        self.p._save = Mock()
        self.new_units = [
            q["unit_number"]
            for q in self.obs["entities"]
            if q["name"] == "assembling-machine-1"
            and q["position"] in [a["position"] for a in self.layout["new_machines"]]
        ]

    def witness(self, finished=0, empty=True):
        return dict(
            ok=True,
            machines=[dict(unit_number=u, products_finished=finished, empty_process=empty) for u in self.new_units],
        )

    def test_input_arms_remain_unapproved_until_actual_empty_zero_baselines(self):
        for finished, empty in ((1, True), (0, False)):
            self.p._new = Mock(return_value=self.witness(finished, empty))
            self.builder.ensure_plan.reset_mock()
            result = self.p.step(self.coldobs, {"rate_per_minute": 12})
            self.assertEqual(result["reason"], "engine_capacity_empty_zero_process_baseline_required")
            self.assertEqual(self.builder.ensure_plan.call_count, 1)
            self.assertEqual(self.p.state["phase"], "cold")
            self.assertEqual(self.builder.ensure_plan.call_args.args[1], self.p.state["cold_plan"])

    def test_empty_baseline_is_saved_before_feed_plan_and_waits_real_products(self):
        self.p._new = Mock(return_value=self.witness())
        self.p.proof = Mock(return_value=None)
        result = self.p.step(self.coldobs, {"rate_per_minute": 12})
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(self.p.state["phase"], "feed")
        self.assertEqual(len(self.p.state["baseline"]), 2)
        self.assertEqual(self.builder.ensure_plan.call_count, 2)
        feed = self.builder.ensure_plan.call_args.args[1]["entities"]
        for q in self.layout["new_input_arms"]:
            self.assertIn(q, feed)

    def test_lost_original_unit_blocks_before_construction_or_budget_queries(self):
        obs = dict(self.obs, entities=self.obs["entities"][1:])
        result = self.p.step(obs, {"rate_per_minute": 12})
        self.assertEqual(result["reason"], "engine_capacity_retained_unit_changed")
        self.builder.ensure_plan.assert_not_called()
        self.p._budget.assert_not_called()

    def test_new_machine_replacement_or_incomplete_native_receipt_is_rejected(self):
        self.assertIsNone(self.p._check(self.obs))
        replacement = deepcopy(self.obs)
        next(q for q in replacement["entities"] if q["unit_number"] == self.new_units[0])["unit_number"] = 99999
        self.assertEqual(self.p._check(replacement)["reason"], "engine_capacity_new_unit_changed")
        self.game.query.return_value = {"ok": True, "machines": []}
        self.assertEqual(self.p._new(self.obs)["reason"], "engine_capacity_new_native_receipt_incomplete")

    def test_prepare_whole_gate_is_read_only_and_rejects_unregistered_matching_additions(self):
        self.p.state = {}
        board = dict(world_id="w", catalog_fingerprint="catalog", blocks={"engine_production": self.old["plan"]})
        for name, value in {"engine-production.json": self.old, "pcb-board.json": board}.items():
            (self.root / name).write_text(json.dumps(value))
        self.builder._occupied_by_plan = Mock(return_value=set())
        self.p._power = Mock(return_value={"ok": True})
        obs = dict(
            self.oldobs,
            technologies={"engine": True},
            enabled_recipes={
                q: True
                for q in (
                    "engine-unit",
                    "assembling-machine-1",
                    "inserter",
                    "long-handed-inserter",
                    "small-electric-pole",
                    "transport-belt",
                )
            },
        )
        with (
            patch("factorio_agent.engine_capacity.unbuilt_external_keepouts", return_value=set()),
            patch("factorio_agent.engine_capacity.surface_pipe_proof", return_value={"ok": True}),
            patch(
                "factorio_agent.engine_capacity.preflight", return_value={"ok": True, "clear_obstacles": []}
            ) as whole,
        ):
            ready = self.p.prepare(obs, {"rate_per_minute": 12})
            self.assertTrue(ready["ok"], ready)
            self.assertEqual(ready["cold_plan"]["clearance_world_id"], "w")
            whole.assert_called_once()
            self.assertEqual(whole.call_args.args[3], ready["plan"]["entities"])
            self.assertEqual(self.p.state, {})
            self.assertFalse(self.p.path.exists())
            self.p._save.assert_not_called()
            self.builder.ensure_plan.assert_not_called()
            occupied = dict(obs, entities=self.obs["entities"])
            self.assertEqual(
                self.p.prepare(occupied, {"rate_per_minute": 12})["reason"],
                "engine_capacity_unregistered_new_asset_already_present",
            )

    def test_proof_requires_each_new_counter_and_full_existing_source_lane_health(self):
        self.assertIsNone(self.p._check(self.obs))
        self.p.state["baseline"] = {str(u): 0 for u in self.new_units}
        self.p._new = Mock(return_value=self.witness(1, False))
        preview = SimpleNamespace(proof=Mock(return_value=None), last_proof_failure={"reason": "component_lane_empty"})
        self.p._preview = Mock(return_value=preview)
        self.assertIsNone(self.p.proof(self.obs))
        self.assertEqual(self.p.last_failure["evidence"]["reason"], "component_lane_empty")
        preview.proof.return_value = {"output": self.p.state["source"], "native": {"machines": []}}
        self.assertEqual(self.p.proof(self.obs)["new_machine_units"], self.new_units)
        self.p._new.return_value["machines"][1]["products_finished"] = 0
        self.assertIsNone(self.p.proof(self.obs))
        self.assertEqual(self.p.last_failure["reason"], "engine_capacity_every_new_machine_must_produce")

    def test_two_authority_publication_resumes_exact_completed_prefix_preserving_pins_and_history(self):
        self.assertIsNone(self.p._check(self.obs))
        self.p._save = EngineCapacity._save.__get__(self.p)
        docs = {
            "engine-production.json": self.old,
            "pcb-board.json": dict(
                world_id="w", catalog_fingerprint="catalog", blocks={"engine_production": self.old["plan"]}
            ),
        }
        for name, value in docs.items():
            (self.root / name).write_text(json.dumps(value))
        proof = {"production_witness": self.witness(1, False)}
        from factorio_agent import engine_capacity as module

        atomic = module._atomic_json
        calls = []

        def interrupt(path, value):
            if Path(path).name in docs:
                calls.append(Path(path).name)
                if len(calls) == 2:
                    raise OSError("known first authority completed")
            return atomic(path, value)

        with patch.object(module, "_atomic_json", side_effect=interrupt), self.assertRaises(OSError):
            self.p._publish(self.obs, proof)
        self.assertIn("publication_intent", json.loads(self.p.path.read_text()))
        self.assertIsNone(self.p._publish(self.obs, {}))
        new = json.loads((self.root / "engine-production.json").read_text())
        for k in PIN_FIELDS + ("recipe_baselines", "feed_replan"):
            self.assertEqual(new[k], self.old[k])
        for k, u in self.old["bindings"].items():
            self.assertEqual(new["bindings"][k], u)
        self.assertEqual(len(new["capacity_revisions"]), 1)
        self.assertEqual(new["rated"]["engines_per_minute"], 12)
        self.assertIsNone(self.p._publish(self.obs, {}))
        self.assertEqual(len(self.engine.state["capacity_revisions"]), 1)
        board = json.loads((self.root / "pcb-board.json").read_text())
        self.assertEqual(board["producer_capacity_allocations"][0]["delta_per_minute"], 6)
        self.assertEqual(board["consumer_capacity_allocations"][0]["delta_per_minute"], 24)
        self.assertEqual(board["consumer_capacity_allocations"][0]["old_per_minute"], 24)
        self.assertEqual(board["consumer_capacity_allocations"][0]["new_per_minute"], 48)
        board["foreign_change"] = True
        (self.root / "pcb-board.json").write_text(json.dumps(board))
        self.assertEqual(self.p._publish(self.obs, {})["reason"], "engine_capacity_publication_compare_and_swap_failed")


class AncestryTests(unittest.TestCase):
    def setUp(self):
        self.c = catalog()
        self.old = producer(self.c)
        self.oldobs = observed(self.old["plan"])
        self.new = deepcopy(self.old)
        self.new.update(additive_engine_layout(self.c, self.old, 4))
        self.obs = observed(self.new["plan"], self.oldobs)
        self.row = receipt(self.old, self.new, self.oldobs, self.obs)
        self.new["capacity_revisions"] = [self.row]
        self.engine = SimpleNamespace(state=self.new, proof=Mock(return_value=native_proof(self.new, self.obs)))
        self.kw = dict(pinned_plan_revision=_hash(self.old["plan"]), pinned_source=source(self.old, self.oldobs))

    def check(self):
        return verify_engine_capacity(None, self.obs, self.c, self.engine, **self.kw)

    def test_future_blue_keeps_original_hash_port_and_four_original_machine_units(self):
        pins = deepcopy(self.kw)
        result = self.check()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.kw, pins)
        self.assertEqual(len(result["original_machine_units"]), 4)
        self.assertEqual(len(result["current_machine_units"]), 6)
        self.assertEqual(result["rated"]["engines_per_minute"], 12)
        self.assertEqual(result["chain"], [self.row["receipt_id"]])

    def test_missing_receipt_replaced_old_unit_or_changed_source_rejected(self):
        self.new["capacity_revisions"] = []
        self.assertFalse(self.check()["ok"])
        self.new["capacity_revisions"] = [self.row]
        saved = self.obs["entities"][0]["unit_number"]
        self.obs["entities"][0]["unit_number"] = 999999
        self.assertFalse(self.check()["ok"])
        self.obs["entities"][0]["unit_number"] = saved
        self.kw["pinned_source"] = dict(self.kw["pinned_source"], unit_number=0)
        self.assertFalse(self.check()["ok"])

    def test_rehashed_recipe_source_pin_or_incomplete_receipt_cannot_change_original_contract(self):
        original = deepcopy(self.row)
        changes = [
            lambda r: r["retained_units"].pop(),
            lambda r: r["new_source_pins"].update(steel_plan_revision="changed"),
            lambda r: r["new_plan"]["entities"].__setitem__(0, dict(r["new_plan"]["entities"][0], recipe="pipe")),
            lambda r: r["production_witness"]["machines"].__setitem__(
                0, dict(r["production_witness"]["machines"][0], products_finished=0)
            ),
            lambda r: r["current_machine_units"].append(r["current_machine_units"][0]),
        ]
        for change in changes:
            row = deepcopy(original)
            change(row)
            row["new_plan_revision"] = _hash(row["new_plan"])
            row["receipt_id"] = _hash({k: v for k, v in row.items() if k != "receipt_id"})
            self.new["capacity_revisions"] = [row]
            self.new["plan"] = row["new_plan"]
            self.assertFalse(self.check()["ok"])
        self.new["capacity_revisions"] = [dict(original, receipt_id="forged")]
        self.new["plan"] = original["new_plan"]
        self.assertFalse(self.check()["ok"])


class BudgetTests(unittest.TestCase):
    def test_verified_steel15_meets12_and_fixed_iron_port_gets_honest_additive_demand(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            c = catalog()
            old = producer(c)
            layout = additive_engine_layout(c, old, 4)
            coal = dict(item="coal", material_supply_id="shared", position={"x": 108.5, "y": 32.5})
            game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root))
            engine = SimpleNamespace(
                state=old,
                steel=SimpleNamespace(
                    state={"sources": [coal], "core": {"rated": {"coal_per_minute_at_max_catalog_usage": 5.4}}}
                ),
            )
            p = EngineCapacity(game, Mock(), c, Mock(), engine=engine)
            bus = {
                "ports": [
                    dict(old["iron_source"], priority="surplus", demand_per_minute=24),
                    dict(item="iron-plate", priority="critical", demand_per_minute=659.5),
                    dict(item="copper-plate", priority="surplus", demand_per_minute=138.5),
                ],
                "capacity": {
                    "materials": {
                        "iron-plate": {"supply_ceiling_per_minute": 375},
                        "copper-plate": {"supply_ceiling_per_minute": 150},
                    }
                },
            }
            docs = {
                "engine-production.json": old,
                "pcb-board.json": {"shared_main_bus": bus},
                "material-sources.json": dict(
                    world_id="w", catalog_fingerprint="catalog", sources={"steel-coal": coal}
                ),
                "material-supply.json": dict(
                    world_id="w",
                    catalog_fingerprint="catalog",
                    supplies={"coal": dict(phase="verified", material_supply_id="shared", bank={})},
                ),
                "brick-production.json": dict(
                    world_id="w",
                    catalog_fingerprint="catalog",
                    phase="verified",
                    sources=[coal],
                    core={"rated": {"coal_per_minute_at_max_catalog_usage": 2.7}},
                ),
            }
            for name, value in docs.items():
                (root / name).write_text(json.dumps(value))
            steel = {"ok": True, "rated": {"steel_per_minute": 15}, "chain": ["valid-steel15"]}
            p._steel = Mock(return_value=steel)
            capacity = {
                "ok": True,
                "all_installed_drop_lanes_verified": True,
                "active_collector_ceiling_per_minute": 66,
            }
            with patch("factorio_agent.engine_capacity.diagnose_bank_capacity", return_value=capacity):
                result = p._budget({"world_id": "w"}, old, layout)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["steel_required"], 12)
            self.assertEqual(result["shared_steel_nameplate"], 15)
            self.assertEqual(result["iron_steady_per_minute"], 48)
            self.assertEqual(result["component_iron_burst_nominal"], 180)
            self.assertEqual(result["iron_additive_delta"], 24)
            self.assertEqual(result["shared_bus"]["iron-plate"]["declared_peak_before"], 683.5)
            self.assertEqual(result["shared_bus"]["iron-plate"]["declared_peak_after"], 707.5)
            self.assertEqual(result["shared_bus"]["iron-plate"]["design_deficit"], 332.5)
            self.assertEqual(result["shared_bus"]["iron-plate"]["new_allocation"], 24)
            self.assertEqual(result["coal_shared_potential"], 66)
            self.assertAlmostEqual(result["coal_required_with_brick"], 8.1)
            board = docs["pcb-board.json"]
            board["consumer_capacity_allocations"] = [
                dict(
                    receipt_id="engine12",
                    consumer="engine_production",
                    item="iron-plate",
                    old_per_minute=24,
                    new_per_minute=48,
                    delta_per_minute=24,
                )
            ]
            (root / "pcb-board.json").write_text(json.dumps(board))
            with patch("factorio_agent.engine_capacity.diagnose_bank_capacity", return_value=capacity):
                again = p._budget({"world_id": "w"}, old, layout)
            self.assertTrue(again["ok"])
            self.assertEqual(again["iron_additive_delta"], 0)
            self.assertEqual(again["shared_bus"]["iron-plate"]["declared_peak_after"], 707.5)
            p._steel.return_value = {"ok": True, "rated": {"steel_per_minute": 7.5}, "chain": []}
            self.assertEqual(
                p._budget({"world_id": "w"}, old, layout)["reason"], "expand_automatic_steel_before_engine_capacity"
            )
            p._steel.return_value = steel
            with patch(
                "factorio_agent.engine_capacity.diagnose_bank_capacity",
                return_value=dict(capacity, all_installed_drop_lanes_verified=False),
            ):
                self.assertEqual(
                    p._budget({"world_id": "w"}, old, layout)["reason"],
                    "engine_capacity_shared_industrial_coal_insufficient",
                )


@unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
class NativeBaselineTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute("""x={world_id="w",machines={{position={x=133.5,y=-104.5},direction=0,unit_number=7}}};d={world_id="w"};f={};s={};game={tick=10};defines={inventory={crafter_input=1}}
input={{valid_for_read=false}};output={{valid_for_read=false}}
e={unit_number=7,position={x=133.5,y=-104.5},direction=0,force=f,surface=s,quality={name="normal"},products_finished=0,crafting_progress=0,energy=100}
e.get_recipe=function()return{name="engine-unit"}end;e.is_connected_to_electric_network=function()return true end
e.get_inventory=function(i)assert(i==defines.inventory.crafter_input,"actual 2.1 enum required");return input end;e.get_output_inventory=function()return output end;target=function()return e end""")

    def test_empty_zero_and_in_progress_or_actual_products_are_distinct(self):
        row = self.lua.execute(NEW_ENGINE_LUA)
        self.assertTrue(row["ok"])
        self.assertTrue(row["machines"][1]["empty_process"])
        self.lua.execute("e.crafting_progress=.25")
        self.assertFalse(self.lua.execute(NEW_ENGINE_LUA)["machines"][1]["empty_process"])
        self.lua.execute(
            'e.crafting_progress=0;e.products_finished=1;output[1]={valid_for_read=true,name="engine-unit",quality={name="normal"},health=1,spoil_tick=0}'
        )
        row = self.lua.execute(NEW_ENGINE_LUA)
        self.assertTrue(row["ok"])
        self.assertEqual(row["machines"][1]["products_finished"], 1)
        self.assertFalse(row["machines"][1]["empty_process"])

    def test_actual_normal_recipe_unit_force_power_and_plain_input_are_required(self):
        for mutation in (
            "e.unit_number=8",
            "e.position.x=134.5",
            "e.force={}",
            "e.energy=0",
            'e.quality.name="uncommon"',
            'e.get_recipe=function()return{name="pipe"}end',
        ):
            self.setUp()
            self.lua.execute(mutation)
            self.assertFalse(self.lua.execute(NEW_ENGINE_LUA)["ok"])
        self.setUp()
        self.lua.execute(
            'input[1]={valid_for_read=true,name="copper-plate",quality={name="normal"},health=1,spoil_tick=0}'
        )
        self.assertEqual(self.lua.execute(NEW_ENGINE_LUA)["reason"], "engine_capacity_new_machine_contaminated")
        self.lua.execute('input[1].name="steel-plate";input[1].quality.name="uncommon"')
        self.assertEqual(self.lua.execute(NEW_ENGINE_LUA)["reason"], "engine_capacity_new_machine_contaminated")


if __name__ == "__main__":
    unittest.main()
