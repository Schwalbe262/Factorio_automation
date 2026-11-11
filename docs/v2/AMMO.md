# Automatic magazine supply

`AmmoProduction(game, builder, catalog, perimeter, *, bus_factory=None,
science=None, green=None, tap=None)` exposes read-only `prepare(obs)`, ordinary
paid `step(obs)`, current `proof(obs)` and `ensure_full_magazines(obs, count)`.
Assign `perimeter.ammo_source = ammo_controller` after construction to enable
factory refills without a constructor import cycle.

The compact core reserves6x7 tiles and uses one AM1, one fast input inserter,
one normal output inserter, four local input belts, two local poles and a wooden
output chest. Catalog speed0.5 and the one-second, four-iron recipe give
**30 magazines/minute and120 iron/minute nominal**. The fast input meets the
existing conservative120items/min budget; a normal input would not. These are
catalog limits, not measured sustained production.

The source is one normally paid **critical** iron main-bus tap, selected through
the existing bounded free-station helper and BusTap cutover guards. Preparation
tries compact service bays relative to the existing bus/industrial anchors,
keeps a bounded mix of near and northern station rows, and proves the complete
tap, adapter, machine footprint, power and defense before publishing a plan.
Resources and other reservations remain keepouts. Two empty service tiles
beside the chest are prospective blockers for power planning. Defense must
commission before the first producer build, and the whole site is checked
again after any added turrets.

The chest is configured to one native inventory slot before producer building
continues. Its catalog stack size is100 magazines in the installed world;
ordinary assembler, arm and input-belt buffers are additional finite buffers.
The factory job needs one real full magazine in the chest to finish; it never
waits for a full buffer. A blocked chest naturally stops further production.
During replenishment the critical tap can reduce plates available to lower
priority consumers; it does not create smelting capacity or promise concurrent
full science throughput.

Public proof requires every saved layout unit, native underground partners,
powered AM/inserters, the real magazine recipe and products counter, native
input/output pickup/drop geometry, the exact typed iron source and native
critical splitter settings, a barred normal-magazine chest and current defense
coverage. Destroyed or changed known units block instead of being adopted.

Before factory commissioning, perimeter deployment retains its ordinary
bootstrap crafting fallback. Once the factory's saved phase is `verified`,
all perimeter acquisition paths use its exact output chest and never fall
back to handcrafting. An empty chest waits; a changed chest blocks. Existing
turret loading still uses the normal world/unit-guarded loader. Refills inspect
the commissioned chest directly rather than requiring global coverage proof:
otherwise the low-ammo post requesting a refill would block its own source.
The public factory proof continues to require defense coverage.

`ammo_transfer` supplies the two exact runtime actions:

- `take_full_magazines`: exact chest/world/actor identities,1..50 magazines
  globally and up to16 unique bounded source-slot indices.
- `configure_ammo_buffer`: the same identities and exactly one available slot.

The native transfer validates all selected slots before moving anything,
accepts only normal firearm-magazine stacks with supported metadata, and takes
only their full-magazine portion. It inserts within actor capacity and reduces
the exact source slot by the inserted count. A partially used first magazine
stays in place; count and bullet-round totals must be conserved. Normal ammo
has an underlying LuaItem in installed2.1.20, so its presence is not treated as
unsupported metadata. Tag/inventory/entity-data traits, damaged/spoiling stacks
and different qualities are rejected. The actions never remove a first name
match from an inventory or refill a partial magazine.

Read-only native prepare at tick2404744 passed in9.00seconds: production area
`[[18,-130],[24,-123]]`,34 producer/adapter entities and a21-tile,2-turn direct
input route. The critical station was y=-113.5. The existing laboratory post
already covered the whole core, so no additional turret or bootstrap magazines
were needed at that snapshot. No live factory construction, ammo transfer or
combat test was performed by the implementation worker.
