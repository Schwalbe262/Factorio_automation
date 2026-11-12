# Continuous commissioned bastion maintenance

`BastionMaintenance(game, bootstrap, catalog, perimeter, hardening).step(obs)`
returns an ordinary action, a blocked result, or `None`. The parent Watch calls
it before ordinary Perimeter upkeep. Only the exact persisted, verified
three-post Hardening cluster is eligible; no placements or scopes are invented.
World, catalog, PCB plan, scope geometry, post identities and native ownership
must agree. Unrelated strategic or offensive posts stay with their existing
controllers.

Known losses use Hardening's paid-build lineage and fresh native absence checks
before clearing a unit binding. All missing turret items and twenty full normal
yellow magazines per missing post must be held before deployment. Three missing
posts use one same-tick `build_and_arm_bastion_group`; one or two missing posts
use a single atomic post action, with supplies prepaid for all missing posts.
The atomic helper independently rechecks normal MAIN stock and every placement.

The following observation binds newly paid units through their original paired
action/result and exact native complete deployment marker. Group receipts expose
their three child transactions. Atomic evidence must name owner `hardening`,
the exact world/catalog/plan/scope/post/unit/actor, and paid gun1 plus twenty full
yellow magazines/200 rounds. Generic reused builds and zero-cost receipt replays
cannot establish a new payment. Verified original payment evidence is cached
before ledger rotation and retains its original actor; actor succession does not
turn an old actor's purchase into a new one.

Birth delivery is deduplicated by deployment ID. Combat may consume the twenty
delivered magazines before observation without losing delivery provenance.
Healthy bound clusters return `None`, allowing Watch's normal refill, paid wall
upkeep and ordinary repair to continue. Incomplete atomic intentions still block
uncertain repayment. This controller makes no combat-victory claim.

Focused tests cover all-three group restoration, funding both missing posts
before a single deployment, healthy fast paths, missing paid provenance,
world/phase guards, original-actor retention, native marker rejection, group
children and consumed-delivery deduplication. No live deployment was executed by
the implementation agent.
