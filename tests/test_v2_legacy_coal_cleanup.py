from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.legacy_coal_cleanup import LegacyCoalCleanup, LEGACY_CACHE_PROBE_LUA
from factorio_agent.core.deterministic_bootstrap import SURVEY_LAYOUT_TICKS


class LegacyCoalCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = SimpleNamespace(fingerprint="catalog", items={"coal": {"stack_size": 50}})
        self.drill = {
            "name": "burner-mining-drill",
            "position": {"x": -51, "y": 11},
            "direction": 0,
            "unit_number": 1343,
        }
        self.chest = {"name": "wooden-chest", "position": {"x": -51.5, "y": 9.5}, "direction": 0, "unit_number": 1342}
        self.electric = {
            "name": "electric-mining-drill",
            "position": {"x": -48.5, "y": 14.5},
            "direction": 4,
            "unit_number": 956,
        }
        self.auto = [
            {"name": "electric-mining-drill", "position": {"x": -53.5, "y": y}, "direction": d, "unit_number": u}
            for y, d, u in ((12.5, 8, 11), (16.5, 0, 12))
        ]
        engines = [
            {"name": "steam-engine", "position": {"x": 37.5, "y": y}, "direction": 8, "unit_number": u}
            for y, u in ((4.5, 501), (9.5, 502))
        ]
        self.obs = {
            "world_id": "w",
            "tick": 100,
            "inventory_empty_slots": 20,
            "entities": deepcopy([self.drill, self.chest, self.electric] + self.auto + engines),
        }
        self.free = 20
        self.power = True
        self.automatic = True
        self.probe_failure = None
        self.cell = {
            "drill": {k: self.drill[k] for k in ("name", "position", "direction")},
            "receiver": {k: self.chest[k] for k in ("name", "position")},
            "electric": False,
            "direct_belt": False,
            "drop_position": {"x": -51.5, "y": 9.65},
        }
        self.bootstrap = SimpleNamespace(
            _existing_cells=Mock(
                return_value={
                    "ok": True,
                    "cells": [
                        deepcopy(self.cell),
                        {"drill": self.electric, "receiver": None, "electric": True, "direct_belt": True},
                    ],
                }
            )
        )
        self.builder = SimpleNamespace(state={"coal_plan": {"drill": deepcopy(self.electric)}})
        self.storage = SimpleNamespace(step=Mock(return_value=None))
        self.outposts = SimpleNamespace(
            path=self.root / "mining-outposts.json",
            state={
                "world_id": "w",
                "banks": [
                    {"ready": True, "plan": {"resource": "coal", "paired": True, "entities": deepcopy(self.auto)}}
                ],
            },
        )
        self.outposts.bank_proof = Mock(
            side_effect=lambda *_: (
                {
                    "drill_units": [11, 12],
                    "native_producer_lanes": {"ok": True, "left": {"coal": 1}, "right": {"coal": 1}},
                }
                if self.automatic
                else None
            )
        )
        self.steam = SimpleNamespace(
            path=self.root / "steam-expansion.json",
            state={
                "world_id": "w",
                "catalog_fingerprint": "catalog",
                "plan": {"entities": deepcopy(self.auto + engines)},
                "samples": [],
            },
        )
        self.steam.sample = Mock(
            side_effect=lambda obs: (
                {
                    "ok": self.power,
                    "tick": obs["tick"] + 1,
                    "engines": [{"unit_number": 501, "kw": 100}, {"unit_number": 502, "kw": 100}],
                }
                if self.power
                else {"ok": False}
            )
        )
        self.steam.proof = Mock(
            side_effect=lambda *_: (
                {"native": True}
                if self.power
                and len(self.steam.state["samples"]) >= 2
                and self.steam.state["samples"][-1]["tick"] - self.steam.state["samples"][0]["tick"] >= 1800
                else None
            )
        )
        self.game = Mock()
        self.game.cfg = SimpleNamespace(runtime_dir=self.root)
        self.game.query.side_effect = self.probe
        self.controller = LegacyCoalCleanup(
            self.game, self.bootstrap, self.builder, self.catalog, self.storage, self.steam, self.outposts
        )

    def probe(self, body):
        if self.probe_failure:
            return {"ok": False, "reason": self.probe_failure}
        encoded, _ = json.JSONDecoder().raw_decode(body.split("helpers.json_to_table(", 1)[1])
        args = json.loads(encoded)
        for key in ("drill", "receiver"):
            spec = args[key]
            live = next(
                (e for e in self.obs["entities"] if e["name"] == spec["name"] and e["position"] == spec["position"]),
                None,
            )
            if live and live["unit_number"] != spec["unit_number"]:
                return {"ok": False, "reason": "legacy_target_identity_changed"}
        emitter = any(e["unit_number"] == 1343 for e in self.obs["entities"])
        receiver = any(e["unit_number"] == 1342 for e in self.obs["entities"])
        return {
            "ok": True,
            "world_id": "w",
            "emitter_present": emitter,
            "receiver_present": receiver,
            "receiver_coal": 800 if receiver else 0,
            "drill_coal": 4 if emitter else 0,
            "empty_slots": self.free,
            "inventory_size": 80,
            "receiver_box": {"left_top": {"x": -51.85, "y": 9.15}, "right_bottom": {"x": -51.15, "y": 9.85}},
            "drill_box": {"left_top": {"x": -51.7, "y": 10.3}, "right_bottom": {"x": -50.3, "y": 11.7}},
        }

    def warm(self):
        result = self.controller.step(self.obs)
        self.assertEqual(result["status"], "waiting")
        for _ in range(3):
            self.obs["tick"] += 600
            result = self.controller.step(self.obs)
        return result

    def remove(self, unit):
        self.obs["entities"] = [e for e in self.obs["entities"] if e["unit_number"] != unit]
        self.obs["tick"] += 30

    def test_prepare_reads_real_cell_shape_binds_units_and_never_writes(self):
        before = deepcopy(self.steam.state)
        with patch("factorio_agent.legacy_coal_cleanup._atomic_json") as write:
            prepared = self.controller.prepare(self.obs)
            write.assert_not_called()
        self.assertTrue(prepared["ok"])
        self.assertEqual(prepared["drill"]["unit_number"], 1343)
        self.assertEqual(prepared["receiver"]["unit_number"], 1342)
        self.assertEqual(self.steam.state, before)
        self.assertFalse(self.controller.path.exists())
        self.bootstrap._existing_cells.assert_called_once_with("coal", max_age_ticks=SURVEY_LAYOUT_TICKS)

    def test_emitter_is_mined_first_and_800_coal_cache_requires_eighteen_free_slots(self):
        mine = self.warm()
        self.assertEqual((mine["name"], mine["expected_entity_unit"]), ("burner-mining-drill", 1343))
        self.assertEqual(mine["expected_entity_world_id"], "w")
        self.remove(1343)
        self.free = 18
        receiver = self.controller.step(self.obs)
        self.assertEqual((receiver["name"], receiver["expected_entity_unit"]), ("wooden-chest", 1342))
        self.assertEqual(self.storage.step.call_args.kwargs["minimum_slots"], 18)
        self.remove(1342)
        done = self.controller.step(self.obs)
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["evidence"]["receiver_coal_checked_before_mining"], 800)
        self.assertTrue(any(e["unit_number"] == 956 for e in self.obs["entities"]))
        self.assertFalse(self.steam.path.exists())
        self.assertFalse(self.outposts.path.exists())

    def test_receiver_waits_for_normal_storage_headroom_without_spilling_or_partial_transfer(self):
        self.warm()
        self.remove(1343)
        self.free = 4
        self.storage.step.return_value = {"type": "deposit", "item": "iron-ore", "count": 500}
        result = self.controller.step(self.obs)
        self.assertEqual(result["type"], "deposit")
        self.assertEqual(self.storage.step.call_args.kwargs["minimum_slots"], 18)
        self.assertFalse(self.controller.state.get("pending"))
        self.assertTrue(any(e["unit_number"] == 1342 for e in self.obs["entities"]))

    def test_plain_metadata_and_automation_probe_failures_are_closed_before_mining(self):
        for reason in (
            "legacy_cache_contains_nonplain_or_noncoal_items",
            "legacy_cache_connected_to_inserter",
            "legacy_cache_adjacent_to_transport",
            "legacy_cache_has_another_emitter",
            "legacy_cache_has_circuit_consumers",
            "legacy_drill_mixed_resource",
        ):
            with self.subTest(reason=reason):
                self.probe_failure = reason
                result = self.controller.prepare(self.obs)
                self.assertEqual(result["reason"], reason)
                self.assertFalse(self.controller.path.exists())

    def test_automatic_plan_owned_target_is_not_called_obsolete(self):
        self.builder.state["coal_plan"]["drill"] = deepcopy(self.drill)
        self.assertEqual(self.controller.prepare(self.obs)["reason"], "legacy_target_still_owned_by_automatic_plan")

    def test_mixed_existing_unpaired_electric_and_two_paired_coal_banks_are_healthy(self):
        old = {
            "ready": True,
            "nominal_rate_per_minute": 30,
            "plan": {"resource": "coal", "entities": [deepcopy(self.electric)]},
        }
        paired = deepcopy(self.outposts.state["banks"][0])
        paired["nominal_rate_per_minute"] = 60
        material = deepcopy(paired)
        self.outposts.state["banks"] = [old, paired, material]
        unpaired_proof = {"drill_units": [956], "native_producer_lanes": None, "native_output_evidence": {"count": 1}}
        paired_proof = {
            "drill_units": [11, 12],
            "native_producer_lanes": {"ok": True, "left": {"coal": 1}, "right": {"coal": 1}},
        }
        self.outposts.bank_proof.side_effect = [unpaired_proof, paired_proof, deepcopy(paired_proof)]
        self.assertEqual(len(self.controller._automatic(self.obs)), 3)
        self.assertEqual(self.outposts.bank_proof.call_count, 3)

    def test_unpaired_burner_or_failed_electric_bank_proof_remains_rejected(self):
        unpaired = {"ready": True, "plan": {"resource": "coal", "entities": [deepcopy(self.drill)]}}
        self.outposts.state["banks"] = [unpaired]
        self.assertIsNone(self.controller._automatic(self.obs))
        self.outposts.bank_proof.assert_not_called()
        unpaired["plan"]["entities"] = [deepcopy(self.electric)]
        self.outposts.bank_proof.side_effect = None
        self.outposts.bank_proof.return_value = None
        self.assertIsNone(self.controller._automatic(self.obs))

    def test_ambiguous_or_electric_only_cells_never_authorize_deletion(self):
        self.bootstrap._existing_cells.return_value["cells"].append(deepcopy(self.cell))
        self.assertEqual(self.controller.prepare(self.obs)["candidates"], 2)
        self.bootstrap._existing_cells.return_value["cells"] = [
            {"drill": self.electric, "receiver": None, "electric": True}
        ]
        self.assertEqual(self.controller.prepare(self.obs)["candidates"], 0)

    def test_changed_saved_receiver_unit_is_never_retargeted(self):
        self.warm()
        self.remove(1343)
        next(e for e in self.obs["entities"] if e["unit_number"] == 1342)["unit_number"] = 9000
        self.assertEqual(self.controller.step(self.obs)["reason"], "legacy_cleanup_will_not_retarget_replacement")

    def test_recovery_requires_healthy_automatic_supply_before_each_mine(self):
        self.warm()
        self.remove(1343)
        self.automatic = False
        result = self.controller.step(self.obs)
        self.assertEqual(result["status"], "waiting")
        self.assertTrue(any(e["unit_number"] == 1342 for e in self.obs["entities"]))
        self.assertFalse(self.controller.state["samples"])

    def test_resume_prunes_emitter_before_receiver_and_never_mines_electric_drill(self):
        self.warm()
        self.remove(1343)
        resumed = LegacyCoalCleanup(
            self.game, self.bootstrap, self.builder, self.catalog, self.storage, self.steam, self.outposts
        )
        result = resumed.step(self.obs)
        self.assertEqual((resumed.state["phase"], result["expected_entity_unit"]), ("receiver", 1342))
        self.assertNotEqual(result["expected_entity_unit"], 956)

    def test_retiring_cache_is_excluded_from_starter_fueling_by_exact_identity(self):
        self.warm()
        self.assertTrue(self.controller.retires_legacy(self.drill, self.obs))
        self.assertFalse(self.controller.retires_legacy(self.electric, self.obs))
        replacement = dict(self.drill, unit_number=9000)
        self.assertFalse(self.controller.retires_legacy(replacement, self.obs))

    def test_native_probe_survives_query_newline_flattening(self):
        self.controller.prepare(self.obs)
        self.assertNotIn("--", LEGACY_CACHE_PROBE_LUA)
        flattened = " ".join(self.game.query.call_args.args[0].splitlines())
        self.assertIn("receiver_coal", flattened)
        self.assertNotIn("--", flattened)


if __name__ == "__main__":
    unittest.main()
