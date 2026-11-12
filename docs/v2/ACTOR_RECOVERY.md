# Exact owner-character recovery

`inspect_actor_recovery(game)` is read-only and calls `game.query` directly.
It works when normal `game.observe` fails with `agent_dead`. It reports the
world/surface, pinned FactoryAutomaton player, old storage unit/pointer, any
still-live old character and the owner's current character, health and explicit
main/guns/ammo/armor/trash inventories. It also reports pending native repair
ownership; such a repair blocks recovery and is never cleared or retargeted.

The character search covers at most sixteen surfaces and128 characters. The
corpse survey deliberately has no force filter: character corpses can fail a
player-force query. It matches the pinned player index within64 tiles of a
stored last-known position, or within256 tiles of the force spawn, with a
thirty-three-result truncation flag. A matching corpse proves owner-death
evidence; it does not identify the dead character's unit. No matching corpse
leaves replacement versus death-without-retained-corpse uncertain.

`execute_actor_recovery(game, expected_world, expected_old_unit,
expected_new_unit)` is an explicit administrative helper. The caller must hold
the existing world writer lock and record the returned result in the ledger.
It requires an invalid old framework pointer, the exact old stored unit with
no live character of that unit anywhere in the bounded character survey, and
the same pinned, connected FactoryAutomaton owner already holding the exact
new character on the current player force/surface. A live old pointer, another
owner, changed successor, wrong world or pending native repair blocks.

The helper changes only `d.actor`, `d.crafting_actor_unit_number`, and an appended
`d.actor_successions` record keyed by world/owner/old/new units. The native audit
records both units, owner, world, force, surface and tick. An exact retry checks
that marker and the current binding, then returns the original marker unchanged.
Conflicting records block. Original actor receipts and inventory-conservation
evidence are retained with their original actor unit.

It never respawns a character, sets a controller, adds/transfers/discards items,
changes health, resets repair state, research, counters, speed or settings.
Old actor-bound relocation/repair receipts remain blocked until a separately
reviewed succession-aware validator accepts their specific use; rebinding the
actor does not rewrite their evidence or establish conserved inventory.

Read-only tick3505134 found old unit11 invalid, no live old character, and the
connected player1's new character10915 at spawn with health250/250. An unfiltered
survey found that player's corpse at(153,-60), death tick3474933. Native repair
was not pending. The new main inventory was empty, with a pistol and ten normal
magazines in the separate guns/ammo inventories. Those were observations of
existing engine state, not grants made by this helper.

Focused tests execute the flattened Lua and check corpse-force independence,
read-only inspection, exact owner/world/unit guards, pending-repair refusal,
unchanged inventory/health/controller/old receipts, append-only audit and
idempotent retries. No live recovery was executed during helper development.
