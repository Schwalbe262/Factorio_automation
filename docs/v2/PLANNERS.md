# Planners: rules, defaults, tuning

Planners are pure functions over the observe receipt and jobs; they only propose
nodes or sizes. Config dataclasses are not read from `automation_config.json`:
tuning = change the dataclass default and the test that pins it.

## Roadmap autopilot (`progression.py`)

- `Supervisor._autopilot`: when < `LOW_QUEUE = 2` nodes pending/ready, <= `AUTOPILOT_LIMIT = 3` nodes (`supervisor.py:47`).
- Order inside `autopilot_nodes`: capacity nodes, then produce nodes, then the
  supply cell node, then `ROADMAP` steps; all count toward the limit.
- Priorities: capacity `CAPACITY_PRIORITY = -50`, produce `PRODUCE_PRIORITY = -51`,
  roadmap step `TOP_PRIORITY - index = -100 - index`. A model request uses 0,
  so it always runs first.
- A step is proposed when: not satisfied, not represented (node/open job), not
  stalled (blocked/failed job), `requires` succeeded, `after` satisfied,
  `native_recipes` enabled, research listed in `available_research`. Science
  steps take `research_capacity.recommended_rates` when `0 < rate <= default`.
- Tune: `ROADMAP` order/`after`/`builds`; pinned by `tests/test_v2_progression.py`.

## Starter capacity (`capacity_planning.plan_capacity`, `CapacityConfig`)

For each raw item with evidence in `resource_supply_options` (rates per minute):

```
required = max(0, shortfall) + max(0, lookahead - max(0, available - required_bill))
T(0)     = 60 * required / net                      (inf when net <= 0)
T(k)     = fund + cross_fund + k * build_seconds + 60 * (required + k * own_cost) / (net + k * gain)
gain     = cell_rate  (coal: cell_rate - fuel_per_drill_per_minute)
expand k when T(0) - T(k) >= min_saving_seconds and T(k) <= max_ratio * T(0)
coal cells needed = ceil(fuel_margin * (smelting cells * 3.6 + drill-only cells * 2.25) / (cell_rate - 2.25)), at least 1
coal: never above cells needed; +1 cell when installed < needed (measured coal rates are not used)
every item: net += max(0, installed cells * cell_rate - produced)   # cells built but not producing yet
```

| Field | Default | Meaning |
|---|---|---|
| `max_step` | 2 | cells added per decision |
| `max_cells` / `max_plate_cells` | 8 / 6 | cap for coal / plates and stone (was 4 for plates: starved the early game on seed 283895142; the time comparison against lookahead demand decides below the cap) |
| `default_cell_rate` | 15.0 | ore/min per burner cell when unmeasured |
| `fuel_margin` | 1.2 | coal deficit threshold |
| `fuel_per_cell_per_minute` / `fuel_per_drill_per_minute` | 3.6 / 2.25 | coal burned by a new smelting / drill-only cell |
| `build_seconds_per_cell` | 15.0 | unless evidence has `build_seconds` |
| `min_saving_seconds` / `max_ratio` | 60.0 / 0.85 | expansion must clearly beat waiting |
| `hand_gathered_seconds` | `{"wood": 2.0}` | fund seconds per item of a cell input with no supply row (the wooden chest of coal/stone cells); any other unknown input makes the option impossible |

Unmeasured production or unknown cell cost means wait. Before `hand_gathered_seconds`
existed, every coal option was infinite because the coal cell's 2 wood had no
supply row, so coal never expanded (seed 283895142: one coal cell for ~10
burners, cold new cells, `power` waited 6 min for "automated coal output"). Sorted by `T(0)`
descending; a decision whose cell cost uses an item already being expanded is
dropped; a resource is skipped once its `mining_outpost` succeeded. Pinned by
`tests/test_v2_capacity_planning.py`, `tests/test_v2_progression.py`.

## Produce (`progression.produce_nodes`)

```
propose item when bulk_item_demand[item].count > BULK_THRESHOLD (20) and no produce job/node for it
rate = round(min(count / COVER_MINUTES (5.0), PRODUCE_MAX_RATE (600), min_p net_p / raw_per_item_p), 1)
skip when any plate net is unmeasured or <= 0, or rate < PRODUCE_MIN_RATE (1.0)
```

`bulk_item_demand`: `Step.builds` of the next 4 steps. Pinned by `tests/test_v2_produce_autopilot.py`.

