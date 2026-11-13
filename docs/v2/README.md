# docs/v2 index

## Maintainer guides (current, checked against the code on 2026-11-13)

| Document | Use it for |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | runtime loop, module map, runtime files, decision layers, safety invariants |
| [ADDING_A_CAPABILITY.md](ADDING_A_CAPABILITY.md) | controller contract, registration, site, routing, state, roadmap, tests |
| [DEBUGGING_A_STALL.md](DEBUGGING_A_STALL.md) | report, ledger, read-only live inspection, known stall patterns, fix rules |
| [PLANNERS.md](PLANNERS.md) | planner formulas, defaults, tuning, pinning tests |
| [SUPERVISOR.md](SUPERVISOR.md) | supervisor flags, planner none/luna, incident replay |
| [LIFECYCLE.md](LIFECYCLE.md) | BAT menu and `lifecycle` commands; parallel measurement runs: "Fleet runs" below |
| [AUTOMATION_CONFIG.md](AUTOMATION_CONFIG.md) | `automation_config.json` fields and ranges |

## Dashboard (read-only run view)

```
python -B tools/dashboard.py [--runtime runtime/v2/<name>] [--port 8765]
```

Opens a server on `http://127.0.0.1:8765/` (loopback only; Ctrl+C stops it).
Default runtime: the `runtime` of `runtime/active-runtime.json`, the one
`lifecycle status` shows. It only reads files in the runtime folder: no RCON,
no locks, no writes. The page polls `/api/state` every 5 s; JSONL logs are
read incrementally and a missing or half-written JSON keeps its last good value.

| Panel | Reads |
|---|---|
| Header | `run.json` (seed), `tools/report_run.report` (wall/game minutes, planner, idle-gap share), `logs/controller-state.json` + `supervisor-process.json` (process check by pid + creation time, else freshness of the last controller event), `lifecycle-pause.json` |
| 진행 중 (now) | `jobs.json` (every non-succeeded job: status, reason, actions, age, evidence digest of blocked jobs), `work_queue.json` (open nodes), `logs/controller-events.jsonl` (`autopilot`, `auto_retry`, `dispatch_deferred`) |
| 완료 (done) | report milestones (`job-events.jsonl` terminal successes); `progression.ROADMAP` matched against jobs/queue: done / active / queued / blocked / ready / not started |
| 최근 활동 | last 40 `action_intent` rows of `job-events.jsonl` with their result, report's repeated-action top 3, `open_incidents` of the controller state, last controller errors |
| 레이아웃 | `site-map.json` + `site-plan.json` drawn by `site.planrender.render_dashboard_svg` (map, zones, corridors, rail, bus lanes, allocations) with the `pcb-board.json` blocks' `reserved_area` on top; a placeholder until `SiteService` wrote the plan |

## Fleet runs (N maps in parallel, one report)

```
python -B tools/fleet.py start --count 8 --minutes 30 [--seeds random | a,b,c] [--name NAME]
python -B tools/fleet.py status [--name NAME]          # one line per run
python -B tools/fleet.py report [--name NAME] [--window 0-30] [--json]
python -B tools/fleet.py stop   [--name NAME]          # lifecycle safe stop + server /quit per run
python -B tools/fleet.py cost                          # machine and per-run estimate
```

`start` makes `runtime/v2/<name>-<seed>` per run (refuses existing folders),
allocates free port pairs from 34400/27400 up (skipping every port in a
`run.json`, a fleet manifest or `active-runtime.json`), generates the maps one
at a time, starts up to `--parallel` (4) runs at once with `--stagger` (10 s),
and writes `runtime/fleets/<name>.json` (seeds, ports, pids with creation
times, status). It never changes `runtime/active-runtime.json`, so
`lifecycle`, the BAT menu and the dashboard stay on the interactive map; the
dashboard can still look at one fleet run with `--runtime runtime/v2/<run> --port 8766`.
`--minutes M` starts a detached watcher (`fleet.py watch`, log
`runtime/fleets/<name>-watch.log`) that safe-stops each run once its game tick
passes M minutes; `status`/`report` do the same check when the watcher is gone.
Stopped runs keep their folders; nothing is deleted.

Each run is what `lifecycle start --no-gui` runs: a dedicated server, the
run's own automation client (`agent-client/`, window hidden, very-low graphics,
no audio) and a `--planner none` supervisor (`Lifecycle._start(select=False)`).
The client is required: the actor is the server-created character bound to the
client's connected player (`deterministic_character.BIND_CRAFTING_PLAYER_LUA`);
hand crafts must run through a player for the engine's research trigger
(`deterministic_bootstrap`: "engine crafting requires an associated player"),
and `supervisor.actor_binding` refuses a character without its pinned player.
Factorio has no headless client, so "no GUI" means one hidden client per run.

