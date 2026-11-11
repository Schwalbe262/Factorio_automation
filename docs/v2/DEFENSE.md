# Strategic gun posts

The user manually repelled the attack that damaged the development factory.
The framework subsequently repaired its furnace district and commissioned
eight proactive gun posts through Luna-selected ordinary jobs. The prior
battle is human intervention, not framework combat success.

| Sector | Native position | Initial magazines |
| --- | --- | ---: |
| West smelter | (-50,-26) | 10 |
| North smelter | (-32,-43) | 10 |
| South coal | (-55,32) | 10 |
| Middle bus | (-20,-66) | 10 |
| North iron | (15,-85) | 10 |
| South steam | (43,18) | 10 |
| North labs | (32,-127) | 10 |
| East compact green | (77,-69) | 10 |

Preparation surveys native turret range18, terrain, resources and factory/bus
reservations before choosing each post within eight tiles of its sector anchor.
These protect selected approach points; they do not form continuous coverage
of the entire factory perimeter. Fixed sectors must be extended as the factory
grows. There are no radar-derived attack forecasts or walls in this version.

`luna-defense-perimeter-20261112` succeeded at tick1477979 with68actions and
zero consecutive failures. All eight exact-unit posts had400/400health and
100normal rounds each; no enemy was within their inspected36tile radius.
That is armed-post commissioning evidence, not a combat stress test.

Every subsequent job runs bounded post upkeep. Below five magazines, a known
live post is topped up toward ten. Turrets and magazines are normally crafted
and paid; no bullets or inventory are granted. The identity/world-guarded loader
transfers only full normal magazines from exact actor inventory stacks and
preserves a partially used source magazine. Changed identities block rather
than adopting an unknown replacement.

Upkeep runs while a job is executing; it is not an idle background service.
Missing posts are reported and can be rebuilt by `defense_perimeter`, but
upkeep does not rebuild them. Damaged health is observable; automatic repair,
continuous ammunition assemblers and belt-fed supply remain unfinished. Those
are required before claiming unattended long-term defense.
