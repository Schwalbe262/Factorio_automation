# Strategic gun posts

The user manually repelled the attack that damaged the development factory.
The framework subsequently repaired its furnace district and commissioned
eight proactive gun posts through Luna-selected ordinary jobs. The prior
battle is human intervention, not framework combat success.

| Sector | Native position | Initial magazines |
| --- | --- | ---: |
| West smelter | (-50,-26) | 10 |
| North smelter | (-32,-43) | 10 |
| South coal | (-55,32) | 10 |
| Middle bus | (-20,-66) | 10 |
| North iron | (15,-85) | 10 |
| South steam | (43,18) | 10 |
| North labs | (32,-127) | 10 |
| East compact green | (77,-69) | 10 |

Preparation surveys native turret range18, terrain, resources and factory/bus
reservations before choosing each post within eight tiles of its sector anchor.
These protect selected approach points; they do not form continuous coverage
of the entire factory perimeter. Fixed sectors must be extended as the factory
grows. There are no radar-derived attack forecasts or walls in this version.

`luna-defense-perimeter-20261112` succeeded at tick1477979 with68actions and
zero consecutive failures. All eight exact-unit posts had400/400health and
100normal rounds each; no enemy was within their inspected36tile radius.
That is armed-post commissioning evidence, not a combat stress test.

Every subsequent job runs bounded post upkeep. Below five magazines, a known
live post is topped up toward ten. Turrets and magazines are normally crafted
and paid; no bullets or inventory are granted. The identity/world-guarded loader
transfers only full normal magazines from exact actor inventory stacks and
preserves a partially used source magazine. Changed identities block rather
than adopting an unknown replacement.

Upkeep runs while a job is executing; it is not an idle background service.
Missing posts are reported and can be rebuilt by `defense_perimeter`, but
upkeep does not rebuild them. Damaged health is observable; automatic repair,
continuous ammunition assemblers and belt-fed supply remain unfinished. Those
are required before claiming unattended long-term defense.

Industrial and outpost controllers can gate their first production build with
`ensure_coverage(obs, scope_id, areas)`. The stable scope ID and rectangle list
are persisted beside the existing posts. The gate returns an ordinary paid
craft/build/guarded ammunition action, or a waiting/blocked result; **only
`None` permits production construction**. `prepare_coverage` performs the same
planning read-only, and `coverage_proof(obs, scope_id)` returns current evidence
after commissioning. `DedicatedMaterials` calls the gate after reserving its
new bank and before building its miners. Existing post identities are preserved;
a missing known post requires explicit perimeter repair.

The coverage metric partitions each rectangle into cells at most four tiles
wide. Every complete cell must have all four corners within a single turret
disk of native attack range minus two tiles. A disk is convex, so this proves
the entire cell is covered and avoids an uncovered hole between independently
covered outer corners. The proof reports area-weighted coverage and exact
covering units. This is geometric coverage, not evidence of a won battle.

New posts are placed outside all native resource tiles, factory/mining/shared-bus
reservations and protected rectangles. A native walkable four-neighbour grid
must connect an adjacent service tile to the protected footprint's perimeter
within the bounded local survey. This is local access at that snapshot, rather
than a global actor path guarantee. Placement and access are checked again
before each build. Scope geometry changes under a previously used ID block.

Before a new scope opens, its added and covering posts each need ten **full
normal** magazines. A partly used magazine is counted separately and cannot
satisfy this requirement. Existing commissioned scopes use the usual five
magazine upkeep threshold so consumption does not permanently stop construction.
There is no automatic ammunition factory or idle ammunition service.

Read-only development-world survey at tick2105961 found complete coverage
possible for the prospective stone bank `[[-23,41],[-15,48]]` with one added
post and the steel reservation `[[75.5,-45.5],[94,-31.5]]` with two. No posts
were built by that survey. The original brick reservation
`[[75.5,-23.5],[94,-9.5]]` blocked: even all573 legal accessible candidate sites
cover only235/259 of its area under this metric, beside native water. Brick
must choose a land-valid alternative and prepare coverage there. These survey
results describe that world snapshot, not fixed placement guarantees.
