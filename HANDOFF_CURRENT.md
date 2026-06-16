# Current Handoff
- Branch: `chore/part130-unattended-qwen9-supervisor`; Part 130 - unattended no-mod Qwen 9B local LLM supervisor.
- Startup context: read `AGENTS.md`, this file, `goal.md`, then exact source ranges; never read `note.md`/`insight.md` in full.
- Persistent scheduler vLLM service is implemented: `slurm-ensure-vllm-service` keeps Qwen 9B warm for 10800s and supervisor gates autopilot/layout on service heartbeat.
- No-mod helpers use `Qwen/Qwen3.5-9B`, 900s strategy/task timeouts, 600s LLM timeout, and `FACTORIO_AI_SLURM_SCHEDULER_GPU_MODEL=a6000ada,a6000`.
- `/tasks` GPU submissions now pass ordered `gpu_model` candidates such as `a6000ada,a6000`, per scheduler docs, instead of collapsing to one A6000 model.
- Live runtime: supervisor PID 64396; service task 8211 ready; `autopilot_gate=ready`; autopilot PID 64416; idle layout PID 12984.
- Validation: PowerShell parser ok; `PYTHONPATH=src pytest tests/test_remote_slurm.py -q` -> 47 passed.
- Next: investigate latest autopilot failure `FactorioController._run_skill() got an unexpected keyword argument 'input_item'`.
