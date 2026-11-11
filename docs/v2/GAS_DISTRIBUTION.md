# Shared gas distribution

`GasDistribution(game,builder,catalog,perimeter,*,petroleum=None,chemical=None)`
provides read-only `prepare(obs)`, paid resumable `step(obs)` and fresh `proof(obs)`.
It creates `gas-distribution.json` and owns the original petroleum outlet's one
declared downstream edge as `chemistry-gas`. It never mines or moves the original
refinery, pipeline or source port. An accepted chemical plan or another consumer
already owning that original edge blocks this upfront manifold operation.

The manifold publishes two new registry aliases, `plastic-gas` and `sulfur-gas`,
both carrying `petroleum-gas`. Their stable descriptors contain typed geometry,
world, exact unit and defense scope. Capacity stays outside those descriptors in
state/proof: one verified refinery currently provides **540 gas/min nameplate in
total**, shared by both outlets. Distinct verified refinery units determine that
capacity. Adding a normally commissioned upstream refinery can update capacity
without changing the aliases or any bound handoff's full source equality. There
is no measured throughput or doubled-production claim.

`gas_manifold_core(x,y,side=-1)` has a straight east header and a north/south T
branch. Both published endpoints remain normal pipes. Two empty outward cells are
reserved for each outlet, and its third cell is the first clear cell outside the
reservation. All three cells must remain resource/terrain/foreign-reservation
clear. The original producer's strict approved straight apron is retained. The
first new source neighbor is declared before building, then bound to the saved
native layout unit after ordinary paid placement.

Eight finite direct/above/below candidates use the existing bounded typed pipe
planner and its unchanged six-turn/`1.35×Manhattan + 4` limits. Native terrain,
resources, mining growth, existing producer expansion reservations and accepted
unbuilt engine assets/full core remain hard constraints. A pipe underground may
cross ordinary non-fluid hardware with clear straight mouths; every crossed
existing asset keeps its exact unit/position/direction before and after the build.
A landing mouth replaces only a prospective manifold inlet pipe, never existing
hardware or a published outlet. Current source-pipeline extent plus the entire
new network must fit the actual native pipeline limit.

Coverage and fresh whole-plan preflight precede construction. Neither declaration
nor preparation inserts fluid or creates game entities. Proof requires current
upstream petroleum production, the bound original handoff, exact manifold units,
gas-only contents, reciprocal native peers, native pipeline extent and gas in
**both** new outlets. Unexpected peers fail; later plastic/sulfur consumers are
accepted only through their exact bound alias handoffs. Both aliases are appended
atomically after proof, preserving every original source and handoff entry.

Read-only native preparation passed at tick **2822837** in 5.66 seconds against
the accepted 370-entity engine plan. The compact option needs **13 normal pipes**,
no undergrounds and no additional defense posts: original outlet6963
`(150.5,-61.5) E` stays fixed, first new neighbor `(151.5,-61.5)`;
`plastic-gas` is `(160.5,-61.5) E` and `sulfur-gas` is `(155.5,-64.5) N`.
The combined native extent is 30.789×4.789 versus the installed 320 limit. This is
preparation evidence. Luna subsequently completed the same normal-cost manifold
at tick **2888709**, independently proved at **2889194**, in five actions with
zero failures (21.94s planning, 4.88s execution). Thirteen pipes were paid and
built, with no undergrounds or new posts. Published outlets are exact units
**7228** and **7231**, respectively; both contained69.4286gas at proof. The
original outlet6963 and refinery6967 remain intact. Thirteen reciprocal edges
were verified. Contents certify a live connection, not a sustained gas rate.
Eleven focused tests cover ownership, paid construction gates, guarded underground
landing/crossed assets, capacity changes with stable aliases, and executable Lua
for native purity, reciprocity, units, extent and later declared consumers.
