# Factorio AI Autoplayer

Layered Factorio AI autoplayer MVP.

The local machine runs Factorio and controls it through RCON. A Factorio Lua mod exposes a small command API:

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

## Current MVP

- Observe player position, inventory, nearby resources, nearby entities, and craftable recipes.
- Execute allowlisted actions only.
- Run a rule-based `produce_iron_plate` skill until at least 10 iron plates exist in inventory or machine outputs.
- Run a reusable rule-based `produce_copper_plate` skill until copper plates exist in inventory or machine outputs.
- Run a rule-based `produce_automation_science_pack` skill until at least 5 automation science packs exist.
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

In another terminal, run the iron plate MVP loop:

```powershell
factorio-ai run-iron-mvp --target 10
```

Run the copper plate MVP loop:

```powershell
factorio-ai run-copper-mvp --target 10
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
chain. Desired production targets are editable per item. If user targets are satisfied, the
strategic LLM may raise targets or add the next rocket-program item automatically.

Use this for production runs when a real LLM endpoint is configured:

```powershell
$env:FACTORIO_AI_SLURM_ENABLED=1
$env:FACTORIO_AI_REQUIRE_LLM_STRATEGY=1
factorio-ai strategy --objective "launch_rocket_program"
```

## Achievement-Compatible Track

The current `factorio-ai` engine is a development and verification track. It uses a mod, RCON,
and Lua commands, so it is not intended for Steam achievement runs.

Achievement-compatible play must run as a separate vanilla track:

- no mods;
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

This command uses `steam://rungameid/427520` and does not pass custom Factorio arguments. The
vanilla executor must navigate the normal GUI, including New Game -> Freeplay (Space Age), with
ordinary keyboard and mouse input. Any path using `--mod-directory`, RCON, or Lua commands belongs
to the development track and must not be used for achievement runs.

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

## Spatial And Rail Planning

Factory placement is a strategic concern, not only a build-detail concern. The strategic payload
includes site-selection and rail-network context so the LLM can choose where a production district,
logistics corridor, or train outpost should go before a deterministic executor validates exact tiles.

When a required resource patch or factory district is far from the current base, the planner should
prefer a rail supply line over long walking loops or very long belts after rail technology and
materials are available. The current catalog exposes `plan_rail_network` and `build_rail_supply_line`
as future skills; until those executors exist, selecting them records a missing-skill backlog entry
instead of faking rail construction.

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

Default remote directory: `~/kakao-bot-worker`

Default job name: `AUTO`

When the running job is `AUTO`, planner requests use `srun --jobid=<AUTO_JOB_ID> --overlap`
to execute `factorio_ai.slurm_worker --task ...` inside the existing AUTO allocation. This avoids
submitting another Slurm job and does not require replacing the existing flight worker loop.

Common environment variables:

- `SUPERCOMPUTER_WORKER_SSH_HOST`
- `SUPERCOMPUTER_WORKER_SSH_USER`
- `SUPERCOMPUTER_WORKER_SSH_KEY`
- `SUPERCOMPUTER_WORKER_SSH_PORT`
- `SUPERCOMPUTER_WORKER_REMOTE_DIR`
- `FACTORIO_AI_SLURM_ENABLED=1`
- `FACTORIO_AI_LLM_BASE_URL=http://127.0.0.1:8000/v1`
- `FACTORIO_AI_LLM_MODEL=<model-name>`

Submit a test task:

```powershell
factorio-ai slurm-submit-test
```

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
