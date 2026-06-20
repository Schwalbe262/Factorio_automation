# Current Handoff
- Branch: `chore/part130-unattended-qwen9-supervisor`; supervisor running, latest health shows autopilot PID `59432`.
- Startup context: read this file and targeted `goal.md`; never read `note.md`/`insight.md` in full.
- Part136 live fix: coal-feed/iron-line oscillation narrowed; latest `llm_decisions.jsonl` entry chose `build_iron_plate_logistic_line_to_gear_mall` ok=true at 2026-06-20 17:13:51 KST.
- Planner fixes: refuel gear-mall iron source burner drill, repair unpowered iron-line inserters, prefer direct gear->belt assembler inserter only when no active lane repair exists.
- Boiler feed: refuses partial long belt routes when belt mall stock is short; boiler feed endpoint prefers expandable bus side opposite steam engines.
- Strategy fixes: completed gear-mall plate line now requires powered inserters and usable fueled source; burner drills are bootstrap-only once Automation+stable power+science allow electric-drill research/mall.
- Live status: server UP, research `logistics` still 0.05; boiler working, but health still reports stale live-skill PID and foundry stale implemented-skill queue.
- Validation: targeted live-fix tests OK; `tests.test_strategy tests.test_planner tests.test_controller` 540 OK; full `unittest discover -s tests` 1027 OK.
- Token checkpoint: goal tracker `5,742,318`; part136 delta `1,244,584`; weekly quota unavailable; token sample recorded.
