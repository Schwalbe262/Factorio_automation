# Current Handoff
- Branch: `chore/part130-unattended-qwen9-supervisor`; strict Qwen supervisor path is live after a clean no-mod reset.
- Part148: scheduler `llm_ready` now requires a ready persistent vLLM heartbeat before client strategy tasks run.
- Part148: stale running vLLM services are cancelled after startup timeout+grace without closing the warm allocation.
- Part148: strict autopilot defaults `FACTORIO_AI_ALLOW_HEURISTIC_AUTOPILOT_FALLBACK=0`; no silent heuristic degrade.
- Part148: invalid strategy JSON retries once with ultra-compact payload; deploy archive excludes `.factorio-ai-scheduler-tasks`.
- Runtime: new save started at 2026-06-20 20:27 UTC; server up, researched=0, supervisor PID 56828, autopilot PID 54148.
- Runtime: vLLM service `12304` on n097/alloc1504 ready; new decision `20:28:25 src=llm produce_iron_plate`; iron target 10/10 reached.
- Validation: `tests.test_controller tests.test_remote_slurm`, `py_compile`, and PowerShell parse passed.
- Token checkpoint: goal tracker 12,052,238; part delta +482,292 since 11,569,946; weekly quota unavailable.
