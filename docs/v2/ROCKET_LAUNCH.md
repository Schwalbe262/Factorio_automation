# Rocket silo, rocket parts, launch (the end of the roadmap)

Code: `src/factorio_agent/rocket_stages.py` (`rocket_silo`, shared supply,
observe receipt), `src/factorio_agent/rocket_feed.py` (`rocket_parts`,
`rocket_launch`), `src/factorio_agent/core/deterministic_launch.py`
(`LAUNCH_LUA`, `OBSERVE_LAUNCH_LUA`, `LaunchStage`), handlers in
`handlers/rocket.py`. The steps come from the generated roadmap tail
(`rocket_plan.tail`): `rocket_silo` after chemical science and the
`rocket-silo` research, `rocket_parts` after it, `rocket_launch` last.

## API facts (Factorio 2.1.20 + Space Age, checked in the install)

Sources: `doc-html/runtime-api.json` (application_version 2.1.20) and
`data/base|space-age/prototypes` of the Steam install.

| Fact | Where |
|---|---|
| Silo: 9x9 (`collision_box` +-4.2), `fixed_recipe = "rocket-part"`, crafting category `rocket-building`, `energy_usage` 250 kW (crafting), `active_energy_usage` 3990 kW (launch sequence), electric `primary-input` | `base/prototypes/entity/entities.lua` (rocket-silo) |
| Space Age: `rocket_parts_required = 50`, `launch_to_space_platforms = true`, rocket inventory 20 slots | `space-age/base-data-updates.lua` |
| Silo recipe: 1000 steel, 200 processing units, 200 electric engines, 100 pipes, 1000 concrete, category `crafting` (hand-craftable) | catalog (`tools/rocket_plan.py`) |
| Starter pack: 60 space platform foundations + 20 steel + 20 processing units (60 s); foundation: 20 steel + 20 copper cable (10 s) | `space-age/prototypes/recipe.lua` |
| Technology `rocket-silo` unlocks `rocket-silo`, `rocket-part`, `cargo-landing-pad`, `space-platform-starter-pack`, `space-platform-foundation` and has the hidden effect `unlock-space-platforms` | `space-age/base-data-updates.lua` |
| `space-platform` technology: research trigger `create-space-platform` (a natural milestone after the launch) | `space-age/prototypes/technology.lua` |
| Crafting-machine input inventory: `defines.inventory.crafter_input` (2.x name; no `rocket_silo_input` define) | runtime-api `defines.inventory` |
| `LuaEntity.rocket_parts` (read/write uint) and `rocket_silo_status` (read only, `defines.rocket_silo_status`) | runtime-api `LuaEntity` |
| Status order: `building_rocket` -> `create_rocket` -> `lights_blinking_open` -> `doors_opening` -> `doors_opened` -> `rocket_rising` -> `arms_advance` -> `rocket_ready` ("the rocket launch can be started by the player") -> `launch_starting` ... | runtime-api `defines.rocket_silo_status` |
| `LuaEntity.launch_rocket(destination?, character?)` (positional), returns false when the silo is not ready; raises `on_rocket_launch_ordered` | runtime-api `LuaEntity.launch_rocket` |
| `defines.cargo_destination.space_platform`: "only used for sending a space platform starter pack to a platform that is waiting for one" | runtime-api `defines.cargo_destination`, `CargoDestination` |
| `LuaForce.create_space_platform{name?, planet, starter_pack}`; `is_space_platforms_unlocked()` controls the space-platforms button | runtime-api `LuaForce` |
| `LuaSpacePlatform.hub`: "does not exist if the platform has not had the starter pack applied"; `apply_starter_pack()` applies it without a delivery; `state` `waiting_for_starter_pack` / `starter_pack_on_the_way` | runtime-api `LuaSpacePlatform`, `defines.space_platform_state` |

## `create_space_platform`: a free player action, not a grant

A player creates a platform from the space-platforms screen (the button exists
once `is_space_platforms_unlocked()`, i.e. after the `rocket-silo` research):
"new platform" asks for a starter pack and a planet and makes a platform in
state `waiting_for_starter_pack`. It costs nothing; the platform has no surface
content and no hub. The hub and the foundation tiles appear only when a
starter pack is applied: normally by delivering the pack with a rocket
(`cargo_destination.space_platform`), or by the script-only
`apply_starter_pack()`.

`LAUNCH_LUA` mirrors exactly the free part:

