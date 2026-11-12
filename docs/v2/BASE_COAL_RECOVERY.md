# Continuous original-boiler coal recovery

`BaseCoalRecovery(game, builder, catalog, perimeter, *, steam=None,
outposts=None, reroute=None)` provides `prepare(obs)` (read-only), `step(obs)`,
`proof(obs)`, `depletion_diagnostic(obs)` and the cheap `verified` property.
Checkpoint: `base-coal-recovery.json`. Only existing ordinary build, splitter
configuration and exact guarded mine actions are emitted. ROOT owns public and
Watch dispatch. Diagnostic failure alone must not suppress ordinary defense work.

The original electric coal drill956 at(-48.5,14.5) was powered but had no minable
resources. Native4387124 inspected152 saved coal/power specs: none were missing,
the coal belts were empty, original boiler167 had no fuel, and inserter393 was
waiting with its own fuel still available. Rebuilding the same exhausted source
or topping up one boiler would not provide continuous fuel.

Recovery uses the existing paired power-coal bank. Fresh native resource-backed
powered potential, both physical drill-drop lanes, exact source3699 and actual
pure coal stock are required before accepting a plan. One shared66/min potential
source supplies two1.8MW boilers, whose pinned coal4MJ fuel and30kJ/tick boiler
usage imply54 coal/min maximum combined demand. The12/min difference is potential
headroom, not measured output and not two independent66/min sources.

Generic cardinal fork candidates preserve the old through line. Native splitter
input priority selects that existing input; output priority is NONE so fuel is
shared. A new bounded corridor reaches an exact existing original-coal turn;
the original boiler input port, its downstream belt/arm/water/engine units and
the working second boiler remain fixed. The source producer956 is explicitly
depleted and never counted as new production.

The old empty predecessor feeding the receiver is normally recovered too. It is
not silently exempted from the transport compiler's foreign-merge guard. Every
new item is normally funded, all nonconflicting branch assets are prebuilt, then
the exact empty predecessor and exact working fork corner are recovered with
saved intent and fresh unit/direction guards. A missing unstarted unit or foreign
replacement blocks; interrupted work does not invent a successful mine.

Whole preflight retains the strict resource/PCB, six-turn/detour, underground
isolation and native pipe-witness rules. Only exact immutable normal pipes use
their actual native orientation in the read-only geometry preview: old startup
templates used8 while native nondirectional pipes reported0. Their saved power
payload and actual entities are not rotated or rewritten.

Commissioning requires pure coal on both split outputs and the old receiver,
fuel/water in both boilers, steam and positive actual generation from all four
engines, sampled across30 game seconds. Fresh source potential is rechecked.
This is automatic-feed and loaded-generation evidence, not a full-load or
sustained-rate certification.

Publication adopts the paid fork in five authorities: factory builder, Steam
expansion, canonical mining bank, PCB board and verified Steam coal reroute.
A durable publication intent stores old/new hashes and resulting payloads before
the first atomic file write. A partially completed prefix resumes only when every
file equals its old or intended new hash; foreign changes block. Optional parent
components receive the same final state after publication.

The builder's active coal plan names the working shared producer and excludes
the obsolete predecessor and exhausted drill. The original source, inactive old
source port and depletion receipt remain explicit retired history, so a later
ordinary `ensure_power` does not rebuild the removed predecessor. All original
boiler interfaces stay fixed.

Read-only whole preparation passed at4480887 in4.25s: NORTH splitter(17,-2.5),
input LEFT/output NONE, new outlet(17.5,-3.5)N, existing receiver884 at
(51.5,-7.5)S. Normal stock:1 splitter,34 belts,4 underground belts;39 new
entities and279 exact preserved native units. Exact recoveries are empty883
at(50.5,-7.5)E and working corner3804 at(16.5,-2.5)N. Underground spans are4
and2. The author performed no gameplay actions or live checkpoint changes.

Nine focused tests pass, including executable native-Lua priority/depletion/
four-engine checks, both ordinary recoveries, foreign/loaded/unstarted failures,
preserved-pipe immutability, retired-source adoption and interrupted five-file
compare-and-swap publication. ROOT runs integration and actual deployment.

Actual recovery completed through automatic Watch-s in12 normal actions. The
30-second native sample verified both automatically fueled boilers and all four
loaded engines. Native4556084 read3.6MW offered and2.482MW headroom. The five
authority files were published with retired-source history; no actor topoff or
source capacity multiplication was used.
