# Handoff detail (2026-11-14)

Long form of `HANDOFF_CURRENT.md`. Read `AGENTS.md` first (binding rules), then `HANDOFF_CURRENT.md`,
then this file, then `docs/v2/README.md`. `note.md` and `insight.md` are search-only archives.

Repository `C:/Users/NEC/Documents/git/factorio_automation`, branch `v2`,
remote `https://github.com/Schwalbe262/Factorio_automation.git`. Factorio 2.1.20 Space Age over RCON.

## 1. Goal and how it is judged

- First goal: a real rocket launch, reproducible on other map seeds; what already works must replay fast.
- Parallel fleets exist to find lost time and stalls in the logs and turn them into rule fixes.
- The factory must grow in quantity (plates, packs, labs, power), not only advance the tech tree.
- Judge by milestone game minutes (`tools/report_run.py`, `tools/fleet.py report`), never by activity.
  Human benchmark: smelting district and main bus within 20 game minutes.
- Decisions are rules in code. The model (Luna) is off (`planner none`); a stronger incident model is
  deferred by the owner.

## 2. Where things stand

### Early game (12 seeds x 45 game minutes, client-free fleets)

| round | commit | district median (reached) | bus median (reached) | idle with work |
|---|---|---|---|---|
| aud-1114-0752 | c98f4a4 | 26.9 (10/12) | 31.8 (8/12) | 2682 s |
| aud-1114-0841 | c29746a | 25.8 (11/12) | 29.5 (11/12) | 946 s |
| aud-1114-0930 | 7711256 | 26.7 (12/12) | 31.9 (12/12) | 391 s |

Every seed now reaches the district and the bus; the pace is still 6 to 12 minutes over the target.
Round `aud-1114-1029` (6e93a8d) and long fleet `long-1114-1021` (4 seeds, 600 min, 6e93a8d) were
running at the pause and are not analysed. Read them first:
`python -B tools/fleet.py report --name <fleet>` and `python -B tools/waste_report.py <fleet>`.

Ranked time losses to the bus (round 0841, analysis scripts in section 7):

1. Fuel upkeep halted a job's own construction (up to 4.6 min): fixed 57fe960, unmeasured.
2. The actor carries coal to the boiler and ~26 starter burners until the bus is done (2 to 9 min):
   `power_coal_feed` rule 3a holds the belt feed until district and bus succeed. See section 5.
3. Underground belts are crafted only after logistics research; the district's first bank follows
   2.5 min (up to 7) later. Pre-craft during research, build the frame parts that need no logistics.
4. Early research is bound by pack supply (~390 s against a 275 s bound for 2 labs).
5. Red science reached 7/12 in 45 min: `automation_science` is admitted only after the bus.

### Mid and late game (live GUI map `runtime/v2/eval-1575138435`, ~590 game minutes)

Proven live on this one map, in game minutes: steel chain 334, oil_gathering 435, engine_production 437,
basic_petroleum 448, plastics research 458, sulfur-processing 464, remote_smelting 509 (iron from the
next patch onto the bus), engine belt row via route tap 527, bus_lane coal 576, plastic 578.

Blocked at the pause:

| job | reason | what is needed |
|---|---|---|
| gas_distribution | original_gas_already_owned_by_chemical_plan | section 5, item A |
| advanced_circuit_production | planner_fault (port conflict, fixed in 7305ffb) | retries now that planner faults are replannable; unobserved |
| steam_expansion stage 3 | resumed from saved state at 589 min | watch that it completes |
| mining_outpost coal 242 | home patch has no site | section 5, item C |
| mining_outpost iron 420 | home patch has no site | covered by remote_smelting; the job itself stays blocked |

Nothing after plastic is proven live: sulfur, sulfuric acid, advanced circuits, chemical science,
advanced petroleum, lubricant, processing units, electric engines, rocket fuel, concrete, silo, launch.
The rocket path exists on paper (`python -B tools/rocket_plan.py <run>`: every bill item has a producer).

Two older long fleets (other seeds, old code bf64b2c and ba4f056) stalled at 240 to 320 game minutes on
steel, brick, green science sites and supply cell deadlines: the mid game is not yet reproducible on
other seeds. `long-1114-1021` is the first long fleet on current code.

## 3. Self-recovery machinery (use it, do not add one-off fixes)

- `plan_needs.py`: a blocked job states what it needs (site, route, source, power, built, preflight).
- `remedy.py` + `Supervisor._remedy_ladder`: replan > layout > site > relax > clear > shrink, tried by
  read-only what-if (`remedy.what_if`, `postmortem.DRY_RUN`). A job with a saved build in progress is
  continued, not re-planned (`postmortem.resumable`, `saved=` readers).
- `clear` rung (`remedy.CLEAR_REBUILD`): paid guarded removal and rebuild. `built_block_repair.py`
  (a plant with no water), `pole_relocation.py` (a pole on a lane head). Both unproven live.
- `bottleneck_audit.py` + `growth_providers.py` (`growth_options`): propose growth when supply is short;
  `idle_with_work` reports waiting with work available.
- Second supply when the home patch is full: `remote_smelting.py` (plate ores), `bus_lane.py` (a new
  lane on a built bus), `production_sources` `route_tap` (a taken port serves a second row),
  `planned_tap.feed_layout` (plates enter a built trunk).
