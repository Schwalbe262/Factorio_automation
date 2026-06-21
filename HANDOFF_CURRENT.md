# Current Handoff
- Branch: `chore/part130-unattended-qwen9-supervisor`; no-mod Factorio, Qwen/vLLM, dashboard, and autopilot are running.
- Part155: human layout learning now treats `connect_entities` and `allow_nearby` Factorio-adjusted builds as deterministic agent actions.
- Part155: drill `mining_target` is no longer part of layout snapshots, preventing resource/depletion observation flicker from becoming human-layout candidates.
- Live after restart: supervisor/autopilot PID `73988`; latest run-health tick 775998; vLLM service 12304 healthy; warnings empty.
- Live status: latest `setup_power` stopped after confirming boiler coal feed active; researched automation/electronics/steam-power; stall_count 0.
- Learning: false pending operator-layout events were retracted; real user layouts should remain as `pending_human_review` for later review/promotion.
- Validation: `tests.test_human_layout_learning tests.test_run_health` passed (14 tests) and full `python -m unittest discover -s tests` passed (1111 tests; ResourceWarning only).
- Token usage: fallback sample recorded at 15,115,394 absolute, delta 574,689 for this sample; weekly quota unavailable because Codex state DB is malformed.
- Next: push Part155, then continue from power-pole mall toward e-circuit production, electric mining drills, burner replacement, red science/labs, and main-belt migration.
