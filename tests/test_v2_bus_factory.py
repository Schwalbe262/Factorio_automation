from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from factorio_agent.board import entity
from factorio_agent.bus_factory import BusFactory, match_recoveries, trace_corridor


class MigrationCorridorTests(unittest.TestCase):
    def test_saved_path_trace_preserves_unrelated_parallel_assets(self):
        es = [
            entity("transport-belt", 0.5, 4.5, 0),
            dict(entity("underground-belt", 0.5, 3.5, 0), belt_to_ground_type="input"),
            dict(entity("underground-belt", 0.5, 0.5, 0), belt_to_ground_type="output"),
            entity("transport-belt", 0.5, -0.5, 4),
            entity("transport-belt", 1.5, -0.5, 4),
            entity("transport-belt", 8.5, 4.5, 0),
        ]
        plan = {"entities": es, "underground_pairs": [{"input": [0.5, 3.5], "output": [0.5, 0.5], "direction": 0}]}
        result = trace_corridor(plan, es[0], es[4])
        self.assertTrue(result["ok"])
        self.assertEqual(result["entities"], es[:5])
        self.assertNotIn(es[-1], result["entities"])
        self.assertEqual(result["entities"][1:-1], es[1:4])

    def test_trace_requires_exact_declared_pair_and_rejects_splitters(self):
        first = dict(entity("underground-belt", 0.5, 3.5), belt_to_ground_type="input")
        last = dict(entity("underground-belt", 0.5, 0.5), belt_to_ground_type="output")
        plan = {"entities": [first, last]}
        self.assertEqual(trace_corridor(plan, first, last)["reason"], "saved_corridor_underground_pair_missing")
        splitter = entity("splitter", 1, 3.5)
        self.assertEqual(
            trace_corridor({"entities": [splitter]}, splitter, last)["reason"],
            "saved_corridor_requires_explicit_splitter_branch",
        )

    def test_trace_loop_and_missing_segment_cannot_authorize_area_recovery(self):
        a, b = entity("transport-belt", 0.5, 0.5, 4), entity("transport-belt", 1.5, 0.5, 12)
        goal = entity("transport-belt", 5.5, 0.5, 4)
        self.assertEqual(trace_corridor({"entities": [a, b]}, a, goal)["reason"], "saved_corridor_loop")
        self.assertEqual(trace_corridor({"entities": [a]}, a, goal)["reason"], "saved_corridor_has_gap")

    def test_owned_specs_bind_exact_observed_direction_and_underground_role(self):
        wanted = dict(entity("underground-belt", 0.5, 0.5, 4), belt_to_ground_type="input")
        live = dict(wanted, unit_number=55)
        self.assertEqual(match_recoveries({"entities": [live]}, [wanted])["entities"][0]["unit_number"], 55)
        changed = dict(live, belt_to_ground_type="output")
        self.assertFalse(match_recoveries({"entities": [changed]}, [wanted])["ok"])
        self.assertEqual(match_recoveries({"entities": []}, [wanted]), {"ok": True, "entities": []})


