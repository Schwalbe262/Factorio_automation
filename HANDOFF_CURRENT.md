# Current Handoff
- Branch: `chore/part130-unattended-qwen9-supervisor`; supervisor running with autopilot PID `73612`.
- Startup context: read this file and targeted `goal.md`; never read `note.md`/`insight.md` in full.
- Live 2026-06-20 14:34 KST: server recently UP, scheduler vLLM service `12161`, new autopilot cycle 1 after restart.
- Belt mall recovered from 0 to 16 transport belts; readiness now `failure_root=None`, heuristic next skill `connect_coal_fuel_feed`.
- Fixed repeated boiler hand-fuel fallback: once boiler coal-feed route has started, missing belts no longer trigger boiler hand-carry fallback.
- Fixed strategy readiness guard: connected gear/belt mall with empty belt output now selects `bootstrap_build_item_mall` before relocation/logistics.
- Validation: `PYTHONPATH=src python -m unittest tests.test_strategy tests.test_planner tests.test_remote_slurm` -> 524 OK.
- Current caveat: monitor PID `73612` to confirm it resumes coal feed/logistics with the recovered belt stock; live-skill heartbeat may lag after restart.
- Token checkpoint: goal tracker `3,440,808`; latest recorded delta `236,232`; weekly quota unavailable.
