# Depleted drill retirement

`DepletedDrillRetirement(game, builder, catalog, *, outposts=None, paired=None,
steam=None)` exposes read-only `prepare(obs)`, `depletion_diagnostic(obs)`,
resumable `step(obs)` and independent `proof(obs)`. It accepts no placement
coordinates. Its checkpoint is `depleted-drill-retirement.json`. Parent jobs and
runtime wiring own activation of `retire_depleted_drill` through
`execute_retire_depleted_drill(game, action)`.

Only electric drills at registered canonical bank positions with known historical
unit IDs qualify. Sources include bank output/binding evidence and the verified
BaseCoalRecovery protected-unit snapshot. Original drill956 is explicitly linked
to BaseCoalRecovery plus the builder's inactive `retired_source` history; it is
not adopted as an active producer. Ambiguous, replaced, missing or unregistered
assets fail closed or remain explicit diagnostics. Existing collectors, poles,
ports, active drills and all canonical layouts/reservations remain unchanged.
The legacy956 source tombstone also blocks ElectricCoal reconstruction when the
physical drill is already absent before this ledger exists; it is not treated
as a paid item-recovery receipt.

Depletion is a fresh native query of the complete prototype mining radius and
**every positive resource type**. Empty output, a stopped animation, lost power,
`no_minable_resources` status or a blocked belt alone cannot authorize recovery.
Damage, non-normal quality, nonempty output or module inventory require ordinary
repair/cleanup before retirement. No goods are deleted to make these checks pass.

The typed action reloads its fixed checkpoint and owner/catalog authority, then
checks world, force, surface, actor, exact unit/position/direction, normal quality,
full health, minability, empty inventories and the complete empty native radius
inside the same Lua operation. It checks MAIN capacity, journals intent, invokes
ordinary `a.mine_entity`, and requires the entity to disappear while MAIN gains
exactly one normal electric drill. The immutable native receipt stores before/
after counts, actor/force/surface, depletion radius and ticks. There is no item
grant, raw resource mining, entity destruction call or custom drop vector.

Successful native receipts are appended to a digest chain. Known completed native
mines can be acknowledged after an interrupted metadata write without mining
again. Exceptions or unexpected results after a possible effect return
`drill_retirement_outcome_unknown`; partial native intent never becomes inferred
success and is not retried automatically. The current typed executor supports
the assisted backend. Character mining needs its own duration-aware receipt
contract and is explicitly unsupported by this executor.

`effective_mining_plan(game, obs, plan, catalog_fingerprint=...)` supplies a copy
excluding only drill nodes backed by authoritative paid retirement receipts.
It filters corresponding producer rows and recomputes required items while
retaining canonical pair count, old grid, growth reservation, ports and history.
A missing prepared/armed target without a recovery receipt blocks reconstruction.
`guard_drill_rebuild(game, action)` also rejects direct or batched builds at
registered depletion targets, covering legacy ElectricCoal/bootstrap paths that
bypass `ensure_plan`. Recovered inventory can be spent normally at a different
productive location with a new native unit ID.

Outposts and paired-lane proofs consume the active view. A fully retired bank is
an intact **zero-capacity** bank, not damaged machinery to rebuild. An exhausted
physical lane need not pretend to produce ore. Extension still grows on the old
four-tile grid; retired holes stay historical. Only newly added drills must meet
new-bank richness thresholds, while the resource survey still deduplicates the
current whole bank's accessible ore. No old drill is rotated or relocated.

`mining_capacity_summary(game, catalog, obs=None)` is one bounded read-only native
query over registered banks. It derives current speed/productivity, resource
backing, power and actual drop/receiver lanes. Full belts can be idle while still
having mining potential. A physical drill cannot be counted twice. Each lane is
capped separately by the native belt speed; bank aliases do not multiply supply.
After retirement exists, `refresh_bus_capacity` and tap profile rebasing cap the
furnace nameplate by fresh ore potential and the actual recipe conversion. Exact
bus ports/aliases and layouts remain unchanged. The parent must invoke this
refresh after retirement completes.

Read-only native prepare5129396 passed in2.062 seconds, selecting eleven safe
recoveries with439 retained source assets, no cleanup blockers and no unregistered
drills. Units:2421,982,2422,2423,2428,2429,2434,2436,5557,5559,956. Unit982 had
become empty since the older ten-drill survey. Native5129478 source potential was
231 iron ore/minute,165 copper ore/minute,231 coal/minute across independent coal
banks and66 stone/minute. Thus the iron plate profile must be capped at231 rather
than its375 furnace nameplate; copper remains capped by150 smelting. None of these
are measured sustained rates. Full preview is
`%TEMP%/factorio-depleted-drill-prepare-5129396.json`. No mine, live checkpoint or
PCB mutation was executed by this prepare.

Focused checks cover native conservation/idempotence/uncertainty, full-radius
foreign-resource rejection, ownership and956 history, damage/modules/quality,
receipt-only active views, direct/batched rebuild prevention, interrupted metadata
acknowledgement, zero-capacity banks, source deduplication and furnace budget caps.
The current retirement census/history is bounded at128 targets; exceeding it
requires an explicit archival migration, never silent receipt deletion.

## External mines and rail follow-up

The next read-only forecaster should return unique accessible resource tiles and
amounts per resource/patch, observed consumer draw over a timed interval, installed
versus active source potential, declared demand and train commissioning lead time.
Overlapping drill radii must share one resource count. An accessible-resource
horizon is not whole-patch life, and potential must not masquerade as measured
consumption. Trigger external-bank planning before a conservative horizon reaches
construction/research/defense lead time plus buffer, rather than waiting for the
last drill to stop.

A reusable rail plan needs accepted flow and growth directions, new productive
paired banks and fixed off-resource ore outlets, normal powered/defended loading
and unloading stations, train/fuel capacity, exact native railway unlocks and
recipe costs, independently proven rail connectivity/path and scheduled train
movement, and typed ore delivery into preserved smelter ports. Prepare the whole
bank/station/rail/unload contract before migration. No rail or external mine is
built or certified by this retirement module.