class MigrationCoordinatorTests(unittest.TestCase):
    def coordinator(self):
        coordinator = BusFactory.__new__(BusFactory)
        coordinator.state = {}
        coordinator.catalog = SimpleNamespace(fingerprint="catalog")
        coordinator._save = lambda: None
        return coordinator

    def test_recovery_prunes_old_identity_before_rebuilding_same_tile(self):
        c = self.coordinator()
        wanted = dict(entity("transport-belt", 0.5, 0.5, 0), unit_number=100)
        c.state["remove"] = [wanted]
        self.assertIsNone(c._recover({"world_id": "w", "entities": []}, "remove"))
        self.assertEqual(c.state["remove"], [])
        replacement = dict(wanted, unit_number=101)
        self.assertIsNone(c._recover({"world_id": "w", "entities": [replacement]}, "remove"))

    def test_recovery_rejects_unexpected_same_name_replacement(self):
        c = self.coordinator()
        wanted = dict(entity("transport-belt", 0.5, 0.5, 0), unit_number=100)
        c.state["remove"] = [wanted]
        self.assertEqual(
            c._recover({"world_id": "w", "entities": [dict(wanted, unit_number=101)]}, "remove")["status"], "blocked"
        )

    def test_recovery_batch_is_bounded_and_every_mine_has_unit_and_world_guards(self):
        c = self.coordinator()
        c.state["remove"] = [dict(entity("transport-belt", i + 0.5, 0.5, 0), unit_number=i + 1) for i in range(13)]
        action = c._recover({"world_id": "w", "entities": c.state["remove"]}, "remove")
        self.assertEqual(action["type"], "mine_many")
        self.assertEqual(len(action["actions"]), 8)
        self.assertEqual([a["expected_entity_unit"] for a in action["actions"]], list(range(1, 9)))
        self.assertTrue(
            all(
                a["type"] == "mine" and a["count"] == 1 and a["expected_entity_world_id"] == "w"
                for a in action["actions"]
            )
        )

    def test_step_rejects_changed_world_before_recovery_or_build(self):
        c = self.coordinator()
        c.state = {"world_id": "old", "catalog_fingerprint": "catalog", "phase": "recover"}
        self.assertEqual(c.step({"world_id": "new"})["reason"], "bus_factory_world_or_catalog_changed")

    def test_prepare_rejects_stale_source_without_writing_any_runtime_state(self):
        c = self.coordinator()
        c.smelting = c.science = c.green = SimpleNamespace(state={"world_id": "old", "catalog_fingerprint": "catalog"})
        with patch("factorio_agent.bus_factory._atomic_json") as write:
            result = c.prepare({"world_id": "new"})
            self.assertFalse(result["ok"])
            write.assert_not_called()

    def test_publish_preserves_fresh_completed_producer_expansion_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            c = self.coordinator()
            c.path = root / "bus-factory.json"
            c.smelting = SimpleNamespace(path=root / "smelting.json", state={"banks": ["stale-five"]})
            c.science = SimpleNamespace(path=root / "science.json", state={})
            c.green = SimpleNamespace(path=root / "green.json", state={})
            c.smelting.path.write_text(
                json.dumps({"banks": ["fresh-six"], "nominal_smelting_capacity_per_minute": {"iron-ore": 225}})
            )
            c.science.path.write_text(json.dumps({"science": "fixed-red"}))
            (root / "pcb-board.json").write_text(json.dumps({"blocks": {}}))
            c.state = {
                key: {} for key in ("plan", "bus", "core", "source_plan", "red_plan", "lab_join", "rated_demand")
            }
            c.state.update(
                world_id="w",
                catalog_fingerprint="catalog",
                requested_rate=30,
                nominal_machine_rate=35,
                route_metrics=[],
                old_green_reserved_area=[[-107, -83], [-53, -30]],
            )
            c._publish()
            self.assertEqual(c.smelting.state["banks"], ["fresh-six"])
            self.assertEqual(c.smelting.state["nominal_smelting_capacity_per_minute"]["iron-ore"], 225)
            self.assertTrue(c.green.state["compact_main_bus"])
            board = json.loads((root / "pcb-board.json").read_text())
            self.assertIn("retiring_logistic_science", board["blocks"])

    def proof_fixture(self):
        c = self.coordinator()
        machines = [
            entity(
                "assembling-machine-1", i * 4 + 0.5, 0.5, recipe="logistic-science-pack" if i < 7 else "iron-gear-wheel"
            )
            for i in range(12)
        ]
        splitter = dict(
            entity("splitter", -11, -30.5), splitter_input_priority="left", splitter_output_priority="right"
        )
        belt = entity("transport-belt", -11.5, -40.5)
        labs = [entity("lab", i * 4 + 0.5, -20.5) for i in range(8)]
        plan = {"entities": machines + [splitter, belt], "underground_pairs": []}
        c.state = {
            "world_id": "w",
            "plan": plan,
            "bus": {
                "trunks": [{"x": -11.5, "item": "iron-plate", "line": 0}],
                "entities": [belt],
                "stations": [{"splitter": splitter}],
            },
        }
        c.science = SimpleNamespace(state={"labs": {"entities": labs}}, proof=lambda _: {"science_units": [1]})
        c.smelting = SimpleNamespace(proof=lambda _: {"banks": "current_source_healthy"})
        c.green = SimpleNamespace(proof=lambda _: {"science_units": [2]})
        actual = []
        for i, spec in enumerate(plan["entities"] + labs):
            live = dict(
                spec,
                unit_number=i + 1,
                energy=100,
                electric_network_connected=True,
                products_finished=1,
                inventory={"logistic-science-pack": 1},
            )
            if spec == belt:
                live["belt_inventory"] = {"iron-plate": 1}
            actual.append(live)
        return c, {"world_id": "w", "tick": 1234, "entities": actual}

    def test_commissioning_proof_requires_native_priorities_typed_materials_and_all_labs(self):
        c, obs = self.proof_fixture()
        self.assertIsNotNone(c.proof(obs))
        for mutate in (
            lambda es: es[12].update(splitter_output_priority="none"),
            lambda es: es[13].update(belt_inventory={"copper-plate": 1}),
            lambda es: es[0].update(products_finished=0),
            lambda es: es[0].update(energy=0),
            lambda es: es[-1].update(inventory={}),
        ):
            changed = deepcopy(obs)
            mutate(changed["entities"])
            self.assertIsNone(c.proof(changed))

    def test_old_western_retirement_cannot_begin_before_commissioning(self):
        c = self.coordinator()
        c.state = {
            "world_id": "w",
            "catalog_fingerprint": "catalog",
            "phase": "build",
            "published": True,
            "plan": {},
            "cleanup": [dict(entity("assembling-machine-1", 0.5, 0.5), unit_number=123)],
        }
        c.builder = SimpleNamespace(ensure_plan=lambda *_: {"status": "succeeded"})
        c.proof = lambda _: None
        result = c.step({"world_id": "w"})
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(c.state["phase"], "commission")
        self.assertEqual(len(c.state["cleanup"]), 1)

    def test_retirement_does_not_continue_after_replacement_factory_fails(self):
        c = self.coordinator()
        c.state = {
            "world_id": "w",
            "catalog_fingerprint": "catalog",
            "phase": "retire",
            "published": True,
            "plan": {},
            "cleanup": [dict(entity("assembling-machine-1", 0.5, 0.5), unit_number=123)],
        }
        c.proof = lambda _: None
        c._recover = lambda *_: (_ for _ in ()).throw(
            AssertionError("old factory must remain while replacement is unhealthy")
        )
        result = c.step({"world_id": "w"})
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(c.state["phase"], "retire")
        self.assertEqual(len(c.state["cleanup"]), 1)

    def test_source_damage_blocks_commissioning_even_with_buffered_products(self):
        c, obs = self.proof_fixture()
        c.smelting = SimpleNamespace(proof=lambda _: None)
        self.assertIsNone(c.proof(obs))


if __name__ == "__main__":
    unittest.main()
