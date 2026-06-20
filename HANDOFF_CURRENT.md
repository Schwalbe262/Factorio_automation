# Current Handoff
- Branch: `chore/part130-unattended-qwen9-supervisor`; current live map is stale/unsafe and should be reset before further play.
- Part147: fixed circuit bootstrap to take buffered `assembling-machine-1` output before feeding/crafting more prerequisites.
- Part147: fixed `BuildItemMallSkill` so placed machines no longer count as available mall output for user-output mall items.
- Part147: site input logistics treats power poles as hard route blockers and detours instead of mining factory power.
- Part147: build-item malls bridge one-pole disconnected power-corridor gaps before repeating ineffective `connect_power`.
- Validation passed: `tests.test_planner` (358) and `py_compile src/factorio_ai/planner.py`.
- Runtime `note.md`/`insight.md` have large generated churn; stage only intentional source/tests/handoff unless explicitly requested.
- Next: reset no-mod save, restart via unattended LLM path without `FACTORIO_AI_FORCE_HEURISTIC_STRATEGY`, then verify Qwen strategy use.
- Token checkpoint: goal tracker 11,569,946; part delta +710,772 since 10,859,174; weekly quota unavailable.
