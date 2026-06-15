# Factorio Automation CLI Handoff

Last updated: 2026-06-15 14:19 KST
Repository: `C:\Users\NEC\Documents\Factorio`
GitHub: `https://github.com/Schwalbe262/Factorio_automation`
Current branch: `master`
Part 72 baseline before this handoff update: `92915d2 Part 71: select manual site improvement targets`

## Goal

Build a vanilla-compatible Factorio autoplayer that can eventually launch a rocket under LLM high-level control.

The intended control split is:

- LLM: high-level strategy, bottleneck reasoning, site layout critique, candidate layout design, failure analysis.
- Deterministic skills: exact walking, mining, crafting, building, inserter/belt placement, power connection, research execution.
- Web monitor: production targets, estimated rates, site graph, logistics links, threats, power networks, token usage, LLM logs, layout candidates.
- Slurm worker: run local Qwen models and background layout simulations while the deterministic executor is busy.

Important user preferences:

- Show GUI demonstrations when needed, but headless/no-GUI is acceptable for fast implementation tests.
- Vanilla/no-mod compatibility is preferred for multiplayer compatibility. Current path uses no-custom-mod RCON/Lua command execution rather than a Factorio mod folder.
- Do not use instant teleport/mining for GUI demos; GUI should look like real gameplay.
- User wants `/factorio` and `/팩토리오` links from Telegram/Kakao bots, with external URL `http://27.115.156.173:8787/factorio?lang=ko&objective=launch_rocket_program`.
- Completed parts should be committed and pushed to GitHub.
- Each completed task should mention approximate Codex token usage delta.

## Current Runtime State

Processes observed before this handoff:

- No-mod Factorio server wrapper: `python -m factorio_ai.cli start-no-mod-server`
- Factorio server: `factorio.exe --start-server ... --rcon-port 27015 --rcon-password factorio-ai`
- Web dashboard: `python -m factorio_ai.cli web --host 0.0.0.0 --port 18889`
- Local web URL: `http://127.0.0.1:18889/factorio?lang=ko&objective=launch_rocket_program`
- External web URL through existing forwarding/proxy: `http://27.115.156.173:8787/factorio?lang=ko&objective=launch_rocket_program`

## Latest Part 74 Status

- The previous live no-mod map was backed up because factory sites had become too scattered for the automation-first logistics goal:
  - Backup: `runtime/vanilla/saves/backups/no-mod-rcon-scattered-20260615-001123.zip`
- A fresh no-mod world is now running:
  - Save: `runtime/vanilla/saves/no-mod-rcon.zip`
  - Initial observe after final reroll: tick 2679, spawn `{x:0,y:0}`, starter inventory `burner-mining-drill:1`, `stone-furnace:1`
  - Observed cliffs: 0
  - Initial strategy: `produce_iron_plate`, priority 96
  - Nearest observed resources: copper about 52 tiles, stone about 5 tiles, iron about 100 tiles, coal about 107 tiles
- Future no-mod world generation disables Nauvis cliffs:
  - `cliff_settings.richness = 0`
  - `cliff_elevation_interval = 0`
- `note.md` and `insight.md` now use loop/insight templates:
  - `note.md` records every exploration, validation, documentation, UI, strategy, and execution loop as `Loop N`.
  - `insight.md` records only confirmed improvements as `Insight N`; failures and hypotheses stay in `note.md`.
- Part 74 also keeps the useful code improvements found on the scattered map:
  - `research_logistics` can repair/reuse an existing lab-adjacent remote steam block instead of failing on starter-local power selection.
  - The red science mall can recover a lab-adjacent powered unassigned assembler.
  - Automation-era repeated hand-carry between distant sites is blocked; strategy now raises missing site-to-site logistic lines to `plan_factory_site`.
- Next priority on the fresh map: bootstrap compact starter-local iron/coal/copper/power, then build research and mall sites close enough for short belts or explicit logistic lines.

## Latest Part 83 Status

- Nearby water was present on the fresh map but the earlier full power-site scan missed it because `find_tiles_filtered(... radius=1024, limit=1600)` clipped the water sample before sorting by distance.
- Power-site discovery now scans staged radii (`96`, `160`, `224`, `384`, `640`, then `1024`) and returns the nearest buildable tier before considering far water.
- Live verification found a starter-local steam layout around pump `{x:-45.5,y:19.5}` and `setup_power` completed in 7 steps. The resulting offshore pump, boiler, steam engine, and pole are working near the starter cluster.
- The starting crashed spaceship/wreckage is protected by default. Mining it requires an explicit `allow_preserved_artifact=true` override; planner obstacle-removal code also skips protected starter artifacts.
- Slurm continuity is now handled by `slurm-ensure-worker --renew-before-minutes <minutes>`, which queues a dependent successor job before the current one expires instead of waiting for the allocation to disappear.

## Latest Part 84 Status

- Slurm renewal is now part of the normal controller heartbeat, not a one-off manual command. Autopilot start, strategy decisions, idle layout start/cycles, and background layout submissions call `ensure_worker_job` with throttling.
- The launchers set `FACTORIO_AI_SLURM_RENEW_BEFORE_MINUTES=360` and `FACTORIO_AI_SLURM_RENEW_CHECK_INTERVAL_SECONDS=1800`, so a successor should be queued up to 6 hours before the current 1-day job expires and rechecked every 30 minutes.
- `CopperPlateSkill` no longer uses pickaxe mining for `copper-ore`. Before belt automation it builds a direct burner drill -> stone furnace copper cell; after a transport-belt assembler is observed, copper can move to belt smelting lines.
- `StoneSupplySkill` is now implemented for the early burner drill -> chest stone pattern, so furnace/drill prerequisites can bootstrap stone without repeated hand stone mining.

If the CLI session starts fresh, verify processes first:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -match 'factorio.exe|python.exe' -and ($_.CommandLine -like '*factorio*' -or $_.CommandLine -like '*Factorio*') } |
  Select-Object ProcessId,Name,CommandLine | Format-List
```

Start web dashboard if missing:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli web --host 0.0.0.0 --port 18889
```

