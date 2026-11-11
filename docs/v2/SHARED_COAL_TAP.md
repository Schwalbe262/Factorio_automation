# Steel coal from the existing shared header

`SharedCoalTap(game,builder,catalog,brick,*,industrial)` provides coordinate-free
read-only `prepare(obs)`, resumable `step(obs)` and fresh `proof(obs)`. The public
capability is `steel_coal_supply`, following commissioned brick production.

The original dedicated coal outlet is retained unused. Native inspection found
its first eastward tile occupied by its power pole, coal ore north/east and the
working brick feed south. Moving the pole alone cannot produce an independent
legal corridor. No source port is rotated or silently reused, and no mining
or underground resource exception is introduced.

The controller traces the saved brick coal delivery, chooses among at most six
downstream straight EAST header sites and places an EAST splitter above it.
The existing lower output continues to brick production. A new upper output
belt faces NORTH for steel. Native input priority is `right`, selecting the
existing lower input; output priority is `none` for ordinary sharing. Both
consumers share the original bank's total nominal capacity, currently 60/min.

Preparation checks the complete splitter/outlet and its empty first NORTH
continuation, all current brick identities and underground pairs, and complete
prospective steel feeds, core, power and whole-layout coexistence. Search has
at most eight prospective steel attempts and the shared 20-second query
deadline. No waypoints are injected and normal route quality limits remain.

Native class preparation at tick 2335046 derived old header belt 6035 at
`(107.5,33.5)`, an EAST splitter at `(107.5,33.0)` and a new NORTH outlet at
`(108.5,32.5)`. Whole preflight passed with prospective steel at `(114,-40)`:
iron route 171 tiles/one turn and coal route 74 tiles/two turns. These are
preparation results, not completed production or measured throughput.

Execution stocks one normal splitter and one belt, prebuilds the clear outlet,
checks replacement stock and fresh coexistence, and mines only the exact owned
old corner. It records started recovery before returning that action. The new
splitter is normally built/configured and all other brick units, source ports
and core ports stay fixed. Acceptance requires coal on both native outputs and
the existing brick recipe, fuel, power, automatic input lanes and output lanes
to remain healthy. A fresh brick counter increase is unnecessary when output
buffers are full.

Only after that proof are `brick-production.plan`, its board copy and the brick
authorization hash updated, and `material-sources.json` gains `steel-coal`.
It uses the same bank ID and shared nominal capacity as `brick-coal`.
`IndustrialOperations.adopt_shared_coal_source` explicitly migrates only an
unbuilt steel consumer, guards current registry/native identities and records
the old unused outlet. It refuses a nonempty steel controller/checkpoint.
Mixed old/new publication can resume after interruption. Original coal ports
5277 and 5278 and their registry entries remain unchanged.

Steel failure evidence now includes candidate, stage, material and both exact
handoff positions. The generic router cheaply rejects hard-blocked short port
prefixes before spending expansion budget; its candidate ranking, total node
budget, six-turn and detour limits are unchanged. This optimization does not
make the original dead-end coal outlet usable.

Parent owns live execution and commits. This implementation task performed
read-only native preparation only; state is `shared-coal-tap.json` when executed.
