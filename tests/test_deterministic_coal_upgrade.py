from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_coal_upgrade import _owners, resume_coal_upgrade, start_coal_upgrade
from factorio_ai.deterministic_energy import EnergyExpansion


def entity(name, x, y, unit, direction=12):
    return {"name": name, "position": {"x": x, "y": y}, "direction": direction, "unit_number": unit}


class CoalUpgradeTests(unittest.TestCase):
    def fixture(self):
        arm = entity("burner-inserter", 34.5, 16.5, 3349)
        pickup = entity("transport-belt", 33.5, 16.5, 1986, 8)
        drop = entity("transport-belt", 35.5, 16.5, 3350, 4)
        pole = entity("small-electric-pole", 33.5, 14.5, 3307, 0)
        spec = {k: deepcopy(v) for k, v in arm.items() if k != "unit_number"}
        feeds = []
        blocks = {"unrelated": {"ok": True, "entities": [entity("stone-furnace", 99, 99, 991)], "ports": []},
                  "energy:power:1": {"ok": True, "entities": [pole], "ports": []}}
        for i in range(14):
            drill = entity("burner-mining-drill", -10-i*3, 0, 900+i)
            plan = {"ok": True, "key": f"energy:feed:{i}", "drill": drill,
                    "entities": [drill] + ([deepcopy(spec)] if i in (5, 6, 7, 13) else []), "ports": [],
                    "required_items": {"burner-mining-drill": 1, **({"burner-inserter": 1} if i in (5, 6, 7, 13) else {})}}
            feeds.append({"plan": plan, "bank": 0, "complete": True, "seeded": True})
            blocks[plan["key"]] = deepcopy(plan)
        link = {"ok": True, "entities": [deepcopy(pickup), deepcopy(spec), deepcopy(drop)], "ports": [],
                "required_items": {"burner-inserter": 1, "transport-belt": 2}}
        blocks["energy:coal-bank:1"] = {**deepcopy(link), "key": "energy:coal-bank:1"}
        state = {"world_id": "world", "catalog_fingerprint": "catalog", "feeds": feeds,
                 "banks": [{"entities": [], "ports": []} for _ in range(4)],
                 "coal_links": {"1": link, "2": {"entities": [], "ports": []}, "3": {"entities": [], "ports": []}}}
        state["banks"][0]["entities"] = [deepcopy(pole)]
        state["banks"][0]["entities"][0]["direction"] = 8  # Existing pole plans may store a nonstructural rotation.
        e = EnergyExpansion.__new__(EnergyExpansion)
        e.state = state
        e._fingerprint = "catalog"
        e.catalog = SimpleNamespace(items={"coal": {"fuel_value": 4e6}}, entities={
            "steam-engine": {"energy_production": 15000}, "burner-mining-drill": {"energy_usage": 2500},
            "burner-inserter": {"energy_usage": 2400}, "fast-inserter": {"energy_usage": 980}})
        e.factory = SimpleNamespace(state={"world_id": "world", "catalog_fingerprint": "catalog", "blocks": blocks,
                                          "links": {"untouched": {"entities": [], "ports": []}}, "power_links": {}},
                                    _save=Mock(), request_recipe_unlock=Mock(return_value={"type": "research", "technology": "fast"}),
                                    ensure_power_connection=Mock(return_value={"status": "succeeded"}))
        e.builder = SimpleNamespace(state={}, ensure_plan=Mock(return_value={"type": "build", "name": "small-electric-pole"}))
        e.bootstrap = SimpleNamespace(ensure_item=Mock(return_value={"type": "craft", "item": "fast-inserter", "count": 1}))
        e._save = Mock()
        obs = {"world_id": "world", "actor_unit_number": 58, "tick": 100, "inventory": {"fast-inserter": 1},
               "entities": [arm, pickup, drop, pole]}
        edge = {**deepcopy(arm), "coal_per_minute": 40.766867587011475,
                "pickup_position": deepcopy(pickup["position"]), "drop_position": {"x": 35.69921875, "y": 16.5}}
        def terminal(unit):
            return {"name": "burner-inserter", "unit_number": unit, "coal_per_minute": 1000}
        belt = {**pickup, "coal_per_minute": 450}
        paths = {"0": [belt], "1": [deepcopy(edge), terminal(3303)], "2": [belt],
                 "3": [deepcopy(edge), terminal(6808), terminal(6817)]}
        evidence = {"feeds": [{"remaining": 1000, "fuel": 1e7, "gross_coal_per_minute": 15} for _ in feeds],
                    "coal_routes": {str(i): deepcopy(paths) for i in range(14)}, "target_kw": 5751.72, "network_id": 1, "tick": 101}
        proof = {"ok": True, "world_id": "world", "actor_unit": 58, "tick": 101, "phase": "old", "unit_number": 3349,
                 "pole": pole, "powered": False, "stock": 1, "enabled": True, "can_place": False,
                 "coal_per_minute": 124.20485175202157}
        return e, obs, evidence, proof

    def begin(self):
        e, obs, evidence, proof = self.fixture()
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value=proof):
            result = start_coal_upgrade(e, obs, evidence, e.capacity(evidence))
        self.assertEqual(result["status"], "waiting")
        obs["tick"] = proof["tick"]
        return e, obs, evidence, proof

    def test_binding_selects_shared_arm_and_preserves_all_fourteen_feeds(self):
        e, obs, evidence, proof = self.fixture()
        before = deepcopy(e.state)
        factory_before = deepcopy(e.factory.state)
        before_capacity = e.capacity(evidence)
        self.assertAlmostEqual(before_capacity["total_kw"], 5741.791172467432)
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value=proof):
            result = start_coal_upgrade(e, obs, evidence, before_capacity)
        self.assertEqual(result["evidence"]["unit_number"], 3349)
        record = e.state.pop("coal_transit_upgrade")
        self.assertEqual(len(record["owners"]), 5)
        self.assertEqual(e.state, before)
        self.assertEqual(e.factory.state, factory_before)
        self.assertEqual(e.capacity(evidence), before_capacity)
        self.assertEqual(record["owners"][-1]["factory_old"].get("key"), "energy:coal-bank:1")
        self.assertNotIn("key", record["owners"][-1]["old"])

    def test_preparation_mine_build_publication_and_cold_reload(self):
        e, obs, _, proof = self.begin()
        original_feeds = deepcopy(e.state["feeds"])
        original_other = deepcopy(e.factory.state["blocks"]["unrelated"])
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value={**proof, "stock": 0}):
            self.assertEqual(resume_coal_upgrade(e, obs)["type"], "craft")
        self.assertEqual(e.state["coal_transit_upgrade"]["phase"], "preparing")
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value=proof):
            mine = resume_coal_upgrade(e, obs)
        self.assertEqual(mine["type"], "mine")
        self.assertEqual(mine["expected_entity_unit"], 3349)
        self.assertEqual(mine["coal_transit_replacement"]["drop"]["unit_number"], 3350)
        self.assertEqual(e.state["feeds"], original_feeds)
        e.state = deepcopy(e.state)  # Reload persisted intent between native operations.
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value={**proof, "phase": "empty", "can_place": True}):
            build = resume_coal_upgrade(e, obs)
        self.assertEqual((build["type"], build["name"]), ("build", "fast-inserter"))
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value={**proof, "phase": "new", "unit_number": 8001, "powered": True}):
            self.assertEqual(resume_coal_upgrade(e, obs)["status"], "waiting")
        self.assertEqual(e.state["coal_transit_upgrade"]["phase"], "published")
        for owner in e.state["coal_transit_upgrade"]["owners"]:
            self.assertEqual(e.factory.state["blocks"][owner["key"]], owner["factory_new"])
        self.assertEqual(e.factory.state["blocks"]["unrelated"], original_other)
        self.assertEqual(len(e.state["feeds"]), 14)
        self.assertTrue(e.state["feeds"][13]["complete"])
        obs["entities"][0].update(name="fast-inserter", unit_number=8001, energy=100)
        with patch("factorio_ai.deterministic_coal_upgrade._survey") as survey:
            self.assertIsNone(resume_coal_upgrade(e, {**obs, "tick": 102}))
        survey.assert_not_called()

    def test_power_loss_prevents_mine_and_requests_normal_connection_repair(self):
        e, obs, _, proof = self.begin()
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value={**proof, "pole": None}):
            result = resume_coal_upgrade(e, obs)
        self.assertEqual(result["status"], "waiting")
        e.factory.ensure_power_connection.assert_called_once()
        self.assertEqual(e.state["coal_transit_upgrade"]["phase"], "preparing")

    def test_guarded_mine_preempts_sync_and_existing_plan_rebuilds(self):
        e, obs, _, proof = self.begin()
        e._sync = Mock(side_effect=AssertionError("must not synchronize/rebuild old plans"))
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value=proof):
            result = e.next_action(obs)
        self.assertEqual(result["type"], "mine")
        e._sync.assert_not_called()
        e.builder.ensure_plan.assert_not_called()

    def test_publication_crash_after_factory_save_recovers_mixed_copies(self):
        e, obs, _, proof = self.begin()
        record = e.state["coal_transit_upgrade"]
        record["phase"] = "building"
        calls = 0
        saved = deepcopy(e.state)
        def save():
            nonlocal calls, saved
            calls += 1
            if calls == 2:
                raise RuntimeError("process interrupted before energy publication")
            saved = deepcopy(e.state)
        e._save = save
        live = {**proof, "phase": "new", "unit_number": 8001, "powered": True}
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value=live):
            with self.assertRaises(RuntimeError):
                resume_coal_upgrade(e, obs)
        e.state = saved
        e._save = Mock()
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value=live):
            self.assertEqual(resume_coal_upgrade(e, obs)["status"], "waiting")
        self.assertEqual(e.state["coal_transit_upgrade"]["phase"], "published")

    def test_published_world_rollback_restores_only_original_arm_references(self):
        e, obs, _, proof = self.begin()
        record = e.state["coal_transit_upgrade"]
        record["phase"] = "building"
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value={**proof, "phase": "new", "unit_number": 8001, "powered": True, "tick": 200}):
            resume_coal_upgrade(e, obs)
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value=proof):
            result = resume_coal_upgrade(e, obs)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(record["phase"], "preparing")
        for owner in record["owners"]:
            self.assertEqual(e.factory.state["blocks"][owner["key"]], owner["factory_old"])
        self.assertTrue(e.state["feeds"][13]["complete"])

    def test_changed_epoch_actor_identity_recipe_and_unknown_proof_do_not_mine(self):
        for change in ({"world_id": "other"}, {"actor_unit_number": 99}, {"tick": -1}):
            with self.subTest(change=change):
                e, obs, _, _ = self.begin()
                before = deepcopy(e.state)
                self.assertEqual(resume_coal_upgrade(e, {**obs, **change})["status"], "blocked")
                self.assertEqual(e.state, before)
        for delta in ({"ok": False}, {"tick": 99}, {"unit_number": 3351}):
            with self.subTest(delta=delta):
                e, obs, _, proof = self.begin()
                with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value={**proof, **delta}):
                    self.assertEqual(resume_coal_upgrade(e, obs)["status"], "blocked")
        e, obs, _, proof = self.begin()
        e._fingerprint = "changed"
        self.assertEqual(resume_coal_upgrade(e, obs)["status"], "blocked")
        e._fingerprint = "catalog"
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value={**proof, "enabled": False}):
            self.assertEqual(resume_coal_upgrade(e, obs)["type"], "research")

    def test_additional_or_divergent_owner_never_gets_silently_rewritten(self):
        for category in ("links", "power_links", "blocks"):
            e, obs, evidence, proof = self.fixture()
            e.factory.state[category]["foreign"] = {"entities": [deepcopy(obs["entities"][0])], "ports": []}
            before = deepcopy(e.factory.state)
            with self.subTest(category=category), patch("factorio_ai.deterministic_coal_upgrade._survey", return_value=proof):
                self.assertEqual(start_coal_upgrade(e, obs, evidence, e.capacity(evidence))["status"], "blocked")
            self.assertEqual(e.factory.state, before)
        e, obs, _, proof = self.begin()
        e.factory.state["blocks"]["energy:feed:13"]["ports"].append({"changed": True})
        before = deepcopy(e.factory.state)
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value=proof):
            self.assertEqual(resume_coal_upgrade(e, obs)["status"], "blocked")
        self.assertEqual(e.factory.state, before)

    def test_normal_energy_choice_reserves_upgrade_without_a_fifteenth_feed(self):
        e, obs, evidence, proof = self.fixture()
        e.state["feeds"][0]["primary"] = True
        e._sync = Mock(return_value=True)
        e._ensure_bank = Mock(return_value=None)
        e._ensure_feed = Mock(return_value=None)
        e._managed = Mock()
        e.factory.register_plan = Mock(return_value={"ok": True})
        e.evidence = Mock(return_value={"ok": True, **evidence})
        e._reserve_feed = Mock(side_effect=AssertionError("no marginal coal supply gain"))
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value=proof):
            result = e.next_action(obs)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(e.state["coal_transit_upgrade"]["old_unit"], 3349)
        e._reserve_feed.assert_not_called()
        self.assertEqual(len(e.state["feeds"]), 14)

    def test_published_missing_fast_repair_preserves_later_legitimate_shared_plans(self):
        e, obs, _, proof = self.begin()
        record = e.state["coal_transit_upgrade"]
        original_receipt = deepcopy(record["owners"])
        record["phase"] = "building"
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value={**proof, "phase": "new", "unit_number": 8001, "powered": True}):
            resume_coal_upgrade(e, obs)
        # A later normal feed can inherit the now-established fast arm.
        later = deepcopy(e.state["feeds"][13])
        later["plan"]["key"] = "energy:feed:14"
        later["plan"]["ports"] = [{"later": "owned source"}]
        e.state["feeds"].append(later)
        e.factory.state["blocks"]["energy:feed:14"] = deepcopy(later["plan"])
        obs["entities"] = [row for row in obs["entities"] if row["unit_number"] != 3349]
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value={**proof, "phase": "empty", "can_place": True}):
            self.assertEqual(resume_coal_upgrade(e, obs)["type"], "build")
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value={**proof, "phase": "new", "unit_number": 8002, "powered": True}):
            self.assertEqual(resume_coal_upgrade(e, obs)["status"], "waiting")
        self.assertEqual(record["new_unit"], 8002)
        self.assertEqual(record["owners"], original_receipt)
        self.assertEqual(e.state["feeds"][14], later)
        self.assertEqual(e.factory.state["blocks"]["energy:feed:14"], later["plan"])

    def test_missing_covering_pole_rebuild_is_paid_and_reobserved_before_mine(self):
        e, obs, _, proof = self.begin()
        obs["entities"] = [row for row in obs["entities"] if row["unit_number"] != 3307]
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value={**proof, "pole": None}):
            self.assertEqual(resume_coal_upgrade(e, obs)["name"], "small-electric-pole")
        self.assertTrue(e.state["coal_transit_upgrade"]["pole_build_pending"])
        new_pole = {**proof["pole"], "unit_number": 3308}
        obs["entities"].append(new_pole)
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value={**proof, "pole": None}):
            self.assertEqual(resume_coal_upgrade(e, obs)["status"], "waiting")
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value={**proof, "pole": new_pole}):
            mine = resume_coal_upgrade(e, obs)
        self.assertEqual(mine["coal_transit_replacement"]["pole"]["unit_number"], 3308)
        self.assertEqual(mine["coal_transit_replacement"]["expected_network_id"], 1)

    def test_completed_network_reconnection_rebinds_before_missing_arm_repair(self):
        e, obs, _, proof = self.begin()
        record = e.state["coal_transit_upgrade"]
        record["phase"] = "building"
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value={**proof, "phase": "new", "unit_number": 8001, "powered": True}):
            resume_coal_upgrade(e, obs)
        obs["entities"] = [row for row in obs["entities"] if row["unit_number"] != 3349]
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value={**proof, "phase": "empty", "pole": None,
                "can_place": True, "rebind_network_id": 2}):
            result = resume_coal_upgrade(e, obs)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(record["expected_network_id"], 2)
        self.assertEqual(record["phase"], "published")
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value={**proof, "phase": "empty", "can_place": True}):
            self.assertEqual(resume_coal_upgrade(e, obs)["type"], "build")
        # An uncompleted cutover cannot silently switch its frozen network.
        e, obs, _, proof = self.begin()
        with patch("factorio_ai.deterministic_coal_upgrade._survey", return_value={**proof, "pole": None, "rebind_network_id": 2}):
            self.assertNotIn("type", resume_coal_upgrade(e, obs))
        self.assertEqual(e.state["coal_transit_upgrade"]["expected_network_id"], 1)

    def test_cold_supervisor_epoch_rejection_precedes_destructive_sync(self):
        from factorio_ai.deterministic_supervisor import DeterministicSupervisor
        from tests import test_deterministic_supervisor as fixtures
        e, obs, _, _ = self.begin()
        for field, value in (("world_id", "other"), ("catalog_fingerprint", "other"), ("actor_unit", 99)):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                root = Path(directory)
                state = deepcopy(e.state)
                state["coal_transit_upgrade"][field] = value
                saved = json.dumps(state).encode()
                (root / "energy-expansion.json").write_bytes(saved)
                supervisor = DeterministicSupervisor(fixtures.fake_game(root))
                supervisor.catalog = SimpleNamespace(fingerprint="catalog")
                supervisor.builder = SimpleNamespace(state={}, _sync=Mock())
                supervisor.factory = SimpleNamespace(state={}, _sync=Mock())
                supervisor.bootstrap = SimpleNamespace(next_action=Mock())
                with patch("factorio_ai.deterministic_repair_control.pending_repair", return_value=None):
                    result = supervisor.next_action(obs, "rocket")
                self.assertEqual(result["status"], "blocked")
                supervisor.builder._sync.assert_not_called()
                supervisor.factory._sync.assert_not_called()
                supervisor.bootstrap.next_action.assert_not_called()
                self.assertEqual((root / "energy-expansion.json").read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
