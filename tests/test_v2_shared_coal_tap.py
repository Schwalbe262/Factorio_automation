from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from factorio_agent.core.deterministic_builder import _plan
from factorio_agent.board import entity
from factorio_agent.brick_production import brick_core
from factorio_agent.industrial import _plan_revision, _source_identity
from factorio_agent.shared_coal_tap import SharedCoalTap, coal_header_candidates, replace_header
from factorio_agent.smelting import merge_entities
from test_v2_industrial import fixture, material


MODULE = "factorio_agent.shared_coal_tap."


def setup(root):
    industrial, obs, _, registry = fixture(root)
    registry["sources"]["coal"] = material("coal", 5277, -5.5, 19.5)
    registry["sources"]["brick-coal"] = material("coal", 5278, -5.5, 20.5)
    Path(root, "material-sources.json").write_text(json.dumps(registry))
    core = brick_core(industrial.catalog, 20, -10)
    header = [entity("transport-belt", x + 0.5, 20.5, 4) for x in range(-6, 15)]
    header += [entity("transport-belt", 15.5, y + 0.5, 0) for y in range(20, -12, -1)]
    plan = _plan(
        merge_entities(core["entities"], header),
        core["ports"],
        reserved_area=core["reserved_area"],
        underground_pairs=[],
    )
    saved = {
        "ok": True,
        "world_id": "w",
        "catalog_fingerprint": "catalog",
        "phase": "verified",
        "core": core,
        "plan": plan,
        "sources": [registry["sources"]["stone"], registry["sources"]["brick-coal"]],
    }
    Path(root, "brick-production.json").write_text(json.dumps(saved))
    industrial.brick.state = saved
    board_path = Path(root, "pcb-board.json")
    board = json.loads(board_path.read_text())
    board["blocks"]["brick_production"] = plan
    board_path.write_text(json.dumps(board))
    industrial.state = {
        "world_id": "w",
        "catalog_fingerprint": "catalog",
        "steel_sources": {"iron": {"item": "iron-plate"}, "coal": registry["sources"]["coal"]},
        "producer_authorizations": {"brick": {"plan_revision": _plan_revision(plan)}},
    }
    Path(root, "industrial-operations.json").write_text(json.dumps(industrial.state))
    industrial._supply = Mock(return_value={"iron": {"item": "iron-plate"}})
    for index, spec in enumerate(plan["entities"]):
        live = dict(spec, unit_number=6000 + index)
        if spec["name"] == "transport-belt" and spec["position"] == registry["sources"]["brick-coal"]["position"]:
            live["unit_number"] = 5278
        obs["entities"].append(live)
    obs["entities"].append(dict(entity("transport-belt", -5.5, 19.5, 4), unit_number=5277))
    industrial.game.query.return_value = {
        "ok": True,
        "stock": {"splitter": 1, "transport-belt": 1},
        "tick": 101,
        "steel_coal": 4,
        "brick_coal": 5,
    }
    industrial.builder.ensure_plan = Mock(return_value={"status": "succeeded"})
    industrial.builder.bootstrap.ensure_item.return_value = {"type": "craft", "recipe": "splitter", "count": 1}
    controller = SharedCoalTap(
        industrial.game, industrial.builder, industrial.catalog, industrial.brick, industrial=industrial
    )
    return controller, industrial, obs, registry


