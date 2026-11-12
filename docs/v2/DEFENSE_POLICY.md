# Adaptive initial defense

`DefensePerimeter` now derives initial targets from native current risk instead
of requiring the eight historical mature-world posts. Its public `prepare`,
`step`, `upkeep` and `proof` methods keep the same signatures. No new config knob,
research unlock, enemy setter or free item path is added.

The bounded read-only survey uses current owned producer entities, charted
enemy units/spawners/worms within 96 tiles, native unit health and attack range,
producer damage, force evolution, actual researched technologies and powered
ammunition machines with native production. Provider observation is not a
sustained supply certificate. The survey includes
at most 128 producers and 128 threats, records truncation, and refreshes after
producer/health/technology changes or 2–10 game seconds. New forward defensive
posts do not become producer references or expand the threat horizon themselves.

| Native conditions | Active targets | Initial magazines / refill below |
| --- | ---: | ---: |
| Quiet early factory, no approach within 64 tiles | 1 | 10 / 5 |
| Approaching units/nests within 64 tiles | 2 | 10 / 5 |
| Units within 40, close threats, damage under pressure, or stronger evolution nearby | 3 | 20 / 10 |
| Strong unit within 24 or severe damage under pressure | 4 | 20 / 10 |

A quiet advanced factory has two targets. Native technologies determine this
phase; catalog recipe presence cannot unlock weapons. The current loader keeps
normally produced firearm magazines. A worm outranging the gun is reported as
a firepower gap rather than treated as a promised gun victory. Old damage alone
without nearby pressure does not cause a new high-risk building campaign.

Quiet policy prefers already paid guards and reports the producer units they
actually cover and leave uncovered. It can reuse a paid reserve without claiming
full factory coverage. Fresh required sites come from actual producer clusters
and the existing native defense coverage survey: resource-free legal placement,
registered production reservations, service access and full focus-cell gun range.
Known worm firing envelopes are excluded. This is defense, not scripted assault.

All existing paid posts, units, positions, lost-unit history and legacy `armed`
phase remain. New required guards append normally; old unbuilt future posts can
be metadata-deferred. Accepted unbuilt targets are reused across steps, not added
again for every material wait. Higher-risk ammunition gets a native initial-load
receipt; subsequent calls use the refill floor rather than topping up every shot.

Proof covers only the current adaptive target set and its actual armed native
units. It includes risk facts, survey tick, retained-post count and uncovered
producers. It is not whole-factory coverage, sustained-ammo proof or a combat
guarantee. Factory-specific `ensure_coverage` scopes still prove their own actual
geometry separately. Historical paid guards continue ordinary upkeep.

After an already approved defense goal, background dispatch rechecks adaptive
risk even when a historical perimeter is armed. It yields to production as soon
as the current target is met. An unavailable native risk survey authorizes no new
posts and no success proof; explicit defense blocks with evidence, while
background records a diagnostic, retains known-post upkeep and lets ordinary
production proceed. The waiting-only fuel prerequisite fallback remains active.

Read-only current-world check at tick 532079 passed in 0.640 s: evolution
0.0388038, nearest unit 86.31 tiles, nearest spawner 89.36, strongest observed
unit 15 HP, 21 producers and no current damage. The one-target quiet proof passed
with all eight paid registered posts retained, zero additional posts, and the
checkpoint bytes unchanged. This is not a fresh-seed adaptive construction test.
