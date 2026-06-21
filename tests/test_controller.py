import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from factorio_ai.config import AppConfig
from factorio_ai.controller import (
    FactorioController,
    ModlessFactorioController,
    RunSummary,
    StrategyStepSummary,
    _guard_post_automation_handcraft,
    _move_detour_action,
    _stale_take_response,
    _virtual_move_response_arrived,
)
from factorio_ai.llm_log import llm_decision_log_path, llm_io_trace_log_path, make_llm_io_trace
from factorio_ai.models import PlannerDecision
from factorio_ai.site_selection import save_selected_improvement_site


def make_test_config(root: Path) -> AppConfig:
    return AppConfig(
        factorio_exe=Path("factorio.exe"),
        runtime_dir=root / "runtime",
        mod_runtime_dir=root / "runtime" / "mods",
        save_path=root / "runtime" / "saves" / "test.zip",
        rcon_host="127.0.0.1",
        rcon_port=27015,
        rcon_password="factorio-ai",
        server_port=34197,
        log_dir=root / "logs",
        agent_player_name="AI",
        slurm_enabled=False,
    )


class ControllerTests(unittest.TestCase):
    def test_stale_take_response_detects_missing_item_race(self):
        self.assertTrue(
            _stale_take_response(
                {"type": "take", "item": "iron-gear-wheel"},
                {"ok": False, "reason": "target does not have item"},
            )
        )
        self.assertFalse(
            _stale_take_response(
                {"type": "insert", "item": "iron-gear-wheel"},
                {"ok": False, "reason": "target does not have item"},
            )
        )

    def test_virtual_move_response_arrived_detects_server_agent_move(self):
        self.assertTrue(
            _virtual_move_response_arrived(
                {
                    "ok": True,
                    "action": "move_to",
                    "status": "arrived",
                    "execution": {"mode": "virtual", "virtual": True},
                }
            )
        )
        self.assertFalse(
            _virtual_move_response_arrived(
                {
                    "ok": True,
                    "action": "move_to",
                    "status": "arrived",
                    "execution": {"mode": "player", "virtual": False},
                }
            )
        )

    def test_guard_blocks_gear_handcraft_after_automation(self):
        observation = {
            "research": {
                "technologies": {
                    "automation": {"researched": True},
                }
            }
        }
        decision = PlannerDecision(
            action={"type": "craft", "recipe": "iron-gear-wheel", "count": 1},
            reason="test",
        )

        guarded = _guard_post_automation_handcraft(observation, decision)

        self.assertEqual(guarded.action, {"type": "wait", "ticks": 120})
        self.assertIn("blocked direct iron-gear-wheel handcraft", guarded.reason)

    def test_guard_allows_gear_handcraft_for_virtual_server_agent(self):
        # The virtual RCON server agent (character_valid=False / kind server) teleports and hand-craft
        # is instant/free, so the anti-handcraft guard must NOT block it -- blocking deadlocked the
        # autopilot in a 487-step wait-loop (2026-06-19). Real walking players keep the guard.
        observation = {
            "research": {"technologies": {"automation": {"researched": True}}},
            "player": {"character_valid": False, "kind": "server"},
        }
        decision = PlannerDecision(
            action={"type": "craft", "recipe": "iron-gear-wheel", "count": 1},
            reason="test",
        )
        self.assertIs(_guard_post_automation_handcraft(observation, decision), decision)

    def test_guard_allows_bootstrap_gear_handcraft_before_automation(self):
        observation = {
            "research": {
                "technologies": {
                    "automation": {"researched": False},
                }
            }
        }
        decision = PlannerDecision(
            action={"type": "craft", "recipe": "iron-gear-wheel", "count": 1},
            reason="test",
        )

        self.assertIs(_guard_post_automation_handcraft(observation, decision), decision)

    def test_guard_blocks_gear_handcraft_when_assembler_exists_even_if_research_missing(self):
        observation = {
            "research": {"technologies": {"automation": {"researched": False}}},
            "inventory": {},
            "entities": [{"name": "assembling-machine-1", "position": {"x": 0, "y": 0}}],
        }
        decision = PlannerDecision(
            action={"type": "craft", "recipe": "iron-gear-wheel", "count": 1},
            reason="test",
        )

        guarded = _guard_post_automation_handcraft(observation, decision)

        self.assertEqual(guarded.action, {"type": "wait", "ticks": 120})
        self.assertIn("blocked direct iron-gear-wheel handcraft", guarded.reason)

    def test_guard_blocks_gear_handcraft_when_assembler_is_available_in_inventory(self):
        observation = {
            "research": {"technologies": {"automation": {"researched": False}}},
            "inventory": {"assembling-machine-1": 1},
            "entities": [],
        }
        decision = PlannerDecision(
            action={"type": "craft", "recipe": "iron-gear-wheel", "count": 1},
            reason="test",
        )

        guarded = _guard_post_automation_handcraft(observation, decision)

        self.assertEqual(guarded.action, {"type": "wait", "ticks": 120})
        self.assertIn("blocked direct iron-gear-wheel handcraft", guarded.reason)

    def test_guard_allows_one_time_first_assembler_bootstrap_gears_after_automation(self):
        observation = {
            "research": {"technologies": {"automation": {"researched": True}}},
            "inventory": {"electronic-circuit": 3, "iron-plate": 19},
            "entities": [],
        }
        decision = PlannerDecision(
            action={
                "type": "craft",
                "recipe": "iron-gear-wheel",
                "count": 5,
                "allow_first_assembler_bootstrap": True,
            },
            reason="test",
        )

        self.assertIs(_guard_post_automation_handcraft(observation, decision), decision)

    def test_guard_blocks_first_assembler_bootstrap_flag_when_assembler_exists(self):
        observation = {
            "research": {"technologies": {"automation": {"researched": True}}},
            "inventory": {"electronic-circuit": 3, "iron-plate": 19},
            "entities": [{"name": "assembling-machine-1", "position": {"x": 0, "y": 0}}],
        }
        decision = PlannerDecision(
            action={
                "type": "craft",
                "recipe": "iron-gear-wheel",
                "count": 5,
                "allow_first_assembler_bootstrap": True,
            },
            reason="test",
        )

        guarded = _guard_post_automation_handcraft(observation, decision)

        self.assertEqual(guarded.action, {"type": "wait", "ticks": 120})
        self.assertIn("blocked direct iron-gear-wheel handcraft", guarded.reason)

    def test_guard_allows_one_direct_transfer_bootstrap_gear_for_spaced_gear_belt_mall(self):
        observation = {
            "research": {"technologies": {"automation": {"researched": True}}},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "recipe": "iron-gear-wheel",
                    "position": {"x": 2.0, "y": 2.0},
                    "electric_network_connected": True,
                },
                {
                    "name": "assembling-machine-1",
                    "recipe": "transport-belt",
                    "position": {"x": 6.0, "y": 2.0},
                    "electric_network_connected": True,
                },
            ],
        }
        decision = PlannerDecision(
            action={
                "type": "craft",
                "recipe": "iron-gear-wheel",
                "count": 1,
                "allow_gear_belt_direct_transfer_bootstrap": True,
            },
            reason="test",
        )

        self.assertIs(_guard_post_automation_handcraft(observation, decision), decision)

    def test_guard_blocks_direct_transfer_bootstrap_flag_without_spaced_mall_pair(self):
        observation = {
            "research": {"technologies": {"automation": {"researched": True}}},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "recipe": "iron-gear-wheel",
                    "position": {"x": 2.0, "y": 2.0},
                    "electric_network_connected": True,
                },
            ],
        }
        decision = PlannerDecision(
            action={
                "type": "craft",
                "recipe": "iron-gear-wheel",
                "count": 1,
                "allow_gear_belt_direct_transfer_bootstrap": True,
            },
            reason="test",
        )

        guarded = _guard_post_automation_handcraft(observation, decision)

        self.assertEqual(guarded.action, {"type": "wait", "ticks": 120})
        self.assertIn("blocked direct iron-gear-wheel handcraft", guarded.reason)

    def test_no_mod_action_blocks_direct_gear_handcraft_after_automation(self):
        class FakeController(ModlessFactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "tick": 1,
                    "inventory": {},
                    "entities": [],
                    "research": {"technologies": {"automation": {"researched": True}}},
                    "player": {"name": "AI"},
                    "execution": {"mode": "player"},
                    "enemies": [],
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            response = controller.act({"type": "craft", "recipe": "iron-gear-wheel", "count": 1})

        self.assertFalse(response["ok"])
        self.assertIn("blocked direct iron-gear-wheel handcraft", response["reason"])

    def test_run_skill_skips_wait_for_virtual_arrived_move(self):
        observation = {
            "tick": 1,
            "inventory": {},
            "entities": [],
            "research": {"technologies": {}},
            "player": {"kind": "server", "character_valid": False, "position": {"x": 0, "y": 0}},
            "execution": {"mode": "virtual", "virtual": True},
        }

        class MoveThenDoneSkill:
            def __init__(self):
                self.calls = 0

            def next_action(self, _observation):
                self.calls += 1
                if self.calls == 1:
                    return PlannerDecision(
                        {"type": "move_to", "position": {"x": 100, "y": 0}},
                        "virtual move to remote buffer",
                    )
                return PlannerDecision(None, "done after virtual move", done=True)

        class FakeController(FactorioController):
            def observe(self):
                return observation

            def act(self, action):
                self.last_action = action
                return {
                    "ok": True,
                    "action": "move_to",
                    "status": "arrived",
                    "execution": {"mode": "virtual", "virtual": True},
                }

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_test_config(Path(tmp))
            controller = FakeController(cfg)
            with patch.object(controller, "_wait_for_move", side_effect=AssertionError("wait should be skipped")):
                run = controller._run_skill(
                    MoveThenDoneSkill(),
                    target_item="transport-belt",
                    target=1,
                    goal="test_virtual_move",
                    max_steps=2,
                    log_prefix="test-virtual-move",
                )

        self.assertTrue(run.ok)
        self.assertEqual(run.steps, 2)
        self.assertEqual(controller.last_action["type"], "move_to")

    def test_run_skill_stops_repeated_bootstrap_seed_without_followup(self):
        observation = {
            "tick": 1,
            "inventory": {"iron-plate": 4},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 10,
                    "recipe": "iron-gear-wheel",
                    "position": {"x": 0, "y": 0},
                    "electric_network_connected": True,
                }
            ],
            "research": {"technologies": {"automation": {"researched": True}}},
        }

        class RepeatingSeedSkill:
            def next_action(self, _observation):
                return PlannerDecision(
                    {
                        "type": "insert",
                        "item": "iron-plate",
                        "count": 1,
                        "unit_number": 10,
                        "name": "assembling-machine-1",
                        "position": {"x": 0, "y": 0},
                        "bootstrap_seed": True,
                        "seed_reason": "test_seed",
                        "expected_followup": "test output increases",
                    },
                    "seed once",
                    metadata={
                        "bootstrap_seed": True,
                        "seed_reason": "test_seed",
                        "expected_followup": "test output increases",
                    },
                )

        class FakeController(FactorioController):
            def observe(self):
                return observation

            def act(self, _action):
                return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_test_config(Path(tmp))
            controller = FakeController(cfg)
            run = controller._run_skill(
                RepeatingSeedSkill(),
                target_item="transport-belt",
                target=1,
                goal="test_seed",
                max_steps=3,
                log_prefix="test-seed",
            )

        self.assertFalse(run.ok)
        self.assertEqual(run.seed_count, 1)
        self.assertIn("bootstrap seed already attempted", run.reason)

    def test_run_skill_allows_bootstrap_seed_topoff_with_different_count(self):
        observation = {
            "tick": 1,
            "inventory": {"iron-plate": 4},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 10,
                    "recipe": "iron-gear-wheel",
                    "position": {"x": 0, "y": 0},
                    "electric_network_connected": True,
                }
            ],
            "research": {"technologies": {"automation": {"researched": True}}},
        }

        class TopoffSeedSkill:
            def __init__(self):
                self.counts = [1, 2, 2]

            def next_action(self, _observation):
                count = self.counts.pop(0)
                return PlannerDecision(
                    {
                        "type": "insert",
                        "item": "iron-plate",
                        "count": count,
                        "unit_number": 10,
                        "name": "assembling-machine-1",
                        "position": {"x": 0, "y": 0},
                        "bootstrap_seed": True,
                        "seed_reason": "test_seed",
                        "expected_followup": "test output increases",
                    },
                    "seed topoff",
                    metadata={
                        "bootstrap_seed": True,
                        "seed_reason": "test_seed",
                        "expected_followup": "test output increases",
                    },
                )

        class FakeController(FactorioController):
            def observe(self):
                return observation

            def act(self, _action):
                return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_test_config(Path(tmp))
            controller = FakeController(cfg)
            run = controller._run_skill(
                TopoffSeedSkill(),
                target_item="transport-belt",
                target=1,
                goal="test_seed",
                max_steps=4,
                log_prefix="test-seed-topoff",
            )

        self.assertFalse(run.ok)
        self.assertEqual(run.seed_count, 2)
        self.assertIn("bootstrap seed already attempted", run.reason)

    def test_run_skill_allows_repeated_bootstrap_seed_after_followup_item_increases(self):
        observations = [
            {
                "tick": 1,
                "inventory": {"iron-plate": 5},
                "entities": [
                    {
                        "name": "assembling-machine-1",
                        "unit_number": 10,
                        "recipe": "iron-gear-wheel",
                        "position": {"x": 0, "y": 0},
                        "electric_network_connected": True,
                    }
                ],
                "research": {"technologies": {"automation": {"researched": True}}},
            },
            {
                "tick": 2,
                "inventory": {"iron-plate": 4, "iron-gear-wheel": 1},
                "entities": [
                    {
                        "name": "assembling-machine-1",
                        "unit_number": 10,
                        "recipe": "iron-gear-wheel",
                        "position": {"x": 0, "y": 0},
                        "electric_network_connected": True,
                    }
                ],
                "research": {"technologies": {"automation": {"researched": True}}},
            },
            {
                "tick": 3,
                "inventory": {"iron-gear-wheel": 2},
                "entities": [],
                "research": {"technologies": {"automation": {"researched": True}}},
                "done": True,
            },
        ]

        class RepeatingSeedSkill:
            def next_action(self, observation):
                if observation.get("done"):
                    return PlannerDecision(None, "follow-up observed", done=True)
                return PlannerDecision(
                    {
                        "type": "insert",
                        "item": "iron-plate",
                        "count": 1,
                        "unit_number": 10,
                        "name": "assembling-machine-1",
                        "position": {"x": 0, "y": 0},
                        "bootstrap_seed": True,
                        "seed_reason": "assembler_produced_bootstrap_gear_iron_seed",
                        "expected_followup": "gear assembler produces iron-gear-wheel for bootstrap",
                    },
                    "seed once",
                    metadata={
                        "bootstrap_seed": True,
                        "seed_reason": "assembler_produced_bootstrap_gear_iron_seed",
                        "expected_followup": "gear assembler produces iron-gear-wheel for bootstrap",
                    },
                )

        class FakeController(FactorioController):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.observe_calls = 0

            def observe(self):
                item = observations[min(self.observe_calls, len(observations) - 1)]
                self.observe_calls += 1
                return item

            def act(self, _action):
                return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_test_config(Path(tmp))
            controller = FakeController(cfg)
            run = controller._run_skill(
                RepeatingSeedSkill(),
                target_item="transport-belt",
                target=1,
                goal="test_seed",
                max_steps=3,
                log_prefix="test-seed",
            )

        self.assertTrue(run.ok)
        self.assertEqual(run.seed_count, 2)
        self.assertEqual(run.reason, "follow-up observed")

    def test_strategy_decision_records_and_strips_llm_io_traces(self):
        class FakeController(FactorioController):
            def observe(self):
                return {"ok": True, "tick": 1, "inventory": {}, "entities": [], "enemies": [], "research": {}}

        trace = make_llm_io_trace(
            trace_id="trace-strategy",
            kind="strategy",
            provider="local_llm",
            model="Qwen/Qwen3.5-9B",
            base_url="http://127.0.0.1:8000/v1",
            system_prompt="system prompt",
            input_prompt="full input prompt",
            raw_output='{"selected_skill":"research_automation"}',
            parsed_json={"selected_skill": "research_automation"},
            ok=True,
        )
        llm_result = {
            "selected_skill": "research_automation",
            "priority": 90,
            "reason": "LLM selected automation research",
            "evidence": [],
            "blockers": [],
            "expected_effect": "unlock assemblers",
            "source": "llm",
            "ok": True,
            "llm_traces": [trace],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_test_config(Path(temp_dir))
            controller = FakeController(cfg)
            with patch("factorio_ai.slurm_worker.run_strategy_request", return_value=llm_result):
                result = controller.strategy_decision("launch_rocket_program")

            rows = [
                json.loads(line)
                for line in llm_io_trace_log_path(cfg.log_dir).read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(result["source"], "llm")
        self.assertNotIn("llm_traces", result)
        self.assertNotIn("llm_trace", result)
        self.assertEqual(result["llm_trace_ids"], ["trace-strategy"])
        self.assertEqual(rows[0]["input_prompt"], "full input prompt")
        self.assertEqual(rows[0]["raw_output"], '{"selected_skill":"research_automation"}')

    def test_required_remote_llm_pending_does_not_submit_strategy_task(self):
        class FakeController(FactorioController):
            def observe(self):
                return {"ok": True, "tick": 1, "inventory": {}, "entities": [], "enemies": [], "research": {}}

        pending_status = {
            "ok": True,
            "llm_ready": False,
            "missing": ["Slurm worker job pending GPU allocation", "GPU allocation"],
            "remote": {
                "pending_jobs": [
                    {
                        "id": "677406",
                        "state": "PENDING",
                        "reason": "(Priority)",
                        "start_time": "2026-06-19T11:30:00",
                    }
                ]
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = FakeController(cfg)
            with (
                patch("factorio_ai.remote_slurm.llm_status", return_value=pending_status) as status,
                patch("factorio_ai.remote_slurm.request_strategy") as request_strategy,
            ):
                with self.assertRaisesRegex(RuntimeError, "remote Slurm LLM not ready"):
                    controller.strategy_decision("launch_rocket_program", require_llm=True)

            log_path = llm_decision_log_path(cfg.log_dir)
            rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        status.assert_called_once()
        request_strategy.assert_not_called()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "remote_slurm")
        self.assertIn("Slurm worker job pending GPU allocation", rows[0]["error"])
        self.assertIn("677406", rows[0]["error"])

    def test_non_required_strategy_skips_remote_even_when_slurm_enabled(self):
        class FakeController(FactorioController):
            def observe(self):
                return {"ok": True, "tick": 1, "inventory": {}, "entities": [], "enemies": [], "research": {}}

        heuristic_result = {
            "selected_skill": "setup_power",
            "priority": 90,
            "reason": "fallback can keep moving",
            "evidence": [],
            "blockers": [],
            "expected_effect": "restore power",
            "source": "heuristic",
            "ok": True,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = FakeController(cfg)
            with (
                patch("factorio_ai.remote_slurm.llm_status") as status,
                patch("factorio_ai.remote_slurm.request_strategy") as request_strategy,
                patch("factorio_ai.slurm_worker.run_strategy_request", return_value=heuristic_result) as local_strategy,
            ):
                result = controller.strategy_decision("launch_rocket_program", require_llm=False)

        status.assert_not_called()
        request_strategy.assert_not_called()
        local_strategy.assert_called_once()
        self.assertIn("selected_skill", result)

    def test_forced_heuristic_strategy_skips_remote_and_local_llm(self):
        class FakeController(FactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "tick": 1,
                    "inventory": {},
                    "entities": [],
                    "enemies": [],
                    "research": {"technologies": {"automation": {"researched": False}}},
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = FakeController(cfg)
            with (
                patch.dict("os.environ", {"FACTORIO_AI_FORCE_HEURISTIC_STRATEGY": "1"}),
                patch("factorio_ai.remote_slurm.llm_status") as status,
                patch("factorio_ai.remote_slurm.request_strategy") as request_strategy,
                patch("factorio_ai.slurm_worker.run_strategy_request") as local_strategy,
            ):
                result = controller.strategy_decision("launch_rocket_program", require_llm=False)

            rows = [
                json.loads(line)
                for line in llm_decision_log_path(cfg.log_dir).read_text(encoding="utf-8").splitlines()
            ]

        status.assert_not_called()
        request_strategy.assert_not_called()
        local_strategy.assert_not_called()
        self.assertEqual(result["source"], "heuristic")
        heuristic_rows = [row for row in rows if row["provider"] == "heuristic_fallback"]
        self.assertTrue(heuristic_rows)
        self.assertIn("forced heuristic strategy", heuristic_rows[-1]["error"])

    def test_required_llm_auto_uses_ready_remote_slurm_when_local_env_missing(self):
        class FakeController(FactorioController):
            def observe(self):
                return {"ok": True, "tick": 1, "inventory": {}, "entities": [], "enemies": [], "research": {}}

        remote_result = {
            "selected_skill": "research_automation",
            "priority": 90,
            "reason": "remote qwen selected automation",
            "evidence": ["remote_slurm_auto=true"],
            "blockers": [],
            "expected_effect": "bootstrap automation research",
            "source": "llm",
            "ok": True,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_test_config(Path(temp_dir))
            controller = FakeController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_LLM_BASE_URL": "",
                        "FACTORIO_AI_LLM_MODEL": "",
                        "FACTORIO_AI_REQUIRE_LLM_AUTO_SLURM": "1",
                    },
                ),
                patch("factorio_ai.remote_slurm.llm_status", return_value={"ok": True, "llm_ready": True}),
                patch("factorio_ai.remote_slurm.request_strategy", return_value=remote_result) as request_strategy,
            ):
                result = controller.strategy_decision("launch_rocket_program", require_llm=True)

            log_path = llm_decision_log_path(cfg.log_dir)
            rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

        request_strategy.assert_called_once()
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["selected_skill"], "research_automation")
        self.assertEqual(rows[0]["provider"], "remote_slurm")
        self.assertEqual(rows[0]["source"], "llm")
        self.assertTrue(rows[0]["ok"])

    def test_remote_strategy_timeout_is_configurable(self):
        class FakeController(FactorioController):
            def observe(self):
                return {"ok": True, "tick": 1, "inventory": {}, "entities": [], "enemies": [], "research": {}}

        remote_result = {
            "selected_skill": "plan_factory_site",
            "priority": 80,
            "reason": "remote qwen selected layout",
            "evidence": [],
            "blockers": [],
            "expected_effect": "diagnose layout",
            "source": "llm",
            "ok": True,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_test_config(Path(temp_dir))
            controller = FakeController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_LLM_BASE_URL": "",
                        "FACTORIO_AI_LLM_MODEL": "",
                        "FACTORIO_AI_REQUIRE_LLM_AUTO_SLURM": "1",
                        "FACTORIO_AI_REMOTE_STRATEGY_TIMEOUT_SECONDS": "123",
                    },
                ),
                patch("factorio_ai.remote_slurm.llm_status", return_value={"ok": True, "llm_ready": True}),
                patch("factorio_ai.remote_slurm.request_strategy", return_value=remote_result) as request_strategy,
            ):
                controller.strategy_decision("launch_rocket_program", require_llm=True)

        self.assertEqual(request_strategy.call_args.kwargs["timeout_seconds"], 123)

    def test_required_llm_auto_slurm_can_be_disabled_for_local_only(self):
        class FakeController(FactorioController):
            def observe(self):
                return {"ok": True, "tick": 1, "inventory": {}, "entities": [], "enemies": [], "research": {}}

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_LLM_BASE_URL": "",
                        "FACTORIO_AI_LLM_MODEL": "",
                        "FACTORIO_AI_REQUIRE_LLM_AUTO_SLURM": "0",
                    },
                ),
                patch("factorio_ai.remote_slurm.llm_status") as status,
                patch("factorio_ai.remote_slurm.request_strategy") as request_strategy,
            ):
                with self.assertRaisesRegex(RuntimeError, "LLM strategy was required"):
                    controller.strategy_decision("launch_rocket_program", require_llm=True)

        status.assert_not_called()
        request_strategy.assert_not_called()

    def test_remote_action_hint_skips_queue_when_llm_pending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = FactorioController(cfg)
            decision = PlannerDecision(
                action={"type": "wait", "ticks": 60},
                reason="test",
                done=False,
            )
            with (
                patch.dict("os.environ", {"FACTORIO_AI_REMOTE_ACTION_HINT_ENABLED": "1"}),
                patch(
                    "factorio_ai.remote_slurm.llm_status",
                    return_value={"ok": True, "llm_ready": False, "missing": ["GPU allocation"], "remote": {}},
                ) as status,
                patch("factorio_ai.remote_slurm.request_plan") as request_plan,
            ):
                action = controller._maybe_apply_remote_hint({}, decision, "produce_iron_plate")

        status.assert_called_once()
        request_plan.assert_not_called()
        self.assertEqual(action, {"type": "wait", "ticks": 60})

    def test_remote_action_hint_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = FactorioController(cfg)
            decision = PlannerDecision(
                action={"type": "craft", "recipe": "iron-gear-wheel", "count": 1},
                reason="test",
                done=False,
            )
            with (
                patch.dict("os.environ", {}, clear=True),
                patch("factorio_ai.remote_slurm.llm_status") as status,
                patch("factorio_ai.remote_slurm.request_plan") as request_plan,
            ):
                action = controller._maybe_apply_remote_hint({}, decision, "produce_iron_plate")

        status.assert_not_called()
        request_plan.assert_not_called()
        self.assertEqual(action, {"type": "craft", "recipe": "iron-gear-wheel", "count": 1})

    def test_slurm_auto_renewal_state_throttles_checks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = FactorioController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_SLURM_RENEW_BEFORE_MINUTES": "360",
                        "FACTORIO_AI_SLURM_RENEW_CHECK_INTERVAL_SECONDS": "3600",
                    },
                ),
                patch(
                    "factorio_ai.remote_slurm.ensure_worker_job",
                    return_value={"ok": True, "action": "pending_successor_exists"},
                ) as ensure_worker,
            ):
                controller._maybe_ensure_slurm_worker(reason="first", force=True)
                controller._maybe_ensure_slurm_worker(reason="second")

            state = json.loads((cfg.runtime_dir / "slurm-renewal.json").read_text(encoding="utf-8"))
            rows = [
                json.loads(line)
                for line in (cfg.log_dir / "slurm-renewal.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        ensure_worker.assert_called_once_with(renew_before_minutes=360)
        self.assertEqual(state["action"], "pending_successor_exists")
        self.assertEqual(state["reason"], "first")
        self.assertEqual(len(rows), 1)

    def test_autopilot_loop_ensures_slurm_worker_before_strategy_cycle(self):
        class FakeController(FactorioController):
            def run_strategy_step(self, *args, **kwargs):
                return StrategyStepSummary(
                    ok=True,
                    reason="test cycle complete",
                    objective="launch_rocket_program",
                    selected_skill="produce_iron_plate",
                    strategy={"source": "llm"},
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = FakeController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_SLURM_RENEW_BEFORE_MINUTES": "360",
                        "FACTORIO_AI_SLURM_RENEW_CHECK_INTERVAL_SECONDS": "3600",
                    },
                ),
                patch(
                    "factorio_ai.remote_slurm.ensure_worker_job",
                    return_value={"ok": True, "action": "renewal_not_needed"},
                ) as ensure_worker,
            ):
                summary = controller.run_autopilot_loop(cycles=1, sleep_seconds=0)

            state = json.loads((cfg.runtime_dir / "slurm-renewal.json").read_text(encoding="utf-8"))

        self.assertTrue(summary.ok)
        ensure_worker.assert_called_once_with(renew_before_minutes=360)
        self.assertEqual(state["reason"], "autopilot_start")
        self.assertEqual(state["action"], "renewal_not_needed")

    def test_background_layout_work_submits_simulation_task_during_active_skill(self):
        observation = {
            "tick": 1,
            "inventory": {},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 10,
                    "recipe": "electronic-circuit",
                    "position": {"x": 0, "y": 0},
                    "electric_network_connected": True,
                    "inventories": {},
                }
            ],
            "resources": [],
            "research": {"technologies": {}},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = FactorioController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS": "0",
                        "FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "queue",
                    },
                ),
                patch("factorio_ai.remote_slurm.submit_task", return_value="layout-task.json") as submit_task,
            ):
                controller._maybe_progress_background_layout_work(
                    observation,
                    "launch_rocket_program",
                    "bootstrap_build_item_mall",
                    4,
                )
            log_path = cfg.log_dir / "layout-improvement-background.jsonl"
            log_text = log_path.read_text(encoding="utf-8")

        submit_task.assert_called_once()
        submitted = submit_task.call_args.args[0]
        self.assertEqual(submitted["type"], "layout_improvement_request")
        self.assertEqual(submitted["payload"]["active_skill"], "bootstrap_build_item_mall")
        self.assertTrue(submitted["payload"]["layout_learning"]["return_learned_skills"])
        self.assertTrue(submitted["payload"]["layout_learning"]["record_only_confirmed"])
        self.assertIn("layout_task_submitted", log_text)

    def test_scheduler_background_layout_waits_when_gpu_not_ready(self):
        observation = {
            "tick": 1,
            "inventory": {},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 10,
                    "recipe": "electronic-circuit",
                    "position": {"x": 0, "y": 0},
                    "electric_network_connected": True,
                    "inventories": {},
                }
            ],
            "resources": [],
            "research": {"technologies": {}},
        }
        status = {
            "llm_ready": False,
            "missing": ["ready scheduler GPU allocation"],
            "remote": {
                "scheduler_ready_free_gpus": 0,
                "pending_gpu_tasks": 2,
                "pending_gpu_allocations": [{"id": 9, "state": "pending"}],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = FactorioController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS": "0",
                        "FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "scheduler",
                    },
                ),
                patch.object(controller, "_maybe_ensure_slurm_worker"),
                patch("factorio_ai.remote_slurm.layout_improvement_status", return_value=status),
                patch("factorio_ai.remote_slurm.request_layout_improvement") as request_layout,
            ):
                controller._maybe_progress_background_layout_work(
                    observation,
                    "launch_rocket_program",
                    "bootstrap_build_item_mall",
                    4,
                )
            log_path = cfg.log_dir / "layout-improvement-background.jsonl"
            log_text = log_path.read_text(encoding="utf-8")

        request_layout.assert_not_called()
        self.assertIsNone(controller._background_layout_thread)
        self.assertIn("layout_scheduler_waiting_for_ready_gpu", log_text)
        self.assertIn("ready scheduler GPU allocation", log_text)

    def test_scheduler_background_layout_starts_workers_until_configured_limit(self):
        observation = {
            "tick": 1,
            "inventory": {},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 10,
                    "recipe": "electronic-circuit",
                    "position": {"x": 0, "y": 0},
                    "electric_network_connected": True,
                    "inventories": {},
                }
            ],
            "resources": [],
            "research": {"technologies": {}},
        }
        status = {
            "llm_ready": True,
            "missing": [],
            "remote": {
                "active_layout_tasks": 0,
                "max_active_layout_tasks": 2,
                "active_layout_capacity_remaining": 2,
            },
        }
        release = threading.Event()
        started: list[int | None] = []

        def fake_request(*_args, **kwargs):
            started.append(kwargs.get("max_active_layout_tasks"))
            release.wait(timeout=2)
            return {"ok": True, "source": "llm"}

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = FactorioController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS": "0",
                        "FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "scheduler",
                    },
                ),
                patch.object(controller, "_maybe_ensure_slurm_worker"),
                patch("factorio_ai.remote_slurm.layout_improvement_status", return_value=status),
                patch("factorio_ai.remote_slurm.request_layout_improvement", side_effect=fake_request) as request_layout,
            ):
                controller._maybe_progress_background_layout_work(
                    observation,
                    "launch_rocket_program",
                    "bootstrap_build_item_mall",
                    4,
                )
                controller._maybe_progress_background_layout_work(
                    observation,
                    "launch_rocket_program",
                    "bootstrap_build_item_mall",
                    5,
                )
                deadline = time.monotonic() + 1.0
                while request_layout.call_count < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                controller._maybe_progress_background_layout_work(
                    observation,
                    "launch_rocket_program",
                    "bootstrap_build_item_mall",
                    6,
                )
                self.assertEqual(request_layout.call_count, 2)
                self.assertEqual(len(controller._background_layout_threads), 2)
                release.set()
                for thread in list(controller._background_layout_threads):
                    thread.join(timeout=2)
                controller._collect_background_layout_threads()

        self.assertEqual(started, [2, 2])

    def test_blocked_strategy_submits_background_layout_work(self):
        observation = {
            "tick": 1,
            "inventory": {},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 10,
                    "recipe": "electronic-circuit",
                    "position": {"x": 0, "y": 0},
                    "electric_network_connected": True,
                    "inventories": {},
                }
            ],
            "resources": [],
            "research": {"technologies": {}},
        }

        class FakeController(FactorioController):
            def observe(self):
                return observation

            def strategy_decision(self, objective, require_llm=False, skip_remote=False):
                return {
                    "selected_skill": "future_build_item_skill",
                    "reason": "needs missing build item executor",
                    "skill_status": {
                        "name": "future_build_item_skill",
                        "implemented": False,
                        "executor": None,
                        "codex_required": True,
                    },
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = FakeController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS": "0",
                        "FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "queue",
                    },
                ),
                patch("factorio_ai.remote_slurm.submit_task", return_value="layout-blocked.json") as submit_task,
                # The foundry now redirects a blocked strategy to a progressing skill instead of
                # stopping. Stub the redirect's skill run so the test stays off the live RCON path
                # and isolates the blocked-strategy bookkeeping (codex-wait, foundry enqueue, layout).
                patch.object(
                    controller,
                    "_run_skill",
                    return_value=RunSummary(False, "redirect stubbed in test", 0, 0, cfg.log_dir / "redirect.log", "iron-plate"),
                ),
            ):
                summary = controller.run_strategy_step("launch_rocket_program")

            log_path = cfg.log_dir / "layout-improvement-background.jsonl"
            rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            wait_state = json.loads((cfg.runtime_dir / "codex-wait.json").read_text(encoding="utf-8"))
            foundry_queue = json.loads((cfg.runtime_dir / "skill-foundry-priority.json").read_text(encoding="utf-8"))

        self.assertFalse(summary.ok)
        submit_task.assert_called_once()
        submitted = submit_task.call_args.args[0]
        self.assertEqual(submitted["type"], "layout_improvement_request")
        self.assertEqual(submitted["payload"]["active_skill"], "codex_wait:future_build_item_skill")
        self.assertEqual(rows[0]["event"], "layout_blocked_strategy_detected")
        self.assertEqual(rows[-1]["event"], "layout_task_submitted")
        self.assertTrue(wait_state["active"])
        self.assertEqual(wait_state["selected_skill"], "future_build_item_skill")
        self.assertEqual(wait_state["active_skill"], "codex_wait:future_build_item_skill")
        # The blocked skill is queued for the local-LLM skill foundry to self-develop.
        queued_skills = {item["skill_name"] for item in foundry_queue["queue"]}
        self.assertIn("future_build_item_skill", queued_skills)

    def test_blocked_no_mod_strategy_can_autostart_codex_wait_layout_loop(self):
        observation = {
            "tick": 1,
            "inventory": {},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 11,
                    "recipe": "transport-belt",
                    "position": {"x": 1, "y": 1},
                    "electric_network_connected": True,
                    "inventories": {},
                }
            ],
            "resources": [],
            "research": {"technologies": {}},
        }

        class DummyProcess:
            pid = 4321

        class FakeController(ModlessFactorioController):
            def observe(self):
                return observation

            def strategy_decision(self, objective, require_llm=False, skip_remote=False):
                return {
                    "selected_skill": "future_build_item_skill",
                    "reason": "needs missing build item executor",
                    "skill_status": {
                        "name": "future_build_item_skill",
                        "implemented": False,
                        "executor": None,
                        "codex_required": True,
                    },
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = FakeController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_CODEX_WAIT_LAYOUT_AUTOSTART": "1",
                        "FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS": "0",
                        "FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "queue",
                        "FACTORIO_AI_SLURM_AUTO_RENEW_ENABLED": "0",
                    },
                ),
                patch("factorio_ai.controller.subprocess.Popen", return_value=DummyProcess()) as popen,
                patch("factorio_ai.remote_slurm.submit_task", return_value="layout-blocked.json"),
                # Keep the new foundry redirect off the live RCON path so the test isolates the
                # codex-wait-layout-loop autostart behavior on a blocked strategy.
                patch.object(
                    controller,
                    "_run_skill",
                    return_value=RunSummary(False, "redirect stubbed in test", 0, 0, cfg.log_dir / "redirect.log", "iron-plate"),
                ),
            ):
                summary = controller.run_strategy_step("launch_rocket_program")

            process_state = json.loads((cfg.runtime_dir / "codex-wait-layout-loop.json").read_text(encoding="utf-8"))
            rows = [
                json.loads(line)
                for line in (cfg.log_dir / "layout-improvement-background.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertFalse(summary.ok)
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertIn("run-no-mod-codex-wait-layout-loop", command)
        self.assertEqual(process_state["pid"], 4321)
        self.assertIn("run-no-mod-codex-wait-layout-loop", process_state["command"])
        self.assertTrue(any(row["event"] == "layout_codex_wait_loop_started" for row in rows))

    def test_manual_codex_work_start_autostarts_no_mod_layout_loop(self):
        class DummyProcess:
            pid = 9876

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = ModlessFactorioController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_CODEX_WAIT_LAYOUT_AUTOSTART": "1",
                        "FACTORIO_AI_CODEX_WAIT_LAYOUT_SLEEP_SECONDS": "0",
                    },
                ),
                patch("factorio_ai.controller.subprocess.Popen", return_value=DummyProcess()) as popen,
            ):
                summary = controller.begin_codex_work(
                    "launch_rocket_program",
                    "future_build_item_skill",
                    "Codex is implementing the missing build item executor.",
                )

            wait_state = json.loads((cfg.runtime_dir / "codex-wait.json").read_text(encoding="utf-8"))
            process_state = json.loads((cfg.runtime_dir / "codex-wait-layout-loop.json").read_text(encoding="utf-8"))
            rows = [
                json.loads(line)
                for line in (cfg.log_dir / "layout-improvement-background.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertIn("run-no-mod-codex-wait-layout-loop", command)
        self.assertTrue(summary["waitState"]["active"])
        self.assertEqual(summary["layoutLoop"]["pid"], 9876)
        self.assertTrue(wait_state["active"])
        self.assertEqual(wait_state["selected_skill"], "future_build_item_skill")
        self.assertEqual(process_state["pid"], 9876)
        self.assertTrue(any(row["event"] == "layout_codex_wait_loop_started" for row in rows))

    def test_manual_codex_work_finish_clears_wait_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_test_config(Path(temp_dir))
            controller = FactorioController(cfg)
            controller.begin_codex_work(
                "launch_rocket_program",
                "future_build_item_skill",
                "Codex is implementing the missing build item executor.",
            )
            summary = controller.finish_codex_work(
                "future_build_item_skill",
                clear_reason="new build item executor committed",
            )
            wait_state = json.loads((cfg.runtime_dir / "codex-wait.json").read_text(encoding="utf-8"))

        self.assertTrue(summary["ok"])
        self.assertFalse(summary["waitState"]["active"])
        self.assertFalse(wait_state["active"])
        self.assertEqual(wait_state["clear_reason"], "new build item executor committed")

    def test_no_mod_strategy_step_strict_real_player_rejects_virtual_agent(self):
        class FakeController(ModlessFactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "tick": 1,
                    "player": {"name": "server", "kind": "server", "character_valid": False},
                    "execution": {"mode": "virtual", "virtual": True, "character_valid": False},
                    "inventory": {},
                    "entities": [],
                    "resources": [],
                    "enemies": [],
                    "research": {"technologies": {}},
                }

            def strategy_decision(self, *args, **kwargs):
                raise AssertionError("strict execution guard should run before LLM strategy")

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            with patch.dict("os.environ", {"FACTORIO_AI_REQUIRE_REAL_PLAYER": "1"}):
                summary = controller.run_strategy_step("launch_rocket_program", require_llm=True)

        self.assertFalse(summary.ok)
        self.assertEqual(summary.strategy["source"], "execution_guard")
        self.assertIn("virtual server agent", summary.reason)

    def test_no_mod_action_strict_real_player_rejects_virtual_agent(self):
        class FakeController(ModlessFactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "tick": 1,
                    "player": {"name": "server", "kind": "server", "character_valid": False},
                    "execution": {"mode": "virtual", "virtual": True, "character_valid": False},
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            with patch.dict("os.environ", {"FACTORIO_AI_REQUIRE_REAL_PLAYER": "1"}):
                response = controller.act({"type": "wait", "ticks": 1})

        self.assertFalse(response["ok"])
        self.assertIn("virtual server agent", response["reason"])

    def test_no_mod_action_strict_real_player_rejects_dead_character(self):
        class FakeController(ModlessFactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "tick": 1,
                    "player": {"name": "r1jae", "kind": "player", "character_valid": False},
                    "execution": {"mode": "player", "virtual": False, "character_valid": False},
                    "enemies": [],
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            with patch.dict("os.environ", {"FACTORIO_AI_REQUIRE_REAL_PLAYER": "1"}):
                response = controller.act({"type": "wait", "ticks": 1})

        self.assertFalse(response["ok"])
        self.assertIn("has no valid character", response["reason"])

    def test_no_mod_action_strict_real_player_rejects_remote_controller(self):
        class FakeModless:
            def act(self, action, player_name=None):
                return {"ok": False, "reason": "restore failed in test"}

        class FakeController(ModlessFactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "tick": 1,
                    "player": {
                        "name": "r1jae",
                        "kind": "player",
                        "character_valid": True,
                        "controller_is_character": False,
                    },
                    "execution": {
                        "mode": "player",
                        "virtual": False,
                        "character_valid": True,
                        "controller_is_character": False,
                    },
                    "enemies": [],
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            controller._modless = FakeModless()
            with patch.dict("os.environ", {"FACTORIO_AI_REQUIRE_REAL_PLAYER": "1"}):
                response = controller.act({"type": "wait", "ticks": 1})

        self.assertFalse(response["ok"])
        self.assertIn("not in character controller mode", response["reason"])
        self.assertIn("restore_character_controller failed", response["reason"])

    def test_no_mod_action_strict_real_player_restores_remote_controller_before_action(self):
        remote_observation = {
            "ok": True,
            "tick": 1,
            "player": {
                "name": "r1jae",
                "kind": "player",
                "position": {"x": 0.0, "y": 0.0},
                "character_valid": True,
                "controller_is_character": False,
            },
            "execution": {
                "mode": "player",
                "virtual": False,
                "character_valid": True,
                "controller_is_character": False,
            },
            "enemies": [],
        }
        character_observation = {
            "ok": True,
            "tick": 2,
            "player": {
                "name": "r1jae",
                "kind": "player",
                "position": {"x": 0.0, "y": 0.0},
                "character_valid": True,
                "controller_is_character": True,
            },
            "execution": {
                "mode": "player",
                "virtual": False,
                "character_valid": True,
                "controller_is_character": True,
            },
            "enemies": [],
        }

        class FakeModless:
            def __init__(self):
                self.actions = []

            def act(self, action, player_name=None):
                self.actions.append((action, player_name))
                return {"ok": True, "action": action.get("type")}

        class FakeController(ModlessFactorioController):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.observations = [remote_observation, character_observation]

            def observe(self):
                if self.observations:
                    return self.observations.pop(0)
                return character_observation

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            fake_modless = FakeModless()
            controller._modless = fake_modless
            with patch.dict("os.environ", {"FACTORIO_AI_REQUIRE_REAL_PLAYER": "1"}):
                response = controller.act({"type": "wait", "ticks": 1})

        self.assertTrue(response["ok"])
        self.assertEqual(response["action"], "wait")
        self.assertEqual(fake_modless.actions[0][0]["type"], "restore_character_controller")
        self.assertEqual(fake_modless.actions[1][0]["type"], "wait")

    def test_no_mod_action_strict_real_player_pauses_near_enemy(self):
        class FakeController(ModlessFactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "tick": 1,
                    "player": {
                        "name": "r1jae",
                        "kind": "player",
                        "position": {"x": 0.0, "y": 0.0},
                        "character_valid": True,
                        "controller_is_character": True,
                    },
                    "execution": {
                        "mode": "player",
                        "virtual": False,
                        "character_valid": True,
                        "controller_is_character": True,
                    },
                    "enemies": [
                        {
                            "name": "small-biter",
                            "type": "unit",
                            "position": {"x": 12.0, "y": 0.0},
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            with patch.dict("os.environ", {"FACTORIO_AI_REQUIRE_REAL_PLAYER": "1"}):
                response = controller.act({"type": "wait", "ticks": 1})

        self.assertFalse(response["ok"])
        self.assertIn("enemy small-biter", response["reason"])
        self.assertIn("near player", response["reason"])

    def test_no_mod_strategy_step_strict_real_player_stops_before_near_enemy_cycle(self):
        class FakeModless:
            def __init__(self):
                self.actions = []

            def act(self, action, player_name=None):
                self.actions.append((action, player_name))
                return {"ok": True, "action": action.get("type")}

        class FakeController(ModlessFactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "tick": 1,
                    "player": {
                        "name": "r1jae",
                        "kind": "player",
                        "position": {"x": 0.0, "y": 0.0},
                        "character_valid": True,
                        "controller_is_character": True,
                    },
                    "execution": {
                        "mode": "player",
                        "virtual": False,
                        "character_valid": True,
                        "controller_is_character": True,
                    },
                    "enemies": [
                        {
                            "name": "small-biter",
                            "type": "unit",
                            "position": {"x": 12.0, "y": 0.0},
                        }
                    ],
                }

            def strategy_decision(self, *args, **kwargs):
                raise AssertionError("enemy execution guard should run before LLM strategy")

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            fake_modless = FakeModless()
            controller._modless = fake_modless
            with patch.dict("os.environ", {"FACTORIO_AI_REQUIRE_REAL_PLAYER": "1"}):
                summary = controller.run_strategy_step("launch_rocket_program", require_llm=True)

        self.assertFalse(summary.ok)
        self.assertEqual(summary.selected_skill, "build_starter_defense")
        self.assertEqual(summary.strategy["source"], "execution_guard")
        self.assertIn("near player", summary.reason)
        self.assertEqual(fake_modless.actions[0][0]["type"], "stop")

    def test_no_mod_action_strict_real_player_pauses_path_near_enemy(self):
        class FakeController(ModlessFactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "tick": 1,
                    "player": {
                        "name": "r1jae",
                        "kind": "player",
                        "position": {"x": 0.0, "y": 0.0},
                        "character_valid": True,
                        "controller_is_character": True,
                    },
                    "execution": {
                        "mode": "player",
                        "virtual": False,
                        "character_valid": True,
                        "controller_is_character": True,
                    },
                    "enemies": [
                        {
                            "name": "biter-spawner",
                            "type": "unit-spawner",
                            "position": {"x": 40.0, "y": 8.0},
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            with patch.dict("os.environ", {"FACTORIO_AI_REQUIRE_REAL_PLAYER": "1"}):
                response = controller.act({"type": "move_to", "position": {"x": 80.0, "y": 0.0}})

        self.assertFalse(response["ok"])
        self.assertIn("near movement path", response["reason"])

    def test_auto_agent_player_name_uses_first_connected_player(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), agent_player_name="auto")
            controller = ModlessFactorioController(cfg)

        self.assertEqual(controller._agent_parameter(), {})
        self.assertNotIn("player_name", controller._agent_action({"type": "wait", "ticks": 1}))

    def test_wait_for_move_refreshes_direction_until_arrival(self):
        class MovingController(FactorioController):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.positions = [
                    {"x": 0.0, "y": 0.0},
                    {"x": 0.25, "y": 0.0},
                    {"x": 1.0, "y": 0.0},
                ]
                self.actions = []
                self.stopped = False

            def observe(self):
                position = self.positions.pop(0) if self.positions else {"x": 1.0, "y": 0.0}
                return {
                    "player": {
                        "name": "r1jae",
                        "kind": "player",
                        "position": position,
                        "character_valid": True,
                        "move": {"active": True},
                    }
                }

            def act(self, action):
                self.actions.append(action)
                return {"ok": True, "action": action.get("type")}

            def stop_agent(self):
                self.stopped = True
                return {"ok": True, "action": "stop"}

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = MovingController(make_test_config(Path(temp_dir)))
            with patch("factorio_ai.controller.time.sleep", return_value=None):
                arrived, reason = controller._wait_for_move(
                    {"type": "move_to", "position": {"x": 1.0, "y": 0.0}, "tolerance": 0.1}
                )

        self.assertTrue(arrived, reason)
        self.assertTrue(controller.stopped)
        self.assertTrue(any(action["type"] == "move_to" for action in controller.actions))

    def test_wait_for_move_default_tolerance_accepts_interaction_range(self):
        class NearbyController(FactorioController):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.actions = []
                self.stopped = False

            def observe(self):
                return {
                    "player": {
                        "name": "r1jae",
                        "kind": "player",
                        "position": {"x": 0.0, "y": 0.0},
                        "character_valid": True,
                        "move": {"active": False},
                    }
                }

            def act(self, action):
                self.actions.append(action)
                return {"ok": True, "action": action.get("type")}

            def stop_agent(self):
                self.stopped = True
                return {"ok": True, "action": "stop"}

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = NearbyController(make_test_config(Path(temp_dir)))
            arrived, reason = controller._wait_for_move({"type": "move_to", "position": {"x": 0.0, "y": 3.5}})

        self.assertTrue(arrived, reason)
        self.assertTrue(controller.stopped)
        self.assertEqual(controller.actions, [])

    def test_wait_for_move_tries_perpendicular_detour_on_stall(self):
        class DetourController(FactorioController):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.positions = [
                    {"x": 0.0, "y": 0.0},
                    {"x": 0.0, "y": 0.0},
                    {"x": 4.0, "y": 0.0},
                    {"x": 0.0, "y": 10.0},
                ]
                self.actions = []
                self.stopped = False

            def observe(self):
                position = self.positions.pop(0) if self.positions else {"x": 0.0, "y": 10.0}
                return {
                    "player": {
                        "name": "r1jae",
                        "kind": "player",
                        "position": position,
                        "character_valid": True,
                        "move": {"active": True},
                    }
                }

            def act(self, action):
                self.actions.append(action)
                return {"ok": True, "action": action.get("type")}

            def stop_agent(self):
                self.stopped = True
                return {"ok": True, "action": "stop"}

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = DetourController(make_test_config(Path(temp_dir)))
            with patch("factorio_ai.controller.time.sleep", return_value=None):
                arrived, reason = controller._wait_for_move(
                    {"type": "move_to", "position": {"x": 0.0, "y": 10.0}, "stall_timeout_seconds": -1}
                )

        self.assertTrue(arrived, reason)
        self.assertTrue(controller.stopped)
        self.assertEqual(controller.actions[0]["position"], {"x": 4.0, "y": 0.0})
        self.assertEqual(controller.actions[0]["max_detour_attempts"], 0)

    def test_move_detour_action_offsets_perpendicular_to_main_axis(self):
        vertical = _move_detour_action({"x": 10.0, "y": 0.0}, {"x": 10.0, "y": 20.0}, 0)
        horizontal = _move_detour_action({"x": 0.0, "y": 10.0}, {"x": 20.0, "y": 10.0}, 1)

        self.assertEqual(vertical["position"], {"x": 14.0, "y": 0.0})
        self.assertEqual(horizontal["position"], {"x": 0.0, "y": 6.0})

    def test_no_mod_move_to_can_use_gui_keyboard_executor(self):
        class FakeController(ModlessFactorioController):
            def observe(self):
                return {
                    "ok": True,
                    "player": {
                        "name": "r1jae",
                        "kind": "player",
                        "position": {"x": 0.0, "y": 0.0},
                        "character_valid": True,
                    },
                    "execution": {"mode": "player", "virtual": False, "character_valid": True},
                }

        class FakeDriver:
            def __init__(self, cfg):
                self.cfg = cfg
                self.held = []
                self.clicks = []

            def activate_factorio(self, timeout_seconds=30.0):
                return True

            def click_window_ratio(self, x_ratio, y_ratio):
                self.clicks.append((x_ratio, y_ratio))

            def hold_keys(self, keys, duration_seconds):
                self.held.append((list(keys), duration_seconds))

        fake_driver = FakeDriver(None)
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_REQUIRE_REAL_PLAYER": "1",
                        "FACTORIO_AI_USE_GUI_INPUT_FOR_MOVEMENT": "1",
                    },
                ),
                patch("factorio_ai.vanilla_gui.VanillaGuiDriver", return_value=fake_driver),
            ):
                response = controller.act({"type": "move_to", "position": {"x": 1.0, "y": 0.0}})

        self.assertTrue(response["ok"])
        self.assertEqual(response["mode"], "gui-input")
        self.assertEqual(response["keys"], ["d"])
        self.assertEqual(fake_driver.clicks, [(0.5, 0.5)])
        self.assertEqual(fake_driver.held[0][0], ["d"])

    def test_autopilot_pulses_layout_work_while_codex_wait_state_is_active(self):
        observation = {
            "tick": 2,
            "inventory": {},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 20,
                    "recipe": "transport-belt",
                    "position": {"x": 12, "y": 4},
                    "electric_network_connected": True,
                    "inventories": {},
                }
            ],
            "resources": [],
            "research": {"technologies": {}},
        }

        class FailingController(FactorioController):
            def observe(self):
                return observation

            def run_strategy_step(self, **kwargs):
                raise RuntimeError("strategy endpoint unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            save_selected_improvement_site(
                cfg.runtime_dir,
                "launch_rocket_program",
                {"site_id": "build_item_mall:2,2", "kind": "build_item_mall", "item": "transport-belt"},
                selected_at="2026-06-14T00:00:00+00:00",
            )
            (cfg.runtime_dir / "codex-wait.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "objective": "launch_rocket_program",
                        "selected_skill": "future_build_item_skill",
                        "active_skill": "codex_wait:future_build_item_skill",
                        "reason": "Codex is implementing the missing build item executor.",
                    }
                ),
                encoding="utf-8",
            )
            controller = FailingController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS": "0",
                        "FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "queue",
                    },
                ),
                patch("factorio_ai.remote_slurm.submit_task", return_value="layout-wait.json") as submit_task,
                patch("factorio_ai.remote_slurm.read_task_state", return_value=("running", None, "")),
            ):
                summary = controller.run_autopilot_loop(cycles=1, sleep_seconds=0)

            rows = [
                json.loads(line)
                for line in (cfg.log_dir / "layout-improvement-background.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertFalse(summary.ok)
        submit_task.assert_called_once()
        submitted = submit_task.call_args.args[0]
        self.assertEqual(submitted["payload"]["active_skill"], "codex_wait:future_build_item_skill")
        self.assertEqual(submitted["payload"]["selected_improvement_site"]["site_id"], "build_item_mall:2,2")
        self.assertTrue(any(row["event"] == "layout_codex_wait_heartbeat" for row in rows))
        self.assertTrue(any(row["event"] == "layout_task_submitted" for row in rows))

    def test_codex_wait_layout_loop_keeps_submitting_until_wait_clears(self):
        observation = {
            "tick": 3,
            "inventory": {},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 30,
                    "recipe": "electronic-circuit",
                    "position": {"x": 20, "y": 4},
                    "electric_network_connected": True,
                    "inventories": {},
                }
            ],
            "resources": [],
            "research": {"technologies": {}},
        }

        class WaitingController(FactorioController):
            def observe(self):
                return observation

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            (cfg.runtime_dir / "codex-wait.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "objective": "launch_rocket_program",
                        "selected_skill": "future_build_item_skill",
                        "active_skill": "codex_wait:future_build_item_skill",
                        "reason": "Codex is implementing the missing build item executor.",
                    }
                ),
                encoding="utf-8",
            )
            controller = WaitingController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS": "0",
                        "FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "queue",
                    },
                ),
                patch(
                    "factorio_ai.remote_slurm.submit_task",
                    side_effect=["layout-wait-1.json", "layout-wait-2.json"],
                ) as submit_task,
                patch("factorio_ai.remote_slurm.read_task_state", return_value=("result", {"ok": True}, "")),
            ):
                summary = controller.run_codex_wait_layout_loop(cycles=2, sleep_seconds=0)

            rows = [
                json.loads(line)
                for line in (cfg.log_dir / "layout-improvement-background.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(summary.ok)
        self.assertEqual(summary.cycles, 2)
        self.assertTrue(summary.wait_active)
        self.assertEqual(submit_task.call_count, 2)
        self.assertTrue(any(row["event"] == "layout_codex_wait_heartbeat" for row in rows))
        self.assertTrue(any(row["event"] == "layout_result" for row in rows))
        self.assertEqual(rows[-1]["event"], "layout_task_submitted")

    def test_idle_layout_loop_submits_when_autopilot_heartbeat_missing(self):
        observation = {
            "tick": 4,
            "inventory": {},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 40,
                    "recipe": "transport-belt",
                    "position": {"x": 10, "y": 4},
                    "electric_network_connected": True,
                    "inventories": {},
                }
            ],
            "resources": [],
            "research": {"technologies": {}},
        }

        class IdleController(FactorioController):
            def observe(self):
                return observation

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            controller = IdleController(cfg)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "queue",
                        "FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS": "999",
                    },
                ),
                patch("factorio_ai.remote_slurm.submit_task", return_value="layout-idle.json") as submit_task,
                patch("factorio_ai.remote_slurm.read_task_state", return_value=("running", None, "")),
            ):
                summary = controller.run_idle_layout_loop(cycles=1, sleep_seconds=0, min_submit_interval_seconds=0)

            rows = [
                json.loads(line)
                for line in (cfg.log_dir / "layout-improvement-background.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(summary.ok)
        self.assertEqual(summary.idle_cycles, 1)
        self.assertEqual(summary.busy_cycles, 0)
        submit_task.assert_called_once()
        submitted = submit_task.call_args.args[0]
        self.assertIn("idle:autopilot_heartbeat_missing", submitted["payload"]["active_skill"])
        self.assertTrue(submitted["payload"]["layout_learning"]["return_learned_skills"])
        self.assertTrue(any(row["event"] == "layout_idle_scheduler_heartbeat" for row in rows))
        self.assertTrue(any(row["event"] == "layout_task_submitted" for row in rows))

    def test_idle_layout_loop_submits_when_autopilot_heartbeat_is_stale(self):
        observation = {
            "tick": 5,
            "inventory": {},
            "entities": [],
            "resources": [],
            "research": {"technologies": {}},
        }

        class IdleController(FactorioController):
            def observe(self):
                return observation

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            (cfg.runtime_dir / "autopilot-heartbeat.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "state": "cycle_start",
                        "updated_at": (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(),
                        "objective": "launch_rocket_program",
                        "cycle": 3,
                    }
                ),
                encoding="utf-8",
            )
            controller = IdleController(cfg)
            with (
                patch.dict("os.environ", {"FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "queue"}),
                patch("factorio_ai.remote_slurm.submit_task", return_value="layout-stale.json") as submit_task,
                patch("factorio_ai.remote_slurm.read_task_state", return_value=("running", None, "")),
            ):
                summary = controller.run_idle_layout_loop(
                    cycles=1,
                    sleep_seconds=0,
                    stale_seconds=15,
                    min_submit_interval_seconds=0,
                )

        self.assertTrue(summary.ok)
        self.assertEqual(summary.idle_cycles, 1)
        submit_task.assert_called_once()
        submitted = submit_task.call_args.args[0]
        self.assertIn("autopilot_heartbeat_stale", submitted["payload"]["active_skill"])

    def test_idle_layout_loop_logs_stale_process_file_before_restart(self):
        observation = {
            "tick": 5,
            "inventory": {},
            "entities": [],
            "resources": [],
            "research": {"technologies": {}},
        }

        class IdleController(FactorioController):
            def observe(self):
                return observation

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            (cfg.runtime_dir / "idle-layout-loop.json").write_text(
                json.dumps(
                    {
                        "pid": 987654,
                        "started_at": "2026-06-15T22:17:11+00:00",
                        "objective": "launch_rocket_program",
                        "state": "running",
                    }
                ),
                encoding="utf-8",
            )
            controller = IdleController(cfg)
            with (
                patch.dict("os.environ", {"FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "queue"}),
                patch("factorio_ai.controller._pid_is_running", return_value=False),
                patch("factorio_ai.remote_slurm.submit_task", return_value="layout-stale.json"),
                patch("factorio_ai.remote_slurm.read_task_state", return_value=("running", None, "")),
            ):
                summary = controller.run_idle_layout_loop(
                    cycles=1,
                    sleep_seconds=0,
                    stale_seconds=15,
                    min_submit_interval_seconds=0,
                )
            rows = [
                json.loads(line)
                for line in (cfg.log_dir / "layout-improvement-background.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(summary.ok)
        self.assertTrue(any(row["event"] == "layout_idle_loop_stale_pid_recovered" for row in rows))

    def test_no_mod_idle_layout_loop_uses_lightweight_observe(self):
        class FakeModless:
            def __init__(self):
                self.calls = []

            def observe(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "ok": True,
                    "tick": 5,
                    "inventory": {},
                    "entities": [],
                    "resources": [],
                    "research": {"technologies": {}},
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            (cfg.runtime_dir / "autopilot-heartbeat.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "state": "cycle_start",
                        "updated_at": (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(),
                        "objective": "launch_rocket_program",
                        "cycle": 3,
                    }
                ),
                encoding="utf-8",
            )
            controller = ModlessFactorioController(cfg)
            fake_modless = FakeModless()
            controller._modless = fake_modless
            with (
                patch.dict("os.environ", {"FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "queue"}),
                patch("factorio_ai.remote_slurm.submit_task", return_value="layout-stale.json"),
                patch("factorio_ai.remote_slurm.read_task_state", return_value=("running", None, "")),
            ):
                summary = controller.run_idle_layout_loop(
                    cycles=1,
                    sleep_seconds=0,
                    stale_seconds=15,
                    min_submit_interval_seconds=0,
                )

        self.assertTrue(summary.ok)
        self.assertEqual(fake_modless.calls[0]["include_planning_sites"], False)
        self.assertEqual(fake_modless.calls[0]["player_name"], "AI")

    def test_no_mod_default_observe_uses_lightweight_mode(self):
        class FakeModless:
            def __init__(self):
                self.calls = []

            def observe(self, **kwargs):
                self.calls.append(kwargs)
                return {"ok": True, "tick": 1, "power_sites": [], "lab_sites": [], "automation_sites": []}

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_test_config(Path(temp_dir))
            controller = ModlessFactorioController(cfg)
            fake_modless = FakeModless()
            controller._modless = fake_modless

            observation = controller.observe()

        self.assertEqual(observation["tick"], 1)
        self.assertEqual(fake_modless.calls[0]["include_planning_sites"], False)

    def test_no_mod_site_input_skill_loop_uses_full_observe(self):
        class FakeModless:
            def __init__(self):
                self.calls = []

            def observe(self, **kwargs):
                self.calls.append(kwargs)
                return {"ok": True, "tick": 1, "power_sites": [], "lab_sites": [], "automation_sites": []}

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_test_config(Path(temp_dir))
            controller = ModlessFactorioController(cfg)
            fake_modless = FakeModless()
            controller._modless = fake_modless

            controller._observe_for_skill_loop("build_site_input_logistic_line", 1)
            controller._observe_for_skill_loop("produce_iron_plate", 1)

        self.assertEqual(fake_modless.calls[0]["include_planning_sites"], False)
        self.assertFalse(fake_modless.calls[0]["lightweight"])
        self.assertTrue(fake_modless.calls[1]["lightweight"])

    def test_no_mod_retries_planning_site_observe_only_when_site_candidate_is_needed(self):
        class FakeModless:
            def __init__(self):
                self.calls = []

            def observe(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("include_planning_sites"):
                    return {"ok": True, "tick": 2, "power_sites": [{"layout": {}}], "lab_sites": [], "automation_sites": []}
                return {"ok": True, "tick": 1, "power_sites": [], "lab_sites": [], "automation_sites": []}

        class FakeSkill:
            def next_action(self, observation):
                if observation.get("power_sites"):
                    return PlannerDecision({"type": "wait", "ticks": 1}, "site candidate available")
                return PlannerDecision(None, "cannot find a buildable water site for steam power")

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_test_config(Path(temp_dir))
            controller = ModlessFactorioController(cfg)
            fake_modless = FakeModless()
            controller._modless = fake_modless
            initial = controller.observe()
            initial_decision = FakeSkill().next_action(initial)

            observation, decision = controller._maybe_retry_skill_with_planning_sites(
                FakeSkill(),
                initial,
                initial_decision,
            )
            cached = controller.observe()

        self.assertEqual(decision.action, {"type": "wait", "ticks": 1})
        self.assertEqual(observation["tick"], 2)
        self.assertEqual([call["include_planning_sites"] for call in fake_modless.calls], [False, True, False])
        self.assertEqual(cached["planning_sites_cached_from_tick"], 2)
        self.assertEqual(cached["power_sites"], [{"layout": {}}])

    def test_no_mod_uses_fresh_planning_site_cache_before_rescanning(self):
        class FakeModless:
            def __init__(self):
                self.calls = []

            def observe(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("include_planning_sites"):
                    raise AssertionError("fresh planning-site cache should avoid a full scan")
                return {"ok": True, "tick": 9, "power_sites": [], "lab_sites": [], "automation_sites": []}

        class FakeSkill:
            def next_action(self, observation):
                if observation.get("power_sites"):
                    return PlannerDecision({"type": "wait", "ticks": 1}, "cached site candidate available")
                return PlannerDecision(None, "cannot find a buildable water site for steam power")

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_test_config(Path(temp_dir))
            cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            (cfg.runtime_dir / "planning-sites-cache.json").write_text(
                json.dumps(
                    {
                        "cached_at": time.time(),
                        "tick": 7,
                        "power_sites": [{"layout": {"offshore_pump": {"name": "offshore-pump"}}}],
                        "lab_sites": [],
                        "automation_sites": [],
                    }
                ),
                encoding="utf-8",
            )
            controller = ModlessFactorioController(cfg)
            fake_modless = FakeModless()
            controller._modless = fake_modless
            initial = controller.observe()
            initial_decision = FakeSkill().next_action(initial)

            observation, decision = controller._maybe_retry_skill_with_planning_sites(
                FakeSkill(),
                initial,
                initial_decision,
            )

        self.assertEqual(decision.action, {"type": "wait", "ticks": 1})
        self.assertEqual(observation["planning_sites_cached_from_tick"], 7)
        self.assertEqual([call["include_planning_sites"] for call in fake_modless.calls], [False])

    def test_no_mod_recent_empty_planning_site_cache_throttles_water_scan_retry(self):
        class FakeModless:
            def __init__(self):
                self.calls = []

            def observe(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("include_planning_sites"):
                    raise AssertionError("recent empty planning-site cache should throttle full water scan")
                return {"ok": True, "tick": 10, "power_sites": [], "lab_sites": [], "automation_sites": []}

        class FakeSkill:
            def next_action(self, observation):
                return PlannerDecision(None, "cannot find a buildable water site for steam power")

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_test_config(Path(temp_dir))
            cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            (cfg.runtime_dir / "planning-sites-cache.json").write_text(
                json.dumps(
                    {
                        "cached_at": time.time(),
                        "tick": 9,
                        "power_sites": [],
                        "lab_sites": [],
                        "automation_sites": [],
                    }
                ),
                encoding="utf-8",
            )
            controller = ModlessFactorioController(cfg)
            fake_modless = FakeModless()
            controller._modless = fake_modless
            initial = controller.observe()
            initial_decision = FakeSkill().next_action(initial)

            observation, decision = controller._maybe_retry_skill_with_planning_sites(
                FakeSkill(),
                initial,
                initial_decision,
            )

        self.assertIsNone(decision.action)
        self.assertIn("cannot find a buildable water site", decision.reason)
        self.assertEqual(observation["planning_sites_cached_from_tick"], 9)
        self.assertEqual([call["include_planning_sites"] for call in fake_modless.calls], [False])

    def test_no_mod_stale_planning_site_cache_rescans_for_lab_retry(self):
        class FakeModless:
            def __init__(self):
                self.calls = []

            def observe(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("include_planning_sites"):
                    return {
                        "ok": True,
                        "tick": 2001,
                        "power_sites": [],
                        "lab_sites": [{"lab_position": {"x": 1, "y": 1}}],
                        "automation_sites": [],
                    }
                return {"ok": True, "tick": 2000, "power_sites": [], "lab_sites": [], "automation_sites": []}

        class FakeSkill:
            def next_action(self, observation):
                if observation.get("lab_sites"):
                    return PlannerDecision({"type": "wait", "ticks": 1}, "lab site candidate available")
                return PlannerDecision(None, "cannot find a powered or wireable lab site near the starter power block")

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_test_config(Path(temp_dir))
            cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            (cfg.runtime_dir / "planning-sites-cache.json").write_text(
                json.dumps(
                    {
                        "cached_at": time.time(),
                        "tick": 10,
                        "power_sites": [],
                        "lab_sites": [],
                        "automation_sites": [],
                    }
                ),
                encoding="utf-8",
            )
            controller = ModlessFactorioController(cfg)
            fake_modless = FakeModless()
            controller._modless = fake_modless
            initial = controller.observe()
            initial_decision = FakeSkill().next_action(initial)

            observation, decision = controller._maybe_retry_skill_with_planning_sites(
                FakeSkill(),
                initial,
                initial_decision,
            )

        self.assertEqual(decision.action, {"type": "wait", "ticks": 1})
        self.assertEqual(observation["tick"], 2001)
        self.assertEqual([call["include_planning_sites"] for call in fake_modless.calls], [False, True])

    def test_idle_layout_loop_skips_when_autopilot_heartbeat_is_fresh_busy(self):
        class BusyController(FactorioController):
            def observe(self):
                raise AssertionError("fresh busy autopilot should not be observed by idle layout loop")

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            (cfg.runtime_dir / "autopilot-heartbeat.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "state": "cycle_start",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "objective": "launch_rocket_program",
                        "cycle": 3,
                    }
                ),
                encoding="utf-8",
            )
            controller = BusyController(cfg)
            with patch("factorio_ai.remote_slurm.submit_task") as submit_task:
                summary = controller.run_idle_layout_loop(cycles=1, sleep_seconds=0, stale_seconds=15)

            rows = [
                json.loads(line)
                for line in (cfg.log_dir / "layout-improvement-background.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(summary.ok)
        self.assertEqual(summary.idle_cycles, 0)
        self.assertEqual(summary.busy_cycles, 1)
        submit_task.assert_not_called()
        self.assertTrue(any(row["event"] == "layout_idle_scheduler_busy" for row in rows))

    def test_idle_layout_loop_skips_when_live_skill_heartbeat_is_fresh_busy(self):
        class BusyController(FactorioController):
            def observe(self):
                raise AssertionError("fresh live skill should not be observed by idle layout loop")

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            (cfg.runtime_dir / "live-skill-heartbeat.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "state": "step",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "objective": "launch_rocket_program",
                        "skill": "connect_coal_fuel_feed",
                        "step": 3,
                    }
                ),
                encoding="utf-8",
            )
            controller = BusyController(cfg)
            with patch("factorio_ai.remote_slurm.submit_task") as submit_task:
                summary = controller.run_idle_layout_loop(cycles=1, sleep_seconds=0, stale_seconds=15)

            rows = [
                json.loads(line)
                for line in (cfg.log_dir / "layout-improvement-background.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(summary.ok)
        self.assertEqual(summary.idle_cycles, 0)
        self.assertEqual(summary.busy_cycles, 1)
        submit_task.assert_not_called()
        self.assertTrue(any(row["event"] == "layout_idle_scheduler_busy" for row in rows))
        busy_row = next(row for row in rows if row["event"] == "layout_idle_scheduler_busy")
        self.assertIn("live skill is active", busy_row["idle_reason"])

    def test_idle_layout_loop_recovers_stale_live_skill_pid(self):
        observation = {
            "tick": 5,
            "inventory": {},
            "entities": [],
            "resources": [],
            "research": {"technologies": {}},
        }

        class IdleController(FactorioController):
            def observe(self):
                return observation

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = replace(make_test_config(Path(temp_dir)), slurm_enabled=True)
            cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            (cfg.runtime_dir / "live-skill-heartbeat.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "state": "step",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "objective": "launch_rocket_program",
                        "skill": "connect_coal_fuel_feed",
                        "step": 2,
                        "pid": 987654,
                    }
                ),
                encoding="utf-8",
            )
            controller = IdleController(cfg)
            with (
                patch.dict("os.environ", {"FACTORIO_AI_BACKGROUND_LAYOUT_MODE": "queue"}),
                patch("factorio_ai.controller._pid_is_running", return_value=False),
                patch("factorio_ai.remote_slurm.submit_task", return_value="layout-after-stale-live.json") as submit_task,
                patch("factorio_ai.remote_slurm.read_task_state", return_value=("running", None, "")),
            ):
                summary = controller.run_idle_layout_loop(
                    cycles=1,
                    sleep_seconds=0,
                    stale_seconds=15,
                    min_submit_interval_seconds=0,
                )
            heartbeat = json.loads((cfg.runtime_dir / "live-skill-heartbeat.json").read_text(encoding="utf-8"))

        self.assertTrue(summary.ok)
        self.assertEqual(summary.idle_cycles, 1)
        submit_task.assert_called_once()
        self.assertFalse(heartbeat["active"])
        self.assertEqual(heartbeat["state"], "stale")
        self.assertIn("pid is not running", heartbeat["stale_reason"])

    def test_autopilot_loop_writes_heartbeat(self):
        class OneStepController(FactorioController):
            def run_strategy_step(self, **kwargs):
                return StrategyStepSummary(
                    ok=True,
                    reason="test cycle complete",
                    objective=kwargs.get("objective", "launch_rocket_program"),
                    selected_skill="produce_iron_plate",
                    strategy={"selected_skill": "produce_iron_plate"},
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_test_config(Path(temp_dir))
            controller = OneStepController(cfg)
            summary = controller.run_autopilot_loop(cycles=1, sleep_seconds=0)
            heartbeat = json.loads((cfg.runtime_dir / "autopilot-heartbeat.json").read_text(encoding="utf-8"))

        self.assertTrue(summary.ok)
        self.assertEqual(heartbeat["state"], "stopped")
        self.assertEqual(heartbeat["cycle"], 1)
        self.assertEqual(heartbeat["objective"], "launch_rocket_program")

    def test_autopilot_heartbeat_clears_stale_live_skill_from_dead_pid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_test_config(Path(temp_dir))
            cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            (cfg.runtime_dir / "live-skill-heartbeat.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "state": "step",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "objective": "launch_rocket_program",
                        "skill": "relocate_gear_belt_mall_to_iron_source",
                        "step": 17,
                        "pid": 987654,
                    }
                ),
                encoding="utf-8",
            )
            controller = FactorioController(cfg)
            with patch("factorio_ai.controller._pid_is_running", return_value=False):
                controller._write_autopilot_heartbeat("launch_rocket_program", "cycle_start", cycle=1)
            heartbeat = json.loads((cfg.runtime_dir / "live-skill-heartbeat.json").read_text(encoding="utf-8"))

        self.assertFalse(heartbeat["active"])
        self.assertEqual(heartbeat["state"], "stale")
        self.assertIn("cleared stale live skill pid 987654", heartbeat["reason"])

    def test_run_skill_writes_live_skill_heartbeat(self):
        class DoneSkill:
            def next_action(self, observation):
                return PlannerDecision(None, "test skill complete", done=True)

        class DoneController(FactorioController):
            def observe(self):
                return {
                    "inventory": {"iron-plate": 1},
                    "entities": [],
                    "resources": [],
                    "research": {"technologies": {}},
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_test_config(Path(temp_dir))
            controller = DoneController(cfg)
            summary = controller._run_skill(
                DoneSkill(),
                target_item="iron-plate",
                target=1,
                goal="produce_iron_plate",
                max_steps=1,
                log_prefix="test-live-skill",
            )
            heartbeat = json.loads((cfg.runtime_dir / "live-skill-heartbeat.json").read_text(encoding="utf-8"))

        self.assertTrue(summary.ok)
        self.assertFalse(heartbeat["active"])
        self.assertEqual(heartbeat["state"], "stopped")
        self.assertEqual(heartbeat["skill"], "produce_iron_plate")
        self.assertEqual(heartbeat["step"], 1)

    def test_strategy_runner_maps_implemented_material_skills(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FactorioController(make_test_config(Path(temp_dir)))
            config = controller._skill_run_config("produce_electronic_circuit", target_count=7, max_steps=123)
            self.assertIsNotNone(config)
            self.assertEqual(config["target_item"], "electronic-circuit")
            self.assertEqual(config["target"], 7)
            self.assertEqual(config["max_steps"], 123)
            coal_config = controller._skill_run_config("setup_coal_supply", target_count=18, max_steps=456)
            self.assertIsNotNone(coal_config)
            self.assertEqual(coal_config["target_item"], "coal")
            self.assertEqual(coal_config["target"], 18)
            self.assertEqual(coal_config["max_steps"], 456)
            fuel_feed_config = controller._skill_run_config("connect_coal_fuel_feed", max_steps=321)
            self.assertIsNotNone(fuel_feed_config)
            self.assertEqual(fuel_feed_config["target_item"], "coal")
            self.assertEqual(fuel_feed_config["goal"], "connect_coal_fuel_feed")
            self.assertEqual(fuel_feed_config["max_steps"], 321)

    def test_strategy_runner_does_not_fake_automation_skill(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FactorioController(make_test_config(Path(temp_dir)))
            belt_config = controller._skill_run_config("build_belt_smelting_line", target_count=12, max_steps=222)
            self.assertIsNotNone(belt_config)
            self.assertEqual(belt_config["goal"], "build_belt_smelting_line")
            self.assertEqual(belt_config["target"], 12)
            self.assertEqual(belt_config["max_steps"], 222)
            power_config = controller._skill_run_config("setup_power", max_steps=333)
            self.assertIsNotNone(power_config)
            self.assertEqual(power_config["goal"], "setup_power")
            self.assertEqual(power_config["max_steps"], 333)
            research_config = controller._skill_run_config("research_automation", max_steps=444)
            self.assertIsNotNone(research_config)
            self.assertEqual(research_config["goal"], "research_automation")
            self.assertEqual(research_config["max_steps"], 444)
            circuit_line_config = controller._skill_run_config("automate_electronic_circuit_line", target_count=6, max_steps=555)
            self.assertIsNotNone(circuit_line_config)
            self.assertEqual(circuit_line_config["goal"], "automate_electronic_circuit_line")
            self.assertEqual(circuit_line_config["target"], 6)
            self.assertEqual(circuit_line_config["max_steps"], 555)
            default_circuit_config = controller._skill_run_config("automate_electronic_circuit_line")
            self.assertIsNotNone(default_circuit_config)
            self.assertEqual(default_circuit_config["target"], 50)
            logistics_config = controller._skill_run_config("research_logistics", max_steps=666)
            self.assertIsNotNone(logistics_config)
            self.assertEqual(logistics_config["goal"], "research_logistics")
            self.assertEqual(logistics_config["max_steps"], 666)
            electric_drill_research_config = controller._skill_run_config("research_electric_mining_drill", max_steps=667)
            self.assertIsNotNone(electric_drill_research_config)
            self.assertEqual(electric_drill_research_config["goal"], "research_electric_mining_drill")
            self.assertEqual(electric_drill_research_config["target_item"], "automation-science-pack")
            self.assertEqual(electric_drill_research_config["target"], 25)
            self.assertEqual(electric_drill_research_config["max_steps"], 667)
            iron_line_config = controller._skill_run_config(
                "build_iron_plate_logistic_line_to_gear_mall",
                target_count=44,
                max_steps=667,
            )
            self.assertIsNotNone(iron_line_config)
            self.assertEqual(iron_line_config["goal"], "build_iron_plate_logistic_line_to_gear_mall")
            self.assertEqual(iron_line_config["target"], 44)
            self.assertEqual(iron_line_config["max_steps"], 667)
            site_input_config = controller._skill_run_config(
                "build_site_input_logistic_line",
                target_count=33,
                max_steps=668,
                input_item="copper-plate",
            )
            self.assertIsNotNone(site_input_config)
            self.assertEqual(site_input_config["goal"], "build_site_input_logistic_line")
            self.assertEqual(site_input_config["target_item"], "transport-belt")
            self.assertEqual(site_input_config["input_item"], "copper-plate")
            self.assertEqual(site_input_config["target"], 33)
            self.assertEqual(site_input_config["max_steps"], 668)
            self.assertEqual(site_input_config["skill"].item, "copper-plate")
            expand_config = controller._skill_run_config("expand_iron_smelting", target_count=38, max_steps=777)
            self.assertIsNotNone(expand_config)
            self.assertEqual(expand_config["goal"], "expand_iron_smelting")
            self.assertEqual(expand_config["target"], 38)
            self.assertEqual(expand_config["max_steps"], 777)
            copper_expand_config = controller._skill_run_config("expand_copper_smelting", target_count=56, max_steps=888)
            self.assertIsNotNone(copper_expand_config)
            self.assertEqual(copper_expand_config["goal"], "expand_copper_smelting")
            self.assertEqual(copper_expand_config["target"], 56)
            self.assertEqual(copper_expand_config["max_steps"], 888)
            defense_config = controller._skill_run_config("build_starter_defense", max_steps=999)
            self.assertIsNotNone(defense_config)
            self.assertEqual(defense_config["goal"], "build_starter_defense")
            self.assertEqual(defense_config["target_item"], "gun-turret")
            self.assertEqual(defense_config["max_steps"], 999)
            mall_config = controller._skill_run_config("bootstrap_build_item_mall", target_count=24, max_steps=1111)
            self.assertIsNotNone(mall_config)
            self.assertEqual(mall_config["goal"], "bootstrap_build_item_mall")
            self.assertEqual(mall_config["target_item"], "transport-belt")
            self.assertEqual(mall_config["target"], 24)
            self.assertEqual(mall_config["max_steps"], 1111)
            long_inserter_mall_config = controller._skill_run_config(
                "bootstrap_build_item_mall",
                target_count=12,
                max_steps=1112,
                target_item="long-handed-inserter",
            )
            self.assertIsNotNone(long_inserter_mall_config)
            self.assertEqual(long_inserter_mall_config["goal"], "bootstrap_build_item_mall")
            self.assertEqual(long_inserter_mall_config["target_item"], "long-handed-inserter")
            self.assertEqual(long_inserter_mall_config["target"], 12)
            self.assertEqual(long_inserter_mall_config["max_steps"], 1112)
            pole_mall_config = controller._skill_run_config(
                "bootstrap_power_pole_mall",
                target_count=18,
                max_steps=1122,
            )
            self.assertIsNotNone(pole_mall_config)
            self.assertEqual(pole_mall_config["goal"], "bootstrap_power_pole_mall")
            self.assertEqual(pole_mall_config["target_item"], "small-electric-pole")
            self.assertEqual(pole_mall_config["target"], 18)
            self.assertEqual(pole_mall_config["max_steps"], 1122)
            electric_drill_mall_config = controller._skill_run_config(
                "bootstrap_electric_mining_drill_mall",
                target_count=7,
                max_steps=1133,
            )
            self.assertIsNotNone(electric_drill_mall_config)
            self.assertEqual(electric_drill_mall_config["goal"], "bootstrap_electric_mining_drill_mall")
            self.assertEqual(electric_drill_mall_config["target_item"], "electric-mining-drill")
            self.assertEqual(electric_drill_mall_config["target"], 7)
            self.assertEqual(electric_drill_mall_config["max_steps"], 1133)
            layout_config = controller._skill_run_config("plan_factory_site", max_steps=2)
            self.assertIsNotNone(layout_config)
            self.assertEqual(layout_config["goal"], "plan_factory_site")
            self.assertEqual(layout_config["target_item"], "layout-plan")
            self.assertEqual(layout_config["max_steps"], 2)
            relocation_config = controller._skill_run_config(
                "relocate_gear_belt_mall_to_iron_source",
                target_count=30,
                max_steps=444,
            )
            self.assertIsNotNone(relocation_config)
            self.assertEqual(relocation_config["goal"], "relocate_gear_belt_mall_to_iron_source")
            self.assertEqual(relocation_config["target_item"], "transport-belt")
            self.assertEqual(relocation_config["target"], 30)
            self.assertEqual(relocation_config["max_steps"], 444)
            dry_run_config = controller._skill_run_config("expand_iron_smelting", max_steps=0)
            self.assertIsNotNone(dry_run_config)
            self.assertEqual(dry_run_config["max_steps"], 0)

    def test_run_strategy_step_passes_strategy_target_count_to_power_pole_mall(self):
        captured: dict[str, object] = {}

        class FakeController(FactorioController):
            def strategy_decision(self, objective, require_llm=False, skip_remote=False):
                return {
                    "selected_skill": "bootstrap_power_pole_mall",
                    "priority": 94,
                    "reason": "need relocation power poles",
                    "evidence": [],
                    "blockers": [],
                    "expected_effect": "",
                    "target_item": "small-electric-pole",
                    "target_count": 25,
                    "skill_status": {
                        "name": "bootstrap_power_pole_mall",
                        "implemented": True,
                        "executor": "BuildItemMallSkill",
                        "codex_required": False,
                    },
                }

            def _run_skill(self, **kwargs):
                captured.update(kwargs)
                return RunSummary(True, "stubbed", 1, 0, self.cfg.log_dir / "stub.log", kwargs.get("target_item", ""))

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_test_config(Path(temp_dir))
            controller = FakeController(cfg)

            summary = controller.run_strategy_step("launch_rocket_program")

        self.assertTrue(summary.ok)
        self.assertEqual(captured["goal"], "bootstrap_power_pole_mall")
        self.assertEqual(captured["target_item"], "small-electric-pole")
        self.assertEqual(captured["target"], 25)

    def test_strategy_step_summary_serializes_run(self):
        run = RunSummary(
            ok=True,
            reason="done",
            steps=3,
            item_count=5,
            log_path=Path("logs/test.jsonl"),
            item_name="electronic-circuit",
        )
        summary = StrategyStepSummary(
            ok=True,
            reason="done",
            objective="launch_rocket_program",
            selected_skill="produce_electronic_circuit",
            strategy={"selected_skill": "produce_electronic_circuit"},
            run=run,
        )
        payload = summary.to_dict()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["run"]["itemName"], "electronic-circuit")

    def test_strategy_step_filters_site_input_metadata_before_running_skill(self):
        class FakeController(FactorioController):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.run_kwargs = {}

            def strategy_decision(self, objective, require_llm=False, skip_remote=False):
                return {
                    "selected_skill": "build_site_input_logistic_line",
                    "source": "llm",
                    "input_item": "copper-plate",
                }

            def _run_skill(self, **kwargs):
                self.run_kwargs = kwargs
                return RunSummary(
                    ok=True,
                    reason="done",
                    steps=1,
                    item_count=0,
                    log_path=self.cfg.log_dir / "site-input.jsonl",
                    item_name=kwargs["target_item"],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            summary = controller.run_strategy_step("launch_rocket_program", require_llm=True)

        self.assertTrue(summary.ok)
        self.assertNotIn("input_item", controller.run_kwargs)
        self.assertEqual(controller.run_kwargs["skill"].item, "copper-plate")
        self.assertEqual(controller.run_kwargs["target"], 40)

    def test_autopilot_loop_records_each_cycle(self):
        class FakeController(FactorioController):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.calls = 0

            def run_strategy_step(self, **kwargs):
                self.calls += 1
                return StrategyStepSummary(
                    ok=True,
                    reason="done",
                    objective=kwargs.get("objective", "launch_rocket_program"),
                    selected_skill="produce_iron_plate",
                    strategy={"selected_skill": "produce_iron_plate"},
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            summary = controller.run_autopilot_loop(cycles=2, sleep_seconds=0)
            lines = summary.log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertTrue(summary.ok)
        self.assertEqual(summary.cycles, 2)
        self.assertEqual(controller.calls, 2)
        self.assertEqual(len(lines), 2)

    def test_autopilot_commit_reuses_strategy_target_metadata(self):
        class FakeController(FactorioController):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.calls: list[dict[str, object]] = []
                self.observe_calls = 0

            def observe(self):
                self.observe_calls += 1
                return {
                    "inventory": {"transport-belt": self.observe_calls},
                    "entities": [],
                    "resources": [],
                    "research": {"technologies": {"automation": {"researched": True}}},
                }

            def run_strategy_step(self, **kwargs):
                self.calls.append(dict(kwargs))
                selected = str(kwargs.get("override_skill") or "bootstrap_build_item_mall")
                strategy = {
                    "selected_skill": selected,
                    "target_item": str(kwargs.get("target_item") or "transport-belt"),
                    "target_count": int(kwargs.get("target_count") or 104),
                }
                return StrategyStepSummary(
                    ok=True,
                    reason="done",
                    objective=kwargs.get("objective", "launch_rocket_program"),
                    selected_skill=selected,
                    strategy=strategy,
                    run=RunSummary(True, "stubbed", 1, 0, self.cfg.log_dir / "stub.log", "transport-belt"),
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            with patch.dict(
                "os.environ",
                {
                    "FACTORIO_AI_AUTOPILOT_COMMIT_SKILL_ENABLED": "1",
                    "FACTORIO_AI_AUTOPILOT_COMMIT_SKILL_MAX": "4",
                },
            ):
                summary = controller.run_autopilot_loop(cycles=3, sleep_seconds=0)

        self.assertTrue(summary.ok)
        self.assertEqual(len(controller.calls), 3)
        self.assertEqual(controller.calls[2]["override_skill"], "bootstrap_build_item_mall")
        self.assertEqual(controller.calls[2]["target_count"], 104)
        self.assertEqual(controller.calls[2]["target_item"], "transport-belt")

    def test_modless_strategy_step_accepts_committed_target_metadata(self):
        captured: dict[str, object] = {}

        class FakeController(ModlessFactorioController):
            def _run_skill(self, **kwargs):
                captured.update(kwargs)
                return RunSummary(True, "stubbed", 1, 0, self.cfg.log_dir / "stub.log", kwargs.get("target_item", ""))

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            summary = controller.run_strategy_step(
                "launch_rocket_program",
                override_skill="bootstrap_build_item_mall",
                target_count=104,
                target_item="transport-belt",
            )

        self.assertTrue(summary.ok)
        self.assertEqual(captured["goal"], "bootstrap_build_item_mall")
        self.assertEqual(captured["target"], 104)
        self.assertEqual(captured["target_item"], "transport-belt")

    def test_finite_autopilot_loop_reports_failed_cycle(self):
        class FakeController(FactorioController):
            def run_strategy_step(self, **kwargs):
                return StrategyStepSummary(
                    ok=False,
                    reason="executor missing",
                    objective=kwargs.get("objective", "launch_rocket_program"),
                    selected_skill="missing",
                    strategy={},
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            summary = controller.run_autopilot_loop(cycles=1, sleep_seconds=0)
        self.assertFalse(summary.ok)
        self.assertEqual(summary.failures, 1)

    def test_strict_require_llm_autopilot_does_not_degrade_to_heuristic(self):
        observation = {"inventory": {}, "entities": [], "resources": [], "research": {"technologies": {}}}

        class FakeController(FactorioController):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.calls = []

            def observe(self):
                return observation

            def run_strategy_step(self, **kwargs):
                self.calls.append(kwargs)
                return StrategyStepSummary(
                    ok=False,
                    reason="LLM response content is not a JSON object",
                    objective=kwargs.get("objective", "launch_rocket_program"),
                    selected_skill="",
                    strategy={},
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = FakeController(make_test_config(Path(temp_dir)))
            with patch.dict(
                "os.environ",
                {
                    "FACTORIO_AI_AUTOPILOT_LLM_DEGRADE_CYCLES": "1",
                    "FACTORIO_AI_ALLOW_HEURISTIC_AUTOPILOT_FALLBACK": "0",
                },
            ):
                summary = controller.run_autopilot_loop(cycles=2, sleep_seconds=0, require_llm=True)

        self.assertFalse(summary.ok)
        self.assertEqual(len(controller.calls), 2)
        self.assertTrue(all(call["require_llm"] for call in controller.calls))
        self.assertTrue(all(not call["skip_remote_strategy"] for call in controller.calls))


class StallWatchdogTests(unittest.TestCase):
    def test_progress_fingerprint_changes_with_research(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = FactorioController(make_test_config(Path(tmp)))
            before = controller._progress_fingerprint(
                {"inventory": {}, "entities": [], "research": {"technologies": {"automation": {"researched": False}}}}
            )
            after = controller._progress_fingerprint(
                {"inventory": {}, "entities": [], "research": {"technologies": {"automation": {"researched": True}}}}
            )
            self.assertNotEqual(before, after)

    def test_progress_fingerprint_tracks_power_pole_mall_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = FactorioController(make_test_config(Path(tmp)))
            base = {
                "inventory": {},
                "entities": [],
                "research": {"technologies": {"automation": {"researched": True}}},
            }
            before = controller._progress_fingerprint(base)
            after = controller._progress_fingerprint(
                {
                    "inventory": {},
                    "entities": [
                        {
                            "name": "wooden-chest",
                            "unit_number": 1,
                            "position": {"x": 0, "y": 0},
                            "inventories": {"1": {"small-electric-pole": 20}},
                        }
                    ],
                    "research": base["research"],
                }
            )

        self.assertNotEqual(before, after)

    def test_progress_fingerprint_tracks_power_pole_mall_bootstrap_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = FactorioController(make_test_config(Path(tmp)))
            before = controller._progress_fingerprint(
                {"inventory": {}, "entities": [], "research": {"technologies": {"automation": {"researched": True}}}}
            )
            after = controller._progress_fingerprint(
                {
                    "inventory": {"wood": 4, "copper-cable": 8},
                    "entities": [],
                    "research": {"technologies": {"automation": {"researched": True}}},
                }
            )

        self.assertNotEqual(before, after)

    def test_ongoing_research_override_keeps_electric_drill_research_without_llm(self):
        observation = {
            "inventory": {},
            "entities": [],
            "research": {
                "current": "electric-mining-drill",
                "progress": 0.32,
                "technologies": {"electric-mining-drill": {"researched": False}},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            controller = FactorioController(make_test_config(Path(tmp)))
            with patch(
                "factorio_ai.controller.heuristic_strategy",
                return_value={"selected_skill": "setup_coal_supply"},
            ) as heuristic:
                skill = controller._ongoing_research_override_skill("launch_rocket_program", observation)

        self.assertEqual(skill, "research_electric_mining_drill")
        heuristic.assert_not_called()

    def test_ongoing_research_override_does_not_fight_dependency_replan(self):
        observation = {
            "inventory": {},
            "entities": [],
            "research": {
                "current": "logistics",
                "progress": 0.05,
                "technologies": {"logistics": {"researched": False}},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            controller = FactorioController(make_test_config(Path(tmp)))
            with patch(
                "factorio_ai.controller.heuristic_strategy",
                return_value={"selected_skill": "research_electric_mining_drill"},
            ):
                skill = controller._ongoing_research_override_skill("launch_rocket_program", observation)

        self.assertIsNone(skill)

    def test_stall_recovery_returns_skill_other_than_stalled_one(self):
        observation = {
            "player": {"position": {"x": 0, "y": 0}},
            "inventory": {"iron-plate": 22, "coal": 1},
            "entities": [{"name": "stone-furnace", "position": {"x": 4, "y": 0}, "inventories": {}}],
            "resources": [{"name": "coal", "position": {"x": 8, "y": 0}, "distance": 8}],
            "research": {"technologies": {"automation": {"researched": False}}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            controller = FactorioController(make_test_config(Path(tmp)))
            skill = controller._stall_recovery_skill("launch_rocket_program", observation, ["produce_iron_plate"])
            self.assertTrue(skill)
            self.assertNotEqual(skill, "produce_iron_plate")

    def test_stall_recovery_bootstraps_iron_when_production_dead(self):
        # No furnace producing iron-plate and no drill mining iron-ore while an iron patch is reachable
        # (the classic stranded-drill iron death). Recovery must force the direct iron cell to break the
        # iron<-belts<-mall<-iron deadlock, even when the strategy keeps looping a downstream skill.
        observation = {
            "player": {"position": {"x": 0, "y": 0}},
            "inventory": {"burner-mining-drill": 1, "stone-furnace": 1, "coal": 5},
            "entities": [
                {"name": "burner-mining-drill", "position": {"x": 70, "y": 67}, "status_name": "no_minable_resources"},
            ],
            "resources": [{"name": "iron-ore", "position": {"x": 56, "y": 68}, "distance": 75}],
            "research": {"technologies": {"automation": {"researched": True}}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            controller = FactorioController(make_test_config(Path(tmp)))
            skill = controller._stall_recovery_skill(
                "launch_rocket_program", observation, ["build_gear_belt_mall_logistics"]
            )
            self.assertEqual(skill, "produce_iron_plate")

    def test_stall_recovery_builds_item_mall_when_gear_belt_layout_missing(self):
        # build_gear_belt_mall_logistics only WIRES existing gear+belt assemblers; with none present it
        # loops forever. Recovery must pick bootstrap_build_item_mall (which builds them). Iron is not
        # "dead" here (no iron-ore patch in view) so rule (1) does not pre-empt.
        observation = {
            "player": {"position": {"x": 0, "y": 0}},
            "inventory": {"iron-plate": 50, "coal": 10},
            # An iron-plate furnace is present so iron production is NOT dead -> rule (1) does not pre-empt.
            "entities": [{"name": "stone-furnace", "recipe": "iron-plate", "position": {"x": 4, "y": 0}}],
            "resources": [{"name": "coal", "position": {"x": 8, "y": 0}, "distance": 8}],
            "research": {"technologies": {"automation": {"researched": True}}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            controller = FactorioController(make_test_config(Path(tmp)))
            skill = controller._stall_recovery_skill(
                "launch_rocket_program", observation, ["build_gear_belt_mall_logistics"]
            )
            self.assertEqual(skill, "bootstrap_build_item_mall")

    def test_stall_recovery_bootstraps_when_existing_belt_mall_is_not_logistics_pair(self):
        observation = {
            "player": {"position": {"x": 0, "y": 0}, "character_valid": False},
            "inventory": {"iron-plate": 50, "coal": 10},
            "entities": [
                {
                    "name": "assembling-machine-1",
                    "unit_number": 10,
                    "recipe": "iron-gear-wheel",
                    "position": {"x": 0, "y": 0},
                    "electric_network_connected": True,
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 11,
                    "recipe": "transport-belt",
                    "position": {"x": 0, "y": -3},
                    "electric_network_connected": True,
                },
                {"name": "stone-furnace", "recipe": "iron-plate", "position": {"x": 4, "y": 0}},
            ],
            "resources": [{"name": "coal", "position": {"x": 8, "y": 0}, "distance": 8}],
            "research": {"technologies": {"automation": {"researched": True}}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            controller = FactorioController(make_test_config(Path(tmp)))
            skill = controller._stall_recovery_skill(
                "launch_rocket_program", observation, ["build_gear_belt_mall_logistics"]
            )
            self.assertEqual(skill, "bootstrap_build_item_mall")

    def test_stall_recovery_keeps_root_repair_even_if_recently_tried(self):
        observation = {
            "player": {"position": {"x": 0, "y": 0}},
            "inventory": {"iron-plate": 50, "coal": 10},
            "entities": [{"name": "stone-furnace", "recipe": "iron-plate", "position": {"x": 4, "y": 0}}],
            "resources": [{"name": "coal", "position": {"x": 8, "y": 0}, "distance": 8}],
            "research": {"technologies": {"automation": {"researched": True}}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            controller = FactorioController(make_test_config(Path(tmp)))
            skill = controller._stall_recovery_skill(
                "launch_rocket_program",
                observation,
                ["build_gear_belt_mall_logistics", "bootstrap_build_item_mall"],
            )
            self.assertEqual(skill, "bootstrap_build_item_mall")

    def test_stall_recovery_repairs_source_furnace_fuel_before_repeating_belt_mall(self):
        observation = {
            "player": {"position": {"x": 0, "y": 0}},
            "inventory": {"transport-belt": 20, "coal": 2},
            "entities": [
                {
                    "name": "stone-furnace",
                    "unit_number": 1458,
                    "recipe": "iron-plate",
                    "position": {"x": -12, "y": 2},
                    "status_name": "no_fuel",
                    "inventories": {"2": {"iron-ore": 2}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 146,
                    "recipe": "iron-gear-wheel",
                    "position": {"x": 8, "y": 8},
                    "electric_network_connected": True,
                    "status_name": "full_output",
                    "inventories": {"3": {"iron-gear-wheel": 5}},
                },
                {
                    "name": "assembling-machine-1",
                    "unit_number": 1779,
                    "recipe": "transport-belt",
                    "position": {"x": 12, "y": 8},
                    "electric_network_connected": True,
                    "status_name": "item_ingredient_shortage",
                    "inventories": {"2": {"iron-gear-wheel": 3}},
                },
            ],
            "resources": [{"name": "coal", "position": {"x": 2, "y": 0}, "distance": 2}],
            "research": {"technologies": {"automation": {"researched": True}, "logistics": {"researched": True}}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            controller = FactorioController(make_test_config(Path(tmp)))
            skill = controller._stall_recovery_skill(
                "launch_rocket_program",
                observation,
                ["build_gear_belt_mall_logistics", "bootstrap_build_item_mall"],
            )
            self.assertEqual(skill, "build_iron_plate_logistic_line_to_gear_mall")

    def test_both_controllers_run_strategy_step_accept_override_skill(self):
        # run_autopilot_loop (base) always calls run_strategy_step(override_skill=...). Every subclass
        # override of run_strategy_step must accept it, or the no-mod autopilot raises TypeError every
        # cycle. (This regressed ModlessFactorioController.)
        import inspect

        for cls in (FactorioController, ModlessFactorioController):
            params = inspect.signature(cls.run_strategy_step).parameters
            self.assertIn("override_skill", params, msg=f"{cls.__name__}.run_strategy_step missing override_skill")

    def test_override_skill_bypasses_llm_strategy(self):
        observation = {"tick": 1, "inventory": {}, "entities": [], "resources": [], "research": {"technologies": {}}}

        class FakeController(FactorioController):
            def observe(self):
                return observation

            def strategy_decision(self, objective, require_llm=False, skip_remote=False):
                raise AssertionError("strategy_decision must not run when override_skill is set")

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_test_config(Path(tmp))
            controller = FakeController(cfg)
            with patch.object(
                controller,
                "_run_skill",
                return_value=RunSummary(True, "forced ok", 1, 5, cfg.log_dir / "x.log", "coal"),
            ):
                summary = controller.run_strategy_step("launch_rocket_program", override_skill="setup_coal_supply")
            self.assertEqual(summary.selected_skill, "setup_coal_supply")
            self.assertEqual(summary.strategy.get("source"), "autopilot_stall_recovery")


if __name__ == "__main__":
    unittest.main()
