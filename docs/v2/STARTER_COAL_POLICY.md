# Starter coal and resource keepouts

The generated compact starter cell has a coal burner drill, four local belts
and a fuel-return burner inserter. Executing its actual resource-check Lua on
an interior coal patch rejects the unscoped inserter. Broadening the mining
whitelist is unnecessary: a read-only survey found boundary placements, and
the six-entity cell at `(-47,23)` passed strict native whole-cell preflight with
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
This task did not reset a seed, construct a starter plant, or
prove complete fresh-seed startup. Eleven focused tests cover selection, native
Lua filtering, attempt/deadline limits, failed-plan persistence and existing
checkpoint behavior.
