# EXP-0011: green commissioning, steam and paired iron expansion

- Date: 2026-11-12 KST; official2.1.20/default development world, speed1.
- Goal: normally supply green science and every lab, increase loaded generation
  and source capacity, diagnose slow construction without bypassing production.
- Hypothesis: typed fixed-port production blocks and resumable normal jobs let
  Luna commission machinery without deciding individual frames or placements.
- Classification: assisted development. Parent fixes and user combat intervention
  invalidate Luna-only acceptance; no sustained-rate or rocket success claimed.

## Green science loop

Native red-only research unlocked logistic-science-pack, steel-processing and
fast-inserter at ticks1069428/1072969/1088638, one research-selection action
each. The actual engine consumed packs; no technology flags were changed.

`luna-logistic-science30-20261112` built seven green machines plus local gear,
wire, circuit, belt and inserter machines. Initial573actions ended blocked at
tick1248029. A sparse single-tile lane sample rejected real flow, and the first
join at x43.5 was downstream of the eighth lab x45.5. Seven labs receiving green
is a real missing-consumer defect, not an observation-only failure.

The same job normally recovered four entry assets and moved the join to
x47.5,y-114.5 upstream of every pickup. Its original history was retained. Final
608actions/zero consecutive failures, proof1282753: all eight labs held16packs,
row lanes held128green/128red. Seven green machines finished
[57,57,56,55,54,45,3] products; intermediate counters were gear737,belt363,
cable637,circuit408,inserter358. No actor inserted packs or machine ingredients.

The proof now checks every lab's receipt, full row lanes and recent unit-bound
evidence, rather than demanding a packet at one exact tile on every recheck.
The native join and full directed lab path remain required. Nominal35green/min
requires192.5iron/52.5copper; with red36, total264.5iron/88.5copper. These figures
are recipe-derived design demand, not measured output.

The first western block's54x53 reservation/956complete-plan entities were too
sparse. Its commissioning was useful evidence, but its design is being replaced
by the compact shared-bus migration in EXP-0012.

## Loaded steam loop

`luna-steam-expansion-20261112` added dedicated opposing coal drills, a boiler
and two engines while preserving the original plant. One owned worker was
gracefully stopped to reload corrected probes, then the same key resumed.
Engine direction canonicalization accepts the same native axis, fluid contents
use actual get_fluid_contents(), and probe errors block explicitly. Evidence
timestamps use the fresh native tick instead of rejecting a probe newer than
the outer observation.

Final209actions/zero consecutive failures, proof1330776. Native history spans
1328831..1330660, over30game-seconds. Both added engines generated535.36kW each,
with steam199.55; boiler held coal5 and water199.73. Added1800kW and total3600kW
are nameplate ratings. This proves loaded positive generation, not full-rating
stress performance. Coal pair60/min is nominal; both collector lanes had stock.

## Mining, damage and recovery loop

`luna-iron-outpost300-20261112` retained the old180/min bank and added a normally
powered four-drill120/min paired bank. Final433actions, proof1388428. Output
joins exact owned trunk1119 through an11tile/twoturn same-item adapter. Collector
lane stocks were5/5 and7/7. Planning487.34s/execution117.74s; total elapsed about
15.56game-minutes/964wall-seconds. Nominal300ore/min is not sustained flow.

During the later furnace expansion, biters damaged furnaces/belts. The user
manually repelled them. The original225iron job blocked at1398968; it was not
silently replaced or counted successful. Ground item recovery was extended to
all reserved footprints, and noncolliding corpses stopped causing false
planning obstruction. All material recovery used ordinary inventory rules.

After EXP-0012's proactive posts, the same smelting key succeeded at1501532:
220actions/zero consecutive failures; all12iron and6copper furnaces had products.
Iron collector held26plates, copper182; ore and coal were on separate input
lanes. Iron capacity225/min still falls39.5/min below full-rated science demand.
Recorded terminal-blocked time1337.76s was excluded from active retry budget;
original creation time, action history and full elapsed time stay visible.

## Validation and next

162v2tests passed at the shared-bus launch checkpoint. This includes regression
coverage for lane audits, steam native evidence, job retry accounting, stock
collection, guarded priorities and migration retirement gates.

Remaining: complete compact bus migration, supply enough iron for a measured
five-minute science rate, permanent steel, construction mall, automatic ammo
and repair. Oil/rail/rocket remain unavailable. Part7 parent usage is recorded
in note.md from measured session counters; cached input is included, child
counters and exact per-task weekly quota are unavailable.
