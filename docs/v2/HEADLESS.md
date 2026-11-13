# Can the actor run without a connected client? (probe, 2026-11-13)

Short answer: **no, not from map creation.** Everything the actor does works
on a dedicated server with zero connected players except one thing the
roadmap needs: a hand craft by a character that no connected player controls
is not credited to the force. Its products are not counted in the item
production statistics and it does not fire `craft-item` research triggers.
`automation-science-pack` is triggered by "craft 1 lab" and a lab can only be
hand-crafted at that point (assemblers come with `automation`, which needs red
science), so a world that never had a connected player cannot reach red
science honestly. Factorio 2.1 has no API to create a player
(`game.create_player` does not exist; `LuaGameScript` only has
`players`/`connected_players`/`remove_offline_players`).

## Probe facts (Factorio 2.1.20 + Space Age, dedicated server, `auto_pause=false`)

Throwaway worlds `runtime/v2/headless-test-probe1` (seed 123456) and
`headless-test-probe2` (seed 654321), started with `runtime.create` +
`runtime.start` and no client. They used fixture items and fixture
research flags that a real run never uses.

| Capability | Without a connected player? | Evidence |
|---|---|---|
| Server keeps ticking with 0 players | yes | tick 1072 -> 21602 with `#game.players == 0` / `#connected_players == 0` |
| Character from `surface.create_entity` (`initialize`) | yes | `a.valid`, `a.player == nil`, `associated_player == nil` |
| `begin_crafting`, `crafting_queue`, real craft time, ingredients | yes | 2 gears: 4 plates removed at queue time, progress 1/30 per tick, done after 30 ticks each, `crafting_queue_size` 1 -> 0 |
| Hand craft counted in `get_item_production_statistics` | **no** | gear `get_input_count` delta 0 (player-less), 2 (same craft with a connected controlling player) |
| `craft-item` trigger by a hand craft (lab -> automation-science-pack, prerequisites researched) | **no** | player-less lab: `researched=false`, `saved_progress=0`; same craft with the client's player controlling the actor: `researched=true` |
| `craft-item` trigger by machines (steam-power 50 iron plates, electronics 10 copper plates in stone furnaces) | yes | both researched in a world that never had a player |
| Lab research | yes | `automation` progress 0.010 -> 0.061 over 300 ticks with 0 connected players |
| `mine_entity`, `can_reach_entity`, `teleport`, `walking_state` | yes | tree mined (+4 wood), 8.3 tiles walked in 62 ticks |
| `create_entity{..., raise_built=true, player=a.player}` | yes | built; `last_user` is nil instead of the player |
| Main/ammo/gun inventories | yes | 10 magazines in the ammo slot, pistol selected |
| Death | partly | `a.die()` leaves a `character-corpse` with the whole inventory (`character_corpse_player_index` 65536 = none); nothing respawns, so `recover-actor` (player successor) has no successor to bind |
| Offline player keeps the actor | **no** | killing the client "logs off" the controlled character: `d.actor` invalid, `p.character` nil, as the API documents for associated characters |
| Detach, then disconnect | yes | `p.set_controller{type=spectator}` while connected leaves the actor in the world; after the client is closed the actor stays valid and crafts (not credited, as above) |

Trigger techs on the Nauvis path are `steam-power`, `electronics` (furnace
plates), `automation-science-pack` (hand-crafted lab), `steel-axe` (furnace
steel), `oil-processing` and `uranium-processing` (`mine-entity`, done by
pumpjacks/drills). Only the lab needs a player.

Cost: the headless server alone measured 367 MB and 0.13 cores (fresh map,
zero players); the hidden client is ~690 MB and a GPU context
([README.md](README.md) "Fleet runs").

## Cheapest honest alternatives

1. **Client only for the lab craft (proven by the probe, not implemented).**
   Run player-less until the lab ingredients are in hand, connect a client,
   bind the actor (`BIND_CRAFTING_PLAYER_LUA`), craft the lab through the
   player, wait for `automation-science-pack`, then `set_controller` to
   spectator and close the client. Runs show the lab trigger at game tick
   15.6k-18.8k (4.3-5.2 min, pace1 fleet). One client process could visit the
   maps in turn. Every check that pins the player must then accept a
   detached actor: `supervisor.actor_binding` (owner index),
   `actor_recovery`, `corpse_recovery`, `nearby_threats` (controller type),
   `defense_relocation` (`owner.connected`), `deterministic_repair_control`,
   `post_raid_recovery`, `_HELPERS` re-binding in `deterministic_game`.
   Death after the detach needs its own rule (a new bare character at the
   spawn, like a respawn), and later hand crafts are no longer in the
   production statistics (the framework reads only plate/science counters,
   which machines make).
2. **Linux node without a display:** a Linux Factorio client under a virtual
   X server (Xvfb with Mesa software OpenGL), one per map or one visiting
   maps for alternative 1. It needs a licensed client install, a write-data
   profile per run, and process control that does not use Windows APIs.
3. The `assisted` backend is not an alternative: it is the move/mine mode
   (teleport, direct ore insert), it still crafts with `a.begin_crafting`
   and still needs the player for the lab trigger.
