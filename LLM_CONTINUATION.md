# Factorio Automation LLM Continuation Guide

This document is the primary handoff for any LLM, Codex CLI session, Claude
session, or future agent continuing this project. Read this before making
strategy or implementation decisions.

## Mission

Build a Factorio autoplayer that can eventually launch a rocket through a
gameplay-compatible path.

The target design is not an iron-plate demo. It is a layered AI system:

1. A strategic LLM chooses the next high-level skill from the current game
   state.
2. Deterministic local skills execute concrete gameplay routines.
3. Factorio is controlled through the no-custom-mod RCON/Lua path whenever
   multiplayer compatibility matters.
4. Slurm workers run Qwen models for strategy, layout review, and model
   comparisons.
5. The web dashboard exposes factory state, production targets, LLM decisions,
   layout issues, threats, token usage, and later training/evaluation data.

The user wants the project to keep moving toward rocket launch, train/outpost
logistics, oil, science progression, layout improvement, and eventual
fine-tuning from traces.

## Non-Negotiable Design Rules

- The LLM is required for strategic play, but it must not control the game at
  tick level.
- The LLM chooses high-level skills such as `expand_iron_smelting`,
  `automate_electronic_circuit_line`, `research_logistics`, or
  `plan_factory_site`.
- Local deterministic skills perform movement, crafting, building, insertion,
  recipe setting, power connection, and validation.
- If the LLM selects a missing executor, do not fake behavior. Record it and
  implement the deterministic executor as a code change.
- No-custom-mod RCON/Lua is the preferred near-term path for multiplayer
  compatibility. It uses official Factorio content but RCON Lua disables
  Steam achievements for that save.
- GUI demonstrations should show real walking/mining/building motion.
  Headless/fast RCON tests can be used during implementation.
- Do not build early factory blocks on top of starter resource patches unless
  there is no practical alternative.
- Site/logistics monitoring is site-level. Do not treat every belt tile as a
  separate logistic link.
- Power must be modeled per connected electric network. If generation is below
  demand, electric machines slow down.
- Defense means early gun turrets plus firearm magazines around factory sites.
  Do not choose early nest clearing until a validated combat/turret-push skill
  exists.

## Current Control Path

Primary path:

```powershell
cd C:\Users\NEC\Documents\Factorio
$env:PYTHONPATH="src"
python -m factorio_ai.cli no-mod-observe
python -m factorio_ai.cli no-mod-strategy --objective launch_rocket_program --require-llm
python -m factorio_ai.cli run-no-mod-strategy-step --objective launch_rocket_program --require-llm
```

Useful BAT files:

```bat
run_factorio_no_mod_server.bat
run_factorio_no_mod_watch_gui.bat
run_factorio_no_mod_llm_autopilot.bat
run_factorio_no_mod_real_player_llm_autopilot.bat
run_factorio_slurm_llm_4b_worker.bat
run_factorio_slurm_llm_4b_attached_benchmark.bat
run_factorio_slurm_llm_9b_worker.bat
run_factorio_slurm_llm_9b_attached_benchmark.bat
run_factorio_slurm_llm_27b_gpu3_queue.bat
```

For test and implementation loops, prefer headless/no-mod commands. For user
review, use the GUI watch helper.

Headless no-mod runs may use the virtual server-side agent when the configured
AI player is not connected. That is acceptable for fast iteration, but not for
GUI/real-play validation. For real-player validation set
`FACTORIO_AI_AGENT_PLAYER=auto` and `FACTORIO_AI_REQUIRE_REAL_PLAYER=1`, or run
`run_factorio_no_mod_real_player_llm_autopilot.bat`. In that mode the
controller uses the first connected GUI player, sets
`FACTORIO_AI_USE_GUI_INPUT_FOR_MOVEMENT=1`, sends WASD input for `move_to`, and
stops instead of falling back to the virtual server agent. The `auto`,
`connected`, `first-connected`, and `*` player names mean "first connected GUI
player"; they must not fall through to the virtual server agent.

