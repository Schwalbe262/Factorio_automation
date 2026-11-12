# Mutual turret bastions

`DefenseHardening(game, bootstrap, builder, catalog, perimeter)` exposes read-only
`prepare(obs, parameters=None)`, ordinary action-producing `step` and native
`proof`. It writes its own checkpoint only when the parent writer calls `step`.
No coordinate overrides or enemy health/damage operations are accepted.

The first deployment selects a known post with actual loss history or observed
damage. Nearby native enemy pressure and repeated losses select two or three
new gun posts. The finite survey considers integer sites within fourteen tiles,
then at most forty-eight native-legal candidates and 4,096 combinations. It
rejects resource tiles, every production reservation/future footprint/typed-port
apron, inaccessible sites and enemy turret attack range plus two tiles.

Each new gun has an eight-wall arc across its front and sides. Its rear two-tile
approach stays open for service. All new guns mutually cover each other; every
wall's complete footprint and the old hotspot must lie within two independent
gun disks using native range minus two. A native walk-grid check includes every
prospective wall and turret before declaring rear service access. Whole-site
ordinary preflight runs before acceptance.

The controller registers a separate board block and a coverage scope referencing
only its own new posts. It preserves existing posts, producer plans and Engine
scope references. Before deployment it normally pre-pays all missing turrets and
their ammunition. A fresh three-post deployment reserves sixty full magazines;
the provider accepts targets through100 while each exact-slot transfer stays
at most50. Each turret's initial twenty-magazine target is tracked from actual
successful guarded-transfer receipts for that exact unit. Firing between
observations cannot erase delivered stock or keep the controller chasing an
instantaneous20.

During combat, three exact-owned functional guns with at least one full normal
magazine each can proceed to paid walls even while their initial reserve is
being replenished. This does not commission the job. Final success still
requires at least ten full magazines per gun and positive health; actual damage
is reported. The missing old hotspot does not prevent new protection. After
verified commissioning, five full magazines remain the ongoing upkeep floor.

Wall research must occur naturally. The installed wall recipe is checked for
five ordinary stone bricks per wall. Three bastions require three paid turrets,
sixty initial full magazines and twenty-four walls (120 normally produced
bricks). Normal builder/bootstrap collection and crafting pay these costs; no
items, ammunition, health or research progress are granted. The parent's existing
ammo provider and guarded loader handle all ammunition transfers.

Every new gun requires an exact successful normal-build receipt. A known lost
gun can be rebuilt normally, or follow a registered Watch replacement only with
both original/replacement paid-build receipts and fresh native identity proof.
Its original unit and receipt remain in loss history before the binding changes.
An unknown replacement is rejected. Proof verifies exact owned normal-quality units, intact
walls, functional guns and normal ammunition, plus the declared geometry and
wall-aware service access. The result certifies current geometric protection,
not victory in combat or destruction of nests. Normal maintenance of later lost
posts remains a parent Watch responsibility. `upkeep(obs)` can normally rebuild
one exact lost wall after recording its old unit and confirming native absence;
rebinding requires the successful paid-build receipt. For damaged bound walls,
the parent may assign `wall_repair(obs, spec, expected_unit)` to its ordinary
engine repair action provider. No health values are set by this module.

Focused tests cover rotated wall arcs, mutual complete-footprint
coverage, adaptive costs, read-only preparation, research/resource/reservation
gates, immediate per-post arming before further construction, ownership changes,
and executable flattened Lua checks for ammunition, damaged functional guns and
foreign-wall rejection.

Read-only native preparation passed at tick3,339,458 in4.907seconds. The observed
plastic hotspot had fifteen recorded lost units. Candidate turret centers were
`(173,-34)`, `(169,-36)` and `(177,-36)`, with twenty-four walls and reserved area
`[[168,-38],[179,-32]]`. Whole native resource/placement preflight, mutual
coverage and prospective wall-aware rear service access passed. This
world-specific preview is not live construction or combat evidence.

The first live gun9478 was destroyed by tick3,349,965. It received21 magazines,
but repeated observation-based top-ups delayed its support guns. The corrected
read-only recovery preview passed at tick3,411,141: original paid build9478 at
3,349,442 and registered Watch replacement10331 at3,403,573 had exact successful
receipts. Native preview showed three owned current guns with7/5/0 full
magazines. Recovery, refill and wall-building actions remain ordinary parent
writer operations; this preview did not mutate their state.
