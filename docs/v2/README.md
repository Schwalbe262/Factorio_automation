# docs/v2 index

## Maintainer guides (current, checked against the code on 2026-11-14)

State of the project and open work: `HANDOFF_CURRENT.md` and `HANDOFF_DETAIL.md` in the repository root.

| Document | Use it for |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | runtime loop, module map, runtime files, decision layers, safety invariants |
| [ADDING_A_CAPABILITY.md](ADDING_A_CAPABILITY.md) | controller contract, registration, site, routing, state, roadmap, tests |
| [DEBUGGING_A_STALL.md](DEBUGGING_A_STALL.md) | report, ledger, read-only live inspection, known stall patterns, fix rules |
| [PLANNERS.md](PLANNERS.md) | planner formulas, defaults, tuning, pinning tests |
| [BALANCERS.md](BALANCERS.md) | belt balancer concepts (output/input balanced, throughput unlimited, lane balance) and where one is or is not needed |
| [LAYOUT_PRINCIPLES.md](LAYOUT_PRINCIPLES.md) | block layout rules (direct insertion vs belt, compactness, growth) and the code/audit enforcing each |
| [SUPERVISOR.md](SUPERVISOR.md) | supervisor flags, planner none/luna, incident replay |
| [LIFECYCLE.md](LIFECYCLE.md) | BAT menu and `lifecycle` commands; parallel measurement runs: "Fleet runs" below |
| [AUTOMATION_CONFIG.md](AUTOMATION_CONFIG.md) | `automation_config.json` fields and ranges |
| [HEADLESS.md](HEADLESS.md) | probe: what works without a connected client, why red science needs one |
| [ROCKET_LAUNCH.md](ROCKET_LAUNCH.md) | rocket_silo / rocket_parts / rocket_launch, the 2.1 Space Age launch API facts, why `create_space_platform` is no grant |

## Dashboard (read-only run view)

```
python -B tools/dashboard.py [--runtime runtime/v2/<name>] [--port 8765] [--host 0.0.0.0]
```

Opens a server on `http://127.0.0.1:8765/` (loopback by default; `--host 0.0.0.0`
serves the local network; Ctrl+C stops it). Default runtime: the `runtime` of
`runtime/active-runtime.json`, the one `lifecycle status` shows; without
`--runtime` the page follows that file when a new map becomes active. It only reads files in the runtime folder: no RCON,
no locks, no writes. The page polls `/api/state` every 5 s; JSONL logs are
read incrementally and a missing or half-written JSON keeps its last good value.