Start no-mod server if missing:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli start-no-mod-server
```

## Recent Completed Work

Latest continuation after Part 105:

- Part 105 extended the long iron route without hand-carry and kept repeated gear/belt inputs behind site-to-site logistics.
- Part 106 added the site placement cost model for the over-distant gear/belt mall, including belt-route cost, relocation cost, power-pole corridor estimate, and power expansion clearance risk.
- Part 107 adds deterministic follow-through for that model:
  - `relocate_gear_belt_mall_to_iron_source` refuses to tear down existing mall assemblers until the required small-electric-pole corridor materials are available.
  - `bootstrap_power_pole_mall` lets Qwen/strategy choose pole automation before relocation when the corridor is short on poles.
  - `research_electric_mining_drill` and `bootstrap_electric_mining_drill_mall` let the strategy replace burner drills after Automation/stable power instead of extending burner mining longer than necessary.
  - Layout capability payloads now include long-handed inserter and module availability, and planner simulation candidates can rerank into long-handed green-circuit and starter-mall variants with `layout_unlocks_considered` and `uses_unlocked_items` evidence.
- Part 108 makes unlock-aware layout optimization react to usable recipes as well as technology flags:
  - Live no-mod Factorio reports `long-handed-inserter` recipe enabled while `long-inserters` technology is absent, so observation now includes `recipe_unlocks` for long inserters, higher-tier assemblers/furnaces, modules, beacons, and power distribution items.
  - Planner, strategy payload, compact Slurm payload, and the web dashboard now distinguish `considered_unlocked_items`, `uses_unlocked_items`, and `unused_unlocked_items`.
  - Green-circuit candidates can use long-handed inserters from recipe unlock state and switch blueprint/rate estimates to `assembling-machine-2/3` when available.
  - Smelting candidates can switch blueprint/rate estimates to `steel-furnace` or `electric-furnace` when available.
  - The dashboard candidate cards show an `Unlock-aware` row so the operator can see which unlocked tools were considered and which were actually used.
  - Full verification: `pytest -q` -> `492 passed`.
- Part 109 tightens unlock-aware layout optimization and sustained power guardrails:
  - `unlock_layout_reassessment` now appears when newly available layout tools can make existing sites obsolete, including long-handed inserters, higher-tier assemblers/furnaces, modules, and beacons.
  - `factory_layout_simulation_candidates` adds a generic `unlock-aware-site-rerank-*` candidate so idle Slurm/Qwen layout loops see retooling work even when a site does not yet match a specialized green-circuit, smelting, or mall pattern.
  - Live no-mod payload now reports `long-handed-inserter` as `available=true`, `recipe_unlocked=true`; top candidates include `green-circuit-long-handed-3-cable-2-circuit-cell` and `unlock-aware-site-rerank-long-handed-inserter`.
  - Strategy capability detection now treats `assembling-machine-2/3` recipe automation as automation evidence, not only `assembling-machine-1`.
  - Critical powered factories now force `setup_power` before electric research/science/layout work, and gear/belt mall power blockers keep the specific `gear/belt mall power` label as well as `factory power`.
  - `SetupPowerSkill` requires boiler fuel reserve before reporting done, so transient steam does not hide an empty boiler.
  - Live Slurm worker is ready on `Qwen/Qwen3.5-4B`, but local `no-mod-strategy --require-llm` still fails if local LLM env vars are unset; use/check the remote worker path for required-Qwen strategy loops.
  - Full verification: `pytest -q` -> `499 passed`.
- Part 110 fixes the required-Qwen strategy path for direct CLI use:
  - `strategy_decision(..., require_llm=True)` now automatically tries the ready remote Slurm worker when local `FACTORIO_AI_LLM_BASE_URL`/`FACTORIO_AI_LLM_MODEL` are unset, even if `FACTORIO_AI_SLURM_ENABLED` was not exported in the current shell.
  - This preserves local-only behavior when local LLM env is configured, and can be disabled with `FACTORIO_AI_REQUIRE_LLM_AUTO_SLURM=0`.
  - Live smoke without local LLM env now succeeds: `python -m factorio_ai.cli no-mod-strategy --require-llm` returned `source=llm` from remote task `strategy-0d5deda1486e444d963b51bdd9c91e94`.
  - The active remote model remains `Qwen/Qwen3.5-4B`; Qwen selected `bootstrap_build_item_mall` while heuristic support still reported the current layout blocker as incomplete site logistics.
  - Full verification: `pytest -q` -> `501 passed`.
- Part 111 prevents Qwen from starting a new build-item mall cycle when existing site inputs are still broken:
  - If Qwen selects `bootstrap_build_item_mall` while existing factory sites have missing/incomplete producer-to-consumer input links, transport-belt production is not automated, and no assembler stock is available for a safe local bootstrap, strategy is guardrailed back to `plan_factory_site`.
  - Live smoke changed Qwen's raw mall choice into `plan_factory_site` with `hand_carry_seed_risk=true`, `transport_belt_automation_ready=false`, and `assembling_machine_1_inventory=0`.
  - This is intentionally a no-hand-carry safety guard, not a full logistics executor; the next missing feature is an executable general site-to-site logistics correction for copper/gear/cable inputs.
  - Full verification: `pytest -q` -> `502 passed`.
- Part 112 makes unlock-aware layout candidates explicit about build-item supply:
  - Long-handed inserters, modules, higher-tier machines/furnaces, and beacons remain automatic layout inputs via `layout_unlocks_considered`, but candidates now also record `used_unlocked_item_state` so Qwen can see `recipe_unlocked`, `stock`, and `automated` separately.
  - Blueprint-backed candidates now include `build_item_supply`, including missing unlocked tools such as `long-handed-inserter x7`, so the UI and Slurm/Qwen payload distinguish "use this better layout" from "first automate/supply the build items".
  - The web dashboard candidate cards now render a `Build items` row next to `Unlock-aware`.
  - Live read-only smoke: current no-mod state reports `long-handed-inserter` as `available=true`, `recipe_unlocked=true`, `stock=0`, `automated=false`; compact Qwen payload preserves that state and the long-handed green-circuit candidate's missing build items.
  - Related verification: `pytest -q tests/test_planner.py tests/test_slurm_worker.py tests/test_web_dashboard.py` -> `220 passed`; full verification `pytest -q` -> `503 passed`.
- Part 113 turns unlock-aware layout supply and repeated site input gaps into executable strategy paths:
  - `long-handed-inserter` is now part of build-item supply planning only when actually available by research, recipe unlock, stock, or an existing assembler.
  - Qwen/heuristic `bootstrap_build_item_mall` decisions can preserve `target_item=long-handed-inserter`, and the controller passes that target into `BuildItemMallSkill` instead of always defaulting to transport belts.
  - `SiteInputLogisticLineSkill` builds general producer-to-consumer belt routes for repeated inputs such as copper plates after transport-belt production exists; before belt automation, strategy routes the issue to `build_gear_belt_mall_logistics`.
  - The gear/belt mall's iron-plate route remains on the specialized `build_iron_plate_logistic_line_to_gear_mall` path, and generic site input logic excludes build-item mall iron/gear missing-source noise so target smelting deficits can still preempt layout diagnostics.
  - Full verification: `PYTHONPATH=src pytest -q` -> `509 passed`.
- Part 114 preserves the exact site-input item from strategy through execution:
  - `build_site_input_logistic_line` decisions now keep a dedicated `input_item` from Qwen normalization, guardrail fallback, heuristic fallback, controller `_skill_run_config`, and `SiteInputLogisticLineSkill`.
  - Generic site-input issue selection now includes `manual_site_logistics_gap` kind text and breaks severity ties by item priority, so copper/iron plate gaps are not hidden by lower-priority derived intermediates when both are present.
  - Live read-only verification shows `long-handed-inserter` is considered by layout optimization from recipe unlock state (`available=true`, `recipe_unlocked=true`, `stock=0`, `automated=false`); top candidates include `green-circuit-long-handed-3-cable-2-circuit-cell` and `unlock-aware-site-rerank-long-handed-inserter`.
  - Live mutation was intentionally not run because the current live blocker is boiler 272 `no_fuel`, and the existing `setup_power` path would manually insert coal. Next missing executor: an automation-first boiler fuel feed using coal belt/inserter logistics.
  - Full verification: `PYTHONPATH=src pytest -q` -> `510 passed`.
- Part 115 adds automation-first boiler coal feed handling:
  - `CoalFuelFeedSkill` now has a boiler-first branch that routes a coal output belt to a boiler-side feed belt, places a burner inserter, primes only that inserter when a starter fuel item is already carried, and waits for belt-fed coal in the boiler.
  - `SetupPowerSkill` delegates boiler refueling to the boiler coal feed path before falling back to generic direct burner fueling, so an existing coal output belt no longer sends the player to mine/insert boiler coal manually.
  - Strategy can preempt ready boiler `no_fuel` recovery to `connect_coal_fuel_feed` when coal supply and belt automation are both ready.
  - Live read-only verification: boiler 272 remains `no_fuel`, `belt_assembler_count=0`; both `CoalFuelFeedSkill` and `SetupPowerSkill` now return `boiler coal feed needs automated transport-belt production or existing belt stock; refusing repeated boiler hand-fueling` with no mutation.
  - Full verification: `PYTHONPATH=src pytest -q` -> `517 passed`.
- Part 116 adds a bounded emergency bootstrap path for the current no-belt-stock power deadlock:
  - `SetupPowerSkill` can now take at most 5 surplus fuel from an existing fuel source or insert at most 5 already-carried fuel into a zero-fuel boiler when the normal boiler coal feed route is blocked by missing belts/inserters.
  - The emergency action is labeled `emergency_bootstrap`, refuses direct resource mining, and only runs when a critical electric factory exists, so it does not become a normal repeated boiler-fueling loop.
  - Live read-only verification: current action is `move_to {x:113,y:18}` with reason `move near surplus fuel source for one-time emergency power bootstrap`; the source is the existing coal drill with 9 coal and the target remains boiler 272.
  - Full verification: `PYTHONPATH=src pytest -q` -> `518 passed`.
  - Immediate follow-up after emergency power returns: build/restore transport-belt automation, then build the boiler coal feed route so steady-state fuel is belt/inserter fed.
- Part 117 verified unlock-aware layout reranking:
  - No code change; this loop confirmed that live layout candidates already consider `long-handed-inserter` from recipe unlock state.
  - Live state: `available=true`, `recipe_unlocked=true`, `researched=false`, `stock=0`, `automated=false`.
  - Live candidates include `green-circuit-long-handed-3-cable-2-circuit-cell` and `unlock-aware-site-rerank-long-handed-inserter`, but they correctly remain not build-ready because long-handed inserters and other build items are missing.
  - Full verification: `PYTHONPATH=src pytest -q` -> `518 passed`.
- Part 118 hardens Qwen attach and creates the next executable belt-automation step:
  - Attached Slurm task execution now retries transient `TimeoutExpired`, SSH, and srun job-step failures, and the remote script can retry when the task file was already moved or a result file already exists.
  - `BuildItemMallSkill("transport-belt")` can now reuse a powered `small-electric-pole` mall assembler when no new assembler is available, small pole stock is sufficient, and the candidate is not part of the circuit assembler cluster.
  - Before setting the reused assembler to `transport-belt`, incompatible contents such as `copper-cable` are cleared.
  - Live read-only verification changed the transport-belt mall decision from `action=null; cannot find a powered or wireable site` to `take copper-cable x4 from assembler unit 318 before setting transport-belt`.
  - Full verification: `PYTHONPATH=src pytest -q` -> `521 passed`.
  - Next live step: run the Qwen/no-mod loop with a small cap so unit 318 is cleared and retooled, then use belt output for the boiler coal feed route.
- Part 119 preserves the live belt assembler while bootstrapping gear input:
  - Unlock-aware layout optimization already considers `long-handed-inserter` automatically from technology, recipe unlock, stock, or an existing assembler, and candidate metadata separates `uses_unlocked_items` from missing build-item supply.
  - The current live belt mall then exposed a separate executor issue: after unit 318 was retooled to `transport-belt`, the nested iron-gear bootstrap tried to retask that same belt assembler back to `iron-gear-wheel`.
  - `BuildItemMallSkill("iron-gear-wheel")` now prefers non-belt powered assemblers before considering any transport-belt assembler, and preserves the only belt assembler when transport-belt output is not ready.
  - `BuildItemMallSkill("transport-belt")` can set the nearby gear assembler recipe before another short emergency power window, so remote Qwen latency does not force repeated boiler fueling before the mall is staged.
  - Strategy guardrails now route `plan_factory_site` or `setup_power` to `bootstrap_build_item_mall target_item=transport-belt` when a belt assembler exists and a nearby non-belt assembler can be retooled to gears.
  - Live Qwen read-only changed from repeated `setup_power` to `bootstrap_build_item_mall target_item=transport-belt`, preserving unit 318 and selecting unit 537 for gear retooling. A capped live step then set unit 537 to `iron-gear-wheel` through the `AI/server` virtual agent.
  - Full verification: `PYTHONPATH=src pytest -q` -> `526 passed`.
  - Current live blocker after this part: unit 537 now needs iron plates, but the nearest source is about 149 tiles away, so hand-carry is correctly refused. Next work should solve this with placement/logistics cost handling rather than manual transfer.
- Part 120 starts the costed gear/belt mall relocation safely:
  - `GearBeltMallRelocationSkill` now computes the relocation power-corridor positions and builds missing `small-electric-pole` placements before mining the existing gear/belt mall assemblers.
  - The executor refuses to tear down the current mall while the corridor is incomplete, checks for blockers at the next corridor position, and requires enough pole stock for the remaining corridor.
  - Strategy small-pole deficit calculation now uses the executor's actual missing corridor count when available, so Qwen does not choose a relocation step that the executor will immediately reject for missing poles.
  - Strategy/heuristic guardrails now route repeated `setup_power`, `plan_factory_site`, build/research, and gear/belt mall `no_power` cases to `relocate_gear_belt_mall_to_iron_source` when relocation is cheaper than a 100+ tile pre-belt iron-plate route and pole supply is sufficient.
  - Live capped mutation placed the first relocation corridor pole: requested `{-29.0, 6.0}`, placed unit 599 at `{-28.5, 6.5}` through the `AI/server` virtual agent, without moving `r1jae`.
  - Live read-only after the no-power guard fix: Qwen raw `bootstrap_build_item_mall` was guardrailed to `relocate_gear_belt_mall_to_iron_source` with `source_distance_tiles=149.1`, `belt_route_cost=150.0`, `relocation_cost=56.0`, and `small_electric_pole_deficit=0`.
  - Full verification: `PYTHONPATH=src pytest -q` -> `531 passed`.
  - Current next action: continue the relocation corridor toward the iron source, then mine/rebuild unit 537 (`iron-gear-wheel`) and unit 318 (`transport-belt`) near unit 395. After transport belts exist, build the steady-state boiler coal feed route.
- Part 121 completes the live gear/belt mall relocation before reboot:
  - Relocation power-corridor placement now detours around protected crash-site spaceship/wreckage and uses nearby valid pole placement instead of failing on exact blocked tiles.
  - Strategy keeps `relocate_gear_belt_mall_to_iron_source` selected when power recovery itself waits on belt mall output, and while relocation is in progress after teardown or partial rebuild.
  - The executor no longer requires spare pole stock after the actual missing corridor count reaches zero.
  - Live mutation through the `AI/server` virtual agent mined old unit 537 (`iron-gear-wheel`) and unit 318 (`transport-belt`) only after the corridor existed, without moving `r1jae`.
  - The mall was rebuilt near the iron source: unit 664 at `{94.5,-69.5}` with recipe `iron-gear-wheel`, and unit 665 at `{97.5,-69.5}` with recipe `transport-belt`.
  - Current live blocker: units 664 and 665 are `no_power`/not connected, transport-belt count is still 0, and local plate/gear/belt logistics has not been built yet.
  - Reboot resume action: start the no-mod server/dashboard if needed, ensure the Slurm worker successor is queued, then continue from local gear-to-belt logistics and power connection near units 664/665. Do not hand-carry iron plates and do not mine the preserved crash-site spaceship.
  - Full verification: `PYTHONPATH=src pytest -q` -> `540 passed`.
- Part 122 fixes fresh-starter Qwen guardrails after the map restart:
  - The current live map is a fresh no-mod starter state again, not the Part 121 relocated-mall state.
  - Qwen 4B still tends to select `bootstrap_build_item_mall` immediately, but before Automation and before basic iron plates this now guardrails to `produce_iron_plate` instead of `research_automation`.
  - Local reconciliation also unwraps older remote Slurm responses that already changed `bootstrap_build_item_mall -> research_automation`, so current local guardrails can recompute the correct starter action even when the worker source lags.
  - Live required-Qwen smoke: strategy `strategy-7194bb8f715d43c78f0f22084bff018e` selected `produce_iron_plate` with `iron_plate_total=0`.
  - Live capped execution built a direct iron bootstrap cell through the virtual server agent: burner mining drill unit 14 at `{75,-69}` outputs directly into stone furnace unit 15 at `{77,-69}`.
  - Verified furnace unit 15 held `iron-plate x26` after the run; `r1jae` was not moved.
  - Current live state after the final capped loop: research automation has started gathering stone/chest prerequisites; inventory has `wooden-chest x1`, `coal x8`; drill unit 14 needs refuel soon and furnace unit 15 still has `coal x2`.
  - Next action: continue `research_automation` from compact stone supply, starter-local steam power, lab placement, and automation science. Keep manual coal/tree mining bounded to unavoidable starter bootstrap and avoid repeated shuttle loops.
  - Full verification: `PYTHONPATH=src pytest -q` -> `542 passed`.
- Part 123 reduces idle-layout interference with active Qwen autopilot:
  - Hidden virtual-agent autopilot was started with `FACTORIO_AI_AGENT_PLAYER=AI`; heartbeat reached `cycle_start` and no GUI/real-player control was used.
  - The idle layout loop was also started, but with `--stale-seconds 15` it misclassified normal attached Slurm/Qwen latency as stale autopilot time and appended Loops 368-396 rapidly.
  - Stopped idle layout parent PID 67768 and child Python PID 74308.
  - Stopped virtual autopilot parent/child processes before committing so tracked `note.md` would not be written concurrently.
  - Updated `run_factorio_no_mod_idle_layout_loop.bat` to use `--stale-seconds 180`.
  - Current state after this operational fix: no continuous autopilot/idle layout process is intentionally left running; no-mod RCON server remains available.
  - Full verification: `PYTHONPATH=src pytest -q` -> `542 passed`.
  - Next follow-up if this repeats: write heartbeat updates around long remote strategy calls so layout work can distinguish active LLM waiting from a truly stalled autopilot.

Part 64 introduced:

- Background idle layout loop so LLM/Slurm can keep doing simulation-only site layout work while deterministic skills are blocked or idle.
- Before/after blueprint copy buttons for layout simulation candidates.
- `/factorio/blueprint` and `/api/factorio/blueprint` copy endpoints.
- External copy smoke tests against `27.115.156.173:8787`.

Part 65 fixed the latest blueprint issues and is already pushed:

- `src/factorio_ai/monitor.py`
  - Site blueprint export now filters entities by site kind.
  - Circuit site blueprints no longer pull nearby steam power, labs, or unrelated machines just because they are spatially near.

- `src/factorio_ai/planner.py`
  - Before-blueprint exports now use a compact representative local cluster when candidate sites are spread far apart.
  - This prevents the "tiny entities in a huge empty blueprint preview" problem.
  - Green-circuit simulation candidate now includes a static operability report.
  - Green-circuit candidate changed to a belt-mediated 3 cable / 2 circuit pattern with output chests.

- `src/factorio_ai/slurm_worker.py`
  - Compact layout payload sent to LLM now includes candidate `validation`.
  - Heuristic layout selection avoids static validation failures and surfaces validation errors as risks.
  - Rules explicitly tell the LLM to treat validation failures as feedback and not mark failed designs build-ready.

- `src/factorio_ai/web_dashboard.py`
  - Layout candidate cards now show validation status (`pass`, `warning`, `fail`) and checked machine count.

- Tests updated:
  - Green-circuit before blueprint does not include power-site entities.
  - Smelting before blueprint uses a compact representative cluster.
  - Green-circuit candidate static validation passes.
  - Dashboard renders validation status.

Part 66 work implements the sandbox validation path:

- `src/factorio_ai/layout_validation.py`
  - New disposable sandbox validator for simulation candidate blueprints.
  - Decodes the candidate blueprint, places entities on a temporary surface, powers the sandbox with substations/solar plus a pole grid, feeds inferred terminal inputs onto belts, waits real server ticks, inspects machine/inserter status, observed items, power, build failures, and output counts, then cleans up the surface.
  - Writes feedback rows to `logs/layout-validation-feedback.jsonl`.
  - Merges latest sandbox feedback back into layout candidate dictionaries as `sandbox_validation`, timestamp, and lesson.

- `src/factorio_ai/cli.py`
  - Adds:

    ```powershell
    $env:PYTHONPATH='src'
    python -m factorio_ai.cli validate-layout-candidate --candidate-id green-circuit-3-cable-2-circuit-cell --variant after --ticks 3600 --player r1jae
    ```

  - The command exits nonzero when the sandbox result is `fail`; this is intentional so CI/operators notice failed candidates.

- `src/factorio_ai/controller.py`, `src/factorio_ai/remote_slurm.py`, `src/factorio_ai/slurm_worker.py`
  - Background layout requests now include compact `layout_validation_feedback`.
  - Slurm compact payloads include candidate `sandbox_validation`, recent lesson text, observed outputs, machine count, inserter count, and failure reasons.
  - Heuristic layout selection avoids sandbox-validation failures and surfaces sandbox reasons as risks.

- `src/factorio_ai/web_dashboard.py`
  - Dashboard candidate cards now show a separate `Sandbox` validation row when feedback exists.
  - The dashboard state merges latest rows from `logs/layout-validation-feedback.jsonl` into current candidates.

- Tests added/updated:
  - `tests/test_layout_validation.py`
  - `tests/test_slurm_worker.py`
  - `tests/test_web_dashboard.py`

Current Part 66 live finding:

- Static validation still reports `green-circuit-3-cable-2-circuit-cell` as `pass`.
- Sandbox validation reports `fail`.
- Latest live smoke wrote this useful lesson to `logs/layout-validation-feedback.jsonl`:
  - expected output `electronic-circuit` was not observed.
  - sandbox fed `copper-plate` and `iron-plate`, all 5 assemblers and all 12 inserters were powered.
  - inserters still waited for source items, so pickup lane, inserter orientation, or belt side is likely unreachable.
  - Do not mark this green-circuit candidate build-ready until a revised layout passes sandbox ticks.

Part 67 fixes the green-circuit candidate with the Part 66 sandbox feedback:

- `src/factorio_ai/planner.py`
  - Corrected green-circuit inserter directions to match live Factorio semantics: inserter `direction` points toward the pickup side, not the drop side.
  - Corrected static validation endpoint modeling so static validation no longer masks reversed inserters.
  - The generated `green-circuit-3-cable-2-circuit-cell` now produces electronic circuits in sandbox ticks.

- `src/factorio_ai/layout_validation.py`
  - Improved sandbox belt feeding by preloading multiple transport-line positions with `LuaTransportLine.insert_at(...)`, which better approximates a supplied input belt than one-time `insert_at_back(...)`.
  - Normalizes empty Lua arrays such as `reasons` and `build_failures` back to JSON lists in Python.
  - Treats intermittent `waiting_for_source_items` as a warning when expected outputs were actually observed, not as a hard failure.

- `src/factorio_ai/slurm_worker.py`
  - Compact Slurm layout payload now includes sandbox `warnings` as well as `reasons`.

- `tests/test_planner.py`
  - Added assertions for the corrected green-circuit inserter directions so this bug does not regress.

Current Part 67 live finding:

- `green-circuit-3-cable-2-circuit-cell` sandbox validation now passes.
- Latest successful smoke:

  ```powershell
  $env:PYTHONPATH='src'
  python -m factorio_ai.cli validate-layout-candidate --candidate-id green-circuit-3-cable-2-circuit-cell --variant after --ticks 1200 --player r1jae
  ```

- Result:
  - `sandbox_validation.status`: `pass`
  - `observed_outputs.electronic-circuit`: `95`
  - `checked_machines`: `5`
  - `powered_machines`: `5`
  - `checked_inserters`: `12`
  - `powered_inserters`: `12`
  - warning remains: intermittent source-item waits may indicate supply/belt-side risk, but the candidate did produce expected output.

Part 68 adds a site-specific pre-build gate for sandbox-proven layouts:

- `src/factorio_ai/planner.py`
  - `green-circuit-3-cable-2-circuit-cell` now has `requires_site_prebuild_gate`, candidate-level `build_ready`, and `site_prebuild_gate`.
  - The gate anchors the proposed blueprint at the current circuit-site centroid when available, otherwise the player position.
  - Checks include:
    - available build items for all blueprint entities.
    - collisions against currently observed map entities.
    - protected starter resource overlap.
    - reach from planned poles to an existing connected power pole.
    - local logistics or stock fallback for `iron-plate` and `copper-plate`.
  - The current test fixture intentionally yields static validation `pass` but site gate `fail`, so sandbox-proven does not mean site-ready.

- `src/factorio_ai/slurm_worker.py`
  - Compact layout payload now includes candidate `site_prebuild_gate` status, build-ready flag, summary, anchor, errors/warnings, and per-check summaries.
  - Layout rules now explicitly say sandbox pass is not site-ready.
  - Heuristic and LLM layout responses are post-processed so a selected candidate with `site_prebuild_gate != pass` or `build_ready=false` is forced back to `build_ready=false`.
  - Existing `sandbox_validation_lesson` payload key is preserved; `sandbox_lesson` remains as a short alias.

- `src/factorio_ai/web_dashboard.py`
  - Candidate cards now show a third validation row: `Pre-build`.
  - The validation panel renderer displays a gate `summary` plus the first errors/warnings, so operators see why a candidate is not build-ready.

- Tests updated:
  - `tests/test_planner.py`
  - `tests/test_slurm_worker.py`
  - `tests/test_web_dashboard.py`

Part 69 adds deterministic placement search on top of the Part 68 pre-build gate:

- `src/factorio_ai/planner.py`
  - Adds `site_placement_search` for `green-circuit-3-cable-2-circuit-cell`.
  - The search evaluates a grid of nearby anchors around the current circuit-site centroid, scores anchors by collision, protected-resource preservation, power reach, input logistics, build items, and distance from seed.
  - The selected `site_prebuild_gate` now uses the best searched anchor instead of blindly using the current site centroid.
  - `site_placement_search.status` is `found` when the best anchor clears collision/resource/power/input logistics checks, even if build items or sandbox feedback still block build-ready.
  - Candidate-level `build_ready` now remains `false` until sandbox feedback is pass, site gate is pass, and placement search is found.
  - `build_ready_blockers` explicitly records missing sandbox feedback, missing build items, power reach failures, input logistics failures, and blocked placement.

- `src/factorio_ai/layout_validation.py`
  - `merge_sandbox_validation_feedback` now recomputes candidate `build_ready` and `build_ready_blockers` when sandbox feedback is attached.
  - A candidate becomes build-ready only after sandbox pass plus site/pre-build/placement pass.

- `src/factorio_ai/slurm_worker.py`
  - Compact layout payload now includes `site_placement_search` and `build_ready_blockers`.
  - Heuristic risks include `site_placement_search=<status>` and the blockers so LLM layout review can distinguish "bad anchor" from "missing build items".

- `src/factorio_ai/web_dashboard.py`
  - Candidate cards now show a `Placement` row with search status, selected anchor, and evaluated anchor count.

- Tests updated:
  - `tests/test_planner.py`
  - `tests/test_layout_validation.py`
  - `tests/test_slurm_worker.py`
  - `tests/test_web_dashboard.py`

Part 70 groups site logistics under the relevant factory sites in the web monitor:

- `src/factorio_ai/web_dashboard.py`
  - Replaces the side-by-side `Factory Sites` and `Logistics Links` panels with one `Factory Sites` panel.
  - Each site row can now render a child `site-logistics-row` containing inbound/outbound/linked logistics entries for that site.
  - Link matching uses exact `site_id` first, then position aliases such as `smelting:x,y`, `plate_smelting_line:group:item:x,y`, and mining patch group aliases.
  - Any logistics links that cannot be matched to a site still render under an `Unassigned Logistics` fallback so links are not hidden.

- `src/factorio_ai/planner.py`
  - Restores the Part 70 worktree to a valid syntax state after an interrupted edit.
  - Adds `prerequisite_tasks` to the green-circuit layout candidate so build blockers become explicit operator tasks such as sandbox validation, missing build items, power extension, input logistics, and anchor selection.

- Tests updated:
  - `tests/test_web_dashboard.py`
  - `tests/test_planner.py`

Part 71 adds manual factory-site improvement target selection:

- `src/factorio_ai/site_selection.py`
  - New runtime state file: `runtime/layout-improvement-target.json`.
  - Stores the operator-selected factory site per objective with site id, kind, item, position, source, and timestamp.

- `src/factorio_ai/web_dashboard.py`
  - Each Factory Sites row now has an `Improve` control.
  - Posting the control stores that site as the selected layout improvement target without overwriting production targets.
  - The selected site renders as a `Selected` badge.
  - Dashboard state loads the selected target and feeds it into `layout_improvement`.
  - Codex token usage table now includes a `Tokens / hour` column computed from each row's token delta and elapsed time since the previous sample.

- `src/factorio_ai/strategy.py`
  - `make_layout_improvement_context`, `make_strategy_payload`, and `heuristic_strategy` accept `selected_improvement_site`.
  - A selected site becomes an `operator_selected_site` layout opportunity with severity `86`, so idle strategy cycles prefer `plan_factory_site` for that target.

- `src/factorio_ai/controller.py`, `src/factorio_ai/remote_slurm.py`, `src/factorio_ai/slurm_worker.py`
  - Runtime selected site is forwarded into local/remote strategy requests and background layout-improvement requests.
  - Compact Slurm payloads preserve `selected_improvement_site` and expose it inside `layout_improvement`.

- Tests added/updated:
  - `tests/test_site_selection.py`
  - `tests/test_web_dashboard.py`
  - `tests/test_strategy.py`
  - `tests/test_slurm_worker.py`
  - `tests/test_controller.py`

Part 72 improves the operator UX and early research priority:

- `src/factorio_ai/site_selection.py`
  - Adds `clear_selected_improvement_site(...)` for removing the runtime layout-improvement target.

- `src/factorio_ai/web_dashboard.py`
  - Selected Factory Sites now render a dedicated "Selected improvement target" summary above the table.
  - The selected row is visually highlighted.
  - The selected row and summary include a `Cancel` control that clears `runtime/layout-improvement-target.json`.
  - The old no-feedback behavior after pressing Select is fixed: after selection, the panel, row highlight, selected badge, and cancel action are visible.

- `src/factorio_ai/targets.py`
  - `automation-science-pack` is listed before `electronic-circuit` in target forms and default rocket-program target order.

- `src/factorio_ai/strategy.py`
  - Rocket-program strategy now keeps early red-science research ahead of green-circuit line work.
  - If Automation is not researched, electronic-circuit deficits are redirected to `research_automation`.
  - If Automation is researched but Logistics is not, electronic-circuit deficits are redirected to `research_logistics`.
  - Self-adjusting guardrail logs are avoided when the strategy is already `research_automation` or `research_logistics`.

- `tests/test_site_selection.py`, `tests/test_web_dashboard.py`, `tests/test_strategy.py`, `tests/test_slurm_worker.py`, `tests/test_targets.py`
  - Cover selection clearing, visible cancel UX, target ordering, red-science-first strategy, and Slurm guardrail behavior.

## Verification Already Run

Focused tests:

```powershell
$env:PYTHONPATH='src'
pytest -q tests\test_planner.py::PlannerTests::test_factory_layout_simulates_green_circuit_pattern_without_applying_it `
  tests\test_planner.py::PlannerTests::test_green_circuit_before_blueprint_does_not_pull_adjacent_power_site `
  tests\test_planner.py::PlannerTests::test_smelting_before_blueprint_uses_compact_representative_cluster `
  tests\test_web_dashboard.py::WebDashboardTests::test_dashboard_html_has_monitor_sections_and_item_icons
```

