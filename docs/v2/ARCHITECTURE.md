# V2 architecture (current code)

Package `src/factorio_agent`, Factorio Space Age 2.1.20 over RCON. Every
statement below names the file that implements it; if code and this file
disagree, the code wins and this file must be fixed.

## Runtime loop

```
Factorio.bat / tools/windows/*.bat
  -> python -m factorio_agent.lifecycle start|stop|new-map|status|logs|gui|shutdown   (lifecycle.py)
       spawns: python -m factorio_agent.supervisor --runtime ABS --planner none|luna  (automation_config.json)
Supervisor.run (supervisor.py) loops _cycle():
  1 _snapshot        observe + status + capabilities + actor-status; world/rollback guard
  2 _resume_known    resume jobs the last safe stop paused
  3 _autopilot       queue < 2 pending/ready -> progression.autopilot_nodes (<= 3 "auto_" nodes)
  4 _auto_retry      retry blocked jobs once another job succeeded (<= 3 per job)
  5 _poll_decision   accept a finished model decision (planner luna only)
  6 _check_incidents incidents.IncidentTracker opens/closes episodes; luna: maybe start one model call
  7 _dispatch        submit ready nodes by priority while < 4 jobs active   (jobs.submit)
  8 _run_slice       "run --all --seconds 25" -> jobs.run_jobs
Commands go through kernel.InProcessBridge -> kernel.Session -> __main__.execute (one RCON socket).
jobs.run_jobs: per pass one observe; jobs in priority order; StarterExecutors.step(capability)
  -> shared prelude, then the capability's handler (capability_registry.handler_for)
  -> action dict: durable pending_action, game.act, receipt -> pass ends (fresh observe next)
  -> status dict: recorded; next job gets the same observation
  pass without any action -> StarterExecutors.idle_action (research lane "lane:research")
```

## Module map