| Panel | Reads |
|---|---|
| Header | `run.json` (seed), `tools/report_run.report` (wall/game minutes, planner, idle-gap share), `logs/controller-state.json` + `supervisor-process.json` (process check by pid + creation time, else freshness of the last controller event), `lifecycle-pause.json` |
| 진행 중 (now) | `jobs.json` (every non-succeeded job: status, reason, actions, age, evidence digest of blocked jobs), `work_queue.json` (open nodes), `logs/controller-events.jsonl` (`autopilot`, `auto_retry`, `dispatch_deferred`) |
| 완료 (done) | report milestones (`job-events.jsonl` terminal successes); `progression.ROADMAP` matched against jobs/queue: done / active / queued / blocked / ready / not started |
| 최근 활동 | last 40 `action_intent` rows of `job-events.jsonl` with their result, report's repeated-action top 3, `open_incidents` of the controller state, last controller errors |
| 레이아웃 | `site-map.json` + `site-plan.json` drawn by `site.planrender.render_dashboard_svg` (map, zones, corridors, rail, bus lanes, allocations) with the `pcb-board.json` blocks' `reserved_area` on top and the board's built belts/undergrounds as tile cells (`belt_layer`: dark inside the block's reservation, red/orange outside, blue for a block without one; one `<path>` per kind and state); a placeholder until `SiteService` wrote the plan |

Tab `병렬 실행` (`#fleets`, endpoint `/api/fleets`): every `<base>/fleets/*.json`
manifest newest first (commit from the manifest or `fleets/reports/<name>.report.txt`,
target, planner, client / TEST MODE, running/stopped by process check, one health
line); per fleet one row per seed with server/supervisor/client state, game minute,
the milestone strip of `fleet.py report` (+ green science when a run has it), furthest
step, active/blocked jobs and the current blocked reason, then min/median/max and
reached N/M; blocked-reason histograms (`fleet.aggregate`) per fleet and over the last
5 fleets; a round-to-round median table with sparkline for name series such as `auto-*`.
Per run it re-reads only changed files (job-events incrementally), ~0.1 s per refresh
with 19 fleets / ~140 runs. A seed links to `/?runtime=<folder>#run`: `/api/state` and
`/api/layout.svg` accept `?runtime=` for a strict folder name under `<base>/v2`.
`--base` (default `runtime`) sets where fleets and runs are read.

## Fleet runs (N maps in parallel, one report)

```
python -B tools/fleet.py start --count 8 --minutes 30 [--seeds random | a,b,c] [--name NAME] [--client none|always|until_lab_trigger]
python -B tools/fleet.py status [--name NAME]          # one line per run
python -B tools/fleet.py report [--name NAME] [--window 0-30] [--json]
python -B tools/fleet.py stop   [--name NAME]          # lifecycle safe stop + server /quit per run
python -B tools/fleet.py cost                          # machine and per-run estimate
python -B tools/fleet.py inspect|revive|postmortem --name NAME --seed SEED   # look into one stopped run
python -B tools/waste_report.py NAME                   # ranked lost time, critical chain, plate bound
python -B tools/rocket_plan.py runtime/v2/<run>        # bill to the rocket and each item's producer
```

More `start` options: `--explore` (layout variants per run), `--keep-problem` / `--keep-timeout`
(leave problem runs up for inspection), `--no-postmortem`, `--stall-minutes`, `--respawn` (client-free
respawn emulation, OFF by default, not approved by the owner), `--base` (runtime base folder; fleets
started from a snapshot worktree pass the main checkout's `runtime`). A stopped problem run gets a
post-mortem under `<run>/postmortem`; `report` lists pace against the 20-minute target, idle with work,
actor deaths and problem runs with their cause.

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

Checkpoint branches (mid-game probes in tens of minutes):
`python -B tools/fleet.py start --from FLEET:SEED[,FLEET:SEED...] --minutes M [--name NAME]`
continues stopped client-free runs with the current code. Each source must
have no live server/supervisor/client, a safe-stop pause marker
(`lifecycle-pause.json` phase `paused`) and a save written after it, and must
not have stopped on an actor death (otherwise refused: the state could be newer
than the save, a tick rollback). Its folder is copied to
`runtime/v2/<name>-<seed>` without process records, locks, `controller-config.json`,
`agent-client/`, `postmortem/` and `vanilla/server-data`; `run.json` gets the
new root, fresh ports, a new RCON password and `branched_from` (fleet, seed,
tick, minute, source commit, world id). The source is only read. The world
identity is unchanged (`storage` in the save and `world-settings.json` agree), so
every query stays world-bound; the ordinary `lifecycle` start resumes the
paused world and the `--planner none` supervisor resumes `jobs.json` as is:
unknown outcomes stay `pending_action` and go through `reconcile`; blocked jobs
are re-admitted only by the supervisor's own auto-retry/replan (its retry
counters are in memory, so a new supervisor starts them afresh). `--minutes M`
counts game minutes after the checkpoint tick (per-run `target_tick`); milestone
minutes stay absolute game time. Branch runs are TEST runs, never proof;
`status`/`report` headers say `BRANCHED from checkpoints fleet:seed@minute`.

`start --client none|always|until_lab_trigger` (default `none`;
`--keep-client` = `always`) picks the actor client mode of every run
(`actor-client.json`, `factorio_agent.actor_client`, [HEADLESS.md](HEADLESS.md)):

- `none` (default, TEST runs): only the dedicated server and a `--planner none`
  supervisor. The server-created character has no player; `connect-player`
  adopts it (`adopt_playerless_actor`). The written test-mode exception in
  AGENTS.md reproduces what a connected player would cause: (a) after the
  real lab craft, `automation-science-pack` is script-credited
  (`credit_hand_craft_trigger`); (b) `emulate_player_presence` charts the
  freeplay start area and the actor's surroundings and generates chunks
  around it at a player's radius. `run.json`, `report_run.py`, `fleet.py
  status/report` and the dashboard header say `TEST MODE client_free`.
  Equivalent to `always` within noise through the smelting district on seed
  1193005910 over 35 game minutes (HEADLESS.md "Equivalence").
- `always` (proof runs, and what `lifecycle start --no-gui` runs): the run's
  own automation client too (`agent-client/`, window hidden, very-low
  graphics, no audio), bound to the actor
  (`deterministic_character.BIND_CRAFTING_PLAYER_LUA`).
- `until_lab_trigger`: `always` until the lab trigger fired, then the
  supervisor detaches the actor (`detach-client`: spectator controller, client
  closed by pid + creation time) and the run continues server-only.

`status` shows the client column as `o` (live), `n` (none by design) or `d`
(detached); `report` has a `cli` column and a TEST MODE header note plus the
credited triggers with their ticks.

Cost measured on this machine (Ryzen 9 7950X3D, 32 logical cpus, 64 GB):
a fresh run is server ~350 MB + client ~690 MB + supervisor ~50 MB, ~0.4
cores in steady state (the client's start-up asset load briefly takes ~4
cores); the 90-minute interactive map uses ~1.2 GB and ~1.5 cores. A
client-free run (`--client none`) measured 420 MB (server 371 + supervisor 49)
and 0.19 cores at game minute ~6, against 1367 MB and 0.67 cores for the same
seed with its hidden client (client 941 MB); `start` still budgets with the
larger measured/default cost. `start`
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

Stall and tidiness columns (also per run in `report_run.py`, there with a per-block
table): `last` = game minute of the last terminal job success, `dry` = the longest span
without any success, `drynow` = game time since the last success at the end of the run
(`report_run.progress_stall`, from `job-events.jsonl`); a run with `drynow` above
`report_run.STALL_DRY_MINUTES` (30) is `stalled`, counted in the header line and listed
under "stalled runs" with the reasons of its blocked jobs at the end (not the watcher's
tick stall). `tidy` = share of the board's built belts outside their block's
`reserved_area` (`factorio_agent.layout_tidiness.board_tidiness(board, site_plan)`:
data in, data out, so a planner sweep can score planned boards the same way; per block
belts / outside / undergrounds / splitters / curves, plus belts outside every reservation
and plan area, and the board's connections with the longest).

## Offline planner sweep (no game, many seeds)

`python -B tools/plan_sweep.py --seeds 0-299` runs the pure planners on synthetic
worlds (`site/fakeworld.make_world(seed, size=512)`) and prints one report: failure
rate per stage, top reasons (count, example seed), master-plan warnings, tidiness
medians per block and per seed, queries the offline game could not answer, and the
stages that need the game. `--json` prints the summary and every row; `--upto GROUP`
stops early (e.g. `--upto smelting_district`); `--seed S --render [--out DIR]` writes
one seed's plan as SVG (`site/planrender`) plus its row and runtime files.

Stages (`factorio_agent/plan_sweep.py`, in roadmap order): `masterplan`
(`plan_site` + `validate`), `mine:<ore>` (`mine_layout.choose_bank` on the planned
patch and exit edge), `smelting_district` (the real `SmeltingDistrict.prepare`
including its ore and coal feed routes from the bank ports), `main_bus`
(`PlannedBus.prepare`), `automation_science` / `logistic_science` (the real
controllers on bus taps, as `tests/test_v2_green_site.World` drives them, with the
real catalog `tests/fixtures/catalog_eval_1575138435.json.gz`), then `block:<core>`
allocations (`SiteService.allocate_candidates`) of steel, brick, petroleum, plastic,
advanced petroleum, advanced circuits, sulfur, blue science and the rocket silo,
footprints probed from their real builders. `OfflineGame` answers only read-only
native checks from the `WorldMap` (route corridor survey, `survey_slots`,
`preflight`, `can_place`, underground span, site identity); power poles are stubbed
ok. A later stage is `skipped: <stage> failed` when its input failed.

Tidiness: belts per block's plan and how many lie outside its reserved area (district:
also its feeder corridors and mining zones; bus: band and header corridor; science:
the allocation), route length / turns / longest feed from `route_metrics`
(`routing.metrics`), underground pairs, planned corridor crossings
(`site.corridors.count_crossings`).

Determinism and cost: same seeds, same rows. The router is the whole cost (hard
seeds spend minutes of CPU in a failing district search), so each seed has a router
work budget counted in `routing.clear_skeleton` calls (`--route-budget`, default
1.5 M, about 45 s); past it the route fails `sweep_route_budget_exhausted` (a sweep
limit, not a planner verdict). Workers default to `min(8, cpus // 4)` and are capped
at 8: live fleets and the GUI run share the machine. Measured on the 32-cpu machine:
300 seeds in 19 minutes with 8 workers (median seed 16 s, slowest 172 s); the rows
are identical at any worker count. Gate: `tests/test_v2_plan_sweep.py`
(one full seed, three early seeds, failure counts pinned as upper bounds; lower the
bound when a fix removes a failure).

Not offline-capable (listed in every report): the mining bank build
(`mining_layout_policy.prepare_new_bank`, native patch survey and exit prefix), power
poles, steam expansion (shore sites), planned steel/brick furnaces (need built banks'
material ports), oil gathering, defense.

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
  capability is wired), FLUID_HANDOFFS, LATE_FLUIDS (advanced petroleum with
  cracking, acid/lubricant, fluid headers, the tail's supply rule), ADVANCED_PETROLEUM.
- Defense and repair: DEFENSE_POLICY, DEFENSE_DEPLOYMENT, DEFENSE_HARDENING,
  DEFENSE_RELOCATION, BASTION_MAINTENANCE, NEARBY_THREATS, NEST_SWEEP,
  NEST_ASSAULT (retired capability), RADAR, RADAR_COVERAGE, AMMO,
  PIERCING_AMMO, INFRASTRUCTURE_REPAIR, REGISTERED_BELT_REPAIR.
- Actor: ACTOR_RECOVERY, CORPSE_RECOVERY.
- Described only in ARCHITECTURE.md so far (no module note): self-recovery (`plan_needs`, `remedy`,
  `bottleneck_audit`, `growth_providers`), `remote_smelting` (second plate supply on the next patch),
  `bus_lane` (a new lane on a built bus), `route_tap` sources, `built_block_repair` and
  `pole_relocation` (the clear rung), `rocket_plan` / `rocket_stages` / `rocket_feed`.
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