Strict real-player execution must pause if the player has no valid character,
is in non-character controller mode after recovery, or if enemies are close to
the player, action target, or movement segment. If a connected real player has
a valid character but is in Factorio remote/map controller mode, the controller
first runs the allowlisted `restore_character_controller` action and re-observes.
Tune the current guard with
`FACTORIO_AI_REAL_PLAYER_ENEMY_STOP_RADIUS`,
`FACTORIO_AI_REAL_PLAYER_ENEMY_TARGET_RADIUS`, and
`FACTORIO_AI_REAL_PLAYER_ENEMY_PATH_RADIUS`.

The strict real-player strategy loop now checks near-player enemy pressure before
LLM strategy or executor work. If an enemy is inside the stop radius, it sends a
`stop` action, records an `execution_guard` strategy result with
`build_starter_defense`, and fails the cycle. This prevents another long LLM or
skill cycle from running while a biter is already close enough to kill the
character.

## Slurm LLM Workers

The project currently owns its Factorio-specific workers rather than depending
on the shared bot AUTO worker.

Fast active worker:

```text
remote dir: ~/factorio-ai-worker
job name: factorio-ai-worker
model: Qwen/Qwen3.5-4B
GPU request: gpu:1
partition preference: gpu4,gpu2,gpu1
vLLM args: --max-model-len 32768 --gpu-memory-utilization 0.85 --enforce-eager
```

Medium comparison worker:

```text
remote dir: ~/factorio-ai-worker-9b
job name: factorio-ai-worker-9b
model: Qwen/Qwen3.5-9B
GPU request: gpu:a6000:1
partition: gpu4
vLLM args: --max-model-len 32768 --gpu-memory-utilization 0.90 --enforce-eager
```

Large queued worker:

```text
remote dir: ~/factorio-ai-worker-27b
job name: factorio-ai-worker-27b
model: Qwen/Qwen3.6-27B-FP8
GPU request: gpu:a6000ada:3
partition: gpu3
vLLM args: --tensor-parallel-size 3 --max-model-len 32768 --gpu-memory-utilization 0.85 --enforce-eager
```

Check readiness:

```powershell
$env:PYTHONPATH="src"
$env:FACTORIO_AI_SLURM_ENABLED="1"
$env:FACTORIO_AI_SLURM_MODE="queue"
$env:FACTORIO_AI_SLURM_REMOTE_DIR="~/factorio-ai-worker"
$env:FACTORIO_AI_SLURM_JOB_NAME="factorio-ai-worker"
python -m factorio_ai.cli slurm-llm-status
```

Compare all configured strategy workers on the current game payload:

```powershell
$env:PYTHONPATH="src"
$env:FACTORIO_AI_AGENT_PLAYER="auto"
$env:FACTORIO_AI_SLURM_ENABLED="1"
$env:FACTORIO_AI_SLURM_MODE="queue"
python -m factorio_ai.cli slurm-compare-strategy-workers --objective launch_rocket_program
```

The latest live comparison showed:

- 4B worker ready, model `Qwen/Qwen3.5-4B`, returned LLM source and selected a
  valid production skill.
- 9B worker ready, model `Qwen/Qwen3.5-9B`. The normal prompt can still hit the
  runtime 4096-token context limit, but the worker retries with
  `ultra_compact_strategy_payload`, sets `max_tokens` explicitly, and can return
  an LLM strategy result. The initial context error remains in
  `llm_initial_error`; `llm_error` is empty after a successful compact retry.
- 27B worker has three RTX 6000 Ada GPUs allocated for
  `Qwen/Qwen3.6-27B-FP8`, but the OpenAI-compatible endpoint is not serving yet.

The command writes `logs/strategy-worker-comparison.jsonl`; the web dashboard
renders the latest comparison under the LLM decision log.

Deploy changed source to the active worker:

