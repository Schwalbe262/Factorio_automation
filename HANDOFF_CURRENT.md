# Current Handoff
- Branch: `chore/part130-unattended-qwen9-supervisor`; Part 130 - unattended no-mod Qwen 9B local LLM supervisor.
- Startup context: read `AGENTS.md`, this file, `goal.md`, then exact source ranges; never read `note.md`/`insight.md` in full.
- Added `run_factorio_no_mod_unattended_llm.{bat,ps1}` to keep server, dashboard, scheduler LLM path, autopilot, and idle layout loop alive.
- Normal no-mod LLM helpers now use `Qwen/Qwen3.5-9B`, A6000-first scheduler candidates, 3 scheduler CPUs, and strategy priority 100.
- `runtime/unattended-llm-supervisor.json` reports vLLM model, GPU candidates, LLM readiness, and loop PIDs.
- Live processes: supervisor PID 57076, autopilot PID 59948, idle layout PID 84428; existing Factorio server/dashboard left running.
- Current scheduler state: 9B configured, but `llm_ready=false` until scheduler provides a ready A6000/A6000Ada GPU allocation.
- Validation: PowerShell parser ok; `pytest tests/test_remote_slurm.py -q` -> 42 passed; next verify first successful 9B strategy cycle and keep self-development patch/spec/test gated, not live self-mutation.
