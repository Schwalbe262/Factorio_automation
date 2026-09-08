from copy import deepcopy
import unittest

from factorio_ai.deterministic_armaments import Armaments
from tests import test_deterministic_armaments as arm_fixtures
from tests import test_deterministic_routine_fairness as fairness_fixtures


class IntakeLifecycleTests(unittest.TestCase):
    def setUp(self):
        arm_fixtures.ArmamentsTests.setUp(self)
        # Initialize before the normally built test turret first appears.
        self.armaments.state.pop("intake_baseline_units")
        self.armaments._sync({**self.obs, "entities": []})
        self.armaments._sync(self.obs)

    def resume(self):
        self.armaments._save()
        return Armaments(self.game, self.bootstrap, self.builder, self.factory, self.catalog)

    def test_new_marker_survives_reload_but_legacy_rows_and_existing_reservations_are_strict(self):
        row = self.armaments._track(self.turret)
        self.assertEqual(row["pending_intake"], {"world_id": "one", "catalog_fingerprint": "prototype-a",
                                               "unit_number": 5, "started_tick": 100})
        resumed = self.resume()
        self.assertEqual(resumed._track(self.turret)["pending_intake"], row["pending_intake"])
        row.pop("pending_intake")
        self.assertNotIn("pending_intake", self.resume()._track(self.turret))
        owner = "armaments:" + self.armaments._key(self.turret)
        for category, key in (("blocks", owner), ("links", owner), ("power_links", owner),
                              ("power_links", "tap:" + owner + ":pole:4,-6"),
                              ("power_links", owner + ":pole:4,-6"),
                              ("power_links", owner + ":pole:4,-6:pole:5,-6")):
            with self.subTest(category=category, key=key):
                self.armaments.state["turrets"].clear()
                self.factory.state[category][key] = {"entities": []}
                self.assertNotIn("pending_intake", self.armaments._track(self.turret))
                self.factory.state[category].clear()

    def test_complete_powered_intake_revokes_exception_without_requiring_refill(self):
        row = self.armaments._track(self.turret)
        row["plan"] = next(self.armaments._intake_candidates(self.turret))
        key = "armaments:" + self.armaments._key(self.turret)
        self.factory.ensure_power_connection.return_value = {"type": "build", "name": "small-electric-pole"}
        self.assertEqual(self.armaments._finish_intake(self.obs, key, row)["type"], "build")
        self.assertIn("pending_intake", row)
        self.factory.ensure_power_connection.return_value = arm_fixtures.ready()
        self.assertEqual(self.armaments._finish_intake(self.obs, key, row)["status"], "succeeded")
        self.assertNotIn("pending_intake", row)
        self.assertFalse(row["owned"])
        self.assertNotIn("sample", row)
        resumed = self.resume()
        self.assertNotIn("pending_intake", resumed._track(self.turret))
        resumed.builder.ensure_plan.return_value = {"type": "build", "name": "transport-belt"}
        resumed._finish_intake(self.obs, key, resumed._track(self.turret))
        self.assertNotIn("pending_intake", resumed._track(self.turret))

    def test_rollback_disappearance_and_replacement_cannot_regrant_exception(self):
        for change in ("rollback", "missing", "replacement"):
            with self.subTest(change=change):
                self.armaments.state["turrets"].clear()
                self.obs["tick"] = 100
                self.obs["entities"] = [self.turret]
                self.armaments._sync(self.obs)
                row = self.armaments._track(self.turret)
                if change == "rollback":
                    self.obs["tick"] = 99
                    self.armaments._sync(self.obs)
                elif change == "missing":
                    self.obs["entities"] = []
                    self.armaments._sync(self.obs)
                else:
                    replacement = {**self.turret, "unit_number": 99}
                    self.assertNotIn("pending_intake", self.armaments._track(replacement))
                    continue
                self.assertNotIn("pending_intake", row)
                self.assertNotIn("pending_intake", self.armaments._track(self.turret))

    def test_legacy_catalog_and_world_resets_baseline_existing_turrets_before_retracking(self):
        for change in ("legacy", "catalog", "world"):
            with self.subTest(change=change):
                self.armaments.state["turrets"].clear()
                row = self.armaments._track(self.turret)
                row.pop("pending_intake", None)
                owner = "armaments:" + self.armaments._key(self.turret)
                self.factory.state["blocks"][owner] = {"entities": []}
                if change == "legacy":
                    self.armaments.state.pop("intake_baseline_units", None)
                elif change == "catalog":
                    self.catalog.fingerprint = "prototype-new"
                else:
                    self.obs["world_id"] = "other-world"
                self.factory._sync(self.obs)
                self.factory.state["blocks"].clear()
                self.factory.state["links"].clear()
                self.armaments._sync(self.obs)
                self.assertIn(5, self.armaments.state["intake_baseline_units"])
                self.assertNotIn("pending_intake", self.armaments._track(self.turret))
                later = {**self.turret, "unit_number": 99, "position": {"x": 20, "y": 20}}
                self.obs["entities"] = [self.turret, later]
                self.obs["tick"] += 1
                self.armaments._sync(self.obs)
                self.assertIn("pending_intake", self.armaments._track(later))
                self.obs["entities"] = [self.turret]


