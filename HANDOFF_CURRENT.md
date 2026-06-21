# Current Handoff
- Branch: `chore/part130-unattended-qwen9-supervisor`; no-mod Factorio server/web/supervisor/autopilot are running with scheduler vLLM service `12304`.
- Part159: fixed gear/belt mall relocation power-corridor detour loop by skipping candidate pole build positions that already contain a power connector.
- Regression added: relocation now refuses to rebuild an existing detour pole while preserving pre-teardown small-pole shortage/bootstrap behavior.
- Validation: relocation subset passed, `tests.test_planner tests.test_strategy tests.test_controller` passed 619 tests, full discover passed 1119 tests (ResourceWarning only).
- Live validation: autopilot PID `21020` restarted on new code; latest relocation trace built a detour pole at `83.5,-19.5` with no repeated `already_exists`; stale PID warning cleared.
- Current live: tick `1587412`, researched `4`, vLLM ready; `bootstrap_power_pole_mall` stopped after reaching small-electric-pole `22/20`.
- Current blockers: red science/labs/electric drills still absent; next work should stabilize e-circuit automation, electric mining drill rollout, burner replacement, red science/labs, and main-belt migration.
- Runtime journals are dirty from unattended autopilot (`note.md`, `insight.md`); stage code/test/handoff/token usage selectively unless intentionally archiving runtime logs.
- Token usage: fallback goal sample `17,002,339` absolute, delta `597,289`; weekly quota unavailable because Codex state DB is malformed.
