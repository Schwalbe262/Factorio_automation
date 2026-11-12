# Automatic blue science

`ChemicalScience(game,builder,catalog,perimeter,*,engine=None,advanced=None,
chemical=None,science=None,green=None,bus_factory=None)` exposes `prepare(obs)`,
`step(obs)` and `proof(obs)`, with `chemical-science.json` state. Preparation is
read-only. Public capability `chemical_science` uses this controller. Normal
research accepts a fresh Blue proof through its optional `blue` provider and
records the actual science supplier units and consumption interval. Missing
suppliers remain explicit blockers; no research materials are inserted.

Native Advanced commissioning exposed a shared static belt model error: a sole
perpendicular feeder can create a corner carrying both lanes. The model now
distinguishes that curve from a T side-load, and Blue includes an empty inline
east-facing tail at local `(-2,3)`. Removing the tail fails the corrected lane
audit. Native Blue whole-plan acceptance and live production still require the
actual ingredient producers and natural chemical-science research; no such
Blue deployment success is claimed here.

The initial 31×13 block reserves four assembler slots and builds two AM1s.
Installed 24-second/2-output recipe and .5 crafting speed give nominal 5 blue
packs/min from 5 engines, 7.5 advanced circuits and 2.5 sulfur/min. Full four-AM
growth preserves ports/assets and gives nominal 10/min, requiring 10 engines and
15 advanced circuits/min; current initial upstream producers cannot sustain that
larger form. Normal/long inserter design budgets are conservative 30 items/min,
not measured rates. All ingredients require fresh exact native producer proofs,
fixed world/unit/facing outputs and pinned producer-plan revisions. No actor
feeds, item grants or research flags implement production.

The lab extension owns a separate eastbound header at lab `y+4`, with eight
ordinary long-handed inserters at `y+2` facing south. Their native two-tile reach
passes over the existing **copper** transport at `y+3`, without changing that
route, the red/green science header, eight lab units or existing poles. Current
row geometry gives outside input `(15.5,-107.5)E`, inside registered input
`(16.5,-107.5)E` and last header tile `(45.5,-107.5)E`. The latter leaves a gap
before the existing green entry. These coordinates derive from the saved lab
row; factory selection and public APIs do not accept seed-specific waypoints.

The generic blue output route stops outside the owned lab reservation. Exactly
one straight entry cell plus the new header/arms are separately audited against
the original owned lab row and native units. Any existing asset at a prospective
blue hardware tile is rejected even if ordinary `can_place` would accept the
same belt direction. This prevents reusing the copper header as blue. Foreign
unbuilt external hardware/tunnels and their halo remain hard for the future
core, new lab interface and all generic routes; no reservation-release exemption
is used. Strict resource, producer reservations, underground identity and
six-turn/1.35×Manhattan+4 routing gates remain in force.

Four complete routes, initial/future hardware and power must pass whole native
preflight before a plan is saved. Routing/preflight share the existing 20-second
query budget and a total twelve route attempts; setup/source proofs and the
separate defense preview are not an end-to-end time guarantee. The complete
plan is registered before protected paid post placement, followed by fresh
preflight and preserved pipe-peer checks before production construction.

After real commissioning, publication adds **only input-port metadata** to
`science.labs` and `board.laboratories`. Their entity lists and red/green plans
remain unchanged: Green's existing lane proof inspects every lab-owned belt.
New south header/arms stay exclusively in the Blue plan. Two-document metadata
publication accepts old/already-published values and can resume after interruption.

Proof returns `science_units`, `lab_units`, actual `lab_science_stock`, catalog
`rated`, and stable typed `source`/`output` descriptors. Native gates require all
powered AMs with real finished products, normal ingredient/output purity, exact
arm reach/held items, native underground pairs and current upstream proofs.
Every original lab must currently contain normal red, green and blue packs.
Under later research demand this strict stock gate may need a separate valid
working-lab witness; it does not certify sustained consumption.

Validation: sixteen focused offline tests pass, including executable Lua tests
for the intended lane contracts, all eight lab stocks, source/quality/power and
long-arm failures; fixed growth, old-entity preservation, foreign unbuilt-plan
guards, failed-plan nonpublication, twelve-route budget and idempotent metadata
publication are covered, plus the corrected-model missing-tail regression. The
75-test focused family includes Advanced, Blue, CompactGreen, lane physics and
Engine. No gameplay or runtime-state writes were performed by this work.
