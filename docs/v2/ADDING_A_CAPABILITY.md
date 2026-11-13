# Adding a capability

Worked example: `produce` (`src/factorio_agent/production_rows.py`, commit 0be9b3d).
Copy its shape. Read `ARCHITECTURE.md` first.

## 1. Controller contract

One class, constructed once per `run` command (executors are rebuilt every
slice, so all state lives in files, never only in memory).

| Method | Returns | Rules |
|---|---|---|
| `prepare(obs, parameters)` | `{"ok": True, ...state}` or `{"ok": False, "reason": ...}` | pure planning: design, site, routes, power, preflight. No `act`. Read-only `game.query` is allowed |
| `step(obs, parameters)` | one **action dict** or one **status dict** | called once per pass with a fresh shared observation |
| `proof(obs, parameters)` | evidence dict or `None` | independent live postcondition on a *fresh* observation; must not trust saved state |

- **Action dict**: has `"type"` (`build`, `build_many`, `insert`, `take`,
  `take_many`, `craft`, `recipe`, `bar`, `move`, `mine`, `research`, `wait`, ...;
  see `DeterministicGame.act` and `WorldBoundGame.act`). `_advance_job`
  stores it as `pending_action`, sends it, records the receipt. Return at most
  one action per call and never send it yourself.
- **Status dict**: no `"type"`, `"status"` in `running | waiting | blocked |
  failed | succeeded`, plus `"reason"` (stable snake_case, it is what the
  report and incidents show) and optional `"evidence"`. Anything else raises
  `executor returned no recognized action or status` (`jobs.py:772`).
- `succeeded` is only a claim: `_advance_job` re-observes and calls `proof`;
  `None` makes the job `blocked / postcondition_not_verified`.
- `waiting` must name what it waits for; a job without a progress change for
  18000 ticks blocks (`objective_progress_stalled`, `jobs.progress_key`).
- Never let `proof` require a state another rule forbids (see
  `DEBUGGING_A_STALL.md`, starter_capacity row).
- Reuse `self.builder.ensure_plan(obs, plan)` to build: it returns the next
  paid action (collect/craft/move/build/recipe) or a status, and runs the
  resource policy check (`power_layout.py:295`). Items come from the actor's
  inventory only (`V2Bootstrap.ensure_item`).

## 2. Register it

1. `CAPABILITIES["<name>"]` (`jobs.py:19`): `parameters` (human-readable
   contract), `requires` (capabilities that must have *succeeded*),
   optional `native_recipes` (gates autopilot until enabled), `effect`.
2. `StarterExecutors.__init__` (`executors.py:38`): construct the controller
   with the shared objects it needs (`self.builder`, `catalog`,
   `site=self.site`), or add a lazy `_<name>()` factory. Never create a
   second `SiteService`.
3. A handler in the matching domain module `src/factorio_agent/handlers/<domain>.py`
   (each <= 400 lines; `handlers/__init__.py` lists them), registered at its bottom:
   `register("<name>", step=<name>_step, proof=<name>_proof, validate=validate_<name>)`.
   Handlers take the executors object as `ex`, e.g. `produce` in `bus_and_industry.py`:
   `early, _ = shared_upkeep(ex, obs, "<name>")`; `if early is not None: return early`;
   then `return ex.<ctl>.step(obs, parameters or {})`. Add
   `ex.builder.ensure_power(obs)` first if the block needs live power.
   `handlers/upkeep.py` holds the shared upkeep (`shared_upkeep`,
   `defense_upkeep_while_waiting`); `StarterExecutors.step` (`executors.py:734`)
   only runs the common prelude (pending repair, initial defense) and dispatches.
4. `proof(ex, obs, result, parameters)`; without one the generic
   `executors.flow_proof` applies. `validate(parameters)` checks structure only
   (raise `ValueError`); without one the capability must take `{}`.
   `record_action` is only for controllers that track receipts.
5. If it selects a technology, put `technology` in its `parameters` (idle
   research then yields) and add it to `RESEARCH_CAPABILITIES` (`jobs.py:428`).
6. `tests/test_v2_capability_registry.py` fails until `CAPABILITIES` and the
   registry agree.

## 3. Get a site (never fixed coordinates)

Follow `production_site.place_line`:

1. Build the layout in canonical coordinates as `build(x0, y0) -> plan`
   (entities with `board.entity(...)`, `ports` with `item`, `position`,
   `facing`, `direction`, and a `reserved_area`).
2. `site.allocate_candidates(kind, along, depth, name=owner, count=4,
   returns_to_bus=..., world_id=obs["world_id"])` (`site/service.py:173`), or
   the wrapper `site.placement.allocated_placements` (`site/placement.py:39`).
3. Orient: `site.fit.orientations(build, size, block.rect,
   site.placement.block_wants(block, inputs, outputs))`; inputs face the
   block's tap band, outputs the block's `output_point`. Use
   `allowed=lambda t: t.mirror is None` when lane order matters.
   `site.transform.apply` / `Transform.inverse()` keep a canonical copy for
   later extension (`production_rows._extend`).
