# Conserved recovery of owned belongings

`inspect_corpse_recovery(game, expected_world, expected_actor_unit, death_tick)`
is read-only. `execute_corpse_recovery` accepts the same positional arguments;
its caller holds the world writer lock. Neither API accepts corpse coordinates.
The CLI enables `keep_single_repair_tool=True`: surplus repair packs remain in
the owned corpse while other goods are recovered, preserving the native repair
driver's one-carried-tool limit. This returns waiting with exact remaining
goods. Ordinary actor movement between separate attempts is allowed; each
individual native transfer still conserves goods without moving the actor.
The current character must be the exact pinned, connected FactoryAutomaton
owner's character on the expected world, player force and Nauvis surface.

Selection finds exactly one character corpse with that player's index and the
exact death tick within256 tiles of force spawn. There is no corpse force
filter: neutral corpses still belong to their recorded player. A truncated
sixty-five-result survey, ambiguous match or missing unfinished corpse blocks.
The native corpse may have no unit number, so its player/death/surface/position
tuple is pinned and revalidated on retries. Corpse inventories above512 slots
are refused before transfer.

Execution uses only `actor.get_main_inventory().transfer_from_stack(stack)` on
the actual native corpse stack. It never reconstructs an item from a name/count
specification. Native stack transfer retains quality, partial ammunition,
repair/tool durability and attached item metadata. Actor position is pinned on
each attempt and checked before/after that transfer; no movement, teleport or
corpse journey is emitted. If a caller requires the actor at spawn(0,0), it must
ensure that through ordinary movement before invoking the helper.

Before and after each attempt, conservation covers corpse plus explicit actor
main/guns/ammo/armor/trash inventories and cursor. Numeric inventory indices are
deduplicated. Receipts retain all-quality item counts, normal item counts,
ammunition rounds and tool durability. Counts/rounds compare exactly; durability
allows at most0.000001 rounding difference. Existing repair/crafting activity
blocks the operation. The helper changes no health, crafting, research, speed,
settings or other actors' possessions, and never explicitly destroys a corpse.

`d.corpse_recoveries` stores an exact world/owner/death marker and a receipt prefix
written before native transfer. Interrupted prefixes must prove conserved goods
before resuming from actual remaining stacks. A failed conservation proof freezes
that record for review. At most512 attempt receipts are accepted. A full main
inventory returns `waiting` with exact remaining items/stacks; the actor may
offload goods normally between completed attempts, while the corpse's recorded
remaining goods and pinned actor position must stay unchanged.

Completed retries transfer zero additional items and return the stored total.
They also tolerate the engine having removed an empty corpse. A pending prefix
that already moved everything can be verified without recreating the corpse.
`transferred_items` is this invocation's new transfer; `total_transferred_items`
is the cumulative verified total. Original actor/death/relocation receipts are
never rewritten. This helper does not certify those older jobs' conservation.

Read-only tick3567065 found one player1/death3474933 corpse at(153,-60), with
63 occupied slots and4104 items, including30 ammunition items/300 rounds and
one tool with total durability60. The current actor10915 was at(134,-94), not
spawn, with79 empty main slots. These are observations, not transferred items;
no live corpse recovery ran during development.

Focused executable Lua tests cover neutral-corpse selection, direct metadata
preservation, partial/full inventories, interrupted prefixes, empty-corpse
idempotency, exact ownership/location guards and deliberate conservation faults.