Result: `4 passed`

Broader tests:

```powershell
$env:PYTHONPATH='src'
pytest -q tests\test_planner.py tests\test_web_dashboard.py tests\test_blueprints.py
```

Result: `130 passed`

Full test suite after the final Slurm payload tweaks:

```powershell
$env:PYTHONPATH='src'
pytest -q
```

Result: `314 passed`

Part 66 focused tests:

```powershell
$env:PYTHONPATH='src'
pytest -q tests\test_layout_validation.py tests\test_slurm_worker.py tests\test_web_dashboard.py
```

Result: `19 passed`

Part 66 full test suite:

```powershell
$env:PYTHONPATH='src'
pytest -q
```

Result: `317 passed`

Part 67 focused tests:

```powershell
$env:PYTHONPATH='src'
pytest -q tests\test_layout_validation.py tests\test_slurm_worker.py tests\test_web_dashboard.py tests\test_planner.py::PlannerTests::test_factory_layout_simulates_green_circuit_pattern_without_applying_it
```

Result: `20 passed`

Part 67 full test suite:

```powershell
$env:PYTHONPATH='src'
pytest -q
```

Result: `317 passed`

Part 68 focused tests:

```powershell
$env:PYTHONPATH='src'
pytest -q tests/test_planner.py tests/test_slurm_worker.py tests/test_web_dashboard.py
```

