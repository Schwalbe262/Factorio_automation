from __future__ import annotations

import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

from .llm_log import (
    extract_io_traces_from_result,
    record_io_traces,
    record_llm_decision,
    strategy_request_summary,
)
from .config import AppConfig, REPO_ROOT
from .factory_readiness import build_factory_readiness
from .layout_llm_settings import load_layout_llm_settings
from .layout_validation import layout_validation_feedback_summary
from .human_layout_learning import (
    record_human_layout_observation,
    remember_agent_layout_action,
)
from .models import (
    ActionValidationError,
    PlannerDecision,
    distance,
    player_position,
    total_item_count,
    validate_action,
)
from .modless_lua import ModlessLuaController
from .monitor import summarize_factory
from .run_journal import (
    record_autopilot_cycle_journal,
    record_layout_loop_journal,
    record_layout_result_insight,
    record_skill_run_journal,
)
from .planner import (
    AutomationScienceSkill,
    BeltSmeltingLineSkill,
    BuildItemMallSkill,
    CircuitAutomationSkill,
    CoalFuelFeedSkill,
    CoalSupplySkill,
    CopperPlateSkill,
    ElectronicCircuitSkill,
    ExpandCopperSmeltingSkill,
    ExpandIronSmeltingSkill,
    FactoryLayoutImprovementSkill,
    GearBeltMallLogisticsSkill,
    GearBeltMallRelocationSkill,
    IronPlateLogisticLineToGearMallSkill,
    IronPlateSkill,
    ResearchAutomationSkill,
    ResearchTechnologySkill,
    SetupPowerSkill,
    SiteInputLogisticLineSkill,
    StoneSupplySkill,
    StarterDefenseSkill,
)
from .rcon import FactorioRconClient
from .site_selection import load_selected_improvement_site
from .skill_registry import IMPLEMENTED_SKILLS, annotate_strategy_with_skill_status
from .strategy import heuristic_strategy, make_strategy_payload, reconcile_strategy_decision, skill_catalog_payload
from .targets import load_targets
from .world_memory import (
    load_world_map_memory,
    merge_world_map_memory_into_observation,
    planning_sites_are_fresh,
    planning_sites_from_memory,
    update_world_map_memory,
)


LLM_TRACE_RESULT_KEYS = {"llm_trace", "llm_traces"}


# Autopilot stall watchdog: a deterministic safety net. If the run keeps selecting the same skill
# while no real progress signal changes for several cycles, force-rotate to a different deterministic
# skill so it never gets permanently stuck on one already-satisfied skill. This is not learning; it
# just guarantees forward motion using the skills that already exist.
_STALL_PROGRESS_ITEMS = (
    "wood",
    "iron-plate",
    "copper-plate",
    "copper-cable",
    "electronic-circuit",
    "automation-science-pack",
    "transport-belt",
    "iron-gear-wheel",
    "small-electric-pole",
    "assembling-machine-1",
    "inserter",
    "electric-mining-drill",
    "lab",
    "logistic-science-pack",
    "coal",
    "steam",
)
_STALL_ROTATION_SKILLS = (
    # Bootstrap-builder recovery skills come first: the most common stall is the strategy looping a
    # skill whose upstream prerequisite was never built. bootstrap_build_item_mall BUILDS the gear/belt
    # assemblers that build_gear_belt_mall_logistics only WIRES (it loops "cannot find spaced powered
    # gear and reusable belt assemblers" when none exist); produce_iron_plate rebuilds a direct beltless
    # iron cell and relocates a drill stranded off its ore patch when iron production has died.
    "bootstrap_build_item_mall",
    "produce_iron_plate",
    "setup_coal_supply",
    "produce_copper_plate",
    "setup_power",
    "research_automation",
    "automate_electronic_circuit_line",
    "research_logistics",
    "plan_factory_site",
)


def _failure_recovery_threshold() -> int:
    # After this many consecutive failed cycles (ANY skills -- covers the strategy oscillating between
    # several infeasible skills, which the same-skill stall counter misses), force prerequisite recovery.
    try:
        return max(2, int(os.getenv("FACTORIO_AI_AUTOPILOT_FAILURE_RECOVERY_CYCLES", "3")))
    except (TypeError, ValueError):
        return 3


def _stall_threshold() -> int:
    try:
        return max(2, int(os.getenv("FACTORIO_AI_AUTOPILOT_STALL_CYCLES", "3")))
    except (TypeError, ValueError):
        return 3


def _commit_skill_enabled() -> bool:
    # When a skill is actively making progress, reuse it for a few cycles without paying a fresh
    # (slow) LLM strategy call. The LLM still picks every new strategy: we re-strategize the moment
    # progress stalls and at least every _commit_skill_max() cycles. Default on; set to 0 to force an
    # LLM strategy call every cycle.
    return os.getenv("FACTORIO_AI_AUTOPILOT_COMMIT_SKILL", "1").strip().lower() not in {"0", "false", "no", "off"}


def _commit_skill_max() -> int:
    # Max consecutive LLM-skipping cycles before a fresh LLM re-evaluation is forced.
    try:
        return max(1, int(os.getenv("FACTORIO_AI_AUTOPILOT_COMMIT_MAX", "4")))
    except (TypeError, ValueError):
        return 4


try:
    # A skill 'wait' action of at least this many ticks yields the cycle (the agent goes and does
    # other factory-expanding work) instead of idling, since the game fills in real-time anyway.
    # Short settle-waits (< this) keep their brief sleep. Set to a huge value to disable yielding.
    _WAIT_YIELD_TICKS = max(60, int(os.getenv("FACTORIO_AI_AUTOPILOT_WAIT_YIELD_TICKS", "180")))
except (TypeError, ValueError):
    _WAIT_YIELD_TICKS = 180


def _llm_degrade_threshold() -> int:
    # After this many consecutive failed cycles (remote LLM hang/error, RecursionError, or a refusal
    # loop), run ONE cycle on the deterministic heuristic strategy so the factory keeps progressing
    # instead of freezing on an unstable serving (require-llm otherwise raises and the agent stalls).
    try:
        return max(1, int(os.getenv("FACTORIO_AI_AUTOPILOT_LLM_DEGRADE_CYCLES", "2")))
    except (TypeError, ValueError):
        return 2


def _llm_degrade_cooldown_cycles() -> int:
    # Once degraded, stay on the heuristic for this many cycles before retrying the (broken) remote.
    # Each failed remote retry costs ~100-540s (RecursionError round-trip or a hung-serving timeout),
    # so retrying every cycle wastes most wall-clock; a cooldown keeps the factory on the fast
    # heuristic path while still periodically probing whether the serving recovered. 0 disables it.
    try:
        return max(0, int(os.getenv("FACTORIO_AI_AUTOPILOT_LLM_DEGRADE_COOLDOWN", "5")))
    except (TypeError, ValueError):
        return 5


def _heuristic_autopilot_fallback_allowed() -> bool:
    return os.getenv("FACTORIO_AI_ALLOW_HEURISTIC_AUTOPILOT_FALLBACK", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


# Self-repair: when a hand-written skill fails this many times in a row, enqueue the local LLM to
# generate a sandbox-gated OVERRIDE that replaces it (auto-rollback to the hand-written one on regress).
def _skill_repair_enabled() -> bool:
    return os.getenv("FACTORIO_AI_SKILL_REPAIR_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _impl_repair_fail_limit() -> int:
    try:
        return max(2, int(os.getenv("FACTORIO_AI_IMPL_REPAIR_FAIL_LIMIT", "3")))
    except (TypeError, ValueError):
        return 3


def _repair_core_denylist() -> set[str]:
    raw = os.getenv("FACTORIO_AI_SKILL_REPAIR_CORE_DENYLIST", "").strip()
    return {item.strip() for item in raw.split(",") if item.strip()}


# --------------------------------------------------------------------------- #
# Self-augmenting failure diagnostics
# When a skill fails, these augmenters inspect the live observation and pull out
# the context that explains the failure (the "missing observation"), which is fed
# to the self-repair codegen so the generated fix can act on it. New gaps are
# supported by registering another augmenter here -- no other wiring changes.
# --------------------------------------------------------------------------- #


def _placement_obstacle_augmenter(
    skill_name: str, reasons: list[str], observation: dict[str, Any]
) -> dict[str, Any] | None:
    """'cannot place entity' -> summarize the trees/rocks/cliffs blocking placement."""
    text = " ".join(str(r) for r in reasons).lower()
    if not any(k in text for k in ("cannot place", "place entity", "no valid position", "no position", "blocked tile")):
        return None
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    counts = {"tree": 0, "rock": 0, "cliff": 0}
    samples: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        etype = str(entity.get("type") or "")
        name = str(entity.get("name") or "")
        if etype == "tree":
            kind = "tree"
        elif etype == "cliff":
            kind = "cliff"
        elif etype == "simple-entity" or name.endswith("rock"):
            kind = "rock"
        else:
            continue
        counts[kind] += 1
        if len(samples) < 8:
            samples.append({"name": name or etype, "position": entity.get("position")})
    if not any(counts.values()):
        return None
    return {
        "missing_observation": "placement_obstacles",
        "obstacle_counts": counts,
        "nearby_obstacles": samples,
        "hint": (
            "'cannot place entity' is caused by these obstacles. Clear a tree/rock with a {'type':'mine',...} action, "
            "or scan outward for the nearest unobstructed tile, before building. Never retry the same blocked tile."
        ),
    }


_POWER_GENERATORS = {"steam-engine", "solar-panel", "steam-turbine"}


def _power_dependent_skills() -> set[str]:
    raw = os.getenv("FACTORIO_AI_POWER_DEPENDENT_SKILLS", "research_automation").strip()
    return {item.strip() for item in raw.split(",") if item.strip()}


def _power_health_augmenter(
    skill_name: str, reasons: list[str], observation: dict[str, Any]
) -> dict[str, Any] | None:
    """Root-cause signal: a power-dependent skill failing while electricity is dead.

    'broken' = no generator placed, OR generators exist but none is connected to an
    electric network (the misaligned offshore-pump/boiler/steam-engine case). Emits a
    root_cause_skill so the controller also repairs setup_power, not just the symptom.
    """
    if skill_name not in _power_dependent_skills():
        return None
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    generators = [e for e in entities if isinstance(e, dict) and str(e.get("name") or "") in _POWER_GENERATORS]
    connected = [e for e in generators if e.get("electric_network_connected")]
    if connected:
        return None  # at least one generator is feeding the grid -> power is fine
    if not generators:
        hint = "no power generator (steam-engine/solar-panel) is placed; setup_power must build one."
    else:
        hint = (
            "power generators exist but NONE is connected to an electric network (misaligned "
            "offshore-pump/boiler/steam-engine or unwired poles); setup_power must fix the alignment/wiring."
        )
    return {
        "missing_observation": "power_health",
        "broken": True,
        "generator_count": len(generators),
        "connected_generator_count": len(connected),
        "root_cause_skill": "setup_power",
        "hint": hint,
    }


_OBSERVATION_AUGMENTERS = [_placement_obstacle_augmenter, _power_health_augmenter]


def _failure_diagnostics(skill_name: str, reasons: list[str], observation: dict[str, Any]) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    if not isinstance(observation, dict):
        return diagnostics
    for augmenter in _OBSERVATION_AUGMENTERS:
        try:
            result = augmenter(skill_name, reasons, observation)
        except Exception:  # noqa: BLE001 - diagnostics are best-effort, never break repair
            result = None
        if isinstance(result, dict) and result:
            key = str(result.get("missing_observation") or getattr(augmenter, "__name__", "augmenter"))
            diagnostics[key] = result
    return diagnostics


@dataclass
class RunSummary:
    ok: bool
    reason: str
    steps: int
    item_count: int
    log_path: Path
    item_name: str
    seed_count: int = 0

    @property
    def iron_plate_count(self) -> int:
        return self.item_count

    @property
    def copper_plate_count(self) -> int:
        return self.item_count

    @property
    def electronic_circuit_count(self) -> int:
        return self.item_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "steps": self.steps,
            "itemCount": self.item_count,
            "itemName": self.item_name,
            "logPath": str(self.log_path),
            "seedCount": self.seed_count,
        }


@dataclass
class StrategyStepSummary:
    ok: bool
    reason: str
    objective: str
    selected_skill: str
    strategy: dict[str, Any]
    run: RunSummary | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "objective": self.objective,
            "selectedSkill": self.selected_skill,
            "strategy": self.strategy,
            "run": self.run.to_dict() if self.run else None,
        }


@dataclass
class AutopilotLoopSummary:
    ok: bool
    reason: str
    objective: str
    cycles: int
    log_path: Path
    last_step: StrategyStepSummary | None = None
    failures: int = 0
    interrupted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "objective": self.objective,
            "cycles": self.cycles,
            "logPath": str(self.log_path),
            "lastStep": self.last_step.to_dict() if self.last_step else None,
            "failures": self.failures,
            "interrupted": self.interrupted,
        }


@dataclass
class CodexWaitLayoutLoopSummary:
    ok: bool
    reason: str
    objective: str
    cycles: int
    log_path: Path
    active_skill: str = ""
    wait_active: bool = False
    interrupted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "objective": self.objective,
            "cycles": self.cycles,
            "logPath": str(self.log_path),
            "activeSkill": self.active_skill,
            "waitActive": self.wait_active,
            "interrupted": self.interrupted,
        }


@dataclass
class IdleLayoutLoopSummary:
    ok: bool
    reason: str
    objective: str
    cycles: int
    idle_cycles: int
    busy_cycles: int
    log_path: Path
    interrupted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "objective": self.objective,
            "cycles": self.cycles,
            "idleCycles": self.idle_cycles,
            "busyCycles": self.busy_cycles,
            "logPath": str(self.log_path),
            "interrupted": self.interrupted,
        }


_PLANNING_SITE_RETRY_MARKERS = (
    "cannot find a buildable water site",
    "cannot find a powered or wireable lab site",
    "cannot find a powered or wireable site",
)
DEFAULT_PLANNING_SITE_CACHE_SECONDS = 180.0
DEFAULT_PLANNING_SITE_CACHE_TICK_DRIFT = 600
GEAR_HANDCRAFT_BLOCKING_ASSEMBLER_NAMES = {"assembling-machine-1", "assembling-machine-2", "assembling-machine-3"}


def _planning_site_retry_needed(decision: PlannerDecision) -> bool:
    if decision.done or decision.action is not None:
        return False
    reason = decision.reason.lower()
    return any(marker in reason for marker in _PLANNING_SITE_RETRY_MARKERS)


def _planning_site_cache_seconds() -> float:
    try:
        return max(
            0.0,
            float(os.getenv("FACTORIO_AI_PLANNING_SITE_CACHE_SECONDS", str(DEFAULT_PLANNING_SITE_CACHE_SECONDS))),
        )
    except (TypeError, ValueError):
        return DEFAULT_PLANNING_SITE_CACHE_SECONDS


def _planning_site_cache_tick_stale(cached_tick: Any, observed_tick: Any) -> bool:
    if not isinstance(cached_tick, (int, float)) or not isinstance(observed_tick, (int, float)):
        return False
    return abs(float(observed_tick) - float(cached_tick)) > DEFAULT_PLANNING_SITE_CACHE_TICK_DRIFT


def _automation_researched_in_observation(observation: dict[str, Any]) -> bool:
    research = observation.get("research")
    if not isinstance(research, dict):
        return False
    technologies = research.get("technologies")
    if not isinstance(technologies, dict):
        return False
    automation = technologies.get("automation")
    return bool(isinstance(automation, dict) and automation.get("researched"))


def _gear_handcraft_automation_context_in_observation(observation: dict[str, Any]) -> bool:
    if _automation_researched_in_observation(observation):
        return True
    inventory = observation.get("inventory")
    if isinstance(inventory, dict) and int(inventory.get("assembling-machine-1") or 0) > 0:
        return True
    for entity in observation.get("entities") or []:
        if isinstance(entity, dict) and str(entity.get("name") or "") in GEAR_HANDCRAFT_BLOCKING_ASSEMBLER_NAMES:
            return True
    return False


def _gear_handcraft_blocking_assembler_available(observation: dict[str, Any]) -> bool:
    inventory = observation.get("inventory")
    if isinstance(inventory, dict) and int(inventory.get("assembling-machine-1") or 0) > 0:
        return True
    for entity in observation.get("entities") or []:
        if isinstance(entity, dict) and str(entity.get("name") or "") in GEAR_HANDCRAFT_BLOCKING_ASSEMBLER_NAMES:
            return True
    return False


def _allow_first_assembler_bootstrap_gears(observation: dict[str, Any], action: dict[str, Any]) -> bool:
    if action.get("allow_first_assembler_bootstrap") is not True:
        return False
    if _gear_handcraft_blocking_assembler_available(observation):
        return False
    try:
        count = max(1, int(action.get("count") or 1))
    except (TypeError, ValueError):
        return False
    if count > 5:
        return False
    inventory = observation.get("inventory")
    if not isinstance(inventory, dict):
        return False
    current_gears = int(inventory.get("iron-gear-wheel") or 0)
    if current_gears + count > 5:
        return False
    return int(inventory.get("electronic-circuit") or 0) >= 3 and int(inventory.get("iron-plate") or 0) >= 9 + (2 * count)


def _allow_gear_belt_direct_transfer_bootstrap_gears(observation: dict[str, Any], action: dict[str, Any]) -> bool:
    if action.get("allow_gear_belt_direct_transfer_bootstrap") is not True:
        return False
    try:
        count = max(1, int(action.get("count") or 1))
    except (TypeError, ValueError):
        return False
    if count > 1:
        return False
    entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
    gear_assemblers = [
        entity
        for entity in entities
        if isinstance(entity, dict)
        and str(entity.get("name") or "") == "assembling-machine-1"
        and entity.get("recipe") == "iron-gear-wheel"
        and entity.get("electric_network_connected") is not False
        and isinstance(entity.get("position"), dict)
    ]
    belt_assemblers = [
        entity
        for entity in entities
        if isinstance(entity, dict)
        and str(entity.get("name") or "") == "assembling-machine-1"
        and entity.get("recipe") == "transport-belt"
        and entity.get("electric_network_connected") is not False
        and isinstance(entity.get("position"), dict)
    ]
    return any(
        abs(float(gear["position"].get("y") or 0.0) - float(belt["position"].get("y") or 0.0)) <= 0.25
        and 3.75 <= abs(float(gear["position"].get("x") or 0.0) - float(belt["position"].get("x") or 0.0)) <= 4.25
        for gear in gear_assemblers
        for belt in belt_assemblers
    )


def _gear_handcraft_guard_reason(observation: dict[str, Any], action: dict[str, Any]) -> str:
    if (
        action.get("type") == "craft"
        and action.get("recipe") == "iron-gear-wheel"
        and _gear_handcraft_automation_context_in_observation(observation)
    ):
        if _allow_first_assembler_bootstrap_gears(observation, action):
            return ""
        if _allow_gear_belt_direct_transfer_bootstrap_gears(observation, action):
            return ""
        if _gear_handcraft_virtual_agent_allowed(observation):
            return ""
        return "blocked direct iron-gear-wheel handcraft after assembler automation exists; use gear mall or a logistic line instead"
    return ""