## Supply cell (`supply_cell.py`, temporary hand-fed factory)

A small chest-fed assembler cell, like the startup science cell: not on the
bus, plates carried in by hand in batches, products taken from bar-capped
chests. It replaces hand crafting of bulk items and stocks the intermediates
(gears, circuits) between power and the bus, so a drill or a lab is only a
final assembly.

```
receipt  supply_cell_candidates = candidates(catalog, jobs, obs, root=runtime)     (__main__ observe)
builds   supply_demand.upcoming_builds over the next DEMAND_HORIZON (8) roadmap steps:
           a step in flight with a saved plan (MEASURED_PLANS: smelting_district, automation_science,
           planned_main_bus) counts plan.required_items minus the plan entities standing on the map,
           every other step counts Step.builds
demand   item_demand(X) = units of X inside those builds (recursive catalog expansion, stops at raw items
           and at X), for every X that is placed or a direct ingredient of a placed item (direct_use);
           current(X) = the same over the steps in flight only; science packs are not counted
choose   greedy, largest hand_seconds(count) first, at most MAX_PRODUCTS (6):
           evaluate(X, stocked = products chosen so far): a stocked direct ingredient is a shared chest,
           not a sub-tree, so X costs only its own machine and saves only its residual hand time
           qualifies when  count > HANDCRAFT_LIMIT (10)
                      and  hand_seconds(count) >= hand_seconds(hardware: per machine 1 assembler, one inserter
                                                   per chest it touches, its input chest; 1 product chest)
                      and  hand_seconds(made) >= hand_seconds(hardware),
                           made = min(count, rate * PRODUCTION_WINDOW_SECONDS (1800) / 60)
                           rate = min(assembler crafts/min, CHEST_INSERTER_PER_MINUTE 50 / items moved,
                                      stocked ingredient rate / units needed)
                      and  layout(chosen + [X]) still fits
build    node auto_supply_cell {"products": chosen} once per map, priority -52, requires power, native assembling-machine-1
retire   node auto_supply_cell_retire {"retire": true} once, priority -1000, when
           the build job succeeded and every product has demand <= HANDCRAFT_LIMIT or a succeeded produce job, or
           every build job is blocked/failed for ABANDON_TICKS (36000) since blocked_at_tick
```

