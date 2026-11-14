# Debugging a stall

A stall is "no job succeeded for a long time", not "few actions". A run can
be busy and stuck. Work read-only on the live runtime; fix rules in code.

## 1. Report

```powershell
python -B tools/report_run.py runtime/v2/<name>            # text
python -B tools/report_run.py runtime/v2/<name> --json     # full data
python -B tools/waste_report.py runtime/v2/<name>          # wasted game seconds by type (or a fleet name)
```

`waste_report` ends with the critical chain to the 20-minute target (`critical_chain`: power ->
research (drill + logistics) -> outposts -> district producer (its first bank's plan prefix) ->
district -> bus, each segment's median end and the constraint that bound it: `labs: 1 lab`,
`pack supply / funding`, `logistics research`, `waits for <entity>`, `plates` = half the iron takes
came back short). `fleet.py report` prints `pace vs target 20 min` (district and bus median minus
20, runs under it).

Read in this order:

| Field | Meaning | Stall sign |
|---|---|---|
| `milestones` | minute + capability of every succeeded job | long gap after the last row |
| `minutes_since_last_success` | wall minutes since the last milestone | > 10 |
| `recent_100_actions_top`, `recent_repeat_share` | top repeated actions of the last 100 intents (`incidents.repeat_summary`) | share >= 0.5: a loop |
| `per_job`, `terminal_results` | status/reason/actions per job | same `blocked` reason repeated |
| `idle_gap_share`, `longest_gaps_seconds` | wall time with no action > 30 s | nothing acts |
| `planning_per_execution`, `kernel_totals` | Python planning vs action time; top RCON queries | planner is the bottleneck |
| `incidents_by_kind`, `incidents` | episodes, remedy, outcome | `no_progress`, unresolved `blocked_job` |

## 2. Ledger and queue

- `jobs.json` -> each job's `status`, `reason`, `evidence`, `pending_action`,
  `progress_at_tick`. A job with `pending_action` or
  `action_outcome_unknown_reconcile_required` needs `reconcile`, never `retry`.
- `work_queue.json` -> nodes `status` (`blocked` with `last_reason` /
  `dispatch_blocked` means submit was refused).
- `python -B -m factorio_agent.lifecycle status` -> current task, diagnosis,
  `seconds_without_progress`.
- Bounded reads only: `Get-Content job-events.jsonl -Tail 80`,
  `rg -n "auto_retry|incident_opened|error" logs/controller-events.jsonl | Select-Object -Last 40`.
  Never paste whole JSONL files.

## 3. Look at the live world (read-only)

```python
import sys; sys.path.insert(0, "src")
from pathlib import Path
from factorio_agent import runtime
root = Path("runtime/v2/<name>").resolve()
game = runtime.game_for(root)          # WorldBoundGame, world-checked queries
obs = game.observe()                   # pure read; never call game.act here
rows = [e for e in obs["entities"] if e["name"] == "burner-mining-drill"]
print([(e["position"], e.get("status_name"), e.get("inventory")) for e in rows])
print(game.query("return {ok=true,tick=game.tick,research=f.current_research and f.current_research.name}"))
```

`python -m factorio_agent --runtime ABS observe` gives the full receipt
(supply options, demand, research capacity) but also appends
`rate-samples.json`; prefer `game.observe()` for inspection.

Site map and plan (write output outside `runtime/`):

```python
import json
from factorio_agent.site.worldmap import WorldMap
from factorio_agent.site.masterplan import MasterPlan
from factorio_agent.site.planrender import render_plan_svg
m = json.loads((root / "site-map.json").read_text(encoding="utf-8"))
p = json.loads((root / "site-plan.json").read_text(encoding="utf-8"))
world, plan = WorldMap.from_json(json.dumps(m["map"])), MasterPlan.from_dict(p["plan"])
print(plan.summary()); print(plan.validate(world))
Path("plan.svg").write_text(render_plan_svg(world, plan), encoding="utf-8")
```

Fresh survey of an area: `site.survey.survey_area(game, Rect(x0, y0, x1, y1)).world`
(read-only queries). Saved bare map files: `python -m factorio_agent.site.render <map.json> <out.svg>`
and `python -m factorio_agent.site.masterplan <map.json> <out.svg>` (re-plans; not the
`site-map.json` wrapper). Recipe row as ASCII:
`python -m factorio_agent.blocks.row runtime/v2/<name>/catalog.json <recipe> <rate>`;
a placed plan: `blocks.render.render_ascii(catalog, state["plan"])`.

## 3b. Post-mortem of a fleet run

A fleet round does not just switch its runs off. At the target game minute the
watcher (`tools/fleet.py watch`, spawned by `start --minutes`) handles every due
run in two passes. A run whose observed game tick (`controller-state.json`
`last_observed_tick`) has not advanced for the stall limit is due as well
(`Fleet.enforce_stall`, stop reason `stalled: no game-time progress for N real
min`, manifest field `stalled`): the limit is `start --stall-minutes`, else 5x
the fleet's median real minutes per game minute, at least 10 real minutes. Its
triage leads with the last controller error (`errored: agent_dead (1
unrecovered actor death)`), so a round always completes:

1. **Pause** each due run: lifecycle safe stop (supervisor ACK, `game.tick_paused`,
   checkpoint save). The server stays up; nothing changes in the world any more.
2. **Triage** each run from its logs (`tools/fleet_triage.py`, written to
   `<run>/postmortem/triage.json`): `errored` (>= 3 controller errors, a job
   ended by `repeated_executor_failure`/`planner_fault`/unknown outcome, processes
   down), `stalled` (no action or success, no slice, or an empty queue for 8
   game min), `blocked` (a job blocked/failed for >= 3 min), `slow` (a milestone
   later than the fleet median by max(5 min, 25 %), or missing while half the
   fleet has it), else `healthy`. The waste report's `run_stalled`,
   `blocked_unretried`, `repeated_block` and `long_wait` rows are attached.
3. **Snapshot** every non-healthy run while its server is up
   (`factorio_agent.postmortem.snapshot`) into `<run>/postmortem/<stamp>-t<tick>/`:
   `observe-raw.json` (every entity), `observe.json` (the full `observe` receipt
   with the planner receipts), `layout.svg` (dashboard map), `probes/<job>.json`
   per blocked/failed job (for a stalled run: its waiting jobs; at most 4) and
   `manifest.json` (`run_files_changed` must be empty). A probe holds: the
   job's ledger row; a dry run of its planner's pure `prepare` (`DRY_RUN`) and of
   its controller `step` (an action it would take now is `would_act`, never
   sent); every evidence note (`evidence_capture`: preflight failures with
   entity and position, every route attempt with endpoints and reason, mining
   bank attempts per exit side and the candidate list with the check each
   failed, the patch-survey receipt fields, smelting candidates rejected by the
   block filters / land survey and per-candidate failure stages); the queries
   the planner sent (`probes/<job>-<mode>-queries.jsonl`); entity / resource /
   water / cliff / charted-chunk dumps of up to 6 areas around the failure
   positions, route spans and candidate areas; the entities standing on each
   failure position (`blockers`); and a one-line `cause`.
4. **Shut down** healthy runs as before. Problem runs stay `paused_problem`; when
   the round is over `settle_kept` keeps the `--keep-problem N` worst (score:
   stalled > blocked > errored > slow, then fewer milestones, longer idle) as
   `kept` and shuts the others down. A kept run is shut down after
   `--keep-timeout` minutes (default 60), by `fleet.py stop`, or by the next
   `fleet.py start` when it needs the memory (oldest held first).

Read-only by construction: the planners run against `postmortem.ReadOnlyGame`
(`act`, `save`, `initialize` refuse; a query body matching `WRITE_PATTERN`, e.g.
`create_entity`, `.insert{`, `d.x=`, `game.tick_paused=`, is refused before it is
sent and recorded as `read_only_violation`) and against a sandbox copy of the
run's JSON state inside the snapshot folder (planner state writes land there;
the probe lists them as `sandbox_writes`; the sandbox is removed). Queries stay
world-bound. The planners' wall-clock budgets are raised to 90 s for the dry
run (full sweep) and the smelting search cursor is reset in the sandbox.

