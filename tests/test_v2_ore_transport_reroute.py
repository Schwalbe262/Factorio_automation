from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from factorio_agent.core.deterministic_builder import FactoryBuilder, _plan
from factorio_agent.board import entity
from factorio_agent.ore_transport_reroute import OreTransportReroute, delivery_context, plan_revision
from factorio_agent.routing import expand, metrics
from factorio_agent.smelting import merge_entities, port


MODULE = "factorio_agent.ore_transport_reroute."


def fixture(root):
    # A registered side-load joins a downward trunk before its off-patch exit.
    trunk = [entity("transport-belt", 0.5, y + 0.5, 8 if y < 4 else 12) for y in range(5)]
    trunk += [entity("transport-belt", x + 0.5, 4.5, 12) for x in range(-1, -9, -1)]
    destination = port("iron-ore", -7.5, 4.5, 8)
    trunk[-1]["direction"] = 8
    primary = port("iron-ore", 0.5, 0.5, 8, "output")
    furnace = entity("stone-furnace", -12, 8)
    iron = _plan([furnace], [destination], resource="iron-ore", product="iron-plate")
    specplan = _plan(trunk + [furnace], [port("iron-plate", -8.5, 10.5, 4, "output")], underground_pairs=[])
    bank = {"plan": _plan([trunk[0]], [primary], resource="iron-ore"), "nominal_rate_per_minute": 180, "ready": True}
    join = dict(trunk[1], unit_number=101)
    secondary = {
        "plan": _plan(
            [entity("transport-belt", 2.5, 1.5, 12)], [port("iron-ore", 2.5, 1.5, 12, "output")], resource="iron-ore"
        ),
        "nominal_rate_per_minute": 120,
        "ready": True,
        "consumer_connection": {"target": join, "item": "iron-ore"},
    }
    docs = {
        "smelting": {"plan": specplan, "banks": [iron]},
        "outposts": {"banks": [bank, secondary]},
        "factory": {"source_plan": deepcopy(specplan), "plan": _plan([], [])},
        "green": {"source_plan": deepcopy(specplan), "plan": _plan([], [])},
        "board": {"blocks": {"smelting_district": deepcopy(specplan)}},
    }
    names = {
        "smelting": "smelting-district.json",
        "outposts": "mining-outposts.json",
        "factory": "bus-factory.json",
        "green": "logistic-science.json",
        "board": "pcb-board.json",
    }
    for key, doc in docs.items():
        doc.update(world_id="w", catalog_fingerprint="catalog")
        Path(root, names[key]).write_text(json.dumps(doc))
    obs = {"world_id": "w", "tick": 100, "inventory": {}, "entities": []}
    owned = merge_entities(specplan["entities"], bank["plan"]["entities"], secondary["plan"]["entities"])
    for index, spec in enumerate(owned):
        obs["entities"].append(dict(spec, unit_number=100 + index, products_finished=10))
    geometry = FactoryBuilder.__new__(FactoryBuilder)
    bootstrap = SimpleNamespace(
        ensure_item=Mock(return_value={"type": "craft", "recipe": "transport-belt", "count": 1})
    )
    builder = SimpleNamespace(
        _occupied_by_plan=geometry._occupied_by_plan,
        bootstrap=bootstrap,
        ensure_plan=Mock(return_value={"status": "succeeded"}),
    )
    game = SimpleNamespace(cfg=SimpleNamespace(runtime_dir=root), query=Mock(return_value={"ok": True, "index": 3}))
    smelting = SimpleNamespace(
        path=Path(root, names["smelting"]), state=docs["smelting"], proof=Mock(return_value={"ok": True})
    )
    outposts = SimpleNamespace(
        path=Path(root, names["outposts"]), state=docs["outposts"], bank_proof=Mock(return_value={"powered": True})
    )
    controller = OreTransportReroute(game, builder, SimpleNamespace(fingerprint="catalog"), smelting, outposts)
    path = expand([(0.5, 3.5), (0.5, 5.5), (-8.5, 5.5), (-8.5, 3.5), (-7.5, 3.5), (-7.5, 4.5)])
    route_entities = []
    for index, (x, y) in enumerate(path):
        direction = 8
        if index + 1 < len(path):
            dx, dy = path[index + 1][0] - x, path[index + 1][1] - y
            direction = {(0, -1): 0, (1, 0): 4, (0, 1): 8, (-1, 0): 12}[dx, dy]
        route_entities.append(entity("transport-belt", x, y, direction))
    route = _plan(route_entities, [], underground_pairs=[], clear_obstacles=[], route_metrics=metrics(path))
    return controller, obs, docs, route


