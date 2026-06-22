# Current Handoff
- Branch `chore/part130-unattended-qwen9-supervisor`; Qwen/vLLM service id `13039` ready and supervisor running.
- Fresh no-mod map is active; server UP, researched=5, live skill `automate_electronic_circuit_line`.
- Slurm home cleanup reduced `~/factorio-ai-worker` from ~27G to 63M; `factorio-ai-models` remains 21G for current 27B AWQ cache.
- `cleanup_and_deploy.ps1` now also prunes old nested scheduler strategy/layout task JSON under deployed `factorio-ai/.factorio-ai-scheduler-tasks`.
- Strategy caps electric-drill prerequisite circuits at 18 and prevents boiler belt stockpiling from preempting active electric-drill dependency work.
- Planner fixes: circuit/assembler bootstrap takes nearby chest-buffered gears, belt mall stops when stock target is reached, electric-drill mall routes missing circuits to circuit automation.
- Validated `tests.test_strategy` 171 OK, relevant PlannerTests 94 OK, controller fake-automation regression OK.
- Restarted autopilot PID `69496`; live reached electric-drill mall, then returned to belt bootstrap/strategy decision for long boiler route belts.
- Dirty runtime-heavy `note.md`/`insight.md` exist; stage only intentional code/tests/handoff/token note unless explicitly cleaning history.