```powershell
$env:PYTHONPATH="src"
$env:FACTORIO_AI_SLURM_ENABLED="1"
$env:FACTORIO_AI_SLURM_REMOTE_DIR="~/factorio-ai-worker"
$env:FACTORIO_AI_SLURM_JOB_NAME="factorio-ai-worker"
$env:FACTORIO_AI_SLURM_GPUS_PER_NODE="1"
$env:FACTORIO_AI_SLURM_GRES="gpu:1"
$env:FACTORIO_AI_SLURM_PARTITION="gpu4,gpu2,gpu1"
$env:FACTORIO_AI_VLLM_MODEL="Qwen/Qwen3.5-4B"
$env:FACTORIO_AI_VLLM_ARGS="--max-model-len 32768 --gpu-memory-utilization 0.85 --enforce-eager"
$env:FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER="0"
$env:FACTORIO_AI_VLLM_PORT="8000"
python -m factorio_ai.cli slurm-deploy
```

Do not confuse model-native context with runtime context. Qwen3.5 can support
larger contexts, but the active vLLM workers are configured at 32768 tokens for
stability and latency. Increase in measured steps only after checking GPU
memory and response time.

## Strategic Flow

The strategy payload is produced by `factorio_ai.strategy.make_strategy_payload`.
It includes:

- objective;
- observation;
- production targets;
- factory monitor summary;
- production estimates and bottlenecks;
- site summaries;
- site-to-site logistics links;
- threats and recent damage;
- power networks;
- research planning;
- build-item mall pressure;
- layout improvement context;
- implemented skill catalog;
- dependency tree toward the objective.

The LLM should output strict JSON with one high-level skill:

```json
{
  "selected_skill": "automate_electronic_circuit_line",
  "priority": 85,
  "reason": "Automation is researched and circuit throughput is below target.",
  "evidence": ["electronic-circuit target deficit"],
  "blockers": ["assembler-based electronic circuit production"],
  "expected_effect": "Build the first powered green circuit cell."
}
```

After the LLM response, `reconcile_strategy_decision` applies deterministic
guardrails. Example: if the LLM selects `produce_electronic_circuit` for a
per-minute electronic-circuit deficit after Automation is researched, the
decision is promoted to `automate_electronic_circuit_line`. This preserves the
LLM source but prevents strategic regression into repeated hand crafting. The
promotion now applies even if an isolated or remote circuit cell exists, because
the monitor's starter-usable target deficit means hand production is still the
wrong sustained-rate response.

## Implemented Skill Executors

Currently implemented strategy skills include:

- `produce_iron_plate`
- `produce_copper_plate`
- `produce_automation_science_pack`
- `produce_electronic_circuit`
- `build_belt_smelting_line`
- `expand_iron_smelting`
- `expand_copper_smelting`
- `setup_power`
- `research_automation`
- `automate_electronic_circuit_line`
- `research_logistics`
- `bootstrap_build_item_mall`
- `build_starter_defense`
- `plan_factory_site`

Important distinction:

- `produce_*` skills are bootstrap or stock skills.
- `expand_*` and `automate_*` skills address sustained per-minute throughput.
- `plan_factory_site` is simulation-only and must not build, mine, demolish, or
  move entities.

## Current Runtime Situation

The current no-mod observation has recently shown:

- Automation is researched.
- Current research is `logistics`.
- `electronic-circuit` target is nonzero.
- The LLM has selected `produce_electronic_circuit` in some runs.
- That is strategically insufficient for sustained throughput, so the local
  guardrail promotes it to `automate_electronic_circuit_line` when appropriate.
- A live no-mod run after the strategy guardrail successfully built and ran the
  first circuit automation cell:
  - `small-electric-pole` at roughly `20.5,-801.5`;
  - cable assembler at roughly `18.5,-799.5`, recipe `copper-cable`;
  - circuit assembler at roughly `22.5,-799.5`, recipe `electronic-circuit`;
  - transfer inserter at roughly `20.5,-799.5`;
  - copper plates were inserted into the cable assembler;
  - iron plates and copper cable were inserted into the circuit assembler;
  - electronic circuits were produced and collected.
- Verification command:
  `python -m factorio_ai.cli run-no-mod-circuit-automation-mvp --max-steps 40`.
  Latest verified result was `ok: true`, reason
  `circuit automation cell is running and target reached: 7/5`.
