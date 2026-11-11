# EXP-0012: shared bus, compact green and proactive defense

- Date: 2026-11-12 KST; official2.1.20/default world, speed1.
- Goal: shared raw-material trunks, compact green, practical defense and faster
  normal execution. Parent development/user combat intervention make this an
  assisted attempt. The user repelled the attack; framework combat is unproven.
- Hypothesis: reusable bus/tap/producer contracts and whole-layout audits remove
  frame-level decisions while preserving lane bandwidth and expansion space.

## Defense result

`luna-defense-perimeter-20261112`:68actions/zero consecutive failures,
proof1477979. Eight exact-unit strategic posts held10normal magazines each,
400/400health, native range18, no nearby enemies. A later fresh check during
migration again found all8healthy with100rounds. [Post map](../docs/v2/DEFENSE.md).
Later-job upkeep refills below5to10; ordinary actor crafting supplies ammo.
Automatic ammo assemblers, general repair and idle monitoring remain absent.

## Shared bus and compact design

One northbound iron and one copper line feed four normally configured priority
splitters for red/green. A second column per material is reserved; underground
crossings over those future columns are installed now. More belt lines do not
create producer throughput. At current225iron/112.5copper supply, full-rated
red36+green35 requires264.5iron/88.5copper, leaving39.5iron/minute deficit.

Compact green has12AM1/216internal entities in30x22 occupied space with32x28
reservation, replacing54x53. Direct wire/circuit/inserter transfers and shared
iron replace separate long raw snakes. Seven adapters satisfy the unchanged
six-turn and1.35*Manhattan+4bounds; longest77tiles/5turns/71Manhattan.
[Bus contracts](../docs/v2/MAIN_BUS.md), [core layout](../docs/v2/COMPACT_GREEN.md).

The complete fresh native migration preflight passed:696new entities,
640initial exact-owned recoveries,458old assets for later retirement,55clearable
trees. Recovery disconnects old raw corridors first, so uninterrupted production
is not guaranteed. Old production hardware and all poles remain until fresh
replacement AND source-smelting proofs permit each retirement batch. Independent
review found and fixed both the stored-proof retirement gap and missing source
health gate before live execution.

## Speed diagnosis and partial migration

Old iron expansion took402iron collection calls to move41plates;343/374belt
calls movedzero despite1740iron on354belts. The live-ranked construction collector
now pools up to16sources under a50item cap with identity/normal-quality and
conservation checks. First8new calls moved314/314iron for paid defense.
This establishes collection efficiency for available stock, not production speed.

`luna-main-bus-compact-20261112` normally recovered the old corridors. Its first
80batches took426.51executionseconds because each batch still sent8RCON calls.
A root-owned stop latch ended the worker at a known action boundary. After
review/178tests, the SAME key resumed with one-query native batches, ordinary
mine_entity recovery and durable reconciliation for uncertain prefixes.

Paired migration had also retained stale copper56.25capacity from its old
three-furnace row. Actual3+3furnaces yield112.5. New all-bank catalog/entity
derivation replaces stale counts and numeric fallbacks;6regressions cover it.
Root corrected only derived runtime capacity metadata under the writer lock.

The resumed worker reached build phase but blocked at130actions/3failures,
tick1561835, on mining a tree. Actual inventory:80slots/zeroempty, wood298 with
capacity2 while the tree required more. It also held624recovered belts,
1594iron/2063copper, ores and757green packs. No items were deleted/granted.
The block is an executor inventory-headroom failure, not a Luna planning failure.

## Current result and next

Defense, green commissioning, power/source expansion and collection improvements
are verified components. Shared-bus/compact live migration is **unfinished** at
this checkpoint. Next add normally paid recovery storage/headroom upkeep, retry
the original known-blocked key, then require native12machine/all8lab/bus priority
proof and complete old hardware retirement. Render the actual finished layout.

The inventory block was subsequently resolved through paid dedicated storage
(EXP-0013): chest4234 normally received371ironore and the same key resumed to
181actions/zero consecutive failures in build phase. Final migration proof is
still pending; this update does not promote partial construction to completion.

Validation checkpoint:178v2tests plus53legacy game/builder tests pass. Per-part
measured usage is in note.md; it includes cached parent input, with no child
counter or exact weekly task quota exposed. No sustained30SPM/5min or rocket
acceptance is claimed.

## Final native result

The SAME key succeeded at1660346 with347actions, zero consecutive failures at
completion (the earlier three inventory-related failures remain in history).
Proof1660440: all12compact machine units and all8labs healthy, typed bus stock
509iron/553copper, four native input-left/output-right splitters. Green counts
[45,45,45,43,42,39,11]; intermediates223belts/573gears/320inserters/320circuits/
483cable. Both lab lanes held128packs; labs held16green total. Preserved source
furnaces12iron/6copper all produced, with separate ore/coal feed lanes,3UGpairs
and3actual source splitters. All458obsolete retirement assets were recovered.

Planning456.81s/execution669.54s; excluded recorded pause/blocked549.91s. The
commissioning wait included the last green station receiving its first inputs;
no additional geometry change, pack insertion or production shortcut was used.
Independent fresh proof1663671 passed again, pending recoveries/retirement0,
old western non-pole production/transport entities0. All8posts healthy100rounds.
Actual board image was regenerated and visually inspected. Cutover is complete;
sustained30SPM/5min, automatic ammo/repair and rocket acceptance remain open.