| Area | Module | Responsibility |
|---|---|---|
| Entry | `lifecycle.py` | start/stop/new-map/status/logs/shutdown; process identity; `runtime/active-runtime.json` pointer |
| Entry | `supervisor.py` | cycle above; `WorkQueue` (`work_queue.json`); auto-retry; incident logging; model calls |
| Entry | `__main__.py` | `execute(argv)`: typed commands (`observe`, `request`, `run`, `pause`, `retry`, ...); builds the observe receipt |
| Entry | `kernel/session.py` | `Session` (cached game + catalog, 2 s observe reuse, profiler), `InProcessBridge` |
| Entry | `automation_config.py` | validated `automation_config.json`: world seed/settings, `controller.planner` |
| Jobs | `jobs.py` | `CAPABILITIES`, `Request.validate`, `Ledger`, `submit`/`control`, `run_jobs`/`run_slice`; re-exports `StarterExecutors` |
| Jobs | `executors.py` | `StarterExecutors`: controller construction, lazy factories, upkeep helpers, `step`/`proof`/`record_action` dispatch, `idle_action` |
| Jobs | `capability_registry.py`, `handlers/` | one `Handler` (step, proof, validate, record_action) per capability, grouped by domain; `handlers/upkeep.py` shared upkeep |
| Plan | `progression.py` | `ROADMAP` (Step list), `autopilot_nodes`, `capacity_nodes`, `produce_nodes`, `lookahead_demand`, `bulk_item_demand` |
| Plan | `early_start.py` | early start ("pre-build"): `CAPABILITIES[c]["early_start"]`, inputs checks (receipt `early_start_inputs`), `early_ready`, admission requires, `Deferral` for `ensure_plan(defer=)`, early clock |
| Plan | `capacity_planning.py` | `plan_capacity`: starter cells, wait vs expand by time |
| Plan | `research_capacity.py` | labs + science assemblers sized together; `research_capacity_receipt`, `recommended_science_rate` |
| Plan | `science_growth.py` | staged red science block: start size, reserved slots, evidence growth rule (`science_growth` capability) |
| Plan | `research_policy.py` | `choose_research` from catalog data (idle research lane) |
| Plan | `incidents.py` | incident kinds, remedy menus, incident documents, `check_decision` |
| Plan | `luna_client.py` | `LunaDecisionClient.decide(document)` via the Codex CLI (planner luna only) |
| Game | `core/rcon.py`, `core/deterministic_game.py` | RCON client; `query`/`observe`/`act` (all world effects are Lua bodies in `act`) |
| Game | `runtime.py` | `create`/`start`/`game_for`/`catalog_for`; `WorldBoundGame`; `NETWORK_SEGMENT_SETTINGS` |
| Game | `core/deterministic_builder.py` | `_plan(entities, ports)`, `FactoryBuilder.ensure_plan` (one action per call), coal sites |
| Game | `power_layout.py` | `V2FactoryBuilder.ensure_plan` = resource policy check + base `ensure_plan`; `RegularizePower` |
| Game | `core/deterministic_bootstrap.py`, `construction_supply.py` | starter cells; `V2Bootstrap`: fuel/stock batching, hand-craft cap |
| Game | `core/world_catalog.py` | live recipes/entities/technologies (`catalog.json`) |
| Space | `site/service.py` | `SiteService`: one survey + master plan per runtime, allocation of district blocks |
| Space | `site/worldmap.py`, `site/survey.py` | tile map; bulk read-only survey |
| Space | `site/masterplan.py`, `site/planbuild.py`, `site/orientation.py`, `site/planrail.py` | bus, zones, feeder corridors, entry/exit sides, rail reservations |
| Space | `site/allocator.py`, `site/placement.py`, `site/fit.py`, `site/transform.py` | blocks along the bus; orient a template into a block (rotate/mirror) |
| Space | `site/candidates.py`, `site/render.py`, `site/planrender.py`, `site/fakeworld.py`, `site/fakegame.py` | free sites; SVG; synthetic maps and a lupa survey fake for tests |
| Space | `smelting_site.py`, `mining_site.py` | smelting district on the plan zone; mining bank exit side from the plan |
| Space | `production_rows.py`, `production_layout.py`, `production_site.py`, `production_sources.py` | `produce` capability: design, place, route, power, preflight, stock chest |
| Space | `supply_cell.py` | `supply_cell` capability: temporary hand-fed assembler cell (belts, ...) before the bus; recipe tree -> cell, ring site, feeding upkeep, `machine_stock` hook (`chain_stock`), planner `supply_cell_nodes`, retirement by mining (PLANNERS.md) |
| Space | `planned_bus.py` | `planned_main_bus`: iron/copper trunks on the plan's bus lanes, fed by a splitter beside each district plate port; publishes `board["shared_main_bus"]` (template `planned_main_bus`) |
| Space | `planned_tap.py` | taps on the planned bus (any direction, either side): splitter + branch to a port outside the band, built trunks dive, reserved lanes tunnelled, trunk extension; planned with the consumer's block (`connect_inputs`), built first (`cutover`), supply rule `tap_budget` |
| Space | `plate_supply.py` | plate resolver for science: bus taps when a planned bus is published (`plate_bus`, `bus_budget`), else the district's registered output ports (old `smelter-lanes.json` only as a same-world fallback), never a port another owner took |
| Space | `blocks/` | recipe rows: `sizing`, `row`, `derate`, `audit`, `stock_tail`, ASCII `render` |
| Space | `planning.py` | `preflight` (whole-block native check), `survey_slots` |
| Space | `transport.py`, `routing.py`, `power_grid.py`, `manufacturing.py` | belt corridors (`plan_connection`; the site plan's bus band is hard for every route but the bus feed), trunks, pole plans, `connect_power_groups` |
| Space | `board.py` | `pcb-board.json` block registry; startup science cell |
| Space | `resource_policy.py` | ore keepouts; only annotated mining entities may stand on ore |
| Tools | `tools/check.ps1`, `tools/report_run.py`, `tools/luna_incident_replay.py`, `tools/dashboard.py` | lint + tests; run report; offline incident replay; read-only web dashboard (docs/v2/README.md) |

All other top-level modules are capability controllers constructed in
`StarterExecutors.__init__` (`executors.py:38`).

## Runtime directory files (`runtime/v2/<name>/`)

| File | Writer | Content |
|---|---|---|
| `run.json`, `world-settings.json` | `runtime.create`, `runtime.start` | ports, seed, rcon password; `world_id` |
| `jobs.json` | `jobs.Ledger.save` (under `writer.lock`) | jobs: status, reason, evidence, pending_action, priority, timings |
| `job-events.jsonl` | `jobs.Ledger.event` | submitted, action_intent, action_result, terminal, pause/resume/retry |
| `work_queue.json` | `supervisor.WorkQueue.save` | queue nodes (id, capability, parameters, priority, depends_on, status, job_key) |
| `pcb-board.json` | `board.Board` and each controller's publish step | registered blocks, ports, connections, shared bus |
| `site-map.json`, `site-plan.json` | `site.service.SiteService.save` | surveyed map with reservations; master plan + allocations |
| `catalog.json` | `runtime.catalog_for` | world catalog |
| `rate-samples.json` | `diagnostics.production_rates` (every CLI `observe`) | production rate samples |
| `logs/controller-events.jsonl` | `Supervisor._event` | cycle events: slice, autopilot, auto_retry, incident_*, call, error |
| `logs/controller-state.json` | `Supervisor._save` | status, active jobs, open incidents, resume keys |
| `logs/incidents.jsonl` | `Supervisor._record_incident` | incident documents (also with planner none) |
| `logs/actions.jsonl` | `DeterministicGame._record_action` | every act and its receipt |
| `logs/profile-run.txt` | `kernel.Session._write_profile` | only with `FACTORIO_AGENT_PROFILE=1` |
| controller state, e.g. `factory-builder.json`, `smelting-district.json`, `mining-outposts.json`, `automation-science.json`, `production-row-<item>.json`, `startup-science-capacity.json` | the controller | its plan, phase, world_id |
| `stop.json` / `supervisor-stop.json` | `stop-worker` command / lifecycle stop | stop markers; `writer.lock`, `controller.lock` are `RunLock` files |

## Decision layers (in cycle order)

| Layer | Code | May | May not |
|---|---|---|---|
| Roadmap autopilot | `progression.autopilot_nodes` via `Supervisor._autopilot` | add up to 3 `auto_<step>` nodes for ready steps, priority `-100 - index`; a step with `early_start` once its inputs are planned (`early_start.early_ready`, PLANNERS.md "Early start") | propose a step whose job is blocked/failed; touch the world |
| Capacity planner | `progression.capacity_nodes` -> `capacity_planning.plan_capacity` | add `starter_capacity` nodes at priority -50 | expand without measured production and known cell cost; before `starter` succeeded; after that resource's `mining_outpost` succeeded |
| Produce planner | `progression.produce_nodes` | add `produce` nodes at priority -51 | propose without positive measured plate net rate |
| Supply cell planner | `supply_cell.supply_cell_nodes` (from `autopilot_nodes`) | add one `supply_cell` build node (-52) and later one retire node (-1000) | propose twice per map; retire while demand remains and no `produce` row took over |
| Research planners | `research_capacity` (observe receipt, `industry.py` startup cell), `research_policy.choose_research` (`StarterExecutors.idle_action`) | size science rate/labs; start lab research when no job acts | start research while a job owns research or any job has an unresolved world effect |
| Auto-retry | `Supervisor._auto_retry` | `retry` a blocked job after a new success, <= 3 times | retry unknown outcomes, rollbacks, deadlines |
| Incidents | `incidents.IncidentTracker` | open `empty_queue`, `blocked_job`, `no_progress`, `planner_review` | act; planner none only logs |
| Model (luna) | `luna_client` + `Supervisor._poll_decision` | one menu remedy: retry/pause a task, add <= 3 nodes, wait | send coordinates, Lua or entities (`supervisor.FORBIDDEN`); gate dispatch; retry unknown outcomes |

Inside a job, `StarterExecutors.step` first runs shared upkeep before the
capability's own controller: `pending_repair` and `_approved_initial_defense`
(`executors.py:734-751`); most handlers then call `handlers.upkeep.shared_upkeep`
(starter/fuel upkeep, background construction, upgrades). So an
action in a job's events may belong to upkeep, not to the capability.

## Safety invariants

1. No granted items or research. All effects are Lua bodies in
   `DeterministicGame.act` (`core/deterministic_game.py:527`): `build` removes
   the item from the actor before `create_entity`; `insert`/`take` move between
   inventories; `research` uses `add_research` after prerequisites and refuses
   trigger technologies. The freeplay inventory is inserted once in `initialize`.
   Nothing checks this automatically: review every new Lua body for it.
2. Durable intent, no blind retry. `_advance_job` (`jobs.py:693`) stores
   `pending_action` before `game.act`; an exception or a reason in
   `UNKNOWN_BATCH_OUTCOMES` leaves the job blocked with
   `action_outcome_unknown_reconcile_required`. `control("retry")` refuses it,
   only `reconcile` clears it (`jobs.py:513-588`); `DeterministicGame._execute`
   never resends a command (`deterministic_game.py:401`).
3. World identity. `WorldBoundGame.query` prefixes every Lua body with a
   `world_id` check (`runtime.py:43`); `Ledger.sync` raises on a changed world
   (`jobs.py:369`); `Supervisor._world_guard` / `_snapshot` (`supervisor.py:809,861`);
   controller state and `SiteService` caches carry `world_id`.
4. Rollback detection. A tick below `last_tick` blocks every job with
   `world_rollback_requires_reconciliation` and records `rollback_tick`
   (`jobs.py:373`); `submit` and `Supervisor._snapshot` then refuse to continue.
5. Resource protection. `V2FactoryBuilder.ensure_plan` runs
   `resource_policy.ensure_resource_policy` before any build (`power_layout.py:295`);
   `planning.preflight` rejects resource violations; only entities annotated by
   `mark_mining_core` / `annotate_mining_exit` may stand on ore; `mine` refuses
   crash-site artifacts.
6. One research-owning job. `jobs._admission_error` with `RESEARCH_CAPABILITIES`
   (`jobs.py:428`); `idle_action` yields to any active job taking a `technology`.
7. Admission limits (`jobs.py:416-486`, `710-761`): at most `MAX_ACTIVE_JOBS = 4`
   active, one per capability; request observation at most 3600 ticks old;
   `requires` capabilities succeeded (`early_start.requires` for an early-start
   capability, which then never takes the last free slot); job deadline 108000
   ticks or 3600 s, not counting early time (`early_ticks`/`early_seconds`);
   blocked after 18000 ticks without a `progress_key` change or 3 identical
   failures; slices at most 50 s.
8. Batching (`construction_supply.py:136-148`): fuel in batches of `FUEL_BATCH = 8`
   for every job; no fuel for burners with full output or `FUEL_SKIP_STOCK = 50`
   finished items; stock collected in `take_many` batches of `COLLECT_BATCH = 10`;
   bootstrap science carried in `SCIENCE_COURIER_BATCH = 5` per lab (`industry.py:33`).
9. Hand-craft cap. Above `HANDCRAFT_LIMIT = 10` missing items with a producing
   `produce` row or supply cell, `V2Bootstrap.ensure_item` collects or waits instead of crafting
   (`construction_supply.py`, `machine_stock.py`, `supply_cell.SupplyCell.stock`).
10. One writer. `submit`, `control`, `run_*` hold `writer.lock`; the supervisor
    holds `controller.lock` (`core/deterministic_state.RunLock`).
