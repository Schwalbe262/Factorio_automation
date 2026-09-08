"""Command line for isolated deterministic runs and observation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .deterministic_game import DeterministicGame, run_config, start_world
from .deterministic_state import request_stop


COMMANDS = {"run-no-mod-deterministic", "deterministic-status", "stop-deterministic", "watch-deterministic"}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Deterministic Space Age autoplayer; no model service required")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--seed", type=int, default=20260908)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--server-port", type=int, default=34200)
    parser.add_argument("--rcon-port", type=int, default=27015)
    parser.add_argument("--backend", choices=["assisted", "character"], default="assisted")
    parser.add_argument("--objective", default="first-space-age-rocket", choices=["first-space-age-rocket"])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--new-world", action="store_true")
    mode.add_argument("--resume", action="store_true")
    parser.add_argument("--connect-only", action="store_true", help="Use the already running dedicated server")
    parser.add_argument("--cycles", type=int, default=0, help="0 runs until the objective or an explicit blocker")
    parser.add_argument("--until", choices=["bootstrap", "power", "rocket"], default="rocket")
    args = parser.parse_args(argv)
    if not 1 <= args.server_port <= 65535 or not 1 <= args.rcon_port <= 65535:
        parser.error("ports must be between 1 and 65535")
    cfg = run_config(args.seed, runtime=args.runtime, server_port=args.server_port, rcon_port=args.rcon_port)
    if args.command == "stop-deterministic":
        request_stop(cfg.runtime_dir / "stop.json")
        print(json.dumps({"ok": True, "status": "stop_requested"}))
        return
    if args.command == "watch-deterministic":
        from .factorio import start_no_mod_gui_client
        process = start_no_mod_gui_client(cfg)
        print(json.dumps({"ok": True, "pid": process.pid, "address": f"127.0.0.1:{cfg.server_port}"}))
        return
    game = DeterministicGame(cfg, backend=args.backend)
    if args.command == "deterministic-status":
        state_path = cfg.runtime_dir / "status.json"
        print(state_path.read_text(encoding="utf-8") if state_path.exists() else
              json.dumps({"status": "not_started", "runtime": str(cfg.runtime_dir)}))
        return
    if not args.connect_only:
        if not args.new_world and not args.resume:
            parser.error("choose --new-world or --resume")
        start_world(cfg, seed=args.seed, new_world=args.new_world, backend=args.backend)
    from .deterministic_supervisor import DeterministicSupervisor
    supervisor = DeterministicSupervisor(game)
    result = supervisor.run(cycles=args.cycles, until=args.until)
    print(json.dumps(result, ensure_ascii=False))
    if result.get("status") in {"blocked", "failed"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
