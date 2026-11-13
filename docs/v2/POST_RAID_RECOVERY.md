# Post-raid repair and recovery

`PostRaidRecovery(game, builder, catalog, *, sweep, perimeter=None)` is a reusable
continuation for a verified, normally paid `nest-sweep.json` wave. It performs no
offensive rebuild or new placement. Original sweep state, projectile-death
records, deployment receipts and PCB plan remain unchanged.

`prepare(obs)` is read-only. `step(obs)` emits ordinary funding, movement, native
repair, repair cleanup, or one `recover_raid_post` action; otherwise it returns
`None` with `last_diagnostic`. `proof(obs)` requires every paid gun to be either
normally recovered or an exact recorded combat loss. `victory_evidence(obs)`
retains historical native victory while separately reporting currently standing
guns, conserved recovery receipts and known losses. Parent sweep/Watch adapters
must use these explicit recovery facts instead of claiming historical guns are
still standing.

The guarded runtime entrypoint is
`execute_recover_raid_post(game, action)`. The action has `type`, `recovery_id`,
`post_name`, `expected_world_id`, `expected_actor_unit`,
`expected_catalog_fingerprint`, and an optional reason. It selects a fixed
registered operation in `post-raid-recovery.json`; callers cannot supply new
coordinates, units or files. The controller has no jobs/runtime wiring yet.

Before recovery, the controller verifies original exact projectile deaths,
complete same-tick paid wave/group markers, current unit identities, local enemy
exclusion and actual hostile-turret ranges. Repairs use the existing native
repair-pack/tool driver with its real durability and cleanup protocol. Damaged
guns are repaired first. Each mine requires actual full health, an armed exact
covering gun and continued armed paths from remaining raid guns to registered
base cover. New unrelated perimeter posts do not invalidate completed recovery
history. Changed protected cover or actor identity blocks pending physical work.

The native mining operation persists its exact BEFORE intent before ordinary
`a.mine_entity`. Conservative aggregate empty-slot capacity covers the recovered
gun and all full/partial magazines. It counts MAIN and character ammo explicitly,
preserving normal yellow/red magazine counts and actual remaining rounds.
Unrelated stack sorting is allowed without losing per-inventory item totals.
No ammo recreation, health setters, inventory grants or invented baselines are
used. A lost reply can finish only from its existing intent and matching actual
conservation evidence; unknown absence or changed inventory blocks. Ambiguous
effects return `post_raid_recovery_outcome_unknown`, which the parent runner must
retain as a durable uncertain intent.

Recovery history is append-only. Exact native combat losses recover zero items
and trigger no rebuilding. The original PCB reservation is retained; releasing
that historical site needs separate explicit ownership coordination. Actor
changes during an accepted recovery require an explicit continuation rather
than a silent checkpoint reset.

Validation: 13 executable flattened-Lua/unit tests pass, including 97 partial
red rounds, native ammo auto-equip, paid identity guards, full-health gating,
safe/covered repair, lost-reply replay and complete-wave history. Read-only
native preview 5179995 passed in 2.123 seconds: all ten original paid guns
15889–15898 remained, six damaged (including 15891 at 7.000016 HP), all locally
safe and covered, with two or three empty slots required per recovery. The
subsequent remaining-cover removal guard has unit-fixture coverage only. No
repair, mining or recovery checkpoint activation occurred. Work was frozen
when the user switched to a new unseen seed; the old world is preserved.