Result: `133 passed`

Part 68 full test suite:

```powershell
$env:PYTHONPATH='src'
pytest -q
```

Result: `317 passed`

Part 69 focused tests:

```powershell
$env:PYTHONPATH='src'
pytest -q tests/test_planner.py tests/test_layout_validation.py tests/test_slurm_worker.py tests/test_web_dashboard.py
```

Result: `137 passed`

Part 69 full test suite:

```powershell
$env:PYTHONPATH='src'
pytest -q
```

Result: `319 passed`

Part 70 focused tests:

```powershell
$env:PYTHONPATH='src'
pytest -q tests/test_web_dashboard.py tests/test_planner.py tests/test_layout_validation.py tests/test_slurm_worker.py
```

Result: `138 passed`

Part 70 full test suite:

```powershell
$env:PYTHONPATH='src'
pytest -q
```

Result: `320 passed`

Part 71 focused tests:

```powershell
$env:PYTHONPATH='src'
pytest -q tests/test_site_selection.py tests/test_web_dashboard.py tests/test_strategy.py tests/test_slurm_worker.py tests/test_controller.py
```

Result: `86 passed`

Part 71 full test suite:

```powershell
$env:PYTHONPATH='src'
pytest -q
```

Result: `328 passed`

Part 66 token usage sample:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli record-token-usage --tokens-used 35141439 --label "part66 sandbox layout validation"
```

Result: appended `delta_tokens=234991` to `logs/token_usage.jsonl`, which is read by the web dashboard token graph.

Part 67 token usage sample:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli record-token-usage --tokens-used 35232450 --label "part67 green circuit sandbox pass"
```