def _gear_handcraft_virtual_agent_allowed(observation: dict[str, Any]) -> bool:
    """The virtual RCON server agent teleports and hand-craft is instant/free, so the anti-handcraft
    guard (which exists to push a WALKING player toward automation) only DEADLOCKS it -- observed live
    as a 487-step ``wait`` loop. Allow gear hand-craft for it; real (walking) players keep the guard."""
    player = observation.get("player") if isinstance(observation.get("player"), dict) else {}
    execution = observation.get("execution") if isinstance(observation.get("execution"), dict) else {}
    return (
        player.get("character_valid") is False
        or execution.get("virtual") is True
        or str(player.get("kind") or "") == "server"
    )


def _guard_post_automation_handcraft(observation: dict[str, Any], decision: PlannerDecision) -> PlannerDecision:
    action = decision.action
    if isinstance(action, dict) and _gear_handcraft_guard_reason(observation, action):
        return PlannerDecision(
            {"type": "wait", "ticks": 120},
            _gear_handcraft_guard_reason(observation, action),
        )
    return decision


def _bootstrap_seed_action_key(action: dict[str, Any] | None) -> tuple[Any, ...] | None:
    if not isinstance(action, dict) or action.get("bootstrap_seed") is not True:
        return None
    return (
        action.get("type"),
        action.get("recipe") or action.get("item"),
        action.get("count"),
        action.get("unit_number"),
        action.get("name"),
        action.get("seed_reason"),
    )


def _bootstrap_seed_followup_item(action: dict[str, Any] | None) -> str | None:
    if not isinstance(action, dict) or action.get("bootstrap_seed") is not True:
        return None
    text = " ".join(
        str(action.get(key) or "")
        for key in ("seed_reason", "expected_followup", "recipe", "item")
    )
    if "transport-belt" in text or "belt output" in text:
        return "transport-belt"
    if "assembling-machine-1" in text:
        return "assembling-machine-1"
    if "inserter" in text:
        return "inserter"
    if "iron-gear-wheel" in text or "gear" in text:
        return "iron-gear-wheel"
    return None


