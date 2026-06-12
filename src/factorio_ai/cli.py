from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .config import load_config
from .controller import FactorioController
from .factorio import create_save, install_mod, start_server, wait_for_rcon
from . import remote_slurm


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Factorio AI autoplayer")
    parser.add_argument("--config", help="Path to config.json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("install-mod", help="Install the Factorio AI mod into the runtime mod directory")

    create_save_parser = subparsers.add_parser("create-save", help="Create the local MVP save")
    create_save_parser.add_argument("--overwrite", action="store_true")

    start_server_parser = subparsers.add_parser("start-server", help="Start local Factorio server and wait")
    start_server_parser.add_argument("--no-wait-rcon", action="store_true")

    subparsers.add_parser("observe", help="Print /ai_observe JSON")

    action_parser = subparsers.add_parser("action", help="Execute /ai_action JSON")
    action_parser.add_argument("json_action")

    run_parser = subparsers.add_parser("run-iron-mvp", help="Run the iron plate MVP loop")
    run_parser.add_argument("--target", type=int, default=10)
    run_parser.add_argument("--max-steps", type=int, default=200)

    subparsers.add_parser("slurm-deploy", help="Deploy project source to the Slurm remote directory")
    subparsers.add_parser("slurm-start-worker", help="Submit the persistent Slurm worker job")
    subparsers.add_parser("slurm-status", help="Print Slurm worker status")
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

    if args.command == "observe":
        print_json(FactorioController(cfg).observe())
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
                "ironPlateCount": summary.iron_plate_count,
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
