from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from factorio_ai.deterministic_armaments import Armaments
from factorio_ai.deterministic_factory import DeterministicFactory
from factorio_ai.factory_templates import DIRECTIONS
from tests import test_deterministic_armaments as armaments_tests


BLOCKED = {"status": "blocked", "reason": "no route within bounds"}


class ArmamentsRetryTests(unittest.TestCase):
    def setUp(self):
        armaments_tests.ArmamentsTests.setUp(self)
        self.key = "armaments:" + self.armaments._key(self.turret)
        self.row = self.armaments._track(self.turret)
        self.row.update(seed_remaining=3, last_seed_tick=70)
        self.armaments._save()
        plans = list(self.armaments._intake_candidates(self.turret))
        self.candidates = plans
        self.first = plans[0]
        self.second = next(p for p in plans if self.port_identity(p) != self.port_identity(self.first))
        self.duplicate = next(p for p in plans if p != self.first and self.port_identity(p) == self.port_identity(self.first))
        self.source = {"kind": "item", "item": "firearm-magazine", "direction": "output",
                       "position": {"x": 20.5, "y": .5}, "facing": 4}
        self.factory.connect_input = Mock(return_value=deepcopy(BLOCKED))

    @staticmethod
    def port_identity(plan):
        port = plan["ports"][0]
        return port["position"]["x"], port["position"]["y"], port["facing"]

    @staticmethod
    def approach(plan):
        port = plan["ports"][0]
        dx, dy = DIRECTIONS[port["facing"]]
        return {"x": port["position"]["x"] - dx, "y": port["position"]["y"] - dy}

    def store_plan(self, plan):
        self.row["plan"] = self.factory.register_plan(self.key, plan, self.obs)
        self.armaments._save()

    def call(self):
        return self.armaments._ensure_intake(self.obs, self.turret, self.row, self.source)

    def reload(self):
        self.factory = DeterministicFactory(self.game, self.bootstrap, self.builder, self.catalog)
        self.factory.connect_input = Mock(return_value=deepcopy(BLOCKED))
        self.factory.ensure_power_connection = Mock(return_value=armaments_tests.ready())
        self.armaments = Armaments(self.game, self.bootstrap, self.builder, self.factory, self.catalog)
        self.row = self.armaments.state["turrets"][self.armaments._key(self.turret)]

    def assert_seed_identity_preserved(self):
        self.assertEqual((self.row["seed_remaining"], self.row["last_seed_tick"]), (3, 70))
        for state in (self.armaments.state, self.factory.state):
            self.assertEqual(state["world_id"], "one")
            self.assertEqual(state["catalog_fingerprint"], "prototype-a")

    def emit_paid_link(self, obs, source, consumer, key):
        self.factory.state["links"][key] = {"ok": True, "entities": [], "ports": [],
            "source_port": deepcopy(source), "consumer_port": deepcopy(consumer),
            "upstream_tap": {"link_key": "existing-ammo-bus", "belt": {"name": "transport-belt",
                "position": {"x": 20.5, "y": .5}, "direction": 4}}}
        self.factory._save()
        return {"type": "build", "name": "transport-belt", "position": deepcopy(consumer["position"])}

    def test_two_failed_candidates_leave_no_plan_or_factory_reservation_after_reload(self):
        self.armaments._intake_candidates = Mock(return_value=iter([self.first, self.second]))
        self.assertEqual(self.call()["status"], "blocked")
        self.assertEqual(self.factory.connect_input.call_count, 2)
        self.assertNotIn("plan", self.row)
        self.assertNotIn(self.key, self.factory.state["blocks"])
        saved_armaments = json.loads(self.armaments.path.read_text(encoding="utf-8"))
        saved_factory = json.loads(self.factory.path.read_text(encoding="utf-8"))
        self.assertNotIn("plan", saved_armaments["turrets"][self.armaments._key(self.turret)])
        self.assertNotIn(self.key, saved_factory["blocks"])
        self.reload()
        self.assertNotIn("plan", self.row)
        self.assertNotIn(self.key, self.factory.state["blocks"])
        self.assert_seed_identity_preserved()

    def test_resumed_failure_skips_exact_plan_and_keeps_alternative_paid_link_provenance(self):
        self.store_plan(self.first)
        self.reload()
        self.armaments._intake_candidates = Mock(return_value=iter([self.first, self.second]))
        attempted = []
        def connect(obs, source, consumer, key):
            attempted.append((consumer["position"]["x"], consumer["position"]["y"], consumer["facing"]))
            return deepcopy(BLOCKED) if len(attempted) == 1 else self.emit_paid_link(obs, source, consumer, key)
        self.factory.connect_input.side_effect = connect
        result = self.call()
        self.assertEqual(result["type"], "build")
        self.assertEqual(attempted, [self.port_identity(self.first), self.port_identity(self.second)])
        self.assertEqual(self.port_identity(self.row["plan"]), self.port_identity(self.second))
        self.assertEqual(self.factory.state["blocks"][self.key], self.row["plan"])
        self.assertEqual(self.factory.state["links"][self.key]["upstream_tap"]["link_key"], "existing-ammo-bus")
        self.reload()
        self.assertEqual(self.port_identity(self.row["plan"]), self.port_identity(self.second))
        self.assertIn(self.key, self.factory.state["links"])
        self.assert_seed_identity_preserved()

    def test_a_different_pole_at_the_same_clear_port_can_unblock_the_route(self):
        self.store_plan(self.first)
        self.armaments._intake_candidates = Mock(return_value=iter([self.first, self.duplicate]))
        poles = []
        def connect(obs, source, consumer, key):
            plan = self.factory.state["blocks"][key]
            poles.append(next(e["position"] for e in plan["entities"] if e["name"] == "small-electric-pole"))
            return deepcopy(BLOCKED) if len(poles) == 1 else self.emit_paid_link(obs, source, consumer, key)
        self.factory.connect_input.side_effect = connect
        self.assertEqual(self.call()["type"], "build")
        self.assertEqual(poles, [next(e["position"] for e in plan["entities"] if e["name"] == "small-electric-pole")
                                 for plan in (self.first, self.duplicate)])
        self.assertEqual(self.port_identity(self.row["plan"]), self.port_identity(self.first))
        self.assert_seed_identity_preserved()

    def test_saved_known_blocked_approach_skips_routing_and_uses_normal_alternative(self):
        self.store_plan(self.first)
        blocked_front = self.approach(self.first)
        self.builder.can_place.side_effect = lambda entities: {
            "ok": not any(e["position"] == blocked_front for e in entities)}
        self.armaments._intake_candidates = Mock(return_value=iter([self.second]))
        self.factory.connect_input.side_effect = self.emit_paid_link
        self.assertEqual(self.call()["type"], "build")
        self.factory.connect_input.assert_called_once()
        self.assertEqual(self.factory.connect_input.call_args.args[2], self.second["ports"][0])
        self.assert_seed_identity_preserved()

    def test_candidate_survey_includes_tree_blocked_front_without_reserving_it(self):
        blocked_front = self.approach(self.first)
        self.builder.can_place.reset_mock()
        self.builder.can_place.side_effect = lambda entities: {
            "ok": not any(e["name"] == "transport-belt" and e["position"] == blocked_front for e in entities)}
        candidates = list(self.armaments._intake_candidates(self.turret))
        self.assertTrue(candidates)
        self.assertTrue(any(any(e["position"] == blocked_front for e in call.args[0])
                            for call in self.builder.can_place.call_args_list))
        self.assertNotIn(self.port_identity(self.first), [self.port_identity(p) for p in candidates])
        for plan in candidates:
            self.assertNotIn(self.approach(plan), [e["position"] for e in plan["entities"]])

    def test_existing_link_survives_blocked_retry(self):
        self.store_plan(self.first)
        self.factory.state["links"][self.key] = {"ok": True, "entities": [], "ports": [], "provenance": "keep"}
        self.factory._save()
        before = deepcopy((self.row, self.factory.state))
        self.armaments._intake_candidates = Mock(return_value=iter([self.second]))
        self.assertEqual(self.call()["status"], "blocked")
        self.assertEqual((self.row, self.factory.state), before)
        self.armaments._intake_candidates.assert_not_called()
        self.reload()
        self.assertIn("plan", self.row)
        self.assertEqual(self.factory.state["links"][self.key]["provenance"], "keep")

    def test_partial_intake_hardware_prevents_reservation_removal(self):
        self.store_plan(self.first)
        arm = next(e for e in self.first["entities"] if e["name"] == "inserter")
        self.obs["entities"].append({**deepcopy(arm), "unit_number": 77})
        before = deepcopy((self.row, self.factory.state["blocks"][self.key]))
        self.armaments._intake_candidates = Mock(return_value=iter([self.second]))
        self.assertEqual(self.call()["status"], "blocked")
        self.assertEqual((self.row, self.factory.state["blocks"][self.key]), before)
        self.armaments._intake_candidates.assert_not_called()
        self.assert_seed_identity_preserved()

    def test_same_key_foreign_reservation_is_not_overwritten_or_deleted(self):
        self.store_plan(self.first)
        foreign = {**deepcopy(self.second), "key": self.key, "owner": "different-reservation"}
        self.factory.state["blocks"][self.key] = foreign
        self.factory._save()
        before = deepcopy((self.row, self.factory.state))
        self.armaments._intake_candidates = Mock(return_value=iter([self.second]))
        self.assertEqual(self.call()["status"], "blocked")
        self.assertEqual((self.row, self.factory.state), before)
        self.factory.connect_input.assert_not_called()
        self.reload()
        self.assertEqual(self.factory.state["blocks"][self.key], foreign)
        self.assert_seed_identity_preserved()

    def test_cleanup_rejects_changed_world_catalog_or_turret_identity_without_writes(self):
        self.store_plan(self.first)
        saved = (self.armaments.path.read_bytes(), self.factory.path.read_bytes())
        for target, field, value in ((self.armaments.state, "world_id", "other"),
                (self.factory.state, "catalog_fingerprint", "other"), (self.row, "unit_number", 999),
                (self.obs, "entities", [])):
            previous = target[field]
            target[field] = value
            before = deepcopy((self.armaments.state, self.factory.state))
            self.assertFalse(self.armaments._discard_unbuilt_intake(self.obs, self.turret, self.key, self.row))
            self.assertEqual((self.armaments.state, self.factory.state), before)
            self.assertEqual((self.armaments.path.read_bytes(), self.factory.path.read_bytes()), saved)
            target[field] = previous

    def test_retry_reaches_north_candidates_after_more_than_sixty_four_failures(self):
        north = next(p for p in self.candidates if p["ports"][0]["facing"] == 8)
        index = self.candidates.index(north)
        self.assertGreater(index, 64)
        self.armaments._intake_candidates = Mock(return_value=iter(self.candidates[:index + 1]))
        def connect(obs, source, consumer, key):
            return self.emit_paid_link(obs, source, consumer, key) if consumer == north["ports"][0] else deepcopy(BLOCKED)
        self.factory.connect_input.side_effect = connect
        self.assertEqual(self.call()["type"], "build")
        self.assertEqual(self.factory.connect_input.call_count, index + 1)
        self.assertEqual(self.port_identity(self.row["plan"]), self.port_identity(north))
        self.assert_seed_identity_preserved()

    def test_real_generator_adopts_own_orphan_before_connection_after_reload(self):
        orphan = self.factory.register_plan(self.key, self.first, self.obs)
        self.assertNotIn("plan", self.row)
        self.reload()
        # Keep the actual generator: its own saved input clearance must not
        # prevent regenerating the exact reservation left by the crash.
        def connect(obs, source, consumer, key):
            saved = json.loads(self.armaments.path.read_text(encoding="utf-8"))
            self.assertEqual(saved["turrets"][self.armaments._key(self.turret)]["plan"], orphan)
            return self.emit_paid_link(obs, source, consumer, key)
        self.factory.connect_input.side_effect = connect
        self.assertEqual(self.call()["type"], "build")
        self.factory.connect_input.assert_called_once()
        self.assertEqual(self.row["plan"], orphan)
        self.reload()
        self.assertEqual(self.row["plan"], orphan)
        self.assertIn(self.key, self.factory.state["links"])
        self.assert_seed_identity_preserved()

    def test_unprovable_orphan_preserves_foreign_reservation_partial_hardware_and_links(self):
        orphan = self.factory.register_plan(self.key, self.first, self.obs)
        original_obs = deepcopy(self.obs)
        for obstruction in ("unmatched-candidate", "partial-hardware", "existing-link", "wrong-key", "world-changed"):
            with self.subTest(obstruction=obstruction):
                self.obs = deepcopy(original_obs)
                self.factory.state["blocks"][self.key] = deepcopy(orphan)
                self.factory.state["links"].pop(self.key, None)
                candidates = [self.first]
                if obstruction == "unmatched-candidate":
                    candidates = [self.second]
                elif obstruction == "partial-hardware":
                    arm = next(e for e in self.first["entities"] if e["name"] == "inserter")
                    self.obs["entities"].append({**deepcopy(arm), "unit_number": 77})
                elif obstruction == "existing-link":
                    self.factory.state["links"][self.key] = {"ok": True, "entities": [], "provenance": "keep"}
                elif obstruction == "wrong-key":
                    self.factory.state["blocks"][self.key]["key"] = "another-owner"
                else:
                    self.obs["world_id"] = "other-world"
                self.factory._save()
                self.armaments._save()
                before = deepcopy((self.armaments.state, self.factory.state))
                saved = (self.armaments.path.read_bytes(), self.factory.path.read_bytes())
                self.armaments._intake_candidates = Mock(return_value=iter(candidates))
                self.factory.connect_input.reset_mock()
                self.assertEqual(self.call()["status"], "blocked")
                self.assertNotIn("plan", self.row)
                self.assertEqual((self.armaments.state, self.factory.state), before)
                self.assertEqual((self.armaments.path.read_bytes(), self.factory.path.read_bytes()), saved)
                self.factory.connect_input.assert_not_called()


if __name__ == "__main__":
    unittest.main()
