# Current Handoff
- Branch `chore/part130-unattended-qwen9-supervisor`; scheduler Qwen/vLLM service id `13039` is ready and used for strategy.
- User requested a fresh-map reset; old save/state/log traces were archived under `runtime/reset-archives/20260623-*`.
- Fresh no-mod map is running; supervisor UP; latest autopilot PID `60012`; dashboard/web and no-mod server are UP.
- Live validation after reset: tick ~61336, researched=`steam-power,electronics,automation-science-pack`; iron direct smelting recovered and working.
- New fix: `IronPlateSkill` can reclaim the only starter coal drill after buffered coal exists, taking 20-30 coal first instead of failing on missing drill.
- Current live skill: `research_automation` running after iron bootstrap; watch Automation completion, then power/lab/science prerequisites.
- Validation: new targeted planner tests OK; `tests.test_planner` 468 OK; `tests.test_strategy` 169 OK.
- Token usage recorded: raw `33,271,010`, delta `317,683`; weekly quota unavailable/null.
- Dirty archives remain: `note.md`/`insight.md` had large pre-existing append-only changes; avoid staging wholesale.
