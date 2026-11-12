# Atomic paid bastion deployment

`bastion_deployment_action(game, obs, post_name, *, owner_kind='hardening')` is a read-only action emitter.
`execute_bastion_deployment(game, action)` handles
`type='build_and_arm_bastion_post'` in the ordinary assisted backend. Only the
parent writer executes the action. This module does not modify framework files.

`bastion_group_deployment_action(game, obs, *, owner_kind='nest_assault')`
emits `type='build_and_arm_bastion_group'`, `group_id` and `deployments`: exactly
three complete single-post envelopes in registered stage order for Hardening or
NestAssault-v1. With `owner_kind='nest_sweep'`, the same API emits the complete
registered committed wave of six through twelve posts. The trusted saved policy
and whole plan determine its size.
`execute_bastion_group(game, action)` performs **one native query**. A native loop
builds and arms every post at the same `game.tick`; there is no Python RPC
between posts. The whole group must be unbuilt and funded before its first payment.

The envelope binds world, actor, registered scope/post, integer position, whole
plan SHA256, ammunition tier, owner-policy revision and the exact current lost-unit generation. The deterministic
deployment ID normalizes integer-valued coordinates. The helper independently
reloads the whitelisted owner file, PCB and its registered perimeter, requiring identical plans, fixed
scope geometry and post access. A stale Hardening binding is accepted only as a
known ancestor in the registered lost-unit lineage, with the canonical current
unit cleared. An unrelated stale binding is rejected.

Owners are restricted to `hardening` (`defense-hardening.json`), `nest_assault`
(`nest-assault.json`) and `nest_sweep` (`nest-sweep.json`). Offensive owners use only their registered private
`perimeter` registry. Offensive posts are not added to the global Perimeter or
its automatic rebuilding loop. The caller cannot select an arbitrary file,
coordinates, native script or under-fire policy.

Only the exact authorized Hardening block is excluded from foreign keepouts.
All other production reservations, shared bus bands, prospective entities and
buried spans, mining reserves and fixed typed-port aprons remain protected.
Native guards recheck resource absence, the entire turret placement, collision
tiles, enemy turret range plus two tiles, exact actor and current world.

Hardening retains its enemy-turret exclusion. Only the trusted NestAssault module
can supply the fixed `controlled_projectile_assault_v1` guard: one registered
stage of three posts, at most twelve exact targets, a 180-second native deadline,
and no further launch after two recorded post losses. Each proposed post needs
two distinct exact current rear guns at least half healthy, each holding five
full normal magazines. Rear witnesses retain their own ammunition tier. Native
target identities, reviewed worm ranges, current force modifiers and the native
death journal must agree. A bounded scan derived from installed turret prototype
ranges rejects new or changed covering worms. The paid posts receive ordinary
worm-first turret UI priorities; the game fires their projectiles. This is an
explicitly authorized material-risk policy, with no zero-loss or acid/DOT guarantee.

NestSweep uses a separate `committed_projectile_wave_v2` policy. Its complete
wave needs two current, exact, armed rear anchors. Every planned gun must have
two planned neighbors within native range minus two, and the formation must
remain connected to both anchors after removal of any single planned gun.
Every exact target, including each spawner, must be in native firing range of
at least two planned guns. Trusted module guards recompute this whole graph,
target coverage, native death-listener ownership, enemy identities and current
force profile before payment. Unlike v1, each front gun can rely on the coherent
planned formation rather than being directly within range of both original
rear anchors. Worm hits and ordinary losses are explicitly accepted; this is
geometric support and firing coverage, not a zero-loss guarantee.

For a wave of N guns, all N placements and N plain turret items plus 20N full
normal piercing magazines must pass before the first debit. A durable native
operation lock binds exactly one group identity before deployment. The group
receipt records N paid turrets, 20N magazines and 200N rounds in one native tick.
The external single-post action rejects NestSweep. A front-line loss never
authorizes another group or automatic rebuilding. The legacy two-loss staged
launch limit is not applied to this already committed wave; its controller
observes the surviving guns and finite native deadline honestly.

The operation requires one plain normal full-health turret item and twenty full
normal magazines in MAIN inventory. It reuses the normal/metadata predicates
from `ammo_transfer`. Full magazines behind a stack's first partial magazine may
be consumed; that partial magazine and its remaining rounds stay unchanged.
Sources are selected again after each count change, allowing ordinary inventory
sorting to move slots. No character ammo, chests, fuel or resource inventories
fund the operation.

