# Resource-avoiding additive-steam coal route

`SteamCoalReroute(game, builder, catalog, steam, outposts)` exposes read-only
`prepare(obs)`, resumable `step(obs)` and commissioning `proof(obs)`. It owns
only the saved corridor from the additive steam bank's east-facing collector
to that plant's coal input. The current interfaces are (-51.5, 14.5) east and
(37.5, -1.5) south; the controller derives them from saved plans and preserves
their positions and directions. It does not change the older plant's coal.

Preparation traces the exact saved route, including declared native
underground pairs, and binds every old asset to its observed unit identity.
Missing or changed old assets block preparation. Only the physically owned
paired drills, local collector and local poles receive a bounded mining-core
scope. The typed east-facing source allows only the shared router's verified
minimal local escape, with the first clear tile at most twelve steps away. Ordinary long
transport avoids resources and mining reservations. Existing six-turn and
`1.35 × Manhattan distance + 4` length limits remain unchanged. Failure to fit
returns the router's blocker; there is no maze fallback.

The controller retains identical old belts and complete identical underground
pairs. It prebuilds every clear addition that cannot prematurely divert old
flow or interact with an old underground mouth. At most eight old units may
require cutover. All deferred replacement items must be held in normal
quality before a guarded single-unit mine. A fresh observation prunes that
old identity before downstream-first gap construction; a new unit is never
retargeted as an old recovery. Canonical source metadata and all foreign
structures, mining equipment, poles, water connections and engines remain
fixed.

The old obsolete corridor and its buffers remain physically present until
coal is observed on **added route entities**, the new underground neighbours
verify, and both additive engines generate under load over a fresh sampled
thirty-game-second window. Shared old-tail coal alone cannot pass this gate.
Retirement mines only saved old-not-new identities, in batches of at most
eight. Every batch requires fresh route/loaded-engine health; a failed sample
preserves remaining old assets and restarts the sample window.

Publication updates the steam plan and bank, refreshes the exact matching
canonical outpost bank from its latest file, and updates `steam_expansion`
plus the existing canonical coal reservation through
`outposts.publish_reservations`. It creates no alternate reservation alias.
Native generation samples are synchronized with the steam component. Unknown
action outcomes remain subject to the parent job's durable-intent recovery.

Pure/temporary-file tests exercise read-only preparation, foreign-asset and
missing-asset rejection, prebuild ordering, prepaid cutover, restart recovery,
whole underground-pair handling, added-path coal, the thirty-second window,
fresh retirement health and the eight-unit bound. The implementation worker
did not execute gameplay. Parent-assisted Luna commissioning subsequently passed:
24 actions, no native failures, proof tick2061572, both new engines generating
over31.3game seconds and coal on added route entities. See
[EXP-0014](../../experiments/EXP-0014-resource-keepouts-and-safe-migrations.md).