Result: appended `delta_tokens=91011` to `logs/token_usage.jsonl`, which is read by the web dashboard token graph.

Part 68 token usage sample:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli record-token-usage --tokens-used 35462230 --label "part68 site prebuild gate final"
```

Result: appended a final `delta_tokens=20480` row to `logs/token_usage.jsonl`; combined Part 68 rows total `229780` tokens and are read by the web dashboard token graph.

Part 69 token usage sample:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli record-token-usage --tokens-used 35660655 --label "part69 placement search final"
```

Result: appended a final `delta_tokens=18355` row to `logs/token_usage.jsonl`; combined Part 69 rows total `198425` tokens and are read by the web dashboard token graph.

Part 70 token usage sample:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli record-token-usage --tokens-used 35769023 --label "part70 grouped site logistics"
```

Result: appended `delta_tokens=108368` to `logs/token_usage.jsonl`.

Part 70 final token usage sample:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli record-token-usage --tokens-used 35808664 --label "part70 grouped site logistics final"
```

Result: appended a final `delta_tokens=39641` row to `logs/token_usage.jsonl`; combined Part 70 rows total `148009` tokens and are read by the web dashboard token graph.

Part 71 token usage sample:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli record-token-usage --tokens-used 36033651 --label "part71 manual site improvement target"
```

Result: appended `delta_tokens=224987` to `logs/token_usage.jsonl`.

Part 71 final token usage sample:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli record-token-usage --tokens-used 36082114 --label "part71 manual site target final"
```

