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
Readonly whole-layout preparation passed2335046; live commissioning is pending.

Future material planning now protects empty output approaches against its own
prospective power bridge. Existing commissioned ports stay unchanged. New
mining/machine jobs gate construction on paid coverage, and current material
proofs also require current live coverage. Powered exhausted drills reduce
available capacity without invalidating their healthy neighbors.

The public furnace-upgrade integration checks fresh active mining, rather than
installed nameplate: it rejected330/min before restoration and passed8replacements
for a360target/375catalog headroom at2316721. Normal48steel/80bricks remain
necessary. Derived bus profiles have guarded idempotent metadata refresh tests;
no physical furnace replacement or profile update has yet been commissioned.

## Research overlap and remaining work
Electric distribution1 was selected normally, run5seconds, then its frontend
job paused2314123. Native research continued0.008185to0.032296 at2314487 and
0.904481 at2338036 while the single writer was free. Completion still needs
its own resumed native proof; no technology flags or science counters changed.

Oil-processing is a native crude-extraction trigger, not another science-pack
research. Observation now exposes prerequisite-ready natural triggers. The
pumpjack module's readonly plan passed:52paid assets, one defended post; actual
oil extraction waits for normally produced steel. Automatic ammo is in progress.
Steel, furnace upgrades, refinery/blue, mall, repair, rail and rocket gates remain
open. All measured usage belongs to Part9; child/per-module shares are unavailable.
