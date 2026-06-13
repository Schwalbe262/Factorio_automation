# Factorio Automation Agent Handoff

This file is the compact handoff context for running the project from Codex CLI,
Claude, or another coding agent.

## Current Objective

Build a Factorio AI autoplayer that can eventually launch a rocket using a real
gameplay-compatible control path.

The intended architecture is hierarchical:

- Strategic LLM: chooses the next high-level objective or skill.
- Local deterministic skills: execute low-level and mid-level Factorio actions.
- Monitoring web UI: shows production rates, targets, factory sites, logistics
  links, threats, defense, LLM decisions, and token usage.
- Slurm AUTO worker: runs larger local LLM inference and model comparisons.

The user wants a long-term path toward:

- no-mod or vanilla-compatible control where possible;
- multiplayer observation support later;
- automatic progression from starter base to science, research, oil, trains,
  rocket, space platform, and space science;
- learning from blueprint/design examples without blindly pasting blueprints;
- fine-tuning over time from traces, successful factory structures, and user
  interventions.

## Local Repository

Primary repo:

```powershell
cd C:\Users\NEC\Documents\Factorio
```

Current branch:

```powershell
git branch --show-current
```

Current remote before rename:

```text
https://github.com/Schwalbe262/Factorio.git
```

User requested the GitHub repository name be changed to:

```text
Factorio_automation
```

After the rename, update local origin:

```powershell
git remote set-url origin https://github.com/Schwalbe262/Factorio_automation.git
```

Latest committed part at the time this handoff was written:

```text
041e17b Part 33: improve no-mod power and research automation
```

There are uncommitted Part 34 changes in the Factorio repo at this point.

## Important External Project

The existing Kakao/Telegram bot project owns the persistent Slurm AUTO worker
opener:

```powershell
cd C:\Users\NEC\Documents\GitHub\kakao-loco-bot
```

Important files:

- `modules/supercomputer-worker.js`: creates `kakao_worker.sh` and submits AUTO.
- `.env.local`: local runtime config, not meant for public commit.
- `restart-telegram-bot.cmd`: restarts the Telegram bot so `.env.local` changes
  are loaded.

Part 34 modified `modules/supercomputer-worker.js` so AUTO jobs request GPU GRES:

```text
SUPERCOMPUTER_WORKER_GPUS_PER_NODE=1
SUPERCOMPUTER_WORKER_GRES=gpu:1
```

It also added these values to `.env.local` locally. Do not expose secrets from
that file.

The Kakao repo was already dirty before the Part 34 work:

- `flight-searcher/data/flight-airlines.json` had unrelated user changes.
- temporary/untracked files existed.

Do not revert those unrelated changes.

## Part 34 Work Already Done

Factorio repo changes:

- `src/factorio_ai/remote_slurm.py`
  - default `FACTORIO_AI_SLURM_GPUS_PER_NODE` changed from `0` to `1`;
  - `slurm-status` now includes Slurm GRES output;
  - `slurm-llm-status` now checks:
    - LLM endpoint env vars;
    - vLLM env vars;
    - GPU env vars;
    - `nvidia-smi -L`;
    - whether Factorio AI is deployed under the remote worker dir;
  - remediation output now distinguishes missing GPU allocation from missing
    LLM endpoint and missing deployment;
  - attached srun env setup now forwards `FACTORIO_AI_VLLM_*` variables and
    derives `FACTORIO_AI_LLM_BASE_URL`/`MODEL` from `FACTORIO_AI_VLLM_MODEL`.
- `slurm/run-factorio-ai-worker.sh`
  - records GPU visibility;
  - refuses to start vLLM when a vLLM model is requested but GPU or vLLM is not
    available, instead of silently falling back.
- `tests/test_remote_slurm.py`
  - tests GPU readiness helpers and remediation output.
- `README.md`
  - documents GPU AUTO env vars and `slurm-llm-status`.

External Kakao repo changes:

