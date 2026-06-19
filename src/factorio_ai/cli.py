from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from .config import load_config
from .controller import FactorioController, ModlessFactorioController
from .factorio import (
    create_no_mod_save,
    create_save,
    install_mod,
    save_no_mod_server,
    start_gui_client,
    start_no_mod_gui_client,
    start_no_mod_server,
    start_save_gui,
    start_server,
    wait_for_rcon,
)
from .modless_lua import ModlessLuaController
from . import remote_slurm
from .layout_validation import validate_layout_candidate
from .targets import load_targets
from .strategy import skill_catalog_payload
from .vanilla_gui import (
    VanillaGuiDriver,
    launch_vanilla_gui,
    prepare_steam_vanilla_mod_list,
    prepare_vanilla_mod_directory,
    restore_latest_steam_mod_list,
)
from .vanilla_perception import classify_bmp_file
from .web_dashboard import FACTORIO_ROUTE, public_dashboard_urls, serve_dashboard
from .token_usage import record_current_codex_thread_usage, record_token_usage, token_usage_summary
from .trace_archive import archive_training_traces, trace_archive_summary
from .run_journal import run_journal_summary


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _observer_player_control_problem(cfg: Any) -> str:
    player_name = str(getattr(cfg, "agent_player_name", "") or "").strip().lower()
    if player_name != "auto":
        return ""
    if _truthy_env("FACTORIO_AI_ALLOW_OBSERVER_CONTROL"):
        return ""
    if not (_truthy_env("FACTORIO_AI_REQUIRE_REAL_PLAYER") or _truthy_env("FACTORIO_AI_USE_GUI_INPUT_FOR_MOVEMENT")):
        return ""
    return (
        "refusing to control the auto-selected connected player. "
        "Use the default AI/server agent for autonomous runs, or set "
        "FACTORIO_AI_ALLOW_OBSERVER_CONTROL=1 only for an explicit manual test."
    )


