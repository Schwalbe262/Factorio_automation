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
| Quiet, no approach within 64 tiles, no attack source, evolution < `QUIET_EVOLUTION` (0.1) | 0 | - |
| Quiet, evolution >= 0.1 | 0 early / 1 mid / 2 advanced tier | 10 / 5 |
| Approaching units/nests within 64 tiles, or an attack expected (`attack_sources`) | 2 | 10 / 5 |
| Units within 40, close threats, damage under pressure, or stronger evolution nearby | 3 | 20 / 10 |
| Strong unit within 24 or severe damage under pressure | 4 | 20 / 10 |

Attack expected (`attack_sources`, since all-1114-0107-1075833632: its
client-free actor died at the spawn at 30.7 game min, evolution 0.007, every
survey "quiet, no enemy within 96 tiles of a producer", no turret and no
gun-turret research): an attack group forms at a nest the pollution cloud
reaches, wherever it is, so the survey also lists, around the force spawn and
in charted chunks only, every spawner within `ATTACK_SOURCE_RADIUS` (384) whose
position has pollution > 0 (`polluted_spawner`) and every enemy unit within
`GROUP_RADIUS` (192) farther than `GROUP_NEST_RADIUS` (24) from any nest
(`moving_group`: an attack or expansion party). Any such source, or a truncated
source list, makes the risk at least `approaching` (2 posts, sited toward the
sources when nothing is within 96 tiles; `sizing` "attack expected: ...").
That level also pulls `research_gun_turret` and then `defense_perimeter` ahead
of their roadmap `after` steps (`progression.defense_urgency_nodes`,
`URGENT_DEFENSE_PRIORITY`, one node at a time, same node ids as the roadmap).

A quiet low-evolution map buys no turret: `defense_perimeter` succeeds with
reason `quiet map, no posts required` and the risk's `sizing` line in its
evidence, so every capability that `requires` it is admitted. The tier
reserve (mid 1, advanced 2) applies only once evolution reaches 0.1. Native
technologies determine the tier; catalog recipe presence cannot unlock weapons.

## Planned world (site service) and growth

On a world with `site-plan.json` for its world id (`site_plan_areas`), every
zone (bus band, smelting, power, mining, outposts, rail stations), district
strip (growth slots), corridor and allocation with its tap band is planned
land. A new post's 2x2 footprint must stay off it (`outside_plan`), in
addition to the board reservations and port exits (`protected_factory_areas`).
Focus: the producers ranked nearest to the threat (damaged first), up to
`FOCUS_TRIES` (8) tried in order. Site: among legal native candidates that
fully cover the focus, the one nearest the threat (threatened side), then
nearest the focus. Only if no strict site exists does the zone or strip that
hosts the focus allow a post on its free land (`placement: relaxed`); bus,
rail and corridor land never. Coverage scopes (`prepare_coverage`) use the
same strict-then-relaxed filter. The fixed `POSTS` / `FUTURE_AREAS` layout is
used only on a world without a site plan; `_legacy_prepare` refuses a planned world.

Growth: other jobs' background step (`step(background=True)`) builds new posts
only at urgent risk (`URGENT_RISK_LEVEL` = 2: units within 40, damage under
pressure); a lower risk that wants more posts than the policy holds only
refills known posts. The observe receipt `defense_growth`
(`growth_receipt`) then asks `progression.defense_growth_nodes` for one
low-priority `defense_growth` job (`{"posts": N}`), which runs the same rule.
Ammunition: hand-crafted magazines until `ammo_production` is commissioned,
then its stock (`ammo_source`). `post_ammo_transfer.py` (yellow-to-red swap)
stays unwired: it needs its own paid action type. The current loader keeps
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