- `modules/supercomputer-worker.js`
  - default AUTO GPU request added;
  - generated `#SBATCH --gres=gpu:<n>` added;
  - status/config output includes requested GRES and GPU visibility;
  - squeue output includes `%b` to show GRES.
- `README.md`
  - documents new AUTO GPU env vars.
- `.env.local`
  - local-only addition:
    - `SUPERCOMPUTER_WORKER_GPUS_PER_NODE=1`
    - `SUPERCOMPUTER_WORKER_GRES=gpu:1`

Remote runtime work:

- `factorio-ai` conda env on the Slurm side has vLLM installed.
- Verified package presence after install:
  - `torch=True`
  - `transformers=True`
  - `vllm=True`
  - `openai=True`

## Slurm Status Notes

The remote worker directory is:

```text
/home1/r1jae262/kakao-bot-worker
```

AUTO GPU allocation was verified with:

```text
squeue GRES: gres/gpu:1
CUDA_VISIBLE_DEVICES=0
nvidia-smi: NVIDIA A10
```

However, Slurm jobs may be short-lived or canceled by the bot restart flow. In
`factorio-ai slurm-status`, trust the `--- jobs ---` section first. The remote
`status.txt` block can be stale if no AUTO job is currently running.

Useful commands:

```powershell
$env:PYTHONPATH="src"
python -m factorio_ai.cli slurm-status
python -m factorio_ai.cli slurm-llm-status
```

To reopen AUTO through the Kakao worker path:

```powershell
cd C:\Users\NEC\Documents\GitHub\kakao-loco-bot
$env:SUPERCOMPUTER_WORKER_GPUS_PER_NODE="1"
$env:SUPERCOMPUTER_WORKER_GRES="gpu:1"
$env:SUPERCOMPUTER_WORKER_ENABLED="true"
.\restart-telegram-bot.cmd
```

The bot should log `gres/gpu:1` in the supercomputer worker startup message.

## Current LLM Worker State

The project now uses Factorio-owned Slurm workers instead of the shared Kakao
`AUTO` job by default.

Current active fast worker:

```text
remote dir: ~/factorio-ai-worker
job name: factorio-ai-worker
model: Qwen/Qwen3.5-4B
GPU request: gres/gpu:1
partition preference: gpu4,gpu2,gpu1
vLLM args: --max-model-len 32768 --gpu-memory-utilization 0.85 --enforce-eager
vLLM env: FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER=0
```

Current medium comparison worker:

```text
remote dir: ~/factorio-ai-worker-9b
job name: factorio-ai-worker-9b
model: Qwen/Qwen3.5-9B
GPU request: gres/gpu:a6000:1
partition: gpu4
vLLM args: --max-model-len 32768 --gpu-memory-utilization 0.90 --enforce-eager
vLLM env: FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER=0
```

Current large queued worker:

```text
remote dir: ~/factorio-ai-worker-27b
job name: factorio-ai-worker-27b
model: Qwen/Qwen3.6-27B-FP8
GPU request: gres/gpu:a6000ada:3
partition: gpu3
vLLM args: --tensor-parallel-size 3 --max-model-len 32768 --gpu-memory-utilization 0.85 --enforce-eager
vLLM env: FACTORIO_AI_VLLM_USE_FLASHINFER_SAMPLER=0
```

Useful local commands:

```cmd
run_factorio_slurm_llm_4b_worker.bat
run_factorio_slurm_llm_4b_attached_benchmark.bat
run_factorio_slurm_llm_9b_worker.bat
run_factorio_slurm_llm_9b_attached_benchmark.bat
run_factorio_slurm_llm_27b_gpu3_queue.bat
run_factorio_no_mod_llm_autopilot.bat
```

`slurm-llm-status` now checks LLM env, visible GPU, deployment, and whether the
OpenAI-compatible `/v1/models` endpoint responds from inside the allocation.
`run_factorio_no_mod_llm_autopilot.bat` requires the active Slurm LLM strategy path, so it
does not silently continue with heuristic strategy when the worker is unavailable.
For code-only validation, prefer the attached benchmark BATs. They deploy current source and use
`srun --jobid` against the existing allocation, avoiding `sbatch` resubmission and GPU queue churn.

