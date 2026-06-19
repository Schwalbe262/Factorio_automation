# Overnight autonomous handoff (user is asleep — do this work yourself)

The user delegated this work to run **autonomously overnight**. You are authorized to edit code,
deploy, restart the supervisor, and cancel/submit vLLM services as needed. **Top priority: do NOT
break the currently-working system** — gate new behavior so the safe path keeps working, test before
enabling, and revert if a change regresses. Read memory **[[llm-serving-cluster]]** first (cluster
gotchas: CRLF SSH key → use `r1jae262_lf.pem` via `SUPERCOMPUTER_WORKER_SSH_KEY`; **deploy only from
PowerShell with absolute `FACTORIO_AI_SLURM_REMOTE_DIR=/home1/r1jae262/factorio-ai-worker`** (Git Bash
mangles paths); TP hangs on n106; kill-for-restart must exclude `$PID`).

## Current state (working baseline — keep it working)
- New map running (fresh seed save), supervisor relaunched, `SERVICE_COUNT=1` (single clean vLLM
  service on port 8000), `--max-model-len 8192` (32k crashed ~13min on one a6000), concise strategy
  prompt deployed, `max_tokens 2048`, `guided_json=1`, model in `/home1/r1jae262/factorio-ai-models`.
- Health trail: `runtime/overnight-health.log` (a background monitor logs vllm_ready / service count /
  autopilot cycle / last trace every 5 min). Check it first to see if the 8192 service stays up.
- ps1: `run_factorio_no_mod_unattended_llm.ps1`. Restart pattern (PowerShell): kill procs matching
  `*factorio_ai.cli*`/`*unattended_llm.ps1*` **excluding `$PID`**, then `Start-Process powershell -File <ps1>`.
- Scheduler env for SSH/deploy commands: `FACTORIO_AI_SLURM_ENABLED=1 FACTORIO_AI_SLURM_MODE=scheduler`
  `FACTORIO_AI_SLURM_SCHEDULER_URL=http://100.112.168.31:8000 FACTORIO_AI_SLURM_SCHEDULER_ACCOUNT=r1jae262`
  `SUPERCOMPUTER_WORKER_SSH_KEY=C:\Users\NEC\.ssh\r1jae262_lf.pem`.

## TASK 1 — port separation for TRUE 4-GPU (the main ask)
All 4 GPUs are on ONE node (n106). TP hangs. Multiple services currently all bind `127.0.0.1:8000`
→ collide (only 1 effective) + share `vllm-service-8000.heartbeat.json` → readiness clobber. Fix =
distinct **localhost** ports per service + route each client to its chosen service's port. **Localhost
only — NO admin/firewall needed.** Design (in `remote_slurm.py` unless noted):
1. `ensure_vllm_service`: assign each new service a distinct port `8000 + i`, skipping ports already
   used by active services. Put it in `task["payload"]["port"]`. (Service `i=0` always gets 8000.)
2. `_scheduler_vllm_service_command(task)`: read `payload["port"]`, `export FACTORIO_AI_VLLM_PORT=<that>`
   at the top so vLLM binds it AND the heartbeat filename uses it (compute heartbeat name from the
   payload port, not the env).
3. `_scheduler_running_vllm_service_task_id()`: return the chosen service's **(task_id, port)** (keep
   the PID-seeded round-robin across running services).
4. `_submit_scheduler_task`: when co-locating a client (line ~779), set
   `task["payload"]["llm_base_url"] = f"http://127.0.0.1:{port}/v1"` from the chosen service's port,
   so routing rides in the payload (the scheduler env-injection is config.env=8000 and can't easily be
   per-task). Make sure the payload is what reaches the worker.
5. `slurm_worker.call_llm_json_with_diagnostics(...)`: add a `base_url: str|None=None` param; use it if
   given else `os.getenv("FACTORIO_AI_LLM_BASE_URL")` (line ~728).
6. `slurm_worker.run_strategy_request` AND the foundry-codegen worker handler: read
   `payload.get("llm_base_url")` and pass it through to `call_llm_json_with_diagnostics`.
7. Readiness (`vllm_service_status`): keep reading the port-8000 heartbeat as the readiness proxy
   (service i=0 is always 8000), OR aggregate per-port heartbeats — simplest is the 8000 proxy.
- **Safety:** all of this only matters when `SERVICE_COUNT>1`. With COUNT=1 the single service stays on
  8000 and behaves exactly as now. Implement, run `python -m unittest discover -s tests`, then TEST
  LIVE: set ps1 `SERVICE_COUNT=4`, `SCHEDULER_GPUS=1`, deploy + restart + cancel old services, and
  confirm via the scheduler API that 4 services come up on ports 8000-8003 AND clients route to
  different ports (check `llm_io_traces.jsonl` base_url variety) AND all 4 GPUs are busy. **If multi-port
  doesn't work, set `SERVICE_COUNT=1` and leave the working 1-GPU system.**

## TASK 2 — show ALL LLM I/O in the trace dashboard (low risk, do this first)
`web_dashboard.py` currently shows only `kind=strategy` traces. Make the LLM I/O Trace view show ALL
kinds (strategy, skill_foundry/codegen, layout_improvement, planner, etc.) from `logs/llm_io_traces.jsonl`
— add a kind column/filter, don't hard-filter to strategy. Verify at http://localhost:18889.

## When done
Append a summary to `runtime/overnight-health.log` and update memory if you learned something durable.
Leave the system RUNNING and stable for the user to wake to. If you enabled 4-GPU, confirm it's serving;
if you fell back to 1-GPU, say so and why.
