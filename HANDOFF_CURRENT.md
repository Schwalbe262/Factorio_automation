# Current Handoff
- Branch: `chore/part130-unattended-qwen9-supervisor`; supervisor running, autopilot PID `38148`, vLLM `QuantTrio/Qwen3.6-27B-AWQ` service id `12917`.
- Part190 fixed circuit/coal-feed recovery loops: scaled circuit input expansion now waits on active smelting when no new ore site is open, with `failure_root`/`repair_skill` metadata.
- Active machine-serving inserters are no longer mined for bootstrap relocation, so boiler feed inserters and working mall/cell inserters are protected.
- Belt-smelting construction now takes buffered `transport-belt`/`inserter` output from nearby mall assembler/chest before failing or stealing live inserters.
- Strategy no longer raises stale boiler-feed belt shortfall when `_boiler_coal_feed_active` is true; live smoke returns `shortfall=None`, target `20`.
- Live after restart: Qwen selected `bootstrap_build_item_mall`; guardrail routed to `connect_coal_fuel_feed`; coal feed cycle ended ok, yielding on boiler feed inserter moving coal.
- Validation: targeted regressions OK; `tests.test_planner` 445 OK; strategy/controller/human-layout/run-health 273 OK; full discover 1204 OK with existing `controller.py:1277` ResourceWarning.
- Token sample: `28,845,682` absolute, `+467,342` this turn; weekly quota unavailable.
- Dirty archives: `note.md`/`insight.md` have large append-only changes; human layout learning remains pending review with delta added=1.
