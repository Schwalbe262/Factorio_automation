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
- A step with a `gate` also needs `progression.gate_ready` (only `machine_made_belts`
  of `power_coal_feed`); a step with a fixed `priority` uses it instead of `-100 - index`.
- Tune: `ROADMAP` order/`after`/`builds`; pinned by `tests/test_v2_progression.py`.

## Critical path and item allocation (`critical_path.py`, used by `jobs.run_jobs`)

One computed definition for every stage of the run (no listed early-game steps):

- Satisfied step: a matching job succeeded or its technology is researched
  (`STEP_TECHNOLOGY` too). On-path step: no fixed `priority` (a fixed priority is
  the roadmap's way of keeping a step off the path, e.g. `power_coal_feed`).
- Dependencies are computed: `after`; the first roadmap step of each `requires`
  capability without a succeeded job; the steps researching each unresearched
  catalog prerequisite of the step's technology; the steps researching a
  technology that unlocks a locked `native_recipes` recipe.
- Frontier = first `CRITICAL_HORIZON = 3` unsatisfied, on-path, not stalled steps
  in roadmap order that do not wait on an unsatisfied off-path or stalled step.
  Critical steps = frontier + their transitive unsatisfied on-path dependencies.
- Critical jobs = active jobs matching a critical step, plus active jobs whose
  `parameters.products/item/product` supply a non-raw item a critical job claims
  (a critical job never waits on a starved slack supplier). Everything else
  (starter capacity, supply cell, produce rows, growth, retirements) is slack.
- Claims (items in general): catalog item names in a critical job's reason,
  `evidence.item` (reserving `evidence.need` units when given) and
  `evidence.needs` `{item: units}`. A job states what it waits for this way.

Allocation rule, applied per pass in `run_jobs`: critical jobs advance first (in
priority order), then slack jobs (in priority order). A slack job plans against
`slack_view`: the claimed units are removed from the character's inventory and
claimed items from machine/furnace outputs and belts; any slack `take`/`take_many`
of a claimed item, an `insert`/`build` that would cut into the claimed units, or a
`craft` that spends an item claimed without a unit count becomes a wait
(`slack_refusal`, reason "slack work yields <item> to the critical path"). Research
jobs and other controllers that store their own transfer intent are not refused
(`GUARD_EXEMPT`), only given the view. So: while a critical job waits for a plate,
no capacity expansion, supply cell feed or pre-crafting takes that plate; capacity
uses plates nobody on the path is waiting for. The node priorities above still
order dispatch and the slack jobs among themselves.

Measure it: `python -B tools/critical_path.py runtime/v2/<name> --from 0 --to 30
--at 10` (critical path at given minutes, per critical job submitted/first
action/end and wait reasons, actor seconds on critical vs slack work, lab idle
seconds after power). Tool limits: technologies researched by the native queue
without a job are not seen; waits are attributed per slice end (25 s).

## Starter power in two phases (`power_coal_feed.py`, `FactoryBuilder.ensure_power` / `ensure_coal_feed`)

The dedicated coal drill and its belt (95 and 141 hand-crafted belts on two
live maps) kept `power` busy for minutes while research waited. A player
fuels the first boiler by hand and lays the belt once belts are machine-made.

```
power            plant built (pump, boiler, engines, poles); coal_plan chosen and saved but not built (reservation);
                 boiler fuelled by the upkeep; success = water, steam, boiler fuel, 2 connected engines for 1800 ticks;
                 evidence hand_fed: true, coal_feed: planned|unplanned            builder state power_mode: hand_fed
upkeep           while power_mode is hand_fed (StarterExecutors.starter_upkeep and idle_action, before maintain_fuel):
                 burn   = boiler energy_usage * 3600 / coal fuel_value          (catalog; base 27 coal/min at 1.8 MW)
                 refill when slot coal + burning remainder < max(8, ceil(burn * 30 s))   (base 14)
                 fill   = min(coal stack * 1 fuel slot, ceil(burn * 2 min)) - slot coal   (base: to 50)
                 insert when carried >= min(fill, 8) or the boiler is out; else ensure_item(coal) from the coal chests
feed step        roadmap power_coal_feed (after power, before electric_coal), gate machine_made_belts, priority -500:
                 receipt coal_feed_belts (power_coal_feed.feed_belt_receipt): belts carried + in the supply cell's
                 belt chest >= the saved coal_plan's belts (100 without a plan), the cell's job need not have succeeded,
                 or a succeeded supply_cell with transport-belt or a succeeded produce row for it,
                 or FEED_FALLBACK_TICKS (54000) after power, or an old map's power job (feed evidence, not hand_fed)
                 -> ensure_coal_feed builds coal_plan, proves power_flow_ready for 1800 ticks -> power_mode: belt_fed
after            hand fuelling stops; electric_coal (requires power_coal_feed) replaces the burner drill
```

| Rule | Where |
|---|---|
| saved state without `power_mode`: `coal_plan` present = belt-fed (old maps keep the old full path), absent = hand-fed | `deterministic_builder.coal_feed_mode` |
| the reserved feed is board block `power_coal_feed` (entities, no ports, reserved area = burner drill footprint joined with the 3x3 site of its electric replacement) while hand-fed and, once belt-fed, until `electric_coal` replaced the drill; an old map without the block gets none, and a site whose 3x3 meets an earlier block keeps the 2x2 | `power_coal_feed.publish_planned_feed`, `feed_block`, `upgrade_pending` |
| the reserved feed is not in `known_project_construction_budget` until the feed step started (`coal_feed_started`) | `power_coal_feed.feed_in_bill`, `diagnostics.construction_budget` |
| a route built over before the feed started is re-planned (at most `COAL_FEED_REPLANS` 2); the re-plan surveys every fringe cell again, so a taken cell fails its preflight and the next one is tried | `FactoryBuilder.ensure_coal_feed` |
| while nothing of the feed stands, the whole saved feed is preflighted first (`_coal_feed_taken`): a taken tile anywhere on the route re-plans before the drill end is built (seed 2005199778 built drill and half the belt, then met the starter lab); "direction differs" (another owner's drill on the feed drill tile) re-plans like "obstructed" (`COAL_FEED_TAKEN_REASONS`) | `FactoryBuilder.ensure_coal_feed` |
| the feed planner reads `board.starter_keepouts` without its own block (board areas, planned-unbuilt tiles, pending starter cells): cells on them are dropped before the candidate cap, the route treats them as occupied (crossing underground allowed), a route on them is refused | `power_layout.feed_keepouts`, `V2FactoryBuilder._choose_coal_plan` |
| candidate cells: cells whose fuel arm or outlet stands on a known coal site are dropped before the cap of `STARTER_COAL_CANDIDATES` (256); before, the richest-first cap kept 2 of 50 fringe cells on seed 2644599892, both under starter cells, and the feed was never planned (`no_boundary_coal_cell_with_complete_resource_avoiding_boiler_route`) | `power_layout.fringe_coal_cells` |
| one reservation rule for planners that take free land near power: new starter cells (`discover_cell`, `StarterCapacity.choose_site`), the starter lab (`StarterResearch.lab`) and the coal feed avoid every board block's reserved area, every planned-but-unbuilt entity tile and every pending starter cell (`board.pending_starter_cells`: chosen, drill not yet built); the startup science cell avoids other blocks' reserved areas too | `board.starter_keepouts`, `KEEPOUT_LUA`, `Board.reserve_startup_science` |
| a new mining bank (`MiningOutposts.prepare` -> `planned_keepouts`) treats other plans' unbuilt tiles (board blocks, every saved bank incl. unpublished bridge poles of the parallel lanes, pending starter cells) as reservations, and a candidate whose reserved area meets a registered block or whose hardware meets a planned tile is skipped (`candidate_failures` names the block); a bank that still cannot be published blocks only its own lane, and an unbuilt one is planned again (`MAX_BANK_REPLANS` 2) | `outposts.planned_keepouts`, `publish_reservations(obs, resource)`, `_replan_conflicting_bank`, `mining_layout_policy.prepare_new_bank(registered, planned)` |
| `POWER_BUILDS` holds the plant and the first fill (50 coal); `POWER_COAL_FEED_BUILDS` the drill, arm and about 100 belts | `progression.py` |

Pinned by `tests/test_v2_power_coal_feed.py`, `tests/test_v2_planned_land.py`.

## Starter capacity (`capacity_planning.plan_capacity`, `CapacityConfig`)

For each raw item with evidence in `resource_supply_options` (rates per minute):

```
required = max(0, shortfall) + max(0, lookahead - max(0, available - required_bill))
T(0)     = 60 * required / net                      (inf when net <= 0)
T(k)     = fund + cross_fund + k * build_seconds + 60 * (required + k * own_cost) / (net + k * gain)
gain     = cell_rate  (coal: cell_rate - fuel_per_drill_per_minute)
expand k when T(0) - T(k) >= min_saving_seconds and T(k) <= max_ratio * T(0)
coal cells needed = ceil(fuel_margin * (smelting cells * 3.6 + drill-only cells * 2.25 + hand-fed boiler) / (cell_rate - 2.25)), at least 1
hand-fed boiler = power_coal_feed.hand_fed_coal_per_minute (0 once the feed step succeeded)
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

Around the electric-mining-drill unlock (`mining.closed_by_unlock`, `CELLS_AFTER_UNLOCK = 2`):

```
drill researched  -> iron/copper targets are capped at 2 cells (no node when 2 stand); plate growth is
                     mining_outpost + smelting_capacity (throughput_growth)
drill researching -> no new iron/copper cell node (it would finish at the unlock and be closed)
open job, target > 2 after the unlock -> succeeds as closed: reason starter_capacity_closed_by_electric_mining_unlock,
                     proof {closed_by_technology, target_cells, productive_cells}; paid cells and the pending site stay
coal/stone        -> unchanged; a chest receiver the collector emptied proves by its drill's native status
                     (working / waiting_for_space_in_destination)
```

Fleet pace1 (8 seeds): 9 plate nodes were proposed at or after the unlock and blocked
(`bulk_starter_capacity_requires_electric_mining_after_unlock`), several with 0 actions holding a
job slot for 3-12 minutes (1193005910: copper 5 at 18.9-22.3 kept the early district out of its slot
until 22.7, two minutes after the outposts); two coal-2 jobs ran 30 minutes into `job_deadline_exceeded`
because their chest was collected empty before every proof. Pinned by
`tests/test_v2_starter_stage_policy.py`, `tests/test_v2_progression.py`.

## Produce (`progression.produce_nodes`)

```
propose item when bulk_item_demand[item].count > BULK_THRESHOLD (20) and no produce job/node for it
rate = round(min(count / COVER_MINUTES (5.0), PRODUCE_MAX_RATE (600), min_p net_p / raw_per_item_p), 1)
skip when any plate net is unmeasured or <= 0, or rate < PRODUCE_MIN_RATE (1.0)
```

`bulk_item_demand`: `Step.builds` of the next 4 steps. Pinned by `tests/test_v2_produce_autopilot.py`.

Line design (`production_layout.plan_sources`): at most two routed solid
inputs; an unsourced one-level intermediate gets a sub-row; two unsourced
intermediates made from the same single sourced input get twin sub-rows on
one mixed belt (engines: steel + gears + pipes). Declared input demand
(`input_demand`, the tap's and a board-port connection's `demand_per_minute`)
counts sub-rows only at the main row's draw and scales to the requested rate
when the built nameplate is higher (a stock row stops when its chest is full).

## Steel block growth (`progression.steel_capacity_nodes`)

```
receipt furnace_block_claims (planned_furnaces.furnace_block_claims), kind steel:
  nominal = built furnaces x nameplate, full = every reserved slot, claimed = sum demand_per_minute of
  board connections whose source is the block's product port
propose steel_capacity {rate_per_minute: round(min(full, claimed x SMELTING_HEADROOM 1.25), 1)}
  when steel_production succeeded, claimed >= SMELTING_GROW_AT (0.8) x nominal, nominal < full,
  no open/stalled steel_capacity row and no succeeded one at >= target; priority SMELTING_CAPACITY_PRIORITY
```

Pinned by `tests/test_v2_planned_products.py`.

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
retire one  else node auto_supply_cell_retire_<item> {"retire_product": item} once per item, priority -1000, when
           a produce job for it succeeded and no active job waits for its cell stock (waits_on_cell)
```

| Phase | Rule |
|---|---|
| design | `recipe_tree(stocked=products)`; `_Grid` places products with most stocked ingredients first; a machine stands 3 tiles from every product chest it touches (its own, its stocked ingredients'), one side each, input chest on a free side (4 sides at most); a machine touching no placed chest goes below the cell; non-stocked intermediates chain off the input chest (`_place`); raw = `progression.RAW_ITEMS` are hand-fed ports; greedy poles; `render` draws it |
| site | 8-tile ring around the powered pole, nearest first; refused: overlap with a board block, planned-unbuilt hardware, a site-plan zone (`SiteService.world().zones()`), ore or buildings (native survey); trees/rocks are cleared |
| build | `ensure_plan`, bar `OUTPUT_STACKS` (2) on each product chest, first feed for `PROOF_UNITS` (2), proof: every machine finished products, every product chest holds stock |
| use | `ensure_item` collects from the product chest; above `HANDCRAFT_LIMIT` it waits when `(missing - stock) * wait_s <= WAIT_FACTOR * missing * hand_s` (`construction_supply._waiting_pays`), else hand crafts; before a hand craft with no finished stock to collect it takes the stocked direct ingredients the craft lacks (`intermediate_take`, batched `_take_output`); an unfed cell (`starved`) is fed from hand first; no progress for `STALL_TICKS` = hand crafting resumes |
| feed (pre-production) | target per input chest = `min(FEED_CAP 100, ceil(need * raw per unit))`, need = demand - product stock - carried, demand = the whole upcoming demand, so stock for the next steps is made while the current one waits; a plate that another active job waits for (`contested_plates`: reason "waiting ... <plate>" or evidence item) feeds only `current` demand and is never collected from furnaces by the idle lane; a trip only when the deficit is >= `FEED_BATCH` (10) or covers the whole target; shared upkeep inserts only plates beyond `FEED_RESERVE` (50); no feed for a product whose `produce` row is producing |
| retire one product | job blocks until the item's produce row is `producing` (`rows_stock`), waits while a job waits for its cell stock, then records `retired_products[item]` (no stock hook, no own feed, no `intermediate_take`); `dismantled_products` = retired products no remaining product takes as a stocked input (transitively), so a shared gear machine stays and is still fed for a remaining belt/inserter machine; `owned_entities` from the node graph (machines of its nodes, inserters 2 tiles from them, its input and product chests) are mined inserters first (room check, guarded `mine`, chest stock to the inventory), then dropped from the plan and the board block (ports, `required_items`, `reserved_area` shrink) |
| retire | feeding stops (`phase: retiring`); waits while any job waits for "machine-made <product>", and at most `DRAIN_TICKS` (3600) while input chests of a barred cell still hold plates; mines inserters, machines, chests (paid, guarded `mine`, contents return to the inventory); keeps poles (other chains may hang on them); removes board block `supply_cell`; `phase: retired` is never proposed again |

Keep the estimate honest: `DemandTests.test_builds_constants_cover_the_offline_plans`
compares `Step.builds` with offline plans (`smelting_site.district_candidate`,
`board.startup_science_cell`) and the live plan range. Pinned by `tests/test_v2_supply_cell.py`.

## Starter retirement (`starter_retirement.py`, capability `starter_retirement`)

Hand-served burner cells stop being served once a permanent source exists, then
are mined. Seed 2099054105: after outposts (21-23 min), district (31.9) and bus
(35.5) the character still fuelled and emptied 16 starter cells (43% of 818
actions were hauling).

```
permanent(r)   every group of PERMANENT_SOURCES[r] has a succeeded job (verified evidence):
                 iron-ore, copper-ore: smelting_district AND planned_main_bus
                 stone: mining_outpost{stone} OR dedicated_materials{stone}
                 coal:  electric_coal AND (mining_outpost{coal} OR dedicated_materials{coal})
retired(r)     permanent(r) and the board shows it (bus trunk carries the plate / a mining bank outputs r);
               coal also needs succeeded starter_retirement of COAL_LAST_AFTER (iron-ore, copper-ore, stone):
               coal cells fuel every other starter cell, so they go last
cells(r)       recorded once (starter-retirement.json): burner drill -> stone-furnace / chest, not electric,
               not dropping on a belt (the power coal drill), not board-registered, not builder-owned;
               linked = an inserter picks from or drops into drill or receiver (LINK_RADIUS 4): kept and fuelled
upkeep         retired(r): unlinked cells(r) get no fuel (V2Bootstrap.maintain_fuel), no new cell (_ensure_cell),
               no starter_capacity node (progression.capacity_nodes)
node           auto_starter_retirement_<r> once per retired r, priority RETIRE_PRIORITY (-1000), order RESOURCES
job step       collect: plates take_many from the cells' furnaces (<= 16 sources, <= 50) when >= COLLECT_BATCH (50)
                        are waiting or every drill stopped; chests: take <= 50 when >= 50 or the drill stopped
               mine:    a drill with no fuel item and not working first, then a receiver that is not working
                        and holds no output (furnace) / no r (chest); guarded mine (unit + world), contents and
                        fuel return to the inventory; an inserter found just before mining keeps the cell
               else waiting starter_retirement_waits_for_cells_to_cool
proof          no unlinked recorded cell of r stands
```

Construction plates after the bus (`collection_points`): the trunk tiles of each built lane
from the last tap window (`along + WINDOW_ROWS + SURPLUS_GAP`) to the tail hold only surplus
plates (nothing is downstream). `V2Bootstrap._unreserved_stock_observation` makes them public
for their plate, `_take_output` ranks them before every other plate source and takes a whole
haul batch (`max(missing, haul_batch)`, <= 50) in one guarded `take_many`. No new hardware: a
chest tap would need a site, power and an inserter (~50 plates/min) for what a backed-up trunk
already holds (8 plates a tile; 33 tiles on the live iron lane). Pinned by `tests/test_v2_starter_retirement.py`.

## Obsolete blocks (`obsolete.py`; startup science: `science_retirement.py`, capability `startup_science_retirement`)

Temporary equipment is torn out and reused once a permanent successor is verified. One rule,
shared helpers in `obsolete.py` (`succeeded`, `active_jobs`, `proposed`, `slots_needed`,
`room_action`, `guarded_mine`, `release_block`, `mark_retired`, `retire_node`, `RETIRE_PRIORITY`);
each module keeps its own trigger and evidence. `obsolete.REGISTRY` lists every case.

```
obsolete(B)  successor(B) verified (succeeded jobs + live/board evidence, per module)
guards       nothing still depends on B: no active job waits for it (reason marker / capability),
             no funded unfinished work inside it, inventory room for what mining returns
             (InventoryStorage deposit, else wait obsolete_block_needs_inventory_room)
dismantle    one paid guarded mine per entity (unit number + world), contents to the inventory
release      board block removed (one atomic write); record phase: retired (never rebuilt)
node         proposed once (a blocked job is auto-retried, never proposed twice), RETIRE_PRIORITY (-1000)
```

| Block | Successor verified | Module |
|---|---|---|
| supply cell | every product has a succeeded `produce` row or demand <= `HANDCRAFT_LIMIT`; one product alone when its `produce` row is producing | `supply_cell.py` |
| starter cells of r | `PERMANENT_SOURCES[r]` + board source; coal last | `starter_retirement.py` |
| startup science cell + slot labs + starter lab | see below | `science_retirement.py` |
| hand-fed boiler fuelling | dedicated coal belt (phase switch, nothing mined) | `power_coal_feed.py` |
| later (registry only) | legacy power coal belt after `electric_coal`; private plate lines after bus taps; stone furnaces of the first bank after steel furnaces; burner pieces of outposts after electric banks | - |

Startup science cell:

```
trigger   automation_science succeeded
          and receipt science_growth.evidence: labs_powered >= 1, machines_observed >= 1, some lab "working"
          and research is running                                  (what science_growth reads)
          and no starter_research / bootstrap_research / bootstrap_logistics job open (paused counts)
          and every roadmap step of those capabilities succeeded or its technology is researched
node      auto_startup_science_retirement once, priority RETIRE_PRIORITY;
          SCIENCE_GROWTH_PRIORITY + 1 when science_growth.grow (move before growth crafts)
job       blocked startup_science_retirement_batch_unfinished while industry.batch_refusal (funded batch,
            unresolved transfer, prewarm) - never dismantle paid plates
          record once: startup_science entities without poles, its lab slots, the starter lab
            (starter-research.json lab_site), batch-bound labs; never an entity of another board block
          room: slots_needed(entities + contents) empty slots, else InventoryStorage.step(minimum_slots)
          mine inserters, assemblers, labs, chests (guarded); phase dismantling
          then release startup_science, phase retired; carried red packs -> insert lab_input of the
            permanent built lab with the fewest packs (< 200), else they stay carried
reuse     recovered labs/assemblers are carried stock: ensure_item counts the inventory before
          crafting, so science_growth places them when its evidence rule grows the row; until then
          they stay in the inventory (labs are not in inventory_storage.KEEP); while the job is
          active and dismantling a lab/assembler it recorded, science_growth waits
          (science_growth_waits_for_moved_startup_hardware); a blocked retirement never holds it
never     industry refuses to reserve or build the cell once dismantled (bootstrap_research_startup_cell_retired)
```

Saving: a 2-machine cell with 4 labs returns 4 labs (40 circuits, 40 gears, 16 belts), 3 assemblers,
6 inserters, 3 chests; growth to 4 labs then needs no lab craft. Pinned by `tests/test_v2_science_retirement.py`.

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
`labs_without_assemblers`) for the startup science cell. Its queue is the batch's
technology plus the upcoming technologies of the roadmap's bootstrap steps
(`industry.bootstrap_technologies`: what the temporary cell researches back to back),
never red-only research the permanent science serves; it is decided at the prebuild,
so the decided machines and labs stand before the first batch binds. Pinned by
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

## Plate throughput growth (`throughput_growth.py`, capabilities `mining_outpost`, `smelting_capacity`)

Ore and furnaces of the paired district follow upcoming plate demand. Seed
2644599892 (min 28-41): iron 2 drills (60/min) under 4 furnaces (75/min),
copper 2 furnaces (37.5/min) under 60/min of ore, `planned_main_bus` waited 8+
minutes for plates; `smelting_capacity_nodes` never fired (taps claimed only a
stock row, and the ore cap hid the furnace target) and nothing grew drills.

```
per plate (iron, copper), H = DEMAND_HORIZON_MINUTES (10):
demand   taps  = declared demand of built bus taps, minus production_row_* (stock rows: already in the bill)
         bill  = shortfall + max(0, lookahead - spare stock)           (capacity_planning, incl. hand collection)
         work  = bill + taps * H;  demand = work / H
supply   capacity = min(built furnaces nominal, connected ore nominal, 2 x lane belt)   (installed units at nominal:
                    a step never looks like a deficit while it ramps)
         measured = production counter (reported only; it includes starter cells)
         signals  = built furnaces idle for ore | full output, drills waiting for belt space
settle   any open mining_outpost/smelting_capacity job of the resource, or one finished < SETTLE_TICKS (3600) ago -> wait
covered  capacity >= demand * COVER_MARGIN (1.1)                         -> no growth
stage    furnace output full -> demand (none); idle furnaces + backed drills -> transport (none);
         furnaces idle for ore (>= STARVED_SHARE 0.5) -> ore; drills backed (>= BACKED_SHARE 0.5) -> furnaces;
         else the smaller nominal stage; equal -> ore first; capacity at the belt -> belt (next unit: another bank)
step     ore:      mining_outpost {resource, rate: ore + 2 x drill rate}  (one pair in reserved_pair_slots;
                   a full bank gets a connected module from outposts.prepare_connected_bank)
         furnaces: smelting_capacity {resource, rate: min(2 (per_side + 1) x furnace rate, ore)} (one slot per row)
                   every slot built -> no growth, reason paired_bank_full_next_growth_is_another_bank
         when the other stage binds next below demand, the step is valued together with that stage's step;
         only the first is proposed, the second is re-decided after the first is measured
pays     T0 = 60 work / capacity
         T1 = fund (other inputs: stock then production, wood 2 s) + trips 30 s + 3 s per entity
              + 60 (work + own plate cost) / (capacity + gain)
         grow when T0 - T1 >= 60 s and T1 <= 0.85 T0
node     auto_throughput_<resource>_<capability>_<rate>, after smelting_district succeeded, one per resource,
         never again after the same step stalled;
         bottleneck_blocks_critical_path(plate) (an active job waits for the plate or "district plates")
           -> critical: priority SMELTING_CAPACITY_PRIORITY (-52), proposed before the roadmap
           -> slack:    SCIENCE_GROWTH_PRIORITY + 1 (-599), after the roadmap
```

Receipt `throughput_growth.plates[]` holds demand, supply, signals, bottleneck,
step (unit, gain, cost), times (wait, expand, saving) and the reason.
`bottleneck_blocks_critical_path` is the single swap point for the shared
critical-path rule. Furnace growth adds coal burn (1.35/min per stone furnace)
that this rule does not size. Pinned by `tests/test_v2_throughput_growth.py`.

After the bus splice a bank's output belt under the bus feed splitter exists only in the district
plan; `SmeltingDistrict.proof` reads each bank through the plan (pace1: every `smelting_capacity`
job raised `KeyError(('transport-belt', x, y))` once the bus was spliced). Pinned on the real
geometry of seed 3794581402 by `tests/test_v2_smelting_growth_after_splice.py`.

## Threat-sized defense (`defense_policy.py`, `defense_perimeter.py`, capabilities `defense_perimeter`, `defense_growth`)

Posts follow the live threat, never a fixed list (doc: `DEFENSE_POLICY.md`):

```
risk  = assess_risk(survey): enemies within 96 tiles of a producer, evolution, damage, tier
posts = 0                                   quiet and evolution < QUIET_EVOLUTION (0.1)
      = tier reserve (early 0, mid 1, adv 2) quiet and evolution >= 0.1
      = 2 / 3 / 4                           approaching (<=64) / high (<=40, damage) / critical
quiet and posts == 0: defense_perimeter succeeds "quiet map, no posts required"
each new post:
  focus     = producers ranked (damaged, nearest threat, density), FOCUS_TRIES (8) tried
  forbidden = board reserved areas + port exits + site_plan_areas (zones, strips,
              corridors, allocations, taps); relaxed only as a fallback: the zone/strip
              hosting the focus may take it, bus/rail/corridor never
  site      = legal native candidate covering the focus, nearest the threat, then the focus
growth:
  other job's background step: build at once only if risk level >= URGENT_RISK_LEVEL (2)
  else receipt defense_growth.grow = risk.minimum_posts > policy active posts
    -> defense_growth_nodes: one {"posts": N} node at DEFENSE_GROWTH_PRIORITY
       (SCIENCE_GROWTH_PRIORITY - 1), requires defense_perimeter succeeded, id bucketed
       per DEFENSE_GROWTH_PERIOD_TICKS (36000) so it is re-evaluated periodically
no site plan for this world: legacy POSTS / FUTURE_AREAS (development map only)
```

Pinned by `tests/test_v2_defense_sizing.py`, `tests/test_v2_defense_policy.py`.

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
`research` action until machine science proves an automatic supply. The labs still
roll straight on: the next batch is funded and produced while the current one is
researched (next section).

## Back-to-back bootstrap batches (`science_pipeline.py`, `BootstrapLogistics.funding_complete` / `prewarm`)

One research-owning job at a time stays the rule; the next batch is funded by the
current owner job (its waits) as a *prewarm*, which cannot start research.

```
owner O ready   = O.funding_complete: bound, no pending transfer, allocated == required (repairs too),
                  machines + labs powered, research == O, O consumed > its baseline        (packs may still be in production)
successor S     = approved work-queue node, pending/ready, deps done or O, highest priority:
                  bootstrap_logistics {} or bootstrap_research{technology} with a finite red-only technology
S baselines     = O's batch end: products_finished = O.baseline + ceil(O.packs / yield), consumed = O.baseline + O.packs
                  (rollback floors = counters observed at reservation: prewarm.observed_*)
S budget        = held + public furnace output - units other active critical jobs claim (claim without units: all)
                  (no lookahead construction bill: S is the next critical research step)
S funding       = balanced packets of PREWARM_PACKET = 5 packs (or the remainder), copper first; else wait
S active        = research S first, then the rest of its funding (a missing remainder is a plate wait)
next pair       = S becomes owner; the consumed reservation (target == owner or researched) is replaced
guard           = while S's prewarm is unresearched only O (same guard hash) and S use the cell (any target file)
```

Where the minutes go (`tools/critical_path.py`, three saved maps): logistics started
0.03-0.4 min after the drill research but its batch was funded only after every drill
pack was in the labs, in 1-2 plate packets on seed 2644599892 (23.6-24.35); now it is
funded from about the drill research's first consumption. Pinned by
`tests/test_v2_science_pipeline.py`.

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
| `mining_outpost` | power | - | `mining_zones`: the site plan has a `mining:<resource>` zone with an output side for iron, copper and coal | electric-mining-drill |
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
coming (technologies_coming): research, native queue, technology of an active job, and the technology of a roadmap
  research step proposed next (not satisfied/stalled, every after satisfied or in flight, admission requires
  succeeded): right after power automation is in flight, so electric-mining-drill and logistics count as coming
runs early (runs_early): a strict requires missing OR a locked_until technology unresearched (the outposts' only
  "early" is their drill research): no deadline, contested plates held, and a hand craft of an item a supply cell
  makes or is about to make (machine_made_items) waits for its machine-made stock
parked (early_start.parked): ensure_plan's deferred wait whose reasons are all research (or plan order behind it)
  reports evidence.research_only; such a waiting job keeps its lane but holds no job slot
  (jobs._admission_error, supervisor._dispatch); it is still stepped every pass
outposts: one lane per resource (PER_RESOURCE_CAPABILITIES); while electric-mining-drill is coming the bank is
  prepared, saved and published (port final), collector belts, poles, bridge and exit route are built, drills
  (sorted last in resource cells) wait; blocked electric_mining_research_required only when it is not coming;
  proof unchanged (ready only after drill output); an early outpost is not an "existing capacity task" for
  supply_planning, so starter capacity for its resource still grows
```

Chain from power on: outposts (3 lanes, park on the drills) -> district (inputs = the banks'
published ports; parks on splitters/undergrounds) -> bus (inputs = district anchor belts built,
which the early district lays; its splice still waits for `smelting_district` to succeed).
A consumer may cut a tap into a built trunk while the bus still builds: the bus builds and splices
`planned_bus.build_plan` (its plan with every built tap applied), never its old trunk belt under
the tap splitter; a tap's own belt on a removed trunk tile is tap hardware in `planned_tap.cutover`
(pace1: two buses blocked "reserved block placement is obstructed", one red science row blocked
`planned_bus_tap_trunk_identity_changed`). Two input ports of one item get two tap names
(`<owner>:<item>`, `<owner>:<item>#2`): the bus state keys taps by name.
District site search: a search that ran out of `PlanningBudget` (`planning_budget_exhausted`)
waits and searches again instead of blocking; the supervisor also auto-retries a `no_clear_*`
block after `AUTO_RETRY_STALL_SECONDS`, and a retry refused for a job slot is not counted
against `AUTO_RETRY_LIMIT` (fixpace1 seed 2692229611: two slot refusals used up the district's retries).
Science cell prebuild (`BootstrapLogistics.prebuild`, called from `starter_research`
while it only waits): the research slot is held by `starter_research`, so the cell is
not a separate early job; the automation job builds it with the same `Deferral`.

```
when   starter_research(automation) waits, research == automation, not researched; next target =
       first unresearched finite bootstrap step (industry.bootstrap_technologies), no batch state, cell not retired
size   _capacity_decision now (machines + labs for the bootstrap queue), reserve the cell with that size
build  cell entities with assemblers last + decided slot labs; Deferral(coming={automation}) leaves the
       assemblers (recipe unlocked by automation) for later; blocked -> prebuild_blocked recorded once
after  automation researched: the batch crafts only the assemblers, binds, funds incrementally, researches
```

Measured (4176033748): drill batch submitted 14.86, first action 16.51, cell built
16.51-17.63, funded 17.65; automation ran 12.95-14.84 with the actor free. The
prebuild moves the chests, inserters, poles and slot labs (about 1 min) into the
automation research. Not before power: the cell site is surveyed from energized
poles and power is the critical consumer of iron then. Pinned by
`tests/test_v2_science_cell_prebuild.py`.

Without early inputs in the receipt the roadmap behaves as before. Other
candidates for the same declaration: `automation_science` (needs the bus's
published taps; its tap cutover splices the bus, so it would need a splice
hold like the bus), `logistic_science` (after its research), `steel_production`
(after steel processing), `dedicated_materials_*`. Pinned by
`tests/test_v2_early_start.py`, `tests/test_v2_early_outposts.py`.

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
