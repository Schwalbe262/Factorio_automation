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
| Death | partly | `a.die()` leaves a `character-corpse` with the whole inventory (`character_corpse_player_index` 65536 = none); nothing respawns, so `recover-actor` (player successor) has no successor to bind; client-free runs emulate the player respawn ("Death and respawn" below) |
| Offline player keeps the actor | **no** | killing the client "logs off" the controlled character: `d.actor` invalid, `p.character` nil, as the API documents for associated characters |
| Detach, then disconnect | yes | `p.set_controller{type=spectator}` while connected leaves the actor in the world; after the client is closed the actor stays valid and crafts (not credited, as above) |

Trigger techs on the Nauvis path are `steam-power`, `electronics` (furnace
plates), `automation-science-pack` (hand-crafted lab), `steel-axe` (furnace
steel), `oil-processing` and `uranium-processing` (`mine-entity`, done by
pumpjacks/drills). Only the lab needs a player.

Cost: the headless server alone measured 367 MB and 0.13 cores (fresh map,
zero players); the hidden client is ~690 MB and a GPU context
([README.md](README.md) "Fleet runs").

## Client modes (implemented, `factorio_agent.actor_client`)

`actor-client.json` in the runtime folder: `{"client": "always" | "none" |
"until_lab_trigger"}`. A missing file is `always` (the interactive map and
every older runtime: unchanged). `tools/fleet.py start` writes it after
`runtime.create`, default `--client none` since the 35-minute equivalence
below (`--keep-client` = `always` for proof runs).

### `none`: client-free TEST runs (owner exception, 2026-11-13)

Only the dedicated server and the supervisor run, from tick 0. `connect-player`
adopts the server-created character (`adopt_playerless_actor`): it records
`d.detached_actor = {kind="detached-actor-v1", mode="none", world_id,
unit_number, ...}` and pins `d.crafting_actor_unit_number`. It refuses a
character that has a player or a world whose actor was ever bound to one.

The single written exception to "nothing granted" (AGENTS.md): the engine does
not credit a player-less hand craft, so the craft-item triggers that need a
hand craft never fire. `hand_trigger_technologies(catalog)` derives that set
from the catalog: roadmap technologies and their prerequisites whose trigger
is `craft-item` of an item some hand-craftable recipe makes (character
crafting categories); `mine-entity` is machine work; any other trigger type
counts as needing a player. On today's roadmap the set is exactly
`automation-science-pack` (craft 1 lab); `steam-power`/`electronics` are
furnace plates and `oil-processing` is a pumpjack mine.

1. The normal `craft` action records, in client-free mode only, a craft whose
   product is the item of a researchable craft-item trigger
   (`d.trigger_crafts`: recipe, count, tick, items consumed at queue time,
   item count before, minimum craft ticks).
2. The bootstrap, at the `automation-science-pack` step, crafts a lab for real
   if there is no such record with the lab still in hand, waits while it is
   queued, then returns `credit_hand_craft_trigger`.
3. `credit_hand_craft_trigger` (`WorldBoundGame.act`, receipt in
   `actions.jsonl` and `job-events.jsonl`) refuses unless ALL hold:
   Python: the action has only `technology`/`item`/`reason`; the runtime mode
   is `none`; the catalog trigger of that technology is `craft-item` of that
   item; the technology is in `hand_trigger_technologies`.
   World (same Lua command): same world id; the actor is the adopted
   client-free actor (`detached_actor()`); the technology exists and is not
   researched; its trigger is `craft-item` of that item; all prerequisites are
   researched; a craft record for it exists with count >= trigger count and
   consumed items; its minimum craft time elapsed; no queue entry of that
   recipe remains; the actor holds at least one more of the item than before
   the craft. Then it sets `technology.researched = true` (the engine applies
   the effects, e.g. the `automation-science-pack` recipe unlock, as for a
   fired trigger) and stores `d.trigger_credits[name]`.
4. Labels: `actor-client.json` and `run.json` carry `test_mode:
   "client_free"` and `script_credited_triggers` (technology, item, credit
   tick, craft tick); `report_run.py` prints `TEST MODE` and labels the
   milestones; `fleet.py status/report` show it in the header, a `cli` column
   (`none`) and a credits line; the dashboard header shows a TEST MODE chip.
   Results of such runs are test-mode results, never proof-run results.

