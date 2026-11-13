from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from factorio_agent.science_pipeline import SciencePipeline
import test_v2_bootstrap_research as fixtures
import test_v2_legacy_science_recovery as legacy


class SciencePipelineTests(unittest.TestCase):
    def setUp(self):
        f = fixtures.BootstrapResearchTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.f = f
        self.obs = f.obs
        self.owner = f.make("electric-mining-drill")
        f.fund(self.owner)
        self.owner.state.update(collected_packs=25, supplied_packs=25)
        self.owner._save()
        f.red()["products_finished"] += 25
        self.obs["production"]["automation-science-pack"]["consumed"] += 10
        self.obs["research"] = "electric-mining-drill"
        self.obs["technologies"].pop("logistics")
        self.obs["research_progress"] = 0.4
        self.f.bootstrap._take_output = Mock(
            side_effect=lambda obs, item, count: dict(
                type="take", item=item, count=count, reason="collect already produced public plates"
            )
        )
        self.ledger = dict(
            schema_version=2,
            world_id="w",
            jobs={
                "owner": dict(
                    key="owner",
                    world_id="w",
                    capability="bootstrap_research",
                    parameters={"technology": "electric-mining-drill"},
                    status="waiting",
                )
            },
        )
        self.queue = dict(
            schema_version=1,
            world_id="w",
            nodes=[
                dict(
                    id="power",
                    job_key="power",
                    capability="power",
                    parameters={},
                    status="completed",
                    priority=90,
                    depends_on=[],
                    paused=False,
                    created_at=0,
                ),
                dict(
                    id="electric",
                    job_key="owner",
                    capability="bootstrap_research",
                    parameters={"technology": "electric-mining-drill"},
                    status="running",
                    priority=80,
                    depends_on=["power"],
                    paused=False,
                    created_at=1,
                ),
                dict(
                    id="logistics",
                    job_key="next",
                    capability="bootstrap_logistics",
                    parameters={},
                    status="pending",
                    priority=70,
                    depends_on=["power", "electric"],
                    paused=False,
                    created_at=2,
                ),
            ],
        )
        self.flush()
        self.pipeline = self.make()
        self.old_owner = self.owner.path.read_bytes()

    def flush(self):
        (self.f.root / "jobs.json").write_text(json.dumps(self.ledger), encoding="utf-8")
        (self.f.root / "work_queue.json").write_text(json.dumps(self.queue), encoding="utf-8")

    def make(self):
        return SciencePipeline(self.f.game, self.f.bootstrap, self.f.builder, self.f.catalog)

    def next(self, **kwargs):
        return self.pipeline.next_action(self.obs, self.owner, **kwargs)

    def ack(self, action, moved=None):
        moved = action["count"] if moved is None else moved
        self.pipeline.record_action(action, dict(ok=True, moved=moved))
        if action["type"] == "insert":
            self.obs["inventory"][action["item"]] -= moved

    def test_owner_must_be_fully_funded_and_its_research_consuming(self):
        allocated = self.owner.state["allocated_inputs"]
        allocated["iron-plate"] -= 1
        self.assertIsNone(self.next())
        self.assertFalse(self.pipeline.path.exists())
        allocated["iron-plate"] += 1
        self.obs["production"]["automation-science-pack"]["consumed"] = self.owner.state["baseline_consumed"]
        self.assertIsNone(self.next())
        self.assertFalse((self.f.root / "bootstrap-logistics.json").exists())
        self.obs["production"]["automation-science-pack"]["consumed"] += 1
        self.obs["research"] = None
        self.assertIsNone(self.next())
        self.assertFalse((self.f.root / "bootstrap-logistics.json").exists())

    def test_back_to_back_funding_starts_while_the_owner_still_produces(self):
        # Funded and researching; no pack collected yet, the machine counter at the owner's baseline.
        self.owner.state.update(collected_packs=0, supplied_packs=0)
        self.owner._save()
        owner_bytes = self.owner.path.read_bytes()
        self.f.red()["products_finished"] -= 25
        self.obs["production"]["automation-science-pack"]["consumed"] -= 9
        action = self.next()
        self.assertEqual((action["type"], action["item"], action["count"]), ("insert", "copper-plate", 5))
        target = self.f.make("logistics")
        # Baselines are the owner's batch end; the observed counters are the rollback floors.
        self.assertEqual(target.state["baseline_products_finished"], 125)
        self.assertEqual(target.state["baseline_consumed"], self.owner.state["baseline_consumed"] + 25)
        self.assertEqual(target.state["prewarm"]["observed_products_finished"], 100)
        self.ack(action)
        plan = target._startup_plan(self.obs)
        self.assertIsNone(target._binding_error(self.obs, plan))
        self.f.red()["products_finished"] -= 1
        self.assertEqual(target._binding_error(self.obs, plan), "bootstrap_research_machine_counter_rollback")
        self.f.red()["products_finished"] += 1
        self.assertEqual(self.owner.path.read_bytes(), owner_bytes)

    def test_successor_rolls_on_and_reserves_the_next_batch_in_turn(self):
        for _ in range(8):
            self.ack(self.next())
        self.obs["technologies"]["electric-mining-drill"] = True
        self.obs["research"] = None
        target = self.f.make("logistics")
        self.assertEqual(target.step(self.obs)["technology"], "logistics")
        # Logistics is the owner now; its prewarmed batch was fully funded.
        self.obs["research"] = "logistics"
        self.obs["production"]["automation-science-pack"]["consumed"] += 26
        self.ledger["jobs"]["owner"]["status"] = "succeeded"
        self.ledger["jobs"]["next"] = dict(
            key="next", world_id="w", capability="bootstrap_logistics", parameters={}, status="running"
        )
        self.queue["nodes"][1]["status"] = "completed"
        self.queue["nodes"][2]["status"] = "running"
        self.queue["nodes"].append(
            dict(
                id="wall",
                job_key="wall",
                capability="bootstrap_research",
                parameters={"technology": "stone-wall"},
                status="pending",
                priority=60,
                depends_on=["logistics"],
                paused=False,
                created_at=3,
            )
        )
        self.flush()
        self.obs["inventory"] = {"iron-plate": 100, "copper-plate": 100}
        pipeline = self.make()
        action = pipeline.next_action(self.obs, target)
        self.assertEqual((action["item"], action["count"]), ("copper-plate", 5))
        self.assertEqual(pipeline.state["target"]["id"], "wall")
        wall = self.f.make("stone-wall")
        self.assertEqual(wall.state["prewarm"]["owner_technology"], "logistics")
        self.assertEqual(wall.state["baseline_products_finished"], target.state["baseline_products_finished"] + 20)

    def test_other_critical_claims_are_kept_but_the_construction_lookahead_is_not(self):
        self.obs["inventory"] = {"iron-plate": 50, "copper-plate": 25}
        for e in self.obs["entities"]:
            e["output_inventory"] = {}
        self.obs["known_project_construction_budget"] = {
            "base_material_upper_bound": {"iron-plate": 500, "copper-plate": 250},
            "unresolved_materials": [],
        }
        jobs = self.ledger["jobs"]
        for key in ("starter", "power"):
            jobs[key] = dict(key=key, world_id="w", capability=key, parameters={}, status="succeeded")
        outpost = dict(
            key="iron",
            world_id="w",
            capability="mining_outpost",
            parameters={"resource": "iron-ore", "rate_per_minute": 60},
            status="waiting",
            reason="waiting for plates",
            evidence={"needs": {"iron-plate": 45}},
        )
        jobs["iron"] = outpost
        self.flush()
        logistics = self.f.make("logistics")
        self.assertEqual(
            self.pipeline._surplus(self.obs, logistics, jobs, exclude=("owner", "next")),
            {"iron-plate": 5, "copper-plate": 25},
        )
        self.assertIsNone(self.next())
        outpost.update(evidence={}, reason="waiting for iron-plate")
        self.assertEqual(self.pipeline._surplus(self.obs, logistics, jobs, exclude=("owner",))["iron-plate"], 0)
        # A slack job's wait reserves nothing.
        outpost.update(capability="starter_capacity", parameters={"resource": "iron-ore"})
        self.assertEqual(self.pipeline._surplus(self.obs, logistics, jobs, exclude=("owner",))["iron-plate"], 50)
        del jobs["iron"]
        self.flush()
        action = self.next()
        self.assertEqual((action["type"], action["item"]), ("insert", "copper-plate"))

    def test_executor_fans_out_actual_receipt_and_reloads_future_owner_in_same_slice(self):
        from factorio_agent.jobs import StarterExecutors

        ex = StarterExecutors.__new__(StarterExecutors)
        ex.game, ex.bootstrap, ex.builder, ex.catalog = self.f.game, self.f.bootstrap, self.f.builder, self.f.catalog
        ex.industry = self.f.make("logistics")
        ex.bootstrap_research_targets = {"electric-mining-drill": self.owner}
        waiting = {
            "status": "waiting",
            "reason": "native research running",
            "evidence": {"autonomous_wait": True, "background_work_safe": True},
        }
        action = ex._science_wait_action(self.obs, self.owner, waiting)
        self.assertEqual(action["item"], "copper-plate")
        ex.record_action(
            "bootstrap_research",
            self.obs,
            action,
            {"ok": True, "moved": action["count"]},
            {"technology": "electric-mining-drill"},
        )
        self.assertEqual(ex.industry.state["allocated_inputs"], {"copper-plate": 5})
        self.assertNotIn("pending_transfer", ex.industry.state)
        self.assertEqual(self.owner.path.read_bytes(), self.old_owner)

    def test_executor_funds_the_next_batch_from_any_wait_of_a_funded_researching_owner(self):
        from factorio_agent.jobs import StarterExecutors

        ex = StarterExecutors.__new__(StarterExecutors)
        ex.game, ex.bootstrap, ex.builder, ex.catalog = self.f.game, self.f.bootstrap, self.f.builder, self.f.catalog
        ex.industry = self.f.make("logistics")
        self.owner.state.update(collected_packs=10, supplied_packs=10)
        waiting = {"status": "waiting", "reason": "waiting_for_finite_machine_science_and_native_research"}
        action = ex._science_wait_action(self.obs, self.owner, waiting)
        self.assertEqual((action["type"], action["item"]), ("insert", "copper-plate"))
        self.assertIs(ex.industry, ex.science_pipeline.target_controller)
        # Not researching the owner's technology: the wait stays a wait.
        self.obs["research"] = None
        self.assertIs(ex._science_wait_action(self.obs, self.owner, waiting), waiting)

    def test_balanced_prefill_reserves_new_baseline_before_copper_and_iron(self):
        action = self.next()
        self.assertEqual((action["type"], action["item"], action["count"]), ("insert", "copper-plate", 5))
        target = self.f.make("logistics")
        self.assertEqual(target.state["baseline_products_finished"], 125)
        self.assertEqual(target.state["allocated_inputs"], {})
        self.assertEqual(target.state["pending_transfer"]["action"], action)
        self.assertEqual(target.state["collected_packs"], 0)
        self.assertEqual(target.state["supplied_packs"], 0)
        self.ack(action)
        action = self.next()
        self.assertEqual((action["item"], action["count"]), ("iron-plate", 10))
        self.ack(action)
        self.assertEqual(self.owner.path.read_bytes(), self.old_owner)
        self.assertEqual(self.obs["research"], "electric-mining-drill")

    def test_existing_starter_research_delegation_is_the_same_approved_owner(self):
        self.ledger["jobs"]["owner"]["capability"] = "starter_research"
        self.queue["nodes"][1]["capability"] = "starter_research"
        self.flush()
        action = self.next()
        self.assertEqual((action["type"], action["item"]), ("insert", "copper-plate"))

    def test_full_one_batch_prefill_is_exact_paid_and_never_changes_lab_or_research(self):
        actions = []
        for _ in range(8):
            action = self.next()
            self.assertIsNotNone(action)
            actions.append(action)
            self.ack(action)
        self.assertEqual(sum(a["count"] for a in actions if a["item"] == "iron-plate"), 40)
        self.assertEqual(sum(a["count"] for a in actions if a["item"] == "copper-plate"), 20)
        self.assertTrue(all(a["type"] == "insert" and a["inventory"] == "chest" for a in actions))
        self.assertIsNone(self.next())
        target = self.f.make("logistics")
        self.assertEqual(target.state["allocated_inputs"], {"iron-plate": 40, "copper-plate": 20})
        self.assertEqual(self.pipeline.state["phase"], "funded")
        self.assertEqual(target.state["collected_packs"], 0)
        self.assertEqual(self.owner.path.read_bytes(), self.old_owner)

    def test_partial_actual_receipt_and_restart_pay_only_the_unfunded_remainder(self):
        action = self.next()
        self.ack(action, 2)
        self.pipeline = self.make()
        action = self.next()
        self.assertEqual((action["item"], action["count"]), ("iron-plate", 10))
        self.ack(action)
        action = self.next()
        self.assertEqual((action["item"], action["count"]), ("copper-plate", 3))
        self.ack(action)
        target = self.f.make("logistics")
        self.assertEqual(target.state["allocated_inputs"], {"iron-plate": 10, "copper-plate": 5})
        self.assertEqual(target.state["baseline_products_finished"], 125)

    def test_unknown_receipt_preserves_pending_and_blocks_future_prewarm(self):
        action = self.next()
        before = (self.f.root / "bootstrap-logistics.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.pipeline.record_action(action, dict(ok=True))
        self.assertEqual((self.f.root / "bootstrap-logistics.json").read_bytes(), before)
        self.pipeline = self.make()
        self.assertIsNone(self.next())
        self.assertEqual(self.pipeline.last_diagnostic["reason"], "science_prewarm_transfer_requires_reconciliation")

    def test_queue_pause_world_missing_dependencies_or_unknown_action_prevent_prefill(self):
        original = deepcopy(self.queue)
        for change in ("paused", "world", "dependency", "unknown"):
            self.queue = deepcopy(original)
            self.ledger["jobs"]["owner"].pop("pending_action", None)
            if change == "paused":
                self.queue["nodes"][-1]["paused"] = True
            elif change == "world":
                self.queue["world_id"] = "foreign"
            elif change == "dependency":
                self.queue["nodes"][0]["status"] = "pending"
            else:
                self.ledger["jobs"]["owner"]["pending_action"] = {}
            self.flush()
            self.assertIsNone(self.next())
            self.assertFalse((self.f.root / "bootstrap-logistics.json").exists())

    def test_reserved_target_does_not_switch_to_a_duplicate_or_automatically_resume_paused_target(self):
        action = self.next()
        self.ack(action)
        self.queue["nodes"][-1]["paused"] = True
        other = dict(self.queue["nodes"][-1], id="other", job_key="other", priority=100, paused=False)
        self.queue["nodes"].append(other)
        self.flush()
        self.pipeline = self.make()
        self.assertIsNone(self.next())
        self.assertEqual(self.pipeline.state["target"]["id"], "logistics")

    def test_highest_priority_red_only_successor_is_reserved_and_others_skipped(self):
        for node_id, technology, priority in (("wall", "stone-wall", 100), ("space", "space-science-pack", 200)):
            self.queue["nodes"].append(
                dict(
                    id=node_id,
                    job_key=node_id,
                    capability="bootstrap_research",
                    parameters={"technology": technology},
                    status="ready",
                    priority=priority,
                    depends_on=["power"],
                    paused=False,
                    created_at=3,
                )
            )
        self.flush()
        action = self.next()
        self.assertEqual(action["reason"], "fund balanced prewarmed machine science for stone-wall")
        self.assertEqual(self.pipeline.state["target"]["id"], "wall")
        self.ack(action)
        self.assertEqual(self.f.make("stone-wall").state["allocated_inputs"], {"copper-plate": 5})

    def test_shared_cell_cannot_redirect_a_reserved_packet_to_another_research(self):
        action = self.next()
        self.ack(action)
        self.obs["technologies"]["electric-mining-drill"] = True
        self.obs["research"] = None
        other = self.f.make("stone-wall")
        self.assertEqual(other.step(self.obs)["reason"], "science_cell_reserved_for_approved_prewarmed_logistics")
        self.assertFalse(other.path.exists())
        self.assertIsNotNone(self.owner.proof(self.obs))

    def test_new_native_recipe_unlock_and_exact_owner_machine_remain_required(self):
        self.obs["enabled_recipes"]["automation-science-pack"] = False
        self.assertIsNone(self.next())
        self.assertFalse((self.f.root / "bootstrap-logistics.json").exists())
        self.obs["enabled_recipes"]["automation-science-pack"] = True
        action = self.next()
        self.ack(action)
        self.f.red()["unit_number"] += 1
        self.assertIsNone(self.next())
        self.assertEqual(self.owner.path.read_bytes(), self.old_owner)

    def test_default_reserve_and_public_existing_output_never_call_mining_or_crafting(self):
        self.obs["inventory"] = {"iron-plate": 0, "copper-plate": 0}
        self.obs["entities"] += [
            dict(
                name="stone-furnace",
                unit_number=201,
                position={"x": 30.5, "y": 30.5},
                output_inventory={"iron-plate": 50},
            ),
            dict(
                name="stone-furnace",
                unit_number=202,
                position={"x": 35.5, "y": 30.5},
                output_inventory={"copper-plate": 25},
            ),
        ]
        action = self.next()
        self.assertEqual((action["type"], action["item"], action["count"]), ("take", "copper-plate", 5))
        self.f.bootstrap.ensure_item.assert_not_called()
        self.assertEqual(
            self.pipeline._surplus(self.obs, self.f.make("logistics")), {"iron-plate": 50, "copper-plate": 25}
        )

    def test_no_whole_packet_means_no_input_transfer(self):
        self.obs["inventory"] = {"iron-plate": 9, "copper-plate": 20}
        for e in self.obs["entities"]:
            e["output_inventory"] = {}
        self.assertIsNone(self.next())
        self.f.bootstrap._take_output.assert_not_called()
        target = self.f.make("logistics")
        self.assertEqual(target.state.get("allocated_inputs", {}), {})

    def test_current_native_completion_has_priority_and_normal_next_job_preserves_prefill(self):
        for _ in range(8):
            action = self.next()
            self.ack(action)
        self.obs["technologies"]["electric-mining-drill"] = True
        self.obs["research"] = None
        self.assertIsNotNone(self.owner.proof(self.obs))
        self.assertIsNone(self.next())
        target = self.f.make("logistics")
        baseline = target.state["baseline_products_finished"]
        self.assertEqual(target.step(self.obs)["technology"], "logistics")
        self.obs["research"] = "logistics"
        self.f.red()["products_finished"] += 20
        self.obs["technologies"]["logistics"] = True
        self.obs["production"]["automation-science-pack"]["consumed"] += 20
        self.assertEqual(target.proof(self.obs)["normally_assembled_packs"], 20)
        self.assertEqual(target.state["baseline_products_finished"], baseline)

    def test_normal_partial_prefill_still_balances_copper_before_waiting_for_all_iron(self):
        action = self.next()
        self.ack(action)
        action = self.next()
        self.ack(action)
        self.obs["technologies"]["electric-mining-drill"] = True
        self.obs["research"] = "logistics"
        target = self.f.make("logistics")
        action = target.step(self.obs)
        self.assertEqual((action["type"], action["item"], action["count"]), ("insert", "copper-plate", 5))

    def test_autonomous_wait_flags_require_full_paid_supplied_batch_and_actual_research(self):
        evidence = self.owner._progress(self.obs)
        self.assertTrue(evidence["autonomous_wait"])
        self.assertTrue(evidence["background_work_safe"])
        for change in ("partial", "pending", "power", "finished", "other_research"):
            state = deepcopy(self.owner.state)
            obs = deepcopy(self.obs)
            if change == "partial":
                self.owner.state["supplied_packs"] = 24
            elif change == "pending":
                self.owner.state["pending_transfer"] = {}
            elif change == "power":
                self.f.red()["energy"] = 0
            elif change == "finished":
                self.obs["technologies"]["electric-mining-drill"] = True
            else:
                self.obs["research"] = "logistics"
            self.assertFalse(self.owner._progress(self.obs)["background_work_safe"], change)
            self.owner.state = state
            self.obs.clear()
            self.obs.update(obs)

    def test_legacy_repurchase_wait_flags_are_pure_and_never_recurse(self):
        f = legacy.LegacyScienceRecoveryTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        action = f.activate()
        f.c.record_action(action, dict(ok=True, moved=40))
        epoch = f.c.state["legacy_repurchase"]
        epoch.update(collected_packs=20, supplied_packs=20)
        f.f.red()["products_finished"] += 20
        f.obs["production"]["automation-science-pack"]["consumed"] += 10
        before = f.c.path.read_bytes()
        evidence = f.c._legacy_repurchase_progress(f.obs)
        self.assertTrue(evidence["autonomous_wait"])
        self.assertTrue(evidence["background_work_safe"])
        self.assertEqual(f.c.path.read_bytes(), before)
        epoch["supplied_packs"] = 19
        self.assertFalse(f.c._legacy_repurchase_progress(f.obs)["autonomous_wait"])


if __name__ == "__main__":
    unittest.main()
