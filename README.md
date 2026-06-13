# Factorio AI Autoplayer

Layered Factorio AI autoplayer MVP.

The local machine runs Factorio and controls it through RCON. There are two development adapters:

1. No-custom-mod RCON/Lua for multiplayer-compatible vanilla servers. Other players do not need the
   Factorio AI mod, but RCON `/silent-command` Lua disables achievements for that save.
2. The older custom-mod development API, which is faster to iterate on but requires every multiplayer
   client to have the same mod setup.

The custom mod exposes a small command API:

- `/ai_observe`
- `/ai_action <json>`
- `/ai_wait <ticks>`

Python owns the safety checks, orchestration, logs, and planner loop. Slurm is optional and is used for higher-latency LLM planning or large evaluation jobs, not for directly mutating the Factorio world.

## Control Philosophy

The LLM is required for strategic play, but it must not drive the game one tick at a time.

The intended split is:

1. Strategic LLM layer: choose the next high-level skill from the current game state.
2. Skill layer: execute stable routines such as `produce_iron_plate`, `setup_power`, or `produce_electronic_circuit`.
3. Executor layer: translate skill actions into either development RCON actions or vanilla keyboard/mouse input.

Example:

```text
Need electronic circuits -> iron plate throughput is too low -> run expand_iron_smelting.
```

The LLM stops at the skill choice and bottleneck explanation. The skill code handles walking,
mining, building, item insertion, retries, and local validation.

If the strategic LLM selects a skill whose executor does not exist yet, the controller must not fake
or improvise game actions. It records the missing skill in `runtime/missing-skills.jsonl`; Codex then
implements that skill as a normal code change.

## LLM Context Budget

Do not confuse the Codex/handoff context with the local Slurm LLM prompt context.

- `README.md`, `AGENT_HANDOFF.md`, and similar documents are long-form handoff material for humans,
  Codex, and other coding agents. They are not injected wholesale into every strategy request.
- The active 4B and 9B Slurm workers run vLLM with `--max-model-len 32768` by default. This is an
  execution setting, not the native Qwen context limit. Qwen3.5 supports much longer contexts, but
  the worker setting should be increased in measured steps after checking GPU memory and latency.
  The 27B comparison worker is also queued with `--max-model-len 32768`.
- Strategy requests send a bounded current-state payload: objective, inventory, target deficits,
  bottlenecks, site/link summaries, layout simulation candidates, power state, research state,
  threats, and implemented skill names. The 4B fast worker test budget is now 16KB+ JSON payloads,
  because the worker context limit is 32K tokens; larger raw design archives should still use
  retrieval or the larger worker path.
- Long-term Factorio knowledge should be added through structured recipe/technology data, design
  pattern summaries, and retrieval of relevant blueprint notes, not by appending a 300k markdown file
  to every LLM call.

## Current MVP

- Observe player position, inventory, nearby resources, nearby entities, and craftable recipes.
- Execute allowlisted actions only.
- Run a rule-based `produce_iron_plate` skill until at least 10 iron plates exist in inventory or machine outputs.
- Run a reusable rule-based `produce_copper_plate` skill until copper plates exist in inventory or machine outputs.
- Run a rule-based `produce_automation_science_pack` skill until at least 5 automation science packs exist.
- Run a rule-based `produce_electronic_circuit` skill for early hand-crafted green circuits.
- Run one strategic step with `run-strategy-step`, which asks the strategic layer for a skill and executes it only if a local executor exists.
- Build a minimal belt-fed iron smelting line with `build_belt_smelting_line`.
- Build the first steam power block with `setup_power`: offshore pump, boiler, steam engine, and small electric pole.
- Ask the strategic layer for the next high-level skill with `factorio-ai strategy`.
- Submit planner tasks to a Slurm worker queue when configured, with local rule-based fallback.

## Requirements

- Windows Factorio install. Default path:
  `C:\Program Files (x86)\Steam\steamapps\common\Factorio\bin\x64\factorio.exe`
- Python 3.10+
- Git
- Optional: SSH/SCP access to the Slurm login node.

## Quick Start

Install the project in editable mode:

```powershell
python -m pip install -e .
```

Install the mod into the local runtime mod directory:

```powershell
factorio-ai install-mod
```

Create a save:

```powershell
factorio-ai create-save
```

Start a local Factorio server with RCON:

```powershell
factorio-ai start-server
```