No item is granted, no other research is touched, lab research stays real.

The written exception has exactly two parts (AGENTS.md): (a) the hand-craft
trigger credit above; (b) the player-presence emulation below, which includes
the engine's player respawn after a death (a new empty character at the spawn;
no item). Nothing else.

#### (b) Player presence: chart and generate around the actor

Measured on `detach-gui-1193005910` / `detach-gui2-1193005910` (`always`)
against `detach-none-1193005910` (`none`, no emulation), same seed:

| What a connected player causes | `always` | `none` before | emulation |
|---|---|---|---|
| freeplay `on_player_created`: `chart_starting_area` (square of `storage.chart_distance` or 200 tiles around the spawn) | 196 chunks charted, exactly chunks -7..6 | 0 charted | once, same area |
| freeplay: `surface.daytime = 0.7` at the join | daytime offset | not set | once |
| freeplay: crash site (12 wreck entities holding items), cutscene | present | absent | **not** recreated (it holds items) |
| chunk generation around the player, progressive | 419 chunks at tick 2.5k, 767 at 18.4k, 1964 (46x46) at 78.7k | 400 (map creation) | `request_to_generate_chunks(actor, min(22, 10 + tick/6900))` |
| charting around the moving character | not separable: the assisted actor stays at the spawn chunk, inside the start square | - | 3 chunks around the actor's chunk |
| evolution / pollution / expansion | 0.0074 / 1492 / on | 0.0073 / 1555 / on | surface-wide, unaffected |

`force.chart` is not processed for a force without players (probe on the
running `detach-none2`: the requested chunk stays `is_chunk_requested_for_charting`
and never becomes charted). So the emulation records the chunks a player would
have charted in `d.presence_emulation.charted`, and every reader of "charted"
uses `actor_client.CHUNK_CHARTED_LUA` (`chunk_charted(cx,cy)`: the engine
chart, or that record in client-free mode only): `mining_layout_policy`
patch survey, `defense_policy` survey, `nearby_threats` (threat survey and
bastion preflight), `nest_assault` candidate survey. Radar visibility
(`radar_coverage`, `radar_network`) stays the engine's: nothing sees further
than the start square plus 3 chunks around the actor.

`emulate_player_presence` (`WorldBoundGame.act`, via the supervisor's
`client-free-presence` command at most every 5 wall seconds) refuses unless the
runtime mode is `none`, the action carries exactly the fixed radii of
`actor_client`, the world id matches and the actor is the adopted client-free
actor. The first call writes `presence_emulation` (radii, start tick) into
`actor-client.json` and `run.json` next to `test_mode`.

Other player-dependent behaviour checked: alerts and `last_user` (nil for
player-less builds) are read by no framework code; `auto_pause=false` is set by
`runtime.start`; respawn does not exist without a player (emulated, below);
freeplay's `created_items` go to the player's own join character, which the
bind discards, so neither mode gives them to the actor.

#### Death and respawn (part of the presence emulation)

What the engine gives a connected player whose character dies: after the
respawn delay (`LuaPlayer.ticks_to_respawn`, 10 s) a new character at the
force spawn with an empty inventory; the old character's items stay in a
`character-corpse` where it died. Freeplay's `on_player_respawned` also
inserts a pistol and ten magazines: those are items and are **not**
reproduced (like its crash site and starting items).

Incident all-1114-0107-1075833632 (client-free, commit 7bb8ff3): the actor
died at 30.6 game min; every `observe` then failed `agent_dead` before
`actor-status` was read, so the old rule (record `actor_dead_without_player`,
safe stop) never fired, the supervisor looped in error backoff for 75+ real
minutes and the fleet watcher (waiting for the target minute) never ended the
run. Now:

- `respawn_client_free_actor` (`actor_death.RESPAWN_LUA`, `WorldBoundGame.act`,
  receipt in `actions.jsonl` and `job-events.jsonl`) refuses unless the runtime
  mode is `none`, the action carries exactly the fixed constants of
  `actor_death` (delay 600 ticks, corpse search radii, threat radius), the
  world id matches, the recorded actor is the adopted client-free actor
  (`d.detached_actor.mode == "none"`) and no character of the old unit is
  alive. The first call records the death (`d.client_free_death`: the newest
  unrecorded corpse within 64 tiles of `d.actor_last_known_position`, which
  the presence update writes, else within 256 of the spawn; its death tick,
  item count and the enemies within 32 tiles of it as the likely cause) and
  waits (`respawn_delay`) until death tick + 600. Then it creates one
  `character` at `find_non_colliding_position` of the force spawn (empty,
  `inventory_empty` in the receipt), binds it (`d.actor`,
  `d.crafting_actor_unit_number`, `d.detached_actor.unit_number`,
  `respawns`) and appends the death to `d.client_free_deaths`. The corpse is
  not touched.