Dry-run planners (`postmortem.DRY_RUN`): `mining_outpost` (`MiningOutposts.prepare`
with the drill count `step` would ask for), `planned_main_bus`
(`PlannedBus.prepare`), `smelting_district` (`SmeltingDistrict.prepare`),
`produce` (`ProductionRows.prepare`), `supply_cell` (`SupplyCell.prepare`; not for
retirements), `dedicated_materials` (`DedicatedMaterials.prepare`). Every other
capability gets the generic dry run of its registered handler `step`; adding a
planner = one `DryRun` entry with its pure `prepare`. A planner that needs a
world write to plan shows the refused write in the probe (that is what it would
need to become read-only).

Commands:

```powershell
python -B tools/fleet.py report --name <fleet>            # "problem runs": class, blocking job, cause, folder, kept ports
python -B tools/waste_report.py <fleet>                   # blocked_unretried rows carry "cause (post-mortem)"
python -B tools/fleet.py inspect --name <fleet> --seed S  # state, ports, triage, causes, read-only snippet
python -B tools/fleet.py inspect --name <fleet> --seed S --gui   # owner: visible client on a COPY of the save
python -B tools/fleet.py revive --name <fleet> --seed S   # stopped run: its server again, paused, no supervisor
python -B tools/fleet.py postmortem --name <fleet> --seed S      # a new snapshot of a kept or revived run
```

