from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .config import load_config
from .controller import FactorioController
from .factorio import create_save, install_mod, start_gui_client, start_save_gui, start_server, wait_for_rcon
from . import remote_slurm
from .vanilla_gui import VanillaGuiDriver, launch_vanilla_gui
from .vanilla_perception import classify_bmp_file
from .web_dashboard import FACTORIO_ROUTE, public_dashboard_urls, serve_dashboard


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Factorio AI autoplayer")
    parser.add_argument("--config", help="Path to config.json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("install-mod", help="Install the Factorio AI mod into the runtime mod directory")

    create_save_parser = subparsers.add_parser("create-save", help="Create the local MVP save")
    create_save_parser.add_argument("--overwrite", action="store_true")

    start_server_parser = subparsers.add_parser("start-server", help="Start local Factorio server and wait")
    start_server_parser.add_argument("--no-wait-rcon", action="store_true")

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

    vanilla_probe_parser = subparsers.add_parser(
        "vanilla-probe",
        help="Probe vanilla screen capture and background/minimized input capabilities",
    )
    vanilla_probe_parser.add_argument("--output-dir", help="Directory for probe screenshots")
    vanilla_probe_parser.add_argument("--minimize-check", action="store_true", help="Temporarily minimize Factorio and try PrintWindow capture")
    vanilla_probe_parser.add_argument("--background-key", help="Post one key to the Factorio window without foreground activation")

    confirm_parser = subparsers.add_parser("confirm-steam-launch", help="Click Steam's custom-arguments continue prompt")
    confirm_parser.add_argument("--timeout", type=float, default=15.0)

    subparsers.add_parser("observe", help="Print /ai_observe JSON")

    strategy_parser = subparsers.add_parser("strategy", help="Ask the strategic LLM layer for the next high-level skill")
    strategy_parser.add_argument("--objective", default="launch_rocket_program")
    strategy_parser.add_argument("--require-llm", action="store_true")

    strategy_step_parser = subparsers.add_parser(
        "run-strategy-step",
        help="Ask the strategic layer for one high-level skill and execute it if implemented",
    )
    strategy_step_parser.add_argument("--objective", default="launch_rocket_program")
    strategy_step_parser.add_argument("--require-llm", action="store_true")
    strategy_step_parser.add_argument("--target", type=int, help="Override the selected skill item target count")
    strategy_step_parser.add_argument("--max-steps", type=int, help="Override the selected skill max step count")

    web_parser = subparsers.add_parser("web", help="Serve the Factorio production monitor at /factorio")
    web_parser.add_argument("--host", default="0.0.0.0")
    web_parser.add_argument("--port", type=int, default=18889)
    web_parser.add_argument("--objective", default="launch_rocket_program")

    action_parser = subparsers.add_parser("action", help="Execute /ai_action JSON")
    action_parser.add_argument("json_action")

    run_parser = subparsers.add_parser("run-iron-mvp", help="Run the iron plate MVP loop")
    run_parser.add_argument("--target", type=int, default=10)
    run_parser.add_argument("--max-steps", type=int, default=200)

    copper_parser = subparsers.add_parser("run-copper-mvp", help="Run the copper plate MVP loop")
    copper_parser.add_argument("--target", type=int, default=10)
    copper_parser.add_argument("--max-steps", type=int, default=250)

    circuit_parser = subparsers.add_parser("run-circuit-mvp", help="Run the electronic circuit MVP loop")
    circuit_parser.add_argument("--target", type=int, default=5)
    circuit_parser.add_argument("--max-steps", type=int, default=500)

    science_parser = subparsers.add_parser("run-science-mvp", help="Run the automation science MVP loop")
    science_parser.add_argument("--target", type=int, default=5)
    science_parser.add_argument("--max-steps", type=int, default=400)

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

    automation_research_parser = subparsers.add_parser(
        "run-automation-research-mvp",
        help="Build and feed the first lab to research Automation",
    )
    automation_research_parser.add_argument("--max-steps", type=int, default=1500)

    circuit_automation_parser = subparsers.add_parser(
        "run-circuit-automation-mvp",
        help="Build a powered assembler cell for electronic circuits",
    )
    circuit_automation_parser.add_argument("--target", type=int, default=5)
    circuit_automation_parser.add_argument("--max-steps", type=int, default=1800)

    logistics_research_parser = subparsers.add_parser(
        "run-logistics-research-mvp",
        help="Research Logistics with the first powered lab",
    )
    logistics_research_parser.add_argument("--max-steps", type=int, default=2200)

    build_item_mall_parser = subparsers.add_parser(
        "run-build-item-mall-mvp",
        help="Build a powered assembler cell for recurring factory-expansion items",
    )
    build_item_mall_parser.add_argument("--item", default="transport-belt")
    build_item_mall_parser.add_argument("--target", type=int, default=20)
    build_item_mall_parser.add_argument("--max-steps", type=int, default=1200)

    subparsers.add_parser("slurm-deploy", help="Deploy project source to the Slurm remote directory")
    subparsers.add_parser("slurm-start-worker", help="Submit the persistent Slurm worker job")
    subparsers.add_parser("slurm-status", help="Print Slurm worker status")
    subparsers.add_parser("slurm-llm-status", help="Print Slurm AUTO LLM readiness")
    subparsers.add_parser("slurm-cancel", help="Cancel the Slurm worker job")
    subparsers.add_parser("slurm-submit-test", help="Submit a planner test task to the Slurm worker")

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
        proc = launch_vanilla_gui(cfg, via_steam=not args.direct, args=launch_args)
        print_json({"ok": True, "pid": proc.pid if proc else None, "viaSteam": not args.direct})
        return

    if args.command == "vanilla-window":
        driver = VanillaGuiDriver(cfg)
        activated = driver.activate_factorio(timeout_seconds=3.0) if args.activate else False
        print_json({"ok": True, "activated": activated, "window": driver.factorio_window_state()})
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

    if args.command == "strategy":
        require_llm = args.require_llm or os.getenv("FACTORIO_AI_REQUIRE_LLM_STRATEGY", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        print_json(FactorioController(cfg).strategy_decision(args.objective, require_llm=require_llm))
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

    if args.command == "slurm-deploy":
        print_json(remote_slurm.deploy())
        return

    if args.command == "slurm-start-worker":
        print_json(remote_slurm.submit_worker_job())
        return

    if args.command == "slurm-status":
        print_json(remote_slurm.status())
        return

    if args.command == "slurm-llm-status":
        print_json(remote_slurm.llm_status())
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

    raise SystemExit(f"unsupported command: {args.command}")


def print_json(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
