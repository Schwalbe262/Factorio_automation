# Current Handoff
- Branch `chore/part130-unattended-qwen9-supervisor`; scheduler Qwen/vLLM service id `13039`.
- Fresh no-mod map is running after reset; backup: `runtime/vanilla/saves/backups/no-mod-rcon-before-reset-20260622-194734.zip`.
- Supervisor UP; latest observed autopilot PID `43396`, server UP at tick `494841`, researched `4`.
- Coal readiness/supply guard fixed: empty unfueled coal source repairs via `setup_coal_supply`, and empty-output coal drills are fueled before chest-to-belt conversion.
- Belt bootstrap guard fixed: route-scale boiler belt targets only unlock after automated gear->belt transfer; pre-transfer cap is `40`.
- Controller now redirects satisfied bootstrap selections; local strategy also recomputes stale remote boiler-belt bootstrap decisions.
- Live result: stale remote bootstrap was adjusted to `connect_coal_fuel_feed`; live skill heartbeat active at step `4`, stall_count `0`.
- Validation: targeted controller/strategy/planner/readiness suites `730 OK`; full `python -m unittest discover -s tests` `1224 OK` after stale-remote guard.
- Dirty archives remain: `note.md`/`insight.md` have large append-only changes from runtime/autopilot; avoid staging wholesale.