- After this, `no-mod-strategy --objective launch_rocket_program --require-llm`
  selected `expand_iron_smelting`, because iron plate throughput became the
  next reported rocket-program bottleneck.
- Burner smelting expansion no longer fails immediately when a line needs fuel
  and the nearest coal patch is far away. The deterministic executor now first
  recovers surplus coal from nearby fueled machines while preserving reserve
  coal, then inserts it into the under-fueled drill, burner inserter, or furnace.
- Live no-mod verification after this change:
  - `run_expand_iron_smelting_mvp(max_steps=6)` recovered surplus coal from a
    stone furnace, inserted coal into a burner mining drill, inserted coal into
    a burner inserter, moved to another surplus source, and recovered more coal;
  - `run_expand_iron_smelting_mvp(max_steps=3)` inserted coal into the target
    stone furnace, then made an emergency haul to a coal patch and mined 8 coal.
- This is still not full coal logistics automation. The next implementation
  should build a coal belt/feed line, staged coal cache, or electric upgrade
  path so burner smelting expansion does not depend on repeated long coal walks.
- `setup_coal_supply` now exists as the first dedicated fuel-logistics
  executor. It builds an output belt and burner mining drill on a coal patch,
  inserts starter coal, and lets the monitor classify the result as a fueled
  `mining_patch` coal site.
- New lab, circuit automation, and build-item-mall executor site selection now
  ignores starter-phase candidates outside the starter logistics radius. The
  current map contains older remote blocks hundreds of tiles away; treat those
  as layout problems or future rail outposts, not as valid local starter sites.
- Bootstrap iron/copper support selection also ignores remote furnaces and
  miners before rail logistics. This matters because expansion skills call
  `IronPlateSkill` / `CopperPlateSkill` for prerequisites; those support calls
  must not walk the real player back to old remote plate outputs.
- Live no-mod verification:
  - command:
    `python -m factorio_ai.cli run-no-mod-strategy-step --objective launch_rocket_program --max-steps 30`;
  - result:
    `selectedSkill: setup_coal_supply`, `ok: true`, `steps: 13`,
    reason `coal supply site is active with fueled burner mining drill and output belt`;
  - monitor saw one fueled coal `mining_patch` and site-level coal logistics
    links from that patch to nearby smelting/power consumers.
- `connect_coal_fuel_feed` now completes the local coal fuel route from the
  starter coal belt to a nearby fuel consumer:
  - command:
    `python -m factorio_ai.cli run-no-mod-strategy-step --objective launch_rocket_program --max-steps 35`;
  - result:
    `selectedSkill: connect_coal_fuel_feed`, `ok: true`, `steps: 11`,
    reason `coal fuel feed is active: belt and burner inserter are feeding a furnace fuel inventory`;
  - monitor now marks the close coal link as `route_observed`, while the far
    iron-smelting and steam-power coal links remain `route_needed`.
- After refueling the source coal drill with another strategy step, the next
  no-mod strategy moved on to `produce_iron_plate`, proving the coal
  supply/feed loop no longer traps the strategy layer.

Next practical runtime check:

```powershell
$env:PYTHONPATH="src"
$env:FACTORIO_AI_SLURM_ENABLED="1"
$env:FACTORIO_AI_SLURM_MODE="queue"
$env:FACTORIO_AI_SLURM_REMOTE_DIR="~/factorio-ai-worker"
$env:FACTORIO_AI_SLURM_JOB_NAME="factorio-ai-worker"
python -m factorio_ai.cli no-mod-strategy --objective launch_rocket_program --require-llm
python -m factorio_ai.cli run-no-mod-strategy-step --objective launch_rocket_program --require-llm --max-steps 80
```

Expected near-term behavior:

1. LLM may choose `produce_electronic_circuit`.
2. Guardrail should promote it to `automate_electronic_circuit_line` if the
   rate deficit and Automation research conditions are met.
3. The skill should build or repair a powered green circuit assembler cell.
4. Background layout improvement should continue while the active skill runs.

## Background Layout Improvement

