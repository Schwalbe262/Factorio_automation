from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.nest_assault import (
    NestAssault,
    stage_candidates,
    choose_bastions,
    deployment_stage,
    execute_start_assault,
    START_ASSAULT_LUA,
    _payload,
    _hash,
)
from factorio_agent.defense_hardening import wall_bastion, mutual_coverage

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


class AssaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = SimpleNamespace(
            fingerprint="c",
            recipes={
                "firearm-magazine": {
                    "ingredients": [{"name": "iron-plate", "amount": 4, "type": "item"}],
                    "products": [{"name": "firearm-magazine", "amount": 1}],
                },
                "piercing-rounds-magazine": {
                    "ingredients": [
                        {"name": "copper-plate", "amount": 2, "type": "item"},
                        {"name": "steel-plate", "amount": 1, "type": "item"},
                        {"name": "firearm-magazine", "amount": 2, "type": "item"},
                    ],
                    "products": [{"name": "piercing-rounds-magazine", "amount": 2}],
                    "energy": 6,
                },
            },
        )
        self.game = SimpleNamespace(
            cfg=SimpleNamespace(runtime_dir=self.root), backend="assisted", expected_world="w", query=Mock()
        )
        self.bootstrap = Mock()
        self.builder = Mock()
        self.perimeter = Mock()
        self.perimeter.ammo_source = None
        self.op = NestAssault(self.game, self.bootstrap, self.builder, self.catalog, self.perimeter)
        self.obs = {"ok": True, "world_id": "w", "actor_unit_number": 11, "inventory": {}, "entities": []}
        self.target = {
            "name": "small-worm-turret",
            "unit_number": 200,
            "position": {"x": 35, "y": -10},
            "world_id": "w",
            "surface_index": 1,
            "force_index": 2,
            "role": "worm",
            "factory_distance": 20,
            "dangerously_close": True,
            "combat": {"range": 25},
        }
        self.report = {
            "targets": [self.target],
            "gun": {"range": 18},
            "friendly_guns": [
                {
                    "name": "gun-turret",
                    "unit_number": i,
                    "position": {"x": x, "y": y},
                    "health": 400,
                    "max_health": 400,
                    "ammo": {"full_normal_magazines": 20},
                }
                for i, x, y in ((301, 0, 0), (302, -4, -2), (303, 4, -2))
            ],
        }

    def state(self):
        posts = [{"name": str(i), "position": {"x": 8, "y": y}} for i, y in enumerate((-8, -4, 0))]
        stage = {"phase": "deploy", "posts": posts, "bindings": {}, "deployment_ids": {}, "backstop_specs": []}
        return {
            "world_id": "w",
            "catalog_fingerprint": "c",
            "actor_unit": 11,
            "id": "operation",
            "targets": [self.target],
            "target_set_hash": _hash([self.target]),
            "active_stage": 0,
            "stages": [stage],
            "phase": "stock",
            "ammo_item": "firearm-magazine",
            "handcrafted_magazines_issued": 0,
        }

    def test_generated_frontier_preserves_two_actual_distinct_armed_rears_and_finite_budget(self):
        choices = stage_candidates(self.report)
        self.assertLessEqual(len(choices), 96)
        self.assertTrue(choices)
        for q in choices:
            self.assertGreaterEqual(len({b["unit_number"] for b in q["support"]}), 2)
            self.assertTrue(
                all(
                    (q["position"]["x"] - b["position"]["x"]) ** 2 + (q["position"]["y"] - b["position"]["y"]) ** 2
                    <= 256
                    for b in q["support"]
                )
            )
        self.report["friendly_guns"][1]["ammo"]["full_normal_magazines"] = 0
        self.report["friendly_guns"][2]["health"] = 199
        self.assertEqual(stage_candidates(self.report), [])

    def test_group_footprints_and_future_protected_corridor_are_never_waived(self):
        choices = [{"position": {"x": 8, "y": y}} for y in (-8, -4, 0)]
        groups = list(choose_bastions(choices, [], 16))
        self.assertTrue(groups)
        bs, hotspot = groups[0]
        self.assertTrue(mutual_coverage(bs, hotspot, 16))
        self.assertEqual(sum(len(b["entities"]) for b in bs), 27)
        self.assertFalse(list(choose_bastions(choices, [[[8, -12], [9, 2]]], 16)))

    def test_reachable_stage_budget_rejects_distant_armed_guns_and_final_nonfiring_stage(self):
        report = deepcopy(self.report)
        report["targets"][0]["position"] = {"x": 100, "y": 0}
        self.assertEqual(stage_candidates(report), [])
        choices = stage_candidates(self.report, remaining_stages=1)
        for q in choices:
            self.assertLessEqual(((q["position"]["x"] - 35) ** 2 + (q["position"]["y"] + 10) ** 2) ** 0.5, 18)

    def test_frontier_sample_contains_spatially_separated_cages_instead_of_only_dense_leading_cells(self):
        choices = stage_candidates(self.report)
        separated = []
        for q in choices:
            if all(max(abs(q["position"][k] - p["position"][k]) for k in ("x", "y")) >= 2 for p in separated):
                separated.append(q)
            else:
                break
        self.assertGreater(len(separated), 24)
        self.assertTrue(list(choose_bastions(choices, [], 16)))

    def test_busy_real_crafting_queue_never_debits_finite_supplement_budget_again(self):
        self.op.state = self.state()
        native = {"native_started": False, "main_ammo": {"full_normal_magazines": 0}, "posts": []}
        obs = dict(
            self.obs, inventory={"gun-turret": 11, "stone-wall": 72}, crafting_queue=[{"recipe": "firearm-magazine"}]
        )
        result = self.op._prestock(obs, native)
        self.assertEqual(result["reason"], "assault_waiting_normally_prepaid_engine_crafting_queue")
        self.assertEqual(self.op.state["handcrafted_magazines_issued"], 0)
        self.bootstrap.ensure_item.assert_not_called()

    def stock_fixture(self, *, started=False):
        self.op.state = self.state()
        self.op.state["ammo_item"] = "piercing-rounds-magazine"
        self.op._save()
        self.obs["inventory"] = {"gun-turret": 11, "stone-wall": 72, "copper-plate": 50}
        native = {
            "ok": True,
            "native_started": started,
            "main_ammo": {"full_normal_magazines": 0},
            "posts": [],
            "targets": [{"unit_number": 200, "present": True}],
            "loss_count": 0,
            "tick": 200,
            "listener_owned": True,
            "started_tick": 100,
            "deadline_tick": 10900,
        }
        return native

    def test_step_stock_collection_and_queue_wait_leave_exact_checkpoint_unchanged(self):
        native = self.stock_fixture()
        before = self.op.path.read_bytes()
        transfer = {"type": "take_many", "item": "steel-plate", "count": 25}
        self.bootstrap.ensure_item.return_value = transfer
        with patch.object(self.op, "inspect", return_value=native):
            self.assertEqual(self.op.step(self.obs), transfer)
            self.assertEqual(json.loads(before), self.op.state)
            self.assertEqual(self.op.path.read_bytes(), before)
            self.assertEqual(self.op.step(self.obs), transfer)
            self.obs["crafting_queue"] = [{"recipe": "steel-plate"}]
            self.assertEqual(self.op.step(self.obs)["reason"], "assault_waiting_normally_prepaid_engine_crafting_queue")
            self.assertEqual(self.op.step(self.obs)["reason"], "assault_waiting_normally_prepaid_engine_crafting_queue")
        self.assertNotIn("crafted_by_item", self.op.state)
        self.assertEqual(self.op.path.read_bytes(), before)

    def test_step_precursor_factory_transfer_does_not_create_unsaved_accounting(self):
        native = self.stock_fixture()
        self.obs["inventory"]["steel-plate"] = 25
        source = Mock(commissioned=True)
        source.ensure_full_magazines.return_value = {"type": "take_full_magazines", "count": 50}
        self.perimeter.ammo_source = source
        self.perimeter.inspect.return_value = {"ok": True, "actor_full_magazines": 0}
        before = self.op.path.read_bytes()
        with patch.object(self.op, "inspect", return_value=native):
            self.assertEqual(self.op.step(self.obs)["type"], "take_full_magazines")
            self.assertEqual(self.op.step(self.obs)["type"], "take_full_magazines")
        self.assertEqual(self.op.path.read_bytes(), before)
        self.assertEqual(json.loads(before), self.op.state)

    def test_step_craft_receipt_reconcile_then_collection_keeps_checkpoint_equal(self):
        native = self.stock_fixture()
        self.obs["inventory"]["steel-plate"] = 25
        self.perimeter.inspect.return_value = {"ok": True, "actor_full_magazines": 50}
        with patch.object(self.op, "inspect", return_value=native):
            action = self.op.step(self.obs)
        self.assertEqual((action["type"], action["recipe"], action["count"]), ("craft", "piercing-rounds-magazine", 25))
        rows = [
            {"event": "action_intent", "key": "job", "tick": 200, "action": action},
            {"event": "action_result", "key": "job", "tick": 200, "result": {"ok": True, "started": 25}},
        ]
        (self.root / "job-events.jsonl").write_text("".join(json.dumps(q) + "\n" for q in rows))
        self.obs["inventory"]["steel-plate"] = 0
        self.bootstrap.ensure_item.return_value = {"type": "take_many", "item": "steel-plate"}
        with patch.object(self.op, "inspect", return_value=native):
            self.assertEqual(self.op.step(self.obs)["type"], "take_many")
            self.assertEqual(self.op.state, json.loads(self.op.path.read_text()))
            self.assertEqual(self.op.step(self.obs)["type"], "take_many")
        self.assertEqual(self.op.state["crafted_by_item"]["piercing-rounds-magazine"], 50)
        self.assertNotIn("pending_craft", self.op.state)

    def test_native_clock_is_saved_before_listener_restore_or_stock_collection_return(self):
        native = self.stock_fixture(started=True)
        native["listener_owned"] = False
        native["listener_state"] = "missing"
        with patch.object(self.op, "inspect", return_value=native):
            self.assertEqual(self.op.step(self.obs)["type"], "start_nest_assault")
            self.assertEqual(self.op.state, json.loads(self.op.path.read_text()))
            self.assertEqual(self.op.step(self.obs)["type"], "start_nest_assault")
            native["listener_owned"] = True
            self.bootstrap.ensure_item.return_value = {"type": "take_many", "item": "steel-plate"}
            self.assertEqual(self.op.step(self.obs)["type"], "take_many")
            self.assertEqual(self.op.state, json.loads(self.op.path.read_text()))

    def test_rejected_overbudget_reconcile_keeps_pending_authority_unchanged(self):
        self.op.state = self.state()
        action = self.op._craft("piercing-rounds-magazine", 101, "bounded rejection fixture")
        before = self.op.path.read_bytes()
        rows = [
            {"event": "action_intent", "key": "job", "tick": 100, "action": action},
            {"event": "action_result", "key": "job", "tick": 100, "result": {"ok": True, "started": 101}},
        ]
        (self.root / "job-events.jsonl").write_text("".join(json.dumps(q) + "\n" for q in rows))
        self.assertEqual(
            self.op._reconcile_craft()["reason"], "assault_native_started_crafting_exceeds_finite_product_bound"
        )
        self.assertEqual(self.op.state, json.loads(before))
        self.assertEqual(self.op.path.read_bytes(), before)

    def test_only_paired_actual_started_recipe_runs_charge_budget_busy_receipt_costs_zero(self):
        self.op.state = self.state()

        def result_for(action, receipt):
            rows = [
                {"event": "action_intent", "key": "job", "tick": 100, "action": action},
                {"event": "action_result", "key": "job", "tick": 100, "result": receipt},
            ]
            (self.root / "job-events.jsonl").write_text("".join(json.dumps(q) + "\n" for q in rows))

        a = self.op._craft("piercing-rounds-magazine", 25, "test ordinary craft")
        self.assertEqual(self.op.state["handcrafted_magazines_issued"], 0)
        result_for(a, {"ok": True, "status": "waiting", "reason": "crafting_queue_busy"})
        self.assertIsNone(self.op._reconcile_craft())
        self.assertEqual(self.op.state["handcrafted_magazines_issued"], 0)
        self.assertNotIn("pending_craft", self.op.state)
        a = self.op._craft("piercing-rounds-magazine", 25, "test actual partial started")
        result_for(a, {"ok": True, "status": "running", "started": 17})
        self.assertIsNone(self.op._reconcile_craft())
        self.assertEqual(self.op.state["crafted_by_item"]["piercing-rounds-magazine"], 34)
        self.assertIsNone(self.op._reconcile_craft())
        self.assertEqual(self.op.state["handcrafted_magazines_issued"], 34)
        a = self.op._craft("firearm-magazine", 25, "test unknown reply")
        result_for(a, {"ok": False, "reason": "action_exception"})
        self.assertEqual(
            self.op._reconcile_craft()["reason"], "assault_craft_outcome_unknown_requires_paired_native_receipt"
        )
        self.assertIn("pending_craft", self.op.state)
        self.assertEqual(self.op.state["handcrafted_magazines_issued"], 34)

    def test_red_preferred_after_native_research_and_missing_ordinary_materials_are_explicit(self):
        obs = deepcopy(self.obs)
        obs["inventory"] = {"iron-plate": 480, "steel-plate": 60, "copper-plate": 120}
        report = {"recipes_enabled": {"piercing-rounds-magazine": True}}
        item, evidence = self.op._select_ammo(obs, report)
        self.assertEqual(item, "piercing-rounds-magazine")
        self.assertEqual(evidence["runs"], 100)
        self.assertEqual(evidence["normal_character_crafting_seconds"], 600)
        obs["inventory"]["steel-plate"] = 59
        item, evidence = self.op._select_ammo(obs, report)
        self.assertEqual(item, "piercing-rounds-magazine")
        self.assertEqual(evidence["missing_ordinary_materials"]["steel-plate"], 41)
        report["recipes_enabled"]["piercing-rounds-magazine"] = False
        self.assertEqual(self.op._select_ammo(obs, report)[0], "firearm-magazine")

    def test_scope_publication_never_changes_global_perimeter_or_its_watch_posts(self):
        self.op.state = self.state()
        s = self.op.state["stages"][0]
        s.update(
            block="offense",
            plan={"entities": []},
            scope_id="scope",
            scope={k: [] for k in ("areas", "cells", "covering_posts", "new_posts")},
        )
        s["scope"]["effective_range"] = 16
        (self.root / "pcb-board.json").write_text(
            json.dumps({"world_id": "w", "catalog_fingerprint": "c", "blocks": {}})
        )
        globalpath = self.root / "defense-perimeter.json"
        globalpath.write_text("DO NOT TOUCH")
        self.assertIsNone(self.op._register(self.obs, s))
        self.assertEqual(globalpath.read_text(), "DO NOT TOUCH")
        self.assertEqual(s["perimeter"]["posts"], s["posts"])
        self.perimeter._save.assert_not_called()

    def test_service_failure_with_clearable_trees_never_accepts_or_latches_a_plan(self):
        choices = [
            {
                "position": {"x": 8, "y": y},
                "support": [dict(q, ammo_item="firearm-magazine") for q in self.report["friendly_guns"][:2]],
            }
            for y in (-8, -4, 0)
        ]
        self.game.query.side_effect = [
            {"ok": True, "legal": [1, 2, 3]},
            {"ok": False, "reason": "disconnected"},
            {"ok": False, "reason": "disconnected"},
        ]
        with (
            patch("factorio_agent.nest_assault.protected_factory_areas", return_value=[]),
            patch("factorio_agent.nest_assault.mining_reservations", return_value=[]),
            patch("factorio_agent.nest_assault.stage_candidates", return_value=choices),
            patch(
                "factorio_agent.nest_assault.preflight",
                return_value={"ok": True, "clear_obstacles": [{"name": "tree"}]},
            ),
        ):
            result = self.op._stage(self.obs, self.report, [self.target], "operation", 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["recent_failures"], ["disconnected"])
        self.assertEqual(result["whole_group_attempts"], 1)
        self.assertEqual(self.game.query.call_count, 3)
        self.assertEqual(self.op.state, {})
        self.assertFalse(self.op.path.exists())

    def test_first_payment_is_one_complete_three_gun_group_never_ordinary_cold_gun_build(self):
        self.op.state = self.state()
        stage = self.op.state["stages"][0]
        stage["plan"] = {"entities": [], "clear_obstacles": []}
        stage["underfire_policy"] = {"already": "frozen"}
        self.op._save()
        native = {
            "ok": True,
            "native_started": True,
            "listener_owned": True,
            "tick": 200,
            "started_tick": 100,
            "deadline_tick": 10900,
            "loss_count": 0,
            "targets": [{"unit_number": 200, "present": True}],
            "posts": [{"name": p["name"]} for p in stage["posts"]],
        }
        action = {
            "type": "build_and_arm_bastion_group",
            "group_id": "group",
            "deployments": [{"post_name": p["name"], "deployment_id": "id" + p["name"]} for p in stage["posts"]],
        }
        self.builder.ensure_plan.return_value = {"status": "succeeded"}
        with (
            patch.object(self.op, "inspect", return_value=native),
            patch.object(self.op, "_prestock", return_value=None),
            patch.object(self.op, "_access", return_value={"ok": True}),
            patch.object(self.op, "_register", return_value=None),
            patch("factorio_agent.defense_deployment.bastion_group_deployment_action", return_value=action) as emitter,
        ):
            result = self.op.step(self.obs)
        self.assertEqual(result, action)
        emitter.assert_called_once_with(self.game, self.obs, owner_kind="nest_assault")
        self.assertEqual(len(stage["deployment_ids"]), 3)
        self.assertEqual(self.builder.ensure_plan.call_args.args[1]["entities"], [])

    def test_absence_and_foreign_or_unknown_death_never_prove_assault_success(self):
        self.op.state = self.state()
        base = {
            "native_started": True,
            "listener_owned": True,
            "targets": [{"unit_number": 200, "present": False}],
            "tick": 30,
            "deaths": {},
            "loss_count": 0,
            "posts": [],
        }
        self.assertIsNone(self.op._proof(base))
        base["deaths"] = {"200": {"own_gun_projectile": False}}
        self.assertIsNone(self.op._proof(base))
        base["deaths"]["200"]["own_gun_projectile"] = True
        self.assertEqual(self.op._proof(base)["targets_cleared"], 1)
        base["targets"][0]["present"] = True
        self.assertIsNone(self.op._proof(base))
        base["targets"] = []
        self.assertIsNone(self.op._proof(base))

    def test_time_and_loss_budget_stops_before_any_new_payment_or_build(self):
        for tick, losses in ((10901, 0), (200, 2)):
            self.op.state = self.state()
            self.op._save()
            native = {
                "ok": True,
                "native_started": True,
                "listener_owned": True,
                "tick": tick,
                "started_tick": 100,
                "deadline_tick": 10900,
                "loss_count": losses,
                "targets": [{"unit_number": 200, "present": True}],
                "posts": [],
            }
            with patch.object(self.op, "inspect", return_value=native):
                r = self.op.step(self.obs)
            self.assertEqual(r["reason"], "assault_finite_time_or_post_loss_limit")
        self.builder.ensure_plan.assert_not_called()
        self.bootstrap.ensure_item.assert_not_called()

    def test_unknown_initialization_receipt_stays_unknown_not_automatically_successful(self):
        self.op.state = self.state()
        self.op._save()
        a = {
            "type": "start_nest_assault",
            "operation_id": "operation",
            "target_set_hash": self.op.state["target_set_hash"],
            "expected_world_id": "w",
            "expected_actor_unit": 11,
        }
        self.game.query.side_effect = TimeoutError("lost native reply")
        self.assertEqual(execute_start_assault(self.game, a)["reason"], "nest_assault_initialization_outcome_unknown")
        self.game.query.side_effect = None
        self.game.query.return_value = {"surprise": True}
        self.assertEqual(execute_start_assault(self.game, a)["reason"], "nest_assault_initialization_outcome_unknown")

    def test_stage_adapter_fails_closed_when_no_active_finite_stage(self):
        for state in ({}, dict(self.state(), active_stage=3), dict(self.state(), phase="stopped")):
            with self.assertRaises(ValueError):
                deployment_stage(state)

    @unittest.skipUnless(LuaRuntime, "optional executable Lua runtime")
    def test_native_death_ledger_preserves_unknown_handler_and_only_credits_real_physical_friendly_gun_cause(self):
        lua = LuaRuntime(unpack_returned_tuples=True)

        def table(v):
            if isinstance(v, dict):
                return lua.table_from({k: table(q) for k, q in v.items()})
            if isinstance(v, list):
                return lua.table_from([table(q) for q in v])
            return v

        lua.execute("""d={world_id="w"};storage={deterministic_player=d};game={tick=100};defines={events={on_entity_died=1}}
f={index=1};s={name="nauvis",index=1};enemy={index=2};a={valid=true,unit_number=11,position={x=-100,y=-100},force=f,surface=s}
script={};local handler=nil;script.get_event_handler=function()return handler end;script.on_event=function(_,fn)handler=fn end
foe={valid=true,name="small-worm-turret",unit_number=200,position={x=35,y=-10},force=enemy,surface=s}
target=function(p,name)if foe.valid and foe.name==name and foe.position.x==p.x and foe.position.y==p.y then return foe end end
""")
        state = self.state()
        args = {
            "expected_world_id": "w",
            "expected_actor_unit": 11,
            "catalog_fingerprint": "c",
            "operation_id": "operation",
            "targets": [self.target],
            "target_set_hash": state["target_set_hash"],
            "backstop_specs": [],
        }
        lua.globals().x = table(args)
        lua.execute("foreign=function()end;script.on_event(1,foreign)")
        run = lambda: lua.execute("return(function()" + " ".join(START_ASSAULT_LUA.splitlines()) + "end)()")
        self.assertEqual(run()["reason"], "assault_existing_death_handler_requires_safe_integration")
        self.assertIsNone(lua.globals().d.nest_assaults)
        lua.execute("script.on_event(1,nil)")
        self.assertTrue(run()["ok"])
        lua.execute("game.tick=500;script.on_event(1,nil)")
        reused = run()
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["started_tick"], 100)
        self.assertEqual(reused["deadline_tick"], 10900)
        lua.execute("""op=d.nest_assaults.operation;op.posts["9"]={unit_number=9}
cause={valid=true,unit_number=9,type="ammo-turret",name="gun-turret",force=f}
event={entity=foe,cause=cause,tick=110,damage_type={name="physical"}};script.get_event_handler(1)(event)
""")
        self.assertTrue(lua.globals().op.deaths["200"].own_gun_projectile)
        self.assertEqual(lua.globals().foe.valid, True)
        lua.execute('event.damage_type.name="acid";script.get_event_handler(1)(event)')
        self.assertFalse(lua.globals().op.deaths["200"].own_gun_projectile)
        lua.execute('event.damage_type.name="physical";cause.force=enemy;script.get_event_handler(1)(event)')
        self.assertFalse(lua.globals().op.deaths["200"].own_gun_projectile)


if __name__ == "__main__":
    unittest.main()
