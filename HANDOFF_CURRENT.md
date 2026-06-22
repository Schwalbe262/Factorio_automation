# Current Handoff
- Branch `chore/part130-unattended-qwen9-supervisor`; scheduler Qwen/vLLM service id `13039` ready and still used for strategy.
- Operator requested a fresh map reset; old save backed up to `runtime/vanilla/saves/backups/no-mod-rcon-before-reset-20260623-034132.zip`.
- Fresh no-mod save created at `runtime/vanilla/saves/no-mod-rcon.zip`; stale world/runtime caches archived under `runtime/reset-archives/20260623-034132-map-reset`.
- Unattended supervisor restarted hidden; server UP on fresh map, latest observed tick ~7972, entities ~252, researched=0.
- Autopilot restarted on fresh map, PID `74316`; strategy source confirmed `llm` in recent decisions and live skill is `setup_coal_supply`.
- Latest health inventory: `iron-plate=9`, `coal=17`, `stone=8`; progress KPI key items showed coal stock ~57.
- No code changes in this reset part; dirty archives remain `note.md`/`insight.md` from append-only runtime logging, avoid staging wholesale.
