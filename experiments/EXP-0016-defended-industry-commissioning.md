# EXP-0016: defended materials and industrial commissioning

2026-11-12 KST, assisted development. World32a34974-d7cd-4ec0-9f35-13259ea32b8e,
official2.1.20, default resources/enemies, speed1. GPT-6 Luna medium performs
typed CLI jobs; parent/implementation workers develop and diagnose. No frozen
Luna-only, sustained-throughput, combat-stress or rocket success claim.

## Hypothesis and native outcomes
Restoring active ore capacity, keeping fixed bus ports and protecting new
reserved production footprints should support normal steel/brick construction.

| Attempt | Actions / failures | Native result |
| --- | --- | --- |
| dedicated stone | 14 / 0 | proof2180067; drills5463/5464, both real lanes,60nominal |
| oil-gathering research | 1 / 0 | completed2204182;100red+100green catalog units; interval104/96consumed |
| iron active restore360 | 22 / 0 | proof2236512; new60pair,390active/420installed; exhausted2422 retained |
| steel surplus bus outlet | 11 / 0 | proof2243159; outlet5606/splitter5581,37.5iron/min declared |
| steel factory | 0 / 0 | blocked2244585 before construction; unresolved at this checkpoint |
| brick factory | 101 / 0 | proof2276064; two native furnaces produced5each; both output lanes |
| electric distribution1 research | 1 / 0 | completed2383222;120red+120green normally consumed |
| shared steel coal | 5 / 0 | proof2422761; splitter6101/outlet6100, existing brick flow preserved |
| steel factory resumed, same key | 60 / 0 | terminal2477638/proof2477998;41steel each, both real output lanes |
| crude extraction | 29 / 0 | proof2486204; pump6507, pipes6504..6506 contain native crude; natural oil-processing unlock |
| iron furnace upgrade360 | 4 / 0 initially | blocked2530060 on spilled coal in exact mined gap; scoped ordinary pickup recovery tested, same-key retry running |

Stone fresh proof2195005 includes live coverage: turret5451(-22,38),400health,
10full normal magazines, all4cells/56area covered. Iron's post5465(3,-70)
likewise has10full magazines. New iron route71tiles/3turns joins exact5424;
1119/1122 and all old miners remain. Prospective tagged cores are accepted only
after complete native preflight; the join search traces the full owned corridor
and native underground pairs instead of stopping after30surface cells.

Brick posts5630/5628 each had400health and10full magazines, covering20cells.
Input lanes held stone22/coal11 and output lanes held5bricks each at commissioning.
Rated37.5bricks/min is distinct from the60stone/min source ceiling of30bricks/min;
neither is a measured sustained rate. Planning183.30s/execution156.30s.

## Failures and changes
The steel factory's generic failure concealed the specific raw-feed blocker.
Its coal outlet5277 has power pole5266 on its first east tile. North/east
continuations are coal resources and the south path is the now-working brick
coal header. Moving the pole alone cannot open an independent legal route.
No pole was moved. The next implementation uses an explicit normal splitter
on the existing brick coal header, preserves5277/5278 and shares60total capacity.
Readonly whole-layout preparation passed2335046; live commissioning completed
2422729. Full idle automatic furnaces can release their recipe while retaining
valid output. The fallback now rechecks exact unit and normal raw/fuel/output
inventories with actual finished products. A second proof fix excludes vertical
raw belts from the horizontal steel output group; native2477917 then passed.
Shared header replaces only owned6035, preserving5277/5278 and brick core ports.
Steel6172/6176 had41products each and output lanes41each at2477998. Posts6170/6171
had400health/10full magazines.7.5steel/min is nameplate, not sustained proof.

Future material planning now protects empty output approaches against its own
prospective power bridge. Existing commissioned ports stay unchanged. New
mining/machine jobs gate construction on paid coverage, and current material
proofs also require current live coverage. Powered exhausted drills reduce
available capacity without invalidating their healthy neighbors.

The public furnace-upgrade integration checks fresh active mining, rather than
installed nameplate: it rejected330/min before restoration and passed8replacements
for a360target/375catalog headroom at2316721. Normal48steel/80bricks remain
necessary. Derived bus profiles have guarded idempotent metadata refresh tests;
the first physical replacement is in progress. Native2531121 found one normal
coal dropped by the input arm into the committed gap. Scoped recovery requires
the exact gap, only normal coal/requested ore and no other collision before an
ordinary conserved pickup.20focused furnace tests pass; full upgrade pending.

## Research overlap and remaining work
Electric distribution1 was selected normally, run5seconds, then its frontend
job paused2314123. Native research continued0.008185to0.032296 at2314487 and
0.904481 at2338036 while the single writer was free. Completion still needs
its own resumed native proof, completed2383222 with120red/120green consumed;
no technology flags or science counters changed.

Oil-processing is a native crude-extraction trigger, not another science-pack
research. Observation now exposes prerequisite-ready natural triggers. The
pumpjack completed29normal actions/0failures2486016, planning54.93s/execution18.87s.
Native2486204 confirmed pump6507 and pipes6504..6506 each holding21.2084crude;
outlet6506(70.5,-303.5)E is published. Post6455 had400health/10full magazines.
Oil-processing unlocked from native extraction. Ammo and petroleum interfaces
are exposed, pending live commissioning. Refinery readonly2504240 passed a305tile/
two-turn route with2underground pairs/6wire hops, native77.789x252extent below320;
344pipe/4undergroundpipe/7poles/1refinery plus3armed posts are normally paid.
All531v2 tests pass with local executable Lua. Native2539805 resource overlap
audit found23miners/82local belts/35poles/1pumpjack and no production building
on resources; legacy mining transport is not permission for new trunks.
Furnace upgrades, ammo/refinery live proofs, blue, mall, repair, rail and rocket
remain open. Child/per-module token shares are unavailable.
