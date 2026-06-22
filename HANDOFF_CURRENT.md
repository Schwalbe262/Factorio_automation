# Current Handoff
- Branch `chore/part130-unattended-qwen9-supervisor`; scheduler Qwen/vLLM service id `13039`.
- Fresh no-mod map is running; supervisor UP; latest autopilot PID `14108`.
- Fixed route-scale boiler feed loop: long coal-feed routes now repair gear/belt mall pairing before retrying `connect_coal_fuel_feed`.
- `BuildItemMallSkill` now refuses false-done transport-belt malls when the paired gear assembler is missing or unpaired.
- Existing belt mall can rebuild/reuse a sidecar gear assembler, including setting recipe on an already-placed unassigned sidecar.
- Controller stall/satisfied recovery now prefers route-scale mall repair over stale `setup_coal_supply` rotation.
- Live validation: bootstrap repaired the mall path and stopped with `build item mall is producing transport-belt and target reached: 134/40`; next live skill is `build_gear_belt_mall_logistics`.
- Validation: targeted suites `735 OK`; full `PYTHONPATH=src python -m unittest discover -s tests` `1229 OK` with existing ResourceWarning.
- Dirty archives remain: `note.md`/`insight.md` have large append-only runtime changes; avoid staging wholesale.
