# Nearby threat survey

`NearbyThreats(game,catalog).survey(obs,maximum_distance=96,maximum_entities=128,
danger_distance=48)` is read-only. `prepare(obs)` adds a bounded assault proposal;
it deliberately returns `ready_for_attack=False` and emits no gameplay action.
`preflight_bastions(obs,stages,backstop_position=...)` reviews up to nine proposed
positions, their 72 optional walls, local service and native turret-priority
read support. Its `ok` means the observation executed, not attack admission.

Distances use only registered, native-confirmed productive buildings: assemblers,
furnaces, labs, miners and power/fluid producers. New guns, walls, poles and depots
never move this reference. World, Nauvis surface, forces and unit identities are
included. At most256 productive references,256 candidate chunks and128 enemies
are considered. Each chunk is tested with `is_chunk_charted` **before** its
bounded entity query; no chart/reveal API is called. Truncation blocks a complete
attack proposal. The primary stationary cluster is separate from remote oil
threats, and an oversized cluster is explicit.

Native data includes actual gun/worm range, cooldown, health, resistance, normal
magazine size/damage, current force damage/speed upgrades, actor main/equipped
full magazines and partial rounds. Native `resistance.percent` is a fraction:
`.15` means15%. The physical damage calculation is a conservative static bound;
flat armor at or above raw damage yields an unresolved/zero lower bound rather
than an invented damage guarantee. Healing, streams/ground damage, movement,
spawn rate and target acquisition are not a simulated battle.

Read-only native snapshot3419819 took0.754s:93 productive references,171 candidate
chunks/151 charted,69 enemies,28 friendly guns, no truncation. The nearest eastern
worm5569 was34.481 tiles from Plastic's plant(170.5,-43.5). Small worms had200HP
and range25; guns range18/cooldown6 and no electric requirement. Spawners5571,
5561,5611,5578,5552 had579.001HP, physical flat2 and fraction.15. Native tier-one
ammo/turret bonuses gave6.05 raw physical damage and3.4425 after that armor:
about169 rounds per snapshot spawner without healing. Four worms add about136
rounds;120 ordinary magazines leave little margin for mobile waves or healing.
The survey also found48 small biters and one medium biter across its full area.

Nine-site review3516393 took0.723s. At that instant no healthy eastern support
guns remained; rear coverage was zero. Only some candidate footprints were
currently clear, full prospective service failed at(195,-49), and stage-three
positions collided with live worm5609/spawner5561. Those positions are
conditional after real deaths, never currently buildable. Spawner5561's health
had grown to586.160. Parent later restored the three rear guns3531783; that newer
snapshot does not retroactively validate the rejected candidate geometry.
Mutual support now uses the native gun range minus2 (16 currently), not18.

Installed local `doc-html/runtime-api.json` is exactly2.1.20. It exposes the normal
turret UI controls `set_priority_target(index,EntityID)`, `get_priority_target`,
`priority_targets`, and `ignore_unprioritised_targets`. A future guarded assault
can prioritize worms while keeping unlisted-target defense enabled. This survey
calls no setter. The writable `shooting_target` is deliberately unnecessary.

A proposed finite operation has at most three three-gun stages, two spare guns,
20 initial full magazines per gun,72 walls,one carried repair pack,180 game seconds and
two allowed post losses. These are proposal bounds, not a success prediction.
Actual site/damage/stock admission and a separate reviewed normal-cost executor
are required. Live enemies cannot be mined or scripted away. Exact native death
and own projectile-cause receipts are required for framework kill claims;
disappearance alone and unrelated/human kills are insufficient.

`NestAssault` is the separate bounded ordinary-projectile executor. Its smaller
initial prepayment is eleven guns,200 magazines and72 walls. It prefers researched
piercing ammunition and collects missing ordinary ingredients before launch;
its saved report states an explicit yellow fallback if the native recipe is
unavailable. Friendly-gun
observations include separate full/partial counts for both tiers, so an armed
red-ammo rear post is not mistaken for an empty yellow post.

Nine focused tests pass, including executable actual Lua for chart filtering,
world/unit guards, native ranges, fractional armor, force modifiers, partial
magazine conservation in observation, hostile-force handling and target caps.
No game/runtime-state writes or attack actions were performed by this module.
