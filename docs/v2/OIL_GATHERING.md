# Natural crude extraction

`OilGathering(game, builder, catalog, perimeter)` exposes read-only
`prepare(obs)`, ordinary paid `step(obs)` and read-only `proof(obs)`, without
parameters. It requires naturally completed `oil-gathering` and an enabled
pumpjack recipe. The installed `oil-processing` technology must retain its
native `mine-entity: crude-oil` research trigger. This controller never writes
research state, counters or fluid contents.

Preparation surveys crude wells in generated chunks around the refinery origin
(`petroleum.refinery_origin`, the crude consumer), nearest connection length
(Manhattan tiles) first, in rings `OIL_SURVEY_RINGS` (256, 384, 512), at most
`OIL_WELLS_PER_RING` (8) wells per ring, four pumpjack orientations and four
adjacent service-pole sites per well. When no field fits:

- ungenerated chunks inside the radius: `prepare` returns a `waiting` result
  with `scout` (nearest ungenerated chunk, a generated enemy-free stand chunk
  within `OIL_SCOUT_WINDOW_CHUNKS` whose stand is a land tile the actor fits on,
  `find_non_colliding_position` within `OIL_SCOUT_STAND_RADIUS` inside the chunk;
  a target that `OIL_SCOUT_MOVES_PER_TARGET` moves do not bring closer is given
  up) and `wait_for` naming that chunk; `step`
  walks the actor there with an ordinary paid `move` and waits. Generation
  follows the actor (a connected player; client-free:
  `emulate_player_presence`). A target still ungenerated
  `OIL_SCOUT_GENERATE_WAIT_TICKS` after arrival is given up (`oil-scout.json`);
  after `OIL_SCOUT_MAX_TARGETS` the survey stops scouting;
- otherwise it blocks with `plan_needs` kind `site`, change "a crude-oil field
  within N tiles (charted)". The remedy rung `site:wider_survey`
  (`oil_survey_radius`) widens the rings by `OIL_SURVEY_RING_STEP` (128) up to
  the hard maximum `OIL_SURVEY_RADIUS_MAX` (1024), scouting included.

A far field's crude connection is planned by `petroleum.plan_crude_connection`:
its corridor survey runs in row strips of `PIPE_SURVEY_CELLS` up to
`PIPE_SURVEY_MAX_CELLS` with the shared long-route fallback. Not yet: pump
segments (a route beyond one native pipeline extent blocks with
`pipeline_requires_explicit_pump_segment`) and a rail alternative for a very
far field. Local output
geometry comes from the installed catalog's four rotated internal fluid-port
positions plus their outward direction. Each candidate includes one pumpjack,
three ordinary pipe tiles and one local pole. A bounded ordinary pole bridge
connects the local service pole to an existing supplied network. Whole native
footprints, resource keepouts, reservations and defense coverage must pass.
The initial pipes are a finite commissioning buffer; no sustained extraction
or refinery supply rate is claimed.

The resource-policy addition is limited to a tagged3x3 pumpjack centered on
an exact live crude well. It rejects solid ore and solid mining reservations.
The existing solid mining resource/entity lists are unchanged. Pipes, poles,
inserters and factories receive no crude-resource exception. The board's crude
mining keepout covers only the exact pumpjack footprint, inside the larger
reserved local service area.

`step` saves `oil-gathering.json`, reserves the site and calls the existing
defense gate before any pumpjack construction. The added scope needs complete
geometric coverage and ten full normal magazines. All machines, pipes, poles,
turrets and ammunition are normally paid. Installed steel supply must provide
the pumpjack recipe's five steel plates; ordinary crafting also consumes ten
gears, five circuits and ten pipes. No special source funding is used.

Every constructed pipe, pole and pumpjack receives a durable exact unit
binding; missing or changed known units block. Commissioning requires a powered
pumpjack, exact native reciprocal fluid peers through every new pipe, actual
crude in the outlet and fresh native `oil-processing.researched`. It then
publishes the exact typed outlet in `fluid-sources.json` under `crude-oil`,
preserving other entries and blocking a different existing crude identity.

Installed2.1.20 connectivity was verified read-only on existing steam pipes:
`get_fluid_box_neighbours(1)` returns `{entity,index}` records. The old
`entity.fluidbox` property is absent in that installation. The proof uses the
[native entity fluid-neighbour API](https://lua-api.factorio.com/latest/classes/LuaEntity.html#get_fluid_box_neighbours),
without creating temporary entities or modifying fluid networks.

At native tick2230813 the nearest surveyed crude well was `(66.5,-304.5)`,
with two further wells near y=-308.5. Oil gathering was researched and oil
processing remained unresearched. Local well, outlet and long power preflight
reached defense in about5.7seconds; the complete prepare correctly waited while
another job's new perimeter posts were still commissioning. Fresh full native
prepare at tick2271889 passed in6.20seconds:52 entities comprising one pumpjack,
three pipes and48 paid poles; east-facing crude outlet `(70.5,-303.5)`, local
reservation `[[65,-306],[71,-302]]`, one added turret and ten full magazines.
The long northern power bridge passed resource keepouts without pole exceptions.
No extraction,
research unlock or live oil construction has been performed by this worker.
