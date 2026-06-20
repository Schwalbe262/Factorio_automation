# Current Handoff
- Branch: `chore/part130-unattended-qwen9-supervisor`; supervisor running but autopilot gated because scheduler LLM is not ready.
- Part144: boiler coal feed no longer keeps/hand-fuels burner inserters; it relocates or builds powered inserter materials first.
- Strategy now lets power/fuel recovery preempt electric-drill/circuit dependency guardrails and satisfied gear/belt mall fallback.
- Live before commit: health no-observe shows no autopilot process, gate=`waiting_for_scheduler_llm`; `slurm-llm-status` reports no running worker job / `llm_ready=false`.
- Validation: `tests.test_strategy` 149 OK; `tests.test_planner` 349 OK; `tests.test_controller` 78 OK.
- Direct live check before LLM outage: `build_gear_belt_mall_logistics` reconciled to `setup_power` when belt stock was 28/20 and power was unconnected.
- Runtime `note.md`/`insight.md` have large generated churn; stage only intentional source/tests/handoff unless told otherwise.
- Token checkpoint: goal tracker 9,694,112; part delta 472,338 since prior checkpoint; weekly quota unavailable; token_usage.py failed with malformed sqlite.
