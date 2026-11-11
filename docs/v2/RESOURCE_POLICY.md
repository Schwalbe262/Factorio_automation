# Resource keepouts

New production buildings, belts, underground routes and power bridges avoid
resource tiles and registered mining growth reservations. An existing owned
asset is grandfathered only while its exact observed unit/configuration remains
in place. Its loss does not authorize rebuilding it on resources. Parent-owned
migration is required for old long corridors or chests that overlap patches.

`resource_policy.py` supplies shared policy-spec generation, native checks and
the cached V2 builder gate. `planning.preflight` checks this policy inside its
existing read-only whole-layout query. `V2FactoryBuilder.ensure_plan` also checks
it before clearing nature, collecting construction materials or building, so a
controller cannot bypass resource policy by skipping preparation. Successful
checks are cached by world, entity plan and mining reservations. Missing or
changed grandfathered units and rollback ticks invalidate the approval. No
per-action policy RPC is needed for an unchanged approved layout.

The mining exception is per entity: `mining_resource` is one of iron-ore,
copper-ore,coal,stone and is allowed only on electric/burner mining drills,
local transport belts and small electric poles. Factories, furnaces, labs,
splitters, underground mouths, inserters and chests cannot use it. The old
`resource_cell` flag grants no exemption. Core assets carry a bounded
`mining_footprint`; non-drill local assets must have a nearby declared or actual
owned drill. Native resource names must match the declared resource.

`mark_mining_core(entities,resource)` annotates a generator's actual local
entities and returns their footprint union. Per-column scopes remain stable
when a mining bank grows westward. Mining pair/bank generators publish this
top-level footprint and tag their EAST output port. Planned power bridges
remain untagged, except their destination when it is an explicitly declared
core pole; every intermediate new pole avoids ore and mining reservations.

A registered mining source port can request a minimal bounded escape.
`transport.plan_connection` checks every route tile even if an old belt already
occupies it. The source prefix must leave both its matching resource patch and
mining reservation at the first clear tile within12steps. The common native
selector prefers straight EAST, then one initial EAST step followed by NORTH
or SOUTH. It permits at most one on-patch corner, without loops, backtracking
or underground mouths. Every candidate checks native placements, the owned or
planned mining source, its nearby drill, matching resource/reservations and
foreign entities; only exact owned recovery targets may be ignored. Another
resource/reservation type or a farther boundary fails closed. Only the necessary on-patch prefix is
tagged with `mining_exit={source_position,facing:4,step,length}`. `length` is the
last on-patch step(0..11); the first clear tile and later trunk are untagged.
Bent exits additionally carry `path`, the complete ordered contiguous list from
the source through its first clear cell. Validation requires matching surface
belts throughout that path and rechecks the first-clear boundary natively.
The original straight metadata API remains supported. Dedicated coal uses the
same selector and adds its splitter and two EAST outputs beyond the clear cell;
those distribution entities receive no mining exemption and share one capacity.
The coal hub also surveys and reserves a clear EAST continuation tile beyond
each output. If the first clear cell cannot fit this apron, the hub may advance
up to four more cells along the exit heading. Every extra connector cell must
stay off all ore/reservations and pass native placement; it remains untagged.
The original prefix metadata still ends at the first clear cell, so the apron
does not enlarge the mining exception or its twelve-step limit.
Its original `mining_reserved_area` preserves drill growth keepouts independently
of the larger complete bank reservation around the hub. Publishing that larger
reservation therefore does not reclassify off-patch distribution as mining land.
The existing six-turn and1.35*Manhattan+4 length gates remain unchanged.

New underground mouths also cannot hide an ore crossing between them. Policy
checks virtual buried-span tiles without creating virtual construction. An old
tunnel is grandfathered only through both exact native paired mouths; losing
either anchor invalidates its cached approval. Pairing with an existing foreign
mouth is also included in a new tunnel survey.
Old mouths scheduled for owned recovery are excluded from prospective pairing;
desired replacement mouths still participate normally.

Legacy runtime ports without mining metadata are not inferred to be authorized
sources. The parent must reconstruct the exact local core from its registered
bank before updating metadata. Long legacy collars that turn on ore may now
fail preparation and need a separate safe route migration. No runtime or game
state was changed by this implementation.

Focused tests cover forbidden/coarse exemptions, local scope geometry, matching
resources, exact grandfathering and cache invalidation, builder enforcement,
bounded exits and hidden underground crossings. Optional executable native
selector tests use `lupa` (`test_v2_mining_exit_native.py`); ordinary Python tests
cover complete L path metadata and off-patch coal distribution. These checks
are not a live route or full-game proof.
