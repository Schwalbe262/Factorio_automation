# Persistent factory ammunition upgrades

`DefenseAmmoPolicy(game,catalog,perimeter,piercing)` exposes read-only
`prepare(obs)`, sole-writer `step(obs)`, `preferred_item(post_name)` and
`last_diagnostic`. `step` emits ordinary factory collection or one
`upgrade_post_ammunition` action; `None` leaves normal defensive upkeep running.
It never crafts ammunition or claims spare steel supply.

The policy selects the three commissioned Hardening posts first, followed by
actual nearby enemy counts and damage. Conversion requires two distinct exact
registered supporting posts within native gun range minus two, each at least
half healthy and carrying a full normal bullet magazine. Both yellow and red
support are accepted. Existing gun geometry, unit ownership, scopes and paid
birth receipts remain unchanged. Unsupported isolated posts remain yellow and
are reported, rather than weakening the support requirement.

Each conversion prepays ten full normal red magazines. Active unfired Sweep or
Assault requirements are reserved before collection/payment; completed,
stopped and already firing operations impose no permanent 200-magazine reserve.
After conversion, a post is topped up toward ten full red magazines only below
five. The output collector already limits each actual factory take to fifty.
An unavailable factory or an empty output does not block ordinary fallback
upkeep. Current factory output is capped and its potential is 40 red/minute,
requiring 160 iron, 40 copper and 20 steel/minute. Existing 7.5 steel/minute is
claimed by Engine: this is finite stock allocation, not a sustained global red
ammunition guarantee.

`defense-ammo-policy.json` holds desired per-post tiers and append-only
per-unit transactions/conversion receipts. It does not rewrite Perimeter or
Hardening state, original build receipts, completed raids or actor history.
An unexecuted transaction can be marked cancelled only after fresh native
absence of its effects marker; cancellation does not erase the old intent.
Unknown partial effects fail closed. Complete native receipts are reconciled
after restart even if normal firing has since consumed the loaded magazines.
An actor successor gets its own transaction, rather than inheriting payment.

The runtime hook is:

```python
if action['type'] == 'upgrade_post_ammunition':
    from .post_ammo_transfer import execute_upgrade_post_ammunition
    return self._record_action(action, execute_upgrade_post_ammunition(self, action))
```

The helper obtains the immutable intent from the policy file, verifies the
registered target/support identities and current active reservations, then
validates all native stock before effects. It creates a durable native intent
at `d.defense_ammo_upgrades[transaction_id]`. It swaps the complete actual
yellow stack into an existing empty MAIN slot using native `swap_stack`, then
loads prepaid full red tails. Yellow partial rounds and actor red partial
rounds are preserved. Normal firing between observation and execution may
decrease the old stack; additions/type/metadata changes reject. Exact per-item
counts and rounds across MAIN plus turret must balance in the same game tick.
No ammo count, health, quality, research or enemy setters are used. No gun is
mined or replaced. A complete marker replay cannot charge again; an incomplete
marker cannot silently replay. Character backend retains native reach checks.

Shared integration is deliberately pending root review. Enabling this controller
before changing yellow-only native proofs would create circular failures. The
minimal six-file continuation is:

1. `defense_perimeter.py`: inspect both tiers into explicit `by_item` fields;
   retain legacy actor yellow fields for existing callers. Count either tier as
   actual armed coverage. Refill the live tier (or accepted preference after
   conversion/loss) through explicit tier providers. Commissioning still needs
   the existing full-magazine thresholds. A desired preference alone is no proof
   that a yellow-to-red conversion happened.
2. `defense_hardening.py`: live proof accepts normal yellow or red using each
   native prototype's magazine size. Preserve historical yellow birth and
   delivery receipts and original actor IDs. Existing atomic birth receipt
   validation remains exact; conversion receipts are a separate ledger.
3. `bastion_maintenance.py`: consume tier-aware native armed evidence; retain
   the existing paid yellow atomic birth ABI until an explicit future birth
   policy is approved. A rebuilt post can then undergo a separate paid upgrade.
4. `radar_network.py`: its two exact registered covering guns may carry either
   supported tier; keep full-health/full-five and every existing geometry guard.
5. `defense_repairs.py`: armed repair protection accepts actual normal rounds
   of either tier; health/range/enemy/reach rules remain unchanged.
6. `nearby_threats.py`: ordinary current backstop evidence accepts either tier;
   retain per-item stock instead of presenting red as yellow. Existing Sweep
   and Assault already carry explicit backstop ammo types.

Root owns jobs/runtime wiring. Schedule urgent missing/empty post recovery
first, then a bounded red-factory raw-input upkeep turn and this policy before
routine yellow top-offs. Normal waits must not starve emergency yellow supply.
Keep `AMMO_GUARD_LUA` and its string interface unchanged. All single/group/wave
deployment envelopes and historical markers remain untouched.

Offline validation covers flattened executable Lua, partial 97-round yellow
recovery, red actor/post partials, support and identity failures, full-inventory
failure before payment, same-tick conservation, reserved raid stock, receipt
replay/uncertainty, restart reconciliation and normal upkeep on factory waits.
No gameplay execution or native installed-policy proof has been performed.