While a long-running skill is executing, the controller can submit a background
layout-improvement task. Default mode is attached `srun` against the active
Slurm allocation.

The same mechanism is used when the strategic LLM selects a skill whose
deterministic executor is missing or still being implemented. The controller
logs the active skill as `codex_wait:<skill-name>` and keeps submitting
simulation-only site-layout review tasks until Codex finishes the executor
work and the next strategy cycle can run it.

The active blocked executor is persisted in:

```text
runtime/codex-wait.json
```

When the user asks Codex to implement a missing build-item/site executor, start
the wait state before editing so Slurm keeps doing useful LLM work while Codex
is busy:

```powershell
$env:PYTHONPATH="src"
$env:FACTORIO_AI_SLURM_ENABLED="1"
$env:FACTORIO_AI_CODEX_WAIT_LAYOUT_AUTOSTART="1"
python -m factorio_ai.cli begin-codex-work --no-mod --objective launch_rocket_program --selected-skill <skill-name> --reason "Codex is implementing the missing deterministic executor."
```

After tests pass and the part is committed/pushed, clear it:

```powershell
$env:PYTHONPATH="src"
python -m factorio_ai.cli finish-codex-work --no-mod --selected-skill <skill-name> --reason "Codex implementation completed and pushed"
```

Autopilot reads this file at the start of each cycle and submits a layout
heartbeat before asking for another strategy step. This means that if a
`bootstrap_build_item_mall`-style executor takes time to implement, the Slurm
LLM should continue simulation-only site layout improvement until the executor
exists and the wait state is cleared.

There is also an opportunistic idle loop for keeping GPUs busy whenever the
game executor is not actively making progress:

```text
run_factorio_no_mod_idle_layout_loop.bat
python -m factorio_ai.cli run-no-mod-idle-layout-loop --objective launch_rocket_program
```

Autopilot writes `runtime/autopilot-heartbeat.json`. The idle loop submits
layout-improvement tasks when that heartbeat is missing, stopped, sleeping,
failed, or older than `--stale-seconds` (default 15s). Fresh `cycle_start` style
heartbeats are treated as busy and pause new idle submissions. This loop is a
GPU filler: it must not apply builds to the map; it only produces simulated
site candidates and logs them to the dashboard.

Environment:

```powershell
$env:FACTORIO_AI_BACKGROUND_LAYOUT_ENABLED="1"
$env:FACTORIO_AI_BACKGROUND_LAYOUT_MODE="attach"
$env:FACTORIO_AI_BACKGROUND_LAYOUT_INTERVAL_SECONDS="20"
$env:FACTORIO_AI_CODEX_WAIT_LAYOUT_AUTOSTART="1"
```

Log:

```text
logs/layout-improvement-background.jsonl
```

Windows helper:

```text
run_factorio_no_mod_codex_wait_layout_loop.bat
```

The result must contain `no_apply: true`. It is only a simulation or review.
The LLM should compare:

- footprint;
- site-to-site distance;
- recipe/machine ratio;
- belt capacity;
- inserter throughput;
- missing or incomplete links;
- resource-tile obstruction;
- power connectivity;
- room for future expansion.

Current simulation candidates include:

- green circuit 3 cable : 2 circuit cell;
- parallel smelting columns;
- lab daisy-chain science feed;
- starter mall compaction;
- flow-shortening moves;
- extra belt lanes when yellow belt capacity is exceeded.

## Web Dashboard

Public URL:

```text
http://27.115.156.173:8787/factorio
```

Dashboard areas:

- desired production targets;
- estimated production and consumption;
- factory sites grouped by site;
- site-to-site logistics links;
- power networks;
- threats and recent damage;
- LLM decisions;
- Codex token usage;
- layout improvement issues, opportunities, and candidates.

Current grouped factory sites expose a blueprint copy button. Simulation candidates expose separate
`variant=before` and `variant=after` copy buttons so model-evaluation jobs can compare the observed
current footprint with the proposed replacement footprint. The site/candidate blueprint is fetched
through `/factorio/blueprint?...` first, with `/api/factorio/blueprint?...` kept as a local fallback;
do not render the raw blueprint string in dashboard HTML. This lets a later Codex/CLI/Claude session
copy or collect successful AI-built site layouts for review and fine-tuning examples without blindly
placing unvalidated external blueprints.