class OreReroutePrepareTests(unittest.TestCase):
    def prepared(self, root, *, check=None):
        c, obs, docs, route = fixture(root)
        with (
            patch(MODULE + "plan_connection", return_value=route),
            patch(MODULE + "preflight", return_value=check or {"ok": True, "clear_obstacles": []}),
            patch(MODULE + "underground_proof", return_value=True),
        ):
            result = c.prepare(obs)
        return c, obs, docs, route, result

    def test_registered_join_and_first_clear_are_derived_without_hardcoded_units(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs, docs, _ = fixture(root)
            context = delivery_context(docs["smelting"], docs["outposts"])
            self.assertEqual(context["last_join_index"], 1)
            self.assertEqual(context["joins"][0]["unit_number"], 101)
            docs["outposts"]["banks"][1]["consumer_connection"]["target"]["position"]["x"] = 99
            with self.assertRaises(ValueError):
                delivery_context(docs["smelting"], docs["outposts"])

    def test_prepare_is_read_only_and_preserves_all_source_units_and_ports(self):
        with tempfile.TemporaryDirectory() as root:
            with patch(MODULE + "_atomic_json") as write:
                c, obs, docs, route, ready = self.prepared(root)
                self.assertTrue(ready["ok"], ready)
                self.assertEqual(ready["source_port"]["position"], {"x": 0.5, "y": 3.5})
                self.assertEqual(ready["registered_joins"][0]["unit_number"], 101)
                self.assertEqual([e["unit_number"] for e in ready["preserved_local_escape"]], [101, 102])
                self.assertFalse(any("mining_resource" in e for e in ready["route"]["entities"]))
                self.assertEqual(ready["final_plan"]["ports"], docs["smelting"]["plan"]["ports"])
                self.assertEqual(c.state, {})
                write.assert_not_called()
                c.builder.ensure_plan.assert_not_called()

    def test_mismatched_source_copy_blocks_before_native_survey(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs, docs, _ = fixture(root)
            docs["green"]["source_plan"]["entities"][0]["direction"] = 4
            c.paths["green"].write_text(json.dumps(docs["green"]))
            self.assertEqual(c.prepare(obs)["reason"], "ore_source_plan_copies_disagree")
            c.game.query.assert_not_called()

    def test_stale_registered_join_unit_and_missing_source_fail_closed(self):
        for mutate in (lambda obs: obs["entities"][1].update(unit_number=999), lambda obs: obs["entities"].pop(0)):
            with tempfile.TemporaryDirectory() as root:
                c, obs, _, _ = fixture(root)
                mutate(obs)
                self.assertFalse(c.prepare(obs)["ok"])
                c.game.query.assert_not_called()

    def test_resource_survey_and_whole_preflight_block_all_actions(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs, _, _ = fixture(root)
            c.game.query.return_value = {"ok": False, "reason": "ore_local_escape_crosses_foreign_resource"}
            self.assertEqual(c.prepare(obs)["reason"], "ore_local_escape_crosses_foreign_resource")
            c.builder.ensure_plan.assert_not_called()
        with tempfile.TemporaryDirectory() as root:
            c, _, _, _, ready = self.prepared(
                root, check={"ok": False, "reason": "new_entity_over_resource_or_mining_reservation"}
            )
            self.assertFalse(ready["ok"])
            c.builder.ensure_plan.assert_not_called()

    def test_intermediate_preflight_ignores_only_actual_cutover_not_retirement(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs, _, route = fixture(root)
            with (
                patch(MODULE + "plan_connection", return_value=route),
                patch(MODULE + "preflight", return_value={"ok": True, "clear_obstacles": []}) as check,
                patch(MODULE + "underground_proof", return_value=True),
            ):
                ready = c.prepare(obs)
            self.assertTrue(ready["ok"], ready)
            ignored = set(check.call_args_list[1].kwargs["ignored_units"])
            self.assertEqual(ignored, {e["unit_number"] for e in ready["pending_cutover"]})
            self.assertFalse(ignored & {e["unit_number"] for e in ready["pending_retirement"]})
            self.assertEqual(check.call_args_list[2].kwargs["ignored_units"], [])


class OreRerouteExecutionTests(unittest.TestCase):
    prepared = OreReroutePrepareTests.prepared

    def running(self, root):
        c, obs, docs, route, ready = self.prepared(root)
        self.assertTrue(ready["ok"], ready)
        c.state = ready
        return c, obs, docs, route

    def test_all_added_stock_is_paid_before_prebuild_or_cutover(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs, _, _ = self.running(root)
            c.game.query.return_value = {"stock": {}}
            result = c.step(obs)
            self.assertEqual(result["type"], "craft")
            c.builder.ensure_plan.assert_not_called()
            self.assertEqual(c.state["phase"], "stock")

    def test_preserved_local_escape_cannot_be_rebuilt_or_retargeted(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs, _, _ = self.running(root)
            obs["entities"][1]["unit_number"] = 999
            result = c.step(obs)
            self.assertEqual(result["reason"], "preserved_ore_source_join_or_smelting_unit_changed")
            c.builder.ensure_plan.assert_not_called()

    def test_started_recovery_prunes_completed_unit_and_never_mines_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs, _, _ = self.running(root)
            key = "pending_cutover"
            with patch(MODULE + "_atomic_json"):
                action = c._recover(obs, key)
                self.assertEqual(action["type"], "mine_many")
                self.assertLessEqual(len(action["actions"]), 8)
                victim = c.state[key][0]
                obs["entities"] = [e for e in obs["entities"] if e["unit_number"] != victim["unit_number"]]
                self.assertIsNone(c._recover(obs, key))
                self.assertEqual(c.state[key], [])
            c, obs, _, _ = self.running(root)
            victim = c.state[key][0]
            next(e for e in obs["entities"] if e["unit_number"] == victim["unit_number"])["unit_number"] = 999
            self.assertEqual(c._recover(obs, key)["reason"], "ore_recovery_will_not_retarget_new_unit")

    def test_publication_updates_all_four_source_copies_without_core_changes(self):
        with tempfile.TemporaryDirectory() as root:
            c, _, docs, _ = self.running(root)
            before = {name: deepcopy(docs[name].get("plan")) for name in ("factory", "green")}
            self.assertIsNone(c._publish())
            current = c._documents()
            self.assertTrue(all(plan_revision(p) == c.state["new_revision"] for p in c._plans(current).values()))
            self.assertEqual({name: current[name]["plan"] for name in ("factory", "green")}, before)
            self.assertIsNone(c._publish())

    def test_partial_publication_resumes_from_mixed_old_and_new_documents(self):
        with tempfile.TemporaryDirectory() as root:
            c, _, _, _ = self.running(root)
            from factorio_agent.core.deterministic_state import _atomic_json

            writes = []

            def interrupted(path, value):
                writes.append(path)
                if len(writes) == 2:
                    raise OSError("interrupted after first authoritative copy")
                _atomic_json(path, value)

            with patch(MODULE + "_atomic_json", side_effect=interrupted):
                with self.assertRaises(OSError):
                    c._publish()
            current = c._documents()
            self.assertEqual(plan_revision(current["smelting"]["plan"]), c.state["new_revision"])
            self.assertEqual(plan_revision(current["factory"]["source_plan"]), c.state["old_revision"])
            self.assertIsNone(c._publish())
            self.assertTrue(
                all(plan_revision(plan) == c.state["new_revision"] for plan in c._plans(c._documents()).values())
            )

    def test_new_units_are_checkpointed_before_next_build_action(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs, _, _ = self.running(root)
            c.state["phase"] = "prebuild"
            spec = c.state["added"][-1]
            obs["entities"].append(dict(spec, unit_number=999))
            c.builder.ensure_plan.return_value = {"type": "build", "name": "transport-belt"}
            c.step(obs)
            saved = json.loads(c.path.read_text())
            self.assertIn(999, [e["unit_number"] for e in saved["new_units"]])

    def proof_fixture(self, root):
        c, obs, docs, _ = self.running(root)
        c.state.update(published=True, phase="commission")
        obsolete = {e["unit_number"] for e in c.state["pending_cutover"]}
        obs["entities"] = [e for e in obs["entities"] if e["unit_number"] not in obsolete]
        actual = {(e["name"], e["position"]["x"], e["position"]["y"]) for e in obs["entities"]}
        for index, spec in enumerate(c.state["route"]["entities"]):
            if (spec["name"], *spec["position"].values()) not in actual:
                obs["entities"].append(dict(spec, unit_number=1000 + index, products_finished=0))
        c.state["baseline"] = {"tick": 99, "counts": c._furnace_counts(obs)}
        furnace = next(e for e in obs["entities"] if e["name"] == "stone-furnace")
        furnace["products_finished"] += 1
        c.game.query.return_value = {"ok": True, "tick": 101, "count": 4}
        return c, obs

    def test_retirement_needs_loaded_added_path_source_and_new_iron_production(self):
        with tempfile.TemporaryDirectory() as root:
            c, obs = self.proof_fixture(root)
            with patch(MODULE + "underground_proof", return_value=True):
                self.assertIsNotNone(c.proof(obs))
                c.game.query.return_value["count"] = 0
                self.assertIsNone(c.proof(obs))
                c.game.query.return_value["count"] = 4
                c.outposts.bank_proof.return_value = None
                self.assertIsNone(c.proof(obs))
            c, obs = self.proof_fixture(root)
            c.state["baseline"]["counts"] = c._furnace_counts(obs)
            with patch(MODULE + "underground_proof", return_value=True):
                self.assertIsNone(c.proof(obs))


if __name__ == "__main__":
    unittest.main()
