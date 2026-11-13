# Debugging a stall

A stall is "no job succeeded for a long time", not "few actions". A run can
be busy and stuck. Work read-only on the live runtime; fix rules in code.

## 1. Report

```powershell
python -B tools/report_run.py runtime/v2/<name>            # text
python -B tools/report_run.py runtime/v2/<name> --json     # full data
python -B tools/waste_report.py runtime/v2/<name>          # wasted game seconds by type (or a fleet name)
```

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
| Blocked job waits for a later success | prerequisite done, job stays blocked for minutes | `Supervisor._auto_retry` after any new success, <= 3 | `test_v2_supervisor.test_dispatch_autopilot_and_auto_retry_run_without_any_model_call` |
| Concurrent block on a planned bridge pole | bank `blocked` `reserved block placement is obstructed` on a `small-electric-pole` outside its reserved area, every retry the same | `MiningOutposts._recover_obstructed_bridge` re-plans only unbuilt bridge poles (`power_grid.replan_power_bridge`, <= 2 per bank); sites avoid `board.planned_unbuilt_hardware` (startup science, smelting district) and `SiteService.mark_board_hardware` (allocator) | `test_v2_power_bridge_replan` |
| Bus waits on its lane tails while plates flow | `planned_main_bus` waits `waiting for district plates to reach every built bus lane tail` 10-15 min, a built tap with `dived_lanes` | the proof skips trunk tiles a built tap replaced (`tap["remove"]`: the crossing branch belt carries the other plate) and ignores taps not cut in yet (`planned_bus.proof`) | `test_v2_planned_bus.test_proof_ignores_the_crossing_belt_of_a_built_tap_that_dives_the_other_lane` |
| Run dead after a re-planning block | every job terminal, `run_stalled` in `tools/waste_report.py`, the bus blocked `planned_bus_feed_route_failed` with 3 retries spent | `_route_failed` / `_preflight_failed` blockers retry after `AUTO_RETRY_STALL_SECONDS`, and past `AUTO_RETRY_LIMIT` up to `IDLE_RETRY_LIMIT` more times while no job is active (`Supervisor._auto_retry`); the bus records `route_failures` / `failed_check` as scalar evidence | `test_v2_supervisor.test_a_failed_route_plan_is_retried_on_quiet_intervals_and_past_the_limit_while_the_run_is_idle` |
| Mining bank finds no site (fleet all-1113-2155, 3 seeds) | `mining_outpost` blocked `no_policy_compliant_paired_bank_with_growth_and_power` with `candidate_failures: []` on all four `exit_sides_tried`, or `mining_patch_native_receipt_changed` | observed seed outside the plan zone: moved onto the plan's patch, or (that patch farther than `MAX_OTHER_PATCH_DISTANCE`) the plan's other patches are tried on every side (`mining_site.patch_exits`); a port already off the ore is a candidate; relaxed rows (`no_growth_pair`, `single_pair_no_growth`, on the patch grid's lattice first); an empty survey is `mining_patch_survey_has_no_resource`; the observation's patch seed is the true nearest tile (ring search); evidence `attempts` (patch, side, candidates, failures) and `needs` (`outposts._needs`) | `test_v2_mining_patch_choice` |
| District corridors fail with legal sites | `no_clear_pcb_smelting_district_with_corridors`, `failure_stage_counts.corridor` close to `legal_sites`, the plan's feeder corridor goes around a patch | a feeder may detour as much as its reserved corridor (`smelting_site.corridor_detour`, `plan_connection(max_detour_ratio=)`); evidence `corridor_failures_by_item`, `needs` | `test_v2_smelting_corridor_detour` |
| Planned belt walls off a mine port | `no_clear_pcb_smelting_district_with_corridors`, every site fails on one feed, another block's unbuilt belt (e.g. the hand-fed power's coal feed) runs in front of that bank's port; clears itself once that belt is built | another block's planned surface belt is tunnelled like a built one; only its neighbour cells stay off-limits to surface belts (`transport.unbuilt_external_keepouts(crossable_belts=)`, `planned_soft`) | `test_v2_planned_belt_crossing` |
| Bank bridge across the smelting zone | few `legal_sites`, a bank's bridge poles inside the plan's smelting zone | pole hops pay `ZONE_POLE_COST` / `DENSE_ZONE_POLE_COST` inside foreign plan zones and bus strips (`power_grid._plan_zone_costs`) | `test_v2_power_bridge_plan_zones` |
| Block built over another capability's unboarded plan | `reserved block placement is obstructed`, the blocker is in `electric-coal.json` / `factory-builder.json` | `board.unboarded_plans` are planned hardware for the district (candidates, routes, poles, whole plan) and mining banks | `test_v2_unboarded_plans` |
| RCON latency with a GUI client | observe ~580 ms, query ~110 ms | `runtime.NETWORK_SEGMENT_SETTINGS` (16384-byte segments) | - |

Known gap: a job that only *waits* (no actions) raises no `no_progress`
incident (`incidents.py` needs repetitive actions); look for long
`waiting` reasons in `jobs.json` yourself.

History: `rg -n "Part3[5-9]|Part40" note.md`, then read only those lines.

## 6. Rules for the fix

1. Fix the general rule where it lives (table above), not the one map.
2. Add a test that uses the live numbers from the report (counts, rates,
   reasons) and fails before the fix.
3. Never edit `jobs.json`, state files or the game by hand; no Lua console
   fixes. Use `retry`/`reconcile`/`pause` through the CLI only.
4. Measure the fix on a new map: `python -B -m factorio_agent.lifecycle new-map`
   (old runtimes are kept; never delete them), then compare `milestones`.