class ConstructionFairnessTests(unittest.TestCase):
    def setUp(self):
        fairness_fixtures.FairnessSafetyTests.setUp(self)
        self.armaments.state["intake_baseline_units"] = []
        self.turret = self.obs["entities"][1]
        self.key = "armaments:" + Armaments._key(self.turret)
        source = {"kind": "item", "item": "firearm-magazine", "direction": "output", "position": {"x": 1, "y": 2}}
        consumer = {**source, "direction": "input", "position": {"x": 4, "y": -6}}
        self.arm = {"name": "inserter", "position": {"x": 4, "y": -5}, "direction": 0}
        self.belt = {"name": "transport-belt", "position": {"x": 4, "y": -6}, "direction": 0}
        self.plan = {"entities": [self.arm], "ports": [consumer], "existing_receiver": {
            "name": "gun-turret", "position": self.turret["position"], "unit_number": 2,
            "world_id": "world", "catalog_fingerprint": "catalog"}}
        self.factory.state["blocks"]["recipe:firearm-magazine"] = {"entities": [
            {"name": "assembling-machine-1", "position": {"x": 1, "y": 0}, "recipe": "firearm-magazine"}], "ports": [source]}
        self.factory.state["blocks"][self.key] = self.plan
        self.factory.state["links"][self.key] = {"entities": [self.belt], "source_port": source, "consumer_port": consumer}
        self.row = {"unit_number": 2, "owned": False, "plan": self.plan, "pending_intake": {
            "world_id": "world", "catalog_fingerprint": "catalog", "unit_number": 2, "started_tick": 100}}
        self.armaments.state["turrets"][Armaments._key(self.turret)] = self.row

    def payload(self):
        return self.driver._payload(self.obs, self.factory, self.armaments)

    def prefer(self):
        return self.driver.prefer_production(self.factory, self.armaments, self.defense, self.obs)

    def test_pending_route_and_pre_link_plan_preserve_observed_identity_without_mutation(self):
        self.obs["entities"].append({**self.arm, "unit_number": 3})
        before = deepcopy((self.factory.state, self.armaments.state))
        routes = self.payload()["routes"]
        self.assertEqual([(r["name"], r.get("unit_number"), r.get("pending", False)) for r in routes],
                         [("transport-belt", None, True), ("inserter", 3, True), ("assembling-machine-1", None, False)])
        self.assertEqual((self.factory.state, self.armaments.state), before)
        self.factory.state["links"].pop(self.key)
        self.assertTrue(self.prefer())
        self.row.pop("pending_intake")
        self.assertFalse(self.prefer())

    def test_malformed_foreign_future_and_legacy_markers_never_relax_saved_routes(self):
        marker = deepcopy(self.row["pending_intake"])
        for value in (None, True, {}, {**marker, "world_id": "other"}, {**marker, "catalog_fingerprint": "other"},
                      {**marker, "unit_number": 99}, {**marker, "started_tick": True},
                      {**marker, "started_tick": 101}, {**marker, "started_tick": -1}):
            with self.subTest(marker=value):
                self.row["pending_intake"] = value
                self.assertFalse(any(route.get("pending") for route in self.payload()["routes"]))

    def test_pending_receiver_and_ports_must_match_its_exact_reservation(self):
        for field, value in (("world_id", "other"), ("catalog_fingerprint", "other"), ("unit_number", 99)):
            with self.subTest(field=field):
                saved = deepcopy(self.plan["existing_receiver"])
                self.plan["existing_receiver"][field] = value
                with self.assertRaises(ValueError):
                    self.payload()
                self.plan["existing_receiver"] = saved
        self.factory.state["links"].pop(self.key)
        self.plan["ports"] = []
        with self.assertRaises(ValueError):
            self.payload()

    def test_missing_or_legacy_baseline_cannot_infer_new_intake_from_marker_alone(self):
        for baseline in (None, "unknown", [True], [-1], [2]):
            with self.subTest(baseline=baseline):
                self.armaments.state["intake_baseline_units"] = baseline
                self.assertFalse(any(route.get("pending") for route in self.payload()["routes"]))

    def test_shared_established_owner_is_strict_in_every_reservation_category(self):
        for category in ("blocks", "links", "power_links"):
            with self.subTest(category=category):
                self.factory.state.setdefault(category, {})["established-owner"] = {"entities": [deepcopy(self.belt)]}
                routes = self.payload()["routes"]
                self.assertFalse(next(row for row in routes if row["name"] == "transport-belt").get("pending", False))
                self.assertTrue(next(row for row in routes if row["name"] == "inserter").get("pending"))
                self.factory.state[category].pop("established-owner")
        self.factory.state.setdefault("power_links", {})["tap:" + self.key + ":pole:4,-6"] = {"entities": [deepcopy(self.belt)]}
        self.assertTrue(all(row.get("pending") for row in self.payload()["routes"] if row["name"] == "transport-belt"))

    def test_existing_quota_and_real_action_receipts_still_control_interleaving(self):
        self.driver.state["completed"] = 2
        self.assertFalse(self.prefer())
        build = self.driver.bind("routine", {"type": "build", "name": "transport-belt"})
        self.driver.record(build, {"ok": True, "status": "succeeded", "unit_number": 42, "reused": True})
        self.assertFalse(self.prefer())
        build = self.driver.bind("routine", build)
        self.driver.record(build, {"ok": True, "status": "succeeded", "unit_number": 42})
        self.assertTrue(self.prefer())
        build = self.driver.bind("production", {"type": "build", "name": "assembling-machine-1"})
        self.driver.record(build, {"ok": True, "status": "succeeded", "unit_number": 43})
        self.assertEqual(self.driver.state["completed"], 0)
        self.assertFalse(self.prefer())

    def test_ordinary_and_tap_nested_power_paths_are_observed_with_their_owner(self):
        pole = {"name": "small-electric-pole", "position": {"x": 8, "y": -8}}
        marker = deepcopy(self.row["pending_intake"])
        for prefix in (self.key, "tap:" + self.key):
            with self.subTest(prefix=prefix):
                power_key = prefix + ":pole:4,-6:pole:5,-6"
                self.factory.state["power_links"] = {power_key: {"entities": [pole]}}
                self.row["pending_intake"] = marker
                found = [r for r in self.payload()["routes"] if r["position"] == pole["position"]]
                self.assertEqual(len(found), 1)
                self.assertTrue(found[0].get("pending"))
                self.row.pop("pending_intake")
                found = [r for r in self.payload()["routes"] if r["position"] == pole["position"]]
                self.assertEqual(len(found), 1)
                self.assertFalse(found[0].get("pending", False))

    def test_shared_ammunition_piece_requires_all_owners_to_be_pending(self):
        other = {**self.turret, "unit_number": 9, "position": {"x": 10, "y": -4}}
        self.obs["entities"].append(other)
        other_key = "armaments:" + Armaments._key(other)
        plan = deepcopy(self.plan)
        plan["existing_receiver"].update(unit_number=9, position=other["position"])
        plan["entities"] = [deepcopy(self.belt)]
        row = {"unit_number": 9, "plan": plan, "owned": False}
        self.armaments.state["turrets"][Armaments._key(other)] = row
        self.factory.state["blocks"][other_key] = plan
        self.factory.state["links"][other_key] = deepcopy(self.factory.state["links"][self.key])
        shared = [r for r in self.payload()["routes"] if r["name"] == "transport-belt"]
        self.assertTrue(shared)
        self.assertFalse(any(r.get("pending") for r in shared))
        row["pending_intake"] = {**self.row["pending_intake"], "unit_number": 9}
        self.assertTrue(all(r.get("pending") for r in self.payload()["routes"] if r["name"] == "transport-belt"))


if __name__ == "__main__":
    unittest.main()
