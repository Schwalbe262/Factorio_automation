# EXP-0018: reserved corridors and active copper capacity

2026-11-12 KST, assisted development. Same default Space Age2.1.20 world,
speed1, Luna medium sole gameplay writer. Parent/implementation agents inspect
and improve code; these results do not count as frozen Luna-only acceptance.

| Loop | Result | Evidence / disposition |
| --- | --- | --- |
| Engine after refinery construction | blocked2646418,0actions | new actual pipe was a hard belt-routing barrier |
| existing-pipe crossing | native plan2681095 passed current geometry | parent rejected route146.5 through a future refinery slot before any construction |
| whole producer reservations | native2723795 blocked | first Engine site has an actual pole/ore/long-belt bottleneck below red science; no exception granted |
| chemical routing diagnosis | all5gas paths passed, coal paths failed | legacy families consumed node budget on impossible paths; no port relocation or budget increase |
| indexed whole-skeleton pruning | chemical native2755259 passed8.73s |280tiles/6turns/1.0448ratio;5,070expanded nodes,138,051invalid skeletons skipped before expansion |
| Engine anchor fairness | third existing anchor reached2775234 | roundrobin station ranks across sites; planned pole119.5,-105.5 was blocked by a neutral dead trunk |
| planned power anchor selection | complete native2803699 passed | choose another already planned clear pole in a copy; no machine/pole geometry change |
| copper upgrade150 | blocked2716030,0actions | active60 vs installed120; drills2434/2436 exhausted; guard retained |
| copper source restoration |26actions/0failures,proof2739412 |new7120/7121/7124/7125 provide120; total180active/240installed; both real new lanes |
| same copper upgrade resumed |10actions/0failures,proof2743923 |7126/7127 produced copper; catalog150/min,75per paired row; old output port retained |
| Engine after defense deployment |blocked2821547,37actions/0failures |owned new turret7152 at135,-95 blocks unbuilt adapter134.5,-95.5; no core machine is built; retain defense and replan only unbuilt feeds |

The router indexes hard cells by row and column and rejects whole invalid
orthogonal skeletons with bounded range queries before expanding tile paths.
It retains the previous family order/ranking. One shared node budget counts
every materialized node across legacy and fallback families; there is no reset,
second400k allowance, extra turn allowance or detour relaxation. Tests count
actual expanded path lengths against the returned visited count. Established
78-tile green/14-tile gear paths retain their accepted geometry.

Existing owned ordinary pipes may be crossed with straight catalog-bounded
underground belts. Exact pipe/world/force/position and reciprocal native peers
are captured and checked before building and during proof. The pipe is neither
removed nor modified. Other production reservations and shared-bus future bands
remain hard over both surface and buried paths. Exact owned typed ports may
use only bounded outward/entry aprons; other ports' first outside cells remain
clear. Resource/mining keepouts retain their previous strict policy.

Final Engine preparation2803699 used six routes and the third generic core
anchor118.5,-114.5. Steel and iron adapters are112/118tiles, four turns each,
ratios1.0566/1.1132. It preserves crude pipes6905(110.5,-52.5) and
6801(72.5,-120.5), including their exact peers. All new entities and buried
spans avoid the refinery/red/lab reservations; gas exit153.5,-61.5 remains clear.
The370entity plan passed with the unchanged20s routing/preflight guard; coverage
and setup retain their original separate calls. Exact total prepare wall20.765s
is not an end-to-end20s bound. Actual Engine deployment then commissioned its
four armed posts, but the post-deployment whole-plan check found a turret on an
unbuilt adapter. It blocked before any core machine was built. The fixed core,
ports and paid tap are retained. Follow-up inspection also found post7154
blocking the mandatory fixed input apron, so rerouting feeds alone cannot solve
both conflicts. A separately guarded, prepaid replacement is in development:
7152(135,-95) to(135,-93),7154(146,-109) to(146,-111). Read-only native combined
preflight2951770 passed; neither post has been moved yet.

Copper restoration preserved the old exhausted drills, collector and joins.
Its new28tile/four-turn route joins owned1217; post7063 had400health and10full
normal magazines. The following two paid furnace replacements produced3/15
copper at proof, with4plates on each output lane. These are real production
and nominal capacity proofs, not a sustained150/min measurement. The refreshed
bus declares375iron/150copper supply. Its422iron declared demand includes the
bounded critical ammo fill, so the47nominal deficit is not hidden.

Gas distribution completed2888709, proof2889194: five actions/zero failures,
13paid normal pipes, no undergrounds or new posts,13reciprocal edges. Alias
7228(160.5,-61.5)E feeds Plastic;7231(155.5,-64.5)N feeds Sulfur. Each contained
69.4286gas. Original6963 remains fixed; the two outlets share one540/min nominal
source. Planning21.94s/execution4.88s, without a sustained throughput claim.

Unbuilt Chemical products select their own published alias; accepted sources
remain pinned. Future post placement protects all registered external assets,
empty producer slots, bus expansion bands and fixed input/output continuations.
Five focused negatives plus existing regressions pass. Fresh complete Plastic
preparation2968151 passed13.11s with336entities using exact alias7228 and coal6983;
Luna resumed the same blocked0-action key. Actual Plastic commissioning remains
pending at this checkpoint. Blue, mall, repair, rail, rocket and sustained-rate
gates remain open. Part9 measured usage is reported as a shared interval;
child/per-module absolute tokens and exact weekly shares are unavailable.