Launch a visible GUI client connected to that local server:

```powershell
factorio-ai launch-gui
```

`launch-gui` calls `factorio.exe` directly with a separate client config under `runtime/client-data`.
This avoids the Steam launch confirmation dialog and lets the dedicated server and GUI client run
at the same time on the local machine.

For a saved-map GUI demo, use:

```powershell
factorio-ai launch-save-gui
```

Or run the Windows helpers from the repository root:

```bat
run_factorio_non_gui.bat
run_factorio_gui.bat
run_factorio_review_gui.bat
run_factorio_watch_gui.bat
run_factorio_no_mod_server.bat
run_factorio_no_mod_watch_gui.bat
run_factorio_no_mod_iron_mvp.bat
run_factorio_no_mod_llm_autopilot.bat
```

`run_factorio_non_gui.bat` starts the development server in a separate window and repeatedly executes
strategic steps. `run_factorio_gui.bat` opens the configured save for visual inspection.
`run_factorio_review_gui.bat` is for interruptible manual inspection: it connects a GUI client to the
current AI server, creates `runtime\review.lock` while the GUI is open, and the non-GUI loop waits on
that lock before continuing. Close the Factorio window when inspection is done.
`run_factorio_watch_gui.bat` now delegates to the no-custom-mod watch helper. It creates a
vanilla-compatible save, starts a LAN/RCON server, and opens a GUI client connected to it. Other
players can join the LAN server without installing the Factorio AI mod, assuming their official
Factorio/Space Age content matches.
`run_factorio_no_mod_llm_autopilot.bat` starts the continuous no-custom-mod autopilot with
Slurm LLM strategy required. It fails instead of silently falling back to heuristics when the
active 4B worker is not ready.

The older modded development server is still launched internally with `--start-server` plus GUI
`--mp-connect`, but it is configured for one local review client by default. Use it for fast skill
iteration only, not for public multiplayer compatibility.

## No-Custom-Mod RCON/Lua Track

This is now the preferred path when multiplayer compatibility matters more than Steam achievements.
It runs with only official Factorio/Space Age mods enabled and uses trusted RCON `/silent-command`
Lua for observation and allowlisted player/server actions.

Create the save and start the LAN/RCON server:

```powershell
factorio-ai create-no-mod-save
factorio-ai start-no-mod-server
```

Observe the current server state without the custom AI mod:

```powershell
factorio-ai no-mod-observe
```

Open a GUI client connected to the no-custom-mod server:

```powershell
factorio-ai launch-no-mod-gui
```

Run the first no-custom-mod iron plate automation proof:

```powershell
factorio-ai run-no-mod-iron-mvp --target 10 --max-steps 120
```

Run the next no-custom-mod material proofs:

```powershell
factorio-ai run-no-mod-copper-mvp --target 5 --max-steps 140
factorio-ai run-no-mod-circuit-mvp --target 2 --max-steps 180
factorio-ai run-no-mod-science-mvp --target 1 --max-steps 260
```

Ask and execute the strategic layer through the no-custom-mod adapter:

```powershell
factorio-ai no-mod-strategy --objective launch_rocket_program
factorio-ai run-no-mod-strategy-step --objective launch_rocket_program --target 3 --max-steps 120
```

Or use:

```bat
run_factorio_no_mod_iron_mvp.bat
```

The no-mod observation currently reports tick, player/server focus, inventory, craftable recipes,
nearby resources, factory entities, enemies, pollution, and key research technologies. Existing
monitor/site/link estimation reads those observation fields directly, so human-built factory changes
become visible as soon as they are inside the observation radius. The remaining work is to port each
skill executor from the custom mod API to this no-mod adapter.

The web dashboard first tries the custom mod observation endpoint, then falls back to the no-mod
RCON/Lua observer. If no Factorio RCON server is running, it now renders an operator-facing offline
message instead of exposing a raw connection refused traceback.

In another terminal, run the iron plate MVP loop:

```powershell
factorio-ai run-iron-mvp --target 10
```

Run the copper plate MVP loop:

```powershell
factorio-ai run-copper-mvp --target 10
```

Run the electronic circuit MVP loop:

```powershell
factorio-ai run-circuit-mvp --target 5
```

Build a minimal belt-fed smelting line:

```powershell
factorio-ai run-belt-smelting-mvp --target 10 --max-steps 700
```

