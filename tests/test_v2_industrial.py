from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.brick_production import brick_core
from factorio_agent.bus_tap import extend_bus, replace_bus
from factorio_agent.industrial import IndustrialOperations, PlanningBudget, free_tap_parameters, industrial_site_anchors
from factorio_agent.main_bus import BusMaterial, ConsumerTap, plan_main_bus
from factorio_agent.smelting import port


def catalog():
    return SimpleNamespace(
        fingerprint="catalog",
        recipes={
            "steel-plate": {
                "energy": 16,
                "categories": ["smelting"],
                "ingredients": [{"name": "iron-plate", "amount": 5}],
                "products": [{"name": "steel-plate", "amount": 1}],
            },
            "stone-brick": {
                "energy": 3.2,
                "categories": ["smelting"],
                "ingredients": [{"name": "stone", "amount": 2}],
                "products": [{"name": "stone-brick", "amount": 1}],
            },
        },
        entities={
            "stone-furnace": {
                "burner": True,
                "crafting_speed": 1,
                "crafting_categories": ["smelting"],
                "energy_usage": 1500,
            },
            "inserter": {},
            "fast-inserter": {},
        },
        items={"coal": {"fuel_value": 4000000}},
    )


def saved_bus():
    return plan_main_bus(
        (BusMaterial("iron-plate", -11.5, (225,), 2), BusMaterial("copper-plate", -3.5, (112.5,), 2)),
        (
            ConsumerTap("red-iron", "iron-plate", -87.5, "critical", demand_per_minute=60),
            ConsumerTap("green-copper", "copper-plate", -107.5, "critical", demand_per_minute=52.5),
        ),
        north_y=-118.5,
        south_y=-12.5,
        port_x=12.5,
    )


def material(item, unit, x, y):
    return dict(
        port(item, x, y, 4, "output"),
        world_id="w",
        unit_number=unit,
        material_supply_id="bank:" + item,
        nominal_shared_rate_per_minute=60,
        **({"mining_resource": "stone"} if item == "stone" else {}),
    )


def observation(bus):
    result = {
        "world_id": "w",
        "tick": 100,
        "inventory": {},
        "technologies": {"steel-processing": True},
        "enabled_recipes": {"steel-plate": True, "stone-brick": True, "fast-inserter": True},
        "entities": [dict(spec, unit_number=1000 + i, belt_inventory={}) for i, spec in enumerate(bus["entities"])],
    }
    mouths = {
        (e["position"]["x"], e["position"]["y"]): e for e in result["entities"] if e["name"] == "underground-belt"
    }
    for pair in bus["underground_pairs"]:
        first, last = mouths[tuple(pair["input"])], mouths[tuple(pair["output"])]
        for a, b in ((first, last), (last, first)):
            a.update(underground_pair_verified=True, underground_neighbour={"unit_number": b["unit_number"]})
    return result


def fixture(root):
    bus = saved_bus()
    obs = observation(bus)
    common = {"world_id": "w", "catalog_fingerprint": "catalog"}
    plan = _plan(deepcopy(bus["entities"]), [], underground_pairs=bus["underground_pairs"])
    documents = {
        "bus-factory.json": dict(common, bus=bus, phase="verified", remove=[], cleanup=[], plan=plan, red_plan=plan),
        "automation-science.json": dict(common, plan=plan),
        "logistic-science.json": dict(common, plan=plan),
        "pcb-board.json": dict(
            common, shared_main_bus=bus, blocks={"logistic_science": dict(plan, reserved_area=[[39, -82], [71, -54]])}
        ),
    }
    for name, value in documents.items():
        Path(root, name).write_text(json.dumps(value))
    registry = dict(
        common,
        sources={
            "coal": material("coal", 5277, -60.5, 30.5),
            "brick-coal": material("coal", 5278, -60.5, 31.5),
            "stone": material("stone", 5400, -20.5, 44.5),
        },
    )
    Path(root, "material-sources.json").write_text(json.dumps(registry))
    game = SimpleNamespace(
        cfg=SimpleNamespace(runtime_dir=root),
        query=Mock(return_value={"ok": True, "max_span": 5, "clear_obstacles": []}),
    )
    builder = SimpleNamespace(
        _occupied_by_plan=FactoryBuilder.__new__(FactoryBuilder)._occupied_by_plan,
        bootstrap=SimpleNamespace(ensure_item=Mock()),
        ensure_plan=Mock(),
    )
    defense = SimpleNamespace(
        ensure_coverage=Mock(return_value=None), coverage_proof=Mock(return_value={"armed": True})
    )
    adapter = IndustrialOperations(game, builder, catalog(), defense)
    return adapter, obs, bus, registry