def _record_and_strip_llm_io_traces(log_dir: Path, result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return result
    traces = extract_io_traces_from_result(result)
    if not traces and not any(key in result for key in LLM_TRACE_RESULT_KEYS):
        return result

    stripped = {key: value for key, value in result.items() if key not in LLM_TRACE_RESULT_KEYS}
    trace_ids, errors = record_io_traces(log_dir, traces)
    if trace_ids:
        stripped["llm_trace_ids"] = trace_ids
    if errors:
        stripped["llm_trace_record_error"] = "; ".join(errors)
    return stripped


def _local_llm_env_configured() -> bool:
    return bool(os.getenv("FACTORIO_AI_LLM_BASE_URL", "").strip() and os.getenv("FACTORIO_AI_LLM_MODEL", "").strip())


class FactorioController:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._remote_llm_status_cache: dict[str, Any] | None = None
        self._remote_llm_status_cache_until = 0.0
        self._background_layout_task_name: str | None = None
        self._background_layout_last_submit = 0.0
        self._background_layout_scheduler_not_ready_until = 0.0
        self._background_layout_thread: threading.Thread | None = None
        self._background_layout_thread_result: dict[str, Any] | None = None
        self._background_layout_threads: list[threading.Thread] = []
        self._background_layout_thread_results: list[dict[str, Any]] = []
        self._background_layout_thread_result_lock = threading.Lock()

    def observe(self) -> dict[str, Any]:
        with self._client() as client:
            response = client.execute_json_command("ai_observe", self._agent_parameter())
        if not response.get("ok"):
            raise RuntimeError(f"observe failed: {response}")
        return response

    def act(self, action: dict[str, Any]) -> dict[str, Any]:
        validate_action(action)
        action = self._agent_action(action)
        with self._client() as client:
            response = client.execute_json_command("ai_action", action)
        return response

    def stop_agent(self) -> dict[str, Any]:
        try:
            response = self.act({"type": "stop"})
            if response.get("ok"):
                return response
        except Exception as exc:
            response = {"ok": False, "reason": str(exc)}

        observation = self.observe()
        current_position = player_position(observation)
        fallback = self.act({"type": "move_to", "position": current_position})
        fallback["fallbackFor"] = "stop"
        fallback["previousStopResponse"] = response
        return fallback

    def wait(self, ticks: int) -> dict[str, Any]:
        with self._client() as client:
            response = client.execute_json_command("ai_wait", str(ticks))
        time.sleep(max(0.05, ticks / 60.0))
        return response

    def run_iron_mvp(
        self,
        target: int = 10,
        max_steps: int = 200,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=IronPlateSkill(target),
            target_item="iron-plate",
            target=target,
            goal="produce_iron_plate",
            max_steps=max_steps,
            log_prefix="iron-mvp",
            log_path=log_path,
        )

    def run_science_mvp(
        self,
        target: int = 5,
        max_steps: int = 400,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=AutomationScienceSkill(target),
            target_item="automation-science-pack",
            target=target,
            goal="produce_automation_science_pack",
            max_steps=max_steps,
            log_prefix="science-mvp",
            log_path=log_path,
        )

    def run_copper_mvp(
        self,
        target: int = 10,
        max_steps: int = 250,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=CopperPlateSkill(target),
            target_item="copper-plate",
            target=target,
            goal="produce_copper_plate",
            max_steps=max_steps,
            log_prefix="copper-mvp",
            log_path=log_path,
        )

    def run_circuit_mvp(
        self,
        target: int = 5,
        max_steps: int = 500,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=ElectronicCircuitSkill(target),
            target_item="electronic-circuit",
            target=target,
            goal="produce_electronic_circuit",
            max_steps=max_steps,
            log_prefix="circuit-mvp",
            log_path=log_path,
        )

    def run_belt_smelting_mvp(
        self,
        target: int = 10,
        max_steps: int = 700,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=BeltSmeltingLineSkill(target),
            target_item="iron-plate",
            target=target,
            goal="build_belt_smelting_line",
            max_steps=max_steps,
            log_prefix="belt-smelting-mvp",
            log_path=log_path,
        )

    def run_power_mvp(
        self,
        max_steps: int = 900,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=SetupPowerSkill(),
            target_item="steam",
            target=1,
            goal="setup_power",
            max_steps=max_steps,
            log_prefix="power-mvp",
            log_path=log_path,
        )

    def run_automation_research_mvp(
        self,
        max_steps: int = 1500,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=ResearchAutomationSkill(),
            target_item="automation-science-pack",
            target=10,
            goal="research_automation",
            max_steps=max_steps,
            log_prefix="automation-research-mvp",
            log_path=log_path,
        )

    def run_circuit_automation_mvp(
        self,
        target: int = 5,
        max_steps: int = 1800,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=CircuitAutomationSkill(target),
            target_item="electronic-circuit",
            target=target,
            goal="automate_electronic_circuit_line",
            max_steps=max_steps,
            log_prefix="circuit-automation-mvp",
            log_path=log_path,
        )

    def run_logistics_research_mvp(
        self,
        max_steps: int = 2200,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=ResearchTechnologySkill("logistics"),
            target_item="automation-science-pack",
            target=20,
            goal="research_logistics",
            max_steps=max_steps,
            log_prefix="logistics-research-mvp",
            log_path=log_path,
        )

    def run_build_item_mall_mvp(
        self,
        target_item: str = "transport-belt",
        target: int = 20,
        max_steps: int = 1200,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=BuildItemMallSkill(target_item, target),
            target_item=target_item,
            target=target,
            goal="bootstrap_build_item_mall",
            max_steps=max_steps,
            log_prefix=f"build-item-mall-{target_item}",
            log_path=log_path,
        )

    def run_expand_iron_smelting_mvp(
        self,
        target_rate: int = 90,
        max_steps: int = 2000,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=ExpandIronSmeltingSkill(float(target_rate)),
            target_item="iron-plate",
            target=target_rate,
            goal="expand_iron_smelting",
            max_steps=max_steps,
            log_prefix="expand-iron-smelting-mvp",
            log_path=log_path,
        )

    def run_expand_copper_smelting_mvp(
        self,
        target_rate: int = 75,
        max_steps: int = 1600,
        log_path: Path | None = None,
    ) -> RunSummary:
        return self._run_skill(
            skill=ExpandCopperSmeltingSkill(float(target_rate)),
            target_item="copper-plate",
            target=target_rate,
            goal="expand_copper_smelting",
            max_steps=max_steps,
            log_prefix="expand-copper-smelting-mvp",
            log_path=log_path,
        )

    def strategy_decision(
        self, objective: str, require_llm: bool = False, skip_remote: bool = False
    ) -> dict[str, Any]:
        observation = self.observe()
        production_targets = load_targets(self.cfg.runtime_dir, objective).per_minute
        selected_improvement_site = load_selected_improvement_site(self.cfg.runtime_dir, objective)
        request_summary = strategy_request_summary(observation, production_targets)
        result: dict[str, Any] | None = None
        force_heuristic = (
            not require_llm
            and os.getenv("FACTORIO_AI_FORCE_HEURISTIC_STRATEGY", "").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        # skip_remote forces the deterministic heuristic path without even touching the remote.
        # Used by the graceful-degradation cycle: _should_try_remote_strategy returns True whenever
        # slurm is enabled (regardless of require_llm), so a degraded cycle would otherwise still
        # call the remote and block on its full ~540s timeout when the serving hangs.
        use_remote_strategy = (not skip_remote) and (not force_heuristic) and self._should_try_remote_strategy(require_llm)
        if use_remote_strategy:
            if self.cfg.slurm_enabled:
                self._maybe_ensure_slurm_worker(reason="strategy_decision")
            started = time.monotonic()
            try:
                status = self._remote_llm_status(refresh=True)
                if not status.get("llm_ready"):
                    reason = self._remote_llm_unready_reason(status)
                    record_llm_decision(
                        self.cfg.log_dir,
                        objective=objective,
                        provider="remote_slurm",
                        result={"source": "remote_unavailable", "ok": False},
                        request_summary=request_summary,
                        error=reason,
                        latency_ms=int((time.monotonic() - started) * 1000),
                    )
                    if require_llm:
                        raise RuntimeError(f"remote Slurm LLM not ready: {reason}")
                else:
                    from . import remote_slurm

                    result = remote_slurm.request_strategy(
                        objective=objective,
                        observation=observation,
                        production_targets=production_targets,
                        selected_improvement_site=selected_improvement_site,
                        available_skills=skill_catalog_payload(),
                        timeout_seconds=_remote_strategy_timeout_seconds(),
                    )
                    result = _record_and_strip_llm_io_traces(self.cfg.log_dir, result)
                    record_llm_decision(
                        self.cfg.log_dir,
                        objective=objective,
                        provider="remote_slurm",
                        result=result,
                        request_summary=request_summary,
                        error="" if result.get("source") == "llm" else "remote Slurm returned non-LLM fallback result",
                        latency_ms=int((time.monotonic() - started) * 1000),
                    )
            except Exception as exc:
                if require_llm and str(exc).startswith("remote Slurm LLM not ready:"):
                    raise
                record_llm_decision(
                    self.cfg.log_dir,
                    objective=objective,
                    provider="remote_slurm",
                    result=None,
                    request_summary=request_summary,
                    error=f"remote Slurm strategy request failed: {type(exc).__name__}: {exc}",
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                if require_llm:
                    raise

        if result is None and (skip_remote or force_heuristic):
            # Degraded cycle: skip BOTH the remote and the local LLM and go straight to the
            # deterministic heuristic so a hung/erroring serving can't stall the factory.
            result = heuristic_strategy(
                objective,
                observation,
                production_targets,
                selected_improvement_site=selected_improvement_site,
            )
            result["source"] = "heuristic"
            result["ok"] = True
            record_llm_decision(
                self.cfg.log_dir,
                objective=objective,
                provider="heuristic_fallback",
                result=result,
                request_summary=request_summary,
                error=(
                    "forced heuristic strategy; skipped remote/local LLM"
                    if force_heuristic
                    else "degraded: skipped remote/local LLM, used heuristic strategy"
                ),
                latency_ms=0,
            )

        if result is None:
            started = time.monotonic()
            try:
                from .slurm_worker import run_strategy_request

                result = run_strategy_request(
                    make_strategy_payload(
                        objective,
                        observation,
                        production_targets,
                        selected_improvement_site=selected_improvement_site,
                    )
                )
                result = _record_and_strip_llm_io_traces(self.cfg.log_dir, result)
                record_llm_decision(
                    self.cfg.log_dir,
                    objective=objective,
                    provider="local_llm",
                    result=result,
                    request_summary=request_summary,
                    error="" if result.get("source") == "llm" else "LLM unavailable or invalid response; used heuristic fallback",
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            except Exception as exc:
                record_llm_decision(
                    self.cfg.log_dir,
                    objective=objective,
                    provider="local_llm",
                    result=None,
                    request_summary=request_summary,
                    error=f"local LLM strategy request failed: {type(exc).__name__}: {exc}",
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                if require_llm:
                    raise
                result = heuristic_strategy(
                    objective,
                    observation,
                    production_targets,
                    selected_improvement_site=selected_improvement_site,
                )
                result["source"] = "heuristic"
                result["ok"] = True
                record_llm_decision(
                    self.cfg.log_dir,
                    objective=objective,
                    provider="heuristic_fallback",
                    result=result,
                    request_summary=request_summary,
                    error="strategy fell back to local heuristic",
                    latency_ms=0,
                )

        before_guardrail = dict(result)
        result = reconcile_strategy_decision(result, objective, observation, production_targets)
        if result.get("guardrail_adjusted") and result != before_guardrail:
            record_llm_decision(
                self.cfg.log_dir,
                objective=objective,
                provider="strategy_guardrail",
                result=result,
                request_summary=request_summary,
                error="LLM strategy adjusted by deterministic feasibility guardrail",
                latency_ms=0,
            )

        if require_llm and result.get("source") != "llm":
            raise RuntimeError(f"LLM strategy was required but source was {result.get('source')}")
        return annotate_strategy_with_skill_status(result, runtime_dir=self.cfg.runtime_dir)

    def _should_try_remote_strategy(self, require_llm: bool) -> bool:
        if not require_llm:
            return False
        if self.cfg.slurm_enabled:
            return True
        if _local_llm_env_configured():
            return False
        auto_slurm = os.getenv("FACTORIO_AI_REQUIRE_LLM_AUTO_SLURM", "1").strip().lower()
        return auto_slurm not in {"0", "false", "no", "off"}

    def run_strategy_step(
        self,
        objective: str = "launch_rocket_program",
        require_llm: bool = False,
        target_count: int | None = None,
        target_item: str | None = None,
        input_item: str | None = None,
        max_steps: int | None = None,
        override_skill: str | None = None,
        skip_remote_strategy: bool = False,
    ) -> StrategyStepSummary:
        if override_skill:
            # Stall watchdog override: skip the LLM and force a specific deterministic skill to break
            # a no-progress loop. Still annotated so a missing executor routes through the foundry.
            strategy = annotate_strategy_with_skill_status(
                {
                    "selected_skill": override_skill,
                    "source": "autopilot_stall_recovery",
                    "reason": f"stall recovery: forced {override_skill} after repeated no-progress cycles",
                    "priority": 70,
                    "blockers": [],
                    "evidence": ["autopilot_stall_recovery"],
                    "expected_effect": "Break a no-progress loop by running a different deterministic skill.",
                },
                runtime_dir=self.cfg.runtime_dir,
            )
            if target_count is not None:
                strategy["target_count"] = target_count
            if target_item is not None:
                strategy["target_item"] = target_item
            if input_item is not None:
                strategy["input_item"] = input_item
        else:
            strategy = self.strategy_decision(
                objective, require_llm=require_llm, skip_remote=skip_remote_strategy
            )
        selected = str(strategy.get("selected_skill") or strategy.get("selected_goal") or "")
        status = strategy.get("skill_status") if isinstance(strategy.get("skill_status"), dict) else {}
        if status.get("codex_required"):
            return self._handle_missing_executor(
                objective,
                selected,
                strategy,
                "executor missing; foundry will attempt to generate it",
                max_steps=max_steps,
            )

        strategy_target_item = target_item
        if strategy_target_item is None and selected == "bootstrap_build_item_mall":
            strategy_target_item = _strategy_target_item(strategy)
        strategy_input_item = input_item
        if strategy_input_item is None and selected == "build_site_input_logistic_line":
            strategy_input_item = _strategy_site_input_item(strategy)
        strategy_target_count = target_count
        if strategy_target_count is None:
            strategy_target_count = _int_or_none(strategy.get("target_count"))
        config = self._skill_run_config(
            selected,
            target_count=strategy_target_count,
            max_steps=max_steps,
            target_item=strategy_target_item,
            input_item=strategy_input_item,
        )
        if config is None:
            return self._handle_missing_executor(
                objective,
                selected,
                strategy,
                "selected skill has no local runner; foundry will attempt to generate it",
                max_steps=max_steps,
            )
        self._clear_codex_wait_state(selected)
        run_config_keys = {
            "skill",
            "target_item",
            "target",
            "goal",
            "max_steps",
            "log_prefix",
            "log_path",
        }
        run_config = {key: value for key, value in config.items() if key in run_config_keys}
        run_config["objective"] = objective
        if self._is_generated_skill(selected):
            run = self._run_generated_skill(selected, run_config)
        else:
            run = self._run_skill(**run_config)
            self._track_implemented_skill_result(objective, selected, run, strategy)
        return StrategyStepSummary(
            ok=run.ok,
            reason=run.reason,
            objective=objective,
            selected_skill=selected,
            strategy=strategy,
            run=run,
        )

    # ------------------------------------------------------------------ #
    # Self-repair: detect a chronically-failing hand-written skill and ask the
    # foundry to generate a sandbox-gated override (auto-rollback on regression).
    # ------------------------------------------------------------------ #

    def _impl_failure_path(self) -> Path:
        return self.cfg.runtime_dir / "impl-skill-failures.json"

    def _track_implemented_skill_result(
        self, objective: str, skill_name: str, run: "RunSummary", strategy: dict[str, Any]
    ) -> None:
        if skill_name not in IMPLEMENTED_SKILLS:
            return
        counts = _read_json_file(self._impl_failure_path())
        if not isinstance(counts, dict):
            counts = {}
        if run.ok:
            if counts.pop(skill_name, None) is not None:
                self._write_impl_failures(counts)
            return
        entry = counts.get(skill_name) if isinstance(counts.get(skill_name), dict) else {}
        fails = int(entry.get("fails") or 0) + 1
        reasons = [str(r) for r in (entry.get("reasons") or []) if r][-3:] + [str(run.reason or "")]
        counts[skill_name] = {"fails": fails, "reasons": reasons[-4:], "updated_at": datetime.now(timezone.utc).isoformat()}
        self._write_impl_failures(counts)
        if (
            _skill_repair_enabled()
            and fails >= _impl_repair_fail_limit()
            and skill_name not in _repair_core_denylist()
        ):
            self._enqueue_skill_improvement(skill_name, reasons[-4:], strategy)

    def _write_impl_failures(self, counts: dict[str, Any]) -> None:
        try:
            self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            self._impl_failure_path().write_text(json.dumps(counts, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _enqueue_skill_improvement(self, skill_name: str, reasons: list[str], strategy: dict[str, Any]) -> None:
        from . import skill_foundry

        ok, _why = skill_foundry.eligible_for_generation(skill_name)
        if not ok:
            return
        # Auto-augment the repair with the missing observation that explains the failure
        # (e.g. the trees/rocks blocking a placement) so the codegen LLM can act on it.
        diagnostics: dict[str, Any] = {}
        try:
            diagnostics = _failure_diagnostics(skill_name, reasons, self.observe())
        except Exception:  # noqa: BLE001 - observation is best-effort
            diagnostics = {}
        try:
            skill_foundry.enqueue_foundry_request(
                self.cfg.runtime_dir,
                skill_name,
                reason="; ".join(r for r in reasons if r)[:500] or f"{skill_name} keeps failing",
                blockers=[r for r in reasons if r][-4:],
                expected_effect=f"Repair {skill_name}: handle the live failures and make progress.",
                target_item=_strategy_target_item(strategy) or strategy.get("target_item"),
                source="autopilot_repair",
                priority=max(0, min(100, _int_or_none(strategy.get("priority")) or 75)),
                mode="override",
                diagnostics=diagnostics,
            )
            skill_foundry.log_foundry_event(
                self.cfg.log_dir,
                "repair_enqueued",
                {
                    "skill_name": skill_name,
                    "reasons": [r for r in reasons if r][-4:],
                    "diagnostics": sorted(diagnostics.keys()),
                },
            )
        except Exception:  # noqa: BLE001 - enqueue must never break the loop
            pass
        # Root-cause targeting: if diagnostics flagged an unhealthy prerequisite (e.g. dead
        # power behind a 'cannot place'/'no progress' symptom), also repair that root-cause
        # skill -- not just the symptom skill the LLM kept selecting.
        for diag in diagnostics.values():
            root = diag.get("root_cause_skill") if isinstance(diag, dict) else None
            if not root or root == skill_name or root in _repair_core_denylist() or root not in IMPLEMENTED_SKILLS:
                continue
            try:
                root_ok, _root_why = skill_foundry.eligible_for_generation(root)
                if not root_ok:
                    continue
                skill_foundry.enqueue_foundry_request(
                    self.cfg.runtime_dir,
                    root,
                    reason=f"root cause of {skill_name} failures: {diag.get('hint', '')}"[:500],
                    blockers=[r for r in reasons if r][-4:],
                    expected_effect=f"Repair {root} (root cause) so {skill_name} can proceed.",
                    source="autopilot_root_cause",
                    priority=max(0, min(100, (_int_or_none(strategy.get("priority")) or 75) + 5)),
                    mode="override",
                    diagnostics={str(diag.get("missing_observation") or "root_cause"): diag},
                )
                skill_foundry.log_foundry_event(
                    self.cfg.log_dir,
                    "root_cause_enqueued",
                    {"symptom": skill_name, "root_cause": root, "hint": diag.get("hint")},
                )
            except Exception:  # noqa: BLE001 - root-cause enqueue is best-effort
                pass

    # ------------------------------------------------------------------ #
    # Self-development: never-stuck missing-executor handling
    # ------------------------------------------------------------------ #

    def _foundry_inline_enabled(self) -> bool:
        return os.getenv("FACTORIO_AI_FOUNDRY_INLINE", "").strip().lower() in {"1", "true", "yes", "on"}

    def _is_generated_skill(self, skill_name: str) -> bool:
        from . import skill_foundry

        # An active self-repair override runs the generated module even for an implemented skill name.
        try:
            if skill_foundry.registered_override(skill_name) is not None:
                return True
        except Exception:  # noqa: BLE001
            pass
        if skill_name in IMPLEMENTED_SKILLS:
            return False
        try:
            entry = skill_foundry.registry_status(skill_name)
        except Exception:  # noqa: BLE001
            return False
        return isinstance(entry, dict) and entry.get("status") == "registered"

    def _foundry_spec_from_strategy(self, selected: str, strategy: dict[str, Any]) -> dict[str, Any]:
        blockers = strategy.get("blockers")
        return {
            "skill_name": selected,
            "reason": str(strategy.get("reason") or ""),
            "blockers": list(blockers) if isinstance(blockers, list) else [],
            "expected_effect": str(strategy.get("expected_effect") or ""),
            "target_item": _strategy_target_item(strategy) or strategy.get("target_item"),
            "goal": selected,
        }

    def _enqueue_foundry_request(
        self, selected: str, strategy: dict[str, Any], *, source: str = "autopilot_gap"
    ) -> dict[str, Any]:
        from . import skill_foundry

        spec = self._foundry_spec_from_strategy(selected, strategy)
        priority = strategy.get("priority")
        priority = int(priority) if isinstance(priority, (int, float)) else 60
        try:
            skill_foundry.enqueue_foundry_request(
                self.cfg.runtime_dir,
                selected,
                reason=spec["reason"],
                blockers=spec["blockers"],
                expected_effect=spec["expected_effect"],
                target_item=spec["target_item"],
                source=source,
                priority=max(0, min(100, priority)),
            )
        except Exception:  # noqa: BLE001 - enqueue must never break the loop
            pass
        return spec

    def _try_inline_foundry(self, spec: dict[str, Any], objective: str) -> None:
        from . import skill_foundry

        ok, _why = skill_foundry.eligible_for_generation(spec["skill_name"])
        if not ok:
            return
        try:
            result = skill_foundry.develop_skill(self.cfg, spec, max_attempts=1)
        except Exception:  # noqa: BLE001
            return
        try:
            from .run_journal import record_foundry_attempt_journal

            record_foundry_attempt_journal(
                self.cfg.log_dir,
                objective=objective,
                skill_name=spec["skill_name"],
                result=result,
                repo_root=self._journal_repo_root(),
            )
        except Exception:  # noqa: BLE001
            pass

    def _handle_missing_executor(
        self,
        objective: str,
        selected: str,
        strategy: dict[str, Any],
        reason: str,
        *,
        max_steps: int | None = None,
    ) -> StrategyStepSummary:
        """Never block forever: record, enqueue for the foundry, run it if ready, else redirect."""

        self._record_codex_wait_state(objective, selected, reason, strategy)
        spec = self._enqueue_foundry_request(selected, strategy, source="autopilot_gap")
        if self._foundry_inline_enabled():
            self._try_inline_foundry(spec, objective)

        config = self._skill_run_config(selected, max_steps=max_steps)
        if config is not None:
            self._clear_codex_wait_state(selected, clear_reason="generated executor registered")
            run_config_keys = {"skill", "target_item", "target", "goal", "max_steps", "log_prefix", "log_path"}
            run_config = {key: value for key, value in config.items() if key in run_config_keys}
            run_config["objective"] = objective
            run = self._run_generated_skill(selected, run_config)
            return StrategyStepSummary(
                ok=run.ok,
                reason=run.reason,
                objective=objective,
                selected_skill=selected,
                strategy=strategy,
                run=run,
            )

        self._maybe_progress_background_layout_for_blocked_strategy(objective, selected, reason)
        return self._keep_progressing_redirect(objective, selected, strategy, reason)

    def _choose_keep_progressing_skill(self) -> str:
        from . import strategy as strategy_mod

        try:
            observation = self.observe()
        except Exception:  # noqa: BLE001
            return "plan_factory_site"
        try:
            coal_needed = getattr(strategy_mod, "_coal_supply_needed", None)
            if callable(coal_needed) and coal_needed(observation):
                return "setup_coal_supply"
            if total_item_count(observation, "iron-plate") < 10:
                return "produce_iron_plate"
            if total_item_count(observation, "copper-plate") < 10:
                return "produce_copper_plate"
            researched = getattr(strategy_mod, "_technology_researched", None)
            if callable(researched) and not researched(observation, "automation"):
                return "research_automation"
        except Exception:  # noqa: BLE001
            return "plan_factory_site"
        return "plan_factory_site"

    def _keep_progressing_redirect(
        self, objective: str, selected: str, strategy: dict[str, Any], reason: str
    ) -> StrategyStepSummary:
        choice = self._choose_keep_progressing_skill()
        config = self._skill_run_config(choice)
        if config is None:
            return StrategyStepSummary(
                ok=False,
                reason=f"missing executor for {selected}; no safe redirect available",
                objective=objective,
                selected_skill=selected,
                strategy=strategy,
            )
        redirect_strategy = dict(strategy)
        redirect_strategy["foundry_redirect"] = {"from": selected, "to": choice, "reason": reason}
        run_config_keys = {"skill", "target_item", "target", "goal", "max_steps", "log_prefix"}
        run_config = {key: value for key, value in config.items() if key in run_config_keys}
        run_config["objective"] = objective
        run = self._run_skill(**run_config)
        return StrategyStepSummary(
            ok=run.ok,
            reason=f"redirected {selected} -> {choice}: {run.reason}",
            objective=objective,
            selected_skill=choice,
            strategy=redirect_strategy,
            run=run,
        )

    def _run_generated_skill(self, skill_name: str, run_config: dict[str, Any]) -> RunSummary:
        """Run a registered generated skill with live failure rollback (auto-quarantine)."""

        from . import skill_foundry

        try:
            before_key = self._live_progress_key(self.observe())
        except Exception:  # noqa: BLE001 - baseline is best-effort
            before_key = None
        try:
            run = self._run_skill(**run_config)
        except Exception as exc:  # noqa: BLE001 - a generated skill must never crash the loop
            failure = f"generated skill raised: {type(exc).__name__}: {exc}"
            self._quarantine_generated_skill(skill_name, failure, signal="exception")
            return RunSummary(False, failure, 0, 0, self.cfg.log_dir / "generated-error.log", run_config.get("target_item", "generated"))
        if run.ok:
            # Catch a no-op "success": the skill returned done but the world did not change (no
            # entity placed, item produced, or research). A generated skill that silently gives up
            # (e.g. wrong research target -> done immediately) must count as a failure so it
            # auto-quarantines instead of permanently blocking the slot as fake success.
            no_progress = False
            if before_key is not None and (getattr(run, "item_count", 0) or 0) <= 0:
                try:
                    after_key = self._live_progress_key(self.observe())
                    no_progress = after_key is not None and after_key == before_key
                except Exception:  # noqa: BLE001
                    no_progress = False
            if no_progress:
                self._record_generated_skill_failure(skill_name, f"no-op: returned done with no measurable progress ({run.reason})")
                return run
            try:
                entry = skill_foundry.registry_status(skill_name) or {}
                skill_foundry.update_skill(skill_name, live_runs=int(entry.get("live_runs") or 0) + 1, live_failures=0)
            except Exception:  # noqa: BLE001
                pass
        else:
            self._record_generated_skill_failure(skill_name, run.reason)
        return run

    def _live_progress_key(self, observation: dict[str, Any]) -> tuple[Any, ...]:
        """Entity count + research/item fingerprint -- if unchanged across a 'done' run, the skill
        made no measurable progress (a no-op)."""
        entities = observation.get("entities") if isinstance(observation, dict) else None
        entity_count = len(entities) if isinstance(entities, list) else 0
        return (entity_count, self._progress_fingerprint(observation))

    def _ongoing_research_override_skill(self, objective: str, observation: dict[str, Any]) -> str | None:
        """Keep feeding an already-active research dependency without waiting on another LLM turn."""
        research = observation.get("research") if isinstance(observation.get("research"), dict) else {}
        current = str(research.get("current_research") or research.get("current") or "")
        research_skill_by_technology = {
            "logistics": "research_logistics",
            "electric-mining-drill": "research_electric_mining_drill",
        }
        skill = research_skill_by_technology.get(current)
        if not skill or self._skill_run_config(skill) is None:
            return None
        techs = research.get("technologies") if isinstance(research.get("technologies"), dict) else {}
        tech_state = techs.get(current) if isinstance(techs.get(current), dict) else {}
        if bool(tech_state.get("researched")):
            return None
        if current == "electric-mining-drill":
            return skill
        try:
            production_targets = load_targets(self.cfg.runtime_dir, objective).per_minute
            selected = str(heuristic_strategy(objective, observation, production_targets).get("selected_skill") or "")
        except Exception:  # noqa: BLE001 - a fast-path failure should fall back to normal strategy.
            return None
        if selected != skill:
            return None
        return skill

    def _quarantine_generated_skill(self, skill_name: str, reason: str, *, signal: str) -> None:
        from . import skill_foundry

        try:
            skill_foundry.set_skill_status(skill_name, "quarantined", reason)
            skill_foundry.log_foundry_event(
                self.cfg.log_dir, "live_disabled", {"skill_name": skill_name, "reason": reason, "signal": signal}
            )
            skill_foundry.write_runtime_mirror(self.cfg.runtime_dir)
        except Exception:  # noqa: BLE001
            pass

    def _record_generated_skill_failure(self, skill_name: str, reason: str) -> None:
        from . import skill_foundry

        try:
            entry = skill_foundry.registry_status(skill_name) or {}
            failures = int(entry.get("live_failures") or 0) + 1
            try:
                limit = max(1, int(os.getenv("FACTORIO_AI_GEN_LIVE_FAIL_LIMIT", "3")))
            except (TypeError, ValueError):
                limit = 3
            if failures >= limit:
                self._quarantine_generated_skill(
                    skill_name, f"live failures >= {limit}: {reason}", signal="repeated_failure"
                )
                skill_foundry.update_skill(skill_name, live_failures=failures)
            else:
                skill_foundry.update_skill(skill_name, live_failures=failures, last_failure_reason=reason)
        except Exception:  # noqa: BLE001
            pass

    def _progress_fingerprint(self, observation: dict[str, Any]) -> tuple[Any, ...]:
        """A compact signal of real progress. If this is unchanged across cycles while the same skill
        keeps being selected, the run is stalled (e.g. re-smelting plates already sitting in a furnace)."""

        research = observation.get("research") if isinstance(observation.get("research"), dict) else {}
        techs = research.get("technologies") if isinstance(research.get("technologies"), dict) else {}
        researched = sum(1 for tech in techs.values() if isinstance(tech, dict) and tech.get("researched"))
        progress = research.get("research_progress")
        if progress is None:
            progress = research.get("progress")
        try:
            progress = round(float(progress or 0.0), 2)
        except (TypeError, ValueError):
            progress = 0.0
        current = str(research.get("current_research") or research.get("current") or "")
        items = tuple(total_item_count(observation, item) for item in _STALL_PROGRESS_ITEMS)
        return (researched, current, progress, items)

    def _progress_kpi_path(self) -> Path:
        return self.cfg.runtime_dir / "progress-kpi.json"

    def _write_progress_kpi(
        self,
        fingerprint: tuple[Any, ...] | None,
        stall_count: int,
        selected: str,
        *,
        failure_root: str | None = None,
        repair_skill: str | None = None,
        seed_count: int = 0,
    ) -> None:
        """Persist a readable progress KPI each cycle so an operator (and run-health) can SEE
        whether the run is actually advancing or stuck -- the watchdog already acts on it."""
        try:
            researched, current, progress, items = fingerprint or (0, "", 0.0, ())
            payload = {
                "researched": researched,
                "current_research": current,
                "research_progress": progress,
                "key_items": dict(zip(_STALL_PROGRESS_ITEMS, items)),
                "selected_skill": selected,
                "stall_count": stall_count,
                "stuck": stall_count >= _stall_threshold(),
                "failure_root": failure_root,
                "repair_skill": repair_skill,
                "seed_count": seed_count,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            self._progress_kpi_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001 - KPI is best-effort, never break the loop
            pass

    def _stall_recovery_skill(
        self, objective: str, observation: dict[str, Any], recent_skills: list[str]
    ) -> str | None:
        """Pick a runnable deterministic skill different from the stalled one: prefer the heuristic
        planner's next step, else rotate through a progression list, else None."""

        from . import strategy as strategy_mod

        recent = set(recent_skills)

        try:
            iron_issue = strategy_mod._gear_mall_iron_plate_logistics_issue(observation)
            if (
                strategy_mod._gear_mall_source_fuel_blocker_preempts(iron_issue)
                and self._skill_run_config("build_iron_plate_logistic_line_to_gear_mall") is not None
            ):
                return "build_iron_plate_logistic_line_to_gear_mall"
        except Exception:  # noqa: BLE001
            pass

        # Prerequisite-aware recovery for the two most common bootstrap deadlocks: the strategy keeps
        # re-selecting a skill that can never progress because an upstream prerequisite was never built.
        # Checked before the generic heuristic/rotation so recovery is deterministic, not order-luck.
        try:
            readiness = build_factory_readiness(observation)
            repair_skill = readiness.repair_skill
            if repair_skill and self._skill_run_config(repair_skill) is not None:
                return repair_skill
        except Exception:  # noqa: BLE001
            pass
        try:
            from .planner import _find_gear_belt_mall_logistics_layout

            entities = observation.get("entities") if isinstance(observation.get("entities"), list) else []
            # (1) Iron production has died -- no furnace producing iron-plate AND no drill mining iron-ore
            #     while an iron patch is reachable (usually a drill stranded off-patch, mining_target=None).
            #     produce_iron_plate rebuilds a direct beltless cell + relocates the stranded drill,
            #     breaking the iron<-belts<-mall<-iron circular deadlock the strategy guardrails can't see.
            iron_mining = any(
                isinstance(e, dict)
                and e.get("name") in ("burner-mining-drill", "electric-mining-drill")
                and str(e.get("mining_target") or "") == "iron-ore"
                for e in entities
            )
            iron_dead = (
                not strategy_mod._iron_plate_source_furnaces(observation)
                and not iron_mining
                and strategy_mod._starter_resource_available(observation, "iron-ore")
            )
            if iron_dead and "produce_iron_plate" not in recent and self._skill_run_config("produce_iron_plate") is not None:
                return "produce_iron_plate"
            # (2) The mall-wiring / iron-line skills only WIRE an EXISTING gear+belt mall:
            #       - build_gear_belt_mall_logistics loops "cannot find spaced powered gear and
            #         reusable belt assemblers" when the gear+belt assembler pair doesn't exist;
            #       - build_iron_plate_logistic_line_to_gear_mall first requires that same gear/belt
            #         layout (it calls _find_gear_belt_mall_logistics_layout) and otherwise loops
            #         "cannot find both an iron-plate source furnace and a powered iron-gear mall".
            #     Both deadlock until bootstrap_build_item_mall actually BUILDS the missing assemblers.
            mall_wiring_skills = {
                "build_gear_belt_mall_logistics",
                "build_iron_plate_logistic_line_to_gear_mall",
            }
            if (
                recent & mall_wiring_skills
                and "bootstrap_build_item_mall" not in recent
                and _find_gear_belt_mall_logistics_layout(observation) is None
                and self._skill_run_config("bootstrap_build_item_mall") is not None
            ):
                return "bootstrap_build_item_mall"
        except Exception:  # noqa: BLE001
            pass

        try:
            planner = strategy_mod.heuristic_strategy(objective, observation, {})
            candidate = str(planner.get("selected_skill") or planner.get("selected_goal") or "")
            if candidate and candidate not in recent and self._skill_run_config(candidate) is not None:
                return candidate
        except Exception:  # noqa: BLE001
            pass
        for candidate in _STALL_ROTATION_SKILLS:
            if candidate in recent:
                continue
            try:
                if self._skill_run_config(candidate) is not None:
                    return candidate
            except Exception:  # noqa: BLE001
                continue
        return None

    def run_autopilot_loop(
        self,
        objective: str = "launch_rocket_program",
        *,
        require_llm: bool = False,
        target_count: int | None = None,
        max_steps: int | None = None,
        cycles: int = 0,
        sleep_seconds: float = 5.0,
        continue_on_error: bool = True,
        log_path: Path | None = None,
    ) -> AutopilotLoopSummary:
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_path or self.cfg.log_dir / f"autopilot-{_timestamp()}.jsonl"
        completed = 0
        failures = 0
        last_step: StrategyStepSummary | None = None
        interrupted = False
        reason = "cycle limit reached" if cycles > 0 else "autopilot loop stopped"
        self._maybe_ensure_slurm_worker(reason="autopilot_start", force=True)
        self._write_autopilot_heartbeat(objective, "starting", cycle=0, reason=reason)
        # Stall watchdog state.
        last_fingerprint: tuple[Any, ...] | None = None
        last_selected = ""
        stall_count = 0
        recent_skills: list[str] = []
        stall_threshold = _stall_threshold()
        seed_count_total = 0
        # Failure watchdog: the same-skill stall counter never trips when the strategy OSCILLATES
        # between several different infeasible skills (iron-line -> coal-feed -> ...), each failing
        # once. Count consecutive failed cycles regardless of which skill, and force prerequisite
        # recovery (e.g. bootstrap_build_item_mall / produce_iron_plate) once it crosses the threshold.
        consecutive_failed_cycles = 0
        failure_recovery_threshold = max(stall_threshold, _failure_recovery_threshold())
        # Commit-to-progressing-skill state: while a skill keeps changing the factory, reuse it
        # without a fresh (slow) LLM strategy call; re-strategize the moment progress stalls and at
        # least every commit_max cycles. The LLM still picks every genuinely new strategy.
        commit_enabled = _commit_skill_enabled()
        commit_max = _commit_skill_max()
        committed_skill: str | None = None
        committed_target_count: int | None = None
        committed_target_item: str | None = None
        committed_input_item: str | None = None
        commit_skips = 0
        last_progress_key: tuple[Any, ...] | None = None
        # Graceful LLM degradation: count consecutive failed cycles; after a threshold, run one cycle
        # on the heuristic so an unstable/hung serving (or a refusal loop) can't freeze the autopilot.
        consecutive_strategy_failures = 0
        llm_degrade_threshold = _llm_degrade_threshold()
        allow_heuristic_fallback = _heuristic_autopilot_fallback_allowed()
        # Sticky degradation: after the threshold trips, stay on the heuristic for this many cycles
        # before probing the remote again (each broken retry costs ~100-540s).
        llm_degrade_cooldown_cycles = _llm_degrade_cooldown_cycles()
        degrade_cooldown = 0

        try:
            with log_path.open("a", encoding="utf-8") as log_file:
                while cycles <= 0 or completed < cycles:
                    self._maybe_ensure_slurm_worker(reason="autopilot_cycle")
                    self._write_autopilot_heartbeat(objective, "cycle_start", cycle=completed + 1)
                    self._maybe_progress_codex_wait_layout(objective)
                    override_skill: str | None = None
                    override_from_commit = False
                    recover_for_stall = stall_count >= stall_threshold
                    recover_for_failures = consecutive_failed_cycles >= failure_recovery_threshold
                    if recover_for_stall or recover_for_failures:
                        try:
                            stall_obs = self.observe()
                        except Exception:  # noqa: BLE001
                            stall_obs = None
                        if stall_obs is not None:
                            # Use a window wide enough to see ALL recently-failed skills, so failure-driven
                            # recovery catches the oscillation case (each distinct skill failing once).
                            window = max(stall_threshold, failure_recovery_threshold)
                            override_skill = self._stall_recovery_skill(
                                objective, stall_obs, recent_skills[-window:] or [last_selected]
                            )
                        if override_skill:
                            trigger = (
                                f"no progress for {stall_count} cycles on '{last_selected}'"
                                if recover_for_stall
                                else f"{consecutive_failed_cycles} consecutive failed cycles"
                            )
                            self._write_autopilot_heartbeat(
                                objective,
                                "stall_recovery",
                                cycle=completed + 1,
                                reason=f"{trigger}; forcing {override_skill}",
                            )
                            stall_count = 0
                            consecutive_failed_cycles = 0
                    # Reuse a still-progressing skill instead of paying another LLM strategy call.
                    if (
                        override_skill is None
                        and commit_enabled
                        and committed_skill is not None
                        and commit_skips < commit_max
                    ):
                        override_skill = committed_skill
                        override_from_commit = True
                        commit_skips += 1
                        self._write_autopilot_heartbeat(
                            objective,
                            "commit_skill",
                            cycle=completed + 1,
                            reason=f"reusing progressing skill '{committed_skill}' without re-strategizing ({commit_skips}/{commit_max})",
                        )
                    elif override_skill is None:
                        # A fresh LLM strategy decision is about to run; reset the skip budget.
                        commit_skips = 0
                        try:
                            research_obs = self.observe()
                            research_skill = self._ongoing_research_override_skill(objective, research_obs)
                        except Exception:  # noqa: BLE001 - normal strategy path can handle observe/LLM errors.
                            research_skill = None
                        if research_skill is not None:
                            override_skill = research_skill
                            self._write_autopilot_heartbeat(
                                objective,
                                "research_commit",
                                cycle=completed + 1,
                                reason=(
                                    f"continuing active research with '{research_skill}' without waiting for a fresh "
                                    "LLM strategy turn"
                                ),
                            )
                    # After repeated failures, degrade this one cycle to the heuristic so a hung/erroring
                    # serving can't freeze progress (the agent otherwise sits frozen mid-action). An
                    # override_skill cycle already bypasses the LLM, so only degrade plain cycles.
                    degrade_to_heuristic = (
                        require_llm
                        and allow_heuristic_fallback
                        and override_skill is None
                        and (
                            consecutive_strategy_failures >= llm_degrade_threshold
                            or degrade_cooldown > 0
                        )
                    )
                    if degrade_to_heuristic and degrade_cooldown > 0:
                        degrade_cooldown -= 1
                    if degrade_to_heuristic:
                        self._write_autopilot_heartbeat(
                            objective,
                            "llm_degraded",
                            cycle=completed + 1,
                            reason=f"{consecutive_strategy_failures} failed cycles; using heuristic strategy to keep progressing",
                        )
                    started = time.monotonic()
                    step_target_count = target_count
                    step_target_item: str | None = None
                    step_input_item: str | None = None
                    if override_from_commit:
                        if step_target_count is None:
                            step_target_count = committed_target_count
                        step_target_item = committed_target_item
                        step_input_item = committed_input_item
                    try:
                        last_step = self.run_strategy_step(
                            objective=objective,
                            require_llm=require_llm and not degrade_to_heuristic,
                            target_count=step_target_count,
                            target_item=step_target_item,
                            input_item=step_input_item,
                            max_steps=max_steps,
                            override_skill=override_skill,
                            # A degraded cycle must NOT touch the hung remote (it would block on the
                            # full remote timeout); force the local heuristic path immediately.
                            skip_remote_strategy=degrade_to_heuristic,
                        )
                    except Exception as exc:
                        last_step = StrategyStepSummary(
                            ok=False,
                            reason=f"strategy cycle failed: {type(exc).__name__}: {exc}",
                            objective=objective,
                            selected_skill="",
                            strategy={},
                        )
                        self._maybe_progress_codex_wait_layout(objective, phase="cycle_error")
                        self._write_autopilot_heartbeat(
                            objective,
                            "cycle_error",
                            cycle=completed + 1,
                            reason=last_step.reason,
                        )
                    # Stall watchdog bookkeeping: count consecutive cycles that re-ran the same skill
                    # without changing the progress fingerprint.
                    try:
                        progress_obs = self.observe()
                        fingerprint = self._progress_fingerprint(progress_obs)
                        progress_key = self._live_progress_key(progress_obs)
                        readiness = build_factory_readiness(progress_obs)
                        failure_root = readiness.failure_root
                        repair_skill = readiness.repair_skill
                    except Exception:  # noqa: BLE001
                        fingerprint = None
                        progress_key = None
                        failure_root = None
                        repair_skill = None
                    selected_now = last_step.selected_skill or ""
                    if last_step.run is not None:
                        seed_count_total += int(getattr(last_step.run, "seed_count", 0) or 0)
                    # "Progress" for commit purposes = entity count OR production/research changed
                    # (so building a smelting line counts, not just producing items).
                    made_progress = (
                        progress_key is not None
                        and last_progress_key is not None
                        and progress_key != last_progress_key
                    )
                    if progress_key is not None:
                        last_progress_key = progress_key
                    if (
                        fingerprint is not None
                        and last_fingerprint is not None
                        and fingerprint == last_fingerprint
                        and selected_now
                        and selected_now == last_selected
                    ):
                        stall_count += 1
                    else:
                        stall_count = 0
                    # Failure watchdog counter: any failed cycle (any skill) increments; any success
                    # resets. Unlike stall_count this survives the strategy hopping between skills, so a
                    # run that oscillates between several infeasible skills still triggers recovery.
                    if last_step.ok:
                        consecutive_failed_cycles = 0
                    else:
                        consecutive_failed_cycles += 1
                    if fingerprint is not None:
                        last_fingerprint = fingerprint
                    last_selected = selected_now
                    # Commit to the skill only while it keeps making progress; otherwise drop the
                    # commitment so the next cycle re-strategizes via the LLM.
                    yielded_wait = str(last_step.reason or "").startswith("yielded for other work")
                    if (
                        commit_enabled
                        and last_step.ok
                        and yielded_wait
                        and repair_skill
                        and selected_now
                        and selected_now != repair_skill
                    ):
                        # A wait-yield is operationally OK, but if readiness already knows the root
                        # prerequisite is missing, the next committed cycle should repair that root
                        # instead of reusing the waiting skill and creating a no-progress loop.
                        committed_skill = repair_skill
                        committed_target_count = None
                        committed_target_item = None
                        committed_input_item = None
                        commit_skips = 0
                    elif commit_enabled and last_step.ok and made_progress and selected_now:
                        strategy_target_count = _int_or_none(last_step.strategy.get("target_count"))
                        committed_skill = selected_now
                        committed_target_count = strategy_target_count
                        committed_target_item = (
                            _strategy_target_item(last_step.strategy)
                            if selected_now == "bootstrap_build_item_mall"
                            else None
                        )
                        committed_input_item = (
                            _strategy_site_input_item(last_step.strategy)
                            if selected_now == "build_site_input_logistic_line"
                            else None
                        )
                    else:
                        committed_skill = None
                        committed_target_count = None
                        committed_target_item = None
                        committed_input_item = None
                    # Track consecutive failed cycles for graceful LLM degradation. A degraded
                    # (heuristic) cycle resets the counter so the next cycle retries the real LLM.
                    if last_step.ok or degrade_to_heuristic:
                        consecutive_strategy_failures = 0
                    else:
                        consecutive_strategy_failures += 1
                        # A real remote retry just failed (hang/RecursionError); arm the cooldown so
                        # the next several cycles stay on the fast heuristic instead of re-paying it.
                        if consecutive_strategy_failures >= llm_degrade_threshold:
                            degrade_cooldown = llm_degrade_cooldown_cycles
                    self._write_progress_kpi(
                        fingerprint,
                        stall_count,
                        selected_now,
                        failure_root=failure_root,
                        repair_skill=repair_skill,
                        seed_count=seed_count_total,
                    )
                    recent_skills.append(selected_now)
                    if len(recent_skills) > 12:
                        recent_skills = recent_skills[-12:]
                    completed += 1
                    if not last_step.ok:
                        failures += 1
                    duration_seconds = round(time.monotonic() - started, 3)
                    payload = {
                        "time": datetime.now(timezone.utc).isoformat(),
                        "cycle": completed,
                        "objective": objective,
                        "ok": last_step.ok,
                        "duration_seconds": duration_seconds,
                        "step": last_step.to_dict(),
                    }
                    json.dump(payload, log_file, ensure_ascii=False, separators=(",", ":"))
                    log_file.write("\n")
                    log_file.flush()
                    record_autopilot_cycle_journal(
                        self.cfg.log_dir,
                        objective=objective,
                        cycle=completed,
                        selected_skill=last_step.selected_skill,
                        ok=last_step.ok,
                        reason=last_step.reason,
                        duration_seconds=duration_seconds,
                        strategy=last_step.strategy,
                        log_path=log_path,
                        repo_root=self._journal_repo_root(),
                    )
                    self._write_autopilot_heartbeat(
                        objective,
                        "cycle_complete" if last_step.ok else "cycle_failed",
                        cycle=completed,
                        reason=last_step.reason,
                    )
                    if not last_step.ok and not continue_on_error:
                        reason = last_step.reason
                        break
                    if cycles <= 0 or completed < cycles:
                        self._write_autopilot_heartbeat(
                            objective,
                            "sleeping",
                            cycle=completed,
                            reason=f"sleeping {sleep_seconds} seconds before next strategy cycle",
                        )
                        time.sleep(max(0.0, sleep_seconds))
        except KeyboardInterrupt:
            interrupted = True
            reason = "autopilot interrupted by user"
            self._write_autopilot_heartbeat(objective, "interrupted", cycle=completed, reason=reason)

        ok = failures == 0 or (continue_on_error and cycles <= 0 and not interrupted)
        if cycles > 0 and completed >= cycles and failures == 0:
            reason = "cycle limit reached"
        elif failures > 0 and continue_on_error:
            reason = f"completed with {failures} failed cycle(s); continuing is enabled"
        self._write_autopilot_heartbeat(
            objective,
            "stopped" if not interrupted else "interrupted",
            cycle=completed,
            reason=reason,
        )
        return AutopilotLoopSummary(
            ok=ok,
            reason=reason,
            objective=objective,
            cycles=completed,
            log_path=log_path,
            last_step=last_step,
            failures=failures,
            interrupted=interrupted,
        )

    def run_codex_wait_layout_loop(
        self,
        objective: str = "launch_rocket_program",
        *,
        cycles: int = 0,
        sleep_seconds: float = 20.0,
    ) -> CodexWaitLayoutLoopSummary:
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.cfg.log_dir / "layout-improvement-background.jsonl"
        completed = 0
        interrupted = False
        active_skill = ""
        reason = "no active Codex wait state"

        try:
            while cycles <= 0 or completed < cycles:
                state = self._read_codex_wait_state()
                if not state.get("active"):
                    break
                active_skill = str(state.get("active_skill") or f"codex_wait:{state.get('selected_skill') or ''}")
                wait_objective = str(state.get("objective") or objective)
                self._maybe_progress_codex_wait_layout(wait_objective, phase="wait_loop")
                completed += 1
                reason = "cycle limit reached" if cycles > 0 and completed >= cycles else "Codex wait layout loop running"
                record_layout_loop_journal(
                    self.cfg.log_dir,
                    loop_type="codex_wait_layout_cycle",
                    objective=wait_objective,
                    cycle=completed,
                    active_skill=active_skill,
                    ok=True,
                    reason=reason,
                    log_path=log_path,
                    metadata={"wait_active": True},
                    repo_root=self._journal_repo_root(),
                )
                if cycles > 0 and completed >= cycles:
                    break
                time.sleep(max(0.0, sleep_seconds))
        except KeyboardInterrupt:
            interrupted = True
            reason = "Codex wait layout loop interrupted by user"

        state = self._read_codex_wait_state()
        wait_active = bool(state.get("active"))
        if completed > 0 and not wait_active and not interrupted:
            reason = "Codex wait state cleared"
        return CodexWaitLayoutLoopSummary(
            ok=not interrupted,
            reason=reason,
            objective=objective,
            cycles=completed,
            log_path=log_path,
            active_skill=active_skill,
            wait_active=wait_active,
            interrupted=interrupted,
        )

    def run_idle_layout_loop(
        self,
        objective: str = "launch_rocket_program",
        *,
        cycles: int = 0,
        sleep_seconds: float = 5.0,
        stale_seconds: float = 15.0,
        min_submit_interval_seconds: float = 0.0,
    ) -> IdleLayoutLoopSummary:
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.cfg.log_dir / "layout-improvement-background.jsonl"
        self._maybe_ensure_slurm_worker(reason="idle_layout_start", force=True)
        process_path = self._idle_layout_process_path()
        existing = _read_json_file(process_path)
        existing_pid = _int_or_none(existing.get("pid")) if isinstance(existing, dict) else None
        if existing_pid and existing_pid != os.getpid() and _pid_is_running(existing_pid):
            self._write_background_layout_log(
                {
                    "event": "layout_idle_loop_already_running",
                    "pid": existing_pid,
                    "objective": objective,
                }
            )
            return IdleLayoutLoopSummary(
                ok=True,
                reason="idle layout loop already running",
                objective=objective,
                cycles=0,
                idle_cycles=0,
                busy_cycles=0,
                log_path=log_path,
            )
        if existing_pid and existing_pid != os.getpid() and not _pid_is_running(existing_pid):
            existing["state"] = "stale"
            existing["stale_detected_at"] = datetime.now(timezone.utc).isoformat()
            existing["stale_reason"] = "recorded idle layout loop pid is not running"
            with process_path.open("w", encoding="utf-8") as file:
                json.dump(existing, file, ensure_ascii=False, indent=2)
            self._write_background_layout_log(
                {
                    "event": "layout_idle_loop_stale_pid_recovered",
                    "pid": existing_pid,
                    "objective": objective,
                }
            )
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        with process_path.open("w", encoding="utf-8") as file:
            json.dump(
                {
                    "pid": os.getpid(),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "objective": objective,
                    "state": "running",
                },
                file,
                ensure_ascii=False,
                indent=2,
            )
        completed = 0
        idle_cycles = 0
        busy_cycles = 0
        interrupted = False
        reason = "idle layout loop running"
        heartbeat_log_interval = max(
            1.0,
            _float_env("FACTORIO_AI_IDLE_LAYOUT_HEARTBEAT_LOG_INTERVAL_SECONDS", 60.0),
        )
        last_idle_heartbeat_log = 0.0
        last_busy_heartbeat_log = 0.0

        try:
            while cycles <= 0 or completed < cycles:
                self._maybe_ensure_slurm_worker(reason="idle_layout_cycle")
                idle, idle_reason, heartbeat = self._autopilot_idle_for_layout(stale_seconds)
                completed += 1
                if idle:
                    idle_cycles += 1
                    try:
                        observation = self._observe_for_idle_layout()
                    except Exception as exc:  # noqa: BLE001
                        self._write_background_layout_log(
                            {
                                "event": "layout_idle_observe_failed",
                                "active_skill": "idle:observe_failed",
                                "active_step": 0,
                                "idle_reason": idle_reason,
                                "heartbeat": heartbeat,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        record_layout_loop_journal(
                            self.cfg.log_dir,
                            loop_type="idle_layout_cycle",
                            objective=objective,
                            cycle=completed,
                            active_skill="idle:observe_failed",
                            ok=False,
                            reason=f"observe failed: {type(exc).__name__}: {exc}",
                            log_path=log_path,
                            metadata={"idle_reason": idle_reason},
                            repo_root=self._journal_repo_root(),
                        )
                    else:
                        active_skill = f"idle:{_slugify_reason(idle_reason)}"
                        now_monotonic = time.monotonic()
                        if now_monotonic - last_idle_heartbeat_log >= heartbeat_log_interval:
                            last_idle_heartbeat_log = now_monotonic
                            self._write_background_layout_log(
                                {
                                    "event": "layout_idle_scheduler_heartbeat",
                                    "active_skill": active_skill,
                                    "active_step": 0,
                                    "idle_reason": idle_reason,
                                    "heartbeat": heartbeat,
                                }
                            )
                        self._maybe_progress_background_layout_work(
                            observation,
                            objective,
                            active_skill,
                            0,
                            minimum_interval_seconds=min_submit_interval_seconds,
                        )
                else:
                    busy_cycles += 1
                    now_monotonic = time.monotonic()
                    if now_monotonic - last_busy_heartbeat_log >= heartbeat_log_interval:
                        last_busy_heartbeat_log = now_monotonic
                        self._write_background_layout_log(
                            {
                                "event": "layout_idle_scheduler_busy",
                                "active_skill": "autopilot",
                                "active_step": 0,
                                "idle_reason": idle_reason,
                                "heartbeat": heartbeat,
                            }
                        )
                reason = "cycle limit reached" if cycles > 0 and completed >= cycles else "idle layout loop running"
                if cycles > 0 and completed >= cycles:
                    break
                time.sleep(max(0.0, sleep_seconds))
        except KeyboardInterrupt:
            interrupted = True
            reason = "idle layout loop interrupted by user"

        final_state = _read_json_file(process_path)
        if _int_or_none(final_state.get("pid")) == os.getpid():
            final_state["state"] = "stopped" if not interrupted else "interrupted"
            final_state["stopped_at"] = datetime.now(timezone.utc).isoformat()
            with process_path.open("w", encoding="utf-8") as file:
                json.dump(final_state, file, ensure_ascii=False, indent=2)

        return IdleLayoutLoopSummary(
            ok=not interrupted,
            reason=reason,
            objective=objective,
            cycles=completed,
            idle_cycles=idle_cycles,
            busy_cycles=busy_cycles,
            log_path=log_path,
            interrupted=interrupted,
        )

    def _observe_for_idle_layout(self) -> dict[str, Any]:
        return self.observe()

    def begin_codex_work(
        self,
        objective: str,
        selected_skill: str,
        reason: str,
    ) -> dict[str, Any]:
        selected_skill = selected_skill.strip()
        if not selected_skill:
            raise ValueError("selected_skill is required")
        reason = reason.strip() or "Codex is implementing a missing deterministic executor."
        strategy = {
            "selected_skill": selected_skill,
            "reason": reason,
            "source": "codex_manual_wait",
            "skill_status": {
                "name": selected_skill,
                "implemented": False,
                "executor": None,
                "codex_required": True,
            },
        }
        self._record_codex_wait_state(objective, selected_skill, reason, strategy)
        state = self._read_codex_wait_state()
        process = _read_json_file(self._codex_wait_layout_process_path())
        return {
            "ok": True,
            "objective": objective,
            "selectedSkill": selected_skill,
            "waitState": state,
            "layoutLoop": process,
        }

    def finish_codex_work(
        self,
        selected_skill: str,
        *,
        clear_reason: str = "Codex implementation completed",
    ) -> dict[str, Any]:
        selected_skill = selected_skill.strip()
        if not selected_skill:
            raise ValueError("selected_skill is required")
        cleared = self._clear_codex_wait_state(selected_skill, clear_reason=clear_reason)
        state = self._read_codex_wait_state()
        return {
            "ok": cleared,
            "selectedSkill": selected_skill,
            "waitState": state,
            "reason": clear_reason if cleared else "no matching active Codex wait state",
        }

    def _skill_run_config(
        self,
        skill_name: str,
        target_count: int | None = None,
        max_steps: int | None = None,
        target_item: str | None = None,
        input_item: str | None = None,
    ) -> dict[str, Any] | None:
        # An active self-repair override (or a registered generated skill) takes precedence over the
        # hand-written executor below. _is_generated_skill() is true for both cases.
        if self._is_generated_skill(skill_name):
            generated = self._generated_skill_run_config(skill_name, target_count, max_steps)
            if generated is not None:
                return generated
        if skill_name == "produce_iron_plate":
            target = target_count or 10
            return {
                "skill": IronPlateSkill(target),
                "target_item": "iron-plate",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 200),
                "log_prefix": "strategy-iron",
            }
        if skill_name == "produce_copper_plate":
            target = target_count or 10
            return {
                "skill": CopperPlateSkill(target),
                "target_item": "copper-plate",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 250),
                "log_prefix": "strategy-copper",
            }
        if skill_name == "produce_automation_science_pack":
            target = target_count or 5
            return {
                "skill": AutomationScienceSkill(target),
                "target_item": "automation-science-pack",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 500),
                "log_prefix": "strategy-science",
            }
        if skill_name == "produce_electronic_circuit":
            target = target_count or 5
            return {
                "skill": ElectronicCircuitSkill(target),
                "target_item": "electronic-circuit",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 500),
                "log_prefix": "strategy-circuit",
            }
        if skill_name == "build_belt_smelting_line":
            target = target_count or 10
            return {
                "skill": BeltSmeltingLineSkill(target),
                "target_item": "iron-plate",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 700),
                "log_prefix": "strategy-belt-smelting",
            }
        if skill_name == "setup_coal_supply":
            target = target_count or 16
            return {
                "skill": CoalSupplySkill(target),
                "target_item": "coal",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 800),
                "log_prefix": "strategy-coal-supply",
            }
        if skill_name == "setup_stone_supply":
            target = target_count or 16
            return {
                "skill": StoneSupplySkill(target),
                "target_item": "stone",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 800),
                "log_prefix": "strategy-stone-supply",
            }
        if skill_name == "connect_coal_fuel_feed":
            return {
                "skill": CoalFuelFeedSkill(),
                "target_item": "coal",
                "target": target_count or 1,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 600),
                "log_prefix": "strategy-coal-fuel-feed",
            }
        if skill_name == "expand_iron_smelting":
            target = target_count or 90
            return {
                "skill": ExpandIronSmeltingSkill(float(target)),
                "target_item": "iron-plate",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 2000),
                "log_prefix": "strategy-expand-iron-smelting",
            }
        if skill_name == "expand_copper_smelting":
            target = target_count or 75
            return {
                "skill": ExpandCopperSmeltingSkill(float(target)),
                "target_item": "copper-plate",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 1600),
                "log_prefix": "strategy-expand-copper-smelting",
            }
        if skill_name == "setup_power":
            return {
                "skill": SetupPowerSkill(),
                "target_item": "steam",
                "target": 1,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 900),
                "log_prefix": "strategy-power",
            }
        if skill_name == "research_automation":
            return {
                "skill": ResearchAutomationSkill(),
                "target_item": "automation-science-pack",
                "target": 10,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 1500),
                "log_prefix": "strategy-automation-research",
            }
        if skill_name == "automate_electronic_circuit_line":
            target = target_count or 50
            return {
                "skill": CircuitAutomationSkill(target),
                "target_item": "electronic-circuit",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 1800),
                "log_prefix": "strategy-circuit-automation",
            }
        if skill_name == "research_logistics":
            return {
                "skill": ResearchTechnologySkill("logistics"),
                "target_item": "automation-science-pack",
                "target": 20,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 2200),
                "log_prefix": "strategy-logistics-research",
            }
        if skill_name == "research_electric_mining_drill":
            return {
                "skill": ResearchTechnologySkill("electric-mining-drill"),
                "target_item": "automation-science-pack",
                "target": 25,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 2200),
                "log_prefix": "strategy-electric-mining-drill-research",
            }
        if skill_name == "bootstrap_build_item_mall":
            target = target_count or 20
            target_item = _strategy_target_item({"target_item": target_item}) or "transport-belt"
            return {
                "skill": BuildItemMallSkill(target_item, target),
                "target_item": target_item,
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 1200),
                "log_prefix": "strategy-build-item-mall",
            }
        if skill_name == "bootstrap_power_pole_mall":
            target = target_count or 20
            target_item = "small-electric-pole"
            return {
                "skill": BuildItemMallSkill(target_item, target),
                "target_item": target_item,
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 1200),
                "log_prefix": "strategy-power-pole-mall",
            }
        if skill_name == "bootstrap_electric_mining_drill_mall":
            target = target_count or 6
            target_item = "electric-mining-drill"
            return {
                "skill": BuildItemMallSkill(target_item, target),
                "target_item": target_item,
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 1600),
                "log_prefix": "strategy-electric-mining-drill-mall",
            }
        if skill_name == "build_gear_belt_mall_logistics":
            target = target_count or 20
            return {
                "skill": GearBeltMallLogisticsSkill(target),
                "target_item": "transport-belt",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 900),
                "log_prefix": "strategy-gear-belt-mall",
            }
        if skill_name == "relocate_gear_belt_mall_to_iron_source":
            target = target_count or 20
            return {
                "skill": GearBeltMallRelocationSkill(target),
                "target_item": "transport-belt",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 900),
                "log_prefix": "strategy-gear-belt-mall-relocation",
            }
        if skill_name == "build_iron_plate_logistic_line_to_gear_mall":
            target = target_count or 40
            return {
                "skill": IronPlateLogisticLineToGearMallSkill(target),
                "target_item": "transport-belt",
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 1200),
                "log_prefix": "strategy-iron-plate-gear-mall-logistics",
            }
        if skill_name == "build_site_input_logistic_line":
            target = target_count or 40
            input_item = _strategy_site_input_item({"input_item": input_item})
            return {
                "skill": SiteInputLogisticLineSkill(target, item=input_item),
                "target_item": "transport-belt",
                "input_item": input_item,
                "target": target,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 1200),
                "log_prefix": "strategy-site-input-logistics",
            }
        if skill_name == "build_starter_defense":
            return {
                "skill": StarterDefenseSkill(),
                "target_item": "gun-turret",
                "target": 1,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 900),
                "log_prefix": "strategy-starter-defense",
            }
        if skill_name == "plan_factory_site":
            return {
                "skill": FactoryLayoutImprovementSkill(),
                "target_item": "layout-plan",
                "target": 1,
                "goal": skill_name,
                "max_steps": _max_steps(max_steps, 1),
                "log_prefix": "strategy-layout-improvement",
            }
        generated = self._generated_skill_run_config(skill_name, target_count, max_steps)
        if generated is not None:
            return generated
        return None

    def _generated_skill_run_config(
        self,
        skill_name: str,
        target_count: int | None = None,
        max_steps: int | None = None,
    ) -> dict[str, Any] | None:
        """Run config for a registered Qwen-generated skill, or None.

        Generated skills only expose ``next_action`` so the standard ``_run_skill``
        loop drives them exactly like hand-written skills. A load failure disables
        the entry so the autopilot redirects instead of crashing.
        """

        from . import skill_foundry

        try:
            entry = skill_foundry.registry_status(skill_name)
            if not isinstance(entry, dict) or entry.get("status") not in {"registered", "override_registered"}:
                return None
            skill_class = skill_foundry.load_generated_skill_class(entry)
            instance = skill_class()
        except Exception as exc:  # noqa: BLE001 - never crash the strategy loop
            try:
                skill_foundry.set_skill_status(skill_name, "failed", f"load failed: {exc}")
            except Exception:  # noqa: BLE001
                pass
            return None
        default_target = entry.get("default_target")
        default_steps = entry.get("default_max_steps")
        target = target_count or (int(default_target) if isinstance(default_target, (int, float)) else 20)
        default_max = int(default_steps) if isinstance(default_steps, (int, float)) else 1200
        return {
            "skill": instance,
            "target_item": entry.get("target_item") or "generated-skill",
            "target": target,
            "goal": skill_name,
            "max_steps": _max_steps(max_steps, default_max),
            "log_prefix": entry.get("log_prefix") or f"strategy-generated-{skill_name}",
        }

    def _run_skill(
        self,
        skill: Any,
        target_item: str,
        target: int,
        goal: str,
        max_steps: int,
        log_prefix: str,
        objective: str = "launch_rocket_program",
        log_path: Path | None = None,
    ) -> RunSummary:
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_path or self.cfg.log_dir / f"{log_prefix}-{_timestamp()}.jsonl"
        started_at = time.monotonic()
        initial_item_count: int | None = None
        last_step = 0
        bootstrap_seed_count = 0
        attempted_bootstrap_seeds: dict[tuple[Any, ...], tuple[str | None, int | None]] = {}

        def finish(ok: bool, reason: str, step: int, observation: dict[str, Any]) -> RunSummary:
            nonlocal initial_item_count
            final_item_count = total_item_count(observation, target_item)
            if initial_item_count is None:
                initial_item_count = final_item_count
            summary = RunSummary(ok, reason, step, final_item_count, log_path, target_item, bootstrap_seed_count)
            self._write_live_skill_heartbeat(
                objective,
                goal,
                "stopped" if ok else "failed",
                step=step,
                reason=reason,
            )
            record_skill_run_journal(
                self.cfg.log_dir,
                objective=objective,
                goal=goal,
                ok=ok,
                reason=reason,
                steps=step,
                item_name=target_item,
                initial_item_count=initial_item_count,
                final_item_count=final_item_count,
                target=target,
                max_steps=max_steps,
                log_path=log_path,
                duration_seconds=time.monotonic() - started_at,
                repo_root=self._journal_repo_root(),
            )
            return summary

        self._write_live_skill_heartbeat(
            objective,
            goal,
            "starting",
            step=0,
            reason=f"target {target} {target_item}",
        )
        try:
            with log_path.open("a", encoding="utf-8") as log_file:
                for step in range(1, max_steps + 1):
                    last_step = step
                    self._write_live_skill_heartbeat(objective, goal, "step", step=step)
                    self._wait_for_review_window()
                    observation = self._observe_for_skill_loop(goal, step)
                    if step == 1 or step % 25 == 0:
                        self._bank_observation_sample(observation)
                    if initial_item_count is None:
                        initial_item_count = total_item_count(observation, target_item)
                    self._maybe_progress_background_layout_work(observation, objective, goal, step)
                    decision = skill.next_action(observation)
                    observation, decision = self._maybe_retry_skill_with_planning_sites(skill, observation, decision)
                    decision = _guard_post_automation_handcraft(observation, decision)
                    self._write_log(log_file, step, observation, decision, None)
                    if decision.done:
                        self._maybe_progress_background_layout_work(observation, objective, goal, step, force_poll=True)
                        return finish(True, decision.reason, step, observation)
                    if decision.action is None:
                        self._maybe_progress_background_layout_work(observation, objective, goal, step, force_poll=True)
                        return finish(False, decision.reason, step, observation)

                    action = self._maybe_apply_remote_hint(observation, decision, goal)
                    seed_key = _bootstrap_seed_action_key(action)
                    if seed_key is not None and seed_key in attempted_bootstrap_seeds:
                        followup_item, previous_count = attempted_bootstrap_seeds[seed_key]
                        current_count = (
                            total_item_count(observation, followup_item)
                            if followup_item and previous_count is not None
                            else None
                        )
                        if current_count is None or current_count <= previous_count:
                            return finish(
                                False,
                                (
                                    "bootstrap seed already attempted without expected follow-up: "
                                    f"{action.get('seed_reason') or decision.reason}"
                                ),
                                step,
                                observation,
                            )
                    response = self.act(action)
                    self._write_log(log_file, step, observation, decision, response)
                    if not response.get("ok"):
                        if _stale_take_response(action, response):
                            time.sleep(0.2)
                            continue
                        return finish(False, f"action failed: {response.get('reason')}", step, observation)
                    remember_agent_layout_action(
                        self.cfg.runtime_dir,
                        action,
                        objective=objective,
                        active_skill=goal,
                        active_step=step,
                    )
                    if seed_key is not None:
                        followup_item = _bootstrap_seed_followup_item(action)
                        followup_count = total_item_count(observation, followup_item) if followup_item else None
                        attempted_bootstrap_seeds[seed_key] = (followup_item, followup_count)
                        bootstrap_seed_count += 1
                    if action.get("type") == "wait":
                        ticks = int(action.get("ticks") or 60)
                        # Factorio runs in real-time on the server: a long "wait for research /
                        # production / steam to fill" idles the agent for nothing -- the process
                        # completes in the background regardless. For such long waits, sleep briefly
                        # then YIELD the cycle so the strategy can do other (factory-expanding) work
                        # meanwhile; the stall watchdog rotates skills if it keeps re-picking the
                        # waiting one. Short settle-waits keep their (capped) brief sleep.
                        if ticks >= _WAIT_YIELD_TICKS:
                            time.sleep(0.2)
                            observation = self._observe_for_skill_loop(goal, step)
                            return finish(True, f"yielded for other work instead of idling: {decision.reason}", step, observation)
                        time.sleep(max(0.05, min(ticks / 60.0, 1.0)))
                    elif action.get("type") == "move_to":
                        if _virtual_move_response_arrived(response):
                            time.sleep(0.05)
                        else:
                            arrived, reason = self._wait_for_move(action)
                            if not arrived:
                                observation = self._observe_for_skill_loop(goal, step)
                                return finish(False, reason, step, observation)
                    else:
                        # Action applies synchronously over RCON; this is just pacing between the
                        # action and the next observe. A few game ticks is enough for state to settle.
                        time.sleep(0.1)

            observation = self._observe_for_skill_loop(goal, max_steps)
            self._maybe_progress_background_layout_work(observation, objective, goal, max_steps, force_poll=True)
            return finish(False, f"max steps reached: {max_steps}", max_steps, observation)
        except Exception as exc:
            self._write_live_skill_heartbeat(
                objective,
                goal,
                "error",
                step=last_step,
                reason=f"{type(exc).__name__}: {exc}",
            )
            raise

    def _observe_for_skill_loop(self, goal: str, step: int) -> dict[str, Any]:
        return self.observe()

    def _maybe_retry_skill_with_planning_sites(
        self,
        skill: Any,
        observation: dict[str, Any],
        decision: PlannerDecision,
    ) -> tuple[dict[str, Any], PlannerDecision]:
        return observation, decision

    def _autopilot_heartbeat_path(self) -> Path:
        return self.cfg.runtime_dir / "autopilot-heartbeat.json"

    def _idle_layout_process_path(self) -> Path:
        return self.cfg.runtime_dir / "idle-layout-loop.json"

    def _bank_observation_sample(self, observation: dict[str, Any]) -> None:
        from . import skill_foundry

        try:
            skill_foundry.record_observation_sample(self.cfg.log_dir, observation)
        except Exception:  # noqa: BLE001 - sampling is best-effort
            pass

    # ------------------------------------------------------------------ #
    # Self-development foundry loop (off the hot autopilot path)
    # ------------------------------------------------------------------ #

    def _skill_foundry_loop_path(self) -> Path:
        return self.cfg.runtime_dir / "skill-foundry-loop.json"

    def _write_skill_foundry_heartbeat(self, objective: str, state: str, **fields: Any) -> None:
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "pid": os.getpid(),
            "state": state,
            "objective": objective,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        payload.update(fields)
        try:
            with self._skill_foundry_loop_path().open("w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _foundry_candidates(self) -> list[dict[str, Any]]:
        """Specs to generate, priority-queue first then distinct unbuilt missing skills."""

        from . import skill_foundry

        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in skill_foundry.load_foundry_queue(self.cfg.runtime_dir):
            name = item.get("skill_name")
            if not name or name in seen:
                continue
            seen.add(name)
            mode = str(item.get("mode") or "new").strip().lower()
            # An implemented skill is only eligible in "override" (self-repair) mode; a plain "new"
            # request for an implemented name is a stale backlog entry and is dropped.
            if name in IMPLEMENTED_SKILLS and mode != "override":
                skill_foundry.remove_from_queue(self.cfg.runtime_dir, name)
                continue
            candidates.append(
                {
                    "skill_name": name,
                    "reason": item.get("reason") or "",
                    "blockers": item.get("blockers") or [],
                    "expected_effect": item.get("expected_effect") or "",
                    "target_item": item.get("target_item"),
                    "goal": name,
                    "priority": int(item.get("priority") or 50),
                    "mode": mode,
                }
            )
        for record in skill_foundry.distinct_missing_skills(self.cfg.runtime_dir):
            name = record.get("selected_skill")
            if not name or name in seen:
                continue
            seen.add(name)
            if name in IMPLEMENTED_SKILLS:
                continue
            candidates.append(
                {
                    "skill_name": name,
                    "reason": record.get("reason") or "",
                    "blockers": record.get("blockers") or [],
                    "expected_effect": record.get("expected_effect") or "",
                    "target_item": None,
                    "goal": name,
                    "priority": 40,
                }
            )
        # Proactive self-development (#3): when there is no reactive work, pre-generate catalog
        # skills that have no executor yet so the otherwise-idle GPUs keep advancing the system.
        # Lowest priority, so failing-skill repairs and strategy-requested skills always go first.
        # eligible_for_generation() skips anything already registered / in cooldown / quarantined.
        if self._foundry_proactive_enabled():
            from .strategy import SKILL_CATALOG

            for cat_name in SKILL_CATALOG:
                if cat_name in seen or cat_name in IMPLEMENTED_SKILLS:
                    continue
                seen.add(cat_name)
                candidates.append(
                    {
                        "skill_name": cat_name,
                        "reason": "proactive pre-generation (idle-GPU self-development)",
                        "blockers": [],
                        "expected_effect": "",
                        "target_item": None,
                        "goal": cat_name,
                        "priority": 20,
                    }
                )
        candidates.sort(key=lambda spec: spec.get("priority", 0), reverse=True)
        return candidates

    @staticmethod
    def _foundry_proactive_enabled() -> bool:
        return os.getenv("FACTORIO_AI_FOUNDRY_PROACTIVE", "").strip().lower() in {"1", "true", "yes", "on"}

    def run_skill_foundry_loop(
        self,
        objective: str = "launch_rocket_program",
        *,
        cycles: int = 0,
        sleep_seconds: float = 30.0,
        max_attempts: int | None = None,
        require_idle: bool = False,
        throttle_seconds: float = 0.0,
    ) -> dict[str, Any]:
        """Continuously generate+validate executors for un-built skills using idle GPU."""

        from . import skill_foundry
        from .run_journal import record_foundry_attempt_journal

        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        completed = 0
        generated = 0
        failed = 0
        last_attempt = 0.0
        while True:
            try:
                self._maybe_ensure_slurm_worker(reason="skill_foundry_cycle")
            except Exception:  # noqa: BLE001
                pass
            candidates = self._foundry_candidates()
            picked: dict[str, Any] | None = None
            skip_reason = "queue empty"
            for spec in candidates:
                ok, why = skill_foundry.eligible_for_generation(spec["skill_name"])
                if ok:
                    picked = spec
                    break
                skip_reason = f"{spec['skill_name']}: {why}"
            idle_ok = True
            if require_idle and picked is not None:
                idle_ok, idle_reason, _hb = self._autopilot_idle_for_layout(15.0)
                if not idle_ok:
                    skip_reason = f"waiting for idle: {idle_reason}"
            throttled = throttle_seconds > 0 and (time.monotonic() - last_attempt) < throttle_seconds
            if picked is not None and idle_ok and not throttled:
                self._write_skill_foundry_heartbeat(
                    objective, "generating", current_skill=picked["skill_name"], queue=[c["skill_name"] for c in candidates]
                )
                result = skill_foundry.develop_skill(self.cfg, picked, max_attempts=max_attempts)
                last_attempt = time.monotonic()
                skill_foundry.remove_from_queue(self.cfg.runtime_dir, picked["skill_name"])
                try:
                    record_foundry_attempt_journal(
                        self.cfg.log_dir,
                        objective=objective,
                        skill_name=picked["skill_name"],
                        result=result,
                        repo_root=self._journal_repo_root(),
                    )
                except Exception:  # noqa: BLE001
                    pass
                if result.get("ok"):
                    generated += 1
                    self._clear_codex_wait_state(picked["skill_name"], clear_reason="generated executor registered")
                else:
                    failed += 1
            else:
                self._write_skill_foundry_heartbeat(
                    objective, "idle", reason=skip_reason, queue=[c["skill_name"] for c in candidates]
                )
            completed += 1
            if cycles and completed >= cycles:
                break
            self._write_skill_foundry_heartbeat(objective, "sleeping", generated_total=generated, failed_total=failed)
            time.sleep(max(0.0, sleep_seconds))
        self._write_skill_foundry_heartbeat(objective, "stopped", generated_total=generated, failed_total=failed)
        return {"ok": True, "cycles": completed, "generated": generated, "failed": failed}

    def _live_skill_heartbeat_path(self) -> Path:
        return self.cfg.runtime_dir / "live-skill-heartbeat.json"

    def _write_live_skill_heartbeat(
        self,
        objective: str,
        goal: str,
        state: str,
        *,
        step: int,
        reason: str = "",
    ) -> None:
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "active": state not in {"stopped", "failed", "error", "interrupted"},
            "state": state,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "objective": objective,
            "skill": goal,
            "step": step,
            "reason": reason,
            "pid": os.getpid(),
        }
        with self._live_skill_heartbeat_path().open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)

    def _read_live_skill_heartbeat(self) -> dict[str, Any]:
        return _read_json_file(self._live_skill_heartbeat_path())

    def _clear_stale_live_skill_heartbeat_for_current_process(self, objective: str) -> None:
        live_skill = self._read_live_skill_heartbeat()
        if not live_skill.get("active"):
            return
        live_pid = _int_or_none(live_skill.get("pid"))
        if live_pid in {None, os.getpid()}:
            return
        if _pid_is_running(live_pid):
            return
        payload = dict(live_skill)
        payload.update(
            {
                "active": False,
                "state": "stale",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "objective": objective,
                "reason": f"cleared stale live skill pid {live_pid} before starting a new autopilot cycle",
                "pid": os.getpid(),
            }
        )
        try:
            with self._live_skill_heartbeat_path().open("w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _write_autopilot_heartbeat(
        self,
        objective: str,
        state: str,
        *,
        cycle: int,
        reason: str = "",
    ) -> None:
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        if state in {"starting", "cycle_start"}:
            self._clear_stale_live_skill_heartbeat_for_current_process(objective)
        payload = {
            "active": state not in {"stopped", "interrupted"},
            "state": state,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "objective": objective,
            "cycle": cycle,
            "reason": reason,
            "pid": os.getpid(),
        }
        with self._autopilot_heartbeat_path().open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)

    def _read_autopilot_heartbeat(self) -> dict[str, Any]:
        return _read_json_file(self._autopilot_heartbeat_path())

    def _autopilot_idle_for_layout(self, stale_seconds: float) -> tuple[bool, str, dict[str, Any]]:
        live_skill = self._read_live_skill_heartbeat()
        if live_skill.get("active"):
            live_pid = _int_or_none(live_skill.get("pid"))
            if live_pid and not _pid_is_running(live_pid):
                live_skill = dict(live_skill)
                live_skill["active"] = False
                live_skill["state"] = "stale"
                live_skill["stale_detected_at"] = datetime.now(timezone.utc).isoformat()
                live_skill["stale_reason"] = "recorded live skill pid is not running"
                with self._live_skill_heartbeat_path().open("w", encoding="utf-8") as file:
                    json.dump(live_skill, file, ensure_ascii=False, indent=2)
            else:
                live_updated_at = _parse_datetime(live_skill.get("updated_at"))
                live_stale_seconds = max(
                    stale_seconds,
                    _float_env("FACTORIO_AI_LIVE_SKILL_BUSY_STALE_SECONDS", 900.0),
                )
                if live_updated_at is not None:
                    live_age_seconds = (datetime.now(timezone.utc) - live_updated_at).total_seconds()
                    if live_age_seconds < live_stale_seconds:
                        heartbeat: dict[str, Any] = {"live_skill": dict(live_skill)}
                        heartbeat["live_skill"]["age_seconds"] = round(max(0.0, live_age_seconds), 3)
                        autopilot = self._read_autopilot_heartbeat()
                        if autopilot:
                            heartbeat["autopilot"] = autopilot
                        skill_name = str(live_skill.get("skill") or "unknown")
                        state = str(live_skill.get("state") or "active")
                        return False, f"live skill is active: {skill_name} {state}", heartbeat
        heartbeat = self._read_autopilot_heartbeat()
        if not heartbeat:
            return True, "autopilot heartbeat missing", {}
        state = str(heartbeat.get("state") or "")
        if state in {"sleeping", "stopped", "interrupted", "cycle_failed", "cycle_error"}:
            return True, f"autopilot state is {state}", heartbeat
        updated_at = _parse_datetime(heartbeat.get("updated_at"))
        if updated_at is None:
            return True, "autopilot heartbeat has no valid timestamp", heartbeat
        age_seconds = (datetime.now(timezone.utc) - updated_at).total_seconds()
        heartbeat = dict(heartbeat)
        heartbeat["age_seconds"] = round(max(0.0, age_seconds), 3)
        if age_seconds >= stale_seconds:
            return True, f"autopilot heartbeat stale for {age_seconds:.1f}s", heartbeat
        return False, f"autopilot is active: {state}", heartbeat

    def _codex_wait_path(self) -> Path:
        return self.cfg.runtime_dir / "codex-wait.json"

    def _record_codex_wait_state(
        self,
        objective: str,
        selected_skill: str,
        reason: str,
        strategy: dict[str, Any],
    ) -> None:
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        path = self._codex_wait_path()
        previous = self._read_codex_wait_state()
        started_at = previous.get("started_at") if previous.get("selected_skill") == selected_skill else None
        payload = {
            "active": True,
            "started_at": started_at or datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "objective": objective,
            "selected_skill": selected_skill,
            "active_skill": f"codex_wait:{selected_skill}",
            "reason": reason,
            "strategy": strategy,
            "layout_work": {
                "enabled": True,
                "mode": os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_MODE", "attach"),
                "until": "Codex implements the missing deterministic executor and the skill becomes executable.",
            },
        }
        with path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        self._maybe_start_codex_wait_layout_loop(objective)

    def _read_codex_wait_state(self) -> dict[str, Any]:
        path = self._codex_wait_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _clear_codex_wait_state(
        self,
        selected_skill: str,
        *,
        clear_reason: str = "deterministic executor is now available",
    ) -> bool:
        state = self._read_codex_wait_state()
        if not state.get("active") or state.get("selected_skill") != selected_skill:
            return False
        state["active"] = False
        state["cleared_at"] = datetime.now(timezone.utc).isoformat()
        state["clear_reason"] = clear_reason
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        with self._codex_wait_path().open("w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
        return True

    def _codex_wait_layout_process_path(self) -> Path:
        return self.cfg.runtime_dir / "codex-wait-layout-loop.json"

    def _codex_wait_layout_cli_command(self) -> str:
        return "run-codex-wait-layout-loop"

    def _maybe_start_codex_wait_layout_loop(self, objective: str) -> None:
        if not self.cfg.slurm_enabled:
            return
        if os.getenv("FACTORIO_AI_CODEX_WAIT_LAYOUT_AUTOSTART", "0").lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return

        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        process_path = self._codex_wait_layout_process_path()
        existing = _read_json_file(process_path)
        existing_pid = _int_or_none(existing.get("pid")) if isinstance(existing, dict) else None
        if existing_pid and _pid_is_running(existing_pid):
            self._write_background_layout_log(
                {
                    "event": "layout_codex_wait_loop_already_running",
                    "pid": existing_pid,
                    "objective": objective,
                }
            )
            return
        if existing_pid and not _pid_is_running(existing_pid):
            existing["state"] = "stale"
            existing["stale_detected_at"] = datetime.now(timezone.utc).isoformat()
            existing["stale_reason"] = "recorded Codex wait layout loop pid is not running"
            with process_path.open("w", encoding="utf-8") as file:
                json.dump(existing, file, ensure_ascii=False, indent=2)
            self._write_background_layout_log(
                {
                    "event": "layout_codex_wait_loop_stale_pid_recovered",
                    "pid": existing_pid,
                    "objective": objective,
                }
            )

        sleep_seconds = os.getenv(
            "FACTORIO_AI_CODEX_WAIT_LAYOUT_SLEEP_SECONDS",
            os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS", "20"),
        )
        command = [
            sys.executable,
            "-m",
            "factorio_ai.cli",
            self._codex_wait_layout_cli_command(),
            "--objective",
            objective,
            "--cycles",
            "0",
            "--sleep-seconds",
            sleep_seconds,
        ]
        env = os.environ.copy()
        src_path = str(REPO_ROOT / "src")
        current_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = src_path if not current_pythonpath else f"{src_path}{os.pathsep}{current_pythonpath}"

        kwargs: dict[str, Any] = {
            "cwd": str(REPO_ROOT),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess,
                "DETACHED_PROCESS",
                0,
            )
        try:
            proc = subprocess.Popen(command, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self._write_background_layout_log(
                {
                    "event": "layout_codex_wait_loop_start_failed",
                    "objective": objective,
                    "command": command,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return

        state = {
            "pid": proc.pid,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "objective": objective,
            "command": command,
        }
        with process_path.open("w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
        self._write_background_layout_log(
            {
                "event": "layout_codex_wait_loop_started",
                "pid": proc.pid,
                "objective": objective,
                "command": command,
            }
        )

    def _maybe_progress_codex_wait_layout(self, objective: str, *, phase: str = "cycle_start") -> None:
        state = self._read_codex_wait_state()
        if not state.get("active"):
            return
        selected = str(state.get("selected_skill") or "")
        if not selected:
            return
        wait_objective = str(state.get("objective") or objective)
        try:
            observation = self.observe()
        except Exception as exc:  # noqa: BLE001
            self._write_background_layout_log(
                {
                    "event": "layout_codex_wait_observe_failed",
                    "phase": phase,
                    "active_skill": f"codex_wait:{selected}",
                    "active_step": 0,
                    "block_reason": state.get("reason"),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return
        self._write_background_layout_log(
            {
                "event": "layout_codex_wait_heartbeat",
                "phase": phase,
                "active_skill": f"codex_wait:{selected}",
                "active_step": 0,
                "block_reason": state.get("reason"),
            }
        )
        self._maybe_progress_background_layout_work(
            observation,
            wait_objective,
            f"codex_wait:{selected}",
            0,
        )

    def _maybe_progress_background_layout_for_blocked_strategy(
        self,
        objective: str,
        selected_skill: str,
        reason: str,
    ) -> None:
        if os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_ON_BLOCKED_STRATEGY", "1").lower() in {
            "0",
            "false",
            "no",
            "off",
        }:
            return
        try:
            observation = self.observe()
        except Exception as exc:  # noqa: BLE001
            self._write_background_layout_log(
                {
                    "event": "layout_blocked_strategy_observe_failed",
                    "active_skill": f"codex_wait:{selected_skill}",
                    "active_step": 0,
                    "block_reason": reason,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return
        self._write_background_layout_log(
            {
                "event": "layout_blocked_strategy_detected",
                "active_skill": f"codex_wait:{selected_skill}",
                "active_step": 0,
                "block_reason": reason,
            }
        )
        self._maybe_progress_background_layout_work(
            observation,
            objective,
            f"codex_wait:{selected_skill}",
            0,
        )

    def _background_layout_max_active_tasks(self) -> int:
        return int(load_layout_llm_settings(self.cfg.runtime_dir)["max_active_layout_tasks"])

    def _record_background_layout_thread_result(self, payload: dict[str, Any]) -> None:
        self._background_layout_thread_result = payload
        with self._background_layout_thread_result_lock:
            self._background_layout_thread_results.append(payload)

    def _collect_background_layout_threads(self) -> None:
        running = [thread for thread in self._background_layout_threads if thread.is_alive()]
        self._background_layout_threads = running
        self._background_layout_thread = running[0] if running else None

        with self._background_layout_thread_result_lock:
            results = list(self._background_layout_thread_results)
            self._background_layout_thread_results.clear()
        for result in results:
            self._write_background_layout_log(result)
        if results:
            self._background_layout_thread_result = results[-1]

    def _maybe_progress_background_layout_work(
        self,
        observation: dict[str, Any],
        objective: str,
        active_skill: str,
        active_step: int,
        *,
        force_poll: bool = False,
        minimum_interval_seconds: float | None = None,
    ) -> None:
        self._maybe_record_human_layout_learning(
            observation,
            objective,
            active_skill,
            active_step,
            source="background_layout_observe",
        )
        if not self.cfg.slurm_enabled or active_skill == "plan_factory_site":
            return
        if os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_ENABLED", "1").lower() in {"0", "false", "no", "off"}:
            return
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._maybe_ensure_slurm_worker(reason="background_layout_work")
            from . import remote_slurm

            self._collect_background_layout_threads()

            if self._background_layout_task_name:
                state, data, raw = remote_slurm.read_task_state(self._background_layout_task_name)
                if state in {"result", "failed", "missing", "unknown"} or force_poll:
                    if state == "result" and data is not None:
                        self._write_background_layout_log(
                            {
                                "event": "layout_result",
                                "task": self._background_layout_task_name,
                                "objective": objective,
                                "active_skill": active_skill,
                                "active_step": active_step,
                                "state": state,
                                "result": data,
                            }
                        )
                        self._background_layout_task_name = None
                    elif state in {"failed", "missing", "unknown"}:
                        self._write_background_layout_log(
                            {
                                "event": "layout_task_unavailable",
                                "task": self._background_layout_task_name,
                                "active_skill": active_skill,
                                "active_step": active_step,
                                "state": state,
                                "data": data,
                                "raw": raw[:500],
                            }
                        )
                        self._background_layout_task_name = None

            if self._background_layout_task_name:
                return
            if force_poll:
                return
            mode = os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_MODE", "attach").strip().lower()
            max_active_layout_tasks = self._background_layout_max_active_tasks()
            running_layout_workers = len(self._background_layout_threads)
            if mode in {"attach", "attached", "srun", "scheduler", "slurm_scheduler"} and (
                running_layout_workers >= max_active_layout_tasks
            ):
                return
            interval = (
                float(minimum_interval_seconds)
                if minimum_interval_seconds is not None
                else float(os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS", "20"))
            )
            now = time.monotonic()
            if mode in {"scheduler", "slurm_scheduler"} and now < self._background_layout_scheduler_not_ready_until:
                return
            if now - self._background_layout_last_submit < interval:
                return
            if mode in {"scheduler", "slurm_scheduler"}:
                status = remote_slurm.layout_improvement_status(max_active_layout_tasks=max_active_layout_tasks)
                if not status.get("llm_ready"):
                    self._background_layout_last_submit = now
                    self._background_layout_scheduler_not_ready_until = now + float(
                        os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_SCHEDULER_NOT_READY_INTERVAL_SECONDS", "60")
                    )
                    remote = status.get("remote") if isinstance(status.get("remote"), dict) else {}
                    self._write_background_layout_log(
                        {
                            "event": "layout_scheduler_waiting_for_ready_gpu",
                            "mode": mode,
                            "active_skill": active_skill,
                            "active_step": active_step,
                            "missing": status.get("missing") or [],
                            "gpu_model_candidates": remote.get("gpu_model_candidates") or [],
                            "selected_gpu_model": remote.get("selected_gpu_model"),
                            "scheduler_ready_free_gpus": remote.get("scheduler_ready_free_gpus"),
                            "pending_gpu_tasks": remote.get("pending_gpu_tasks"),
                            "active_layout_tasks": remote.get("active_layout_tasks"),
                            "max_active_layout_tasks": remote.get("max_active_layout_tasks"),
                            "active_layout_capacity_remaining": remote.get("active_layout_capacity_remaining"),
                            "pending_gpu_allocations": remote.get("pending_gpu_allocations") or [],
                        }
                    )
                    return
                self._background_layout_scheduler_not_ready_until = 0.0

            targets = load_targets(self.cfg.runtime_dir, objective)
            monitor = summarize_factory(observation, objective, production_targets=targets.per_minute)
            validation_feedback = layout_validation_feedback_summary(self.cfg.log_dir)
            selected_improvement_site = load_selected_improvement_site(self.cfg.runtime_dir, objective)
            self._background_layout_last_submit = now
            if mode in {"attach", "attached", "srun", "scheduler", "slurm_scheduler"}:
                thread = threading.Thread(
                    target=self._background_layout_attached_worker,
                    args=(
                        objective,
                        active_skill,
                        active_step,
                        observation,
                        targets.per_minute,
                        monitor,
                        validation_feedback,
                        selected_improvement_site,
                    ),
                    daemon=True,
                )
                self._background_layout_threads.append(thread)
                self._background_layout_thread = thread
                thread.start()
                self._write_background_layout_log(
                    {
                        "event": "layout_attached_started",
                        "mode": mode,
                        "active_skill": active_skill,
                        "active_step": active_step,
                        "running_layout_workers": running_layout_workers + 1,
                        "max_active_layout_tasks": max_active_layout_tasks,
                    }
                )
            else:
                task = {
                    "type": "layout_improvement_request",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "payload": {
                        "objective": objective,
                        "active_skill": active_skill,
                        "active_step": active_step,
                        "observation": observation,
                        "production_targets": targets.per_minute,
                        "factory_monitor": monitor,
                        "layout_validation_feedback": validation_feedback,
                        "selected_improvement_site": selected_improvement_site,
                        "layout_learning": remote_slurm.layout_learning_request_context(),
                    },
                }
                self._background_layout_task_name = remote_slurm.submit_task(task)
                self._write_background_layout_log(
                    {
                        "event": "layout_task_submitted",
                        "task": self._background_layout_task_name,
                        "active_skill": active_skill,
                        "active_step": active_step,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            self._write_background_layout_log(
                {
                    "event": "layout_background_error",
                    "active_skill": active_skill,
                    "active_step": active_step,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    def _maybe_record_human_layout_learning(
        self,
        observation: dict[str, Any],
        objective: str,
        active_skill: str,
        active_step: int,
        *,
        source: str,
    ) -> None:
        if os.getenv("FACTORIO_AI_HUMAN_LAYOUT_LEARNING", "1").strip().lower() in {"0", "false", "no", "off"}:
            return
        try:
            event = record_human_layout_observation(
                self.cfg.runtime_dir,
                self.cfg.log_dir,
                observation,
                objective=objective,
                active_skill=active_skill,
                active_step=active_step,
                source=source,
            )
        except Exception as exc:  # noqa: BLE001 - learning trace must not break the game loop
            self._write_background_layout_log(
                {
                    "event": "human_layout_learning_error",
                    "active_skill": active_skill,
                    "active_step": active_step,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return
        if event is not None:
            self._write_background_layout_log(
                {
                    "event": "operator_intervention_candidate_recorded",
                    "active_skill": active_skill,
                    "active_step": active_step,
                    "delta_summary": event.get("delta_summary"),
                    "trace": "operator-intervention-layout-learning.jsonl",
                    "learning_label": event.get("learning_label"),
                }
            )

    def _background_layout_attached_worker(
        self,
        objective: str,
        active_skill: str,
        active_step: int,
        observation: dict[str, Any],
        production_targets: dict[str, float],
        monitor: dict[str, Any],
        validation_feedback: dict[str, Any],
        selected_improvement_site: dict[str, Any],
    ) -> None:
        try:
            from . import remote_slurm

            result = remote_slurm.request_layout_improvement(
                objective,
                active_skill,
                active_step,
                observation,
                production_targets=production_targets,
                factory_monitor=monitor,
                layout_validation_feedback=validation_feedback,
                selected_improvement_site=selected_improvement_site,
                timeout_seconds=int(os.getenv("FACTORIO_AI_BACKGROUND_LAYOUT_TIMEOUT_SECONDS", "180")),
                force_attached=True,
                max_active_layout_tasks=self._background_layout_max_active_tasks(),
            )
            # Persist the layout LLM I/O trace (and strip it from the result) so the dashboard
            # shows layout_improvement calls alongside strategy/foundry, not just strategy.
            result = _record_and_strip_llm_io_traces(self.cfg.log_dir, result)
            self._record_background_layout_thread_result(
                {
                    "event": "layout_result",
                    "mode": "attach",
                    "objective": objective,
                    "active_skill": active_skill,
                    "active_step": active_step,
                    "result": result,
                }
            )
        except Exception as exc:  # noqa: BLE001
            self._record_background_layout_thread_result(
                {
                    "event": "layout_background_error",
                    "mode": "attach",
                    "active_skill": active_skill,
                    "active_step": active_step,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    def _write_background_layout_log(self, payload: dict[str, Any]) -> None:
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.cfg.log_dir / "layout-improvement-background.jsonl"
        row = {"time": datetime.now(timezone.utc).isoformat(), **payload}
        with path.open("a", encoding="utf-8") as file:
            json.dump(row, file, ensure_ascii=False, separators=(",", ":"))
            file.write("\n")
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        if row.get("event") == "layout_result" and result:
            record_layout_result_insight(
                self.cfg.log_dir,
                objective=str(row.get("objective") or "launch_rocket_program"),
                active_skill=str(row.get("active_skill") or ""),
                result=result,
                repo_root=self._journal_repo_root(),
            )

    def _journal_repo_root(self) -> Path:
        runtime_dir = Path(self.cfg.runtime_dir)
        if runtime_dir.name == "runtime":
            return runtime_dir.parent
        return REPO_ROOT

    def _client(self) -> FactorioRconClient:
        return FactorioRconClient(self.cfg.rcon_host, self.cfg.rcon_port, self.cfg.rcon_password)

    def _agent_parameter(self) -> dict[str, Any]:
        player_name = self._configured_agent_player_name()
        if player_name:
            return {"player_name": player_name}
        return {}

    def _agent_action(self, action: dict[str, Any]) -> dict[str, Any]:
        player_name = self._configured_agent_player_name()
        if not player_name or "player_name" in action:
            return action
        targeted = dict(action)
        targeted["player_name"] = player_name
        return targeted

    def _configured_agent_player_name(self) -> str:
        player_name = str(self.cfg.agent_player_name or "").strip()
        if player_name.lower() in {"auto", "connected", "first-connected", "*"}:
            return ""
        return player_name

    def _wait_for_move(self, action: dict[str, Any]) -> tuple[bool, str]:
        target = action.get("position")
        if not isinstance(target, dict):
            return False, "move_to did not include a target position"

        first_observation = self.observe()
        start_position = player_position(first_observation)
        initial_distance = distance(start_position, target)
        tolerance = float(action.get("tolerance") or 4.0)
        timeout_seconds = min(240.0, max(20.0, initial_distance * 3.0 + 10.0))
        stall_timeout_seconds = float(action.get("stall_timeout_seconds") or 8.0)
        deadline = time.monotonic() + timeout_seconds
        last_distance = initial_distance
        best_distance = initial_distance
        last_progress_at = time.monotonic()
        detour_attempts = 0
        max_detour_attempts = int(action.get("max_detour_attempts") or 8)

        while time.monotonic() < deadline:
            if self._review_lock_active():
                self.stop_agent()
                self._wait_for_review_window()
                return False, "move paused for GUI review"
            observation = self.observe()
            current_position = player_position(observation)
            current_distance = distance(current_position, target)
            last_distance = current_distance
            if current_distance <= tolerance:
                self.stop_agent()
                return True, f"arrived at move target within {current_distance:.2f} tiles"

            if current_distance < best_distance - 0.15:
                best_distance = current_distance
                detour_attempts = 0
                last_progress_at = time.monotonic()
            elif time.monotonic() - last_progress_at > stall_timeout_seconds:
                if detour_attempts < max_detour_attempts:
                    detour = _move_detour_action(current_position, target, detour_attempts)
                    detour_attempts += 1
                    refresh = self.act(detour)
                    if not refresh.get("ok"):
                        self.stop_agent()
                        return False, f"move detour failed: {refresh.get('reason')}"
                    time.sleep(0.5)
                    detour_observation = self.observe()
                    detour_position = player_position(detour_observation)
                    if distance(detour_position, current_position) > 0.2:
                        last_progress_at = time.monotonic()
                        last_distance = distance(detour_position, target)
                        continue
                self.stop_agent()
                return False, f"move made no progress; remaining distance {current_distance:.2f}"

            refresh = self.act(action)
            if not refresh.get("ok"):
                self.stop_agent()
                return False, f"move refresh failed: {refresh.get('reason')}"
            time.sleep(0.5)

        self.stop_agent()
        return False, f"move_to timed out; remaining distance {last_distance:.2f}"

    def _review_lock_active(self) -> bool:
        return (self.cfg.runtime_dir / "review.lock").exists()

    def _wait_for_review_window(self) -> None:
        if not self._review_lock_active():
            return
        self.stop_agent()
        while self._review_lock_active():
            time.sleep(1.0)

    def _maybe_apply_remote_hint(
        self,
        observation: dict[str, Any],
        decision: PlannerDecision,
        goal: str,
    ) -> dict[str, Any]:
        if not self.cfg.slurm_enabled or decision.action is None:
            return decision.action or {"type": "wait", "ticks": 60}
        if os.getenv("FACTORIO_AI_REMOTE_ACTION_HINT_ENABLED", "0").lower() not in {"1", "true", "yes", "on"}:
            return decision.action
        try:
            from . import remote_slurm

            status = self._remote_llm_status()
            if not status.get("llm_ready"):
                return decision.action
            result = remote_slurm.request_plan(
                observation=observation,
                legal_actions=[decision.action],
                goal=goal,
                timeout_seconds=5,
            )
            hint = result.get("action_hint")
            if isinstance(hint, dict):
                validate_action(hint)
                return hint
        except (Exception, ActionValidationError):
            return decision.action
        return decision.action

    def _remote_llm_status(self, *, refresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not refresh and self._remote_llm_status_cache is not None and now < self._remote_llm_status_cache_until:
            return self._remote_llm_status_cache

        from . import remote_slurm

        status = remote_slurm.llm_status()
        self._remote_llm_status_cache = status
        self._remote_llm_status_cache_until = now + 30.0
        return status

    def _slurm_renewal_state_path(self) -> Path:
        return self.cfg.runtime_dir / "slurm-renewal.json"

    def _slurm_renewal_log_path(self) -> Path:
        return self.cfg.log_dir / "slurm-renewal.jsonl"

    def _maybe_ensure_slurm_worker(self, *, reason: str, force: bool = False) -> None:
        if not self.cfg.slurm_enabled:
            return
        if os.getenv("FACTORIO_AI_SLURM_AUTO_RENEW_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
            return
        interval_seconds = max(60.0, _float_env("FACTORIO_AI_SLURM_RENEW_CHECK_INTERVAL_SECONDS", 1800.0))
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        state_path = self._slurm_renewal_state_path()
        state = _read_json_file(state_path)
        now = datetime.now(timezone.utc)
        checked_at = _parse_datetime(state.get("checked_at"))
        if not force and checked_at is not None and (now - checked_at).total_seconds() < interval_seconds:
            return
        renew_before_minutes = _int_or_none(os.getenv("FACTORIO_AI_SLURM_RENEW_BEFORE_MINUTES"))
        payload: dict[str, Any] = {
            "event": "slurm_worker_renewal_check",
            "checked_at": now.isoformat(),
            "reason": reason,
            "renew_before_minutes": renew_before_minutes,
            "interval_seconds": interval_seconds,
        }
        try:
            from . import remote_slurm

            result = remote_slurm.ensure_worker_job(renew_before_minutes=renew_before_minutes)
            payload["ok"] = bool(result.get("ok"))
            payload["action"] = result.get("action")
            payload["result"] = result
        except Exception as exc:  # noqa: BLE001
            payload["ok"] = False
            payload["error"] = f"{type(exc).__name__}: {exc}"
        with state_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        with self._slurm_renewal_log_path().open("a", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
            file.write("\n")

    @staticmethod
    def _remote_llm_unready_reason(status: dict[str, Any]) -> str:
        missing = status.get("missing") if isinstance(status.get("missing"), list) else []
        parts = [str(item) for item in missing if str(item)]
        remote = status.get("remote") if isinstance(status.get("remote"), dict) else {}
        jobs = remote.get("pending_jobs") or remote.get("jobs")
        if isinstance(jobs, list) and jobs:
            job = jobs[0] if isinstance(jobs[0], dict) else {}
            if isinstance(job, dict):
                job_bits = [
                    str(job.get("id") or ""),
                    str(job.get("state") or ""),
                    str(job.get("reason") or ""),
                    str(job.get("start_time") or ""),
                ]
                job_text = " ".join(bit for bit in job_bits if bit)
                if job_text:
                    parts.append(f"job {job_text}")
        return "; ".join(parts) if parts else "remote Slurm LLM is not ready"

    @staticmethod
    def _write_log(
        log_file: Any,
        step: int,
        observation: dict[str, Any],
        decision: PlannerDecision,
        response: dict[str, Any] | None,
    ) -> None:
        payload = {
            "time": datetime.now(timezone.utc).isoformat(),
            "step": step,
            "tick": observation.get("tick"),
            "decision": {
                "action": decision.action,
                "reason": decision.reason,
                "done": decision.done,
                "metadata": decision.metadata,
            },
            "response": response,
            "inventory": observation.get("inventory"),
            "player": observation.get("player"),
        }
        json.dump(payload, log_file, ensure_ascii=False, separators=(",", ":"))
        log_file.write("\n")
        log_file.flush()


class ModlessFactorioController(FactorioController):
    """Reuse skill loops with the no-custom-mod RCON/Lua adapter."""

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__(cfg)
        self._modless = ModlessLuaController(cfg)
        self._planning_sites_cache: dict[str, Any] = {}
        self._planning_sites_cache_loaded = False

    def run_strategy_step(
        self,
        objective: str = "launch_rocket_program",
        require_llm: bool = False,
        target_count: int | None = None,
        target_item: str | None = None,
        input_item: str | None = None,
        max_steps: int | None = None,
        override_skill: str | None = None,
        skip_remote_strategy: bool = False,
    ) -> StrategyStepSummary:
        if _real_player_execution_required():
            observation = self.observe()
            observation, recovery_problem = self._maybe_restore_real_player_controller(observation)
            if recovery_problem:
                return StrategyStepSummary(
                    ok=False,
                    reason=recovery_problem,
                    objective=objective,
                    selected_skill="",
                    strategy={
                        "selected_skill": "",
                        "source": "execution_guard",
                        "reason": recovery_problem,
                        "player": observation.get("player"),
                        "execution": observation.get("execution"),
                    },
                )
            problem = _real_player_execution_problem(observation)
            if problem:
                return StrategyStepSummary(
                    ok=False,
                    reason=problem,
                    objective=objective,
                    selected_skill="",
                    strategy={
                        "selected_skill": "",
                        "source": "execution_guard",
                        "reason": problem,
                        "player": observation.get("player"),
                        "execution": observation.get("execution"),
                    },
                )
            threat = _real_player_enemy_action_threat(observation, {"type": "wait", "ticks": 1})
            if threat:
                stop_response = self._modless.act(
                    {"type": "stop"},
                    player_name=self._configured_agent_player_name(),
                )
                return StrategyStepSummary(
                    ok=False,
                    reason=threat,
                    objective=objective,
                    selected_skill="build_starter_defense",
                    strategy={
                        "selected_skill": "build_starter_defense",
                        "source": "execution_guard",
                        "reason": threat,
                        "expected_effect": "Pause real-player movement before enemy contact kills the character.",
                        "player": observation.get("player"),
                        "execution": observation.get("execution"),
                        "stop_response": stop_response,
                    },
                )
        return super().run_strategy_step(
            objective=objective,
            require_llm=require_llm,
            target_count=target_count,
            target_item=target_item,
            input_item=input_item,
            max_steps=max_steps,
            override_skill=override_skill,
            skip_remote_strategy=skip_remote_strategy,
        )

    def observe(self) -> dict[str, Any]:
        return self._observe_modless(include_planning_sites=False)

    def _observe_for_idle_layout(self) -> dict[str, Any]:
        return self._observe_modless(include_planning_sites=False)

    def _observe_for_skill_loop(self, goal: str, step: int) -> dict[str, Any]:
        # Per-step skill observe: drop the radius-768 far scans and trim resources to the nearest
        # few per type (skills only act on nearby geometry + nearest resources). Full observes still
        # happen for strategy decisions and the planning-site retry path.
        if goal in {
            "build_site_input_logistic_line",
            "automate_electronic_circuit_line",
            "expand_iron_smelting",
            "expand_copper_smelting",
        }:
            return self._observe_modless(include_planning_sites=False, lightweight=False)
        return self._observe_modless(include_planning_sites=False, lightweight=True)

    def _maybe_retry_skill_with_planning_sites(
        self,
        skill: Any,
        observation: dict[str, Any],
        decision: PlannerDecision,
    ) -> tuple[dict[str, Any], PlannerDecision]:
        if not _planning_site_retry_needed(decision):
            return observation, decision
        self._load_planning_sites_cache(observation)
        if self._planning_sites_cache_is_fresh():
            cached_observation = self._merge_cached_planning_sites(observation)
            cached_decision = skill.next_action(cached_observation)
            return cached_observation, cached_decision
        full_observation = self._observe_modless(include_planning_sites=True)
        return full_observation, skill.next_action(full_observation)

    def _observe_modless(self, *, include_planning_sites: bool, lightweight: bool = False) -> dict[str, Any]:
        response = self._modless.observe(
            player_name=self._configured_agent_player_name(),
            include_planning_sites=include_planning_sites,
            lightweight=lightweight,
        )
        if not response.get("ok"):
            raise RuntimeError(f"no-mod observe failed: {response}")
        memory = self._remember_world_map(response, include_planning_sites=include_planning_sites)
        if include_planning_sites:
            self._update_planning_sites_cache(response)
            return merge_world_map_memory_into_observation(
                response,
                memory,
                max_age_seconds=_planning_site_cache_seconds(),
            )
        self._load_planning_sites_cache(response)
        merged = self._merge_cached_planning_sites(response)
        return merge_world_map_memory_into_observation(
            merged,
            memory,
            max_age_seconds=_planning_site_cache_seconds(),
        )

    def _update_planning_sites_cache(self, observation: dict[str, Any]) -> None:
        for key in ("power_sites", "lab_sites", "automation_sites"):
            sites = observation.get(key)
            if isinstance(sites, list):
                self._planning_sites_cache[key] = sites
        self._planning_sites_cache["tick"] = observation.get("tick")
        self._planning_sites_cache["cached_at"] = time.time()
        self._persist_planning_sites_cache()

    def _merge_cached_planning_sites(self, observation: dict[str, Any]) -> dict[str, Any]:
        if not self._planning_sites_cache or not self._planning_sites_cache_is_fresh():
            return observation
        merged = dict(observation)
        for key in ("power_sites", "lab_sites", "automation_sites"):
            if key in self._planning_sites_cache:
                merged[key] = self._planning_sites_cache[key]
        merged["planning_sites_cached_from_tick"] = self._planning_sites_cache.get("tick")
        cached_at = self._planning_sites_cache.get("cached_at")
        if isinstance(cached_at, (int, float)):
            merged["planning_sites_cache_age_seconds"] = round(max(0.0, time.time() - cached_at), 3)
        return merged

    def _planning_sites_cache_path(self) -> Path:
        return self.cfg.runtime_dir / "planning-sites-cache.json"

    def _load_planning_sites_cache(self, observation: dict[str, Any] | None = None) -> None:
        if self._planning_sites_cache_loaded:
            return
        self._planning_sites_cache_loaded = True
        path = self._planning_sites_cache_path()
        if not path.exists():
            self._load_planning_sites_cache_from_world_memory(observation)
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._load_planning_sites_cache_from_world_memory(observation)
            return
        if not isinstance(data, dict):
            self._load_planning_sites_cache_from_world_memory(observation)
            return
        cached_at = data.get("cached_at")
        if not isinstance(cached_at, (int, float)) or time.time() - cached_at > _planning_site_cache_seconds():
            self._load_planning_sites_cache_from_world_memory(observation)
            return
        cached_tick = data.get("tick")
        if _planning_site_cache_tick_stale(cached_tick, observation.get("tick") if isinstance(observation, dict) else None):
            self._load_planning_sites_cache_from_world_memory(observation)
            return
        cache: dict[str, Any] = {"cached_at": cached_at, "tick": cached_tick}
        for key in ("power_sites", "lab_sites", "automation_sites"):
            sites = data.get(key)
            if isinstance(sites, list):
                cache[key] = sites
        if any(key in cache for key in ("power_sites", "lab_sites", "automation_sites")):
            self._planning_sites_cache = cache
        else:
            self._load_planning_sites_cache_from_world_memory(observation)

    def _load_planning_sites_cache_from_world_memory(self, observation: dict[str, Any] | None = None) -> None:
        memory = load_world_map_memory(self.cfg.runtime_dir)
        if not planning_sites_are_fresh(memory, max_age_seconds=_planning_site_cache_seconds()):
            return
        cache = planning_sites_from_memory(memory)
        if _planning_site_cache_tick_stale(
            cache.get("tick"),
            observation.get("tick") if isinstance(observation, dict) else None,
        ):
            return
        if any(key in cache for key in ("power_sites", "lab_sites", "automation_sites")):
            self._planning_sites_cache = cache

    def _planning_sites_cache_is_fresh(self) -> bool:
        cached_at = self._planning_sites_cache.get("cached_at")
        return isinstance(cached_at, (int, float)) and time.time() - cached_at <= _planning_site_cache_seconds()

    def _persist_planning_sites_cache(self) -> None:
        payload = {
            "cached_at": self._planning_sites_cache.get("cached_at"),
            "tick": self._planning_sites_cache.get("tick"),
        }
        for key in ("power_sites", "lab_sites", "automation_sites"):
            if key in self._planning_sites_cache:
                payload[key] = self._planning_sites_cache[key]
        try:
            self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
            self._planning_sites_cache_path().write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _remember_world_map(self, observation: dict[str, Any], *, include_planning_sites: bool) -> dict[str, Any]:
        try:
            return update_world_map_memory(
                self.cfg.runtime_dir,
                observation,
                include_planning_sites=include_planning_sites,
                source="no-mod-full-planning-observe" if include_planning_sites else "no-mod-lightweight-observe",
            )
        except OSError:
            return load_world_map_memory(self.cfg.runtime_dir)

    def act(self, action: dict[str, Any]) -> dict[str, Any]:
        validate_action(action)
        observation: dict[str, Any] | None = None
        if action.get("type") == "craft" and action.get("recipe") == "iron-gear-wheel":
            observation = self.observe()
            guard_reason = _gear_handcraft_guard_reason(observation, action)
            if guard_reason:
                return {
                    "ok": False,
                    "reason": guard_reason,
                    "mode": "modless-rcon-lua",
                    "player": observation.get("player"),
                    "execution": observation.get("execution"),
                }
        if _real_player_execution_required():
            observation = observation or self.observe()
            observation, recovery_problem = self._maybe_restore_real_player_controller(observation)
            if recovery_problem:
                return {
                    "ok": False,
                    "reason": recovery_problem,
                    "mode": "modless-rcon-lua",
                    "player": observation.get("player"),
                    "execution": observation.get("execution"),
                }
            problem = _real_player_execution_problem(observation)
            if problem:
                return {
                    "ok": False,
                    "reason": problem,
                    "mode": "modless-rcon-lua",
                    "player": observation.get("player"),
                    "execution": observation.get("execution"),
                }
            threat = _real_player_enemy_action_threat(observation, action)
            if threat:
                return {
                    "ok": False,
                    "reason": threat,
                    "mode": "modless-rcon-lua",
                    "player": observation.get("player"),
                    "execution": observation.get("execution"),
                }
            if action.get("type") == "move_to" and _gui_input_movement_enabled():
                return self._act_gui_move_to(action, observation)
        return self._modless.act(self._agent_action(action), player_name=self._configured_agent_player_name())

    def _maybe_restore_real_player_controller(self, observation: dict[str, Any]) -> tuple[dict[str, Any], str]:
        if not _real_player_controller_restore_needed(observation):
            return observation, ""
        restore = self._modless.act(
            {"type": "restore_character_controller"},
            player_name=self._configured_agent_player_name(),
        )
        if not restore.get("ok"):
            problem = _real_player_execution_problem(observation)
            return observation, f"{problem}; restore_character_controller failed: {restore.get('reason')}"
        refreshed = self.observe()
        if _real_player_controller_restore_needed(refreshed):
            problem = _real_player_execution_problem(refreshed)
            return refreshed, f"{problem}; restore_character_controller did not restore character controller"
        return refreshed, ""

    def _act_gui_move_to(self, action: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        target = action.get("position")
        if not isinstance(target, dict):
            return {"ok": False, "reason": "move_to requires numeric position", "mode": "gui-input"}
        current = player_position(observation)
        player = observation.get("player") if isinstance(observation.get("player"), dict) else {}
        if player.get("controller_is_character") is False:
            return {
                "ok": False,
                "reason": "GUI movement requires the player to be in character controller mode; close map/remote view first",
                "mode": "gui-input",
                "player": player,
            }
        target_position = {"x": float(target.get("x") or 0.0), "y": float(target.get("y") or 0.0)}
        remaining = distance(current, target_position)
        tolerance = float(action.get("tolerance") or 0.75)
        if remaining <= tolerance:
            return {
                "ok": True,
                "action": "move_to",
                "status": "arrived",
                "mode": "gui-input",
                "position": current,
                "target": target_position,
                "distance": round(remaining, 3),
            }
        keys = _movement_keys(current, target_position)
        if not keys:
            return {"ok": False, "reason": "move_to could not derive movement keys", "mode": "gui-input"}
        duration = float(action.get("duration_seconds") or min(0.9, max(0.12, remaining / 12.0)))
        try:
            from .vanilla_gui import VanillaGuiDriver

            driver = VanillaGuiDriver(self.cfg)
            if not driver.activate_factorio(timeout_seconds=2.0):
                return {"ok": False, "reason": "Factorio GUI window could not be activated for movement", "mode": "gui-input"}
            driver.click_window_ratio(0.5, 0.5)
            time.sleep(0.05)
            driver.hold_keys(keys, duration)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"GUI movement failed: {type(exc).__name__}: {exc}", "mode": "gui-input"}
        return {
            "ok": True,
            "action": "move_to",
            "status": "moving",
            "mode": "gui-input",
            "keys": keys,
            "duration_seconds": round(duration, 3),
            "position": current,
            "target": target_position,
            "distance": round(remaining, 3),
            "agent": {"name": player.get("name"), "kind": player.get("kind"), "character_valid": player.get("character_valid")},
            "execution": {"mode": "player", "input": "gui-keyboard", "virtual": False},
        }

    def wait(self, ticks: int) -> dict[str, Any]:
        response = self.act({"type": "wait", "ticks": ticks})
        time.sleep(max(0.05, ticks / 60.0))
        return response

    def _codex_wait_layout_cli_command(self) -> str:
        return "run-no-mod-codex-wait-layout-loop"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _real_player_execution_required() -> bool:
    return os.getenv("FACTORIO_AI_REQUIRE_REAL_PLAYER", "").strip().lower() in {"1", "true", "yes", "on"}


def _gui_input_movement_enabled() -> bool:
    return os.getenv("FACTORIO_AI_USE_GUI_INPUT_FOR_MOVEMENT", "").strip().lower() in {"1", "true", "yes", "on"}


def _movement_keys(current: dict[str, float], target: dict[str, float]) -> list[str]:
    dx = float(target.get("x") or 0.0) - float(current.get("x") or 0.0)
    dy = float(target.get("y") or 0.0) - float(current.get("y") or 0.0)
    keys: list[str] = []
    if dy < -0.2:
        keys.append("w")
    elif dy > 0.2:
        keys.append("s")
    if dx < -0.2:
        keys.append("a")
    elif dx > 0.2:
        keys.append("d")
    return keys


def _move_detour_action(current: dict[str, float], target: dict[str, float], attempt: int) -> dict[str, Any]:
    dx = float(target.get("x") or 0.0) - float(current.get("x") or 0.0)
    dy = float(target.get("y") or 0.0) - float(current.get("y") or 0.0)
    sign = 1.0 if attempt % 2 == 0 else -1.0
    offset = min(12.0, 4.0 + float(attempt // 2) * 2.0)
    position = {"x": float(current.get("x") or 0.0), "y": float(current.get("y") or 0.0)}
    if abs(dy) >= abs(dx):
        position["x"] += sign * offset
    else:
        position["y"] += sign * offset
    return {
        "type": "move_to",
        "position": position,
        "duration_seconds": 0.8,
        "tolerance": 1.5,
        "max_detour_attempts": 0,
    }


def _real_player_enemy_action_threat(observation: dict[str, Any], action: dict[str, Any]) -> str:
    if action.get("type") == "stop":
        return ""
    enemies = observation.get("enemies")
    if not isinstance(enemies, list):
        return ""
    enemy_positions: list[tuple[dict[str, Any], dict[str, float]]] = []
    for enemy in enemies:
        if not isinstance(enemy, dict):
            continue
        position = _position_or_none(enemy.get("position"))
        if position is not None:
            enemy_positions.append((enemy, position))
    if not enemy_positions:
        return ""

    current = player_position(observation)
    stop_radius = _float_env("FACTORIO_AI_REAL_PLAYER_ENEMY_STOP_RADIUS", 24.0)
    target_radius = _float_env("FACTORIO_AI_REAL_PLAYER_ENEMY_TARGET_RADIUS", 32.0)
    path_radius = _float_env("FACTORIO_AI_REAL_PLAYER_ENEMY_PATH_RADIUS", 20.0)

    nearest_current = min(
        ((distance(current, enemy_position), enemy) for enemy, enemy_position in enemy_positions),
        key=lambda item: item[0],
    )
    if nearest_current[0] <= stop_radius:
        return _enemy_threat_reason("near player", nearest_current[1], nearest_current[0], stop_radius)

    target = _action_target_position(action)
    if target is None:
        return ""
    nearest_target = min(
        ((distance(target, enemy_position), enemy) for enemy, enemy_position in enemy_positions),
        key=lambda item: item[0],
    )
    if nearest_target[0] <= target_radius:
        return _enemy_threat_reason("near action target", nearest_target[1], nearest_target[0], target_radius)

    if action.get("type") == "move_to":
        nearest_path = min(
            (
                (_distance_to_segment(enemy_position, current, target), enemy)
                for enemy, enemy_position in enemy_positions
            ),
            key=lambda item: item[0],
        )
        if nearest_path[0] <= path_radius:
            return _enemy_threat_reason("near movement path", nearest_path[1], nearest_path[0], path_radius)
    return ""


def _real_player_execution_problem(observation: dict[str, Any]) -> str:
    execution = observation.get("execution") if isinstance(observation.get("execution"), dict) else {}
    player = observation.get("player") if isinstance(observation.get("player"), dict) else {}
    mode = str(execution.get("mode") or "").strip().lower()
    kind = str(player.get("kind") or execution.get("agent_kind") or "").strip().lower()
    character_valid = bool(player.get("character_valid") or execution.get("character_valid"))
    if mode == "virtual" or kind == "server" or execution.get("virtual"):
        return (
            "real player execution required, but the no-mod adapter selected the virtual server agent; "
            "connect a GUI client as the configured AI player or disable FACTORIO_AI_REQUIRE_REAL_PLAYER for headless tests"
        )
    if not character_valid:
        name = str(player.get("name") or execution.get("agent_name") or "unknown")
        return f"real player execution required, but player {name} has no valid character"
    if player.get("controller_is_character") is False or execution.get("controller_is_character") is False:
        name = str(player.get("name") or execution.get("agent_name") or "unknown")
        return f"real player execution required, but player {name} is not in character controller mode"
    return ""


def _real_player_controller_restore_needed(observation: dict[str, Any]) -> bool:
    execution = observation.get("execution") if isinstance(observation.get("execution"), dict) else {}
    player = observation.get("player") if isinstance(observation.get("player"), dict) else {}
    mode = str(execution.get("mode") or "").strip().lower()
    kind = str(player.get("kind") or execution.get("agent_kind") or "").strip().lower()
    character_valid = bool(player.get("character_valid") or execution.get("character_valid"))
    if mode == "virtual" or kind == "server" or execution.get("virtual") or not character_valid:
        return False
    return player.get("controller_is_character") is False or execution.get("controller_is_character") is False


def _action_target_position(action: dict[str, Any]) -> dict[str, float] | None:
    for key in ("position", "near"):
        position = _position_or_none(action.get(key))
        if position is not None:
            return position
    return None


def _position_or_none(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        return {"x": float(value["x"]), "y": float(value["y"])}
    except (KeyError, TypeError, ValueError):
        return None


def _distance_to_segment(point: dict[str, float], start: dict[str, float], end: dict[str, float]) -> float:
    sx = float(start["x"])
    sy = float(start["y"])
    ex = float(end["x"])
    ey = float(end["y"])
    px = float(point["x"])
    py = float(point["y"])
    dx = ex - sx
    dy = ey - sy
    if dx == 0 and dy == 0:
        return distance(point, start)
    t = ((px - sx) * dx + (py - sy) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    closest = {"x": sx + t * dx, "y": sy + t * dy}
    return distance(point, closest)


def _enemy_threat_reason(scope: str, enemy: dict[str, Any], threat_distance: float, radius: float) -> str:
    name = str(enemy.get("name") or enemy.get("type") or "enemy")
    position = enemy.get("position") if isinstance(enemy.get("position"), dict) else {}
    return (
        "real player execution paused: "
        f"enemy {name} is {threat_distance:.1f} tiles {scope} "
        f"(limit {radius:.1f}) at {float(position.get('x') or 0.0):.1f},{float(position.get('y') or 0.0):.1f}"
    )


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


def _remote_strategy_timeout_seconds() -> int:
    return max(5, int(_float_env("FACTORIO_AI_REMOTE_STRATEGY_TIMEOUT_SECONDS", 90.0)))


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _slugify_reason(value: str) -> str:
    output = []
    for char in value.lower():
        if char.isalnum():
            output.append(char)
        elif output and output[-1] != "_":
            output.append("_")
    text = "".join(output).strip("_")
    return text[:80] or "idle"


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _strategy_target_item(strategy: dict[str, Any]) -> str | None:
    item = str(strategy.get("target_item") or strategy.get("item") or "").strip()
    if item in {
        "transport-belt",
        "inserter",
        "long-handed-inserter",
        "burner-inserter",
        "firearm-magazine",
        "gun-turret",
        "burner-mining-drill",
        "electric-mining-drill",
        "stone-furnace",
        "small-electric-pole",
        "assembling-machine-1",
    }:
        return item
    return None


def _strategy_site_input_item(strategy: dict[str, Any]) -> str | None:
    item = str(strategy.get("input_item") or strategy.get("item") or "").strip()
    if item in {
        "iron-plate",
        "copper-plate",
        "iron-gear-wheel",
        "copper-cable",
        "electronic-circuit",
        "automation-science-pack",
        "logistic-science-pack",
    }:
        return item
    return None


def _max_steps(value: int | None, default: int) -> int:
    if value is None:
        return default
    return max(0, int(value))


def _stale_take_response(action: dict[str, Any], response: dict[str, Any]) -> bool:
    return (
        action.get("type") == "take"
        and str(response.get("reason") or "") == "target does not have item"
    )


def _virtual_move_response_arrived(response: dict[str, Any]) -> bool:
    execution = response.get("execution") if isinstance(response.get("execution"), dict) else {}
    return (
        str(response.get("action") or "") == "move_to"
        and str(response.get("status") or "") == "arrived"
        and (execution.get("virtual") is True or str(execution.get("mode") or "").lower() == "virtual")
    )


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return int(exit_code.value) == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, SystemError):
        return False
    return True
