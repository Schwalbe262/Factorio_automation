# Current Handoff
- Branch `chore/part130-unattended-qwen9-supervisor`; local Qwen path active via scheduler vLLM service id `13039`.
- User requested a map reset; old save backed up to `runtime/vanilla/saves/backups/no-mod-rcon-before-reset-20260622-194734.zip`.
- Fresh no-mod save created with `create-no-mod-save --overwrite`; server PID `32504`, dashboard PID `6104`, autopilot PID `68576`, GUI launch requested through Steam.
- Fresh map health after restart: server UP, researched=1, inventory includes iron/coal/stone, latest live skill `setup_coal_supply` yielded while coal drill fills chest.
- Fixes in this part: local seed source no longer steals assembler input materials; direct gear-transfer inserter is protected from relocation loops; starter stone output chest clears rock/tree blockers before build.
- Validation: targeted planner tests OK; `tests.test_planner` 457 OK; direct live observe OK on fresh map.
- Current concern: power site is ~55 tiles from spawn; continue monitoring early research/power rather than hand-driving.
- Dirty archives remain: `note.md`/`insight.md` have large append-only changes; avoid staging wholesale.
- Token usage sample: current thread `30,779,328` absolute; weekly quota unavailable.
