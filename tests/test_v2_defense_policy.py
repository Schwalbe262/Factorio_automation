from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from factorio_agent.defense_policy import DefensePolicy, assess_risk, SURVEY_LUA
from factorio_agent.defense_perimeter import DefensePerimeter
from factorio_agent.defense_coverage import coverage_cells


def asset(unit, x=0, y=0, health=100):
    return dict(
        unit_number=unit,
        name="burner-mining-drill",
        type="mining-drill",
        position={"x": x, "y": y},
        health=health,
        max_health=100,
    )


def threat(unit=90, x=84, y=0, health=15, kind="unit"):
    return dict(
        unit_number=unit,
        name="small-biter",
        type=kind,
        position={"x": x, "y": y},
        health=health,
        max_health=health,
        range=1,
    )


def facts():
    return dict(
        ok=True,
        world_id="w",
        tick=1000,
        surface_index=1,
        force_index=1,
        evolution_factor=0.0348617,
        gun_range=18,
        assets=[asset(1), asset(2, 6), asset(3, 60)],
        guns=[],
        targets=[],
        technologies={"automation": True},
    )


def policy(names=("paid",), level=0):
    risk = assess_risk(facts(), "w")
    risk.update(
        level=level,
        minimum_posts=len(names),
        maximum_posts=len(names),
        ammo_target=20 if level >= 2 else 10,
        refill_below=10 if level >= 2 else 5,
    )
    return dict(
        schema_version=1,
        world_id="w",
        catalog_fingerprint="cat",
        surveyed_tick=1000,
        risk=risk,
        active_posts=list(names),
        covered_producer_units=[1],
        uncovered_producer_units=[2, 3],
        status="deploy",
    )


class RiskTests(unittest.TestCase):
    def test_quiet_native_early_small_enemies_do_not_demand_eight_future_posts(self):
        f = facts()
        f["assets"] = [asset(1)]
        f["targets"] = [threat(), threat(91, 89, kind="unit-spawner", health=350)]
        risk = assess_risk(f, "w")
        self.assertEqual((risk["risk"], risk["minimum_posts"], risk["ammo_target"]), ("quiet", 0, 10))
        self.assertEqual(risk["nearest_unit"], 84)
        self.assertEqual(risk["native_tier"], "early")

    def test_approaching_units_and_current_damage_raise_real_target_and_ammo_requirements(self):
        f = facts()
        f["assets"] = [asset(1)]
        f["targets"] = [threat(x=60)]
        self.assertEqual(assess_risk(f, "w")["minimum_posts"], 2)
        f["assets"][0]["health"] = 80
        high = assess_risk(f, "w")
        self.assertEqual((high["risk"], high["minimum_posts"], high["ammo_target"]), ("high", 3, 20))
        f["assets"][0]["health"] = 40
        self.assertEqual(assess_risk(f, "w")["minimum_posts"], 4)

    def test_old_damage_without_nearby_pressure_does_not_force_new_remote_guns(self):
        f = facts()
        f["assets"] = [asset(1, health=40)]
        f["targets"] = [threat(x=90)]
        self.assertEqual(assess_risk(f, "w")["risk"], "quiet")
        f["recent_damage_units"] = [1]
        self.assertEqual(assess_risk(f, "w")["risk"], "critical")

    def test_native_unit_strength_evolution_and_current_tech_are_not_catalog_assumptions(self):
        f = facts()
        f["assets"] = [asset(1)]
        f["targets"] = [threat(x=23, health=75)]
        self.assertEqual(assess_risk(f, "w")["risk"], "critical")
        f["targets"] = [threat(x=60)]
        f["evolution_factor"] = 0.5
        self.assertEqual(assess_risk(f, "w")["risk"], "high")
        f = facts()
        self.assertEqual(assess_risk(f, "w")["native_tier"], "early")
        f["technologies"]["chemical-science-pack"] = True
        # Quiet low-evolution map: the tier reserve waits for evolution.
        self.assertEqual((assess_risk(f, "w")["native_tier"], assess_risk(f, "w")["minimum_posts"]), ("advanced", 0))
        f["evolution_factor"] = 0.15
        self.assertEqual((assess_risk(f, "w")["native_tier"], assess_risk(f, "w")["minimum_posts"]), ("advanced", 2))

    def test_missing_native_fields_and_wrong_world_fail_closed(self):
        for key, value in [("evolution_factor", None), ("gun_range", None), ("world_id", "other"), ("assets", [])]:
            f = facts()
            f[key] = value
            with self.assertRaises(ValueError):
                assess_risk(f, "w")
        f = facts()
        f["assets"][0].pop("max_health")
        with self.assertRaises(ValueError):
            assess_risk(f, "w")
        self.assertIn("e.max_health", SURVEY_LUA)
        self.assertNotIn("prototype.max_health", SURVEY_LUA)
        self.assertIn("is_chunk_charted", SURVEY_LUA)
        self.assertIn("get_evolution_factor(s)", SURVEY_LUA)


