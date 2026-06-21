# Current Handoff
- Branch: `chore/part130-unattended-qwen9-supervisor`; no-mod Factorio server/web/supervisor/autopilot are running with scheduler vLLM service `12304`.
- Part159: fixed relocation detour-pole `already_exists` loop by skipping candidate build positions that already contain a power connector; pushed as `9c4903c`.
- Part160: fixed relocation power-corridor blockers by moving near and mining recoverable blockers instead of failing before old-mall teardown.
- Validation: relocation blocker/detour subset passed, `tests.test_planner tests.test_strategy tests.test_controller` passed 620 tests, full discover passed 1120 tests (ResourceWarning only).
- Live validation: autopilot PID `51044` ran new code; trace `strategy-gear-belt-mall-relocation-20260621-061853.jsonl` moved to blocker and mined stone-furnace unit `236`.
- Current live: tick `1643214`, researched `4`, vLLM ready; active skill `expand_copper_smelting` step `17`; small-electric-pole `21`, electronic-circuit `3`.
- Current blockers: red science/labs/electric drills still absent; next work should stabilize e-circuit automation, electric mining drill rollout, burner replacement, red science/labs, and main-belt migration.
- Runtime journals are dirty from unattended autopilot (`note.md`, `insight.md`); stage code/test/handoff selectively unless intentionally archiving runtime logs.
- Token usage: fallback goal sample `17,083,490` absolute, delta `81,151`; weekly quota unavailable because Codex state DB is malformed.
