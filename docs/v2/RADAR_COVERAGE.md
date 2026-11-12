# Factory radar coverage

`RadarCoverage(game, builder, catalog, perimeter, *, radars=None)` provides
read-only `prepare(obs)` and `coverage_report(obs)`, resumable `step(obs)`,
`upkeep(obs)` and `proof(obs)`. There are no coordinates, raw actions or new
runtime handlers. State is `radar-coverage.json`; original RadarNetwork and radar
15071 remain unchanged. Parent jobs/Watch wiring owns activation.

The installed 2.1.20 prototype API
`get_max_distance_of_nearby_sector_revealed('normal')` supplies the constant
visibility radius in chunks. Native force `is_chunk_visible` is checked separately
from `is_chunk_charted`. Normal radar currently has radius3: a 7 by 7 chunk square,
32 tiles per chunk, drawing300 kW. Far-sector exploration and old charted history
are not counted as current factory visibility. Existing radars count only when
their exact normal, force/surface-bound units have power, working status and a
valid native scan progress. Progress zero at a cycle boundary is valid.

Targets union all accepted PCB bodies and reserved expansion areas, mining,
smelting, steam, utilities, shared-bus reservations, buried belt/pipe crossings,
fixed source exits, registered defense posts and other actual owned structures.
Half-open footprints and negative coordinates use real chunk boundaries. Corpses,
loose items, ghosts and the moving character do not create target chunks. The
initial bounded native census4917841 found63 target chunks: all63 charted, but
only31 within the powered radar's actual visible footprint.

The deterministic bounded set-cover prefers sites under two already armed
registered guns. Every entire 7 by 7 service reservation must fit within both
native gun disks with a two-tile margin. Site checks reject resource tiles, mining
growth, accepted future producer/trunk footprints and fixed-port aprons, nearby
enemies and hostile worm range. All four service sides need native walking access.
Power bridges preserve exact source poles/networks, avoid future hardware and
reserve planned electrical load plus at least20% native offered capacity.

When only one existing armed anchor covers a remote gap, the read-only standard
DefensePerimeter planner is cloned, excluding just that anchor from selection.
It must select one distinct additional normal gun, with both guns covering the
entire radar service area. The full radar, second gun and power plan passes native
preflight before adoption. A persisted compare-and-swap intent adds only the
prepared post/scope to global Perimeter, retaining every prior post and scope.
Known interrupted writes resume idempotently; unrelated changes fail closed.
Previously published scopes belonging to this controller may make normal binding
and commissioning progress without being overwritten by their preview.

Actual execution calls unchanged `ensure_coverage` to pay for that gun and ten
full normal magazines. It then rechecks two actual full-health guns with at least
five full magazines each before building the radar. A one-anchor survey is only
a planning step. Selected yellow or piercing ammo is read from each registered
post; no conversion, grant, offensive deployment or special damage action exists.

Candidate surveys are capped at96 for the existing-pair family and another96 for
the single-anchor fallback, twelve complete site attempts and four new radars per
generation, under one20-second query budget. The fallback is generated only for
remaining gaps, so already covered base candidates cannot displace remote oil
sites. Four-tile candidate diversity avoids repeating a blocked service side by
the same pole/pipe. There are no world-specific waypoints or relaxed land guards.

Native whole prepare5002861 passed in18.110 seconds, covering all63 target chunks
in the prospective plan. It selected four additional radars and six poles, plus
one separately owned defense gun and ten magazines. Positions were derived as
80.5,-64.5; -48.5,31.5; 130.5,-124.5; and61.5,-300.5. The new oil guard57,-301
partners existing gun6455. Added radar peak is1.2 MW; native headroom remained
1.885 MW with720 kW reserve required. Full read-only preview is
`%TEMP%/factorio-radar-coverage-prepare-5002861.json`. No construction, checkpoint
publication or game action was executed by that prepare.

`proof` remains false until every current target chunk is both inside a powered
radar footprint and natively visible. Commissioned additions also retain exact
bindings, PCB plans, local security/access and power evidence. Gaps and their
factory owners are explicit. Future accepted factories expand the target union;
upkeep may plan further additive coverage while preserving existing radars.
Missing or replaced paid assets require explicit normal repair, not silent ID
adoption. This is a factory-visibility policy, not full-map exploration or combat
stress acceptance.

Focused tests cover chunk edges, all target families, buried spans, bounded
set-cover/fallback fairness, original radar retention, native visibility versus
chart history, power/scan identity, exact ammo tiers and partial magazines,
resource keepouts, normal defense-first ordering and interrupted scope publication.
