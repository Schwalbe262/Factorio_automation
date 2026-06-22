# Current Handoff
- Branch `chore/part130-unattended-qwen9-supervisor`; scheduler Qwen/vLLM service id `13039` is ready and still used for strategy when no deterministic prerequisite fast-path applies.
- Fresh no-mod map is running after reset; supervisor UP; latest autopilot PID `61584`; server UP.
- New controller fix: strict-LLM autopilot now writes `strategy_decision`/`strategy_dispatch` heartbeats and runs deterministic prerequisite overrides before waiting on Qwen.
- New readiness fix: incomplete gear/belt transfer no longer blocks progress when buffered transport belts are sufficient (`>=20`); low stock still repairs the connection.
- Live validation: progressed past belt-mall loop, produced red science, completed `electric-mining-drill` research (`researched=5`), and is running `automate_electronic_circuit_line`.
- Validation: `tests.test_controller tests.test_strategy tests.test_factory_readiness tests.test_run_health` -> 281 OK; focused fast-path/readiness tests OK.
- Dirty archives remain: `note.md`/`insight.md` had large pre-existing append-only changes; avoid staging wholesale.
