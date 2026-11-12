# Independent water intake

`WaterGathering(game,builder,catalog,perimeter)` exposes `prepare(obs)`, `step(obs)`
and `proof(obs)`. Preparation reads the world without saving state. The caller
executes ordinary paid construction actions returned by `step`. The controller
persists `water-gathering.json` and publishes only its proven exact pipe under
`fluid-sources.json` → `sources.water`. Crude and petroleum entries are preserved.
An existing water entry without this intake checkpoint causes an explicit block.

`water_core(catalog,position,direction)` derives the physical output from the
installed offshore-pump fluid connection and rotated internal position, then adds
three straight normal pipes. In the inspected catalog the base connection points
south, so facing is `(8 + rotation) % 16`. Native prototype inspection confirms no
electric or burner supply is required. The generic exported `energy_usage` value
is not used to infer an external power requirement or a measured pumping rate.

Planning surveys shore tiles within 32 tiles of up to four existing offshore pumps,
keeps at most 24 legal sites, and shares a twenty-second query budget. Native pump
placement must succeed. The entire rotated asymmetric pump body, all pipes and two
future continuation cells must avoid resources and mining growth reservations.
Existing industrial/main-bus reservations and foreign fluid networks are kept
clear. Defense coverage and whole-plan preflight precede construction; the coast
and whole plan are checked again after any new coverage hardware is installed.

The original steam pump and its immediate peer identities are recorded and must
remain unchanged. No existing pump, pipe or power hardware is moved or mined.
Native proof requires exact new units, reciprocal indexed fluid peers, water-only
contents and actual water in both pump and outlet. The outlet's native fluid segment
must differ from every preserved steam-pump peer segment and fit the installed
pipeline extent. The offshore-pump fluid box itself has no pipeline segment in
2.1.20; segment identity is queried only on the pipe peers and new pipe outlet.
Destroyed or replaced bound intake hardware blocks automatic reconstruction.

Read-only preparation passed at tick **2657444** in 5.84 seconds: a new pump at
`(87.5,-25.5)` facing west toward the water (direction12), with its independent
eastbound outlet `(90.5,-25.5)`. It requires one offshore pump and three pipes,
uses existing armed coverage and preserves pump166 `(54.5,0.5)` direction4 plus
its pipe peer397. This records preparation only; construction and native water
publication followed at tick **2669609**: five normal-cost actions, zero failures,
pump6990, pipes6987..6989 with100water each. Original166/397 were preserved and
existing armed post6171 covered the intake. This proves finite water buffers,
not sustained pumping throughput. No fluid insertion, item grant, research
edit or production-counter edit is emitted.

The board plan carries the intake's exact `defense_scope_id`, allowing its
published outlet to use the same strict bounded source apron as other producers.
A verified legacy plan missing only this metadata can publish it after fresh
native proof and exact old/desired board equality. Board-first publication is
idempotent across a restart; changed geometry or a different scope blocks. The
source descriptor, ports, entities and physical game remain unchanged.
