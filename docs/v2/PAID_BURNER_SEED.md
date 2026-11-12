# Paid burner startup allowance

V2 power startup uses `PaidBurnerSeed.prepare/step` and the normally paid
`seed_paid_burner` runtime action. It grants no coal: at most eight normal coal
items are transferred from exact actor inventory slots to one registered power
burner. Character reach still applies in the character backend.

`paid-burner-seeds.json` is independent of the builder's old logical `seeds`
dictionary. It binds world, catalog, native unit, original position/direction,
high-water tick and the unique paid non-reused build receipt. It never resets
on world/catalog changes or rollback. The old logical observed flag is unchanged.

Legacy adoption streams bounded paired job intents/results, not unbound action
logs. A previously fueled same birth stays closed. A replacement birth after a
proved fueled predecessor gets its own single eight-coal allowance. A reused
build, ambiguous birth, unknown outcome or missing evidence grants no allowance.
Any observed running burner is durably closed to future fresh-start seeding.

Known partial receipts expose only the original remaining allowance. Native
storage and Python checkpoints both retain cumulative spend. The same native
query guards world/unit/force/surface/position/direction and fuel inventory before
exact normal-slot debit. Unknown exceptions or malformed receipts preserve the
intent and require CLI reconciliation; they are never automatically replayed.
Same-unit cold starvation reports that continuous fuel recovery is required.

Read-only current-world inspection at tick749703 found dedicated drill1287 at
(-35,38), with five coal and positive burning fuel. Its birth receipt was a normal
paid build at499443 and the independent audit proved replacement. The running
framework had already delivered exactly eight coal and closed this allowance.
The older coal8 action at635076 belonged to a different starter drill(-42,32).
The inspection called no action, step, save or process control.

Focused tests cover legacy replacement, same-unit closure, partial acknowledgments,
world/catalog/rollback guards, unknown non-replay and executable Lua normal-coal
conservation, quality preservation, native identity/reach checks and the eight cap.
