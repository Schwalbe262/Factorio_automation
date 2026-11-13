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
| `max_cells` / `max_plate_cells` | 8 / 8 | cap for coal / plates and stone (was 4 for plates: starved the early game on seed 283895142; the time comparison against lookahead demand decides below the cap) |
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

## Idle research choice (`research_policy.choose_research`)

Rank: (a) first researchable step on the unlock path of a wanted technology
or recipe (next 6 roadmap steps); (b) same for `upgrades.PRIORITIES`; (c)
cheapest finite (`unit_count * pack kinds`). Only packs with proven supply
(`NormalResearch.supplied_packs`), never trigger technologies; at most every
`IDLE_RESEARCH_INTERVAL_TICKS = 600` (`jobs.py:594`).
Pinned by `tests/test_v2_research_policy.py`, `tests/test_v2_idle_research.py`.

## Batching and hand-craft constants (`construction_supply.py`, `industry.py`)

| Constant | Value | Rule | Test |
|---|---|---|---|
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