class IndustrialSelectionTests(unittest.TestCase):
    def test_free_station_rows_follow_translated_bus_geometry_and_spacing(self):
        bus = plan_main_bus(
            (BusMaterial("iron-plate", 15.5, (225,), 2),),
            (ConsumerTap("existing", "iron-plate", 60.5, "critical"),),
            north_y=50.5,
            south_y=90.5,
            port_x=40.5,
        )
        choices = free_tap_parameters(bus, name="future-mall", item="iron-plate", demand_per_minute=12)
        self.assertEqual(choices[0]["splitter_y"], 52.5)
        self.assertTrue(all(abs(row["splitter_y"] - 60.5) >= 3 and row["line"] == 0 for row in choices))
        self.assertTrue(all(row["splitter_y"] != -115.5 for row in choices))

    def test_site_anchors_include_land_alternatives_and_move_with_current_board(self):
        bus = saved_bus()
        board = {"blocks": {"green": {"reserved_area": [[39, -82], [71, -54]]}}}
        anchors = industrial_site_anchors(bus, board, "stone-brick")
        self.assertEqual(anchors[0], (82, -18))
        self.assertIn((82, -62), anchors)
        board["blocks"]["green"]["reserved_area"][1][0] += 20
        self.assertEqual(industrial_site_anchors(bus, board, "stone-brick")[0], (102, -18))
        self.assertEqual(len(anchors), 8)

    def test_actual_tap_prepare_is_read_only_and_uses_catalog_demand(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, obs, _, _ = fixture(root)
            before = {p.name: p.read_bytes() for p in Path(root).glob("*.json")}
            ready = adapter.prepare_steel_supply(obs)
            self.assertTrue(ready["ok"], ready)
            self.assertEqual(ready["parameters"]["splitter_y"], -116.5)
            self.assertEqual(ready["parameters"]["demand_per_minute"], 37.5)
            self.assertEqual(ready["parameters"]["priority"], "surplus")
            self.assertEqual(adapter.state, {})
            self.assertEqual(before, {p.name: p.read_bytes() for p in Path(root).glob("*.json")})
            adapter.builder.ensure_plan.assert_not_called()

    def test_station_search_is_bounded_and_failed_candidates_are_never_saved(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, obs, _, _ = fixture(root)
            with (
                patch("factorio_agent.industrial.BusTap") as preview,
                patch("factorio_agent.industrial.time.monotonic", return_value=0),
            ):
                preview.return_value.prepare.return_value = {
                    "ok": False,
                    "reason": "tap_new_corridor_crosses_resource_patch",
                }
                ready = adapter.prepare_steel_supply(obs)
            self.assertFalse(ready["ok"])
            self.assertEqual(ready["attempts"], 12)
            self.assertEqual(preview.return_value.prepare.call_count, 12)
            self.assertFalse(adapter.path.exists())
            self.assertFalse(adapter.tap.path.exists())

    def test_native_coal_identity_failure_blocks_tap_before_any_paid_action(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, obs, _, _ = fixture(root)
            adapter.game.query.return_value = {"ok": False, "reason": "industrial_source_identity_changed"}
            with patch.object(adapter.tap, "step") as step:
                result = adapter.steel_supply(obs)
            self.assertEqual(result["reason"], "industrial_source_identity_changed")
            step.assert_not_called()
            self.assertFalse(adapter.path.exists())

    def test_brick_preview_can_choose_northern_land_after_default_water_sites(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, obs, _, _ = fixture(root)
            route = _plan([], [], underground_pairs=[], clear_obstacles=[], route_metrics={"turns": 2, "length": 10})
            with (
                patch("factorio_agent.brick_production.survey_slots", return_value={"ok": True, "legal": [5]}),
                patch("factorio_agent.brick_production.plan_connection", return_value=route),
                patch(
                    "factorio_agent.brick_production.connect_power_groups", return_value={"ok": True, "entities": []}
                ),
                patch("factorio_agent.brick_production.preflight", return_value={"ok": True, "clear_obstacles": []}),
            ):
                ready = adapter.prepare_brick_production(obs)
            self.assertTrue(ready["ok"], ready)
            self.assertEqual(ready["core"]["producer_rows"]["right"][0]["position"], {"x": 82, "y": -62})
            self.assertEqual(ready["sources"][0]["mining_resource"], "stone")
            self.assertFalse(adapter.path.exists())
            self.assertFalse(adapter.brick.path.exists())

    def test_world_catalog_or_missing_material_registry_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, obs, _, _ = fixture(root)
            adapter.state = {"world_id": "other", "catalog_fingerprint": "catalog"}
            self.assertEqual(adapter.prepare_steel_supply(obs)["reason"], "industrial_world_or_catalog_changed")
            adapter.game.query.assert_not_called()
            adapter.state = {}
            Path(root, "material-sources.json").unlink()
            self.assertFalse(adapter.prepare_brick_production(obs)["ok"])
            self.assertIsNone(adapter.brick_production_proof(obs))

    def test_budget_blocks_further_queries_after_deadline(self):
        game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir="unused"), query=Mock())
        clock = [0]

        def slow(script):
            clock[0] = 21
            return {"ok": True}

        game.query.side_effect = slow
        with patch("factorio_agent.industrial.time.monotonic", side_effect=lambda: clock[0]):
            budget = PlanningBudget(game)
            self.assertEqual(budget.query("first")["reason"], "industrial_planning_budget_exhausted")
            self.assertEqual(budget.query("second")["reason"], "industrial_planning_budget_exhausted")
        self.assertEqual(game.query.call_count, 1)


class IndustrialSupplyTests(unittest.TestCase):
    def installed(self, root):
        adapter, obs, bus, registry = fixture(root)
        parameters = free_tap_parameters(bus, name="steel-iron", item="iron-plate", demand_per_minute=37.5)[0]
        new = extend_bus(bus, parameters, obs)["bus"]
        for name in ("bus-factory.json", "automation-science.json", "logistic-science.json", "pcb-board.json"):
            path = Path(root, name)
            doc = json.loads(path.read_text())
            if name == "bus-factory.json":
                doc["bus"] = new
                for key in ("plan", "red_plan"):
                    doc[key] = replace_bus(doc[key], bus, new)
            elif name == "pcb-board.json":
                doc["shared_main_bus"] = new
                doc["blocks"]["logistic_science"] = replace_bus(doc["blocks"]["logistic_science"], bus, new)
            else:
                doc["plan"] = replace_bus(doc["plan"], bus, new)
            path.write_text(json.dumps(doc))
        output = deepcopy(next(p for p in new["ports"] if p["name"] == "steel-iron"))
        splitter = next(row["splitter"] for row in new["stations"] if row["name"] == "steel-iron")
        output.update(
            unit_number=7000,
            world_id="w",
            priority_splitter={
                "name": "splitter",
                "position": splitter["position"],
                "unit_number": 7001,
                "direction": 0,
            },
        )
        adapter.tap.state = {
            "world_id": "w",
            "catalog_fingerprint": "catalog",
            "phase": "verified",
            "parameters": parameters,
        }
        adapter.tap.proof = Mock(return_value={"port": output})
        adapter.tap.step = Mock(return_value={"status": "succeeded"})
        return adapter, obs, registry, output

    def test_exact_supply_is_bound_only_after_successful_tap_proof(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, obs, _, output = self.installed(root)
            adapter.tap.state["phase"] = "commission"
            adapter.tap.step.return_value = {"status": "waiting"}
            self.assertEqual(adapter.steel_supply(obs)["status"], "waiting")
            self.assertNotIn("steel_sources", adapter.state)
            adapter.tap.state["phase"] = "verified"
            adapter.tap.step.return_value = {"status": "succeeded"}
            self.assertEqual(adapter.steel_supply(obs)["status"], "succeeded")
            saved = json.loads(adapter.path.read_text())
            self.assertEqual(saved["steel_sources"]["iron"]["unit_number"], output["unit_number"])
            self.assertEqual(saved["steel_sources"]["iron"]["priority_splitter"]["unit_number"], 7001)
            self.assertEqual(saved["steel_sources"]["coal"]["unit_number"], 5277)

    def test_cached_supply_rechecks_native_unit_priority_and_registry_identity(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, obs, registry, _ = self.installed(root)
            self.assertEqual(adapter.steel_supply(obs)["status"], "succeeded")
            before = deepcopy(adapter.state["steel_sources"])
            adapter.game.query.return_value = {"ok": False, "reason": "industrial_surplus_priority_changed"}
            self.assertIsNone(adapter.steel_supply_proof(obs))
            self.assertEqual(adapter.steel_supply(obs)["status"], "blocked")
            self.assertEqual(adapter.state["steel_sources"], before)
            adapter.game.query.return_value = {"ok": True}
            registry["sources"]["coal"]["unit_number"] = 8888
            Path(root, "material-sources.json").write_text(json.dumps(registry))
            self.assertEqual(adapter.steel_supply(obs)["status"], "blocked")
            self.assertEqual(adapter.state["steel_sources"], before)


class IndustrialDefenseGateTests(unittest.TestCase):
    def producer(self, root):
        adapter, obs, _, registry = fixture(root)
        core = brick_core(adapter.catalog, 82, -62)
        ready = {
            "ok": True,
            "world_id": "w",
            "catalog_fingerprint": "catalog",
            "phase": "build",
            "core": core,
            "plan": _plan(core["entities"], core["ports"], reserved_area=core["reserved_area"], underground_pairs=[]),
            "sources": [registry["sources"]["stone"], registry["sources"]["brick-coal"]],
        }
        adapter.brick.state = ready
        adapter.brick.step = Mock(return_value={"type": "build", "name": "stone-furnace"})
        adapter.brick.proof = Mock(return_value={"normal_bricks": True})
        return adapter, obs

    def test_coverage_action_precedes_every_producer_build_and_reserves_future_core(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, obs = self.producer(root)
            action = {"type": "craft", "recipe": "gun-turret", "count": 1}
            adapter.defense.ensure_coverage.return_value = action
            self.assertEqual(adapter.brick_production(obs), action)
            adapter.brick.step.assert_not_called()
            adapter.builder.ensure_plan.assert_not_called()
            self.assertEqual(
                adapter.defense.ensure_coverage.call_args.args[1:],
                ("industrial:brick", [adapter.brick.state["core"]["reserved_area"]]),
            )
            board = json.loads(Path(root, "pcb-board.json").read_text())
            self.assertEqual(board["blocks"]["brick_production"], adapter.brick.state["plan"])

    def test_whole_plan_is_rechecked_after_defense_then_authorization_is_cached(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, obs = self.producer(root)
            with patch("factorio_agent.industrial.preflight", return_value={"ok": True}) as preflight:
                self.assertEqual(adapter.brick_production(obs)["type"], "build")
                self.assertEqual(adapter.brick_production(obs)["type"], "build")
            self.assertEqual(preflight.call_count, 1)
            self.assertEqual(adapter.brick.step.call_count, 2)
            self.assertIn("brick", json.loads(adapter.path.read_text())["producer_authorizations"])

    def test_defense_or_post_coverage_route_failure_blocks_before_machine_cost(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, obs = self.producer(root)
            adapter.defense.coverage_proof.return_value = None
            self.assertEqual(adapter.brick_production(obs)["reason"], "industrial_native_coverage_proof_required")
            adapter.brick.step.assert_not_called()
            adapter.defense.coverage_proof.return_value = {"armed": True}
            with patch(
                "factorio_agent.industrial.preflight",
                return_value={"ok": False, "reason": "block_terrain_or_immovable_obstruction"},
            ):
                result = adapter.brick_production(obs)
            self.assertEqual(result["stage"], "industrial_plan_after_defense")
            adapter.brick.step.assert_not_called()

    def test_technology_and_missing_defense_block_before_supply_mutations(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, obs, _, _ = fixture(root)
            obs["technologies"] = {}
            with patch.object(adapter, "steel_supply") as supply:
                self.assertEqual(adapter.steel_production(obs)["reason"], "native_steel_processing_required")
                supply.assert_not_called()
            adapter.defense = None
            self.assertEqual(adapter.brick_production(obs)["reason"], "industrial_defense_coverage_required")
            self.assertFalse(adapter.path.exists())

    def test_candidate_diagnostics_reach_public_blocked_evidence_for_job_ledger(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, obs, _, _ = fixture(root)
            diagnostic = {
                "candidate_failures": [{"stage": "raw_feed", "item": "coal", "reason": "no_clean_trunk_corridor"}]
            }
            with (
                patch.object(adapter, "steel_supply", return_value={"status": "succeeded"}),
                patch.object(
                    adapter,
                    "_prepare_production",
                    return_value={
                        "ok": False,
                        "reason": "no_steel_site_with_complete_native_connections",
                        "evidence": diagnostic,
                    },
                ),
            ):
                result = adapter.steel_production(obs)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["evidence"], diagnostic)
            adapter.defense.ensure_coverage.assert_not_called()

    def test_production_proof_requires_armed_coverage_and_current_registered_sources(self):
        with tempfile.TemporaryDirectory() as root:
            adapter, obs = self.producer(root)
            self.assertIsNotNone(adapter.brick_production_proof(obs))
            adapter.defense.coverage_proof.return_value = None
            self.assertIsNone(adapter.brick_production_proof(obs))
            adapter.defense.coverage_proof.return_value = {"armed": True}
            path = Path(root, "material-sources.json")
            saved = json.loads(path.read_text())
            saved["sources"]["stone"]["unit_number"] += 1
            path.write_text(json.dumps(saved))
            self.assertIsNone(adapter.brick_production_proof(obs))


if __name__ == "__main__":
    unittest.main()
