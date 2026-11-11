# EXP-0017: paid upgrades and fixed-port consumers

2026-11-12 KST, assisted development on the same default Space Age2.1.20 world.
Luna medium remains the sole gameplay writer. Parent/implementation agents
develop, inspect native state and reconcile explicitly recorded metadata. No
frozen Luna-only or sustained-rate acceptance is claimed.

| Loop | Attempt and result | Evidence / remaining issue |
| --- | --- | --- |
| upgrade initial | blocked after4actions/0reported failures | exact owned furnace4139 mined; native2531121 found coal1 in its gap |
| pickup recovery | ineffective, stopped after191cumulative actions/0reported failures | each pickup succeeded but the live input arm dropped again between ticks; same key paused2561974 |
| atomic paid gap build |8replacements physically produced;228total actions |2579456: units6551..6558, iron83/coal84 input lanes, requested360 and catalog375/min |
| independent proof | callback exception exposed premature persisted success | missing StarterExecutors game/catalog fixed; sync now saves waiting until proof returns; exceptions persist blocked, never success |
| native revalidation | verified2585802, counters/actions preserved |8real producers and both lanes; derived source375iron/112.5copper synchronized under writer lock |
| ammo tap | blocked2587552 after18actions | root capacity refresh225to375 differed from the already prepared tap's cached profile; no geometry changed |
| profile-only rebase | same ammo key succeeded,34total actions/0failures |2598944: assembler6591 made3 normal full magazines into exact barred chest6582; actor held0 |
| plastics research | selected normally, frontend paused |200red+200green catalog cost; native0.003178 continued0.019022 at2601083; completion pending |
| basic petroleum | worker33103 building,60actions/0failures at2616688 | normal pipe crafting/construction; no petroleum production claim yet |

`build_furnace_gap` performs the normally conserved ground pickup and prepaid
one-item build in one native command, preventing the inserter from dropping
between those operations. Exact pending old unit/world, actor, empty footprint,
normal coal/requested ore, stack metadata and pickup capacity are checked first.
The native dry run changed nothing. Four executable Lua/envelope tests include
resource conservation and foreign collision/quality/capacity/actor rejection.

Tap capacity rebasing is allowed only after a verified source upgrade. Every
non-capacity entity/port/station/priority/reservation must match the exact prior
or resulting contract, and current profiles must equal catalog-derived actual
source types. New demand is recomputed separately; source and belt capacity are
never multiplied by outlets. Changed geometry and invented rates still block.

Ammo is rated30magazines/min with120iron/min while filling its bounded100magazine
chest. This critical burst can reduce lower-priority science supply. It is not
a concurrent full-rate science claim. All three perimeter acquisition paths use
the manufactured full-normal-magazine provider after commissioning; real
downstream transfer during new post deployment is pending verification.

The engine module is implemented/frozen, not yet built: compact31x19 with two
engine AM1s plus gear/pipe AM1s, four future engine slots, fixed steel output and
a new surplus iron tap. Nominal6engine/min needs24iron+6steel/min; the current
shared steel source is7.5/min. Native read-only whole preparation2597954 passed
both adapters, future keepouts, power and four armed posts. Twelve focused tests
include executable ingredient/lane/normal-quality/native product proofs.

Exact fluid consumer declarations/bindings now allow a producer-approved native
gas output connection without accepting unrelated fluid peers. Seven handoff and
nineteen petroleum tests cover reciprocal edges, contamination and replaced units.
Live chemistry requires normal plastics research, a new independent coal supply
and proven gas; sulfur also needs independent water and explicit gas distribution.
These remain open, as do mall/repair/blue/rail/rocket and sustained30SPM.

Usage checkpoint shared with Part9:72,818,543parent tokens including cached input;
account23to28%(+5percentage points). Child absolute counts and exact per-module
weekly shares are unavailable. Never infer those shares from the account meter.