Build the first steam power block:

```powershell
factorio-ai run-power-mvp --max-steps 900
```

Run one strategy-selected skill:

```powershell
factorio-ai run-strategy-step --objective launch_rocket_program
```

Or run the automation science MVP loop from a fresh server save:

```powershell
factorio-ai run-science-mvp --target 5 --max-steps 500
```

Ask the strategic layer what to do next:

```powershell
factorio-ai strategy --objective "produce electronic circuits"
```

Start the local production monitor:

```powershell
factorio-ai web
```

The command binds to `0.0.0.0` by default and prints the reachable public gateway URL. Open the URL it prints,
for example:

```text
http://27.115.156.173:8787/factorio
```

`/factorio` is the canonical route. `/팩토리오` redirects to `/factorio?lang=ko`.
The dashboard can switch between EN and KR from the header.

The monitor has no login, no admin role, and no session expiry. It shows estimated production,
estimated consumption, net rates, target deficits, bottlenecks, dependency tree, and technology
chain. It also shows recent player/robot factory edits from the development mod and early throughput
constraints such as recipe ratios, belt capacity, and inserter transfer estimates. Desired production
targets are editable per item. If user targets are satisfied, the
strategic LLM may raise targets or add the next rocket-program item automatically.

For example, vanilla electronic circuits require 3 copper cable assemblers for every 2 electronic
circuit assemblers at the same assembler tier: one copper cable assembler outputs 120 cable/min in an
assembling-machine-1, while one circuit assembler consumes 180 cable/min.

Use this for production runs when a real LLM endpoint is configured:

```powershell
$env:FACTORIO_AI_SLURM_ENABLED=1
$env:FACTORIO_AI_REQUIRE_LLM_STRATEGY=1
factorio-ai strategy --objective "launch_rocket_program"
```

For the no-custom-mod autopilot path, use:

```cmd
run_factorio_no_mod_llm_autopilot.bat
```

## Achievement-Compatible Track

The current `factorio-ai` engine is a development and verification track. It uses a mod, RCON,
and Lua commands, so it is not intended for Steam achievement runs.

Achievement-compatible play must run as a separate vanilla track:

- no non-official mods;
- no Lua console commands;
- no RCON world mutation;
- one normal Factorio client controlled through ordinary keyboard and mouse input;
- screen or save-independent perception for inventory, map, entities, and production state.

The planner and skill code should stay portable across both tracks. The mod/RCON track is used to
iterate quickly and prove behavior, then the vanilla track reuses the same high-level decisions with
a different executor.

Launch the normal Steam game for this track:

```powershell
factorio-ai launch-vanilla-gui
```

Or use the Windows helper:

```bat
run_factorio_vanilla_gui.bat
run_factorio_vanilla_freeplay.bat
run_factorio_vanilla_probe.bat
run_factorio_restore_steam_mods.bat
```

Steam vanilla launch backs up `%APPDATA%\Factorio\mods\mod-list.json` under
`runtime\vanilla\steam-mod-list-backups`, then writes a vanilla Space Age mod list containing only
`base`, `elevated-rails`, `quality`, and `space-age`. This avoids passing custom launch arguments
through Steam and prevents enabled user mods from stopping the run from being a vanilla/achievement
candidate. `run_factorio_restore_steam_mods.bat` restores the latest backup when the user wants the
normal mod setup back. The vanilla executor must navigate the normal GUI, including New Game ->
Freeplay (Space Age), with ordinary keyboard and mouse input. Any path using non-official mods,
RCON, or Lua commands belongs to the development track and must not be used for achievement runs.
`launch-vanilla-gui` reports failure if the real `factorio.exe` game window is not detected, and
`vanilla-window` includes diagnostics for false positives such as Steam's Factorio settings window
or Factorio-owned error dialogs.

To open a new vanilla Freeplay (Space Age) run from the main menu:

```powershell
factorio-ai vanilla-start-freeplay
```

This is a low-level menu executor. It assumes the vanilla main menu is visible and uses normal mouse
clicks and keyboard input. The current verified path enters Single Player -> New Game -> Freeplay
(Space Age), starts the map with default generation settings, presses `Tab` to skip the intro, and
then leaves the player controllable in the actual game world.

The vanilla track changes the low-level algorithm, not the whole AI:

- shared: LLM strategy, production targets, bottleneck reasoning, tech-tree planning, blueprint lessons, and skill selection;
- development adapter: exact Lua/RCON observation plus allowlisted action execution for fast verification;
- vanilla adapter: screen/window capture, OCR or visual classifiers, hotkeys, mouse clicks, and ordinary keyboard movement;
- required contract: the same skill intent, such as `expand_iron_smelting`, must be executable through either adapter.

Useful vanilla diagnostics:

```powershell
factorio-ai vanilla-window
factorio-ai vanilla-screenshot --output runtime\vanilla\screenshots\current.bmp
factorio-ai vanilla-screen-state --output runtime\vanilla\screenshots\screen-state.bmp
factorio-ai vanilla-probe --minimize-check
```

For background use, keep the Factorio window open but not minimized. The default screenshot method
uses `PrintWindow`, which can capture the Factorio window even when another app covers it. Minimized
play is different: the current probe only gets a small title-bar-sized frame after minimization, so
minimized automation is not enabled. Foreground `SendInput` remains the reliable
achievement-compatible input path; background `PostMessage` can be probed, but Factorio must prove it
actually consumes those inputs before background movement/building is trusted.

Low-level vanilla input is available for controlled probes:

```powershell
factorio-ai vanilla-key shift
factorio-ai vanilla-key shift --background
```

Use foreground input for real play. Background key posting is only a capability probe until state
changes prove that Factorio consumed the input.

## Blueprint Library

Rocket-scale play should use proven layouts instead of inventing every production block from
scratch. `factorio_ai.blueprints` decodes Factorio blueprint exchange strings and summarizes entity
counts, so blueprint sources such as Factorio Prints can feed the planner as candidate designs.
The goal is not blind copy/paste. The code infers a blueprint lesson: likely purpose, bottlenecks,
design principles, and rough ratios.

The intended planner flow is:

1. Decode candidate blueprints.
2. Infer why the design exists and which bottleneck it addresses.
3. Convert useful explanations into fine-tuning examples.
4. Ask the Slurm LLM worker to rank designs against the current game state.
5. Execute only validated skill/build actions through the active executor.

## Layout Improvement Simulation

`plan_factory_site` is implemented as a safe planning skill. It does not demolish or build anything.
When urgent production, defense, research, and power work are not blocking progress, the LLM can use
idle strategy cycles to inspect the current site graph and generate simulated improvement candidates.

The current layout evaluator separates:

- hard issues: disconnected power, incomplete logistics links, manual fuel/feed, remote starter power,
  or production blocks placed on starter resource patches;
- optimization opportunities: nonstandard smelting rows, inefficient green-circuit ratios, long
  intermediate-item flows, manual lab feed, scattered mall cells, and belt-capacity risk;
- simulation candidates: before/after estimates for pattern changes such as `3 cable : 2 circuit`
  green-circuit cells, parallel smelting columns, lab daisy chains, mall compaction, flow shortening,
  and extra belt lanes.

The simulation is RateCalculator-style static reasoning: recipe rates, machine counts, estimated
item flow, belt capacity, footprint, and distance are scored without applying the layout to the live
map. A future build command can take the best saved candidate and hand it to a deterministic executor
that validates exact tiles, collisions, power, belts, inserters, and resource preservation before
building.

## Spatial And Rail Planning

Factory placement is a strategic concern, not only a build-detail concern. The strategic payload
includes site-selection and rail-network context so the LLM can choose where a production district,
logistics corridor, or train outpost should go before a deterministic executor validates exact tiles.

When a required resource patch or factory district is far from the current base, the planner should
prefer a rail supply line over long walking loops or very long belts after rail technology and
materials are available. The current catalog exposes `plan_rail_network` and `build_rail_supply_line`
as future skills; until those executors exist, selecting them records a missing-skill backlog entry
instead of faking rail construction.

## Automation Skill Boundary

The first material skills can still hand-mine and hand-craft because they bootstrap a new world.
That is not enough for rocket-scale play. Factory growth must move to automation skills that build
working production blocks:

- miners extract ore;
- belts move items between blocks;
- inserters move items into and out of machines;
- furnaces smelt plates;
- assembling machines craft intermediates;
- power poles and fuel keep the block running.

For this reason, `expand_iron_smelting`, `build_belt_smelting_line`, `setup_power`, and
`automate_electronic_circuit_line` are separate skill contracts. The first belt-smelting and steam
power executors now exist; the remaining higher-throughput smelting and assembling-machine executors
are still reported as missing instead of substituting a hand-crafting routine.