## Factorio Runtime Commands

Run tests:

```powershell
$env:PYTHONPATH="src"
python -m unittest discover -s tests
```

Useful no-mod MVP commands:

```powershell
$env:PYTHONPATH="src"
python -m factorio_ai.cli run-no-mod-power-mvp --max-steps 80
python -m factorio_ai.cli run-no-mod-automation-research-mvp --max-steps 80
python -m factorio_ai.cli run-no-mod-circuit-automation-mvp --max-steps 120
```

GUI/watch scripts exist in the repo for user review. Prefer checking their
current contents before running:

```powershell
rg -n "watch|review|vanilla|factorio" *.bat
```

## Web Dashboard

The Factorio monitor public URL should use the external IP:

```text
http://27.115.156.173:8787/factorio
```

The user wanted Telegram/Kakao commands:

```text
/factorio
/팩토리오
```

Both should return/redirect to the Factorio dashboard link. The web dashboard
supports EN/KR language switching.

## Design Constraints From User

Key constraints to preserve:

- GUI demonstrations should show real walking/mining/building motions.
- Headless/fast modes can be used for implementation tests, but GUI demos
  should behave like gameplay.
- Avoid instant teleport/mining as the main path.
- No-mod or vanilla-compatible path is preferred because multiplayer/mod
  compatibility matters.
- Current long-term goal is not a single iron plate demo. It is autonomous
  factory growth under LLM strategy.
- LLM should choose goals and diagnose bottlenecks. It should not decide every
  tick-level action.
- If LLM selects a missing skill, do not fake it. Record missing skill and have
  Codex implement the deterministic executor.
- Site placement matters. Avoid building early factories on top of resource
  patches when possible.
- Logistic links should be between sites, not individual belts.
- Sites should be grouped by nearby entities, not listed as hundreds of
  individual machines.
- When the LLM has no urgent production/research/defense work, it should use
  idle cycles for site layout improvement.
- `plan_factory_site` is a simulation/planning skill, not a build skill. It
  may propose blueprint-style improvements, compare before/after rates,
  footprint, distance, ratios, and belt-capacity risk, but it must mark those
  candidates `simulation_only` / `not_applied` until a deterministic build
  executor is explicitly selected.
- Defenses should start with gun turrets and ammo production, not early nest
  clearing.
- Power matters:
  - insufficient generation throttles consumers;
  - networks share power only when connected;
  - a single connected main grid is the default.
- Production rate estimation must consider assembler speed, recipe time, belt
  throughput, inserter throughput, modules, and bottlenecks.

## References Requested By User

Use these as design references, not blind copy-paste:

- https://factorioprints.com/
- https://mods.factorio.com/mod/RateCalculator
- https://mods.factorio.com/mod/mining-patch-planner
- https://github.com/rimbas/mining-patch-planner
- https://mods.factorio.com/mod/OilOutpostPlanner
- https://github.com/Coppermine-factorio/OilOutpostPlanner
- https://lua-api.factorio.com/latest/classes/LuaEntity.html

## Git And Commit Rules

The user wants completed parts pushed to GitHub.

Before commit:

```powershell
git status --short
$env:PYTHONPATH="src"
python -m unittest discover -s tests
```

Suggested Part 34 commit message:

```text
Part 34: prepare Slurm GPU LLM worker diagnostics
```

After commit and push, if GitHub repo has been renamed:

```powershell
git remote set-url origin https://github.com/Schwalbe262/Factorio_automation.git
git push origin master
```

## Token Accounting

User asked to state tokens used per completed work item.

Known checkpoints:

- Part 34 diagnostic start was around `17,948,370` tokens.
- Slurm GPU diagnosis checkpoint was `18,023,860` tokens.
- Before creating this handoff file, `get_goal` reported `18,530,998` tokens.

Update this section or final response with the latest delta after completing
the current task.
