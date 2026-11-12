# Finite normal-projectile nest assault

Retired from the public gameplay capability list after user feedback. Historical
paused checkpoints and native receipts remain intact. New work uses the single
concentrated `nest_sweep` capability described in NEST_SWEEP.md; the dispatcher
rejects resuming staged offensive harassment.

`NestAssault(game,bootstrap,builder,catalog,perimeter)` exposes `prepare(obs)`,
`step(obs)` and `proof(obs)`. Preparation is read-only. The controller saves only
its own `nest-assault.json` checkpoint and registered PCB blocks when executed
through the normal job coordinator. No attack has been executed by this module's
author during implementation.

The target set is one dangerously close charted stationary cluster, at most12
exact native unit/force/surface/world identities. Distances originate from actual
productive factory buildings. New offensive guns never move that origin. Missing
targets alone do not establish victory.

The first group is generated from at most96 integer positions supported by two
actual distinct armed rear guns within native gun range minus2. It reserves three
mutually covering guns and24 ordinary walls with open rear service. All ore,
mining growth, productive PCB reservations, future external transport, typed
ports and source approaches remain protected. At most4096 pure groups and four
whole native group preflights are considered; local service is bounded4096cells.
Preparation requires an actual successful prospective service proof; trees do
not excuse a failed service result. Distant groups that cannot reach a firing
opportunity within the remaining three-stage geometric envelope are rejected.

Before the first paid turret, stock eleven guns,200 full normal magazines and72
walls: three60-magazine groups,20 reserve magazines and two spare guns. The
selected ammunition is immutable. Researched piercing rounds are preferred;
missing ordinary ingredients are collected before launch. The installed catalog has
two piercing magazines per run, using two yellow magazines, one steel plate and
two copper plates. Yellow fallback is explicit only if the native piercing
recipe is unavailable. Existing turret ammunition is never raid stock. The
automatic yellow factory is preferred; finite normal engine handcrafting is
allowed only as this assault's supplement, with at most200 of each magazine tier
actually started. Budgets use paired native craft receipts and actual `started`
runs times catalog yield. Queue-busy receipts consume zero budget, active queues
are waited on, and unknown outcomes require reconciliation. At most one repair
pack is described; the actor does not travel to or
repair the frontline and must remain at least48tiles from every target.

`start_nest_assault` is a typed metadata operation handled by
`execute_start_assault`. It validates all initial enemy identities before setting
up a native `on_entity_died` ledger. An unrelated existing handler blocks safely;
it is never overwritten. The native journal fixes a10800tick launch deadline and
records exact friendly gun physical-projectile death causes and post losses.
Unknown initialization outcomes stay explicit for durable-intent reconciliation.
Two hundred piercing magazines require100 ordinary six-second recipe runs,600
game seconds of character crafting. Setup is outside the180second launch budget.
After full stock, first-stage geometry and exact rear bindings are refreshed
before any turret placement. Restoring a missing native listener reuses the
original deadline; an absent listener never authorizes new payment.

Each new group uses the independently guarded
`bastion_group_deployment_action(...,owner_kind='nest_assault')`: all three sites
and actual stock are checked before payment, then three normally paid guns and60
full magazines are deployed in one native query and one game tick. Worm-first
target priorities use the ordinary turret UI API, while unlisted targets remain
enabled. No enemy damage, health, destruction or research setter is used.

Immediately before payment, the guard repeats rear identity/health/full-stock,
force damage/speed, target identity/range and native deadline/loss checks. It
scans the bounded maximum installed enemy-turret range and rejects new covering
worms; authorization of known25range worms is not a blanket worm exemption.
Controlled under-fire trials may lose deployed guns. Two losses or180game seconds
stop new stages, and ten seconds without target damage during a firing opportunity
blocks further advance. Existing guns can continue ordinary fire afterward.

Offensive posts use a **stage-owned** perimeter registry. They are never inserted
into the global maintenance/rebuild list. Aborted assaults therefore cannot cause
Watch to rebuild guns forever inside a nest or block unrelated factory gates.
No dead paid post is automatically repurchased. At most three generated stages
are attempted, and stage2/3 native enemy collisions remain blocked until real
deaths clear those footprints.

Proof requires every original target to be natively absent **and** have its exact
native friendly-gun physical-projectile death receipt. It reports target deaths,
selected ammo, surviving posts and losses. Other or unknown causes are not
credited, and the result makes no whole-map safety claim. Focused tests execute
the real Lua ledger, unknown-handler protection, stock tier selection, finite
geometry, scope isolation and stop budgets. Actual combat acceptance is pending
the sole authorized gameplay writer.

Read-only whole preparation passed at native3767007 in4.797s: generated turret
centres(186,-43),(186,-35),(188,-39), two exact armed rear witnesses11376/11377,
three whole gun/wall cages with native service, one approved natural obstacle and
ten stationary targets. Current ordinary stock could fund200 piercing magazines'
ingredients. This is prospective geometry/stock evidence, not a battle result;
launch rechecks current units and can choose different generated geometry.

Development run `luna-nearby-nest-assault-20261112` reached99 actions at tick
3837109 before launch, then stopped on `assault_checkpoint_changed`. The stock
path had initialized an in-memory craft map without saving it before returning
a collection action. Stock reads now leave the checkpoint unchanged; native
craft reconciliation saves only a verified accepted outcome, and the native
launch clock saves before early returns. Real controller-step regressions cover
repeated collection, precursor transfer, queue waiting and receipt reconciliation.
Retry keeps this same operation, paid stock and action history. This failure and
the fix are not evidence of an enemy kill.
