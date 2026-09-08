from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_ai.deterministic_routine_fairness import RoutineFairness, completed_action


def success(**receipt):
    return {"ok": True, "status": "succeeded", **receipt}


def transfer(count=5):
    return {"type": "insert", "name": "gun-turret", "position": {"x": 1, "y": 2},
            "item": "firearm-magazine", "count": count}


class CompletedActionTests(unittest.TestCase):
    def test_positive_adapter_receipts(self):
        cases = [(transfer(), success(moved=1))]  # A partial positive transfer did real work.
        cases += [({"type": kind}, success(moved=2)) for kind in ("take", "recover_equipped_ammo")]
        cases += [({"type": "build"}, success(unit_number=101)),
                  ({"type": "mine"}, success(mined=2)),
                  ({"type": "build_many", "actions": [{"type": "build"}] * 2},
                   success(completed=2, built=1, reused=1))]
        for action, outcome in cases:
            with self.subTest(action=action):
                self.assertTrue(completed_action(action, outcome))

    def test_navigation_polls_and_nonmaterial_acknowledgements_do_not_count(self):
        cases = [({"type": kind}, success(position={"x": 1, "y": 2}))
                 for kind in ("take", "insert", "build", "mine")]
        cases += [({"type": "move"}, success(moved=1)),
                  ({"type": "stop"}, success()),
                  ({"type": "craft"}, success(started=2)),
                  ({"type": "craft"}, {"ok": True, "status": "running", "started": 2}),
                  ({"type": "craft"}, {"ok": True, "status": "waiting", "reason": "crafting_queue_busy"}),
                  ({"type": "recipe"}, success(direction=4)),
                  ({"type": "bar"}, success(slots=1))]
        for action, outcome in cases:
            with self.subTest(action=action, outcome=outcome):
                self.assertFalse(completed_action(action, outcome))

    def test_zero_invalid_and_reused_receipts_do_not_count(self):
        for value in (None, False, True, 0, -1, "1", float("nan"), float("inf")):
            for kind, field in (("insert", "moved"), ("mine", "mined")):
                with self.subTest(kind=kind, value=value):
                    self.assertFalse(completed_action({"type": kind}, success(**{field: value})))
        for unit_number in (None, False, True, 0, -1, 1.5, "101"):
            with self.subTest(unit_number=unit_number):
                self.assertFalse(completed_action({"type": "build"}, success(unit_number=unit_number)))
        self.assertFalse(completed_action({"type": "build"}, success(unit_number=101, reused=True)))

    def test_incomplete_failed_or_different_continued_action_is_not_completed(self):
        for fields in ({"ok": False}, {"ok": 1}, {"status": "running"},
                       {"status": "waiting"}, {"continued_action": {"type": "mine"}}):
            with self.subTest(fields=fields):
                self.assertFalse(completed_action(transfer(), success(moved=5, **fields)))

    def test_batch_requires_full_completion_and_a_new_build(self):
        action = {"type": "build_many", "actions": [{"type": "build"}] * 2}
        for fields in ({"ok": False, "completed": 1, "built": 1, "failed_index": 1},
                       {"ok": False, "completed": 1, "built": 1, "uncertain_index": 1},
                       {"completed": 1, "built": 1}, {"completed": 3, "built": 1},
                       {"completed": 2, "built": 0, "reused": 2}):
            with self.subTest(fields=fields):
                self.assertFalse(completed_action(action, success(**fields)))
        for actions in (None, [], "build"):
            self.assertFalse(completed_action({"type": "build_many", "actions": actions},
                                              success(completed=0, built=1)))
        single = {"type": "build_many", "actions": [{"type": "build"}]}
        for completed in (True, 1.0, "1"):
            with self.subTest(completed=completed):
                self.assertFalse(completed_action(single, success(completed=completed, built=1)))

    def test_batch_rejects_failure_or_uncertainty_even_at_index_zero(self):
        action = {"type": "build_many", "actions": [{"type": "build"}] * 2}
        for field in ("failed_index", "uncertain_index"):
            for index in (0, 1):
                with self.subTest(field=field, index=index):
                    self.assertFalse(completed_action(action, success(completed=2, built=2, **{field: index})))


class RoutineFairnessReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=Path(self.temp.name)), query=Mock())
        self.fairness = RoutineFairness(self.game)
        self.assertTrue(self.fairness._sync({"world_id": "world-a", "tick": 100}, "catalog-a"))
        self.fairness.safety = {"ok": True, "quiet": True, "routes_ready": True}

    def saved(self):
        return json.loads(self.fairness.path.read_text(encoding="utf-8"))

    def complete_routine(self, count=1):
        for _ in range(count):
            action = self.fairness.bind("routine", transfer())
            self.fairness.record(action, success(moved=1))

    def test_each_bound_receipt_is_consumed_once_and_batch_counts_once(self):
        action = transfer()
        self.fairness.record(action, success(moved=1))
        self.assertEqual(self.fairness.state["completed"], 0)
        self.assertIs(self.fairness.bind("routine", action), action)
        with patch.object(self.fairness, "_save", wraps=self.fairness._save) as save:
            self.fairness.record(deepcopy(action), success(moved=1))
            self.fairness.record(action, success(moved=1))
            save.assert_called_once_with()
        self.assertEqual(self.saved()["completed"], 1)
        batch = {"type": "build_many", "actions": [{"type": "build"}] * 3}
        self.fairness.bind("routine", batch)
        self.fairness.record(batch, success(completed=3, built=3))
        self.assertEqual(self.saved()["completed"], 2)
        self.complete_routine(3)
        self.assertEqual(self.saved()["completed"], 3)

    def test_selection_matches_exact_action_and_cannot_be_mutated_after_bind(self):
        for change in ({"count": 10}, {"position": {"x": 2, "y": 2}}, {"type": "take"}):
            with self.subTest(change=change):
                selected = transfer()
                self.fairness.bind("routine", selected)
                changed = {**selected, **change}
                with patch.object(self.fairness, "_save") as save:
                    self.fairness.record(changed, success(moved=1))
                    self.fairness.record(selected, success(moved=1))
                    save.assert_not_called()
        batch = {"type": "build_many", "actions": [{"type": "build", "position": {"x": 1, "y": 2}}]}
        self.fairness.bind("routine", batch)
        batch["actions"][0]["position"]["x"] = 3
        self.fairness.record(batch, success(completed=1, built=1))
        self.assertEqual(self.saved()["completed"], 0)

    def test_counter_three_survives_noncompleted_work_in_either_lane(self):
        self.complete_routine(3)
        cases = [(transfer(), success(position={"x": 1, "y": 2})),
                 (transfer(), success(moved=0)),
                 ({"type": "build"}, success(unit_number=101, reused=True)),
                 ({"type": "craft"}, {"ok": True, "status": "running", "started": 2}),
                 ({"type": "move"}, success()),
                 ({"type": "build_many", "actions": [{"type": "build"}] * 2},
                  {"ok": False, "status": "failed", "completed": 1, "built": 1, "failed_index": 1})]
        for lane in ("routine", "production"):
            for action, outcome in cases:
                with self.subTest(lane=lane, action=action):
                    self.fairness.bind(lane, action)
                    before = self.fairness.path.read_bytes()
                    with patch.object(self.fairness, "_save") as save:
                        self.fairness.record(action, outcome)
                        save.assert_not_called()
                    self.assertEqual(self.fairness.state["completed"], 3)
                    self.assertEqual(self.fairness.path.read_bytes(), before)
        action = self.fairness.bind("production", {"type": "build"})
        self.fairness.record(action, success(unit_number=102))
        self.assertEqual(self.saved()["completed"], 0)

    def test_noncompleted_receipt_consumes_selection_before_later_positive_receipt(self):
        action = self.fairness.bind("routine", transfer())
        self.fairness.record(action, success(position={"x": 1, "y": 2}))
        self.fairness.record(action, success(moved=1))
        self.assertEqual(self.saved()["completed"], 0)

    def test_routine_requires_quiet_evidence_at_selection(self):
        for safety in ({"ok": False, "quiet": True}, {"ok": True, "quiet": False}, {}):
            with self.subTest(safety=safety):
                self.fairness.safety = safety
                action = self.fairness.bind("routine", transfer())
                self.fairness.safety = {"ok": True, "quiet": True}
                self.fairness.record(action, success(moved=1))
                self.assertEqual(self.saved()["completed"], 0)
        action = self.fairness.bind("emergency", transfer())
        self.fairness.record(action, success(moved=1))
        self.assertEqual(self.saved()["completed"], 0)

    def test_restore_retains_counter_but_never_pending_selection(self):
        self.complete_routine(3)
        action = self.fairness.bind("routine", transfer())
        restored = RoutineFairness(self.game)
        self.assertIsNone(restored.selection)
        self.assertFalse(restored.safety["ok"])
        restored.record(action, success(moved=1))
        self.assertTrue(restored._sync({"world_id": "world-a", "tick": 101}, "catalog-a"))
        self.assertEqual(restored.state["completed"], 3)
        self.assertEqual(self.saved()["last_tick"], 101)

    def test_rollback_world_or_catalog_change_resets_persisted_counter(self):
        for world, tick, catalog in (("world-a", 99, "catalog-a"),
                                     ("world-b", 100, "catalog-a"),
                                     ("world-a", 100, "catalog-b")):
            with self.subTest(world=world, tick=tick, catalog=catalog):
                self.fairness._sync({"world_id": "world-a", "tick": 100}, "catalog-a")
                self.fairness.safety = {"ok": True, "quiet": True}
                self.complete_routine(3)
                old_action = self.fairness.bind("routine", transfer())
                factory = SimpleNamespace(catalog=SimpleNamespace(fingerprint=catalog))
                defense = SimpleNamespace(_assets=Mock(return_value=[]), _observe_damage=Mock(return_value=False))
                self.game.query.return_value = {"ok": True, "quiet": True, "routes_ready": True}
                with patch.object(self.fairness, "_payload", return_value={}):
                    self.assertFalse(self.fairness.prefer_production(factory, object(), defense,
                                                                    {"world_id": world, "tick": tick}))
                self.fairness.record(old_action, success(moved=1))
                self.assertEqual(self.saved(), {"schema_version": 1, "world_id": world,
                                               "catalog_fingerprint": catalog, "last_tick": tick, "completed": 0})


if __name__ == "__main__":
    unittest.main()