- The corpse items come back only by `recover_client_free_belongings`
  (`recover-belongings` in client-free mode): the actor's own corpse of that
  death (position and death tick from the record), no enemy within 32 tiles of
  it (else `waiting` `belongings_unsafe_enemies_near_corpse`), empty crafting
  queue; the assisted actor moves to the corpse (a teleport, as every assisted
  move), each stack goes into its main inventory with counts per item and
  quality conserved (else `blocked`), and it moves back; the character backend
  must already be in reach. A corpse that is gone is recorded `lost`.
- The supervisor treats `agent_dead` as its own state (`actor_dead`, never
  error backoff, `Supervisor._actor_dead`): `respawn-actor` every cycle until it
  succeeds, then every job whose last action has an unknown outcome is
  reconciled (`reconcile`, never retried), and the corpse items are owed
  (`belongings_due`): tried between slices every 30 s while unsafe, at most 60
  times, while the jobs continue from base stock. A refused respawn stops the
  run (`actor_death_unrecoverable`). Events: `actor_died`,
  `actor_respawn_waiting`, `actor_respawned`, `death_reconcile`,
  `belongings_deferred`, `belongings_recovered` / `belongings_unrecovered`;
  the record is `actor-client.json` `deaths` (read by `fleet.py report` and
  `waste_report.py` `actor_death`).
- A connected player (`always`) is respawned by the engine; the supervisor
  binds the successor with `recover-actor` once `actor-status` says
  `recovery_ready`, then recovers the corpse with the existing
  `recover-belongings` (`corpse_recovery`). `until_lab_trigger` after the
  detach has an offline spectator player and no emulation: the run stops
  (`actor_dead_without_player`).

### `until_lab_trigger`

The run starts as `always`. When an observation shows
`automation-science-pack` researched, the supervisor calls `detach-client`:
`detach_actor` refuses unless the hand-trigger set is researched, the crafting
queue is empty and the actor is still the pinned connected owner's
character; it sets the owner's controller to spectator and records
`d.detached_actor` (with `player_index`). Then the client is closed by pid +
creation time (`stop_crafting_client`), the actor is checked valid and
unowned after the server sees the disconnect, and the transition is recorded
in `actor-client.json` (`detached`, `closed_client`). Every step is re-entrant;
a lost receipt is reconciled from the world record. Unit-tested (fake game and
Lua); the live equivalence proof was done for `none` only.

### Player-pin checks with a detached actor (both modes)

`DETACHED_ACTOR_LUA` (`detached_actor()`): the recorded unit number and world
id, still `d.actor`, and no player. Adapted: `supervisor.actor_binding`
(`_detached_binding`, plus `detached`/`client_mode` in the binding),
`actor-status` (`detached`, `detached_record`), `connect-player` (no client,
`detached: true`), `recover-actor` (refuses: no player successor),
`corpse_recovery` (offline owner + detached actor), `nearby_threats` and
`nest_assault` (`detached_character`), `defense_relocation` and
`post_raid_recovery` (owner-control checks), native repair control (no
controller to check), the `_HELPERS` re-bind (off once detached),
`BIND_CRAFTING_PLAYER_LUA` (refuses: `actor_detached_server_only`), the
bootstrap guard (client-free: the credit path; detached before the trigger:
blocked), lifecycle (`_client`, `_record` of the closed client, `status`
reports `client.expected=false`), `tools/fleet.py` (alive, cost, client
column). `checkpoint` reads only the world id and tick (unchanged).

Death after the detach (`until_lab_trigger`): the corpse keeps the inventory
and nothing respawns, so the supervisor writes `actor_dead` (reason
`actor_dead_without_player`) to `actor-client.json`, logs
`actor_dead_without_player` and safe-stops the run. No replacement character
is created. In client-free mode (`none`) the death is respawned as a player's
would be ("Death and respawn" above).