Result: appended a final `delta_tokens=48463` row to `logs/token_usage.jsonl`; combined Part 71 rows total `273450` tokens and are read by the web dashboard token graph.

## Live Smoke Checks Already Run

Dashboard:

```powershell
Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:18889/factorio?lang=ko&objective=launch_rocket_program'
Invoke-WebRequest -UseBasicParsing 'http://27.115.156.173:8787/factorio?lang=ko&objective=launch_rocket_program'
```

Both returned HTTP `200`.

Current generated candidate inspection from dashboard state:

- `copper-plate-parallel-smelting-columns` before blueprint: `15` entities, width `24.0`, height `3.0`.
- `green-circuit-3-cable-2-circuit-cell` validation:
  - status `pass`
  - checked machines `5`
  - errors `[]`
  - after blueprint entities include `assembling-machine-1`, `inserter`, `iron-chest`, `small-electric-pole`, `transport-belt`
  - before blueprint names include `assembling-machine-1`, `inserter`, `small-electric-pole`
  - before blueprint no longer includes boiler/steam-engine/offshore-pump.

Part 68 dashboard HTTP smoke:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli web --host 127.0.0.1 --port 18890 --objective launch_rocket_program
Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:18890/factorio?lang=en&objective=launch_rocket_program'
```

Result:

- Local dashboard HTTP smoke ran on port `18890`; the validation process was stopped after the smoke.
- HTTP response contained `Pre-build`.
- HTTP response contained `green-circuit-3-cable-2-circuit-cell`.
- HTTP response contained the site-gate/pre-build text.
- The in-app Browser plugin was attempted, but no `iab` browser was available (`agent.browsers` returned an empty list), so no browser screenshot was captured.

Part 69 dashboard HTTP smoke:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli web --host 127.0.0.1 --port 18891 --objective launch_rocket_program
Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:18891/factorio?lang=en&objective=launch_rocket_program'
```

Result:

- Local dashboard HTTP smoke ran on port `18891`; the validation process was stopped after the smoke.
- HTTP response contained `Placement`.
- HTTP response contained `green-circuit-3-cable-2-circuit-cell`.
- HTTP response contained `anchor=`.
- The in-app Browser plugin was attempted again, but no `iab` browser was available, so no browser screenshot was captured.

Part 70 dashboard HTTP smoke:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli web --host 127.0.0.1 --port 18892 --objective launch_rocket_program
Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:18892/factorio?lang=ko&objective=launch_rocket_program'
```

Result:

- Local dashboard HTTP smoke ran on port `18892`; the validation process was stopped after the smoke.
- HTTP status was `200`.
- HTTP response contained `site-logistics-row` and `site-logistics-link`.
- HTTP response did not contain the old peer heading `<h2>Logistics Links</h2>`.
- The in-app Browser plugin was attempted again, but no `iab` browser was available, so no browser screenshot was captured.

Part 71 dashboard HTTP smoke:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli web --host 127.0.0.1 --port 18894 --objective launch_rocket_program
Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:18894/factorio?lang=en&objective=launch_rocket_program'
```

Result:

- Local dashboard HTTP smoke ran on port `18894`; the validation process was stopped after the smoke.
- HTTP status was `200`.
- HTTP response contained `name="action" value="select_improvement_site"`.
- HTTP response contained `site-improvement-button`.
- HTTP response contained `Tokens / hour`.
- HTTP response contained `token-chart`.
- The in-app Browser plugin was attempted again, but no `iab` browser was available, so no browser screenshot was captured.

Part 72 verification:

```powershell
$env:PYTHONPATH='src'
pytest -q tests\test_strategy.py tests\test_targets.py tests\test_site_selection.py tests\test_web_dashboard.py
```

Result: `58 passed`

```powershell
$env:PYTHONPATH='src'
pytest -q
```

Result: `336 passed`

Live strategy check:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli no-mod-strategy --objective launch_rocket_program
```

Result:

- `selected_skill`: `research_logistics`
- `priority`: `92`
- evidence included `automation_science_pack_total=0`, `automation_researched=true`, `logistics_researched=false`
- This confirms the current loop now prioritizes red-science Logistics research before the green-circuit line.

Live web restart/check:

- Web process restarted on port `18889`.
- Current observed web command after restart: `python -m factorio_ai.cli web --host 0.0.0.0 --port 18889 --objective launch_rocket_program`
- Local URL checked: `http://127.0.0.1:18889/factorio?lang=en&objective=launch_rocket_program`
- External URL checked: `http://27.115.156.173:8787/factorio?lang=en&objective=launch_rocket_program`
- Both returned HTTP `200`.
- Both contained the select action and `research_logistics`.
- Target form order check passed: `automation-science-pack` input appears before `electronic-circuit`.

Selection/cancel smoke:

- Selected the first site via POST: `circuit_automation:group:mixed:20.5,-799.5`
- Response showed `Selected improvement target`, `clear_improvement_site`, and `<tr class="site-selected-row">`.
- Then POSTed `clear_improvement_site`; response no longer showed the selected panel or clear action.
- Runtime target file was left cleared after the smoke.
- One server log `ValueError: selected improvement site requires a site_id` was caused by an earlier malformed manual smoke command with an empty `site_id`; ignore it unless it recurs from real UI usage.

Browser verification:

- The in-app Browser plugin was attempted again.
- `iab` was unavailable (`Browser is not available: iab`), so no visual screenshot/click automation was captured.

Part 73 verification:

```powershell
$env:PYTHONPATH='src'
pytest -q tests\test_run_journal.py tests\test_token_usage.py tests\test_web_dashboard.py tests\test_controller.py
```

Result: `50 passed`

```powershell
$env:PYTHONPATH='src'
pytest -q
```

Result: `341 passed`

Dashboard smoke after restarting the web process:

```powershell
Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:18889/factorio?lang=en&objective=launch_rocket_program'
Invoke-WebRequest -UseBasicParsing 'http://27.115.156.173:8787/factorio?lang=en&objective=launch_rocket_program'
```

Result:

- both returned HTTP `200`.
- both contained `Goal Plan`, `Recent Loop Notes`, `Recent Insights`, and `Weekly %`.

Part 73 token usage sample:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli record-token-usage --tokens-used 38675003 --label "part73 goal journal and dashboard" --source codex
```

Result: appended `delta_tokens=243637` to `logs/token_usage.jsonl`. `FACTORIO_AI_WEEKLY_TOKEN_QUOTA` was not set, so weekly percent is `unknown`.

Part 73 adds goal and loop journal tracking:

- Root Markdown files:
  - `goal.md` now stores the long-term rocket/Space Age roadmap, current sprint, factory quality criteria, and learning roadmap.
  - `note.md` is the human-readable loop execution journal.
  - `insight.md` is the human-readable improvement journal and is only appended when a loop produces meaningful progress.

- `src/factorio_ai/run_journal.py`
  - New structured source logs: `logs/run-notes.jsonl` and `logs/run-insights.jsonl`.
  - Appends Markdown summaries to `note.md` and `insight.md`.
  - Provides `run_journal_summary(...)` for CLI and dashboard consumption.

- `src/factorio_ai/controller.py`
  - Skill loops append one note per skill execution and insight rows for item-count increases or successful skill completion.
  - Autopilot cycles append one note per strategy cycle.
  - Idle and Codex-wait layout cycles append notes.
  - Layout results append insights only when before/after evidence confirms an actual improvement; simulation-only candidates and next-focus suggestions stay in `note.md` and raw logs.

- `src/factorio_ai/token_usage.py`
  - Token summaries now include `latest_delta_tokens`, optional `FACTORIO_AI_WEEKLY_TOKEN_QUOTA`, and weekly percentage fields.
  - If the weekly quota env var is unset, percentage renders as `unknown`.

- `src/factorio_ai/web_dashboard.py`
  - Dashboard now renders Goal Plan, Recent Loop Notes, Recent Insights, and weekly token percentage.

- `src/factorio_ai/cli.py`
  - Adds `run-journal-summary`.

- Tests added/updated:
  - `tests/test_run_journal.py`
  - `tests/test_token_usage.py`
  - `tests/test_web_dashboard.py`

Part 73 smoke:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli run-no-mod-idle-layout-loop --objective launch_rocket_program --cycles 1 --sleep-seconds 0 --stale-seconds 0 --min-submit-interval-seconds 9999
```

Result:

- `ok: true`
- one `idle_layout_cycle` note was appended to `note.md` and `logs/run-notes.jsonl`.
- no `insight.md` entry was appended because the smoke did not improve the factory.

## Important Caveat: Blueprint Validation

Part 66 adds a first sandbox validation path, but it is still an early validator, not a complete production simulator.

Completed now:

1. Create a disposable sandbox validation path for blueprint candidates: done.
2. Import/place a candidate blueprint into a temporary isolated surface: done.
3. Feed controlled input items: done for inferred terminal inputs on source belt lanes.
4. Run ticks: done by waiting real server ticks.
5. Inspect machine status, item movement, output counts, power, collision/buildability, and belt/inserter throughput: partially done. Current output includes build failures, observed outputs, input insertions, machine statuses, inserter statuses, powered machine/inserter counts, and ticks.
6. Write results to `logs/layout-validation-feedback.jsonl`: done.
7. Add failed candidate feedback to future LLM payloads: done for background layout Slurm payloads.
8. Convert validation feedback into fine-tuning examples later: not done.

Proposed feedback JSONL shape:

```json
{
  "timestamp": "2026-06-14T00:00:00+09:00",
  "candidate_id": "green-circuit-3-cable-2-circuit-cell",
  "variant": "after",
  "static_validation": {"status": "pass"},
  "sandbox_validation": {
    "status": "fail",
    "reasons": [
      "electronic-circuit assembler at x=6,y=1 never received iron plate",
      "output inserter has no reachable sink"
    ],
    "observed_outputs": {"electronic-circuit": 0},
    "ticks": 3600
  },
  "lesson": "Do not mark green circuit layouts build-ready unless both iron-plate input and copper-cable transfer paths are proven by sandbox ticks."
}
```

This file can later be transformed into Qwen fine-tuning examples.

## Next Implementation Priority

Part 74 restarted the live no-mod game on a fresh cliffs-off map after the previous factory became too scattered. The next CLI session should continue from the fresh-map bootstrap path:

