# Starter coal and resource keepouts

The generated compact starter cell has a coal burner drill, four local core
belts, one ordinary straight outlet belt and a fuel-return burner inserter.
Executing its actual resource-check Lua on
an interior coal patch rejects the unscoped inserter. Broadening the mining
whitelist is unnecessary: a read-only survey found boundary placements, and
the earlier six-entity variant at `(-47,23)` passed strict native whole-cell preflight with
no grandfathered entities or nature clearing. Its arm is at `(-45.5,23.5)`.

The v2 builder now selects a boundary cell whose fuel arm and outgoing port are
both off resources and mining reservations. It then uses the shared typed,
bounded transport planner and preflights the complete power/cell/coal route
before returning a coal plan for persistence. Failure tries another candidate
and never latches a rejected new coal plan. Only the drill and its local belts
carry the existing mining scopes; no inserter exception was added.

The cheap boundary survey considers at most 256 sites. Full routing is limited
to ten attempts and a 20-second planning deadline, checked between queries.
One in-flight native call may finish after the deadline. A bounded failure
reports attempts, elapsed time, candidates examined and the eight most recent
candidate/stage/reason records; it does not relax corridor quality limits.

Existing saved coal plans remain unchanged. Their ordinary resource checks
preserve exact live assets or block an incompatible/lost asset; selection never
deletes a checkpoint or relocates an operating plant. A hypothetical selection
against the current already-fed boiler failed closed and left its saved coal
plan unchanged. With the new budgets it stopped in 20.61 seconds after one full
route attempt and 17 boundary candidates, returning
`starter_coal_planning_budget_exhausted` with recent failure details.
That earlier check did not reset a seed, construct a starter plant, or
prove complete fresh-seed startup. Its eleven focused tests covered selection, native
Lua filtering, attempt/deadline limits, failed-plan persistence and existing
checkpoint behavior.

On fresh seed1053994892 the initial selector exhausted its20-second budget.
The corner outlet received a perpendicular upstream belt from its own collector;
the shared crossing compiler correctly rejected this as an unproved foreign feed.
Publishing the next paid straight belt fixes that geometry without a crossing
exception. The new outlet has no mining-resource scope and must be off ore.

Whole preflight then exposed cosmetic direction4 in saved ordinary pipe specs,
while native pipes report direction0. Only exact observed ordinary pipes are
normalized in a temporary preflight projection. Every planning query guards
their unit/name/type/force/surface/position and native direction. Underground
pipes and machines retain all direction checks; the saved power plan and ports
are unchanged.

Read-only complete selection succeeded in6.744 seconds with the original
20-second/ten-attempt limits:81 entities,79 paid belts,74-tile route,5turns,
Manhattan68 and detour ratio1.088235. Fifteen focused tests passed, including
native changed-unit/force/surface/direction rejection and saved-plan preservation.
Luna subsequently retried the same83-action power job; the real coal plan was
saved and the job completed at119 actions with zero failures. Native tick234428
proved water200, steam400, nine coal on belts, both connected/energized engines
and1938 verification ticks. This is startup flow readiness; generation and total
delivered energy were zero with no connected consumers, so it is not a sustained
load measurement. The15coal/min starter source still needs later expansion for
both engines' full nameplate load.

The caller now retains concrete failed-plan diagnostics. Layout search failures
carry `failure_category=layout_planning`, visible in the bounded planner snapshot;
Luna is instructed to distinguish executor faults from a material shortage.
Previously it expanded coal cells2->4->6->8 without resolving the route defect.
