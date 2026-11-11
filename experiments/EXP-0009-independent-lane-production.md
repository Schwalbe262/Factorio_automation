# EXP-0009: independent producers on both belt lanes

- Date: 2026-11-11; same official2.1.20/default development world, speed1.
- User correction: splitting a single production row onto two lanes cannot
  increase production. Use opposing furnace and mining rows with bandwidth
  accounting. Treat each lane as a separate channel, not a display property.
- Hypothesis: paired independent producers around one straight collector,
  fixed consumer ports and surveyed expansion give a clean scalable module.
- Options: rejected cosmetic output redistribution; use real splitters on the
  furnace INPUT to share ore/coal between producer rows. Mining drills drop
  directly onto the two collector sides, with no output splitter.

## Furnace loop and real failure

`luna-paired-smelting-20261112` normally recovered152 obsolete owned entities,
retained connected ore targets/coal power and built iron5+5/copper3+3 furnaces.
Copper moved to leave a service corridor; downstream plate ports stayed
iron(-9.5,-29.5), copper(-9.5,-17.5). Exact unit/world identities guard recovery.

Native geometry passed, but live recovery exposed an iron electrical island.
We stopped at an action boundary, added a normal paid four-hop pole bridge and
resumed the SAME job. Existing full output buffers prevented fresh production
checks: normal bounded withdrawal of two plates from each of four old iron
furnaces (8total) made room for new native cycles. No raw ore/fuel was inserted.
Future plans explicitly include the row-connecting pole chain.

Result:234actions, final consecutive failures0; proof899150. Every one of the
16 furnaces produced freshly beyond its own pre-migration baseline. Iron's new
left row products[4,4,4,4,4], old right row[436,429,446,609,549]; these cumulative
counts are NOT rates. Shared collector inventories ironleft1/right4 and
copper4/4. Per-lane catalog capacity iron93.75, copper56.25 plates/min.

## Mining loop and real failure

The first aligned candidate failed native collision against an orphaned old
starter furnace; discovery now includes owned isolated raw-patch furnaces.
Copper's original lower row failed the actual remaining-resource threshold.
A short two-turn collar permits a richer aligned site while retaining its
external output. The readonly preflight ignores only exact owned recovery IDs;
unknown structures, terrain, cliffs and other forces stay protected.

`luna-paired-mining-20261112` planned58 ordinary recoveries, including11legacy
burner/furnace assets. First execution blocked at60actions/tick998448: one
spilled iron-ore item obstructed pole(14.5,-60.5). This was NOT established as an
inventory-full problem. Receiver-first recovery briefly left an emitting drill.
The fix recovers emitters first in future migrations and ordinarily takes
existing ground items inside an exact blocked resource-site footprint.

The SAME job was reopened through the new guarded `retry` interface. It keeps
its request, action history and original deadline; unknown outcomes require
reconciliation. The failed attempt remains in the event journal even though
the receipt's consecutive-failure counter returned to0.

Result:77total actions, one historical placement failure, proof1011081:

| Resource | Pairs/drills | Nominal/min | Native collector stock left/right | Fixed external output |
|---|---|---|---|---|
| Iron | 3/6 | 180 | 43/4 | (18.5,-62.5), east |
| Copper | 2/4 | 120 | 31/3 | (12.5,-33.5), east |

Native drill drops: iron north y=-62.6523 / south y=-62.3477; copper north
y=-35.6523 / south y=-35.3477. Each group was powered, correctly oriented and
dropped onto its declared live collector receiver. Mining drops use the NEAR
side here, unlike an across-belt inserter's far-side drop. Unique remaining
surveyed ore55277/24443 deduplicates overlapping drill coverage.

Same-row future columns fail actual ore/terrain gates at these mineral edges;
do not claim unused expansion slots exist. Growth preserves the fixed port and
adds complete pairs only in proven reservations. Otherwise create another
paired block and explicitly connect its outlet to the existing ore trunk.
One auxiliary iron pair passed native placement/resource/power preflight;
its construction and downstream connection remain untested. Existing single
coal sources were retained to preserve the operating power supply.

## Validation and limits

Fresh independent checks at1027038 and1033772 still passed paired production.
The loaded design bound is min(ore, independent per-lane producers, per-lane
belt capacity), before fuel/power/inserter/demand losses: iron180/min,
copper112.5/min. Native normal belt capacity450/min/lane. Stock on two lanes
does not prove sustained throughput. No unseen-seed or frozen Luna-only score:
the parent diagnosed and improved the executor during this development run.
See EXP-0010 for the real downstream science/research load and shared Part6
validation/token sample. Continuous-rate/green/mall/defense/rocket gates remain.