- Retry rules: `supervisor._auto_retry`, `_replannable`, `REPLAN_REASONS`, `SUPPLY_RETRY_REASONS`,
  `RETRY_CLEARED_BY`.

## 4. Working procedure

- Gate before every commit, from PowerShell: `powershell -ExecutionPolicy Bypass -File tools/check.ps1`.
  The lupa tests run only in the main checkout: rerun the gate after merging a worktree. Do not edit
  files while the gate runs.
- Merge into the main checkout only with the GUI supervisor paused:
  `python -B -m factorio_agent.lifecycle stop`, merge, check `git status` for conflicts, then
  `python -B -m factorio_agent.lifecycle start` (set `PYTHONPATH=src`).
- Fleets run from a frozen snapshot worktree under `runtime/fleets/code/<name>` with
  `--base <repo>/runtime`, so merges cannot mix modules into a running round. Remove the snapshot when
  the fleet has stopped (`git worktree remove --force`).
- `git add` specific files; keep the tree clean; remove merged worktrees at once.
- Never put `--` comments in Lua bodies (they are sent as one line).
- Per part: `git diff --stat`, update `HANDOFF_CURRENT.md` (10 lines), append one event to `note.md`.
- Dashboard: `python -B tools/dashboard.py --host 0.0.0.0` (port 8765, follows the active runtime).

## 5. Open work, with entry points

A. **Gas takeover for sulfur** (blocks chemical science on the live map). The refusal is in
   `gas_distribution.py` `GasDistribution._registry`. Design worked out on the live map: a tee splice
   on plastic's gas route (3 new pipes from the first route cell past the refinery's apron, no pipe
   removed), then move the registry row, publish the plastic alias, rewrite plastic's saved plan
   (`chemical-production.json`, board block `chemical_plastic-bar`) through the controller's own state
   (it caches in memory), then build. Tests beside `tests/test_v2_gas_distribution.py`. Also reorder
   `progression.py` so `gas_distribution` precedes `chemical_production_plastic` on fresh maps.
B. **Then watch the chain**: advanced circuits (plastic by its own route), sulfur, chemical science,
   and what `rocket_plan` proposes next. A plastic bus lane will use `bus_lane`; widening the band
   when it has no spare lane (`bus_lane_no_room_in_band`) is not implemented.
C. **Remote bank for a non-plate ore** (coal): `MiningOutposts.prepare_on_patch` +
   `planned_tap.feed_layout`, proposed like `remote_smelting.remote_smelting_nodes`.
D. **Remote block follow-ups**: growth of the remote bank and furnaces, defense and depletion
   registration (the bank is only a board block), scouting when no known patch qualifies.
E. **Fluid pipes off bus tap-port columns** (the crude pipe on the east column forced a pipe bridge).
F. **Early pace**: boiler fed from the coal trunk as soon as the coal outpost succeeds
   (`power_coal_feed` rule 3a, `power_coal_tap` admission); pre-crafting during research; district
   frame before logistics; earlier red science block; pack supply for early research.
G. **Smaller**: quiet site instead of a defended one when turrets are not craftable;
   `no_clear_pcb_smelting_district_with_corridors` on seed 1180503872 (the source trunk's own belts
   count as obstacles); bus reach drop 10 to 7 between 462e304 and af83ce4 never bisected; compact and
   sparse mine/smelting shapes not buildable; steel/brick head rework; lab growth constants
   (start 2, step 2, frame 8) should come from supply; N-to-M balancer generator; restart speed
   ("fast replay") never measured; pump segments (`petroleum.pump_segments`) unproven live, pump
   direction and fluidbox index assumed.

## 6. Owner decisions still open

1. Payback-driven iron starter cells replace the fixed 6-cell cap (default on, switch
   `capacity_planning.PLATE_CELL_POLICY`, `PLATE_CELLS_CAP` reverts). This reverses an earlier explicit
   instruction; the owner has not confirmed it. Since dc5ee38 cells stop when the district starts.
2. Client-free respawn emulation after an actor death is implemented but OFF
   (`fleet.py start --respawn`); it would widen the test-mode exception in `AGENTS.md`.

## 7. Where the evidence is

- Fleet reports: `runtime/fleets/reports/<fleet>.report.txt`; run folders `runtime/v2/<fleet>-<seed>`
  with post-mortems under `<run>/postmortem` (`fleet.py inspect|revive|postmortem`).
- Analysis and probe scripts (throwaway, session scratchpad, run with `python -I` from a checkout; they
  are outside the repository and may be gone):
  `C:\Users\NEC\AppData\Local\Temp\claude\a29fd59902280fca\e534bb8c-6eb0-435c-b962-137b17003590\scratchpad`
  subfolders `w-pace4` (critical chain per run), `w-live3` and `w-live4` (dry runs on the live map),
  `fleet_loop.sh` and `fleet_long.sh` (fleet rounds from snapshots).
- Owner rules: `AGENTS.md`, `goal.md`, line 8 of `HANDOFF_CURRENT.md`, `docs/v2/LAYOUT_PRINCIPLES.md`,
  `docs/v2/BALANCERS.md`.