## Space Age Objective

The long-term target extends past the first rocket. The AI should eventually launch rockets, build
space platforms, design compact space-efficient spacecraft, and produce space science packs. That
requires a stronger design layer than the early Nauvis skills:

- strategic LLM: select the next planet/platform/science objective and diagnose bottlenecks;
- design learner: study blueprints and successful layouts to infer ratios, footprints, and logistics principles;
- deterministic executors: place validated belts, inserters, assemblers, rails, power, pipes, rockets, and platform entities;
- fine-tuning loop: save successful and failed layout decisions as training examples for future local models.

The current belt-smelting skill is intentionally small, but it establishes the executor pattern
needed for later compact factory and spacecraft layout skills.

## Enemy Awareness

The observation API reports hostile `unit`, `unit-spawner`, and `turret` entities as `enemies`.
The strategic payload summarizes nearest enemy distance, spawner/turret presence, type/name counts,
and a danger level. Close hostile pressure selects `build_starter_defense` before blindly expanding
production toward nests.

The development mod also records enemy-caused `damaged` and `destroyed` factory events. The monitor
shows threat context, recent damage, armed turret count, and whether pollution has reached observed
spawners. That gives the planner enough evidence to pause expansion, rebuild broken sites, and queue
defensive work.

The current movement executor does not yet route around nests. The next vanilla-compatible movement
layer must treat enemy bases and polluted spawners as high-cost zones and move through safe waypoints
instead of sending a single direct `move_to` target.

## Build Item Mall

Scaling to rockets requires automating expansion items, not just plates. The strategy payload exposes
`build_item_supply` for belts, inserters, burner inserters, burner drills, stone furnaces, small poles,
and assembling machines. LLM strategy can select `bootstrap_build_item_mall` when the factory is
blocked by hand-crafted build items. The executor for that skill is still pending; until then, missing
selection is logged for Codex implementation.

Resource-specific miner placement is validated with `required_resource` so a copper expansion drill
cannot silently slide onto iron ore when `allow_nearby` is used.

## Learning Loop

The model should improve over time instead of relying only on a commercial LLM forever.

Data sources to save:

- strategic decisions: observation, selected skill, reason, blockers, result;
- failed skill runs: last observation, failed action, local rejection reason;
- successful factory blocks: objective, blueprint lesson, required tech, production effect;
- vanilla GUI runs: screen state, selected skill, input sequence summary, outcome.

Fine-tuning target:

- input: current objective, summarized game state, available skills, candidate blueprint lessons;
- output: selected high-level skill, bottleneck diagnosis, expected effect, safety notes.

The fine-tuned model should still operate only at the strategic layer. Skill execution remains
deterministic and locally validated.

## Slurm Worker

The remote worker follows the same shape as the reference projects:

- `queue/`
- `running/`
- `results/`
- `failed/`
- `logs/`

Default remote directory: `~/factorio-ai-worker`

Default job name: `factorio-ai-worker`

The Factorio worker owns its Slurm queue and job name by default. This keeps it separate from the
Kakao/Telegram bot `AUTO` worker and from crypto-lab jobs such as `optimization`, so a cancel action
in another project does not accidentally cancel the Factorio LLM worker.

Legacy attach mode still exists for experiments: if `FACTORIO_AI_SLURM_JOB_NAME=AUTO`,
`FACTORIO_AI_SLURM_REMOTE_DIR=~/kakao-bot-worker`, and `FACTORIO_AI_SLURM_MODE=attach` are set,
planner requests use `srun --jobid=<AUTO_JOB_ID> --overlap` inside the existing Kakao allocation.
That mode is no longer the default because shared `AUTO` ownership is easy to confuse.

Common environment variables:

- `SUPERCOMPUTER_WORKER_SSH_HOST`
- `SUPERCOMPUTER_WORKER_SSH_USER`
- `SUPERCOMPUTER_WORKER_SSH_KEY`
- `SUPERCOMPUTER_WORKER_SSH_PORT`
- `FACTORIO_AI_SLURM_REMOTE_DIR=~/factorio-ai-worker`
- `FACTORIO_AI_SLURM_JOB_NAME=factorio-ai-worker`
- `FACTORIO_AI_SLURM_ENABLED=1`
- `FACTORIO_AI_SLURM_GPUS_PER_NODE=1`
- `FACTORIO_AI_SLURM_GRES=gpu:1`
- `FACTORIO_AI_SLURM_PARTITION=gpu4,gpu2,gpu1`
- `FACTORIO_AI_VLLM_MODEL=Qwen/Qwen3.5-4B`
- `FACTORIO_AI_LLM_BASE_URL=http://127.0.0.1:8000/v1`
- `FACTORIO_AI_LLM_MODEL=<model-name>`

