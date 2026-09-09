from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_armaments import Armaments
from factorio_ai.deterministic_defense import DeterministicDefense
from factorio_ai.deterministic_routine_fairness import RoutineFairness
from factorio_ai.deterministic_supervisor import DeterministicSupervisor


def entity(name, number, x, y, **extra):
    return dict(name=name, unit_number=number, position={"x": x, "y": y}, health=200, **extra)


def fixture(root):
    game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(root)), backend="assisted",
                           query=Mock(return_value={"ok": True, "quiet": True, "routes_ready": True, "tick": 1000}))
    obs = {"ok": True, "world_id": "world", "tick": 100, "actor_unit_number": 58,
           "entities": [entity("lab", 1, 0, 0), entity("gun-turret", 2, 4, -4, inventory={"firearm-magazine": 10})],
           "technologies": {"automation": True, "electric-mining-drill": True}}
    identity = dict(world_id="world", catalog_fingerprint="catalog", last_tick=100)
    factory = Mock(catalog=SimpleNamespace(fingerprint="catalog"), state={**identity, "blocks": {}, "links": {}})
    armaments = Mock(state={**identity, "turrets": {}}, _key=Armaments._key)
    defense = DeterministicDefense(game, Mock())
    return game, obs, factory, armaments, defense


class FairnessSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game, self.obs, self.factory, self.armaments, self.defense = fixture(self.temp.name)
        self.driver = RoutineFairness(self.game)
        self.driver._sync(self.obs, "catalog")
        self.driver.state["completed"] = 3

    def prefer(self):
        return self.driver.prefer_production(self.factory, self.armaments, self.defense, self.obs)

    def test_new_seeded_turret_without_link_allows_slot_without_claiming_automatic_ownership(self):
        self.assertTrue(self.prefer())
        self.assertEqual(self.armaments.state["turrets"], {})
        self.armaments.next_action.assert_not_called()

    def test_foreign_controller_actor_or_missing_observation_never_surveys_or_selects(self):
        for change in ("world", "catalog", "future_tick", "actor", "observation"):
            with self.subTest(change=change):
                game, obs, factory, arms, defense = fixture(self.temp.name)
                if change == "world": arms.state["world_id"] = "other"
                if change == "catalog": factory.state["catalog_fingerprint"] = "other"
                if change == "future_tick": arms.state["last_tick"] = 101
                if change == "actor": obs.pop("actor_unit_number")
                if change == "observation": obs["ok"] = False
                self.assertFalse(self.driver.prefer_production(factory, arms, defense, obs))
                game.query.assert_not_called()

    def test_failed_unknown_or_unsafe_survey_retains_normal_order(self):
        self.driver.state["completed"] = 0
        self.assertFalse(self.prefer())
        build = self.driver.bind("routine", {"type": "build", "name": "transport-belt"})
        self.driver.record(build, {"ok": True, "status": "succeeded", "unit_number": 42})
        self.assertEqual(self.driver.state["completed"], 3)
        responses = [None, [], {}, {"ok": False}]
        responses += [{"ok": True, "quiet": False, "reason": reason} for reason in
                      ("damaged_actor", "damaged_asset", "nearby_enemy", "low_turret_ammunition")]
        responses += [{"ok": True, "quiet": True, "routes_ready": False, "reason": reason} for reason in
                      ("ammunition_route_missing", "ammunition_route_unpowered", "ammunition_route_contaminated")]
        for response in responses:
            with self.subTest(response=response):
                self.game.query.return_value = response
                self.assertFalse(self.prefer())
        self.game.query.side_effect = TimeoutError("survey unavailable")
        self.assertFalse(self.prefer())

    def test_legacy_zero_through_three_counter_reload_requires_fresh_safe_observation(self):
        for completed in range(4):
            with self.subTest(completed=completed):
                self.driver.state["completed"] = completed
                self.driver._save()
                self.driver = RoutineFairness(self.game)
                self.assertFalse(self.driver.safety["ok"])
                self.assertIsNone(self.driver.selection)
                self.assertEqual(self.prefer(), completed >= 3)
                self.assertEqual(self.driver.state["completed"], completed)

    def test_missing_malformed_or_rolled_back_live_tick_cannot_grant_science(self):
        for tick in (None, True, 99, 100.0):
            with self.subTest(tick=tick):
                self.game.query.return_value = {"ok": True, "quiet": True, "routes_ready": True, "tick": tick}
                self.assertFalse(self.prefer())
        self.game.query.return_value["tick"] = 100
        self.assertTrue(self.prefer())
        self.obs["tick"] = -1
        self.assertFalse(self.prefer())

    def test_recent_damage_stays_urgent_after_health_is_restored(self):
        self.defense._observe_damage(self.obs, self.defense._assets(self.obs))
        self.obs["tick"] += 1
        self.obs["entities"][0]["health"] -= 20
        self.defense._observe_damage(self.obs, self.defense._assets(self.obs))
        self.obs["entities"][0]["health"] += 20
        self.assertFalse(self.prefer())
        self.assertEqual(self.driver.safety["reason"], "recent_asset_damage")
        self.game.query.assert_not_called()

    def test_saved_intake_requires_matching_source_and_consumer_without_mutating_plans(self):
        source = {"kind": "item", "item": "firearm-magazine", "direction": "output", "position": {"x": 1, "y": 2}}
        consumer = {**source, "direction": "input", "position": {"x": 4, "y": -6}}
        turret = self.obs["entities"][1]
        key = Armaments._key(turret)
        arm = entity("inserter", 3, 4, -5, direction=0)
        self.obs["entities"].append(arm)
        plan = {"entities": [{k: v for k, v in arm.items() if k != "unit_number"}], "ports": [consumer]}
        producer = {"entities": [entity("assembling-machine-1", 4, 1, 0, recipe="firearm-magazine")], "ports": [source]}
        self.factory.state["blocks"]["recipe:firearm-magazine"] = producer
        self.factory.state["links"]["armaments:" + key] = {"source_port": source, "consumer_port": consumer, "entities": []}
        self.armaments.state["turrets"][key] = {"unit_number": 2, "owned": False, "plan": plan}
        self.assertFalse(self.prefer())
        self.factory.state["blocks"]["armaments:" + key] = deepcopy(plan)
        before = deepcopy((self.factory.state, self.armaments.state))
        self.assertTrue(self.prefer())
        self.assertEqual((self.factory.state, self.armaments.state), before)
        self.factory.state["blocks"]["armaments:" + key]["ports"] = []
        self.assertFalse(self.prefer())
        self.factory.state["blocks"]["armaments:" + key] = deepcopy(plan)
        self.factory.state["links"]["armaments:" + key]["consumer_port"] = {**consumer, "item": "iron-plate"}
        self.game.query.reset_mock()
        self.assertFalse(self.prefer())
        self.game.query.assert_not_called()

    def test_completed_routine_build_can_accrue_while_new_saved_route_is_unfinished(self):
        self.driver.state["completed"] = 2
        self.game.query.return_value = {"ok": True, "quiet": True, "routes_ready": False}
        self.assertFalse(self.prefer())
        action = self.driver.bind("routine", {"type": "build", "name": "transport-belt"})
        self.driver.record(action, {"ok": True, "status": "succeeded", "unit_number": 42})
        self.assertEqual(self.driver.state["completed"], 3)
        self.assertFalse(self.prefer())
        self.game.query.return_value["routes_ready"] = True
        self.game.query.return_value["tick"] = self.obs["tick"]
        self.assertTrue(self.prefer())


class FairnessSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        game, self.obs, factory, arms, defense = fixture(self.temp.name)
        self.supervisor = s = DeterministicSupervisor(game)
        s.catalog = factory.catalog
        s.bootstrap = Mock()
        s.bootstrap.next_action.return_value = {"status": "succeeded"}
        s.builder = Mock(state={"power_verified_once": True})
        s.builder.ensure_power.return_value = {"status": "succeeded"}
        s.builder.owns_automated_burner.return_value = False
        s.factory, s.armaments, s.defense = factory, arms, defense
        s.defense.next_action = Mock(return_value={"status": "succeeded"})
        s.defense.requirements = Mock(return_value={"research": []})
        s.energy = Mock(state={})
        s.energy.next_action.return_value = None
        s.fluids = Mock()
        s.fluids.maintain_coproducts.return_value = None
        self.routine = {"type": "take", "item": "iron-plate", "count": 2}
        self.production = {"type": "build", "name": "assembling-machine-1"}
        s.armaments.next_action.return_value = self.routine
        s.factory.next_action.return_value = self.production
        patcher = patch("factorio_ai.deterministic_ready_research.ready_research", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def choose(self):
        self.obs["tick"] += 1
        return self.supervisor.next_action(self.obs, "rocket")

    def due(self):
        self.choose()
        self.supervisor.fairness.state["completed"] = 3

    def test_three_actual_routine_receipts_then_one_factory_action_without_polling_discarded_seed(self):
        s = self.supervisor
        for _ in range(3):
            action = self.choose()
            self.assertEqual(action, self.routine)
            s.fairness.record(action, {"ok": True, "status": "succeeded", "moved": 2})
        s.factory.next_action.assert_not_called()
        s.armaments.next_action.reset_mock()
        self.assertEqual(self.choose(), self.production)
        s.armaments.next_action.assert_not_called()
        s.defense.next_action.assert_not_called()
        s.fairness.record(self.production, {"ok": True, "status": "succeeded", "unit_number": 8})
        self.assertEqual(self.choose(), self.routine)

    def test_factory_lane_survives_navigation_polls_and_queued_crafting(self):
        self.due()
        s = self.supervisor
        for result in ({"ok": True, "status": "running", "position": {}},
                       {"ok": True, "status": "succeeded", "position": {}},
                       {"ok": True, "status": "running", "started": 1}):
            self.assertEqual(self.choose(), self.production)
            s.fairness.record(self.production, result)
            self.assertEqual(s.fairness.state["completed"], 3)

    def test_factory_waiting_falls_through_once_but_failure_is_never_hidden(self):
        self.due()
        s = self.supervisor
        s.factory.next_action.return_value = {"status": "waiting"}
        s.factory.next_action.reset_mock()
        self.assertEqual(self.choose(), self.routine)
        s.factory.next_action.assert_called_once()
        failure = {"status": "blocked", "reason": "recipe unavailable"}
        s.factory.next_action.return_value = failure
        s.armaments.next_action.reset_mock()
        self.assertEqual(self.choose(), failure)
        s.armaments.next_action.assert_not_called()

    def test_power_coproduct_and_unknown_safety_keep_priority_without_spending_science_turn(self):
        self.due()
        s = self.supervisor
        repair = {"type": "build", "name": "transport-belt"}
        s.energy.next_action.return_value = repair
        self.assertEqual(self.choose(), repair)
        s.energy.next_action.return_value = None
        s.fluids.maintain_coproducts.return_value = repair
        self.assertEqual(self.choose(), repair)
        s.fairness.record(repair, {"ok": True, "status": "succeeded", "unit_number": 9})
        self.assertEqual(s.fairness.state["completed"], 3)
        s.fluids.maintain_coproducts.return_value = None
        s.game.query.return_value = {"ok": False, "reason": "unknown enemy survey"}
        self.assertEqual(self.choose(), self.routine)
        self.assertEqual(s.fairness.state["completed"], 3)


if __name__ == "__main__":
    unittest.main()