4. Re-audit the turned plan, skip candidates overlapping any board
   `reserved_area`.
5. Keep the chosen block with `site.placement.settle(site, world_id, owner,
   block.name)`; on failure `settle(..., None)` releases all candidates.

Legacy controllers (`science.legacy_science_candidates`,
`green_science.LEGACY_GREEN_ANCHORS`, `brick_production.prepare(anchors=...)`,
`steel_production.prepare(anchors=...)`) still carry development-map
coordinates. Do not copy them. Red and green science use theirs only when
the controller has no site service; with one, an empty allocation is a
blocked reason (`green_science_needs_deeper_district`, ...).

A route may end inside a registered board block only at one of its
registered, built ports (`transport._production_keepouts`). A consumer
that taps or feeds another block (green's plate splitter branch, its lab
entry) extends its chain to a port just outside that block's reservation.

## 4. Route, power, preflight

In this order, against the same entity list (`production_site.py:116-129`):

| Step | Call |
|---|---|
| input belts | `transport.plan_connection(game, builder, obs, source_port, input_port, reserved_entities)`; sources from `production_sources.resolve(board, product, item, owner=...)` |
| plates on the planned bus | `planned_tap.connect_inputs(...)` plans taps + routes with the block; in `step` call `planned_tap.publish` and `planned_tap.cutover` before `ensure_plan` (see `MAIN_BUS.md`, taps); no route may enter the bus band |
| merge | `smelting.merge_entities(entities, route["entities"])` |
| power | `manufacturing.connect_power_groups(game, builder, obs, groups, entities)` |
| whole-block check | `planning.preflight(game, builder, obs, entities)` (overlap, ore keepout, owned-entity configuration, clearable trees/rocks, no tree cap: `site/nature.py`) |
| plan | `core.deterministic_builder._plan(entities, ports, reserved_area=..., clear_obstacles=..., clearance_world_id=obs["world_id"], underground_pairs=...)` |

## 5. Persist state, register the block

- One JSON file per instance in `game.cfg.runtime_dir`
  (`production-row-<item>.json`), written with `core.deterministic_state._atomic_json`.
  Store `world_id` and `catalog_fingerprint`; on mismatch return
  `blocked` (`production_rows.py:285`).
- Save the prepared state *before* the first action, so a restart continues
  the same plan instead of re-planning a different site.
- Publish the plan in `pcb-board.json` `blocks["<prefix>_<instance>"]` and its
  `connections` (`ProductionRows._publish`). Output ports listed there are
  what `V2Bootstrap._unreserved_stock_observation` lets other jobs collect.

## 6. Add it to the roadmap (`src/factorio_agent/progression.py`)

`Step("<id>", "<name>", {params}, after=("<earlier step id>",), builds={item: count})`
in `ROADMAP` order. `builds` is the main hardware it places; it feeds
`lookahead_demand` (capacity planner) and `bulk_item_demand` (produce
planner). Without it later demand is under-estimated. Demand-driven
capabilities (like `produce`) get a `*_nodes` function called from
`autopilot_nodes` instead of a fixed step. `tests/test_v2_progression.py`
checks ids, `after` order, `requires` and that every step validates.

## 7. Test offline

| Need | Pattern |
|---|---|
| job runner | scripted `Game` + `Executor` (`tests/test_v2_concurrent_jobs.py`) |
| controller | `SimpleNamespace(cfg=SimpleNamespace(runtime_dir=tmp), query=Mock())`, `builder=Mock()` with `ensure_plan.return_value`, a `FakeSite.allocate_candidates` returning `site.planzones.Allocation` (`tests/test_v2_production_rows.py`) |
| catalog | small dict catalog helper (`tests/test_v2_production_layout.catalog`) |
| map / site plan | `site.fakeworld.make_world(seed)` + `site.masterplan.plan_site` over seeds 1..12 (`tests/test_v2_site_masterplan.py`) |
| Lua bodies | lupa `LuaRuntime` executing the snippet (`tests/test_v2_planning.py`); `site.fakegame.FakeSurveyGame` for surveys |
| fixed geometry | `tests/test_v2_brick_production.py` (audit of a template) |

Test the action/status shapes, the proof on a fresh observation, the
restart path (state file present) and world/catalog change.

## 8. Before commit

1. `powershell -ExecutionPolicy Bypass -File tools/check.ps1 test_v2_<yours> test_v2_capability_registry test_v2_jobs test_v2_progression`, then the full `tools/check.ps1`.
2. `git diff --stat`; review only your hunks.
3. `git add` the exact files. Never `git add -A`/`.`: `oil_gathering.py` and
   `water_gathering.py` carry frozen uncommitted WIP and must stay unstaged.
4. Update `HANDOFF_CURRENT.md` (<= 10 lines) and append one event to `note.md`.