Production statistics after the detach / in client-free mode count only
machine output: the observation reads iron/copper plates, coal, science packs,
circuits and rocket parts. Plates and science come from furnaces/assemblers
and labs (credited); `construction_supply.production_rate("coal")` sizes coal
haul batches from drill output (a player-less hand-mined coal would not
count; the rate is then the drills' real rate); circuit rates in
`supply_options` lose hand-crafted circuits, which they measure as factory
output anyway; science `products_finished` proofs are machine-side.

### Equivalence

**With (a) and (b), seed 1193005910, 35 game minutes** (`fleet.py report
--name detach-none4` / `--name detach-gui3`, same code; milestone = game
minute the step succeeded):

| run | power | auto | emd | logi | iron | copr | coal | smelt | blocked now | charted / generated at end | evolution / pollution | RAM / cores |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `none` | 8.2 | 11.8 | 18.8 | 24.0 | 18.4 | 19.4 | 19.9 | 30.7 | 1x `production_row_unsupported` [produce, inserter] at tick 112k | 196 (emulated) / 2629 | 0.0136 / 2731 | 441 MB / 0.30 |
| `always` | 8.5 | 12.2 | 17.8 | 24.9 | 18.2 | 19.2 | 19.5 | - | none | 196 / 2262 | 0.0125 / 1838 | 1543 MB / 0.78 |

Milestones agree within about one game minute both ways; the client-free run
went further (smelting district at 30.7, hence more pollution) and then hit
`production_row_unsupported`, a reason the `always` fleets (`auto-*`) also show
at that stage. The credit fired once (lab crafted, credit at tick 17301), no
controller errors in either run. The actor never left the spawn chunk, so the
emulated chart stayed the 196-chunk start square, the same as the player's.
Generation overshoots a little (2629 vs 2262: the generator adds a margin
around a script request). `tools/fleet.py start` now defaults to `--client
none`.

**Before (b), 21 game minutes** (`detach-none` / `detach-gui`):

| run | power | auto | emd | iron | copr | coal | blocked now | RAM / cores |
|---|---|---|---|---|---|---|---|---|
| `none` | 8.2 | 11.8 | 17.6 | - | - | - | 3x `mining_patch_native_receipt_changed` [mining_outpost] | 420 MB / 0.19 |
| `always` | 8.6 | 12.2 | 18.2 | 19.2 | 19.6 | 20.4 | 1x `bulk_starter_capacity_requires_electric_mining_after_unlock` | 1367 MB / 0.67 |

The scripted credit fired once: lab craft queued at tick 15915, credit at
tick 17286 (first attempt, no refusal), `automation-science-pack` recipe
enabled by it; no other research was touched.

Up to the electric-mining-drill research the modes match (client-free 0.4-0.6
min earlier). After it they do not: the save of the client-free run has 0
charted chunks (400 generated) against 196 charted (1964 generated) in the
`always` run. A connected player charts and generates chunks around its
character; a player-less character does neither. `mining_layout_policy.
survey_patch` reads only charted chunks, gets an empty tile list and every
mining outpost blocks. That gap is what exception (b) closes.

### Problem runs after a round (post-mortem, keep-alive)

At the target minute a client-free run is safe-stopped and paused, triaged,
and, when it is not healthy, snapshotted read-only into `<run>/postmortem/`
(DEBUGGING_A_STALL.md "Post-mortem of a fleet run"). `fleet.py start
--keep-problem N` (default 1) keeps the N worst problem runs paused with their
server up for `--keep-timeout` minutes (default 60). Cost: a paused client-free
run is its dedicated server only, ~0.37-0.45 GB and ~0.01 cores (no ticks, no
supervisor), the memory it already used while playing; the default 1 adds
under 4 % to a 12-run round and the next `start` releases it first when its
capacity check would fail. `--keep-problem 0` stops every run as before;
`--no-postmortem` skips the snapshot. A kept run is never joined by a client
(a connected player would change the client-free world); `fleet.py inspect
--gui` opens a copy of its save in a separate visible client instead.
`fleet.py revive` restarts a stopped run's server from its save (`runtime.start`,
which rewrites `server.json`/`world-settings.json` and saves the same paused
world), checks `game.tick_paused` and pauses it through the lifecycle marker if
the save was not paused; no supervisor, no client.

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