The immutable `ammo_item` is restricted to `firearm-magazine` or
`piercing-rounds-magazine`. Hardening defaults to yellow. NestAssault's saved
stage, policy and private registered posts must agree on the selected tier.
Native prototypes must report ammunition type, bullet category, magazine size10
and adequate stack capacity; piercing magazines also require the force's normal
recipe to be enabled. Receipts record the actual selected item, category, size
and round counts. Other ammunition tiers and partial magazines remain untouched.

A native intent is persisted in `d.defense_deployments[deployment_id]` before
payment. The normal paid build consumes exactly one eligible turret item. Its
native unit is recorded immediately, then twenty full magazines are transferred
through the same native insert/count conservation pattern as the existing ammo
collector. The completed receipt records gun1, magazines20, rounds200, actual
actor before/after totals, world/catalog/plan/scope identity and equal start/armed
ticks. No ammo, health, research or enemy damage fields are assigned.

A completed receipt can be replayed only for its exact surviving owned unit.
It reports `reused_receipt=True`, `paid_this_call=0` and `loaded_this_call=0`;
the original transaction still records `paid_turrets=1` and
`loaded_magazines=20`. An unmarked pre-existing turret is always rejected.
An incomplete intent blocks repayment and adoption. An unexpected native
exception may leave a paid, known or ambiguously created turret pending recovery;
the macro does not erase the structure or guess that resources should be granted
back. A failed create with confirmed absence refunds only the paid turret and
records that known failure.

The group checks all three trusted contexts, all three fresh native positions,
native policies and aggregate MAIN stock of three plain turrets and sixty full
magazines before creating a group intent or spending anything. Group intents
live in `d.defense_deployment_groups[group_id]`. Each successful native child
receipt is appended immediately to a bounded three-element prefix. Fresh success
records gun3, magazines60, rounds600, actual aggregate conservation and identical
start/armed ticks. Completed retries validate all exact surviving paid units and
report zero new payment. An unexpected failure preserves the prefix and any
child intent and blocks automatic replay; it does not claim atomic rollback or
adopt an unknown entity. Ordinary recovery requires a separate reviewed path.

Parent integration must lazily intercept this action type and retain ordinary
action logging. A managed Perimeter callback can prepay gun1/magazines20 and use
the emitter after normal known-loss binding has been cleared. Hardening's paid
build and stocking reconciliation must recognize the exact marker-backed atomic
receipt, verify its world/actor/plan/unit and deduplicate by deployment ID.
Generic `result.reused=True` build results remain ineligible as payment evidence.
No existing controller is opted into the macro by this new module.

Forty focused tests execute flattened Lua for costs, same-tick loading,
round conservation, partial magazines, inventory sorting, marker retries,
interrupted creation, aggregate prepayment, all-position-before-payment guards,
same-query three-post deployment, piercing recipe/category checks, private
assault registration and metadata/world/resource/worm/foreign-future negatives.
Committed-wave fixtures execute six, ten and twelve-gun groups, reject a late
blocked site before any payment, reject 199 full red magazines for ten guns,
require two-gun spawner coverage and robust rear connectivity, reject a second
wave or unowned death listener, and preserve an interrupted six-post prefix.
Read-only native validation at tick3,550,851 confirmed the real registered
context and actor10915. The installed2.1.20 prototype returned one turret ammo
slot, magazine size10 and stack size100. No real build-and-arm action was run by
this agent. The official [prototype inventory API](https://lua-api.factorio.com/latest/classes/LuaEntityPrototype.html#get_inventory_size)
documents the read-only capacity method; installed-version results above are
the authority for this implementation. Ammunition category and magazine size are
read through the official [item prototype API](https://lua-api.factorio.com/latest/classes/LuaItemPrototype.html#ammo_category).
Native macro execution and combat outcomes remain unverified by this agent;
the executable Lua fixtures establish operation and failure semantics.

Later parent acceptance inspected two real completed Hardening group receipts:
native3930174 and3946305 each paid three guns and sixty full magazines with
identical start/armed tick and `same_tick=true`. These were ordinary maintenance
deployments in the development world. The ten-gun offensive wave remains pending
its own native deployment and target-death proof.
