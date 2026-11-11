# EXP-0004 — PCB-style coal corridor

- Date: 2026-11-11, same default 2.1.20 Nauvis world/seed 101 as EXP-0002.
- Part: layout integration within initial v2 execution framework.
- User requirement: clean PCB-like factory blocks, fixed ports, trunks,
  controlled branching/crossings and expansion corridors; avoid snake-like belts.
- Hypothesis: constrain whole corridor geometry and simplify local self-feeding,
  instead of accepting the first route that fits around separately placed power.
- Initial measured plan: 140 belts total, 13-belt burner self-feed; main route
  length 126/6 turns. Existing paid boiler, pump and engines retained.
- Implementation: shared eight-tile trunk lattice, fixed headings, bounded
  corridor candidates, maximum six turns and detour gate; no maze fallback.
  V2 builder policy hook leaves legacy v1 routing behavior intact on v1.
- Preflight failures recorded: existing owned belt with another orientation
  blocked native ghost checks; admit replacement only with verified ownership,
  compatible terrain collision layers, and no other colliders. Initial compact
  exit faced an adjacent starter drill; changed the port to an eastward escape.
  No hardware was removed during these failed preparation attempts.
- Successful native preflight: 132 belts, four-turn length-128 trunk, detour
  ratio 1.1228, four-belt self-feed, zero natural obstacles to clear.
- Construction: ordinary identity/world-guarded mining recovers 97 obsolete
  installed entities. Reuse materials; retain shared power hardware and drill.
  Recover/build stages persist and publish only the validated replacement plan.
- Live issue: cold starter production needs fuel upkeep during migration.
  Safely stopped at an action boundary, added upkeep, resumed the same job;
  no unknown mutation outcomes and no new world.
- Result: solved for this corridor. Job pcb-power-layout-20261111 succeeded,
  114 actions, zero executor failures, native entity postcondition tick 156363.
  Layout integrity only; power flow subsequently verified in EXP-0002.
- Comparison artifact: runtime/v2/dev-101/coal-layout-comparison.png; generator
  tools/render_v2_layout.ps1. Original plan and migration data retained ignored.
- Limits: new main route is two tiles longer; only whole-plan belt count and
  turns improved. Full production zoning, underground/splitter layers, capacity,
  loaded energy and rocket automation remain unproved/incomplete.
- Next: loaded power and startup research, followed by regular production blocks.
- Tokens: report the measured parent implementation interval at part delivery;
  subagent token counters are not exposed, not estimated as zero.

- Delivery usage sample: parent thread 3193689 -> 30059441 = 26865752 tokens including cached input; weekly account meter 4% -> 6% (+2pp). Concurrent child token counters unavailable; not an exact per-part quota percentage.
