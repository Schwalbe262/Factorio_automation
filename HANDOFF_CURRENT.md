# Current Handoff
- Branch `chore/part130-unattended-qwen9-supervisor`; server UP, researched=5, Qwen/vLLM service id `[13286]` ready, supervisor running.
- Fixed long boiler coal-feed oscillation: strategy/controller/planner now preserve route-scale belt target instead of collapsing bootstrap mall to 20/40.
- Boiler/fuel guardrails no longer force `connect_coal_fuel_feed` when belt route prerequisites are unbuildable; recovery keeps `bootstrap_build_item_mall target~227`.
- Live validation: restarted autopilot PID `62532`; latest `bootstrap_build_item_mall` ran with `target=227` and increased transport belts `48 -> 56`.
- Validated `tests.test_planner` 477 OK, `tests.test_strategy` 172 OK, `tests.test_controller` 100 OK.
- Slurm home cleanup already reduced `~/factorio-ai-worker` to ~65M; `factorio-ai-models` remains 21G current 27B AWQ cache.
- Dirty runtime-heavy `note.md`/`insight.md` exist; stage only intentional code/tests/handoff unless explicitly cleaning history.
