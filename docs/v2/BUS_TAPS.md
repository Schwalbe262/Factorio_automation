# Adding a consumer to an installed bus

`BusTap(game,builder,catalog,bus_factory=...,science=...,green=...)` exposes
`prepare(obs,parameters)`, `step(obs,parameters)` and `proof(obs)`. Pass existing
component objects so publication refreshes their in-memory state as well as
their files. Parameters are name, item, splitter_y, priority, demand_per_minute
and optional active line index(default0).

Example requested contract: name`steel-iron`, item`iron-plate`, splitter_y-51.5,
priority`surplus`, demand37.5/min. The existing bus determines the columns and
outlet x; its current geometry yields output(12.5,-52.5)E. This station is a
proposal pending native resource/structure checks, not an approved live site.

Preparation reconstructs every material/source rate/reserved column and
consumer/merge from the saved bus. It recompiles the contracts and preserves
all old ports, directions, expansion reservations and underground pairs. Old
crossing layers remain even if their original crossing belt was retired. Only
the new complete horizontal branch is compiled against today's foreign belts.
The only allowed recovery is one exact owned NORTH surface trunk belt; every
other existing native unit must remain unchanged.

New resource placement is forbidden. A read-only native query checks every
new branch path tile, including buried underground spans, plus all new/changed
footprints. Shared preflight checks the added entities with only the exact
replaced belt virtually removed. Existing bus entities are grandfathered solely
by verified unchanged unit identities/configuration; build refuses to repair a
lost grandfathered unit. Thus old resource overlaps never authorize a new tap
or a new underground route across a resource patch. Future mining reservations
also remain subject to shared planning policy.

Before the guarded mining action, all splitter, belt and underground items
are normally acquired and approved nature is normally cleared. Stock and
retained units are checked again immediately before mining. Completed recovery
is removed from pending state before the replacement is built/configured.
The builder applies native input/output priorities. NORTH surplus taps use
left/left, retaining continuation priority toward science.

Publication changes only bus subsets in bus-factory.plan/red_plan,
automation-science.plan, logistic-science.plan, board.logistic_science and
board.shared_main_bus. It adds no production machines and retains production
ports, coal/source plans and lab joins. Fresh documents are validated before
writing; an interrupted mixed old/new publication can resume idempotently.

Proof requires the native splitter configuration, unchanged retained units,
exact underground partners and actual correctly typed material at the outlet.
Its `port` includes actual belt unit and `priority_splitter` unit/position for
`SteelProduction`. A new surplus outlet does not add producer bandwidth or
guarantee a supply rate. Empty surplus output remains an explicit wait.

Focused tests cover preserved topology and capacity, exact one-unit removal,
stock-before-cutover, resource checks beneath tunnels, read-only preparation,
publication without new machines and native commissioning. No live tap was
installed by this change; root owns deployment and per-part usage reporting.