`--gui` copies the run's save to `<run>/postmortem/gui/` and starts a visible
single-player client with its own profile there (`--load-game`); the run's
server and files are untouched. Never join a run's server with a client: a
connected player changes a client-free world (HEADLESS.md). The copy loads
paused when the run was paused (`/c game.tick_paused=false` to let it run).

## 4. Profile

- Set `FACTORIO_AGENT_PROFILE=1` in the environment that starts the
  supervisor; each `run` slice writes `logs/profile-run.txt` (top 45
  cumulative functions, `kernel/session.py:96`).
- `kernel_totals.top_queries` in the report (from `Session.metrics`, query
  profile keyed by the first 72 characters of each Lua body,
  `core/deterministic_game.py:375`) shows which query dominates.

## 5. Stall patterns already met

| Pattern | Symptom in the report | Rule now in code | Test |
|---|---|---|---|
| Single-item transfer loop | `recent_100_actions_top` = take 1 coal / insert 1 coal, share ~1.0 | `FUEL_BATCH` in `V2Bootstrap.maintain_fuel` and `_take_output` for every job (`construction_supply.py:272,376`) | `test_v2_fuel_batching` |
| Burner cells fed while output is full | fuel actions continue, plates idle in furnaces, boiler/coal drill empty | skip `full_output` burners and cells with `FUEL_SKIP_STOCK` finished items (`construction_supply.py:239,285`) | `test_v2_fuel_batching.test_cell_with_enough_finished_output_gets_no_fuel` |
| Dedicated drill on a depleted fringe | `power` waiting, drill `no_minable_resources` | `DEDICATED_COAL_MINIMUM = 4000` in `FactoryBuilder.coal_sites` (`core/deterministic_builder.py:156`) | none yet |
| Proof requires a state another rule forbids | `starter_capacity` never succeeds, all cells built | stocked idle cells count in `StarterCapacity.proof` (`mining.py:84`) | none yet |
| Planner ignores stock | capacity nodes while plates sit in furnaces | spare stock offsets lookahead in `capacity_planning._facts` | `test_v2_capacity_planning.test_spare_stock_covers_upcoming_demand` |
| Too many hand-fuelled cells | iron grows 1 -> 8 cells, fuelling crowds out work | `CapacityConfig.max_plate_cells` (8 since seed 283895142, where 4 starved the early game; coal rule + `FUEL_BATCH` cover fuel) | `test_v2_capacity_planning.test_max_cells_cap` |
| Busy hauling after the bus | `recent_100_actions_top` = `fuel starter production` / `collect live construction stocks iron-plate` from stone furnaces after `smelting_district` + `planned_main_bus` succeeded | `starter_retirement.retired_resources`: no fuel/new cells for retired resources, `starter_retirement` job collects and mines the cells, plates come from the bus surplus (`collection_points`); coal cells retire last; check `starter-retirement.json` | `test_v2_starter_retirement` |
| Startup science cell still standing after the permanent row | labs/assemblers of `startup_science` stand idle after `automation_science` succeeded; or `science_growth` waits `science_growth_waits_for_moved_startup_hardware` | `science_retirement.science_retirement_nodes` needs `automation_science` succeeded, receipt `science_growth.evidence` with a `working` lab and `research` set, no open `starter_research`/`bootstrap_*` job and every such roadmap step satisfied; the job blocks `startup_science_retirement_batch_unfinished` while a bootstrap batch is funded (`industry.batch_refusal`), waits `obsolete_block_needs_inventory_room` without storage; check `startup-science-retirement.json` (`phase`, `entities`); growth is held only while that job is active and dismantling | `test_v2_science_retirement` |
| Fixed coordinates on a new seed | `0 legal sites`, every corridor fails | sites from `SiteService` / `site.placement` (smelting, red/green science, produce) | `test_v2_smelting_plan_site`, `test_v2_site_*` |
| East-only mining exits | coal exit points into another patch; smelting routes fail | `mining_site.plan_exit` turns banks toward the consumer zone | `test_v2_mining_site_exit` |
| Research jobs fight over the lab | second job blocked `another_research_is_running` | one research-owning job (`jobs._admission_error`); idle lane yields | `test_v2_concurrent_jobs.test_only_one_research_owning_job_is_active`, `test_v2_idle_research` |
| A planner raises on missing state | job `blocked` `planner_fault:<Exception>:...`; other jobs keep running | `jobs.PLANNER_FAULTS` blocks only that job (game/connection errors still stop the slice); fix the planner | `test_v2_concurrent_jobs.test_planner_fault_blocks_only_its_own_job` |
| Blocked job waits for a later success | prerequisite done, job stays blocked for minutes | `Supervisor._auto_retry` after any new success (<= 3 quick retries), then the game-time backoff | `test_v2_supervisor.test_dispatch_autopilot_and_auto_retry_run_without_any_model_call` |
| Concurrent block on a planned bridge pole | bank `blocked` `reserved block placement is obstructed` on a `small-electric-pole` outside its reserved area, every retry the same | `MiningOutposts._recover_obstructed_bridge` re-plans only unbuilt bridge poles (`power_grid.replan_power_bridge`, <= 2 per bank); sites avoid `board.planned_unbuilt_hardware` (startup science, smelting district) and `SiteService.mark_board_hardware` (allocator) | `test_v2_power_bridge_replan` |
| Bus waits on its lane tails while plates flow | `planned_main_bus` waits `waiting for district plates to reach every built bus lane tail` 10-15 min, a built tap with `dived_lanes` | the proof skips trunk tiles a built tap replaced (`tap["remove"]`: the crossing branch belt carries the other plate) and ignores taps not cut in yet (`planned_bus.proof`) | `test_v2_planned_bus.test_proof_ignores_the_crossing_belt_of_a_built_tap_that_dives_the_other_lane` |
| Run dead after a re-planning block | every job terminal, `run_stalled` in `tools/waste_report.py`, the bus blocked `planned_bus_feed_route_failed` with 3 retries spent | `_route_failed` / `_preflight_failed` blockers retry after `AUTO_RETRY_STALL_SECONDS`, and past `AUTO_RETRY_LIMIT` up to `IDLE_RETRY_LIMIT` more times while no job is active (`Supervisor._auto_retry`); the bus records `route_failures` / `failed_check` as scalar evidence | `test_v2_supervisor.test_a_failed_route_plan_is_retried_on_quiet_intervals_and_past_the_limit_while_the_run_is_idle` |
| Mining bank finds no site (fleet all-1113-2155, 3 seeds) | `mining_outpost` blocked `no_policy_compliant_paired_bank_with_growth_and_power` with `candidate_failures: []` on all four `exit_sides_tried`, or `mining_patch_native_receipt_changed` | observed seed outside the plan zone: moved onto the plan's patch, or (that patch farther than `MAX_OTHER_PATCH_DISTANCE`) the plan's other patches are tried on every side (`mining_site.patch_exits`); a port already off the ore is a candidate; relaxed rows (`no_growth_pair`, `single_pair_no_growth`, on the patch grid's lattice first); an empty survey is `mining_patch_survey_has_no_resource`; the observation's patch seed is the true nearest tile (ring search); evidence `attempts` (patch, side, candidates, failures) and `needs` (`outposts._needs`) | `test_v2_mining_patch_choice` |
| District corridors fail with legal sites | `no_clear_pcb_smelting_district_with_corridors`, `failure_stage_counts.corridor` close to `legal_sites`, the plan's feeder corridor goes around a patch | a feeder may detour as much as its reserved corridor (`smelting_site.corridor_detour`, `plan_connection(max_detour_ratio=)`); evidence `corridor_failures_by_item`, `needs` | `test_v2_smelting_corridor_detour` |
| Planned belt walls off a mine port | `no_clear_pcb_smelting_district_with_corridors`, every site fails on one feed, another block's unbuilt belt (e.g. the hand-fed power's coal feed) runs in front of that bank's port; clears itself once that belt is built | another block's planned surface belt is tunnelled like a built one; only its neighbour cells stay off-limits to surface belts (`transport.unbuilt_external_keepouts(crossable_belts=)`, `planned_soft`) | `test_v2_planned_belt_crossing` |
| Bank bridge across the smelting zone | few `legal_sites`, a bank's bridge poles inside the plan's smelting zone | pole hops pay `ZONE_POLE_COST` / `DENSE_ZONE_POLE_COST` inside foreign plan zones and bus strips (`power_grid._plan_zone_costs`) | `test_v2_power_bridge_plan_zones` |
| Block built over another capability's unboarded plan | `reserved block placement is obstructed`, the blocker is in `electric-coal.json` / `factory-builder.json` | `board.unboarded_plans` are planned hardware for the district (candidates, routes, poles, whole plan) and mining banks | `test_v2_unboarded_plans` |
| Roadmap frozen behind one lane | `empty_queue`/no new jobs for 20+ min; ready research nodes behind a waiting `research` job (`waiting_for_science_pack_supply`) | `queued_count` skips nodes of busy lanes; research needing an unbuilt pack producer is not proposed | `test_v2_supervisor.test_ready_nodes_behind_a_busy_lane_do_not_keep_the_autopilot_off`, `test_v2_progression.test_green_research_waits_for_the_green_block_which_is_proposed_first` |
| Long cross-base belt needs > 6 turns | furnace block `no_clean_trunk_corridor` on every candidate (stone bank north of the district, blocks south of the bus) | `plan_connection` opts into `routing` long-route fallback (fewest-turn corridor, <= `LONG_ROUTE_TURNS`, >= `LONG_ROUTE_DISTANCE` tiles) | `test_v2_routing.LongRouteFallbackTests` |
| Outlet sealed by a neighbour's unbuilt plan | `route_port_escape_reserved_by_unbuilt_plan` with `escape` in the job evidence `failures` | named, not searched; re-planned when the facts change (the neighbour is built) | `test_v2_transport.SealedEscapeTests`, `test_v2_planned_furnaces.test_every_candidate_failure_reaches_the_job_evidence` |
| Planning failures stay blocked after the facts changed (eval-1575138435, 75 min) | `logistic_science` `no_green_science_site_with_clean_connections`, `mining_outpost` and `dedicated_materials` `no_policy_compliant_paired_bank_with_growth_and_power` blocked 25+ min with no success since; the post-mortem dry run fits all three; the reasons were no re-planning blockers and their 3 retries were spent within 7 min | the remedy ladder (ARCHITECTURE.md): `needs` on the job, a read-only what-if per rung, `retry --remedy` when it fits, `replan` re-armed on new facts or every `REMEDY_RECHECK_SECONDS`; check the node's `retry_plan` / job `remedies_tried`, `next_remedy` | `test_v2_remedy_ladder` |
| Short tap route has no corridor | a 5-tile iron tap route `no_clean_trunk_corridor` on every tap row; the block's other tap (copper) was routed first across its approach | `planned_tap.connect_inputs` plans the set again with the failing input first, once per input (`route_order` / `orders_tried`) | `test_v2_remedy_ladder.RouteOrderTests` |
| Return feed on a jogged trunk (long-1114-0039 seeds 1188507449, 4031765348) | `trunk_return_feed` blocked `return_feed_trunk_not_observed` while the trunk stands; the consumer route turns right after the port (first straight run = 1 tile) | `TrunkReturnFeed.prepare` walks every straight run (`mine_trunk.trunk_segments`) up to the first tapped run; the entry records its `segment` | `test_v2_trunk_return_feed.LayoutTests` |
| Steam pair finds no slot (seed 1277653921) | `steam_expansion_no_buildable_bank_slot`, empty evidence; both row ends taken by the plant's coal feed / the shore | free-site water runs also join beside the plant's surface water pipes (`steam_siting.water_sources`); `rejected_slots` + `needs`; rung `site:wider_steam_search`; an unpowered plant is a wait | `test_v2_steam_layout.FallbackTests`, `test_v2_steam_expansion` |
| Startup science retirement blocked (seeds 1024125955, 4031765348) | `startup_science_retirement_without_permanent_science` with no green science: research needs green packs, permanent labs idle | idle powered labs holding machine-made red packs verify the takeover; otherwise `waiting` | `test_v2_science_retirement.StepTests` |
| Chest tile littered by its own inserter (seeds 1024125955, 1188507449) | magazine row `reserved block placement is obstructed` on its iron chest, `terrain_or_entity_collision` | `containers_before_their_inserters` in `ensure_plan`; ground stacks on a reserved tile are picked up first (`_recover_ground_items`) | `test_v2_build_ahead.ChestOrderTests` |
| Plant at its limit reads as unpowered (eval-1575138435, 257 min) | `steam_expansion` waiting `running_source_plant_required`, power bridges `no_supplied_pole_network`, machines `low_power`; engines `working` with `energy` 0 | `power_grid.generating` / `supplied_networks` (energy or status `working`) everywhere a supplied network is looked up | `test_v2_steam_expansion.test_a_plant_at_its_limit_generates_with_an_empty_buffer` |
| Power link on a science lab slot (aud-1114-0526 seed 1723389324) | `supply_cell` `reserved block placement is obstructed`, blocker a `small-electric-pole` the plan put inside a lab the startup cell built later | `board.reserved_growth_hardware`: lab slots, their poles and the west extension are planned hardware (`planned_unbuilt_hardware`) for every other planner | `test_v2_power_link_poles.LabSlotsArePlannedHardwareTests` |
| Fixed pole chain over ore (seed 1032498969) | `bootstrap_logistics` / `bootstrap_research` `new_entity_over_resource_or_mining_reservation` on a grid-chain pole outside the cell's area | block surveys run `board.LINK_POLE_LUA` on every new link pole (ore, mining reservations; a standing own pole is adopted); all candidates closed this way -> `plan_needs` kind `power` | `test_v2_power_link_poles.LinkPoleLuaTests` |
| Supply cell in the lake (seed 4058652641) | `no_clear_supply_cell_site` `{"candidates": 31}`, all within 48 tiles of a lakeside pole | site-map filter (`supply_cell.SiteLand`, `rejected_by`), reach 80 tiles, pole-hop links that keep out of belt corridors (`power_grid.plan_connection(keep_out=)`), rung `relax:supply_cell_late_zones` | `test_v2_power_link_poles.SupplySiteLandTests` |
| Crossing bus feeds (aud-1114-0436 seed 3098273696) | `planned_bus_feed_route_failed`, `no_clean_trunk_corridor` for both items, either feed alone routes; post-mortem "nothing there now" | a second pair of orders where an earlier feed keeps off the later feeds' head approach tiles (`transport.plan_connection(keep_clear=)`) | `test_v2_route_crossings.CrossingFeedsTests` |
| Bridge pole in front of a lane head (seed 2446866888) | bus feed `no_clean_trunk_corridor` for one item; a bank's bridge pole column inside the header corridor | pole hops keep out of site-plan corridors (`power_grid._plan_corridors`), open only when no other hop route exists; a pole already built stays (needs a paid rebuild) | `test_v2_route_crossings.BridgeCorridorTests` |
| Corridor survey budget from a far guide (seed 3242269094) | district `belt_corridor_survey_budget_exceeded` on every site for one ore; the planned corridor belongs to another patch | `transport._plan_connection` drops a guide whose box exceeds `CORRIDOR_SURVEY_TILES`; an idle run re-plans a spent ladder after `REMEDY_IDLE_RECHECK_SECONDS` | `test_v2_route_crossings`, `test_v2_remedy_ladder.test_an_idle_run_replans_a_spent_ladder_sooner` |
| Trunk tap search stops at a dive (same map) | `power_coal_tap` `mine_trunk_tap_unavailable`, `tap_beyond_bus_length` with a short `segments` run | `mine_trunk.trunk_segments(undergrounds=True)` passes underground pairs in `plan_tap`; `power_coal_tap` has a DryRun | `test_v2_mine_trunk.test_tap_search_passes_the_trunk_underground_under_a_crossing_line` |
| Growth connects to a dead-end bank (same map) | `mining_outpost` `owned_ore_trunk_trace_failed` at a bank's own port, `paired_mining_reserved_columns_exhausted` | the patch trunk bank first (`MiningOutposts.plan_growth`); a proven bank whose defense deferral lapsed is armed (`_construction_coverage`) before it counts as missing capacity; the mining DryRun mirrors `step` | `test_v2_eval_1575138435_late` |
| Cross-base feed has no corridor (same map) | `steel_supply` `no_steel_furnace_block_with_complete_connections`, coal route 176 tiles `no_clean_trunk_corridor` on every candidate | rung `relax:waypoint_route` (`transport.waypoint_connection`: two legs through a waypoint) | `test_v2_eval_1575138435_late.WaypointRouteTests` |
| Steam pair finds no coal (same map) | `steam_expansion_layout_unroutable`, the pair's dedicated coal bank `no_safe_bounded_mining_exit` | the pair's coal is a `serve="first"` tap on the coal patch trunk first (`SteamExpansion._trunk_layout`); power grows by `steam_expansion {"stage": k}` from the `power_growth` receipt | `test_v2_steam_expansion.test_coal_comes_from_the_coal_patch_trunk_before_a_dedicated_bank`, `PowerGrowthTests` |
| Output-full block blamed on its feed (same map) | auditor: stone mining `transport`, brick furnaces `no_ingredients` holding 100 bricks | a machine holding a full stack of its product is `full` (`bottleneck_audit._holds_full_stack`) | `test_v2_bottleneck_audit.test_brick_furnaces_holding_a_full_brick_stack_are_full_not_a_stone_transport_limit` |
| Machine-only ingredient read as a dead end (eval-1575138435, 320 min) | `engine_production` blocked `required item needs a production machine`, evidence `steel-plate` 8 (the row's steel chest) while the steel furnaces worked | `ensure_item`: a machine-only item a built machine makes (`_live_producers`, recipe set, powered) is `waiting for automated <item> output`; only no producer at all blocks | `test_v2_machine_only_wait` |
| Executor failure is a dead end (same map) | `oil_gathering` blocked `repeated_executor_failure:position_blocked`, the scout `move` to a deep-water stand three times | the failure is `needs` (`plan_needs.from_executor_failure`: action, position, reason) and re-planning (`_replannable`); scout stands are land tiles inside the chunk (`OIL_SURVEY_LUA` `find_non_colliding_position`), a target the moves do not reach is given up (`OIL_SCOUT_MOVES_PER_TARGET`) | `test_v2_remedy_ladder.ExecutorFailureTests`, `test_v2_oil_gathering` |
| Long power bridge hidden behind the budget (same map) | oil well 219 tiles out, every candidate `power_connection_survey_budget_exceeded` | a finer grid that only exceeds the budget keeps the coarse "no connection", so the outside-anchor search runs; A* expands each node once from 7-tile buckets | `test_v2_power_bridge_plan_zones.test_a_long_bridge_from_a_pole_inside_a_block_starts_at_a_pole_outside` |
| Claimed steel read as no producer (same map, 330 min) | `engine_production` and `oil_gathering` blocked `required item needs a production machine` (steel 8 / 5) with 163 steel in the idle furnaces | an idle furnace (no recipe observed) holding the item counts as a producer, also in a slack job's view (`critical_path.slack_view` hides claimed output); with no producer at all the evidence names `producer` (`rocket_plan.producer_for`) and `Supervisor._missing_producers` queues it | `test_v2_machine_only_wait`, `test_v2_remedy_ladder.SupervisorLadderTests.test_a_machine_only_item_no_machine_makes_gets_its_producer_proposed` |
| Long crude pipe (same map, ~470 tiles) | `basic_petroleum` `no_bounded_basic_petroleum_site_with_complete_native_connections` | `plan_needs` from the failing stage (`petroleum._petroleum_needs`), DryRun, rungs `site:longer_connection`, `relax:route_turns_max`, `relax:waypoint_route` (two pipe legs, `plan_crude_connection`); a pipe beyond the native extent gets inline powered pumps (`petroleum.plan_pumps`, `test_v2_petroleum_remedy.PumpSegmentTests`) | `test_v2_petroleum_remedy` |
| Machine-only item block after a restart (same map, 375 min) | engine_production / oil_gathering blocked `required item needs a production machine`, dry run `waiting for automated steel-plate output`, every job terminal, actor idle | `SUPPLY_RETRY_REASONS` join the quiet-interval retry (`Supervisor._auto_retry`) | `test_v2_supervisor.MultiJobTests.test_a_machine_only_item_block_is_retried_after_a_quiet_interval_without_a_success` |
| Construction steel owned by its only consumer (same map, 375 min) | engine_production / oil_gathering `waiting for automated steel-plate output` with `have` 0 while steel furnaces hold 85 + 78 behind a full output belt whose port tile the engine row's route also owns | a block machine holding >= `SURPLUS_OUTPUT_STOCK` of its own output-port item is takeable surplus (`V2Bootstrap._unreserved_stock_observation`) | `test_v2_construction_ownership.RegisteredStockOwnershipTests.test_a_backed_up_pile_of_a_blocks_own_product_is_construction_surplus` |
| Dead-end bank counted as supply (same map, 375 min) | iron `mining_outpost` `no_clean_extra_bank_connection_to_owned_ore_trunk`; a 2-drill bank's port belt (47.5,-25.5) full of ore 4 tiles beside the trunk, no consumer | `MiningOutposts.dead_end_banks` (ready, no consumer connection, not a district source port or board source, nothing in front of the live port belt); `plan_growth` routes it onto the trunk first (`prepare_dead_end_connection`, built as an extension, only the route preflighted) | `test_v2_eval_1575138435_late.DeadEndBankTests` |
| RCON latency with a GUI client | observe ~580 ms, query ~110 ms | `runtime.NETWORK_SEGMENT_SETTINGS` (16384-byte segments) | - |
| Dead actor hangs the run (all-1114-0107-1075833632, 30.6 min) | every `observe` fails `agent_dead`, controller `error` every second, job ticks stop while the game ticks on, the watcher never ends the run | `agent_dead` is the supervisor's own state (`Supervisor._actor_dead`): client-free, the death is recorded and the run ends `errored: agent_dead` (the optional respawn emulation `respawn-actor`, off by default, owner approval pending: HEADLESS.md "Death and respawn") or `recover-actor` for a player, unknown outcomes reconciled, corpse items by `recover-belongings` when no enemy is near; the watcher's stall timeout ends a run without game-time progress; `fleet.py report` "actor deaths", `waste_report` `actor_death`; an expected attack (polluted nest, moving group) pulls gun turrets forward (DEFENSE_POLICY.md) | `test_v2_actor_death` |
| Runs idle for hours after a last success (fleet long-1114-0612, 4 seeds, last success 29-90 of ~240 game min) | every remaining job blocked past its 3 retries (`objective_progress_stalled`, `job_deadline_exceeded` from the wall clock, `replacement_electric_drill_has_no_power`, `production_row_extension_exceeds_reserved_slots` with no success to wait for) | no final strike: `blocked_category` + `_backoff_retry` (`BACKOFF_MINUTES`, identical failures double, one per cycle); deadline in game time only; stalls/deadlines drop stale `needs`; only unknown outcomes, rollbacks and `permanent_block` stay final (a `blocked_job` incident). The node's `retry_plan` names `next_retry_at_tick` | `test_v2_remedy_ladder.LongFleetEndStateTests`, `test_v2_supervisor.MultiJobTests.test_an_identical_failure_doubles_the_backoff` |
| A job only waits for hours | `waiting` with the same reason, no actions | `no_progress` incident with the job key as subject after `wait_stall_minutes` (20) game minutes unchanged | `test_v2_incidents.WaitStallTests` |

History: `rg -n "Part3[5-9]|Part40" note.md`, then read only those lines.

## 6. Rules for the fix

1. Fix the general rule where it lives (table above), not the one map.
2. Add a test that uses the live numbers from the report (counts, rates,
   reasons) and fails before the fix.
3. Never edit `jobs.json`, state files or the game by hand; no Lua console
   fixes. Use `retry`/`reconcile`/`pause` through the CLI only.
4. Measure the fix on a new map: `python -B -m factorio_agent.lifecycle new-map`
   (old runtimes are kept; never delete them), then compare `milestones`.
