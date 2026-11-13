"""CLI used by the gameplay subagent; never calls a model API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from factorio_agent.core.deterministic_state import RunLock, _atomic_json
from . import runtime
from .jobs import CAPABILITIES, Ledger, Request, StarterExecutors, control, run_jobs, run_slice, submit, summary


class OneShotServices:
    """Build game access objects per command. A long-lived session supplies cached ones instead."""

    def game(self, root):
        return runtime.game_for(root)

    def catalog(self, root, game):
        return runtime.catalog_for(root, game)

    def executors(self, root, game, catalog):
        return StarterExecutors(game, catalog)


def execute(argv=None, services=None):
    """Run one typed command and return its receipt. Raises on failure; never prints."""
    services = services or OneShotServices()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=Path("runtime/v2/dev-101"))
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--seed", type=int, default=101)
    create.add_argument("--server-port", type=int, default=34260)
    create.add_argument("--rcon-port", type=int, default=27060)
    for command in ("start", "connect-player", "observe", "capabilities", "actor-status", "checkpoint"):
        commands.add_parser(command)
    recovery = commands.add_parser("recover-actor")
    recovery.add_argument("--world-id", required=True)
    recovery.add_argument("--expected-old-unit", type=int, required=True)
    recovery.add_argument("--expected-new-unit", type=int, required=True)
    parking = commands.add_parser("return-to-base")
    parking.add_argument("--world-id", required=True)
    parking.add_argument("--expected-actor-unit", type=int, required=True)
    for name in ("belongings-status", "recover-belongings"):
        belongings = commands.add_parser(name)
        belongings.add_argument("--world-id", required=True)
        belongings.add_argument("--expected-actor-unit", type=int, required=True)
        belongings.add_argument("--death-tick", type=int, required=True)
    commands.add_parser("stop-worker")
    commands.add_parser("clear-worker-stop")
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
    request.add_argument("--product")
    request.add_argument("--rate-per-minute", type=float)
    request.add_argument("--priority", type=int, default=0, help="higher runs first among active jobs")
    execute = commands.add_parser("run")
    selection = execute.add_mutually_exclusive_group(required=True)
    selection.add_argument("--key")
    selection.add_argument("--all", action="store_true", help="interleave every active job")
    execute.add_argument("--seconds", type=float, default=25)
    worker = commands.add_parser("worker")
    worker.add_argument("--key", required=True)
    worker.add_argument("--max-seconds", type=float, default=900)
    for name in ("pause", "resume", "reconcile", "retry"):
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
    elif args.command == "checkpoint":
        with RunLock(root / "writer.lock"):
            game = services.game(root)
            obs = game.query("return {ok=true,world_id=d.world_id,tick=game.tick}")
            if not obs.get("ok"):
                raise RuntimeError(obs.get("reason", "live world observation required"))
            game.save()
            Ledger(root).event("world-lifecycle", "checkpoint_requested", tick=obs["tick"], world_id=obs["world_id"])
            result = {"status": "checkpoint_requested", "world_id": obs["world_id"], "observed_tick": obs["tick"]}
    elif args.command == "watch":
        from factorio_agent.core.deterministic_character import set_client_visibility

        result = set_client_visibility(runtime.configuration(root), visible=not args.hide)
    elif args.command == "capabilities":
        result = CAPABILITIES
    elif args.command == "actor-status":
        from .actor_recovery import inspect_actor_recovery

        result = inspect_actor_recovery(services.game(root))
    elif args.command == "recover-actor":
        from .actor_recovery import execute_actor_recovery

        with RunLock(root / "writer.lock"):
            game = services.game(root)
            action = dict(
                type="recover_actor",
                world_id=args.world_id,
                expected_old_unit=args.expected_old_unit,
                expected_new_unit=args.expected_new_unit,
            )
            result = execute_actor_recovery(game, args.world_id, args.expected_old_unit, args.expected_new_unit)
            game._record_action(action, result)
            Ledger(root).event(
                "actor-lifecycle",
                "actor_recovery",
                tick=result.get("tick"),
                old_unit=args.expected_old_unit,
                new_unit=args.expected_new_unit,
                result=result,
            )
    elif args.command == "belongings-status":
        from .corpse_recovery import inspect_corpse_recovery

        result = inspect_corpse_recovery(services.game(root), args.world_id, args.expected_actor_unit, args.death_tick)
    elif args.command == "return-to-base":
        from .defense_repairs import return_to_spawn_action

        with RunLock(root / "writer.lock"):
            game = services.game(root)
            action = return_to_spawn_action(game, args.world_id, args.expected_actor_unit)
            result = game.act(action) if action.get("type") else action
            Ledger(root).event("actor-lifecycle", "return_to_base", result=result)
    elif args.command == "recover-belongings":
        from .corpse_recovery import execute_corpse_recovery

        with RunLock(root / "writer.lock"):
            game = services.game(root)
            action = dict(
                type="recover_owned_belongings",
                world_id=args.world_id,
                expected_actor_unit=args.expected_actor_unit,
                death_tick=args.death_tick,
                keep_single_repair_tool=True,
            )
            receipt = execute_corpse_recovery(
                game, args.world_id, args.expected_actor_unit, args.death_tick, keep_single_repair_tool=True
            )
            game._record_action(action, receipt)
            result = {k: v for k, v in receipt.items() if k != "conservation"}
            Ledger(root).event("actor-lifecycle", "belongings_recovery", tick=result.get("tick"), result=result)
    elif args.command == "status":
        data = Ledger(root).data
        result = data["jobs"].get(args.key, {"status": "error", "reason": "unknown_job_key"}) if args.key else data
    elif args.command == "stop-worker":
        if not root.is_dir():
            raise ValueError("runtime directory does not exist")
        # Out-of-band input, consumed between durable actions by run_slice.
        # Do not contend for its writer lock or edit the job ledger here.
        stop = root / "stop.json"
        if not stop.exists():
            _atomic_json(stop, {"source": "gameplay-worker-cli", "requested_at": time.time()})
        result = {"ok": True, "status": "stop_requested"}
    elif args.command == "clear-worker-stop":
        stop = root / "stop.json"
        if stop.exists():
            marker = json.loads(stop.read_text(encoding="utf-8"))
            if marker.get("source") != "gameplay-worker-cli":
                raise ValueError("stop marker belongs to another operator")
            stop.unlink()
        result = {"ok": True, "status": "worker_stop_cleared"}
    elif args.command == "observe":
        game = services.game(root)
        obs = game.observe()
        if not obs.get("ok"):
            raise RuntimeError(obs.get("reason", "observation_failed"))
        obs["electricity"] = runtime.electricity_snapshot(game)
        from .defense_policy import DefensePolicy, assess_risk

        try:
            obs["adaptive_defense_risk"] = assess_risk(DefensePolicy(game, None).survey(obs), obs["world_id"])
        except (ValueError, KeyError, TypeError, RuntimeError):
            obs["adaptive_defense_risk"] = {
                "status": "unavailable",
                "reason": "bounded current producer and threat facts unavailable",
            }
        from .diagnostics import (
            production_rates,
            construction_budget,
            port_lane_snapshot,
            smelting_throughput_bounds,
            factory_constraints,
        )

        obs["production_rate_measurement"] = production_rates(root, obs)
        obs["belt_lane_observation"] = port_lane_snapshot(root, game, obs)
        if (root / "catalog.json").exists():
            obs["known_project_construction_budget"] = construction_budget(root, obs, services.catalog(root, game))
            obs["smelting_throughput_bounds"] = smelting_throughput_bounds(root, obs, services.catalog(root, game))
            obs["factory_constraints"] = factory_constraints(root, obs, services.catalog(root, game))
            from .normal_research import ready_options

            obs["available_research"] = ready_options(services.catalog(root, game), obs)
            from .capability_gates import native_recipe_gates

            obs["native_capability_gates"] = native_recipe_gates(services.catalog(root, game), obs, CAPABILITIES)
            from .supply_planning import supply_options

            obs["resource_supply_options"] = supply_options(
                root,
                game,
                obs,
                services.catalog(root, game),
                obs["known_project_construction_budget"],
                obs["production_rate_measurement"],
            )
            from .progression import lookahead_demand

            try:
                jobs_list = list(Ledger(root).data["jobs"].values())
                obs["roadmap_material_demand"] = lookahead_demand(services.catalog(root, game), jobs_list, obs)
            except Exception as exc:  # a demand estimate must never break observe
                obs["roadmap_material_demand"] = {}
                obs["roadmap_material_demand_error"] = f"{type(exc).__name__}: {exc}"[:200]
            from .progression import bulk_item_demand

            try:
                jobs_list = list(Ledger(root).data["jobs"].values())
                obs["bulk_item_demand"] = bulk_item_demand(services.catalog(root, game), jobs_list, obs)
            except Exception as exc:  # a demand estimate must never break observe
                obs["bulk_item_demand"] = {}
                obs["bulk_item_demand_error"] = f"{type(exc).__name__}: {exc}"[:200]
            from .supply_cell import candidates as supply_cell_candidates

            try:
                jobs_list = list(Ledger(root).data["jobs"].values())
                obs["supply_cell_candidates"] = supply_cell_candidates(
                    services.catalog(root, game), jobs_list, obs, root=root
                )
            except Exception as exc:  # a demand estimate must never break observe
                obs["supply_cell_candidates_error"] = f"{type(exc).__name__}: {exc}"[:200]
            from .research_capacity import research_capacity_receipt

            try:
                jobs_list = list(Ledger(root).data["jobs"].values())
                obs["research_capacity"] = research_capacity_receipt(services.catalog(root, game), jobs_list, obs)
            except Exception as exc:  # a sizing estimate must never break observe
                obs["research_capacity_error"] = f"{type(exc).__name__}: {exc}"[:200]
            from .science_growth import growth_receipt

            try:
                jobs_list = list(Ledger(root).data["jobs"].values())
                growth = growth_receipt(root, services.catalog(root, game), jobs_list, obs)
                if growth is not None:
                    obs["science_growth"] = growth
            except Exception as exc:  # a growth estimate must never break observe
                obs["science_growth_error"] = f"{type(exc).__name__}: {exc}"[:200]
        result = summary(obs, Ledger(root))
    elif args.command == "request":
        parameters = json.loads(args.parameters)
        typed = {
            name: getattr(args, name)
            for name in ("resource", "cells", "technology", "product", "rate_per_minute")
            if getattr(args, name) is not None
        }
        if typed:
            if parameters:
                raise ValueError("use typed parameters or JSON, not both")
            parameters = typed
        result = submit(
            root,
            services.game(root),
            Request(args.key, args.world_id, args.observed_tick, args.capability, parameters),
            priority=args.priority,
        )
    elif args.command in {"pause", "resume", "reconcile", "retry"}:
        result = control(root, services.game(root), args.key, args.command)
    elif args.command in {"run", "worker"}:
        game = services.game(root)
        # Catalog publication and job execution share the same writer lock.
        with RunLock(root / "writer.lock"):
            catalog = services.catalog(root, game)
        executors = services.executors(root, game, catalog)
        if args.command == "run" and args.all:
            result = run_jobs(root, game, executors, seconds=args.seconds)
        elif args.command == "run":
            result = run_slice(root, game, executors, args.key, seconds=args.seconds)
        else:
            if not 0 < args.max_seconds <= 3600:
                raise ValueError("worker duration must be greater than zero and at most 3600 seconds")
            deadline = time.monotonic() + args.max_seconds
            while True:
                result = run_slice(
                    root, game, executors, args.key, seconds=min(50, max(0.001, deadline - time.monotonic()))
                )
                print(
                    json.dumps({k: result.get(k) for k in ("key", "status", "reason", "last_tick", "actions")}),
                    flush=True,
                )
                if (
                    result["status"] in {"succeeded", "blocked", "failed", "paused"}
                    or result["reason"] == "operator_stop_requested"
                    or time.monotonic() >= deadline
                ):
                    break
    return result


def main(argv=None):
    print(json.dumps(execute(argv), ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, FileNotFoundError, FileExistsError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