The dashboard should remain bilingual EN/KR where practical.

## Training And Fine-Tuning Direction

Do not start by fine-tuning raw gameplay. First collect structured training
data.

Useful trace sources:

- `logs/llm_decisions.jsonl`
- `logs/layout-improvement-background.jsonl`
- strategy-step logs under `logs/strategy-*.jsonl`
- runtime missing-skill records;
- user edits or manual multiplayer changes;
- successful factory site summaries;
- failed action responses and final observations.

Suggested dataset records:

```json
{
  "task_type": "strategy_selection",
  "observation_summary": {},
  "factory_monitor": {},
  "available_skills": [],
  "selected_skill": "automate_electronic_circuit_line",
  "guardrail_adjusted": false,
  "outcome": {"ok": true, "next_observation_delta": {}}
}
```

For layout learning:

```json
{
  "task_type": "layout_review",
  "site_graph": {},
  "candidate": {},
  "static_rate_estimate": {},
  "selected_candidate_id": "green-circuit-3-cable-2-circuit-cell",
  "outcome": "simulation_only"
}
```

Fine-tuning should start with small strategy/layout adapters before trying to
train an end-to-end game controller. The deterministic skills remain the
authority for exact game actions.

## External Design References

Use these for concepts and tests, not blind copy-paste:

- https://factorioprints.com/
- https://mods.factorio.com/mod/RateCalculator
- https://mods.factorio.com/mod/mining-patch-planner
- https://github.com/rimbas/mining-patch-planner
- https://mods.factorio.com/mod/OilOutpostPlanner
- https://github.com/Coppermine-factorio/OilOutpostPlanner
- https://lua-api.factorio.com/latest/classes/LuaEntity.html

Reference mapping:

- RateCalculator: static production/consumption estimates, rate ratios, belt
  and inserter bottleneck checks.
- Mining Patch Planner: miner placement and patch coverage without wasting
  resource tiles.
- Oil Outpost Planner: pumpjack grouping, pipe routing, power coverage, and
  remote outpost planning.
- Factorio Prints: reusable design principles and topology patterns, not
  direct blueprint pasting.

## Test And Commit Procedure

Before finishing a part:

```powershell
git status --short
$env:PYTHONPATH="src"
python -m unittest discover -s tests
```

If Slurm strategy changed:

```powershell
python -m factorio_ai.cli slurm-deploy
python -m factorio_ai.cli no-mod-strategy --objective launch_rocket_program --require-llm
```

If game execution changed:

```powershell
python -m factorio_ai.cli run-no-mod-strategy-step --objective launch_rocket_program --require-llm --max-steps 80
```

Record token usage for the web dashboard:

```powershell
python -m factorio_ai.cli record-token-usage --tokens-used <current_goal_tokens> --label "<part label>" --source codex
```

Commit and push each completed part:

```powershell
git add <changed files>
git commit -m "Part NN: concise description"
git push origin master
```

Current remote:

```text
https://github.com/Schwalbe262/Factorio_automation.git
```

## Next Work Candidates

The highest-value next tasks are:

1. Implement deterministic fuel logistics for burner-era smelting expansion:
   coal belt/feed line, staged coal cache, or an electric-miner/electric-smelter
   upgrade path once the required tech and items exist.
2. Improve `expand_iron_smelting` / `expand_copper_smelting` so plate
   expansion does not scatter one-off burner rows and does not stall on distant
   coal without a logistics plan.
3. Continue `research_logistics` after iron/copper throughput is stable enough
   to feed science and build items.
4. Build a proper red science/lab feed loop instead of manual lab insertion.
5. Improve power placement so the first steam block is near spawn/factory
   unless water forces distance.
6. Add stronger site graph learning from human edits and successful layouts.
7. Add structured export for fine-tuning examples.
8. Implement next missing executors for green science, mall expansion, rails,
   oil, and later rocket-silo chain.