Start or reuse the current active 4B worker:

```cmd
run_factorio_slurm_llm_4b_worker.bat
```

After code changes, do not cancel and resubmit the worker just to verify Python-side logic. Keep the
existing allocation and attach a one-shot benchmark with `srun`:

```cmd
run_factorio_slurm_llm_4b_attached_benchmark.bat
```

Equivalent PowerShell:

```powershell
$env:FACTORIO_AI_SLURM_ENABLED="1"
$env:FACTORIO_AI_SLURM_REMOTE_DIR="~/factorio-ai-worker"
$env:FACTORIO_AI_SLURM_JOB_NAME="factorio-ai-worker"
$env:FACTORIO_AI_SLURM_GPUS_PER_NODE="1"
$env:FACTORIO_AI_SLURM_GRES="gpu:1"
$env:FACTORIO_AI_SLURM_PARTITION="gpu4,gpu2,gpu1"
$env:FACTORIO_AI_VLLM_MODEL="Qwen/Qwen3.5-4B"
$env:FACTORIO_AI_VLLM_ARGS="--max-model-len 32768 --gpu-memory-utilization 0.85 --enforce-eager"
$env:FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER="0"
factorio-ai slurm-start-worker
```

The larger 9B and 27B workers are kept as separate worker jobs so they can wait or run without
blocking the active 4B worker:

```cmd
run_factorio_slurm_llm_9b_worker.bat
```

For code-change verification on the already running 9B allocation:

```cmd
run_factorio_slurm_llm_9b_attached_benchmark.bat
```

The 9B worker uses:

- `FACTORIO_AI_SLURM_REMOTE_DIR=~/factorio-ai-worker-9b`
- `FACTORIO_AI_SLURM_JOB_NAME=factorio-ai-worker-9b`
- `FACTORIO_AI_SLURM_GPUS_PER_NODE=1`
- `FACTORIO_AI_SLURM_GRES=gpu:a6000:1`
- `FACTORIO_AI_SLURM_PARTITION=gpu4`
- `FACTORIO_AI_VLLM_MODEL=Qwen/Qwen3.5-9B`
- `FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER=0`

```cmd
run_factorio_slurm_llm_27b_gpu3_queue.bat
```

It uses:

- `FACTORIO_AI_SLURM_REMOTE_DIR=~/factorio-ai-worker-27b`
- `FACTORIO_AI_SLURM_JOB_NAME=factorio-ai-worker-27b`
- `FACTORIO_AI_SLURM_GPUS_PER_NODE=3`
- `FACTORIO_AI_SLURM_GRES=gpu:a6000ada:3`
- `FACTORIO_AI_SLURM_PARTITION=gpu3`
- `FACTORIO_AI_VLLM_MODEL=Qwen/Qwen3.6-27B-FP8`
- `FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER=0`

If the worker is pending, `slurm-llm-status` reports `Slurm worker job pending GPU allocation`.
If it is running without visible CUDA devices, the same command reports `GPU allocation` in
`missing`.

Submit a test task:

```powershell
factorio-ai slurm-submit-test
```

Check whether the worker allocation can actually run LLM strategy requests:

```powershell
factorio-ai slurm-llm-status
```

`slurm-status` only proves that the allocation exists. `slurm-llm-status` checks whether
`FACTORIO_AI_LLM_BASE_URL` and `FACTORIO_AI_LLM_MODEL` are visible inside the attached Slurm task,
whether `nvidia-smi -L` sees a GPU, whether `/v1/models` responds from the LLM endpoint, and whether
the Factorio AI code has been deployed under the remote worker directory.

## GitHub Workflow

This repository is intended to be committed and pushed by part:

1. Project skeleton.
2. Observation system.
3. Safe action execution.
4. Iron plate MVP.
5. Slurm worker.
6. Research automation expansion.

The current remote is configured as:

```powershell
git remote -v
```