1. the starter pack comes from the actor's inventory into the rocket inventory
   (`inv.remove` after `cargo.insert`; rolled back if the removal fails), so
   the pack is paid (hand-crafted or collected);
2. `f.create_space_platform{...starter_pack=...}` only after
   `f.is_space_platforms_unlocked()` (`space_platforms_locked` otherwise);
3. a platform that already has a hub before this run's launch fails the action
   (`platform_hub_exists_before_delivery`): no hub may appear without the
   delivered pack;
4. `silo.launch_rocket({type=defines.cargo_destination.space_platform, space_platform=...})`;
5. `apply_starter_pack` is never called (a test scans every body for it).

Success (`OBSERVE_LAUNCH_LUA` -> observation `launch`): ordered by this run,
platform hub valid, `rockets_launched` above the baseline taken before the
order. `jobs.summary` sets `rocket_launch_verified` from that live receipt
(`rocket_feed.launch_verified`), never from a job's saved claim.

## The three stages

`rocket_silo` (`RocketSilo`): a 13x13 production block from `SiteService`
(silo in a 2-tile ring reserved for the belt feed, feed side turned to the tap
band), corner pole bridged to the grid, preflight (trees cleared, ore/water
refused); state `rocket-silo.json` and board block `rocket_silo` saved before
the first action. Power: measured headroom (`POWER_LUA`) must carry 250 kW +
max(300 kW, 20 % of offered); the launch peak is reported. Supply: the
hand-craft leaves of the silo (steel, processing units, engines, concrete,
iron plates for pipes) are collected in batches from published stock
(`V2Bootstrap.ensure_item`); a leaf without stock is a named wait
`rocket_silo_waits_for_<item>` with the producer capability and status; a
published row stops hand crafts above `HANDCRAFT_LIMIT`. Proof: silo at the
planned position, connected, charged, live recipe `rocket-part`.

`rocket_parts` (`RocketParts`): what to deliver = parts still to craft x part
recipe - what the input holds (live observation only, nothing remembered, so
an unknown outcome is reconciled by observing again). Hand feed:
`feed_rocket_silo` (`FEED_SILO_LUA`: exact silo identity and world, only
recipe ingredients, into `crafter_input`, the actor is charged exactly what
was inserted; the part counter is read, never written) when the slot is at
most half full. Belt feed: three inserters, a pole and three belt ports on the
reserved side, routed from free belt output ports (`production_sources.resolve`;
a stock chest is no belt source), chosen when its build seconds are below the
hand trips of the rest (`feed_choice`). Proof: `rocket_silo_status ==
rocket_ready`.

`rocket_launch` (`RocketLaunch`): `LaunchStage` on the observation; the pack
is collected or hand-crafted through the stage supply (1220 steel, 600 copper
plates, 20 processing units); then the `launch` act. A `launch_not_ready`
receipt is not resent for 600 ticks.

## Gate and named waits

`rocket_stages.receipt` (observe receipt `rocket_stages`, in the summary)
marks a stage `ready: false` while a hand-craft leaf of its item is neither
carried nor on a published output port; `progression.autopilot_nodes` then
does not propose it. The jobs name the item and the producer capability
while they wait (`<stage>_waits_for_<item>`, evidence `producer`,
`producer_status`, `needs` = critical-path claims).

## Assumptions and gaps (verify on a long run)

- Producer rows publish a board output port with `item` at a stock chest
  (`production_rows` contract: `{"direction": "output", "item", "name": <chest>}`)
  or a belt port (`tail.mode == "belt"`: no `name`). Only stock-chest ports
  (and plates from furnaces / bus collection points) are collected and count
  for the gate; a belt-output row feeds its consumer, is never collected from
  (belts are removed from the collection view for non-plates), does not stop
  a hand craft, and a leaf made only by one is a named wait with
  `producer_status: belt_row_only`. Example: electronic circuits become a belt
  row when the processing-unit row consumes them; no stage needs circuits
  directly (processing units are a non-hand-craftable leaf), and if a hand
  craft ever did, it would craft them from plates instead of draining the belt.
  Steel comes from `steel_production`'s port.
- No capability grows the power plant again (`steam_expansion` adds one pair
  once): a short grid is a named wait with `short_watts` in the receipt.
- The silo's 3.99 MW launch draw is reported, not required.
- Carried silo materials (about 30 inventory slots) are claimed through
  `evidence.needs`; other jobs could still spend them outside the critical
  path guard.