1. Run compact starter-local `produce_iron_plate`/iron bootstrap on the fresh map.
2. Add coal supply and coal fuel feed before scaling burner smelting or steam power.
3. Add copper and steam power near the starter cluster; do not create remote starter sites before rail.
4. Build research and mall sites close to their producer sites, with short belts/chests/inserters for repeated inputs.
5. Keep green-circuit line work behind early red-science research unless Logistics is researched or the research path reports a concrete prerequisite blocker.
4. Continue the selected-site operator loop after research is moving: add live observed-state detail for selected `site_placement_search.selected_anchor`, blockers, prerequisite tasks, and top candidate anchors.
5. Convert `prerequisite_tasks` into strategy hints or deterministic prerequisite tasks: build-item mall, power pole corridor, iron/copper belt link, stock collection, or anchor reselection.
6. Make layout candidate generation prefer the operator-selected site when multiple sites of the same kind/item exist, instead of only surfacing the selected site as LLM context.
7. Add a deterministic build executor only after sandbox pass, `site_prebuild_gate.status=pass`, `site_placement_search.status=found`, selected-site context is honored, and required build items are available.
8. Consider exporting `logs/layout-validation-feedback.jsonl`, `site_prebuild_gate`, `site_placement_search`, selected-site rows, and `prerequisite_tasks` rows into Qwen fine-tuning examples later.
9. Rerun before any future build-ready claim:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli validate-layout-candidate --candidate-id green-circuit-3-cable-2-circuit-cell --variant after --ticks 3600 --player r1jae
```

10. Only mark the candidate build-ready after sandbox validation, site pre-build gate, deterministic site placement checks, and prerequisite tasks all pass.

## Core Commands

Start a fresh Codex CLI session from this handoff, without relying on the Desktop conversation:

```powershell
.\continue_factorio_cli.bat
```

The start prompt used by the bat is:

```text
Read docs\CLI_HANDOFF.md only as the handoff context. Do not assume the previous desktop conversation is available. Continue the Factorio automation project from that document: verify current git status, run tests, commit/push the current validated changes if still uncommitted, then implement the next highest-priority item described in the handoff.
```

Observe current game:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli no-mod-observe --player r1jae
```

Run strategy:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli no-mod-strategy --objective launch_rocket_program
```

Run one no-mod strategy step for diagnosis only:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli run-no-mod-strategy-step --objective launch_rocket_program --max-steps 1
```

Normal gameplay should not depend on Codex manually invoking strategy steps. Keep the LLM autopilot running and let Codex work only on missing executors, guardrails, tests, UI, and documentation:

```powershell
.\run_factorio_no_mod_real_player_llm_autopilot.bat
```

That batch enables `FACTORIO_AI_SLURM_ENABLED=1`, requires LLM strategy, uses the Qwen Slurm worker, opens/uses the real player client, and starts the idle layout loop in parallel.

Run idle layout loop once:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli run-no-mod-idle-layout-loop --objective launch_rocket_program --cycles 1 --sleep-seconds 0 --stale-seconds 0
```

Ensure the active 4B Slurm worker has a running or queued successor well before the 1-day allocation expires:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli slurm-ensure-worker --renew-before-minutes 360
```

Open review GUI:

```powershell
.\run_factorio_watch_gui.bat
```

## Known Design Issues To Keep In Mind

- Factory sites have historically been too scattered. Site placement must prefer starter-local clusters until rail logistics exist.
- Power must prefer starter-local or already connected water. Do not build isolated remote starter steam power; a remote water block is only valid when the dependent factory site is co-located there or a reachable power/logistics corridor already exists.
- Full water/site scans must preserve nearest-water ordering. Use staged radius scans or cached spatial memory; do not sort only after a large limited sample has already discarded nearby tiles.
- Production blocks must avoid covering starter ore/coal patches unless unavoidable.
- Preserve starting crashed spaceship/wreckage like a protected landmark unless the operator explicitly requests removal.
- Research automation must use assemblers and labs; hand-crafting science packs is not acceptable for sustained progress.
- Iron/copper bootstrap must place/fuel direct burner mining drill -> stone furnace cells; repeated pickaxe mining of ore is not acceptable for normal plate supply.
- Stone bootstrap should place a burner mining drill outputting into a wooden/iron chest when possible.
- Labs usually need daisy chain or belt-fed science distribution.
- Burner drills are only bootstrap; later replace with electric drills.
- After `long-inserters`, modules, beacons, better assemblers/furnaces, rails, or quality tiers unlock, rerank existing and proposed site layouts. The Qwen payload and deterministic planner should show which unlocked tools were considered, not rely on a fixed pre-unlock layout chain.
- Boiler fuel should become coal belt plus inserter, not manual coal insertion.
- Defense must build gun turrets plus firearm magazine supply around factory sites before trying to clear nests.
- Threat logic should distinguish nearby nests, pollution-triggered attacks, recent damage, and actual factory danger.
- Logistics links are site-to-site links, not individual belt segment links.
- Do not spend scarce hand-crafted belts on site-to-site paths before `transport-belt` production is automated by a mall assembler. Bootstrap-local direct insertion is allowed; repeated site links should wait for belt automation.
- Trains should be used for far resources/outposts once local logistics become too long.
- Part 82 recovered the bad remote steam entities and verified that `SetupPowerSkill` now returns a remote-water blocker even when a full planning-site scan finds remote `power_sites`.
- Part 83 fixed the nearest-water scan order, built working starter-local steam power, protected starting wreckage, and queued a dependent Slurm successor before the current 1-day job expired.
- Part 84 moved Slurm renewal into launcher/controller heartbeats, added direct iron/copper smelting bootstrap, added stone drill->chest supply, and gates belt smelting expansion until transport-belt production is automated.

## LLM Model Direction

User wants latest Qwen models where feasible:

- Prefer Qwen 3.5 / 3.6 family over Qwen 2.5.
- Compare smaller models first when GPU3 is busy.
- Keep a GPU3 Slurm job queued for larger 27B/28B FP8 model attempts.
- Do not exclude `n077` permanently; failures may be temporary.
- AUTO Slurm job should request up to 3 GPUs if needed, but smaller models can run on less busy GPU resources.

The LLM should receive current game state, production targets, monitor bottlenecks, site graph, selected improvement site, layout issues, candidate validation, and failure feedback. It should not receive only a generic "how do I launch a rocket" prompt.

## Web Monitor Expectations

Dashboard should include:

- Desired production targets and user-editable rates.
- Estimated production / consumption / deficits.
- Factory sites grouped by site, not individual entities.
- Site-to-site logistics links grouped under related factory sites, with only unmatched links shown as a fallback list.
- Manual site selection controls for choosing the next layout-improvement target.
- Power networks and unconnected consumers.
- Threats / defense and recent damage.
- LLM decision logs.
- Codex token usage graph with true time-axis spacing and KST timestamps.
- Layout issues, opportunities, simulation candidates, and before/after blueprint copy buttons.
- Candidate validation status, sandbox validation results, site pre-build gate, placement search status, and build-ready blockers.
- Latest training trace archive summary, especially high-value layout/strategy/operator-intervention traces.

## Training Trace Archive

Raw logs and generated archives are local runtime data and are ignored by Git. Before resetting maps, pruning logs, or starting a major new sprint, preserve the current traces:

```powershell
$env:PYTHONPATH='src'
python -m factorio_ai.cli archive-training-traces --label "partXX-short-description"
python -m factorio_ai.cli trace-archive-summary
```

Default output is `runtime/trace_archives/YYYYMMDD-HHMMSS-<label>/` with `manifest.json`, `index.jsonl`, `README.md`, and raw copied logs. The exporter prioritizes:

- layout background loops
- sandbox layout validation feedback
- layout/strategy JSONL runs
- LLM decision logs
- `note.md` and `insight.md`
- future human/operator intervention comparisons

Human edits to an agent-built factory site should be treated as data, not automatically accepted. Compare the agent's previous layout snapshot against the human-modified layout and only append an `insight.md` entry when metrics improve, such as throughput per tile, bottleneck removal, lower power/pollution, shorter logistics distance, or a sandbox validation pass.

## Git Hygiene

Before finishing any CLI continuation:

```powershell
git status --short
$env:PYTHONPATH='src'; pytest -q
python -m factorio_ai.cli record-token-usage --tokens-used <goal_tokens_used> --label "partXX short label"
git add ...
git commit -m "Part XX: concise description"
git push origin master
```

Never revert user changes unless explicitly requested.
