from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.advanced_circuits import (
    AdvancedCircuitProduction,
    ADVANCED_CIRCUIT_PROOF_LUA,
    _key,
    advanced_circuit_core,
    advanced_circuit_rates,
    audit_advanced_circuit_core,
)
from factorio_agent.board import entity
from factorio_agent.bus_tap import extend_bus
from factorio_agent.main_bus import BusMaterial, ConsumerTap, plan_main_bus
from factorio_agent.smelting import port
from factorio_agent.transport import xy

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


def catalog():
    recipes = {}
    for name, energy, raw, output in (
        ("advanced-circuit", 6, {"plastic-bar": 2, "electronic-circuit": 2, "copper-cable": 4}, 1),
        ("electronic-circuit", 0.5, {"iron-plate": 1, "copper-cable": 3}, 1),
        ("copper-cable", 0.5, {"copper-plate": 1}, 2),
    ):
        recipes[name] = {
            "category": "crafting",
            "energy": energy,
            "ingredients": [{"name": n, "type": "item", "amount": a} for n, a in raw.items()],
            "products": [{"name": name, "type": "item", "amount": output}],
        }
    return SimpleNamespace(
        fingerprint="c",
        recipes=recipes,
        entities={"assembling-machine-1": {"crafting_speed": 0.5, "crafting_categories": ["crafting"]}},
    )


class AdvancedCoreTests(unittest.TestCase):
    def test_recursive_catalog_bom_and_growth_with_two_wire_machines(self):
        r = advanced_circuit_rates(catalog())
        self.assertEqual(
            (r["advanced_circuits_per_minute"], r["iron_per_minute"], r["copper_per_minute"], r["plastic_per_minute"]),
            (10, 20, 50, 20),
        )
        self.assertEqual(
            (r["green_wire_per_minute"], r["direct_wire_per_minute"], r["total_wire_per_minute"]), (60, 40, 100)
        )
        self.assertEqual((r["wire_nominal_per_minute"], r["green_nominal_per_minute"]), (120, 60))
        large = advanced_circuit_rates(catalog(), 4)
        self.assertEqual(
            (large["advanced_circuits_per_minute"], large["total_wire_per_minute"], large["wire_nominal_per_minute"]),
            (20, 200, 240),
        )
        self.assertEqual(
            (large["component_nominal_iron_per_minute"], large["component_nominal_copper_per_minute"]), (60, 120)
        )

    def test_changed_recipe_category_and_overloaded_arms_fail_closed(self):
        for change in ("category", "ingredient", "yield", "time"):
            c = catalog()
            if change == "category":
                c.recipes["advanced-circuit"]["category"] = "fluid-crafting"
            if change == "ingredient":
                c.recipes["advanced-circuit"]["ingredients"][0]["name"] = "coal"
            if change == "yield":
                c.recipes["copper-cable"]["products"][0]["amount"] = 0
            if change == "time":
                c.recipes["advanced-circuit"]["energy"] = 1
            with self.assertRaises(ValueError, msg=change):
                advanced_circuit_rates(c)

    def test_initial_and_all_expanded_cells_have_native_geometry_lanes_and_power(self):
        for count in (2, 3, 4):
            p = advanced_circuit_core(catalog(), 0.5, 0.5, count)
            a = audit_advanced_circuit_core(p, catalog())
            self.assertTrue(a["ok"], a["errors"])
            self.assertEqual(a["machine_count"], {2: 4, 3: 6, 4: 7}[count])
            self.assertEqual(len(p["underground_pairs"]), 4)
            self.assertEqual(p["reserved_area"], [[-4, -1], [27, 21]])
            self.assertFalse(any("drop_position" in e or "pickup_position" in e for e in p["entities"]))
            for arm in a["arm_targets"]:
                self.assertTrue(arm["allowed_items"])
                self.assertLessEqual(arm["steady_items_per_minute"], arm["conservative_design_budget_per_minute"])

    def test_growth_preserves_every_fixed_asset_port_and_output_access(self):
        small = advanced_circuit_core(catalog(), 0.5, 0.5)
        large = advanced_circuit_core(catalog(), 0.5, 0.5, 4)
        for field in ("ports", "reserved_area", "underground_pairs", "output_access"):
            self.assertEqual(small[field], large[field])
        self.assertTrue(all(e in large["entities"] for e in small["entities"]))
        self.assertEqual(
            small["required_items"],
            {
                "transport-belt": 117,
                "assembling-machine-1": 4,
                "fast-inserter": 5,
                "underground-belt": 8,
                "inserter": 4,
                "long-handed-inserter": 2,
                "small-electric-pole": 6,
            },
        )

    def test_wrong_long_arm_missing_wire_and_yellow_green_upgrade_are_detected(self):
        c = catalog()
        p = advanced_circuit_core(c, 0.5, 0.5)
        next(e for e in p["entities"] if e["name"] == "long-handed-inserter")["name"] = "inserter"
        self.assertFalse(audit_advanced_circuit_core(p, c)["ok"])
        p = advanced_circuit_core(c, 0.5, 0.5)
        p["entities"] = [e for e in p["entities"] if xy(e) != (-0.5, 7.5)]
        self.assertFalse(audit_advanced_circuit_core(p, c)["ok"])
        for role in ("green-wire", "green-output", "iron-input"):
            p = advanced_circuit_core(c, 0.5, 0.5, 4)
            next(e for e in p["entities"] if e.get("arm_role") == role)["name"] = "inserter"
            self.assertTrue(
                any(
                    s.startswith("inserter_budget_or_target_invalid")
                    for s in audit_advanced_circuit_core(p, c)["errors"]
                ),
                role,
            )

    def test_missing_empty_tail_reproduces_native_both_lane_copper_corner(self):
        c = catalog()
        p = advanced_circuit_core(c, 0.5, 0.5)
        p["entities"] = [e for e in p["entities"] if xy(e) != (-1.5, 0.5)]
        self.assertTrue(any(e.startswith("typed_lane_mismatch") for e in audit_advanced_circuit_core(p, c)["errors"]))

    def test_native_underground_pair_theft_and_occupied_growth_slot_are_rejected(self):
        c = catalog()
        p = advanced_circuit_core(c, 0.5, 0.5)
        p["entities"].append(dict(entity("underground-belt", 7.5, 6.5, 12), belt_to_ground_type="output"))
        self.assertIn("underground_pair_can_be_stolen", audit_advanced_circuit_core(p, c)["errors"])
        p = advanced_circuit_core(c, 0.5, 0.5)
        p["entities"].append(entity("small-electric-pole", 15.5, 12.5))
        self.assertIn("future_machine_slot_occupied", audit_advanced_circuit_core(p, c)["errors"])


class AdvancedControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cat = catalog()
        self.game = SimpleNamespace(
            cfg=SimpleNamespace(runtime_dir=self.root), query=Mock(return_value={"ok": True, "pole_index": 1})
        )
        self.builder = SimpleNamespace(
            _occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,
            ensure_plan=Mock(return_value={"type": "craft", "recipe": "assembling-machine-1", "count": 1}),
            bootstrap=SimpleNamespace(
                ensure_item=Mock(return_value={"type": "craft", "recipe": "splitter", "count": 1})
            ),
        )
        self.defense = SimpleNamespace(
            prepare_coverage=Mock(return_value={"ok": True}),
            ensure_coverage=Mock(return_value=None),
            coverage_proof=Mock(return_value={"complete": True}),
        )
        self.bus = plan_main_bus(
            [BusMaterial("iron-plate", -11.5, (375,), 2), BusMaterial("copper-plate", -3.5, (150,), 2)],
            [ConsumerTap("red", "iron-plate", -87.5, "critical", demand_per_minute=50)],
            north_y=-122.5,
            south_y=-10.5,
            port_x=12.5,
        )
        self.board = {"world_id": "w", "catalog_fingerprint": "c", "blocks": {}, "shared_main_bus": self.bus}
        (self.root / "pcb-board.json").write_text(json.dumps(self.board))
        self.docs = {"board": self.board, "factory": {"world_id": "w", "catalog_fingerprint": "c", "bus": self.bus}}
        self.plastic = dict(
            port("plastic-bar", 90.5, -40.5, 4, direction="output"),
            name="transport-belt",
            unit_number=700,
            world_id="w",
            defense_scope_id="plastic",
        )
        self.chemical = SimpleNamespace(
            state={
                "world_id": "w",
                "catalog_fingerprint": "c",
                "products": {
                    "plastic-bar": {
                        "phase": "verified",
                        "plan": {"entities": [entity("transport-belt", 90.5, -40.5, 4)]},
                    }
                },
            },
            proof=Mock(return_value={"source": self.plastic}),
        )
        self.c = AdvancedCircuitProduction(self.game, self.builder, self.cat, self.defense, chemical=self.chemical)
        self.tap = SimpleNamespace(state={}, _documents=Mock(side_effect=lambda: self.docs), _save=Mock(), step=Mock())
        self.c.tap = self.tap
        self.obs = {
            "ok": True,
            "world_id": "w",
            "tick": 10,
            "technologies": {"advanced-circuit": True},
            "enabled_recipes": {
                name: True
                for name in (
                    "advanced-circuit",
                    "electronic-circuit",
                    "copper-cable",
                    "assembling-machine-1",
                    "fast-inserter",
                    "inserter",
                    "long-handed-inserter",
                    "underground-belt",
                    "splitter",
                )
            },
            "entities": [dict(e, unit_number=i + 1000) for i, e in enumerate(self.bus["entities"])]
            + [dict(self.plastic, direction=4)],
        }
        self.tap.prepare = Mock(side_effect=self.preview)
        self.preview_patch = patch(
            "factorio_agent.advanced_circuits.BusTap", return_value=SimpleNamespace(prepare=self.tap.prepare)
        ).start()
        self.anchors = patch(
            "factorio_agent.advanced_circuits.advanced_circuit_anchors", return_value=[(72.5, -100.5)]
        ).start()
        self.land = patch(
            "factorio_agent.advanced_circuits.survey_slots", return_value={"ok": True, "legal": [1]}
        ).start()
        self.preflight = patch(
            "factorio_agent.advanced_circuits.preflight", return_value={"ok": True, "clear_obstacles": []}
        ).start()
        self.route = patch(
            "factorio_agent.advanced_circuits.plan_connection",
            return_value=_plan([], [], underground_pairs=[], route_metrics={"length": 10, "turns": 1}),
        ).start()
        self.power = patch(
            "factorio_agent.advanced_circuits.connect_power_groups", return_value={"ok": True, "entities": []}
        ).start()
        self.addCleanup(patch.stopall)

    def preview(self, obs, params):
        bus = self.docs["factory"]["bus"]
        result = extend_bus(bus, params, obs)
        if not result.get("ok"):
            return result
        live = next(
            e
            for e in obs["entities"]
            if e["name"] == result["remove_spec"]["name"] and xy(e) == xy(result["remove_spec"])
        )
        return {
            "ok": True,
            "old_bus": bus,
            "new_bus": result["bus"],
            "added_entities": result["added_entities"],
            "parameters": deepcopy(params),
            "remove": deepcopy(live),
            "retained_units": [
                {"name": e["name"], "position": e["position"], "unit_number": e["unit_number"]}
                for e in obs["entities"]
                if e != live
            ],
        }

    def install(self):
        self.c.state = self.c.prepare(self.obs)
        self.assertTrue(self.c.state["ok"], self.c.state)
        s = self.c.state
        s["phase"] = "build"
        bus = s["prospective_bus"]
        self.docs["factory"]["bus"] = bus
        self.docs["board"]["shared_main_bus"] = bus
        self.obs["entities"] = [dict(e, unit_number=i + 2000) for i, e in enumerate(bus["entities"])] + [
            dict(self.plastic, direction=4)
        ]
        self.obs["entities"] += [dict(e, unit_number=i + 5000) for i, e in enumerate(s["plan"]["entities"])]
        for item, name in (("iron-plate", "advanced-iron"), ("copper-plate", "advanced-copper")):
            p = next(p for p in bus["ports"] if p["name"] == name)
            station = next(r for r in bus["stations"] if r["name"] == name)
            e = next(e for e in self.obs["entities"] if e["name"] == "transport-belt" and xy(e) == xy(p))
            split = next(
                e for e in self.obs["entities"] if e["name"] == "splitter" and xy(e) == xy(station["splitter"])
            )
            s["tap_sources"][item] = dict(
                p,
                unit_number=e["unit_number"],
                priority_splitter={
                    "name": "splitter",
                    "position": split["position"],
                    "unit_number": split["unit_number"],
                    "direction": 0,
                },
            )
        self.assertIsNone(self.c._bind(self.obs))
        mouths = {xy(e): e for e in self.obs["entities"] if e["name"] == "underground-belt"}
        for p in s["plan"]["underground_pairs"]:
            a, b = mouths[tuple(p["input"])], mouths[tuple(p["output"])]
            for one, two in ((a, b), (b, a)):
                one.update(underground_pair_verified=True, underground_neighbour={"unit_number": two["unit_number"]})

    def test_prepare_is_readonly_both_surplus_taps_whole_three_feeds_and_growth(self):
        before = (self.root / "pcb-board.json").read_bytes()
        with patch("factorio_agent.advanced_circuits._atomic_json") as write:
            result = self.c.prepare(self.obs)
            write.assert_not_called()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.c.state, {})
        self.assertFalse(self.c.path.exists())
        self.assertEqual((self.root / "pcb-board.json").read_bytes(), before)
        self.assertEqual(
            [(p["item"], p["priority"], p["demand_per_minute"]) for p in result["parameters"]],
            [("iron-plate", "surplus", 20), ("copper-plate", "surplus", 50)],
        )
        self.assertEqual(self.route.call_count, 3)
        self.assertEqual(self.preflight.call_count, 2)
        units = self.preflight.call_args.kwargs["ignored_units"]
        self.assertEqual(len(set(units)), 2)
        self.assertEqual(result["plastic_source"]["unit_number"], 700)
        self.builder.ensure_plan.assert_not_called()
        self.assertTrue(all(e in self.preflight.call_args.args[3] for e in result["second_added_entities"]))

    def test_natural_technology_missing_plastic_and_unfinished_bus_fail_before_cost(self):
        self.assertEqual(
            self.c.prepare({**self.obs, "technologies": {}})["reason"],
            "native_advanced_circuit_technology_and_component_recipes_required",
        )
        self.chemical.state["products"].clear()
        self.assertEqual(self.c.prepare(self.obs)["reason"], "commissioned_automatic_plastic_factory_required")
        self.assertEqual(self.route.call_count, 0)
        self.builder.bootstrap.ensure_item.assert_not_called()
        self.assertFalse(self.c.path.exists())

    def test_core_cannot_occupy_another_factories_unbuilt_external_feed(self):
        self.board["blocks"]["future"] = {
            "reserved_area": [[500, 500], [510, 510]],
            "entities": [entity("transport-belt", 72.5, -100.5, 4)],
        }
        result = self.c.prepare(self.obs)
        self.assertEqual(result["reason"], "no_advanced_circuit_site_clear_of_reserved_and_unbuilt_external_plans")
        self.land.assert_not_called()
        self.tap.prepare.assert_not_called()
        self.builder.ensure_plan.assert_not_called()
        self.assertFalse(self.c.path.exists())

    def test_failed_corridor_preserves_exact_stage_and_does_not_latch_plan(self):
        self.route.return_value = {"ok": False, "reason": "no_clean_trunk_corridor", "visited": 400}
        r = self.c.step(self.obs)
        self.assertEqual(r["status"], "blocked")
        self.assertEqual(r["evidence"]["failures"][0]["stage"], "plastic_route")
        self.assertEqual(r["evidence"]["failures"][0]["detail"]["visited"], 400)
        self.assertFalse(self.c.path.exists())
        self.tap.step.assert_not_called()

    def test_one_twelve_route_budget_and_compact_public_failure_evidence(self):
        self.anchors.return_value = [(72.5 + 32 * i, -100.5) for i in range(6)]
        self.land.return_value = {"ok": True, "legal": list(range(1, 7))}

        def route(game, builder, obs, source, destination, reserved):
            if source["item"] == "plastic-bar":
                return _plan([], [], underground_pairs=[], route_metrics={"length": 10, "turns": 1})
            return {
                "ok": False,
                "reason": "no_clean_trunk_corridor",
                "visited": 123,
                "path": [{"x": i, "y": i} for i in range(1000)],
                "entities": ["huge_unused_payload"],
            }

        self.route.side_effect = route
        result = self.c.prepare(self.obs)
        self.assertFalse(result["ok"])
        self.assertEqual(result["route_attempts"], 12)
        self.assertEqual(self.route.call_count, 12)
        self.assertTrue(all(row["detail"]["visited"] == 123 for row in result["failures"]))
        self.assertNotIn("huge_unused_payload", json.dumps(result))
        self.assertNotIn('"path"', json.dumps(result))

    def test_second_tap_fresh_prepares_updated_bus_and_binds_real_first_units(self):
        self.c.state = self.c.prepare(self.obs)
        s = self.c.state
        self.assertTrue(s["ok"], s)
        s["phase"] = "taps"

        def completed(obs, params):
            self.tap.state["phase"] = "verified"
            p = next(p for p in self.tap.state["new_bus"]["ports"] if p["name"] == params["name"])
            return {
                "status": "succeeded",
                "evidence": {"port": dict(p, unit_number=900, priority_splitter={"unit_number": 901})},
            }

        self.tap.step.side_effect = completed
        self.assertEqual(self.c._tap_step(self.obs)["status"], "waiting")
        self.assertEqual(s["tap_index"], 1)
        first = self.tap.state["new_bus"]
        self.docs["factory"]["bus"] = first
        self.docs["board"]["shared_main_bus"] = first
        removed = s["first_tap"]["remove"]["unit_number"]
        self.obs["entities"] = [e for e in self.obs["entities"] if e["unit_number"] != removed]
        first_units = []
        for index, e in enumerate(s["first_tap"]["added_entities"]):
            self.obs["entities"].append(dict(e, unit_number=8000 + index))
            first_units.append(8000 + index)
        self.tap.prepare.reset_mock()
        self.assertEqual(self.c._tap_step(self.obs)["status"], "waiting")
        self.assertEqual(self.tap.prepare.call_count, 1)
        self.assertEqual(self.tap.state["old_bus"], first)
        self.assertTrue(set(first_units) <= {e["unit_number"] for e in self.tap.state["retained_units"]})
        self.assertEqual(s["phase"], "build")

    def test_second_tap_changes_fail_closed_before_any_step(self):
        self.c.state = self.c.prepare(self.obs)
        s = self.c.state
        s.update(phase="taps", tap_index=1)
        self.tap.prepare.return_value = None
        self.tap.prepare.side_effect = lambda obs, p: {
            "ok": True,
            "new_bus": s["prospective_bus"],
            "added_entities": s["second_added_entities"],
            "remove": {"unit_number": -1},
        }
        self.assertEqual(
            self.c._tap_step(self.obs)["reason"],
            "advanced_tap_fresh_preflight_changed_planned_geometry_or_owned_cutover",
        )
        self.tap.step.assert_not_called()

    def test_defense_before_core_build_and_post_defense_obstacle_stays_blocked(self):
        self.install()
        self.defense.ensure_coverage.return_value = {"type": "craft", "recipe": "firearm-magazine", "count": 10}
        self.assertEqual(self.c.step(self.obs)["recipe"], "firearm-magazine")
        self.builder.ensure_plan.assert_not_called()
        self.defense.ensure_coverage.return_value = None
        self.preflight.return_value = {"ok": False, "reason": "post_clips_input_apron"}
        self.assertEqual(self.c.step(self.obs)["reason"], "advanced_circuit_site_changed_after_defense")
        self.builder.ensure_plan.assert_not_called()
        board = json.loads((self.root / "pcb-board.json").read_text())
        self.assertEqual(board["blocks"]["advanced_circuit_production"], self.c.state["plan"])

    def test_lost_owned_asset_source_or_preserved_pipe_blocks_ordinary_builder(self):
        self.install()
        self.obs["entities"].pop()
        self.assertEqual(self.c.step(self.obs)["reason"], "owned_advanced_circuit_asset_missing_or_replaced")
        self.builder.ensure_plan.assert_not_called()
        self.obs["entities"].append(dict(self.c.state["plan"]["entities"][-1], unit_number=99999))
        self.assertEqual(self.c.step(self.obs)["reason"], "owned_advanced_circuit_asset_missing_or_replaced")
        self.obs["entities"][-1]["unit_number"] = self.c.state["bindings"][_key(self.c.state["plan"]["entities"][-1])]
        with patch(
            "factorio_agent.advanced_circuits.surface_pipe_proof",
            return_value={"ok": False, "reason": "pipe_peer_changed"},
        ):
            self.assertEqual(self.c.step(self.obs)["reason"], "advanced_circuit_preserved_pipe_network_changed")
            self.assertIsNone(self.c.proof(self.obs))
            self.builder.ensure_plan.assert_not_called()
        self.chemical.state["products"]["plastic-bar"]["plan"]["changed"] = True
        self.assertEqual(self.c.step(self.obs)["reason"], "advanced_circuit_bound_plastic_plan_changed")

    def legacy_mixer(self):
        self.install()
        s = self.c.state
        x, y = xy({"position": s["core"]["anchor"]})
        tail = entity("transport-belt", x - 2, y, 4)
        for plan in (s["core"], s["plan"]):
            plan["entities"] = [e for e in plan["entities"] if e != tail]
            plan["required_items"]["transport-belt"] -= 1
        s["bindings"].pop(_key(tail))
        s["core"]["raw_mixer_inline_tail"] = False
        self.obs["entities"] = [
            e for e in self.obs["entities"] if not (e["name"] == tail["name"] and xy(e) == xy(tail))
        ]
        board = json.loads((self.root / "pcb-board.json").read_text())
        board["blocks"]["advanced_circuit_production"] = deepcopy(s["plan"])
        (self.root / "pcb-board.json").write_text(json.dumps(board))
        return tail

    def test_legacy_repair_prepare_is_readonly_exact_one_paid_belt_and_all_old_bindings(self):
        tail = self.legacy_mixer()
        before = deepcopy(self.c.state)
        with patch("factorio_agent.advanced_circuits._atomic_json") as write:
            result = self.c.prepare_mixer_repair(self.obs)
            write.assert_not_called()
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["needed"])
        self.assertEqual(result["tail"], tail)
        self.assertEqual(result["required_items"], {"transport-belt": 1})
        self.assertEqual(result["preserved_bindings"], before["bindings"])
        self.assertEqual(self.c.state, before)
        self.assertEqual(result["core"]["ports"], before["core"]["ports"])
        self.assertEqual([e for e in result["plan"]["entities"] if e not in before["plan"]["entities"]], [tail])
        self.assertTrue(all(e in result["plan"]["entities"] for e in before["plan"]["entities"]))

    def test_legacy_adoption_updates_only_owned_plan_and_blocks_foreign_tail_or_lost_old_asset(self):
        tail = self.legacy_mixer()
        old_plan = deepcopy(self.c.state["plan"])
        self.obs["entities"].append(dict(tail, unit_number=99999))
        self.assertEqual(
            self.c.prepare_mixer_repair(self.obs)["reason"], "advanced_mixer_repair_tail_occupied_without_owned_binding"
        )
        self.obs["entities"].pop()
        self.assertIsNone(self.c._adopt_mixer_repair(self.obs))
        self.assertIsNone(self.c._reserve(self.obs))
        self.assertEqual(self.c.state["mixer_repair"]["phase"], "await_native_pure_lanes")
        saved = json.loads((self.root / "pcb-board.json").read_text())["blocks"]["advanced_circuit_production"]
        self.assertEqual(saved["ports"], old_plan["ports"])
        self.assertTrue(all(e in saved["entities"] for e in old_plan["entities"]))
        self.assertEqual(len(saved["entities"]), len(old_plan["entities"]) + 1)
        self.assertFalse(self.c.state["construction_authorized"])
        self.builder.ensure_plan.assert_not_called()

    def test_repair_recovery_uses_only_exact_owned_header_sources_and_existing_bounded_collector(self):
        from factorio_agent.construction_supply import validate_take_many

        self.legacy_mixer()
        self.assertIsNone(self.c._adopt_mixer_repair(self.obs))
        tail = self.c.state["mixer_repair"]["tail"]
        self.obs["entities"].append(dict(tail, unit_number=99999))
        self.c._bind(self.obs)
        x, y = xy({"position": self.c.state["core"]["anchor"]})
        q = next(e for e in self.obs["entities"] if e["name"] == "transport-belt" and xy(e) == (x + 11, y))
        row = {key: deepcopy(q[key]) for key in ("name", "position", "unit_number")}
        self.game.query.return_value = {"ok": True, "wrong": {"copper-plate": [row]}, "copper_left": 1, "iron_right": 1}
        action = self.c._mixer_cleanup(self.obs)
        validate_take_many(action)
        self.assertEqual(
            (action["type"], action["item"], action["count"], action["world_id"]),
            ("take_many", "copper-plate", 50, "w"),
        )
        self.assertEqual(action["sources"], [dict(row, kind="belt")])
        row["unit_number"] = -1
        self.assertEqual(self.c._mixer_cleanup(self.obs)["reason"], "advanced_mixer_repair_inspection_receipt_invalid")
        row["unit_number"] = q["unit_number"]
        self.c.state["mixer_repair"]["cleanup_attempts"] = 16
        self.assertEqual(self.c._mixer_cleanup(self.obs)["reason"], "mixer_cleanup_budget_exceeded")
        self.c.state["mixer_repair"]["cleanup_attempts"] = 15
        self.assertEqual(self.c._mixer_cleanup(self.obs)["type"], "take_many")
        self.assertEqual(self.c.state["mixer_repair"]["cleanup_attempts"], 16)
        self.game.query.return_value = {"ok": True, "wrong": {}, "copper_left": 1, "iron_right": 1}
        self.assertIsNone(self.c._mixer_cleanup(self.obs))
        self.assertEqual(self.c.state["mixer_repair"]["phase"], "verified")

    @unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
    def test_actual_proof_lua_both_surplus_feeds_three_recipes_lanes_quality_and_output(self):
        self.install()
        lua = LuaRuntime(unpack_returned_tuples=True)

        def table(value):
            if isinstance(value, dict):
                return lua.table_from({k: table(v) for k, v in value.items()})
            if isinstance(value, list):
                return lua.table_from([table(v) for v in value])
            return value

        def decode(value):
            if not hasattr(value, "items"):
                return value
            pairs = dict(value.items())
            if pairs and all(type(k) is int for k in pairs) and set(pairs) == set(range(1, len(pairs) + 1)):
                return [decode(pairs[i]) for i in range(1, len(pairs) + 1)]
            return {k: decode(v) for k, v in pairs.items()}

        lua.globals().helpers = lua.table_from({"json_to_table": lambda raw: table(json.loads(raw))})
        lua.execute("""f={};s={};game={tick=20};defines={inventory={crafter_input=1}};entities={}
powered=true;wrong_recipe=false;wrong_quality=false;foreign_item=false;swapped_lanes=false;empty_output=false;buried_bad=false
function k(q)return q.name..":"..q.position.x..":"..q.position.y end
function target(p,name)return entities[name..":"..p.x..":"..p.y]end
function belt(q,items)
 q.force=f;q.surface=s;q.get_max_transport_line_index=function()return buried_bad and q.name=="underground-belt"and 4 or 2 end
 q.get_transport_line=function(index)return{get_contents=function()
  if empty_output and q.is_outlet then return{}end
  if index>2 then return{{name="stone",quality="normal",count=1}}end
  local result={};local lane=swapped_lanes and 3-index or index
  for _,name in ipairs(items[lane]or{})do result[#result+1]={name=foreign_item and"stone"or name,quality=wrong_quality and"uncommon"or"normal",count=1}end
  return result end}end;entities[k(q)]=q;return q
end
function machine(q)
 q.force=f;q.surface=s;q.energy=100;q.products_finished=1;q.is_connected_to_electric_network=function()return powered end
 q.get_recipe=function()return{name=wrong_recipe and"firearm-magazine"or q.recipe}end
 local raw=q.recipe=="advanced-circuit"and"plastic-bar"or(q.recipe=="electronic-circuit"and"iron-plate"or"copper-plate")
 q.get_inventory=function()return{{valid_for_read=true,name=foreign_item and"stone"or raw,quality={name=wrong_quality and"uncommon"or"normal"}}}end
 q.get_output_inventory=function()return{{valid_for_read=true,name=foreign_item and"stone"or q.recipe,quality={name="normal"}}}end
 entities[k(q)]=q;return q
end
function arm(q)
 q.force=f;q.surface=s;q.energy=100;q.is_connected_to_electric_network=function()return powered end
 q.drop_position={x=q.drop_position.x,y=q.drop_position.y+.2};q.held_stack={valid_for_read=false};entities[k(q)]=q;return q
end
function splitter(q)q.force=f;q.surface=s;q.splitter_input_priority="left";q.splitter_output_priority="left";entities[k(q)]=q;return q end""")
        state = self.c.state
        core = state["core"]
        audit = state["audit"]
        units = state["bindings"]

        def bound(e):
            return dict(e, unit_number=units[_key(e)])

        for e in core["machines"]:
            lua.globals().machine(table(bound(e)))
        for e in audit["arm_targets"]:
            lua.globals().arm(table(bound(e)))
        for e in audit["belt_contracts"]:
            items = e["allowed_items"]
            lua.globals().belt(table(bound(e)), table([items, items]))
        for m in core["red_machines"] + core["wire_machines"] + [core["green_machine"]]:
            point = (xy(m)[0], xy(m)[1] - 3)
            e = next(e for e in audit["belt_contracts"] if xy(e) == point)
            items = (
                [["copper-cable"], ["electronic-circuit"]]
                if m["recipe"] == "advanced-circuit"
                else [["copper-plate"], ["iron-plate"]]
            )
            lua.globals().belt(table(bound(e)), table(items))
        for source in list(state["tap_sources"].values()) + [state["plastic_source"]]:
            lua.globals().belt(
                table(dict(source, name="transport-belt", direction=source["facing"])),
                table([[source["item"]], [source["item"]]]),
            )
            if "priority_splitter" in source:
                lua.globals().splitter(table(source["priority_splitter"]))
        outlet = dict(core["ports"][-1], name="transport-belt")
        lua.globals().belt(
            table(dict(bound(outlet), direction=4, is_outlet=True)), table([["advanced-circuit"], ["advanced-circuit"]])
        )
        self.game.query.side_effect = lambda body: decode(
            lua.execute("return(function() " + " ".join(body.splitlines()) + " end)()")
        )
        self.assertIsNotNone(self.c.proof(self.obs), self.c.last_proof_failure)
        for change, restore in (
            ("powered=false", "powered=true"),
            ("wrong_recipe=true", "wrong_recipe=false"),
            ("wrong_quality=true", "wrong_quality=false"),
            ("foreign_item=true", "foreign_item=false"),
            ("swapped_lanes=true", "swapped_lanes=false"),
            ("empty_output=true", "empty_output=false"),
            ("buried_bad=true", "buried_bad=false"),
        ):
            lua.execute(change)
            self.assertIsNone(self.c.proof(self.obs), change)
            lua.execute(restore)
        first = lua.globals().entities[_key(core["machines"][0])]
        first.products_finished = 0
        self.assertIsNone(self.c.proof(self.obs))
        first.products_finished = 1
        arm = lua.globals().entities[_key(audit["arm_targets"][0])]
        arm.pickup_position.y += 1
        self.assertIsNone(self.c.proof(self.obs))
        arm.pickup_position.y -= 1
        arm.held_stack = table({"valid_for_read": True, "name": "stone", "quality": {"name": "normal"}})
        self.assertIsNone(self.c.proof(self.obs))
        arm.held_stack = table({"valid_for_read": False})
        for source in state["tap_sources"].values():
            split = lua.globals().entities[_key(source["priority_splitter"])]
            split.splitter_output_priority = "right"
            self.assertIsNone(self.c.proof(self.obs))
            split.splitter_output_priority = "left"
            source_live = lua.globals().entities[_key(dict(source, name="transport-belt"))]
            unit = source_live.unit_number
            source_live.unit_number += 1
            self.assertIsNone(self.c.proof(self.obs))
            source_live.unit_number = unit
        for forbidden in ("create_entity", "set_recipe", "insert_fluid", "set_research", ".insert(", ".remove("):
            self.assertNotIn(forbidden, ADVANCED_CIRCUIT_PROOF_LUA)
        # The actual repair inspector is read-only and refuses a native curve or
        # incompatible held stack before suggesting any guarded stock collection.
        self.c.state["mixer_repair"] = {"phase": "await_native_pure_lanes"}
        anchor = core["anchor"]
        recipient = lua.globals().entities[_key(entity("transport-belt", anchor["x"] - 1, anchor["y"], 4))]
        recipient.belt_shape = "straight"
        action = self.c._mixer_cleanup(self.obs)
        self.assertEqual(action["type"], "take_many")
        recipient.belt_shape = "left"
        self.assertEqual(
            self.c._mixer_cleanup(self.obs)["evidence"]["reason"], "advanced_mixer_native_recipient_not_straight"
        )
        recipient.belt_shape = "straight"
        arm = lua.globals().entities[_key(audit["arm_targets"][0])]
        arm.held_stack = table({"valid_for_read": True, "name": "iron-plate", "quality": {"name": "normal"}})
        self.assertEqual(
            self.c._mixer_cleanup(self.obs)["evidence"]["reason"],
            "advanced_mixer_incompatible_held_stack_requires_guarded_manual_recovery",
        )


if __name__ == "__main__":
    unittest.main()
