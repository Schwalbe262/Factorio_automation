# EXP-0014: resource keepouts and operating-factory migrations

Date: 2026-11-12 KST. Assisted development, GPT-6 Luna medium executes typed
jobs; parent supplies fixes and migration targets. This is not a frozen Luna-only
attempt. World `32a34974-d7cd-4ec0-9f35-13259ea32b8e`, installed 2.1.20,
default enemies/resources and speed 1, normal construction/materials/research.

## Goal and hypothesis
Keep early production and long transport off resource patches while retaining
working mines, furnaces, bus and labs. Shared checks plus narrow local extraction
scopes should prevent recurrence; prebuild, paid cutover, native supply proof and
exact-unit recovery should migrate existing offending routes safely.

## Changes and observed results
- Shared preflight/builder/transport/power policy checks ore, mining reservations
  and buried spans. Existing exact assets are retained, never silently rebuilt
  on ore after loss. Native negative test: AM1 at (17.5,-84.5) was placeable by
  Factorio but correctly rejected by resource policy.
- Green migration key `luna-green-resource-reroute-20261112`: first run stopped
  after 13 normal tree removals because the builder rejected a clearance-only
  plan. Fixed world-bound empty-plan completion; retry used the same job key.
  Final retry: 50 actions, 0 native failures, proof tick 1942869. Fresh inspection
  1952013 confirmed all 47 old iron-patch belt identities gone, no pending
  recovery/retirement, same compact core ports/units, all 12 machines producing,
  all eight labs supplied. Copper branch 78 tiles/4 turns; iron 43/6; unchanged
  route quality gates. Planning 48.87s, execution 52.08s, blocked wait 264.10s.
- Coal candidate failures exposed an obstructed east-only exit and an outlet
  whose following tile still contained ore. The shared selector now permits
  E straight or E once then N/S, first clear tile within 12 steps. A separately
  checked off-patch hub/apron may continue at most four clear cells; this never
  expands the ore exception. Key `luna-dedicated-coal-20261112` succeeded in
  12 actions, proof 1958663, new miners 5279/5280. Both real producer lanes hold
  coal. Outlet units 5277/5278 share nominal 60/min; both next tiles are off ore.
  Planning 23.97s, execution 8.05s.
- Legacy cleanup key `luna-legacy-coal-cleanup-20261112`: seven actions, no
  failures, proof 2029863. Normally recovered isolated burner 1343 first, then
  chest 1342 containing 800 coal after 18-slot headroom and isolation checks.
  All electric mining/power assets retained; fresh 30-second native generation
  proof and automatic coal proof passed. No discard or material grant. Planning
  56.84s, execution 3.00s.
- Extended router candidates initially displaced an established route needed
  by the next connection. Fixed by trying/ranking the established family first,
  with bounded extensions only if no accepted old-family route exists. Native
  green reroute and regression tests passed without relaxing six-turn or
  `1.35 * Manhattan + 4` limits.
- Steam native preflight exposed symmetric engine direction 8/native 0, a
  source anchor incorrectly included among ignored recovery units, and incomplete
  scoped-exit metadata in staged builds. Fixed axis comparison, retained source
  anchor, and complete-context cutover checks. Tick 2052070 preflight passed:
  113 tiles, five turns, ratio 1.076, 66 prebuild, three deferred, one exact old
  conflict, 90 obsolete. Key `luna-steam-coal-resource-reroute-20261112`
  then succeeded in 24 actions/0 failures, proof 2061572, loaded interval
  2059694–2061572, added-path coal witness 86. Both new engines generated
  67.45kW each. Fresh inspection 2064790 found no obsolete-only old positions
  still live, all new route positions present and no recovery remaining; all
  12 superseded coal-patch belt identities in the original audit were gone.
  Local collector/necessary escape remains. Planning 50.14s, execution 22.20s.

## Validation, unresolved work and next action
Full v2 suite at this checkpoint: 353 total, 11 optional Lua-runtime skips,
342 passing. Steam focused suite: 15 passing. Fresh native green, coal and
legacy proofs above are stronger than fixture-only success; they do not prove
sustained science rates or defense against an actual attack.

Iron job `luna-iron-resource-reroute-20261112` now succeeded on its original key:
24 actions/0 failures, proof2131116,86tiles/4turns/ratio1.04878, joins1119/1122
preserved. Initially stopped at fresh-production commissioning: all12furnaces
held100plates and one primary drill2422 had exhausted its patch. Kept the old
route buffers while diagnosing. Added safe CLI worker-stop input; the earlier
pause attempt correctly failed while another process owned the lock. After
safe stop/pause, native fluid-handling research50units completed2124329,
creating real demand. Updated mining proof to retain powered exhausted assets
while reporting150+120+60=330remaining capacity, installed360; no fake counters.
Resumed original job: new-path ore554 and136newironplates at final receipt.
Planning119.61s/execution23.86s/paused199.94s. Fresh2137264 proofTRUE,
205newironplates, all23oldcopper-patch belt identities gone, no pending recovery,
same joins and healthy compact factory. Local collectors/necessary exits remain.

Resource policy exposed a real fresh-start compatibility issue: the old selector
could latch an on-ore self-fuel arm. V2 now surveys off-ore boundary arms/ports,
uses shared transport and whole-plan preflight before persisting any new plan.
Existing plans remain fixed. Native boundary core passed; hypothetical full
selection against the occupied existing boiler failed safely in20.61s under a
10-route/20s budget. This is not a successful fresh-seed startup test.

Additive defense coverage is connected before dedicated mining construction.
Native readonly stone and steel candidate coverage passed; an impossible brick
site beside water correctly blocked. No new coverage post or stone bank has
been built. Steel/bricks/furnace upgrade are offline modules only. Mall,
automatic ammunition/repair, oil, rail and rocket remain unresolved. Next restore
active ore capacity, then guarded supply expansion and industry commissioning.

Part 8 parent usage sample: 57,412,163 tokens including cached input;
account weekly meter 17% to 22% (+5 percentage points). This includes the larger
Part 8 interval, not only this experiment. Child usage and this part's exact
share of the weekly quota are unavailable.

Part8 final measured checkpoint: 74,975,472 parent tokens including cached input; account weekly meter17% to23% (+6pp). This is the combined Part8 interval; child/per-module tokens and exact weekly quota share are unavailable.
