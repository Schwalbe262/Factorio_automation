# Current Handoff
- Branch: `chore/part130-unattended-qwen9-supervisor`; supervisor running with autopilot PID `55196`, vLLM service `12917`.
- Part184 fixed site-input route completion, smelting input inserter power repair, reusable inserter relocation for first-circuit/bootstrap loops, and dead coal-supply recovery before site/circuit loops.
- Strategy now uses `target_count=90` for smelting-fuel plate recovery and preempts site/circuit work with `setup_coal_supply` only when live coal supply is dead and inventory coal <20.
- Live: coal supply repair succeeded (`strategy-coal-supply-20260622-003136.jsonl`, coal 22->51); current circuit automation is progressing through expanded iron smelting build instead of step-1 fuel-logistics failure.
- Tests: `tests.test_strategy tests.test_controller` 255 OK; `tests.test_planner tests.test_strategy tests.test_controller` 680 OK; full discover 1180 OK (one existing ResourceWarning).
- Remaining ops warnings: stale operator layout learning and implemented skills still listed in foundry queue.
- Do not stage dirty `note.md`/`insight.md` wholesale; append-only archive is large and pre-existing.
- Token sample: `25,732,811`; weekly quota unavailable; Codex token logger failed with malformed SQLite DB.