def _guard_observer_player_control(cfg: Any, command: str) -> None:
    problem = _observer_player_control_problem(cfg)
    if not problem:
        return
    print_json({"ok": False, "command": command, "reason": problem})
    raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Factorio AI autoplayer")
    parser.add_argument("--config", help="Path to config.json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("install-mod", help="Install the Factorio AI mod into the runtime mod directory")

    create_save_parser = subparsers.add_parser("create-save", help="Create the local MVP save")
    create_save_parser.add_argument("--overwrite", action="store_true")

    start_server_parser = subparsers.add_parser("start-server", help="Start local Factorio server and wait")
    start_server_parser.add_argument("--no-wait-rcon", action="store_true")

    create_no_mod_save_parser = subparsers.add_parser(
        "create-no-mod-save",
        help="Create a vanilla-compatible save without the custom AI mod",
    )
    create_no_mod_save_parser.add_argument("--overwrite", action="store_true")

    start_no_mod_server_parser = subparsers.add_parser(
        "start-no-mod-server",
        help="Start a vanilla-compatible local/LAN server with RCON Lua enabled but no custom mod",
    )
    start_no_mod_server_parser.add_argument("--no-wait-rcon", action="store_true")

    subparsers.add_parser(
        "no-mod-server-save",
        help="Persist the running no-mod server state to its save file via RCON /server-save",
    )

    no_mod_gui_parser = subparsers.add_parser(
        "launch-no-mod-gui",
        help="Launch a no-custom-mod GUI client connected to the no-mod RCON server",
    )
    no_mod_gui_parser.add_argument("--window-size", default="1600x900")
    no_mod_gui_parser.add_argument("--no-connect", action="store_true")
    no_mod_gui_parser.add_argument("--confirm-timeout", type=float, default=20.0)

    launch_gui_parser = subparsers.add_parser("launch-gui", help="Launch a GUI Factorio client and connect to the local server")
    launch_gui_parser.add_argument("--window-size", default="1600x900")
    launch_gui_parser.add_argument("--no-connect", action="store_true")

    review_gui_parser = subparsers.add_parser(
        "review-gui",
        help="Open a development mod/RCON GUI client for manual inspection",
    )
    review_gui_parser.add_argument("--window-size", default="1600x900")
    review_gui_parser.add_argument("--confirm-timeout", type=float, default=20.0)
    review_gui_parser.add_argument("--no-wait", action="store_true", help="Do not wait for the GUI process to close")

    watch_gui_parser = subparsers.add_parser(
        "watch-gui",
        help="Open a development-only mod/RCON GUI client while the AI keeps running",
    )
    watch_gui_parser.add_argument("--window-size", default="1600x900")
    watch_gui_parser.add_argument("--confirm-timeout", type=float, default=20.0)
    watch_gui_parser.add_argument("--no-wait", action="store_true", help="Do not wait for the GUI process to close")

    launch_save_gui_parser = subparsers.add_parser("launch-save-gui", help="Launch GUI Factorio and load the configured save")
    launch_save_gui_parser.add_argument("--window-size", default="1600x900")

    vanilla_gui_parser = subparsers.add_parser(
        "launch-vanilla-gui",
        help="Launch normal Steam Factorio for achievement-compatible GUI automation",
    )
    vanilla_gui_parser.add_argument("--direct", action="store_true", help="Launch factorio.exe directly instead of Steam")
    vanilla_gui_parser.add_argument("--window-size", help="Optional direct-launch window size, e.g. 1600x900")
    vanilla_gui_parser.add_argument("--wait-timeout", type=float, default=45.0, help="Seconds to wait for the real game window")

    vanilla_window_parser = subparsers.add_parser("vanilla-window", help="Report the detected vanilla Factorio window")
    vanilla_window_parser.add_argument("--activate", action="store_true", help="Bring the Factorio window to foreground")

    vanilla_screenshot_parser = subparsers.add_parser(
        "vanilla-screenshot",
        help="Capture the detected vanilla Factorio window without mods or RCON",
    )
    vanilla_screenshot_parser.add_argument("--output", help="Output BMP path")
    vanilla_screenshot_parser.add_argument("--method", choices=["auto", "screen", "window"], default="auto")

    vanilla_state_parser = subparsers.add_parser(
        "vanilla-screen-state",
        help="Capture and classify the current vanilla Factorio screen state",
    )
    vanilla_state_parser.add_argument("--output", help="Output BMP path")
    vanilla_state_parser.add_argument("--method", choices=["auto", "screen", "window"], default="auto")
    vanilla_state_parser.add_argument("--no-capture", action="store_true", help="Classify an existing --output BMP")

    vanilla_key_parser = subparsers.add_parser(
        "vanilla-key",
        help="Send one vanilla-safe keyboard input to the detected Factorio window",
    )
    vanilla_key_parser.add_argument("key", help="Key name, e.g. escape, tab, w, a, s, d, shift")
    vanilla_key_parser.add_argument("--duration", type=float, default=0.05)
    vanilla_key_parser.add_argument(
        "--background",
        action="store_true",
        help="Use experimental background PostMessage instead of foreground SendInput",
    )

    vanilla_start_parser = subparsers.add_parser(
        "vanilla-start-freeplay",
        help="From the vanilla main menu, click through Single Player -> New Game -> Freeplay (Space Age)",
    )
    vanilla_start_parser.add_argument("--no-skip-intro", action="store_true", help="Do not press Tab after the map loads")

    vanilla_probe_parser = subparsers.add_parser(
        "vanilla-probe",
        help="Probe vanilla screen capture and background/minimized input capabilities",
    )
    vanilla_probe_parser.add_argument("--output-dir", help="Directory for probe screenshots")
    vanilla_probe_parser.add_argument("--minimize-check", action="store_true", help="Temporarily minimize Factorio and try PrintWindow capture")
    vanilla_probe_parser.add_argument("--background-key", help="Post one key to the Factorio window without foreground activation")

    subparsers.add_parser(
        "prepare-steam-vanilla-mod-list",
        help="Back up the user's Factorio mod-list and enable only official Space Age mods for Steam vanilla launch",
    )
    subparsers.add_parser("restore-steam-mod-list", help="Restore the latest backed-up Steam Factorio mod-list")

    confirm_parser = subparsers.add_parser("confirm-steam-launch", help="Click Steam's custom-arguments continue prompt")
    confirm_parser.add_argument("--timeout", type=float, default=15.0)

    subparsers.add_parser("observe", help="Print /ai_observe JSON")

    no_mod_observe_parser = subparsers.add_parser(
        "no-mod-observe",
        help="Print observation JSON through vanilla /silent-command Lua over RCON",
    )
    no_mod_observe_parser.add_argument("--player", help="Preferred player name to observe")
    no_mod_observe_parser.add_argument(
        "--full-planning-sites",
        action="store_true",
        help="Include expensive water/lab/automation placement candidate scans",
    )

    no_mod_action_parser = subparsers.add_parser(
        "no-mod-action",
        help="Execute an allowlisted no-mod RCON/Lua action JSON",
    )
    no_mod_action_parser.add_argument("json_action")
    no_mod_action_parser.add_argument("--player", help="Preferred player name for player-scoped actions")

    apply_design_parser = subparsers.add_parser(
        "apply-cell-design",
        help="Build a stored cell-library design in the live game at an anchor (dry-run unless --execute)",
    )
    apply_design_parser.add_argument("--key", required=True, help="Library design key (see /factorio/layouts)")
    apply_design_parser.add_argument("--x", type=float, default=0.0, help="Anchor x (cell origin) on nauvis")
    apply_design_parser.add_argument("--y", type=float, default=0.0, help="Anchor y (cell origin) on nauvis")
    apply_design_parser.add_argument(
        "--execute", action="store_true",
        help="Actually place the entities (default: dry-run, print the plan only)",
    )
    apply_design_parser.add_argument("--player", help="Preferred player name for the build actions")

    strategy_parser = subparsers.add_parser("strategy", help="Ask the strategic LLM layer for the next high-level skill")
    strategy_parser.add_argument("--objective", default="launch_rocket_program")
    strategy_parser.add_argument("--require-llm", action="store_true")

    no_mod_strategy_parser = subparsers.add_parser(
        "no-mod-strategy",
        help="Ask the strategic layer using no-custom-mod RCON/Lua observation",
    )
    no_mod_strategy_parser.add_argument("--objective", default="launch_rocket_program")
    no_mod_strategy_parser.add_argument("--require-llm", action="store_true")

    strategy_step_parser = subparsers.add_parser(
        "run-strategy-step",
        help="Ask the strategic layer for one high-level skill and execute it if implemented",
    )
    strategy_step_parser.add_argument("--objective", default="launch_rocket_program")
    strategy_step_parser.add_argument("--require-llm", action="store_true")
    strategy_step_parser.add_argument("--target", type=int, help="Override the selected skill item target count")
    strategy_step_parser.add_argument("--max-steps", type=int, help="Override the selected skill max step count")

    no_mod_strategy_step_parser = subparsers.add_parser(
        "run-no-mod-strategy-step",
        help="Ask the strategic layer for one high-level skill and execute it through no-custom-mod RCON/Lua",
    )
    no_mod_strategy_step_parser.add_argument("--objective", default="launch_rocket_program")
    no_mod_strategy_step_parser.add_argument("--require-llm", action="store_true")
    no_mod_strategy_step_parser.add_argument("--target", type=int, help="Override the selected skill item target count")
    no_mod_strategy_step_parser.add_argument("--max-steps", type=int, help="Override the selected skill max step count")

    autopilot_parser = subparsers.add_parser(
        "run-autopilot",
        help="Continuously ask the strategic layer for skills and execute them",
    )
    autopilot_parser.add_argument("--objective", default="launch_rocket_program")
    autopilot_parser.add_argument("--require-llm", action="store_true")
    autopilot_parser.add_argument("--target", type=int, help="Override the selected skill item target count")
    autopilot_parser.add_argument("--max-steps", type=int, help="Override each selected skill max step count")
    autopilot_parser.add_argument("--cycles", type=int, default=0, help="Number of strategy cycles; 0 means run until interrupted")
    autopilot_parser.add_argument("--sleep-seconds", type=float, default=5.0)
    autopilot_parser.add_argument("--stop-on-error", action="store_true")

    no_mod_autopilot_parser = subparsers.add_parser(
        "run-no-mod-autopilot",
        help="Continuously run the strategic layer through no-custom-mod RCON/Lua",
    )
    no_mod_autopilot_parser.add_argument("--objective", default="launch_rocket_program")
    no_mod_autopilot_parser.add_argument("--require-llm", action="store_true")
    no_mod_autopilot_parser.add_argument("--target", type=int, help="Override the selected skill item target count")
    no_mod_autopilot_parser.add_argument("--max-steps", type=int, help="Override each selected skill max step count")
    no_mod_autopilot_parser.add_argument("--cycles", type=int, default=0, help="Number of strategy cycles; 0 means run until interrupted")
    no_mod_autopilot_parser.add_argument("--sleep-seconds", type=float, default=5.0)
    no_mod_autopilot_parser.add_argument("--stop-on-error", action="store_true")

    code_agent_parser = subparsers.add_parser(
        "run-no-mod-code-agent",
        help="FLE-style code-gen loop: the LLM writes a Python program each step (driven via a high-level API) to control the factory",
    )
    code_agent_parser.add_argument("--objective", default="launch_rocket_program")
    code_agent_parser.add_argument("--cycles", type=int, default=0, help="Number of program steps; 0 means run until interrupted")
    code_agent_parser.add_argument("--sleep-seconds", type=float, default=3.0)
    code_agent_parser.add_argument("--program-timeout", type=float, default=60.0, help="Per-program sandbox wall-clock limit (seconds)")
    code_agent_parser.add_argument("--max-tokens", type=int, default=2048, help="LLM max tokens for each generated program")

    codex_wait_layout_parser = subparsers.add_parser(
        "run-codex-wait-layout-loop",
        help="Keep submitting simulation-only layout improvement work while Codex is implementing a missing skill",
    )
    codex_wait_layout_parser.add_argument("--objective", default="launch_rocket_program")
    codex_wait_layout_parser.add_argument("--cycles", type=int, default=0, help="Number of pulses; 0 means run until the wait state clears")
    codex_wait_layout_parser.add_argument("--sleep-seconds", type=float, default=20.0)

    no_mod_codex_wait_layout_parser = subparsers.add_parser(
        "run-no-mod-codex-wait-layout-loop",
        help="No-custom-mod variant of the Codex wait layout loop",
    )
    no_mod_codex_wait_layout_parser.add_argument("--objective", default="launch_rocket_program")
    no_mod_codex_wait_layout_parser.add_argument("--cycles", type=int, default=0, help="Number of pulses; 0 means run until the wait state clears")
    no_mod_codex_wait_layout_parser.add_argument("--sleep-seconds", type=float, default=20.0)

    idle_layout_parser = subparsers.add_parser(
        "run-idle-layout-loop",
        help="Keep GPUs busy with simulation-only layout improvement and confirmed skill learning whenever autopilot is idle or stale",
    )
    idle_layout_parser.add_argument("--objective", default="launch_rocket_program")
    idle_layout_parser.add_argument("--cycles", type=int, default=0, help="Number of pulses; 0 means run until interrupted")
    idle_layout_parser.add_argument("--sleep-seconds", type=float, default=5.0)
    idle_layout_parser.add_argument("--stale-seconds", type=float, default=15.0)
    idle_layout_parser.add_argument("--min-submit-interval-seconds", type=float, default=0.0)

    no_mod_idle_layout_parser = subparsers.add_parser(
        "run-no-mod-idle-layout-loop",
        help="No-custom-mod idle GPU filler layout and confirmed skill-learning loop",
    )
    no_mod_idle_layout_parser.add_argument("--objective", default="launch_rocket_program")
    no_mod_idle_layout_parser.add_argument("--cycles", type=int, default=0, help="Number of pulses; 0 means run until interrupted")
    no_mod_idle_layout_parser.add_argument("--sleep-seconds", type=float, default=5.0)
    no_mod_idle_layout_parser.add_argument("--stale-seconds", type=float, default=15.0)
    no_mod_idle_layout_parser.add_argument("--min-submit-interval-seconds", type=float, default=0.0)

    for _foundry_name in ("run-skill-foundry-loop", "run-no-mod-skill-foundry-loop"):
        _foundry_parser = subparsers.add_parser(
            _foundry_name,
            help="Continuously let the local LLM author + validate new skill executors for un-built skills (idle GPU)",
        )
        _foundry_parser.add_argument("--objective", default="launch_rocket_program")
        _foundry_parser.add_argument("--cycles", type=int, default=0, help="Number of cycles; 0 means run until interrupted")
        _foundry_parser.add_argument("--sleep-seconds", type=float, default=30.0)
        _foundry_parser.add_argument("--max-attempts", type=int, default=None, help="Generation attempts per skill per cycle")
        _foundry_parser.add_argument("--require-idle", action="store_true", help="Only generate while autopilot is idle/sleeping")
        _foundry_parser.add_argument("--throttle-seconds", type=float, default=0.0, help="Minimum seconds between generation attempts")

    for _health_name in ("run-health", "no-mod-run-health"):
        _health_parser = subparsers.add_parser(
            _health_name,
            help="Print a one-shot health digest of the unattended run (heartbeats, generated skills, decisions)",
        )
        _health_parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of the text digest")
        _health_parser.add_argument("--no-observe", action="store_true", help="Skip the live game observation (faster, no RCON)")

    subparsers.add_parser(
        "slurm-cancel-vllm-services",
        help="Cancel all active Factorio vLLM service tasks on the scheduler (forces a fresh service next start)",
    )

    begin_codex_parser = subparsers.add_parser(
        "begin-codex-work",
        help="Mark a missing executor as being implemented by Codex and keep LLM layout work running",
    )
    begin_codex_parser.add_argument("--objective", default="launch_rocket_program")
    begin_codex_parser.add_argument("--selected-skill", required=True)
    begin_codex_parser.add_argument(
        "--reason",
        default="Codex is implementing a missing deterministic executor.",
    )
    begin_codex_parser.add_argument("--no-mod", action="store_true", help="Use the no-custom-mod controller for loop autostart")

    finish_codex_parser = subparsers.add_parser(
        "finish-codex-work",
        help="Clear the Codex wait state so the background layout loop can stop",
    )
    finish_codex_parser.add_argument("--selected-skill", required=True)
    finish_codex_parser.add_argument("--reason", default="Codex implementation completed")
    finish_codex_parser.add_argument("--no-mod", action="store_true", help="Use the no-custom-mod controller")

    web_parser = subparsers.add_parser("web", help="Serve the Factorio production monitor at /factorio")
    web_parser.add_argument("--host", default="0.0.0.0")
    web_parser.add_argument("--port", type=int, default=18889)
    web_parser.add_argument("--objective", default="launch_rocket_program")

    validate_layout_parser = subparsers.add_parser(
        "validate-layout-candidate",
        help="Place a simulation candidate in a disposable sandbox surface and record validation feedback",
    )
    validate_layout_parser.add_argument("--candidate-id", required=True)
    validate_layout_parser.add_argument("--variant", choices=["before", "after"], default="after")
    validate_layout_parser.add_argument("--objective", default="launch_rocket_program")
    validate_layout_parser.add_argument("--ticks", type=int, default=3600)
    validate_layout_parser.add_argument("--player", help="Preferred no-mod player name for observation")
    validate_layout_parser.add_argument("--no-cleanup", action="store_true", help="Leave the sandbox surface in the save")

    design_cells_parser = subparsers.add_parser(
        "design-cells",
        help="Observe the live game and deterministically design + store optimal cell layouts for the top production deficits",
    )
    design_cells_parser.add_argument("--objective", default="launch_rocket_program")
    design_cells_parser.add_argument("--top", type=int, default=3, help="How many top-deficit items to design")
    design_cells_parser.add_argument("--player", help="Preferred no-mod player name for observation")

    token_parser = subparsers.add_parser("record-token-usage", help="Append a Codex token usage sample")
    token_parser.add_argument("--tokens-used", type=int, required=True)
    token_parser.add_argument("--label", default="")
    token_parser.add_argument("--source", default="codex")
    token_parser.add_argument("--timestamp")

    current_token_parser = subparsers.add_parser(
        "record-current-codex-thread-usage",
        help="Append the current Factorio Codex thread tokens_used sample from the local Codex state DB",
    )
    current_token_parser.add_argument("--label", default="")
    current_token_parser.add_argument("--source", default="codex_thread")
    current_token_parser.add_argument("--timestamp")
    current_token_parser.add_argument("--thread-id", help="Codex thread id to record; overrides cwd selection")
    current_token_parser.add_argument(
        "--state-db",
        help=r"Codex state sqlite path; defaults to C:\Users\NEC\.codex\state_5.sqlite",
    )
    current_token_parser.add_argument(
        "--cwd",
        help=r"Thread cwd to match when --thread-id is omitted; defaults to the current working directory",
    )

    subparsers.add_parser("token-usage-summary", help="Print the recorded Codex token usage summary")
    subparsers.add_parser("run-journal-summary", help="Print goal, loop note, and insight journal summary")
    archive_parser = subparsers.add_parser(
        "archive-training-traces",
        help="Archive local logs/notes/insights into a fine-tuning trace bundle",
    )
    archive_parser.add_argument("--label", default="")
    archive_parser.add_argument("--output-root", help="Archive root directory; defaults to runtime/trace_archives")
    archive_parser.add_argument("--no-copy-raw", action="store_true", help="Write manifest/index only without raw copies")
    archive_parser.add_argument("--no-text-logs", action="store_true", help="Skip .log/.err/.out runtime diagnostics")
    archive_parser.add_argument("--limit", type=int, help="Limit source file count for testing or dry runs")
    summary_parser = subparsers.add_parser("trace-archive-summary", help="Print local training trace archive summary")
    summary_parser.add_argument("--output-root", help="Archive root directory; defaults to runtime/trace_archives")
    summary_parser.add_argument("--limit", type=int, default=5)

    action_parser = subparsers.add_parser("action", help="Execute /ai_action JSON")
    action_parser.add_argument("json_action")

    run_parser = subparsers.add_parser("run-iron-mvp", help="Run the iron plate MVP loop")
    run_parser.add_argument("--target", type=int, default=10)
    run_parser.add_argument("--max-steps", type=int, default=200)

    no_mod_iron_parser = subparsers.add_parser(
        "run-no-mod-iron-mvp",
        help="Run the iron plate MVP loop through the no-custom-mod RCON/Lua adapter",
    )
    no_mod_iron_parser.add_argument("--target", type=int, default=10)
    no_mod_iron_parser.add_argument("--max-steps", type=int, default=200)

    copper_parser = subparsers.add_parser("run-copper-mvp", help="Run the copper plate MVP loop")
    copper_parser.add_argument("--target", type=int, default=10)
    copper_parser.add_argument("--max-steps", type=int, default=250)

    no_mod_copper_parser = subparsers.add_parser(
        "run-no-mod-copper-mvp",
        help="Run the copper plate MVP loop through the no-custom-mod RCON/Lua adapter",
    )
    no_mod_copper_parser.add_argument("--target", type=int, default=10)
    no_mod_copper_parser.add_argument("--max-steps", type=int, default=250)

    circuit_parser = subparsers.add_parser("run-circuit-mvp", help="Run the electronic circuit MVP loop")
    circuit_parser.add_argument("--target", type=int, default=5)
    circuit_parser.add_argument("--max-steps", type=int, default=500)

    no_mod_circuit_parser = subparsers.add_parser(
        "run-no-mod-circuit-mvp",
        help="Run the electronic circuit MVP loop through the no-custom-mod RCON/Lua adapter",
    )
    no_mod_circuit_parser.add_argument("--target", type=int, default=5)
    no_mod_circuit_parser.add_argument("--max-steps", type=int, default=500)

    science_parser = subparsers.add_parser("run-science-mvp", help="Run the automation science MVP loop")
    science_parser.add_argument("--target", type=int, default=5)
    science_parser.add_argument("--max-steps", type=int, default=400)

    no_mod_science_parser = subparsers.add_parser(
        "run-no-mod-science-mvp",
        help="Run the automation science MVP loop through the no-custom-mod RCON/Lua adapter",
    )
    no_mod_science_parser.add_argument("--target", type=int, default=5)
    no_mod_science_parser.add_argument("--max-steps", type=int, default=500)

    belt_smelting_parser = subparsers.add_parser(
        "run-belt-smelting-mvp",
        help="Build and run a minimal belt-fed iron smelting line",
    )
    belt_smelting_parser.add_argument("--target", type=int, default=10)
    belt_smelting_parser.add_argument("--max-steps", type=int, default=700)

    expand_iron_parser = subparsers.add_parser(
        "run-expand-iron-smelting-mvp",
        help="Add belt-fed iron smelting capacity",
    )
    expand_iron_parser.add_argument("--target-rate", type=int, default=90)
    expand_iron_parser.add_argument("--max-steps", type=int, default=2000)

    expand_copper_parser = subparsers.add_parser(
        "run-expand-copper-smelting-mvp",
        help="Add belt-fed copper smelting capacity",
    )
    expand_copper_parser.add_argument("--target-rate", type=int, default=75)
    expand_copper_parser.add_argument("--max-steps", type=int, default=1600)

    power_parser = subparsers.add_parser("run-power-mvp", help="Build the first steam power block")
    power_parser.add_argument("--max-steps", type=int, default=900)

    no_mod_power_parser = subparsers.add_parser(
        "run-no-mod-power-mvp",
        help="Build the first steam power block through the no-custom-mod RCON/Lua adapter",
    )
    no_mod_power_parser.add_argument("--max-steps", type=int, default=900)

    automation_research_parser = subparsers.add_parser(
        "run-automation-research-mvp",
        help="Build and feed the first lab to research Automation",
    )
    automation_research_parser.add_argument("--max-steps", type=int, default=1500)

    no_mod_automation_research_parser = subparsers.add_parser(
        "run-no-mod-automation-research-mvp",
        help="Build and feed the first lab to research Automation through the no-custom-mod RCON/Lua adapter",
    )
    no_mod_automation_research_parser.add_argument("--max-steps", type=int, default=1500)

    circuit_automation_parser = subparsers.add_parser(
        "run-circuit-automation-mvp",
        help="Build a powered assembler cell for electronic circuits",
    )
    circuit_automation_parser.add_argument("--target", type=int, default=5)
    circuit_automation_parser.add_argument("--max-steps", type=int, default=1800)

    no_mod_circuit_automation_parser = subparsers.add_parser(
        "run-no-mod-circuit-automation-mvp",
        help="Build a powered assembler cell for electronic circuits through the no-custom-mod RCON/Lua adapter",
    )
    no_mod_circuit_automation_parser.add_argument("--target", type=int, default=5)
    no_mod_circuit_automation_parser.add_argument("--max-steps", type=int, default=1800)

    logistics_research_parser = subparsers.add_parser(
        "run-logistics-research-mvp",
        help="Research Logistics with the first powered lab",
    )
    logistics_research_parser.add_argument("--max-steps", type=int, default=2200)

    no_mod_logistics_research_parser = subparsers.add_parser(
        "run-no-mod-logistics-research-mvp",
        help="Research Logistics with the first powered lab through the no-custom-mod RCON/Lua adapter",
    )
    no_mod_logistics_research_parser.add_argument("--max-steps", type=int, default=2200)

    build_item_mall_parser = subparsers.add_parser(
        "run-build-item-mall-mvp",
        help="Build a powered assembler cell for recurring factory-expansion items",
    )
    build_item_mall_parser.add_argument("--item", default="transport-belt")
    build_item_mall_parser.add_argument("--target", type=int, default=20)
    build_item_mall_parser.add_argument("--max-steps", type=int, default=1200)

    no_mod_build_item_mall_parser = subparsers.add_parser(
        "run-no-mod-build-item-mall-mvp",
        help="Build a powered assembler cell through the no-custom-mod RCON/Lua adapter",
    )
    no_mod_build_item_mall_parser.add_argument("--item", default="transport-belt")
    no_mod_build_item_mall_parser.add_argument("--target", type=int, default=20)
    no_mod_build_item_mall_parser.add_argument("--max-steps", type=int, default=1200)

    subparsers.add_parser("slurm-deploy", help="Deploy project source to the Slurm remote directory")
    subparsers.add_parser("slurm-start-worker", help="Submit the persistent Slurm worker job")
    slurm_ensure_parser = subparsers.add_parser(
        "slurm-ensure-worker",
        help="Ensure a Slurm worker is running or queued before the current allocation expires",
    )
    slurm_ensure_parser.add_argument(
        "--renew-before-minutes",
        type=int,
        default=None,
        help="Queue a dependent successor when the running worker has less than this many minutes left",
    )
    subparsers.add_parser("slurm-status", help="Print Slurm worker status")
    subparsers.add_parser("slurm-llm-status", help="Print Slurm worker LLM readiness")
    subparsers.add_parser("slurm-vllm-service-status", help="Print scheduler persistent vLLM service readiness")
    vllm_service_parser = subparsers.add_parser(
        "slurm-ensure-vllm-service",
        help="Ensure a scheduler task keeps the configured vLLM model loaded",
    )
    vllm_service_parser.add_argument("--duration-seconds", type=int, default=None)
    subparsers.add_parser("slurm-cancel", help="Cancel the Slurm worker job")
    subparsers.add_parser("slurm-submit-test", help="Submit a planner test task to the Slurm worker")
    model_benchmark_parser = subparsers.add_parser(
        "slurm-submit-model-benchmark",
        help="Compare multiple OpenAI-compatible LLM model names on the current strategy payload",
    )
    model_benchmark_parser.add_argument("--objective", default="launch_rocket_program")
    model_benchmark_parser.add_argument(
        "--models",
        default=os.getenv("FACTORIO_AI_LLM_BENCHMARK_MODELS")
        or "Qwen/Qwen3.5-27B,Qwen/Qwen3.5-9B,Qwen/Qwen3.5-4B",
        help="Comma-separated model names visible to the remote LLM server",
    )
    model_benchmark_parser.add_argument("--timeout", type=int, default=300)
    model_benchmark_parser.add_argument(
        "--attached",
        action="store_true",
        help="Run the benchmark by attaching to an existing running Slurm job with srun instead of the queue worker",
    )
    model_benchmark_parser.add_argument(
        "--no-deploy",
        action="store_true",
        help="With --attached, skip deploying the current source before running the one-shot task",
    )
    worker_compare_parser = subparsers.add_parser(
        "slurm-compare-strategy-workers",
        help="Compare the same current strategy payload across configured Slurm LLM workers",
    )
    worker_compare_parser.add_argument("--objective", default="launch_rocket_program")
    worker_compare_parser.add_argument(
        "--workers",
        default=os.getenv("FACTORIO_AI_STRATEGY_WORKERS"),
        help=(
            "Comma-separated label=remote_dir@job_name specs. "
            "Defaults to 4b, 9b, and 27b Factorio workers."
        ),
    )
    worker_compare_parser.add_argument("--timeout", type=int, default=90)
    worker_compare_parser.add_argument(
        "--no-log",
        action="store_true",
        help="Print the comparison without appending logs/strategy-worker-comparison.jsonl",
    )

    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    if args.command == "install-mod":
        path = install_mod(cfg)
        print_json({"ok": True, "modPath": str(path)})
        return

    if args.command == "create-save":
        path = create_save(cfg, overwrite=args.overwrite)
        print_json({"ok": True, "savePath": str(path)})
        return

    if args.command == "start-server":
        proc = start_server(cfg)
        print_json({"ok": True, "pid": proc.pid})
        if not args.no_wait_rcon:
            wait_for_rcon(cfg)
            print_json({"ok": True, "rconReady": True})
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            raise
        return

    if args.command == "create-no-mod-save":
        path = create_no_mod_save(cfg, overwrite=args.overwrite)
        print_json({"ok": True, "savePath": str(path), "mode": "no-mod-rcon-lua"})
        return

    if args.command == "no-mod-server-save":
        result = save_no_mod_server(cfg)
        print_json({"ok": True, "result": str(result).strip(), "mode": "no-mod-rcon-lua"})
        return

    if args.command == "slurm-cancel-vllm-services":
        print_json(remote_slurm.cancel_vllm_services())
        return

    if args.command in {"run-health", "no-mod-run-health"}:
        from .run_health import format_run_health, gather_run_health

        summary = gather_run_health(cfg, observe=not args.no_observe)
        if args.json:
            print_json(summary)
        else:
            print(format_run_health(summary))
        return

    if args.command == "start-no-mod-server":
        proc = start_no_mod_server(cfg)
        print_json({"ok": True, "pid": proc.pid, "mode": "no-mod-rcon-lua"})
        if not args.no_wait_rcon:
            wait_for_rcon(cfg)
            print_json({"ok": True, "rconReady": True, "mode": "no-mod-rcon-lua"})
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            raise
        return

    if args.command == "launch-no-mod-gui":
        proc = start_no_mod_gui_client(cfg, window_size=args.window_size, connect=not args.no_connect)
        clicked = False
        try:
            clicked = VanillaGuiDriver(cfg).click_steam_continue_prompt(timeout_seconds=args.confirm_timeout)
        except Exception:
            clicked = False
        print_json({"ok": True, "pid": proc.pid, "mode": "no-mod-rcon-lua", "steamPromptClicked": clicked})
        return

    if args.command == "launch-gui":
        proc = start_gui_client(cfg, window_size=args.window_size, connect=not args.no_connect)
        print_json({"ok": True, "pid": proc.pid})
        return

    if args.command == "review-gui":
        server_started = False
        try:
            wait_for_rcon(cfg, timeout_seconds=3)
        except Exception:
            create_save(cfg)
            server_proc = start_server(cfg)
            server_started = True
            wait_for_rcon(cfg)
            print_json({"ok": True, "serverStarted": True, "serverPid": server_proc.pid})

        stop_response = FactorioController(cfg).stop_agent()
        proc = start_gui_client(cfg, window_size=args.window_size, connect=True, ensure_mod=False)
        clicked = False
        try:
            clicked = VanillaGuiDriver(cfg).click_steam_continue_prompt(timeout_seconds=args.confirm_timeout)
        except Exception:
            clicked = False
        print_json(
            {
                "ok": True,
                "pid": proc.pid,
                "serverStarted": server_started,
                "steamPromptClicked": clicked,
                "agentStopped": stop_response,
            }
        )
        if not args.no_wait:
            proc.wait()
            print_json({"ok": True, "guiClosed": True, "exitCode": proc.returncode})
        return

    if args.command == "watch-gui":
        if os.getenv("FACTORIO_AI_ALLOW_MODDED_WATCH") != "1":
            raise SystemExit(
                "watch-gui is a deferred development mod/RCON watch mode. "
                "Set FACTORIO_AI_ALLOW_MODDED_WATCH=1 only for local development."
            )
        server_started = False
        try:
            wait_for_rcon(cfg, timeout_seconds=3)
        except Exception:
            create_save(cfg)
            server_proc = start_server(cfg)
            server_started = True
            wait_for_rcon(cfg)
            print_json({"ok": True, "serverStarted": True, "serverPid": server_proc.pid})

        proc = start_gui_client(cfg, window_size=args.window_size, connect=True, ensure_mod=False)
        clicked = False
        try:
            clicked = VanillaGuiDriver(cfg).click_steam_continue_prompt(timeout_seconds=args.confirm_timeout)
        except Exception:
            clicked = False
        print_json({"ok": True, "pid": proc.pid, "serverStarted": server_started, "steamPromptClicked": clicked, "mode": "watch"})
        if not args.no_wait:
            proc.wait()
            print_json({"ok": True, "guiClosed": True, "exitCode": proc.returncode})
        return

    if args.command == "launch-save-gui":
        proc = start_save_gui(cfg, window_size=args.window_size)
        print_json({"ok": True, "pid": proc.pid, "savePath": str(cfg.save_path)})
        return

    if args.command == "launch-vanilla-gui":
        launch_args: list[str] = []
        if args.window_size:
            launch_args.extend(["--window-size", args.window_size])
        steam_mod_list = prepare_steam_vanilla_mod_list(cfg.runtime_dir) if not args.direct else None
        proc = launch_vanilla_gui(
            cfg,
            via_steam=not args.direct,
            args=launch_args,
            prepare_steam_mod_list=args.direct,
        )
        driver = VanillaGuiDriver(cfg)
        prompt_clicked = False
        try:
            prompt_clicked = driver.click_steam_continue_prompt(timeout_seconds=20.0)
        except Exception:
            prompt_clicked = False
        window_ready = driver.activate_factorio(timeout_seconds=max(0.0, args.wait_timeout))
        window_state = driver.factorio_window_state()
        ok = bool(window_state.get("found"))
        payload = {
            "ok": ok,
            "pid": proc.pid if proc else None,
            "viaSteam": not args.direct,
            "isolatedModDirectory": str(prepare_vanilla_mod_directory(cfg.runtime_dir)) if args.direct else None,
            "steamModList": steam_mod_list,
            "steamPromptClicked": prompt_clicked,
            "windowReady": window_ready,
            "window": window_state,
            "diagnostics": driver.factorio_window_diagnostics(),
        }
        if not ok:
            payload["reason"] = "real Factorio game window was not detected after launch"
        print_json(payload)
        if not ok:
            raise SystemExit(1)
        return

    if args.command == "prepare-steam-vanilla-mod-list":
        print_json({"ok": True, "steamModList": prepare_steam_vanilla_mod_list(cfg.runtime_dir)})
        return

    if args.command == "restore-steam-mod-list":
        print_json({"ok": True, "steamModList": restore_latest_steam_mod_list(cfg.runtime_dir)})
        return

    if args.command == "vanilla-window":
        driver = VanillaGuiDriver(cfg)
        activated = driver.activate_factorio(timeout_seconds=3.0) if args.activate else False
        print_json(
            {
                "ok": True,
                "activated": activated,
                "window": driver.factorio_window_state(),
                "diagnostics": driver.factorio_window_diagnostics(),
            }
        )
        return

    if args.command == "vanilla-screenshot":
        snapshot = VanillaGuiDriver(cfg).capture_factorio_window(args.output, method=args.method)
        print_json({"ok": True, "snapshot": snapshot.to_dict(), "method": args.method})
        return

    if args.command == "vanilla-screen-state":
        output = args.output or str(cfg.runtime_dir / "vanilla" / "screenshots" / "screen-state.bmp")
        snapshot = None
        if not args.no_capture:
            snapshot = VanillaGuiDriver(cfg).capture_factorio_window(output, method=args.method)
            output = str(snapshot.path)
        state = classify_bmp_file(output)
        print_json(
            {
                "ok": True,
                "screen_state": state.to_dict(),
                "snapshot": snapshot.to_dict() if snapshot else None,
                "method": args.method,
            }
        )
        return

    if args.command == "vanilla-key":
        driver = VanillaGuiDriver(cfg)
        if args.background:
            posted = driver.post_key_to_factorio(args.key, duration_seconds=args.duration)
            print_json({"ok": posted, "mode": "background", "key": args.key, "verified": False})
            if not posted:
                raise SystemExit(1)
        else:
            activated = driver.activate_factorio(timeout_seconds=5.0)
            if not activated:
                print_json({"ok": False, "mode": "foreground", "key": args.key, "reason": "Factorio window not found"})
                raise SystemExit(1)
            driver.press_key(args.key, duration_seconds=args.duration)
            print_json({"ok": True, "mode": "foreground", "key": args.key, "activated": activated})
        return

    if args.command == "vanilla-start-freeplay":
        result = VanillaGuiDriver(cfg).start_space_age_freeplay_from_main_menu(skip_intro=not args.no_skip_intro)
        print_json({"ok": True, "result": result})
        return

    if args.command == "vanilla-probe":
        report = VanillaGuiDriver(cfg).probe_background_capabilities(
            args.output_dir,
            minimize_check=args.minimize_check,
            background_key=args.background_key,
        )
        print_json({"ok": report.window_found, "probe": report.to_dict()})
        if not report.window_found:
            raise SystemExit(1)
        return

    if args.command == "confirm-steam-launch":
        clicked = VanillaGuiDriver(cfg).click_steam_continue_prompt(timeout_seconds=args.timeout)
        print_json({"ok": clicked})
        if not clicked:
            raise SystemExit(1)
        return

    if args.command == "observe":
        print_json(FactorioController(cfg).observe())
        return

    if args.command == "no-mod-observe":
        print_json(
            ModlessLuaController(cfg).observe(
                player_name=args.player,
                include_planning_sites=args.full_planning_sites,
            )
        )
        return

    if args.command == "design-cells":
        from . import cell_autodesign

        observation = ModlessLuaController(cfg).observe(
            player_name=getattr(args, "player", None),
            include_planning_sites=False,
        )
        print_json(cell_autodesign.design_cells(cfg, observation, objective=args.objective, top_n=args.top))
        return

    if args.command == "no-mod-action":
        try:
            action = json.loads(args.json_action)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid action JSON: {exc}") from exc
        print_json(ModlessLuaController(cfg).act(action, player_name=args.player))
        return

    if args.command == "apply-cell-design":
        from . import cell_apply

        class _ActAdapter:
            def __init__(self, controller, player):
                self._c, self._player = controller, player

            def act(self, action):
                return self._c.act(action, player_name=self._player)

        controller = _ActAdapter(ModlessLuaController(cfg), args.player) if args.execute else None
        print_json(cell_apply.apply_design(
            controller, cfg.runtime_dir, args.key, args.x, args.y, execute=args.execute,
        ))
        return

    if args.command == "strategy":
        require_llm = args.require_llm or os.getenv("FACTORIO_AI_REQUIRE_LLM_STRATEGY", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        print_json(FactorioController(cfg).strategy_decision(args.objective, require_llm=require_llm))
        return

    if args.command == "no-mod-strategy":
        require_llm = args.require_llm or os.getenv("FACTORIO_AI_REQUIRE_LLM_STRATEGY", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        payload = ModlessFactorioController(cfg).strategy_decision(args.objective, require_llm=require_llm)
        payload["adapter"] = "no-mod-rcon-lua"
        print_json(payload)
        return

    if args.command == "run-strategy-step":
        require_llm = args.require_llm or os.getenv("FACTORIO_AI_REQUIRE_LLM_STRATEGY", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        summary = FactorioController(cfg).run_strategy_step(
            objective=args.objective,
            require_llm=require_llm,
            target_count=args.target,
            max_steps=args.max_steps,
        )
        print_json(summary.to_dict())
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-no-mod-strategy-step":
        _guard_observer_player_control(cfg, args.command)
        require_llm = args.require_llm or os.getenv("FACTORIO_AI_REQUIRE_LLM_STRATEGY", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        summary = ModlessFactorioController(cfg).run_strategy_step(
            objective=args.objective,
            require_llm=require_llm,
            target_count=args.target,
            max_steps=args.max_steps,
        )
        payload = summary.to_dict()
        payload["adapter"] = "no-mod-rcon-lua"
        print_json(payload)
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-autopilot":
        require_llm = args.require_llm or os.getenv("FACTORIO_AI_REQUIRE_LLM_STRATEGY", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        summary = FactorioController(cfg).run_autopilot_loop(
            objective=args.objective,
            require_llm=require_llm,
            target_count=args.target,
            max_steps=args.max_steps,
            cycles=args.cycles,
            sleep_seconds=args.sleep_seconds,
            continue_on_error=not args.stop_on_error,
        )
        print_json(summary.to_dict())
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-no-mod-autopilot":
        _guard_observer_player_control(cfg, args.command)
        require_llm = args.require_llm or os.getenv("FACTORIO_AI_REQUIRE_LLM_STRATEGY", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        summary = ModlessFactorioController(cfg).run_autopilot_loop(
            objective=args.objective,
            require_llm=require_llm,
            target_count=args.target,
            max_steps=args.max_steps,
            cycles=args.cycles,
            sleep_seconds=args.sleep_seconds,
            continue_on_error=not args.stop_on_error,
        )
        payload = summary.to_dict()
        payload["adapter"] = "no-mod-rcon-lua"
        print_json(payload)
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-no-mod-code-agent":
        _guard_observer_player_control(cfg, args.command)
        import time as _time
        from datetime import datetime, timezone
        from . import code_agent

        controller = ModlessFactorioController(cfg)
        player_name = controller._configured_agent_player_name()
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        log_path = cfg.log_dir / f"code-agent-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.jsonl"
        hb_path = cfg.runtime_dir / "code-agent-heartbeat.json"

        def _act(action):
            return controller._modless.act(action, player_name=player_name)

        # HYBRID: expose the proven deterministic skills as a tool the generated program can call.
        _skill_tools = [
            "produce_iron_plate", "produce_copper_plate", "setup_coal_supply", "setup_power",
            "research_automation", "bootstrap_build_item_mall", "build_gear_belt_mall_logistics",
            "build_iron_plate_logistic_line_to_gear_mall", "expand_iron_smelting",
            "produce_electronic_circuit", "research_logistics",
        ]
        _skill_tools = [s for s in _skill_tools if controller._skill_run_config(s) is not None]

        def _run_skill(name, max_steps):
            if name not in _skill_tools:
                return {"ok": False, "reason": f"unknown skill '{name}'; available: {_skill_tools}", "skill": name}
            try:
                summary = controller.run_strategy_step(override_skill=name, max_steps=int(max_steps))
                return {"ok": bool(summary.ok), "reason": str(summary.reason)[:200], "skill": name}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "skill": name}

        def _heartbeat(cycle, state, reason=""):
            try:
                hb_path.write_text(json.dumps({
                    "cycle": cycle, "state": state, "reason": reason,
                    "objective": args.objective, "pid": os.getpid(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }), encoding="utf-8")
            except OSError:
                pass

        def _on_step(cycle, program, result):
            rec = {
                "time": datetime.now(timezone.utc).isoformat(), "cycle": cycle,
                "ok": result.ok, "actions_run": result.actions_run,
                "program": program, "output": result.output[:2000], "error": result.error[:1000],
            }
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print_json({"cycle": cycle, "ok": result.ok, "actions_run": result.actions_run,
                        "output": result.output[:200], "error": result.error[:200]})
            _heartbeat(cycle + 1, "ok" if result.ok else "program_error",
                       reason=(result.error[:120] if result.error else f"{result.actions_run} actions"))
            _time.sleep(max(0.0, args.sleep_seconds))

        def _complete(prompt):
            return code_agent.remote_program_complete(prompt, max_tokens=args.max_tokens)

        _heartbeat(0, "starting")
        completed = code_agent.run_code_agent_loop(
            _act, controller.observe, _complete,
            objective=args.objective,
            cycles=args.cycles,
            timeout_seconds=args.program_timeout,
            on_step=_on_step,
            log=lambda m: None,
            run_skill=_run_skill,
            skill_names=_skill_tools,
        )
        print_json({"adapter": "no-mod-code-agent", "completed_steps": completed, "log": str(log_path)})
        return

    if args.command == "run-codex-wait-layout-loop":
        summary = FactorioController(cfg).run_codex_wait_layout_loop(
            objective=args.objective,
            cycles=args.cycles,
            sleep_seconds=args.sleep_seconds,
        )
        print_json(summary.to_dict())
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-no-mod-codex-wait-layout-loop":
        summary = ModlessFactorioController(cfg).run_codex_wait_layout_loop(
            objective=args.objective,
            cycles=args.cycles,
            sleep_seconds=args.sleep_seconds,
        )
        payload = summary.to_dict()
        payload["adapter"] = "no-mod-rcon-lua"
        print_json(payload)
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-idle-layout-loop":
        summary = FactorioController(cfg).run_idle_layout_loop(
            objective=args.objective,
            cycles=args.cycles,
            sleep_seconds=args.sleep_seconds,
            stale_seconds=args.stale_seconds,
            min_submit_interval_seconds=args.min_submit_interval_seconds,
        )
        print_json(summary.to_dict())
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-no-mod-idle-layout-loop":
        summary = ModlessFactorioController(cfg).run_idle_layout_loop(
            objective=args.objective,
            cycles=args.cycles,
            sleep_seconds=args.sleep_seconds,
            stale_seconds=args.stale_seconds,
            min_submit_interval_seconds=args.min_submit_interval_seconds,
        )
        payload = summary.to_dict()
        payload["adapter"] = "no-mod-rcon-lua"
        print_json(payload)
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command in {"run-skill-foundry-loop", "run-no-mod-skill-foundry-loop"}:
        no_mod = args.command.startswith("run-no-mod")
        controller = ModlessFactorioController(cfg) if no_mod else FactorioController(cfg)
        result = controller.run_skill_foundry_loop(
            objective=args.objective,
            cycles=args.cycles,
            sleep_seconds=args.sleep_seconds,
            max_attempts=args.max_attempts,
            require_idle=args.require_idle,
            throttle_seconds=args.throttle_seconds,
        )
        if no_mod:
            result["adapter"] = "no-mod-rcon-lua"
        print_json(result)
        if not result.get("ok"):
            raise SystemExit(1)
        return

    if args.command == "begin-codex-work":
        controller = ModlessFactorioController(cfg) if args.no_mod else FactorioController(cfg)
        print_json(controller.begin_codex_work(args.objective, args.selected_skill, args.reason))
        return

    if args.command == "finish-codex-work":
        controller = ModlessFactorioController(cfg) if args.no_mod else FactorioController(cfg)
        print_json(controller.finish_codex_work(args.selected_skill, clear_reason=args.reason))
        return

    if args.command == "web":
        urls = public_dashboard_urls(args.host, args.port)
        print_json(
            {
                "ok": True,
                "url": urls[0],
                "urls": urls,
                "host": args.host,
                "port": args.port,
                "route": FACTORIO_ROUTE,
            }
        )
        serve_dashboard(cfg, host=args.host, port=args.port, objective=args.objective)
        return

    if args.command == "validate-layout-candidate":
        try:
            observation = ModlessLuaController(cfg).observe(player_name=args.player, include_planning_sites=False)
            adapter = "no-mod-rcon-lua"
        except Exception:
            observation = FactorioController(cfg).observe()
            adapter = "custom-mod-rcon"
        feedback = validate_layout_candidate(
            cfg,
            observation,
            candidate_id=args.candidate_id,
            variant=args.variant,
            ticks=max(0, args.ticks),
            cleanup=not args.no_cleanup,
        )
        feedback["ok"] = feedback.get("sandbox_validation", {}).get("status") == "pass"
        feedback["adapter"] = adapter
        feedback["log_path"] = str(cfg.log_dir / "layout-validation-feedback.jsonl")
        print_json(feedback)
        if not feedback["ok"]:
            raise SystemExit(1)
        return

    if args.command == "record-token-usage":
        sample = record_token_usage(
            cfg.log_dir,
            args.tokens_used,
            label=args.label,
            source=args.source,
            timestamp=args.timestamp,
        )
        print_json({"ok": True, "sample": sample.to_dict()})
        return

    if args.command == "record-current-codex-thread-usage":
        sample, thread = record_current_codex_thread_usage(
            cfg.log_dir,
            state_db_path=Path(args.state_db) if args.state_db else None,
            cwd=args.cwd if args.cwd else Path.cwd(),
            thread_id=args.thread_id,
            label=args.label,
            source=args.source,
            timestamp=args.timestamp,
        )
        print_json({"ok": True, "sample": sample.to_dict(), "thread": thread.to_dict()})
        return

    if args.command == "token-usage-summary":
        print_json({"ok": True, "token_usage": token_usage_summary(cfg.log_dir)})
        return

    if args.command == "run-journal-summary":
        print_json({"ok": True, "run_journal": run_journal_summary(cfg.log_dir)})
        return

    if args.command == "archive-training-traces":
        output_root = Path(args.output_root) if args.output_root else cfg.runtime_dir / "trace_archives"
        result = archive_training_traces(
            cfg.log_dir,
            output_root,
            label=args.label,
            copy_raw=not args.no_copy_raw,
            include_text_logs=not args.no_text_logs,
            limit=args.limit,
        )
        print_json({"ok": True, "trace_archive": result})
        return

    if args.command == "trace-archive-summary":
        output_root = Path(args.output_root) if args.output_root else cfg.runtime_dir / "trace_archives"
        print_json({"ok": True, "trace_archive": trace_archive_summary(output_root, limit=args.limit)})
        return

    if args.command == "action":
        try:
            action = json.loads(args.json_action)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid action JSON: {exc}") from exc
        print_json(FactorioController(cfg).act(action))
        return

    if args.command == "run-iron-mvp":
        summary = FactorioController(cfg).run_iron_mvp(target=args.target, max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "ironPlateCount": summary.item_count,
                "logPath": str(summary.log_path),
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-no-mod-iron-mvp":
        summary = ModlessFactorioController(cfg).run_iron_mvp(target=args.target, max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "ironPlateCount": summary.item_count,
                "logPath": str(summary.log_path),
                "mode": "no-mod-rcon-lua",
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-copper-mvp":
        summary = FactorioController(cfg).run_copper_mvp(target=args.target, max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "copperPlateCount": summary.item_count,
                "logPath": str(summary.log_path),
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-no-mod-copper-mvp":
        summary = ModlessFactorioController(cfg).run_copper_mvp(target=args.target, max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "copperPlateCount": summary.item_count,
                "logPath": str(summary.log_path),
                "mode": "no-mod-rcon-lua",
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-circuit-mvp":
        summary = FactorioController(cfg).run_circuit_mvp(target=args.target, max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "electronicCircuitCount": summary.item_count,
                "logPath": str(summary.log_path),
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-no-mod-circuit-mvp":
        summary = ModlessFactorioController(cfg).run_circuit_mvp(target=args.target, max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "electronicCircuitCount": summary.item_count,
                "logPath": str(summary.log_path),
                "mode": "no-mod-rcon-lua",
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-science-mvp":
        summary = FactorioController(cfg).run_science_mvp(target=args.target, max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "automationSciencePackCount": summary.item_count,
                "logPath": str(summary.log_path),
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-no-mod-science-mvp":
        summary = ModlessFactorioController(cfg).run_science_mvp(target=args.target, max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "automationSciencePackCount": summary.item_count,
                "logPath": str(summary.log_path),
                "mode": "no-mod-rcon-lua",
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-belt-smelting-mvp":
        summary = FactorioController(cfg).run_belt_smelting_mvp(target=args.target, max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "ironPlateCount": summary.item_count,
                "logPath": str(summary.log_path),
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-expand-iron-smelting-mvp":
        summary = FactorioController(cfg).run_expand_iron_smelting_mvp(target_rate=args.target_rate, max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "ironPlateCount": summary.item_count,
                "targetRatePerMinute": args.target_rate,
                "logPath": str(summary.log_path),
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-expand-copper-smelting-mvp":
        summary = FactorioController(cfg).run_expand_copper_smelting_mvp(target_rate=args.target_rate, max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "copperPlateCount": summary.item_count,
                "targetRatePerMinute": args.target_rate,
                "logPath": str(summary.log_path),
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-power-mvp":
        summary = FactorioController(cfg).run_power_mvp(max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "logPath": str(summary.log_path),
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-no-mod-power-mvp":
        summary = ModlessFactorioController(cfg).run_power_mvp(max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "logPath": str(summary.log_path),
                "mode": "no-mod-rcon-lua",
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-automation-research-mvp":
        summary = FactorioController(cfg).run_automation_research_mvp(max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "automationSciencePackCount": summary.item_count,
                "logPath": str(summary.log_path),
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-no-mod-automation-research-mvp":
        summary = ModlessFactorioController(cfg).run_automation_research_mvp(max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "automationSciencePackCount": summary.item_count,
                "logPath": str(summary.log_path),
                "mode": "no-mod-rcon-lua",
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-circuit-automation-mvp":
        summary = FactorioController(cfg).run_circuit_automation_mvp(target=args.target, max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "electronicCircuitCount": summary.item_count,
                "logPath": str(summary.log_path),
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-no-mod-circuit-automation-mvp":
        summary = ModlessFactorioController(cfg).run_circuit_automation_mvp(target=args.target, max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "electronicCircuitCount": summary.item_count,
                "logPath": str(summary.log_path),
                "mode": "no-mod-rcon-lua",
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-logistics-research-mvp":
        summary = FactorioController(cfg).run_logistics_research_mvp(max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "automationSciencePackCount": summary.item_count,
                "logPath": str(summary.log_path),
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-no-mod-logistics-research-mvp":
        summary = ModlessFactorioController(cfg).run_logistics_research_mvp(max_steps=args.max_steps)
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "automationSciencePackCount": summary.item_count,
                "logPath": str(summary.log_path),
                "mode": "no-mod-rcon-lua",
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-build-item-mall-mvp":
        summary = FactorioController(cfg).run_build_item_mall_mvp(
            target_item=args.item,
            target=args.target,
            max_steps=args.max_steps,
        )
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "itemName": summary.item_name,
                "itemCount": summary.item_count,
                "target": args.target,
                "logPath": str(summary.log_path),
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "run-no-mod-build-item-mall-mvp":
        summary = ModlessFactorioController(cfg).run_build_item_mall_mvp(
            target_item=args.item,
            target=args.target,
            max_steps=args.max_steps,
        )
        print_json(
            {
                "ok": summary.ok,
                "reason": summary.reason,
                "steps": summary.steps,
                "itemName": summary.item_name,
                "itemCount": summary.item_count,
                "target": args.target,
                "logPath": str(summary.log_path),
                "mode": "no-mod-rcon-lua",
            }
        )
        if not summary.ok:
            raise SystemExit(1)
        return

    if args.command == "slurm-deploy":
        print_json(remote_slurm.deploy())
        return

    if args.command == "slurm-start-worker":
        print_json(remote_slurm.submit_worker_job())
        return

    if args.command == "slurm-ensure-worker":
        print_json(remote_slurm.ensure_worker_job(renew_before_minutes=args.renew_before_minutes))
        return

    if args.command == "slurm-status":
        print_json(remote_slurm.status())
        return

    if args.command == "slurm-llm-status":
        print_json(remote_slurm.llm_status())
        return

    if args.command == "slurm-vllm-service-status":
        print_json(remote_slurm.vllm_service_status())
        return

    if args.command == "slurm-ensure-vllm-service":
        print_json(remote_slurm.ensure_vllm_service(duration_seconds=args.duration_seconds))
        return

    if args.command == "slurm-cancel":
        print_json(remote_slurm.cancel())
        return

    if args.command == "slurm-submit-test":
        result = remote_slurm.request_plan(
            observation={"inventory": {"coal": 4}, "resources": [], "entities": []},
            legal_actions=[{"type": "wait", "ticks": 60}],
            goal="produce_iron_plate",
            timeout_seconds=30,
        )
        print_json(result)
        return

    if args.command == "slurm-submit-model-benchmark":
        models = [item.strip() for item in args.models.split(",") if item.strip()]
        try:
            observation = ModlessFactorioController(cfg).observe()
        except Exception:
            observation = {"inventory": {}, "entities": [], "resources": [], "enemies": []}
        payload = {
            "objective": args.objective,
            "observation": observation,
            "production_targets": {},
            "available_skills": [],
        }
        if args.attached:
            os.environ["FACTORIO_AI_SLURM_MODE"] = "attach"
            if not args.no_deploy:
                remote_slurm.deploy()
        result = remote_slurm.request_strategy_model_benchmark(payload, models=models, timeout_seconds=args.timeout)
        print_json(result)
        return

    if args.command == "slurm-compare-strategy-workers":
        observation = ModlessFactorioController(cfg).observe()
        production_targets = load_targets(cfg.runtime_dir, args.objective).per_minute
        workers = remote_slurm.parse_strategy_worker_specs(args.workers)
        result = remote_slurm.compare_strategy_workers(
            objective=args.objective,
            observation=observation,
            production_targets=production_targets,
            available_skills=skill_catalog_payload(),
            workers=workers,
            timeout_seconds=args.timeout,
        )
        if not args.no_log:
            cfg.log_dir.mkdir(parents=True, exist_ok=True)
            with (cfg.log_dir / "strategy-worker-comparison.jsonl").open("a", encoding="utf-8") as log_file:
                json.dump(result, log_file, ensure_ascii=False, separators=(",", ":"))
                log_file.write("\n")
        print_json(result)
        return

    raise SystemExit(f"unsupported command: {args.command}")


def print_json(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