| Phase | Rule |
|---|---|
| design | `recipe_tree(stocked=products)`; `_Grid` places products with most stocked ingredients first; a machine stands 3 tiles from every product chest it touches (its own, its stocked ingredients'), one side each, input chest on a free side (4 sides at most); a machine touching no placed chest goes below the cell; non-stocked intermediates chain off the input chest (`_place`); raw = `progression.RAW_ITEMS` are hand-fed ports; greedy poles; `render` draws it |
| site | 8-tile ring around the powered pole, nearest first; refused: overlap with a board block, planned-unbuilt hardware, a site-plan zone (`SiteService.world().zones()`), ore or buildings (native survey); trees/rocks are cleared |
| build | `ensure_plan`, bar `OUTPUT_STACKS` (2) on each product chest, first feed for `PROOF_UNITS` (2), proof: every machine finished products, every product chest holds stock |
| use | `ensure_item` collects from the product chest; above `HANDCRAFT_LIMIT` it waits when `(missing - stock) * wait_s <= WAIT_FACTOR * missing * hand_s` (`construction_supply._waiting_pays`), else hand crafts; before a hand craft with no finished stock to collect it takes the stocked direct ingredients the craft lacks (`intermediate_take`, batched `_take_output`); an unfed cell (`starved`) is fed from hand first; no progress for `STALL_TICKS` = hand crafting resumes |
| feed (pre-production) | target per input chest = `min(FEED_CAP 100, ceil(need * raw per unit))`, need = demand - product stock - carried, demand = the whole upcoming demand, so stock for the next steps is made while the current one waits; a plate that another active job waits for (`contested_plates`: reason "waiting ... <plate>" or evidence item) feeds only `current` demand and is never collected from furnaces by the idle lane; a trip only when the deficit is >= `FEED_BATCH` (10) or covers the whole target; shared upkeep inserts only plates beyond `FEED_RESERVE` (50); no feed for a product whose `produce` row is producing |
| retire | feeding stops (`phase: retiring`); waits while any job waits for "machine-made <product>", and at most `DRAIN_TICKS` (3600) while input chests of a barred cell still hold plates; mines inserters, machines, chests (paid, guarded `mine`, contents return to the inventory); keeps poles (other chains may hang on them); removes board block `supply_cell`; `phase: retired` is never proposed again |

Keep the estimate honest: `DemandTests.test_builds_constants_cover_the_offline_plans`
compares `Step.builds` with offline plans (`smelting_site.district_candidate`,
`board.startup_science_cell`) and the live plan range. Pinned by `tests/test_v2_supply_cell.py`.

## Research capacity (`research_capacity.py`, `ResearchCapacityConfig`)

```
lab time  = units * unit_seconds / (labs * lab_speed)          lab_speed = 1.0
pack time = 60 * max(0, units * per_unit - stock) / rate + one craft cycle
T = max(lab time, pack times), summed over the queue (current + next roadmap technologies, horizon 4)
option (a, l) = extra assemblers for the limiting pack, extra labs, each 0..max_step
T(a, l) = fund + (a * machines_per_assembler + l) * build_seconds + T_queue(new hardware, plate-capped rate)
expand when T(0,0) >= min_remaining_seconds, saving >= min_saving_seconds, T(a,l) <= max_ratio * T(0,0)
```

| Field | Default |
|---|---|
| `max_step` | 2 |
| `max_assemblers` / `max_labs` | 3 / 4 (startup cell: `board.MAX_STARTUP_SCIENCE_MACHINES = 2`, `MAX_STARTUP_LABS = 4`) |
| `build_seconds_per_machine` | 20.0 |
| `min_saving_seconds` / `max_ratio` / `min_remaining_seconds` | 60.0 / 0.85 / 120.0 |

`recommended_science_rate`: `rate = floor(min(30, min_items net / raw_per_pack))`,
`labs = ceil(rate * sum(units*unit_seconds) / (60 * lab_speed * sum(units*packs)))`.
Used by the observe receipt (`research_capacity_receipt`) and by
`industry.py` (`_capacity_decision`, `_decided_labs` with
`labs_without_assemblers`) for the startup science cell. Pinned by
`tests/test_v2_research_capacity.py`, `tests/test_v2_science_cell_extension.py`.

## Staged red science (`science_growth.py`, capability `science_growth`)

`automation_science` reserves the whole block (all requested machine slots,
eight lab slots, belts for the full row) on the board, but builds only a start:

```
labs     = START_LABS (2)
machines = clamp(ceil(lab_pack_rate(queue, 2 labs) / pack_rate_per_machine), 1, min(START_SCIENCE_MACHINES (2), plate-supported machines))
proof    = built machines made packs, built labs powered and one holds packs or works
```

Unbuilt slots stay in the `automation_science`/`laboratories` blocks (occupied for
every planner) and are listed in `automation-science.json` `stage.growth_entities`;
slots are built as a prefix, so every subset is one pole network. Growth rule
(`growth_decision`, observe receipt `science_growth`, autopilot `science_growth_nodes`
at `SCIENCE_GROWTH_PRIORITY = TOP_PRIORITY - 500`, below every roadmap step):

```
no growth unless research runs and every built lab/machine is observed and powered
idle     = some built lab status != working
backlog  = lab-row belt holds >= BELT_BACKUP_PACKS (8) packs or every lab >= LAB_BACKUP_PACKS (2) or a machine is full_output
starving = some lab missing_science_packs and the lab belt is empty
short    = some machine item_ingredient_shortage / no_ingredients
may add labs     = not idle and backlog and free lab slots
may add machines = starving and not short and machines < plate-supported limit
how many: plan_research_capacity(queue, observed hardware + pack stock,
          max_labs = labs + free if may add labs else labs,
          max_assemblers = machines + free if may add machines else machines)   (max_step 2)
```

A lab is never added while a built lab is idle. The startup cell (`industry._extend_labs`,
`_grow_cell`) adds no slot lab while research runs and a powered lab is idle without packs.
Pinned by `tests/test_v2_science_growth.py`, `tests/test_v2_template_power.py`.

## Native research queue (`research_queue.py`)

The labs never wait between technologies: the game's own research queue is
kept filled. `plan_queue` (pure) orders up to `RESEARCH_QUEUE_SIZE = 7`
technologies, current research included:

```
running research first (never interrupted or switched)
then unlock paths, prerequisites first, of: roadmap-wanted technologies in roadmap
  order (next 6 steps, incl. STEP_TECHNOLOGY), technologies unlocking wanted recipes,
  upgrades.PRIORITIES; then the cheapest finite supplied technology, repeated
a technology enters only if enabled, not a trigger technology, every prerequisite
  researched or earlier in the plan, every pack in NormalResearch.supplied_packs
no supplied packs -> empty plan: nothing is queued
```

Action `research_queue {technologies}` (`DeterministicGame.act`): writes current
research, the listed technologies, then the rest of the existing native queue,
skipping (and reporting in `skipped`) what the force cannot research in that
order; the receipt `research_queue` is the queue read back. `observe` reports
`research_queue` beside `research`.

Who writes it:
- idle lane (`executors.idle_action`, at most every `IDLE_RESEARCH_INTERVAL_TICKS = 600`):
  when a planned technology is missing and the native queue has room. Skipped
  while an active bootstrap research-owning job exists (capability other than
  `research`) or a funded bootstrap batch runs its technology.
- roadmap `research` step for T (`NormalResearch.step`): T researched -> succeeded;
  T anywhere in the native queue -> waiting; else one `research_queue` action
  with T right behind the running research, then the rest of the plan (even when
  the queue is full of fillers).
- autopilot proposes no job for a research step whose technology is already in
  the native queue (`progression._natively_queued`); it is satisfied once researched.

Bootstrap phase: no queue. Finite hand-funded batches (`industry.BootstrapLogistics`,
`StarterResearch`) pay for exactly one technology's `unit_count`; a queued successor
would consume leftover packs and the batch accounting, so they keep the one-at-a-time
`research` action until machine science proves an automatic supply.

Research capacity (`research_capacity.queue_targets`, used by the receipt and
`science_growth`): current research, then the native queue, then roadmap technologies.

Pinned by `tests/test_v2_research_queue.py`, `tests/test_v2_idle_research.py`.

## Policy preferences (`research_policy.choose_research`)

Rank: (a) first researchable step on the unlock path of a wanted technology
or recipe (next 6 roadmap steps); (b) same for `upgrades.PRIORITIES`; (c)
cheapest finite (`unit_count * pack kinds`). Only packs with proven supply
(`NormalResearch.supplied_packs`), never trigger technologies. The queue plan
above uses the same ranking repeatedly.
Pinned by `tests/test_v2_research_policy.py`.

## Batching and hand-craft constants (`construction_supply.py`, `industry.py`)

| Constant | Value | Rule | Test |
|---|---|---|---|
| `HAUL_MINUTES` | 0.5 | one hauling trip carries this much of the item's measured production (`construction_supply.haul_batch`): plates between `COLLECT_BATCH` 10 and `PLATE_BATCH` 50, coal fetch between 24 and `FUEL_FETCH` 48, burner fill between 8 and `FUEL_FILL` 20; no production counter -> the floor | `test_v2_construction_supply.test_plate_trips_grow_with_measured_production` |
| `FUEL_BATCH` | 8 | fetch fuel only when 8 coal piled up at a running coal drill; fuel collection for any job below 8 waits | `test_v2_fuel_batching` |
| `FUEL_SKIP_STOCK` | 50 | no fuel for a cell holding 50 finished items; such cells still count as productive in `StarterCapacity.proof` | `test_v2_fuel_batching` |
| `COLLECT_BATCH` | 10 | `take_many` waits until 10 items are on hand (or less is needed, or a verified buffer chest) | `test_v2_construction_supply`, `test_v2_machine_stock` |
| `MAX_ITEMS` / `MAX_SOURCES` | 50 / 16 | one `take_many` envelope (`validate_take_many`) | `test_v2_construction_supply` |
| `HANDCRAFT_LIMIT` | 10 | above it, with a producing `produce` row or supply cell, collect or wait instead of hand crafting | `test_v2_machine_stock`, `test_v2_supply_cell` |
| `machine_stock.STALL_TICKS` | 3600 | a row without products for this long is `stalled` (hand crafting resumes) | `test_v2_machine_stock` |
| `industry.SCIENCE_COURIER_BATCH` | 5 | bootstrap science carried per lab per trip | `test_v2_research_capacity` |
| `DEDICATED_COAL_MINIMUM` | 4000 | coal under a dedicated power drill | none yet |

## Build ahead (`core/deterministic_builder.FactoryBuilder.ensure_plan`)

```
for entity in plan order:
  existing -> continue
  item missing -> result = ensure_item(item)
    if result is a plain "waiting" status (no action):
      build the first later entity whose item is carried and whose footprint is free
      (not a mining drill or inserter: they keep plan order; <= BUILD_AHEAD_CHECKS = 4 placement checks)
    else return result
```

A job waiting for one item no longer holds back what it can already place.
Pinned by `tests/test_v2_build_ahead.py`.

## Early start (`early_start.py`, "pre-build")

A roadmap block starts once its inputs are planned, not finished: the saved
plans of its prerequisites fix its geometry, and most of its hardware needs no
new research. Declared per capability in `jobs.CAPABILITIES[c]["early_start"]`:

| Capability | `requires` (admission) | `running` | `inputs` | `locked_until` |
|---|---|---|---|---|
| `smelting_district` | power | - | `mining_bank_ports`: per source (iron 60, copper 30, coal 30) a saved bank, built or not, whose port is on the board | logistics |
| `planned_main_bus` | power | smelting_district | `district_feed_anchors`: district plan registered, the belt under each bus feed splitter built | logistics |

```
propose step S early (progression.autopilot_nodes -> early_start.early_ready) when the strict rule fails and
  early.requires succeeded, every S.after step satisfied or in flight, every early.running succeeded or active,
  receipt early_start_inputs[S.capability].known, and every locked_until technology researched or coming
  (research, native queue, technology of an active job, STEP_TECHNOLOGY), native gate unlockers within locked_until
  priority unchanged: TOP_PRIORITY - index, below every step it runs ahead of
admit (jobs.submit, WorkQueue.refresh): early.requires instead of requires;
  an early job never takes the last free slot (EARLY_FREE_SLOTS = 1: at most 3 active when it is admitted)
build (ensure_plan(defer=early_start.deferral(...))), per plan entity in order:
  recipe locked by a coming technology            -> left for later, "waiting for research <t> to unlock <recipe>"
  hand craft would use a plate another active job waits for (contested_plates; only while early) -> later
  acquisition only waits                         -> later (the next item is tried: furnaces from stone go on)
  mining drill / inserter behind a deferred entity -> later (plan order)
  everything else is crafted and placed; all done but deferred -> "waiting for deferred plan entities: ..."
succeed only on the unchanged native proof (district: plates on the output ports; bus: plates on lane tails);
  the bus splice into the district waits for smelting_district to succeed (planned_bus.step)
clocks: while early (a strict requires missing) the job deadline does not run (early_ticks/early_seconds);
  the stall rule keeps its progress key (entities, research progress), which research or the prerequisites move
demand: an early job's saved plan counts in supply-cell demand but not as current (contested plates never feed it)
inputs final: a bank port is final once the bank is saved (exit side chosen before saving; bridge re-plans keep
  ports); the district records source_ports and, when one is no longer a saved bank port, re-plans if nothing of it
  is built (board block dropped) or blocks with smelting_inputs_changed_after_partial_build
```

Without early inputs in the receipt the roadmap behaves as before. Other
candidates for the same declaration: `automation_science` (needs the bus's
published taps; its tap cutover splices the bus, so it would need a splice
hold like the bus), `logistic_science` (after its research), `steel_production`
(after steel processing), `dedicated_materials_*`. Pinned by
`tests/test_v2_early_start.py`.

## Supervisor and incidents (`supervisor.py`, `incidents.py`, `IncidentConfig`)

| Kind (severity order) | Opens when | Default |
|---|---|---|
| `empty_queue` | no active job, no pending/ready node, roadmap unfinished | `idle_grace_seconds = 60` |
| `blocked_job` | a job stays blocked (auto-retry restarts the timer) | `blocked_grace_seconds = 60` |
| `no_progress` | jobs active, no success for `progress_minutes = 10`, top two actions >= `repeat_share = 0.5` of the last 100 | |
| `planner_review` | periodic | `review_minutes = None` (off) |

Episode closes `resolved` or `expired` after `outcome_minutes = 10`; one model
consultation per incident per `incident_retry_seconds = 300`, none sooner than
`decision_interval` (60) after the last; document capped at
`DOCUMENT_CHAR_CAP = 24000`; at most `MAX_NEW_NODES = 3` per `add_nodes`.
`AUTO_RETRY_LIMIT = 3` retries per job. Pinned by `tests/test_v2_incidents.py`,
`tests/test_v2_supervisor.py`.