Cost measured on this machine (Ryzen 9 7950X3D, 32 logical cpus, 64 GB):
a fresh run is server ~350 MB + client ~690 MB + supervisor ~50 MB, ~0.4
cores in steady state (the client's start-up asset load briefly takes ~4
cores); the 90-minute interactive map uses ~1.2 GB and ~1.5 cores. `start`
uses the larger of the last measured fleet run (`runtime/fleets/cost.json`,
written by `status`) and 1500 MB / 1.5 cores, keeps 6 GB and 4 cpus free, and
refuses more runs than fit (`--force` overrides): with ~36 GB free that is
about 18, so 16 runs fit next to the interactive map.

`report` prints one table: per run the game minute each key step succeeded
(power, automation, electric-mining-drill, logistics, the three outposts,
smelting district, main bus, red science), the furthest roadmap step, lab idle
seconds and actor seconds critical/slack in the window (`tools/critical_path.py`);
then min/median/max per column, how many runs reached it, blocked/failed job
reasons across runs (numbers normalized; count, example seed) and controller
errors (`tools/report_run.py`). 16 runs fit in about 35 lines; `--json` has
the full rows.

## Capability and subsystem reference

Module-level notes. The modules they describe exist and match on spot checks;
many still describe development-map (fixed) coordinates, which is the current
behaviour of those controllers, not a rule to copy.

- Startup and research: BOOTSTRAP_RESEARCH, BOOTSTRAP_RESEARCH_RECOVERY,
  PAID_BURNER_SEED, STARTER_COAL_POLICY, SCIENCE_PIPELINE, NATIVE_CAPABILITY_GATES.
- Mining, smelting, transport: MINING_EXPANSION, RESOURCE_POLICY, BELT_LANES,
  MATERIAL_OUTPUT_ACCESS, ORE_TRANSPORT_REROUTE, STEAM_COAL_REROUTE,
  BASE_COAL_RECOVERY, SHARED_COAL_TAP, CHEMISTRY_COAL, FURNACE_UPGRADE,
  RESERVED_CORRIDORS, ROUTING_FORWARD_JOG, ROUTING_RETURN_JOG,
  TRANSPORT_PIPE_CROSSINGS.
- Bus and production: MAIN_BUS, BUS_TAPS, GREEN_BUS_REROUTE, COMPACT_GREEN,
  CONSTRUCTION_MALL, INDUSTRIAL, STEEL, STEEL_CAPACITY, BRICKS, ENGINES,
  ENGINE_CAPACITY, ADVANCED_CIRCUITS, CHEMICAL_SCIENCE.
- Fluids: OIL_GATHERING, PETROLEUM, CHEMICAL_PRODUCTION, GAS_DISTRIBUTION,
  WATER, WATER_DISTRIBUTION (status line "intended to be" is stale: the
  capability is wired), FLUID_HANDOFFS.
- Defense and repair: DEFENSE_POLICY, DEFENSE_DEPLOYMENT, DEFENSE_HARDENING,
  DEFENSE_RELOCATION, BASTION_MAINTENANCE, NEARBY_THREATS, NEST_SWEEP,
  NEST_ASSAULT (retired capability), RADAR, RADAR_COVERAGE, AMMO,
  PIERCING_AMMO, INFRASTRUCTURE_REPAIR, REGISTERED_BELT_REPAIR.
- Actor: ACTOR_RECOVERY, CORPSE_RECOVERY.
- External: REFERENCE_SOURCES (blueprint sources, no code claims).

## Historical (may be wrong)

Kept unedited for history. Do not follow them without checking the code.

| Document | What is no longer true |
|---|---|
| PLAYER_API.md | "one active job per world" (now up to 4, `run --all`); capability table misses `prepare_components`, `radar_coverage`, `steel_capacity`, `engine_capacity`, `produce` |
| PROGRESSION_POLICY.md | describes Luna choosing routine work; routine order is now `progression.ROADMAP` + planners |
| MINING_LAYOUT_GUIDE.md | "EAST-only execution"; banks now exit toward the consumer (`mining_site.py`) |
| DEFENSE.md | fixed sector posts only; adaptive policy, radar and walls now exist |
| DEFENSE_WATCH.md | "one writer, stop the worker before another goal"; jobs now interleave |
| DEPLETED_DRILL_RETIREMENT.md | claims runtime wiring of `retire_depleted_drill`; the class is never constructed |
| MAIN_BUS_EXPANSION.md | unimplemented plan (`source_upgrades.py`, `BulkCircuitProduction` do not exist) |
| WORKSPACE.md | repository housekeeping snapshot from before the restructure |
