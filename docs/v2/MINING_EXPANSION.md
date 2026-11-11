# Active mining capacity and protected additions

`mining_outpost` takes an absolute target, for example
`{"resource":"iron-ore","rate_per_minute":360}`. Installed drills whose native
status reports no remaining mineable resources stay owned but contribute zero
to active capacity. Loss of power or missing assets still blocks expansion.
The controller tries its fixed-port upstream extension first; a full reserved
bank instead receives a new normally paid paired module connected to the
existing same-item delivery trunk.

Connected preparation follows the exact saved source-to-furnace corridor,
including explicit underground pairs, for at most160 saved entities. Potential
side-loads use existing straight ordinary belts, bind their current unit and
direction, and reject foreign items or entry tiles on resources/mining growth
reservations. A bounded search tries up to six bank sites and three join probes
per site: near, middle and far along distance-ranked available joins. A rejected
site is added only to local planning reservations; preparation never persists
those rejections. Whole prospective entity footprints, native placement,
resource keepouts, underground spans and route quality must all pass. No old
miner or aggregation belt is recovered or moved by this additive controller.

`MiningOutposts(..., perimeter=existing_perimeter)` enables the construction
gate. If construction order requires it, assigning `outposts.perimeter` before
calling `step` is equivalent. Pending new banks and extensions publish their
canonical mining reservation, save a stable `defense_scope_id`, then call
`ensure_coverage(obs, scope_id, [reserved_area])` before `builder.ensure_plan`.
Only the gate's `None` result permits mining construction. It requires complete
geometric coverage and ten full normal magazines when commissioning the scope.
Only banks carrying this explicit new scope require current coverage in their
output proof; unchanged legacy banks retain their existing proof contract.

A future mining output on a resource patch can be routed before construction
only when it is an exact east-facing belt in the original supplied tagged core,
with nearby tagged drill entities sharing its bounded core footprint. Annotation
validation and whole-core native preflight run before passing that original
core to the local-exit selector. The selector additionally checks live resource
purity and availability. No ignored-unit exemption is passed to the prospective
core's preflight. Existing owned output routing retains its previous behavior.
The twelve-step local-exit bound and whole-route turn/detour limits are unchanged.

Read-only development-world evidence at tick2206754: the three existing iron
banks were installed360/active330 because unit2422 was exhausted; all remained
healthy. The primary bank had no spare reserved columns. A new two-drill paired
bank `[[5,-77],[9,-70]]` passed with60 nominal capacity, adding headroom to390
active/420 installed. Its77-entity plan joined westbound owned belt5424 at
`(-25.5,-55.5)` through a south-facing entry immediately north. Preparation took
9.22seconds and site1/route2; the71-tile,3-turn route passed
`length <= 1.35*51+4`. Existing joins1119/1122 were retained. Read-only defense
preparation found one additional off-resource post and ten full magazines
sufficient for the28-tile mining reservation. These are planning snapshot
results; no construction, restored capacity or sustained throughput is claimed.
