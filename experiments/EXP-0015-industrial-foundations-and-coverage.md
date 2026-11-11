# EXP-0015: industrial foundations, not live commissioning

Date: 2026-11-12 KST. Assisted development on v2, normal catalog/material rules.
Goal: build the missing steel/brick/furnace-replacement modules and require
defense coverage before extending supply. No rocket or industry success claim.

## Implemented and validated
- Steel: opposing furnace rows, automatic pure iron/coal feeds, fixed output,
  catalog-derived 7.5 steel/min from37.5iron/min and at most2.7coal/min.
  Requires an installed native surplus-priority iron bus tap and exact coal port.
  Thirteen focused tests pass. No public job or native steel construction yet.
- Bricks: opposing furnaces, fast input inserters, normal output inserters,
  typed registered stone/coal sources and whole-site/route/power preflight.
  Nameplate37.5bricks/min needs75stone/min; a60stone source bounds output at30.
  Sixteen focused tests pass. No native brick commissioning or public job yet.
- Furnace upgrade: normally pay each steel furnace before recovering one exact
  old stone furnace, reobserve the gap, build normally, require fresh output.
  225to300 nominal selects four balanced replacements costing24steel/40bricks;
  target360 selects eight total, giving375nameplate at48steel/80bricks.
  All existing source-plan authorities synchronize with world/revision guards,
  persisted publication intent and per-file atomic restart recovery.
  Seventeen focused tests pass. No live replacement or public job yet.
- Additive defense: partition planned rectangles into cells and require each
  complete cell inside one armed turret's native-range-minus2 disk. New posts
  avoid ore/reservations and pass local access checks. Preserve existing post
  identities; require ten full normal magazines before commissioning a scope.
  Dedicated material jobs now gate construction on this proof. Native readonly
  stone and steel candidates passed. A proposed brick site beside water failed
  complete accessible coverage, correctly; choose a land-valid alternative.
  No new scoped turret has been built and no enemy attack was stress-tested.

## Failures and unresolved work
The first furnace publisher omitted retained bus/science source-plan copies.
Fixed before any live upgrade; tests cover mixed old/new file revisions after
an interrupted write and rejection of unrelated layout/route changes.

Current iron source installed capacity360 has only330remaining after one drill
exhausted. Restore active supply before a live360upgrade. Stone source,
steel surplus tap, native steel/brick commissioning, public industrial job
wiring and fresh-seed acceptance remain pending. Cached capacity reports will
need refresh after upgrades. Mall, automatic ammo/repair, oil, rail and rocket
remain unimplemented. These module tests do not close those gates.

Validation: 402 total v2 tests pass with local optional Lua runtime enabled,
zero skips. Token accounting belongs to the measured Part8 interval, which
also includes EXP-0014; exact per-module and child usage are unavailable.

Part8 final measured checkpoint: 74,975,472 parent tokens including cached input; account weekly meter17% to23% (+6pp). This is the combined Part8 interval; child/per-module tokens and exact weekly quota share are unavailable.