class SharedCoalTapTests(unittest.TestCase):
    def prepare(self, root):
        tap, industrial, obs, registry = setup(root)

        def prospective(observation, core, iron, coal, added, unit, budget):
            return {"ok": True, "entities": merge_entities(added, core["entities"]), "core": core, "route_metrics": []}

        with (
            patch(MODULE + "preflight", return_value={"ok": True, "clear_obstacles": []}),
            patch(MODULE + "survey_slots", return_value={"ok": True, "legal": [1]}),
            patch.object(tap, "_prospective_steel", side_effect=prospective),
        ):
            ready = tap.prepare(obs)
        self.assertTrue(ready["ok"], ready)
        return tap, industrial, obs, registry, ready

    def test_header_is_derived_from_straight_consumer_route_and_faces_above_it(self):
        with tempfile.TemporaryDirectory() as root:
            tap, _, _, _, ready = self.prepare(root)
            choices = coal_header_candidates(ready["brick_snapshot"])
            self.assertEqual(choices[0]["old"]["position"], {"x": 13.5, "y": 20.5})
            self.assertEqual(ready["splitter"]["position"], {"x": 13.5, "y": 20})
            self.assertEqual(ready["outlet"]["position"], {"x": 14.5, "y": 19.5})
            self.assertEqual(ready["outlet"]["direction"], 0)
            self.assertEqual(
                (ready["splitter"]["splitter_input_priority"], ready["splitter"]["splitter_output_priority"]),
                ("right", "none"),
            )
            self.assertEqual(ready["required_stock"], {"splitter": 1, "transport-belt": 1})
            self.assertEqual(tap.state, {})
            self.assertFalse(tap.path.exists())

    def test_replacement_preserves_every_other_entity_underground_pair_and_core_port(self):
        with tempfile.TemporaryDirectory() as root:
            _, _, _, _, ready = self.prepare(root)
            old = ready["brick_snapshot"]["plan"]
            new = ready["final_brick_plan"]
            self.assertEqual(old["ports"], new["ports"])
            self.assertTrue(all(e in new["entities"] for e in old["entities"] if e != ready["old_spec"]))
            self.assertEqual(old["underground_pairs"], new["underground_pairs"])
            self.assertEqual(new, replace_header(new, ready["old_spec"], ready["new_entities"]))

    def test_built_steel_or_changed_brick_authorities_prevent_preparation(self):
        with tempfile.TemporaryDirectory() as root:
            tap, industrial, obs, _ = setup(root)
            industrial.steel.state = {"phase": "build"}
            self.assertEqual(tap.prepare(obs)["reason"], "shared_coal_adoption_requires_unbuilt_steel")
            industrial.steel.state = {}
            path = Path(root, "pcb-board.json")
            board = json.loads(path.read_text())
            board["blocks"]["brick_production"]["entities"].pop()
            path.write_text(json.dumps(board))
            self.assertEqual(tap.prepare(obs)["reason"], "shared_coal_brick_authorities_disagree")

    def test_paid_stock_precedes_prebuild_and_exact_one_corner_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            tap, industrial, obs, _, ready = self.prepare(root)
            tap.state = ready
            tap.game.query.return_value = {"stock": {}}
            self.assertEqual(tap.step(obs)["type"], "craft")
            tap.builder.ensure_plan.assert_not_called()
            tap.state["phase"] = "cutover"
            tap.game.query.return_value = {"stock": {"splitter": 1}}
            with patch(MODULE + "preflight", return_value={"ok": True}):
                action = tap.step(obs)
            self.assertEqual(action["type"], "mine")
            self.assertEqual(action["expected_entity_unit"], ready["old"]["unit_number"])
            self.assertEqual(action["expected_entity_world_id"], "w")
            self.assertEqual(json.loads(tap.path.read_text())["recovery_started"], True)

    def test_unstarted_missing_corner_or_foreign_replacement_never_gets_retargeted(self):
        with tempfile.TemporaryDirectory() as root:
            tap, _, obs, _, ready = self.prepare(root)
            tap.state = ready
            tap.state["phase"] = "cutover"
            obs["entities"] = [e for e in obs["entities"] if e.get("unit_number") != ready["old"]["unit_number"]]
            with patch(MODULE + "preflight", return_value={"ok": True}):
                self.assertEqual(tap.step(obs)["reason"], "shared_coal_unstarted_corner_missing")
            obs["entities"].append(dict(ready["old_spec"], unit_number=99999))
            with patch(MODULE + "preflight", return_value={"ok": True}):
                self.assertEqual(tap.step(obs)["reason"], "shared_coal_recovery_unit_changed")

    def live(self, root):
        tap, industrial, obs, registry, ready = self.prepare(root)
        tap.state = ready
        tap.state["phase"] = "commission"
        obs["entities"] = [e for e in obs["entities"] if e.get("unit_number") != ready["old"]["unit_number"]]
        for index, spec in enumerate(ready["new_entities"]):
            obs["entities"].append(dict(spec, unit_number=9000 + index))
        return tap, industrial, obs, registry

    def test_both_native_coal_outputs_and_existing_brick_health_are_required(self):
        with tempfile.TemporaryDirectory() as root:
            tap, _, obs, _ = self.live(root)
            with patch(MODULE + "BrickProduction.proof", return_value={"tick": 100, "normal_bricks": True}):
                self.assertIsNotNone(tap._health(obs))
                tap.game.query.return_value["steel_coal"] = 0
                self.assertIsNone(tap._health(obs))
            tap.game.query.return_value["steel_coal"] = 4
            with patch(MODULE + "BrickProduction.proof", return_value=None):
                self.assertIsNone(tap._health(obs))

    def test_proven_publication_preserves_sources_and_shared_capacity_then_explicitly_adopts(self):
        with tempfile.TemporaryDirectory() as root:
            tap, industrial, obs, registry = self.live(root)
            with patch(MODULE + "BrickProduction.proof", return_value={"tick": 100, "normal_bricks": True}):
                proof = tap._health(obs)
            self.assertIsNone(tap._publish(obs, proof))
            saved = json.loads(Path(root, "material-sources.json").read_text())["sources"]
            self.assertEqual(saved["coal"], registry["sources"]["coal"])
            self.assertEqual(saved["brick-coal"], registry["sources"]["brick-coal"])
            self.assertEqual(saved["steel-coal"]["nominal_shared_rate_per_minute"], 60)
            self.assertEqual(saved["steel-coal"]["material_supply_id"], saved["brick-coal"]["material_supply_id"])
            self.assertEqual(industrial.state["steel_coal_alias"], "steel-coal")
            self.assertEqual(industrial.state["steel_sources"]["coal"]["unit_number"], 9001)
            self.assertEqual(industrial.state["coal_adoption"]["from"]["unit_number"], 5277)
            self.assertEqual(
                industrial.state["producer_authorizations"]["brick"]["plan_revision"], tap.state["new_brick_revision"]
            )
            self.assertEqual(industrial.brick.state["core"]["ports"], tap.state["core_ports"])
            self.assertIsNone(tap._publish(obs, proof))
            self.assertEqual(industrial.state["coal_adoption"]["from"]["unit_number"], 5277)

    def test_adoption_cannot_change_an_existing_steel_consumer(self):
        with tempfile.TemporaryDirectory() as root:
            tap, industrial, obs, _ = self.live(root)
            with patch(MODULE + "BrickProduction.proof", return_value={"tick": 100}):
                proof = tap._health(obs)
            industrial.steel.state = {"phase": "build"}
            self.assertEqual(tap._publish(obs, proof)["reason"], "shared_coal_cannot_retarget_built_steel")
            self.assertNotIn("steel-coal", json.loads(Path(root, "material-sources.json").read_text())["sources"])

    def test_mixed_authority_publication_resumes_after_an_interrupted_write(self):
        with tempfile.TemporaryDirectory() as root:
            tap, _, obs, _ = self.live(root)
            with patch(MODULE + "BrickProduction.proof", return_value={"tick": 100}):
                proof = tap._health(obs)
            from factorio_agent.core.deterministic_state import _atomic_json

            writes = []

            def interrupted(path, value):
                writes.append(path)
                if len(writes) == 2:
                    raise OSError("interrupted")
                _atomic_json(path, value)

            with patch(MODULE + "_atomic_json", side_effect=interrupted):
                with self.assertRaises(OSError):
                    tap._publish(obs, proof)
            self.assertEqual(
                _plan_revision(json.loads(tap.brick.path.read_text())["plan"]), tap.state["new_brick_revision"]
            )
            self.assertIsNone(tap._publish(obs, proof))


if __name__ == "__main__":
    unittest.main()