class AdditivePlanningTests(unittest.TestCase):
    def setUp(self):
        self.f = facts()
        self.game = SimpleNamespace(query=Mock(side_effect=lambda _: deepcopy(self.f)))
        self.f["technologies"]["logistic-science-pack"] = True
        self.f["evolution_factor"] = 0.15  # mid tier holds one reserve post above QUIET_EVOLUTION
        self.engine = DefensePolicy(self.game, SimpleNamespace(fingerprint="cat"))
        self.obs = {
            "world_id": "w",
            "tick": 1000,
            "entities": deepcopy(self.f["assets"]),
            "technologies": {"automation": True},
        }

    def native_site(self, game, candidates, areas, reserved, bounds):
        center = {"x": areas[0][0][0] + 2, "y": areas[0][0][1] + 2}
        legal = [p for p in candidates if abs(p["x"] - center["x"]) + abs(p["y"] - center["y"]) >= 6]
        return {
            "ok": True,
            "range": 18,
            "magazine_size": 10,
            "candidates": [{"position": legal[0], "local_walk_access": True}],
        }

    def test_paid_legacy_posts_and_loss_history_are_retained_while_quiet_policy_yields(self):
        state = {
            "phase": "armed",
            "posts": [
                dict(
                    name="paid" + str(i),
                    unit_number=100 + i,
                    position={"x": 0, "y": 6 + i * 4},
                    commissioned=True,
                    lost_unit_numbers=[50 + i],
                )
                for i in range(8)
            ],
        }
        self.f["guns"] = [{"unit_number": p["unit_number"], "position": p["position"]} for p in state["posts"]]
        before = deepcopy(state)
        result = self.engine.prepare(self.obs, state, [])
        self.assertTrue(result["ok"])
        self.assertEqual(result["new_posts"], [])
        self.assertEqual(len(result["policy"]["active_posts"]), 1)
        self.assertEqual(state, before)
        self.assertIn(3, result["policy"]["uncovered_producer_units"])

    def test_fresh_early_site_uses_current_producers_native_resource_and_access_survey(self):
        reserved = [[[20, 20], [30, 30]]]
        with patch("factorio_agent.defense_policy.native_candidate_survey", side_effect=self.native_site) as survey:
            result = self.engine.prepare(self.obs, {}, reserved)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["new_posts"]), 1)
        self.assertEqual(result["new_posts"][0]["focus"], self.f["assets"][0]["position"])
        self.assertEqual(survey.call_args.args[3], reserved)
        self.assertTrue(result["new_posts"][0]["local_walk_access"])
        self.assertEqual(result["policy"]["ammo_item"], "firearm-magazine")

    def test_fresh_map_empty_lua_enemy_and_gun_tables_are_a_valid_quiet_survey(self):
        self.f["targets"] = {}
        self.f["guns"] = {}
        self.f["technologies"] = {"automation": True}
        with patch("factorio_agent.defense_policy.native_candidate_survey", side_effect=self.native_site):
            result = self.engine.prepare(self.obs, {}, [])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["risk"]["risk"], "quiet")
        self.assertEqual(result["new_posts"], [])

    def test_nonempty_malformed_enemy_table_still_fails_closed(self):
        self.f["targets"] = {"bad": "shape"}
        result = self.engine.prepare(self.obs, {}, [])
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "adaptive_defense_policy_unavailable")

    def test_unbuilt_accepted_target_is_idempotent_instead_of_appending_guns_every_action(self):
        with patch("factorio_agent.defense_policy.native_candidate_survey", side_effect=self.native_site) as survey:
            first = self.engine.prepare(self.obs, {}, [])
            state = {"posts": first["new_posts"], "adaptive_policy": first["policy"]}
            again = self.engine.prepare(self.obs, state, [])
        self.assertEqual(again["new_posts"], [])
        self.assertEqual(first["policy"]["active_posts"], again["policy"]["active_posts"])
        self.assertEqual(survey.call_count, 1)
        self.assertEqual(self.game.query.call_count, 1)

    def test_risk_increase_grows_additively_without_moving_the_original_gun(self):
        old = dict(name="paid", unit_number=100, position={"x": 0, "y": 6}, commissioned=True, lost_unit_numbers=[88])
        self.f["guns"] = [{"unit_number": 100, "position": old["position"]}]
        state = {"posts": [old]}
        quiet = self.engine.prepare(self.obs, state, [])
        state["adaptive_policy"] = quiet["policy"]
        self.f["targets"] = [threat(x=30)]
        self.f["tick"] = 2000
        self.obs["tick"] = 2000
        with patch("factorio_agent.defense_policy.native_candidate_survey", side_effect=self.native_site):
            high = self.engine.prepare(self.obs, state, [])
        self.assertTrue(high["ok"])
        self.assertEqual(high["risk"]["minimum_posts"], 3)
        self.assertEqual(len(high["policy"]["active_posts"]), 3)
        self.assertEqual(len(high["new_posts"]), 2)
        self.assertEqual(state["posts"][0], old)
        self.assertEqual(old["unit_number"], 100)

    def test_changed_producer_health_refreshes_native_risk_before_cached_interval(self):
        self.f["guns"] = [{"unit_number": 100, "position": {"x": 0, "y": 6}}]
        self.engine.survey(self.obs)
        self.obs["tick"] += 1
        self.obs["entities"][0]["health"] = 40
        self.f["assets"][0]["health"] = 40
        result = self.engine.survey(self.obs)
        self.assertEqual(result["recent_damage_units"], [1])
        self.assertEqual(self.game.query.call_count, 2)


class AdaptivePerimeterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.c = DefensePerimeter.__new__(DefensePerimeter)
        self.c.path = Path(temporary.name) / "defense-perimeter.json"
        self.c.catalog = SimpleNamespace(fingerprint="cat")
        self.c.game = SimpleNamespace(query=Mock(return_value={"ok": True, "resources": 0}))
        self.c.adaptive_enabled = True
        self.c.policy_engine = SimpleNamespace(
            prepare=Mock(
                return_value={"ok": True, "range": 18, "policy": policy(), "risk": policy()["risk"], "new_posts": []}
            )
        )
        self.c.state = dict(
            world_id="w",
            catalog_fingerprint="cat",
            phase="armed",
            range=18,
            posts=[
                dict(
                    name="paid", unit_number=100, position={"x": 0, "y": 6}, commissioned=True, lost_unit_numbers=[90]
                ),
                dict(name="future_lab", position={"x": 100, "y": -100}, commissioned=False),
            ],
        )
        self.c._save = Mock()
        self.c.bootstrap = SimpleNamespace(ensure_item=Mock())
        self.report = dict(
            ok=True,
            tick=1001,
            changed=[],
            missing=["future_lab"],
            unarmed=[],
            refill=[],
            actor_full_magazines=10,
            posts=[
                dict(name="paid", unit_number=100, position={"x": 0, "y": 6}, full_magazines=10, ammo_rounds=100),
                dict(name="future_lab", missing=True, full_magazines=0),
            ],
        )
        self.c.inspect = Mock(return_value=self.report)
        self.obs = dict(
            ok=True,
            world_id="w",
            tick=1000,
            inventory={"gun-turret": 0},
            enabled_recipes={"gun-turret": True, "firearm-magazine": True},
        )

    def test_current_target_proof_yields_without_building_unpaid_future_lab_post(self):
        paid = deepcopy(self.c.state["posts"][0])
        result = self.c.step(self.obs)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["evidence"]["active_post_names"], ["paid"])
        self.assertIn("no whole-factory coverage", result["evidence"]["scope"])
        self.assertEqual(self.c.state["posts"][0], paid)
        self.assertEqual(self.c.state["phase"], "armed")
        self.assertTrue(self.c.state["posts"][1]["deferred_by_policy"])
        self.c.bootstrap.ensure_item.assert_not_called()
        self.c.game.query.assert_not_called()

    def test_higher_active_ammo_target_requires_actual_native_full_magazines(self):
        preview = self.c.policy_engine.prepare.return_value
        preview["policy"] = policy(level=2)
        preview["risk"] = preview["policy"]["risk"]
        result = self.c.step(self.obs)
        self.assertEqual((result["type"], result["item"], result["count"]), ("insert", "firearm-magazine", 10))
        self.assertIsNone(self.c.proof(self.obs))

    def test_quiet_commissioned_guard_does_not_top_up_every_shot_before_production_can_resume(self):
        self.report["posts"][0]["full_magazines"] = 7
        self.assertEqual(self.c.step(self.obs)["status"], "succeeded")
        self.c.bootstrap.ensure_item.assert_not_called()

    def test_early_quiet_zero_target_finishes_without_buying_future_posts(self):
        preview = self.c.policy_engine.prepare.return_value
        preview["policy"] = policy(names=())
        preview["risk"] = preview["policy"]["risk"]
        result = self.c.step(self.obs)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["evidence"]["active_post_names"], [])
        self.c.bootstrap.ensure_item.assert_not_called()

    def test_quiet_early_industrial_scope_defers_guns_but_nearby_site_enemies_restore_gate(self):
        self.c.policy_engine.survey = Mock(return_value=facts())
        self.c.game.query.return_value = {"ok": True, "world_id": "w", "tick": 1001, "nearby_enemies": 0}
        areas = [[[20, 0], [24, 4]]]
        self.assertIsNone(self.c.ensure_coverage(self.obs, "early-mine", areas))
        proof = self.c.coverage_proof(self.obs, "early-mine")
        self.assertFalse(proof["defense_required"])
        self.assertNotIn("geometry", proof)
        self.c.bootstrap.ensure_item.assert_not_called()
        self.c.game.query.return_value["nearby_enemies"] = 1
        self.assertIsNone(self.c.coverage_proof(self.obs, "early-mine"))

    def test_quiet_deferral_does_not_change_scope_identity_or_cover_unknown_surveys(self):
        self.c.policy_engine.survey = Mock(return_value=facts())
        self.c.game.query.return_value = {"ok": True, "world_id": "w", "tick": 1001, "nearby_enemies": 0}
        areas = [[[20, 0], [24, 4]]]
        self.assertIsNone(self.c.ensure_coverage(self.obs, "early-mine", areas))
        changed = self.c.ensure_coverage(self.obs, "early-mine", [[[30, 0], [34, 4]]])
        self.assertEqual(changed["reason"], "defense_coverage_scope_geometry_changed")
        self.c.game.query.return_value = {"ok": False}
        self.assertIsNone(self.c._quiet_scope_deferral(self.obs, "early-mine"))

    def test_high_risk_paid_initial_load_receipt_uses_refill_floor_after_loading(self):
        preview = self.c.policy_engine.prepare.return_value
        preview["policy"] = policy(level=2)
        preview["risk"] = preview["policy"]["risk"]
        self.report["posts"][0]["full_magazines"] = 20
        self.assertEqual(self.c.step(self.obs)["status"], "succeeded")
        self.assertEqual(self.c.state["posts"][0]["policy_loaded_target"], 20)
        self.report["posts"][0]["full_magazines"] = 15
        self.assertEqual(self.c.step(self.obs)["status"], "succeeded")
        self.c.bootstrap.ensure_item.assert_not_called()

    def test_unknown_policy_never_spends_or_claims_low_risk_success(self):
        self.c.policy_engine.prepare.return_value = {"ok": False, "reason": "adaptive_defense_policy_unavailable"}
        before = deepcopy(self.c.state)
        result = self.c.step(self.obs)
        self.assertEqual((result["status"], result["reason"]), ("blocked", "adaptive_defense_policy_unavailable"))
        self.assertEqual(self.c.state, before)
        self.assertIsNone(self.c.proof(self.obs))
        self.c._save.assert_not_called()

    def test_explicit_factory_scope_can_build_its_guard_outside_the_small_adaptive_target(self):
        self.assertEqual(self.c.step(self.obs)["status"], "succeeded")
        self.obs["inventory"]["gun-turret"] = 1
        result = self.c.step(self.obs, required_posts={"future_lab"})
        self.assertEqual((result["type"], result["position"]), ("build", {"x": 100, "y": -100}))
        self.assertEqual(self.c.state["adaptive_policy"]["active_posts"], ["paid"])

    def test_deferred_unbuilt_post_cannot_count_as_existing_factory_coverage(self):
        self.c.step(self.obs)
        areas = [[[98, -102], [102, -98]]]
        native = {
            "ok": True,
            "range": 18,
            "magazine_size": 10,
            "candidates": [{"position": {"x": 100, "y": -100}}, {"position": {"x": 104, "y": -100}}],
        }
        with patch("factorio_agent.defense_perimeter.native_candidate_survey", return_value=native):
            prepared = self.c.prepare_coverage(self.obs, "new_factory", areas)
        self.assertTrue(prepared["ok"])
        self.assertEqual(len(prepared["new_posts"]), 1)
        self.assertEqual(prepared["new_posts"][0]["position"], {"x": 104, "y": -100})
        self.assertNotIn("future_lab", prepared["scope"]["covering_posts"])

    def test_adaptive_success_cannot_hide_a_missing_or_unarmed_factory_scope_guard(self):
        self.c.step(self.obs)
        self.c.state["posts"].append(
            dict(name="factory", unit_number=200, position={"x": 60, "y": 0}, commissioned=True)
        )
        row = dict(name="factory", unit_number=200, position={"x": 60, "y": 0}, missing=True, full_magazines=10)
        self.report["posts"].append(row)
        scope = dict(
            covering_posts=["factory"], new_posts=[], cells=coverage_cells([[[58, -2], [62, 2]]]), effective_range=16
        )
        self.assertIsNone(self.c._coverage_evidence(self.report, scope))
        row.update(missing=False, full_magazines=4)
        self.assertIsNone(self.c._coverage_evidence(self.report, scope))
        row["full_magazines"] = 10
        self.assertIsNotNone(self.c._coverage_evidence(self.report, scope))


if __name__ == "__main__":
    unittest.main()
