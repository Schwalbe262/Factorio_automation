"""CLI used by the gameplay subagent; never calls a model API."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from factorio_ai.deterministic_state import RunLock
from . import runtime
from .jobs import CAPABILITIES, Ledger, Request, StarterExecutors, control, run_slice, submit, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=Path("runtime/v2/dev-101"))
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--seed", type=int, default=101)
    create.add_argument("--server-port", type=int, default=34260)
    create.add_argument("--rcon-port", type=int, default=27060)
    for command in ("start", "connect-player", "observe", "capabilities"):
        commands.add_parser(command)
    commands.add_parser("status").add_argument("--key")
    request = commands.add_parser("request")
    request.add_argument("--key", required=True)
    request.add_argument("--world-id", required=True)
    request.add_argument("--observed-tick", type=int, required=True)
    request.add_argument("--capability", required=True)
    request.add_argument("--parameters", default="{}", help="JSON object matching the capability contract")
    request.add_argument("--resource")
    request.add_argument("--cells", type=int)
    request.add_argument("--technology")
    request.add_argument("--rate-per-minute",type=float)
    execute = commands.add_parser("run")
    execute.add_argument("--key", required=True)
    execute.add_argument("--seconds", type=float, default=25)
    worker = commands.add_parser("worker")
    worker.add_argument("--key", required=True)
    worker.add_argument("--max-seconds", type=float, default=900)
    for name in ("pause", "resume", "reconcile"):
        commands.add_parser(name).add_argument("--key", required=True)
    watch = commands.add_parser("watch")
    watch.add_argument("--hide", action="store_true")
    args = parser.parse_args(argv)
    root = args.runtime.resolve()
    if args.command == "create":
        result = runtime.create(root, seed=args.seed, server_port=args.server_port, rcon_port=args.rcon_port)
    elif args.command == "start":
        result = runtime.start(root)
    elif args.command == "connect-player":
        result = runtime.connect_player(root)
    elif args.command == "watch":
        from factorio_ai.deterministic_character import set_client_visibility
        result = set_client_visibility(runtime.configuration(root), visible=not args.hide)
    elif args.command == "capabilities":
        result = CAPABILITIES
    elif args.command == "status":
        data=Ledger(root).data
        result=data["jobs"].get(args.key,{"status":"error","reason":"unknown_job_key"}) if args.key else data
    elif args.command == "observe":
        game = runtime.game_for(root)
        obs = game.observe()
        if not obs.get("ok"):
            raise RuntimeError(obs.get("reason", "observation_failed"))
        obs["electricity"] = runtime.electricity_snapshot(game)
        from .diagnostics import production_rates,construction_budget,port_lane_snapshot
        obs["production_rate_measurement"]=production_rates(root,obs)
        obs["belt_lane_observation"]=port_lane_snapshot(root,game,obs)
        if (root/"catalog.json").exists():
            obs["known_project_construction_budget"]=construction_budget(root,obs,runtime.catalog_for(root,game))
        result = summary(obs, Ledger(root))
    elif args.command == "request":
        parameters = json.loads(args.parameters)
        typed = {name: getattr(args, name) for name in ("resource", "cells", "technology","rate_per_minute") if getattr(args, name) is not None}
        if typed:
            if parameters:
                raise ValueError("use typed parameters or JSON, not both")
            parameters = typed
        result = submit(root, runtime.game_for(root), Request(args.key, args.world_id,
                        args.observed_tick, args.capability, parameters))
    elif args.command in {"pause", "resume", "reconcile"}:
        result = control(root, runtime.game_for(root), args.key, args.command)
    elif args.command in {"run", "worker"}:
        game = runtime.game_for(root)
        # Catalog publication and job execution share the same writer lock.
        with RunLock(root / "writer.lock"):
            catalog = runtime.catalog_for(root, game)
        executors = StarterExecutors(game, catalog)
        if args.command == "run":
            result = run_slice(root, game, executors, args.key, seconds=args.seconds)
        else:
            if not 0 < args.max_seconds <= 3600:
                raise ValueError("worker duration must be greater than zero and at most 3600 seconds")
            deadline = time.monotonic() + args.max_seconds
            while True:
                result = run_slice(root, game, executors, args.key, seconds=min(50, max(.001, deadline - time.monotonic())))
                print(json.dumps({k: result.get(k) for k in ("key", "status", "reason", "last_tick", "actions")}), flush=True)
                if result["status"] in {"succeeded", "blocked", "failed", "paused"} or result["reason"] == "operator_stop_requested" or time.monotonic() >= deadline:
                    break
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, FileNotFoundError, FileExistsError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
