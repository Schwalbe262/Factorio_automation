# Current Handoff
- Branch `chore/part130-unattended-qwen9-supervisor`; local Qwen path active via scheduler vLLM service id `13039`.
- Fresh no-mod map is running after reset; old save backup: `runtime/vanilla/saves/backups/no-mod-rcon-before-reset-20260622-194734.zip`.
- Current supervisor UP; latest observed autopilot PID `32148`, dashboard/server remained under supervisor, Qwen decisions still show `src=llm`.
- Fixed fresh-map coal readiness: empty/unfueled coal supply now maps to `starter_fuel_supply_starved -> setup_coal_supply`.
- Fixed coal supply sequencing: fuel an empty-output coal drill before replacing its chest with a belt.
- Fixed belt mall runaway: long boiler-route belt targets only scale after gear/belt transfer is automated; pre-automation belt bootstrap target caps at 40.
- Live result: coal supply recovered; transport-belt mall stopped as target reached `125/40` instead of continuing toward route-scale `220`.
- Validation: `tests.test_planner` 460 OK; `tests.test_controller` + `tests.test_factory_readiness` 101 OK; full discover 1221 OK before final cap, cap covered by planner tests.
- Dirty archives remain: `note.md`/`insight.md` have large append-only changes; avoid staging wholesale.
