# Layout principles for production blocks

Blocks are generated from the recipe tree and catalog numbers, never from a
copied blueprint. Each rule names the code that decides or enforces it. The
shape audit (`blocks/cluster_audit.layout_findings`) runs on clusters and is
tested on rows, so an extension cannot regress into the old green core
(6 machines, 426 belts, 50 undergrounds, 6 splitters, 74 x 74 tiles), which the
tests show failing it.

| # | Rule | Decided / enforced by |
|---|---|---|
| 1 | No belt loops around a machine, no crossing inside a block, short paths. Port sides are not fixed: plates enter on the bus edge (`site.fit.orientations` turns the input ports to the allocation's tap band); the product side is chosen from the consumer's position among the free sides of the compact layout (`cluster_block(output_side=)`, `green_science.planned_green_clusters` picks the side nearest the lab entry; every side shares one reservation). | `layout_findings`: `belt_wraps_machine` (belts within 3 tiles on 3+ sides), `belt_cycle`, `internal_crossing` (underground or splitter inside the block) |
| 2 | Direct insertion or a belt, per producer -> consumer edge, from the machine ratio and the flow. Direct when one side is one machine and the other at most `DIRECT_MAX_CONSUMERS = 2` machines (what a one-tile gap can reach in a 2-row cluster) and the flow per pair fits `MAX_ARMS_PER_EDGE = 3` inserters of the row inserter (`params.inserter_rate`: 30/min plain, 120/min fast at hand size 1). Otherwise a belt: a row lane, or a bus lane when several blocks use the item. | `blocks/cluster.decide_edge`; a belt edge raises `needs_belt` and `production_layout.design_line` keeps the row design |
| 3 | Ratios from the catalog: crafts/min of a machine = 60 x crafting speed / recipe energy; demand propagates down the tree with product amounts (a belt craft yields 2). One machine per intermediate serves every consumer; the start is one product machine (start small). | `cluster.tree`, `cluster.propagate`; `needs_more_machines` when a node would need two |
| 4 | Belt lanes are a resource: two sourced items share one input belt, one lane each, side-loaded from two feeders at the belt head; rows use a second belt with long-handed inserters for more inputs. | `cluster_block` head; `blocks/sizing.plan_lanes` for rows; audit `lane_mixed`, `lane_capacity_exceeded` |
| 5 | Bus practice: taps are splitters with output priority to the branch, branches tunnel under other lanes; copper cable never goes on the bus (it is made next to the circuit machine). | `planned_tap.layout`, `planned_bus.branch_priorities` |
| 6 | Growth in place: rows reserve slots along their belts (`blocks/row.extend_row`). A cluster has no slots: `cluster.grow_decision(plan, n)` re-evaluates every edge for `n` product machines without touching the built plan; an edge that turns into `belt` means the next stage is a row block fed by a lane next to it. | `grow_decision`; `production_layout.extend_line` refuses a cluster |
| 7 | Throughput is a constraint: inserter rate per machine edge, lane capacity per input port, declared crafts never above nameplate. | `audit_cluster`: `inserter_throughput_insufficient`, `lane_capacity_exceeded`, `declared_rate_above_nameplate` |
| 8 | Input order follows the bus: `cluster_block(plate_order=)` puts the first item on the north feeder (left lane) so a caller can follow its bus lane order. | caller |

Shape bounds (`cluster_audit`): at most `MAX_BELTS_PER_SLOT = 16` belts per
built or reserved machine slot, and machine tiles (reserved slots counted) at
least `MIN_DENSITY = 0.12` of the bounding box.

## Belt lanes and bandwidth (`belt_flow.py`, `bandwidth_audit.py`)

A belt is two lanes, each half the belt (yellow: 450 items/min per lane, 900 per belt). Where
items join decides how many lanes they can use, so a plan's capacity is not "one belt" wherever
a belt runs. Cause (owner, eval-4177156614, brick and steel blocks): raw and coal were each
side-loaded onto one short belt and that belt entered a splitter, so each furnace row got half a
lane of raw, a quarter belt: "splitting a half lane in half means only a quarter gets through".

Rules of the flow model (`belt_flow.solve`, pure, on plan entities):

| join | lanes |
|---|---|
| head-on (into the back of a belt, underground or splitter) | kept |
| turn (a belt fed from one side and not from behind is a curve) | kept |
| side-load (fed from a side while it has a back input, or from both sides) | both feeder lanes onto the target's near lane; straight traffic first, the side-load fills the rest |
| underground pair | kept |
| splitter | kept; per item even split between live outputs; a branch that takes less (declared demand x margin) leaves the rest to the other; an output with nothing behind it backs up; output priority fills first; a filter sends its item to the priority side |
| inserter | picks from both lanes, drops on the far lane (`lanes.inserter_output_lane`) |
| drill | drops on the near lane (prototype `vector_to_place_result`; unverified in game, irrelevant to capacity) |

Not modelled, conservative: side-loading onto an underground (lane-filter trick) carries nothing
and is reported `unmodelled_join`; belt loops are `belt_cycle`; a filtered item never overflows
to the other side. The capacity is the steady state when every consumer draws at full speed.

Findings (`bandwidth_findings`): `capacity_shortfall` / `single_lane_feed` (a consumer's line
delivers less than its downstream demand x `MARGIN` 1.1; single lane when both lanes would
have sufficed), `side_load_before_splitter` (a feeder with more than one lane of an item is
side-loaded within `SIDE_LOAD_REACH` tiles before a splitter that splits it: half the feeder is
lost and each branch gets half a lane), `lane_starvation` (the item reaches the consumer on
neither lane), `unbalanced_lanes` (parallel belts of one flow). A side-load whose lane covers
the demand is not a finding: one lane is all a consumer below half a belt needs.

Audit (`bandwidth_audit.audit_root`, every plan with ports or consumers): furnace blocks and
smelting banks (every furnace input inserter at full reserved growth: raw and coal per
furnace), the bus with its taps (tap demand), produce rows and the green science cluster
(their bus taps get the trunk from `trunk_side` when the trunk is not in their plan), mining
banks with their trunk taps (serve-after taps pass the tap's `primary_demand` on first), the
material-supply coal hub, the power coal feed and the wood feed. Input ports feed a full belt
(the port's lane only when it declares one), drills their nominal rate. Results: the furnace
plan record `bandwidth` (head choice, candidates, findings), the observe receipt
`belt_bandwidth` (per block counts and findings, `counts_by_type`, `rework`) and the fleet
report section "belt bandwidth findings" (count per block type).

Generator (`feed_head.rank_heads`; `planned_furnaces.head_choice` for steel/brick blocks,
`smelting_site.district_head_choice` for the smelting district banks): a paired furnace bank
has two feed heads, the shared head of `smelting.paired_furnace_bank` and
`feed_head.split_feed_head` (raw split head-on first, each half side-loaded onto its row; coal
likewise from the other side, one underground; the community layout: one yellow ore belt feeds
two rows of 24 stone furnaces). Each is run through the flow model at full reserved growth and
ranked by (shortfalls, wasted capacity, entities): the split head wins for steel (waste), bricks
(a row's six furnaces need the whole quarter belt, below the margin) and the district banks (12
slots per row need 225 ore/min, the shared head delivers exactly 225 per row, the split head
450). Per row it delivers a full lane of raw and of coal. The district's coal branches then run
along column X0+5.5 into each bank's inner coal port (`smelting_site._coal_network`); the
district keeps its 48 x 32 frame and ports.

Existing blocks keep their head until it limits them. Growth is guarded (`growth_evidence` for
steel/brick blocks, `SmeltingDistrict._grow` via `head_rework.bank_growth_check` for district
banks -> `feed_head_bandwidth_below_next_slot_demand` when the flow model says the next slot
would starve), and the receipt's `rework` lists built blocks below their demand.

Rework (`head_rework.py`, capability `feed_head_rework`, district banks; steel/brick blocks not
yet, their coal route would have to be planned again): proposed once per bank when the head
limits the built or the refused rows and the rework (actor time for added + removed entities
plus the cut-over stop) is cheaper than a second bank. The plan is republished first (state and
board), the new parts that do not stand where old ones stand are built beside the running head,
then a stocked cut-over (`head_rework.CUTOVER`, every item carried, room for every mined
entity, every built furnace holding ore): coal branch onto the new coal port, old coal side-load
removed (coal never reaches a lane that will carry ore; checked stage by stage with the flow
model), raw splitter, ore branch, old splitter and lower column, upper coal riser; then the old
belts are mined and their items reused. Proof: the new head stands, no old part stands, the rows
carry one item per lane. The shared head feeds 10 stone furnaces per row with margin, the split
head all 12 reserved slots (24 at a full 48-furnace frame).

Bank shape against the owner's reference (factorioprints "Early Game Saturated Stone Furnace
Setup": 2 x 24 furnaces facing one shared centre output belt, inputs on the two outer belts,
ore and coal each split first): `paired_furnace_bank` already has that arrangement (outer input
belts, one shared centre output, upper row on one lane, lower row on the other; the flow model
shows both output lanes filled, 225/min each at 12 slots per row). Only the row length differs
(12 slots per row inside the 48-tile district; a saturated belt needs 24 per row).

## Rebuilding is allowed

A built block is not frozen. Growth into reserved slots is the cheap default, but when the
ratio, the flow or the neighbours change (a second pack machine turns a direct-insertion edge
into a belt edge; a row has to move for a bus lane), the affected part is dismantled by paid
`mine` actions and rebuilt from a new plan. The decision uses the same time comparison as
every expansion, with the dismantling, the rebuild and the lost output counted as cost, and
it never removes the only supply of a critical consumer. Not implemented as a general
capability yet; the retirement code (`obsolete.py`) has the guarded dismantle it needs.

## Trees never move a planned site (`site/nature.py`)

Owner's rule (2026-11-13): the layout is not routed around trees. The bus, bus-side block
slots (smelting, produce rows, science, clusters, mall), their feed/tap routes and growth
strips keep the position the plan gives them; trees and rocks there are cleared. Position is
scored first and the tree count only breaks exact ties (`nature.ranked`, `nature.tiebreak`;
`candidates.free_sites`, `siteselect.search_bus`). No plan has a tree cap any more (`planning.preflight`,
`transport` corridors, the starter coal corridor): the builder clears every `clear_obstacles`
entry first, one paid `mine` per step, checking `CLEARANCE_BATCH` obstacles per query
(`V2FactoryBuilder._next_clearance`). Water, cliffs, ore, non-minable or player entities and other
blocks still block. Mining outposts and off-bus temporary cells (startup science cell, startup lab,
supply cell, coal hub) have a free position and may still prefer clear ground.

## The cluster (`blocks/cluster.py`)

Machines on a square grid with a one-tile gap (pitch = machine size + 1);
every direct edge joins two edge-adjacent cells, its inserter in the gap;
every machine taking sourced items touches the input belt, which runs along
the top and may turn once down the right edge; poles stand on gap corners.
The smallest bounding box wins. First green science unit (fake catalog,
canonical frame; K cable, C circuit, I inserter, G gear, B belt, S pack,
`@` plate ports, `O` pack port):

```
@v...................
.>>>>>>>>>>>>>>>>>>>v
@^...i.+.i.+.i.+.i..v
....KKK.CCC.III.GGG.v
....KKKiCCCiIIIiGGG.v
....KKK.CCC.III.GGG.v
.............i.+.i.+v
............SSS.BBB.v
............SSSiBBBiv
............SSS.BBB.v
...........+.i.......
.............O.......
```

6 assemblers, 12 inserters, 33 belts, 6 poles, no underground or splitter,
23 x 14 tiles reserved. The same generator lays out red science (G -> R),
the belt, inserter and magazine stock rows and engines (G -> E <- P).

## Where it is used

* `produce` (`production_layout.design_line`): a row line first; when no row
  can carry the tree (a two-level intermediate such as the inserter), a cluster
  line ending in a stock chest (`cluster_line`). Three belt-fed solids (low
  density structure: copper, steel, plastic) are a row with a **lane-merge
  adapter** (genome `merge: lane_merge`): the heaviest item has the near belt to
  itself, the two lighter ones arrive on two full routed belts that side-load
  from opposite sides onto the start of the far belt, one item per lane (never
  a mixed lane, no sushi); `audit_line` checks the lanes and
  `production_site` rejects a placement whose routes feed an adapter tile
  (`adapter_intrusions`). Output mode `stock_chest` (default, construction
  stock) or `belt`: the row's output belt end is published as a free belt port
  (`belt_tail`), so a row that cannot make an input in-line (a processing unit
  needs circuits, which need cable) routes from it; `rocket_plan` makes a row a
  belt row exactly when such a consumer exists.
* `logistic_science` on a planned world: the cluster (or the split row, see "Green
  science" below) on iron and copper bus taps, packs routed to the lab entry; no district branch (blocked
  `green_science_needs_planned_bus` without a published bus). `green_core`
  is kept only for a controller without a site service.

Not yet: intermediates on bus lanes with lane injection (gears/circuits for
several blocks), and the second green stage (a pack row on a mixed lane fed
by the unit's inserter and belt machines, as `grow_decision` asks for).

## Candidate layouts (`layout_variants`, `layout_selection`)

A block is no longer one fixed shape. The generators above take a few parameters,
a **genome**, and rebuild the block from it deterministically; a layout is never
stored as entities.

* Genome (`layout_variants.Genome`): `form` (`row`: intermediates on internal belt
  lanes / sub-rows; `cluster`: every intermediate by direct insertion), `machine`,
  row `spare` (`none` | `default` = as many slots again | `max` = up to 3x the built
  count within the lane budget), row `merge` (`lane_merge` for three belt-fed solids), cluster `grid`, `turned` (input belt on one side or
  turning down a second side), `cells` (which machine in which cell), `output_side`.
  `modules` / `beacons` are reserved and empty; `layout_variants.effects` is the one
  place their effects will enter the metrics.
* Candidates (`layout_variants.candidates`): the bounded genome space is enumerated
  (blocks have at most 8 machines; at most `MAX_GENOMES`), every genome is generated
  and audited (`audit_line`, `audit_cluster`, `layout_findings`; a failing one is
  dropped), and the best per **niche** is kept (MAP-Elites style archive):
  `port relation (same / opposite / adjacent) / aspect (flat / square / tall along the
  input edge) / rate (short / meets / double the request) / growth (none / in_place)`.
  Inside a niche: fewer raw plates, belts, tiles, entities win.
* Descriptor (module doc of `layout_variants`): footprint, entity counts, raw cost,
  ports with sides and lanes, edges (direct / belt / port), rate now, growth ceiling,
  what limits it and what a larger rate needs, `power_kw` (catalog `energy_usage`)
  and `pollution_per_min` (catalog `emissions_per_minute`; `-` for a catalog exported
  before that field existed).
* Library (`data/layouts/<item>.json`, `python -B tools/layouts.py build`): generator
  version + catalog fingerprint + request; niche -> genome, compact descriptor, scores,
  optional `evidence` (`tools/layouts.py evidence <runs>` merges runs / proofs / median
  build time). At runtime every entry is regenerated and re-audited; a failing entry is
  skipped and reported in the block's `layout.library_skipped`; missing niches are
  generated on the fly. A test regenerates every committed entry.
* Selection (`layout_selection.score`, lower wins, named weights): port cost of the
  best orientation (`site.fit.port_score`), shortfall below the request (`W_SHORTFALL`),
  horizon demand above the growth ceiling (`W_CEILING`; horizon = 2x the request or the
  roadmap's upcoming units), raw plates (heavier in the first two game hours), entity
  count (build time), reserved area; power and pollution weights and a power budget
  exist and are 0 / off by default. `production_site.place_line` probes up to
  `PROBE_LIMIT` candidates on a real allocation, ranks them, places the winner (the
  next one on failure) and saves `layout` in `production-row-<item>.json`: variant,
  genome, descriptor, score terms, runner-ups, `chosen_tick`, `verified_tick`.
* Exploration (off by default): `python -B tools/fleet.py start ... --explore` records
  `explore: true` in the fleet manifest and writes every run's
  `<runtime>/layout-settings.json` (`{"explore": true}`; without the flag an explicit
  `{"explore": false}`, so nothing inherited switches it on; the older
  `FACTORIO_LAYOUT_EXPLORE=1` in the environment of `start` still counts as the flag).
  Optional keys `top_k`, `margin`. A seed from (world id, block) picks among the best
  `top_k` feasible candidates within the margin, so one map always explores the same way;
  produce rows, red science and green science all use it. `fleet.py status` / `report`
  headers say "layout EXPLORATION on". The report's "layout variants used" section lists the
  variants of each block side by side (blocks `produce:<item>`, the science blocks,
  `mining:<resource>` from the banks' `layout` record, proof = bank `ready` at its
  `output_evidence` tick, and `smelting:<resource>` from each district bank's shape variant,
  proof = the `smelting_district` milestone): runs, proofs, proof rate, median build game-seconds
  (chosen -> verified), median game-seconds from the block's proof to its next milestone
  (science blocks: the next `research_*` milestone; produce rows: any), explored, kW,
  pollution; with two or more variants of a block, `-> leading:` names the one the data
  favours (proof rate, then the quicker follow-up, then build time; at least
  `MIN_RUNS_TO_LEAD` runs). `tools/layouts.py evidence <runs>` merges the same numbers into
  the library entries (the science section too).
* Pollution: the catalog export now carries `emissions_per_minute` per entity (the energy
  source's `emissions_per_joule` x max energy usage x 3600). The field is excluded from
  `WorldCatalog.fingerprint` (`FINGERPRINT_EXCLUDED`), so a running map's catalog identity is
  the same whether its `catalog.json` was exported before or after; until a map re-exports
  its catalog the metric shows `-`. `tools/layouts.py show` prints kW and pollution per
  candidate (now and at the full frame for science blocks).

Used by `produce` (and through it the planned `ammo_production` and
`engine_production` rows), by red science (`science_layout`) and green science
(`green_layout`), both below. Mines (`mine_layout`, "Mine output trunk" below) and smelting bank
shapes (`smelting_layout`, next) are families too.

### Smelting bank shapes (`smelting_layout.py`)

A wrapper around `smelting.paired_furnace_bank`; the generator, its feed head and its row
arrangement are not touched (the feed-head work owns them, and a new head lands in every
candidate). Reference (owner, a saturated early stone furnace setup, principles only): two rows
facing one shared centre output belt, each row filling its near lane, inputs on the two outer
belts (ore and coal on their own lanes), units stacked side by side; 24 stone furnaces per row x
18.75 plates/min = one lane, so 2 x 24 saturate one yellow belt. The district bank is that shape.

* Genome `BankGenome`: `slots` per row (6 / 12 / 24: the reserved row frame), `units` (1 long bank
  or 2 shorter ones side by side), `input_side` / `output` (`outer` / `shared_centre`, the
  generator's form; `centre` inputs with outer outputs are enumerated and reported
  `does_not_generate` until a head exists), `furnace` (stone; steel in the same 2 x 2 slots,
  needs its research; electric fails the slot audit until a 3 x 3 slot exists).
* Audit (hard): no shared tiles, every inserter between a belt and a furnace and inside a pole's
  supply area, each unit's poles one chain. Descriptor: rate now / full (each row capped at its
  lane, a unit at one belt), counts now / full, raw now / full, coal per minute, kW, pollution,
  footprint, `saturates_belt`, and the lane flow model's findings at the full frame
  (`bandwidth_audit.furnace_bank`: the shared head's `side_load_before_splitter` /
  `single_lane_feed` stay visible).
* Library `data/layouts/smelting-bank.json` (12 entries), `python -B tools/layouts.py show
  smelting-bank`. Selection (`rank`): the room per resource (length x depth) and the furnace's
  research filter the options, `layout_selection.score` ranks them. Real catalog, iron, start 2
  per row (75/min):

| context | default demand (horizon 150) | horizon 600/min |
|---|---|---|
| district: 48 long, 16 deep per resource | `smelt-6x1-stone` 88 | `smelt-12x1-stone` 1313 (today's bank; the 6-slot ceiling 225 costs more) |
| long zone 96 x 16 | `smelt-6x1-stone` 88 | `smelt-24x1-stone` 164 (the saturated reference) |
| 48 x 32, steel researched | `smelt-6x1-stone` 88 | `smelt-12x1-steel` 122 |

Runtime: record only. A district bank's `layout` record is derived from its plan
(`smelting_layout.record_for`: `capacity_slots`, furnace) for the fleet report; the district keeps
its zone contract (48 x 32, 12 slots) and its generator call. Not yet: the district choosing the
shape (slots, units) through `rank` in its own place loop, which needs the district geometry to
take a bank length other than 48 (after the feed-head change lands).

### Science blocks (`science_layout.py`)

The red science block (machine part + lab part + the pack link between them) is a genome
family, chosen on the site by `AutomaticScience.prepare` (`_choose_layout`): every
candidate is probed on one allocation (`science_layout.rank`: `site.fit` port cost of the
plate inputs towards the tap band + `layout_selection.score`), optionally explored, then
placed in that order; `automation-science.json` keeps the `layout` record (`chosen_tick`,
`verified_tick` stamped at the first proof). A block planned before this keeps its built
template and gets the record `sci-single-row-template` (`science_layout.legacy_record`).

* Genome: `machines` `single` (the proven row: one shared gear machine at the head
  side-loading gears; ports N/S of the head) | `direct` (pairs `[R][G][R]`: one gear
  machine between two pack machines by direct insertion, iron and copper on the two
  lanes of one plate belt fed at its head; both ports face W), `labs` `row` (the eight-lab
  row) | `both_sides` (labs on both sides of a half-length belt) | `chain` (a belt-fed row
  plus a second row behind its first columns fed lab-to-lab), `lab_slots` 8. The machine
  frame is the request's (`science_growth.frame_size`), so a library genome regenerates at
  any frame.
* Composition: labs on top, machines below, plates at the west end; the pack belt runs to
  a link column one tile east of the lab head, north, and west into the head (turns only,
  red keeps the right lane of the westbound lab belt; green side-loads onto the left one
  and its entry tunnels under the column, as under the routed link of the template). A
  longer machine row moves the labs east to the column. Sides are padded to even lengths
  so quarter turns stay on the tile grid.
* Audit (`science_layout.audit`, hard): slots readable (`slot_shapes` for the direct pairs),
  no overlap, parts inside their reservations, every inserter between a belt and a machine,
  every pack machine's packs reach the lab head on one lane by turns only, every lab
  reachable (belt or fed lab), every stage prefix (machines x labs) powered per part and
  every built inserter's both ends built (growth never moves a built entity), lane loads at
  the full frame within the lane capacity, `layout_findings` of the machine part (the
  template's gear side-load beside its first machine is the one accepted
  `belt_wraps_machine`), green's lab entry fits.
* Descriptor: rate now (start stage) and at the full frame, in-place ceiling and its limit
  (`gear_output_inserter`: the single row's one head arm moves every gear, 30/min at the
  conservative plain-inserter budget, so 5 machines; `frame_slots` for the pairs), entities
  now (the build term) and at the full frame, raw plates of the full frame (`raw_total`)
  and of the start, kW now / full, pollution, plate draw at full frame, lane loads, growth
  slots, `green_entry`, `blue_lab_extension` (only the row takes
  `chemical_science.blue_lab_extension`) and `rebuild_ahead` (a lab part that cannot: its
  later rebuild as the row, 2 x `W_ENTITY` per lab-part entity + `W_AREA` x the row's 512
  tiles; scored with `W_REBUILD_AHEAD`).
* Library: section `science_block` of `data/layouts/automation-science-pack.json`
  (`python -B tools/layouts.py build automation-science-pack`), one entry per niche
  `aspect / machines / labs`; `python -B tools/layouts.py show science-block [--frame N]`
  draws every candidate (start stage, `:` = reserved slot) and ranks them in
  `science_layout.contexts()`.

Selection on the real catalog, request 6/min, default demand:

```
frame 6, square 40x40 site, bus W     sci-single-row 332, sci-single-bothsides 381, sci-single-chain 388
frame 6, shallow 44 along x 28 deep   sci-direct-row 387 (the single row's 32 tiles do not fit the depth)
frame 6, narrow 26 along x 44 deep    sci-direct-row 347
frame 2, square 40x40                 sci-direct-row 263, sci-direct-bothsides 282, sci-single-row 288
frame 2, shallow 44 x 28              sci-direct-bothsides 282, sci-direct-row 295 (turned, port 48)
frame 6, 48x48, horizon 60/min        sci-direct-row (its ceiling 36/min passes the row's gear arm 30/min)
```

### Green science (`green_layout.py`)

Two audited candidates, ranked by `green_science.planned_green_layouts` (one probe allocation
each: plate ports' `site.fit` cost + `W_PORT` x the pack port's distance to the lab entry +
`layout_selection.score`), both placed (allocation names `logistic_science#i` and
`logistic_science_split#i`, `settle_green` keeps one), best first, the record on every core:

| candidate | geometry | start | growth story |
|---|---|---|---|
| `cl-am1-4x2t-...` (cluster) | one pack machine + one machine per intermediate, all direct insertion, plates on one two-lane belt | 6 machines, 23 x 14, 5/min | none in place: a second pack machine flips the gear / inserter / belt edges to belts (`grow_decision`): a second cluster or a rebuild |
| `green-split-6` (split row) | the inserter cluster (cable -> circuit -> inserter <- gear) and the belt cluster (gear -> belt, mirrored below) as their own direct-insertion clusters sized for the frame, dropping onto one intermediate belt (inserter on one lane, belt on the other: the inserter cluster's port tile feeds straight into the belt cluster's, no side-load), a pack row along it (1 built, 5 slots reserved), packs to a port at the far end | 7 machines, 46 x 16, 5/min | in place up to 30/min (each sub-cluster's one output arm, `inserter_rate`) by building the reserved pack slots; beyond: a second split row |

Audit of the split (`audit_split`): no overlap, every inserter between a belt and a machine,
the two intermediates on different lanes, every pack slot (built or reserved) reaching the
output port, the pack row's poles one chain, the sub-clusters' rates and the pack arms carry
the frame; each sub-cluster also passes `audit_cluster`. The split's ports: three plate inputs
(the inserter cluster's iron and copper, the belt cluster's iron, each its own tap) on the bus
edge and the pack port opposite.

Default: the horizon is the request (no green growth capability builds reserved slots yet), and
the split's ceiling is not credited (`capability: not_implemented`), so the cheaper cluster wins
unless the site favours the split (pack port much nearer the lab entry). With a growth
capability the in-place slots would count (`demand_for`'s horizon). The inserter produce row
keeps one geometry (its tree is a chain; the split's inserter sub-cluster is the same cluster).

Not yet: twin machine rows sharing one plate belt. Their two output belts put red packs on
both lanes of the lab belt (an inserter always drops on the far lane, a side-load moves
items to the side they come from), which green's lane check refuses; it needs a lane
merge that keeps one lane (or green on its own belt) first.

Examples (`python -B tools/layouts.py show <item>`, real catalog; letters: G gear,
B belt, A magazine, E engine, P pipe, `@` input, `O` output, `:` reserved slot).
Transport belt, 30/min, three of nine candidates:

```
[cl-am1-2x1s-l-1000] same/square/meets/none     11x9, 2 machines, 9 belts, 98.5 plates
    @>>>>>>>>
    .+.i.+.i.
    ..BBB.GGG
    OiBBBiGGG
    ..BBB.GGG

[cl-am1-1x2t-b-0001] adjacent/flat/meets/none   9x13, 2 machines, 15 belts, 108.5 plates
    @>>>>>v
    .+.i..v
    ..GGG.v
    ..GGG.v
    ..GGG.v
    ...i.+v
    ..BBB.v
    ..BBBiv
    ..BBB.v
    .+.i...
    ...O...

[row-am1-default] opposite/square/meets/in_place   22x16, 3 machines, 45 belts, 257.5 plates
    @>>>>>>>>...........
    .iii.iii............
    .GGG.GGG............
    +GGG+GGG+...........
    .GGG.GGG............
    .ii..ii.............
    >>>>>>>>>>>>>>>>>>..
    .........@>>>>>>>>..
    ..........iiL.......
    ..........BBB.:::...
    .........+BBB+:::...
    ..........BBB.:::...
    ..........iii.....+.
    .........>>>>>>>>>iO
```

Magazine, 10/min: the winner per context (score in tile-equivalents):

```
selection, low demand (the request, no growth expected):
  bus W, consumer N              cl-am1-1x1s-r-00 44.65, cl-am1-1x1s-l-00 50.95, row-am1-none 52.55
  bus W, consumer E (opposite)   cl-am1-1x1s-b-00 60.65, cl-am1-1x1s-r-00 61.65, row-am1-none 69.55
  bus W, back to the bus         cl-am1-1x1s-l-00 29.95, cl-am1-1x1s-b-00 36.65, cl-am1-1x2t-l-01 42.45
selection, high demand (4x over the horizon):
  bus W, consumer N              row-am1-default 124.19, row-am1-none 178.15, cl-am1-1x1s-r-00 273.76
  narrow: bus W, 12 deep         row-am1-none 167.15, row-am1-default 178.19, cl-am1-1x1s-b-00 261.76
```

Engine unit, 6/min: twin-sub-row rows (`none` 20x22, `default` 28x22 with 2 slots up
to 12/min, `max` 44x22 with 6 slots up to 24/min) and a 3-machine cluster (18x10,
3/min with assembling machine 1: short of the request, so it only wins where nothing
else fits).

## Mine output trunk (`mine_trunk.py`)

A mine does not leave a patch in many branches. Cause on eval-2874201674: three
capabilities each planned their own drills and belt on one coal patch -
`power`/`power_coal_feed` (burner drill + belt east to the boiler, later swapped to an
electric drill by `electric_coal`), `mining_outpost` coal (paired bank, exit south to the
smelting district; the district records "existing power feed remains separate") and
`dedicated_materials` coal (`material_supply.py`: an *independent* pair planned with every
existing bank as a keep-out, so it landed elsewhere on the patch with its own splitter hub
for brick/steel coal). Their belts then crossed.

Rule:

1. One mining block per patch: the patch's oldest proven non-material bank
   (`mine_trunk.patch_bank`, patch = within `PATCH_REACH` of the resource seed), grown in
   place along its reserved strip by `mining_outpost` (`prepare_extension`). A new consumer
   does not open a second drill group. Burner starter cells with chests are exempt (retired
   by `starter_retirement`).
2. The bank's output port (its facing chosen by the site fit, `mining_site.site_exit`) is the
   head of the trunk; `trunk_bus` models it as a one-lane planned bus with the next lane
   reserved on the side away from the taps. Lanes: `lane_count = ceil(demand / belt rate)`
   (`BELT_RATE_PER_MINUTE`, both belt sides); 2+ lanes get an input-balanced merge at the
   patch edge (`balancer_plan`: 2-2 = one splitter; more lanes are reserved and recorded,
   docs/v2/BALANCERS.md).
3. Consumers take from the trunk with priority splitter taps built by the bus tap geometry
   (`planned_tap.layout` / `candidates` on a `BusView` of the trunk): one trunk belt is
   replaced by the splitter, the branch leaves sideways in two tiles, never crossing a lane;
   tap windows keep `WINDOW_ROWS + WINDOW_GAP` rows apart. `serve="first"` gives the branch
   output priority (power's coal), `serve="after"` serves the downstream consumer first and
   the branch takes the rest (materials).
4. Demand: each tap records `demand_per_minute` and the patch's first-consumer demand in
   `mine-trunks.json`; the observe receipt `mine_trunks` compares the total with the patch's
   proven nominal supply and `mine_trunk_nodes` proposes a `mining_outpost` target that grows
   the same bank (priority -53, after critical plate growth).
5. Cut-over (`mine_trunk.cutover`): the replaced trunk belt is first removed from its owner's
   saved plan and board copy (`release_trunk_belts`: smelting district, mining banks), every
   item is carried, then the one belt is mined by unit and the splitter built and configured.

Wired today: `dedicated_materials` (coal and stone, also the chemistry purpose) taps the patch
bank when one exists (`material_supply.TRUNK_MODE`; coal gets a two-output even hub splitter
for `coal` / `brick-coal`). A supply prepared before this rule keeps its own bank. Before any
consumer opens its own group the trunk is searched further (`plan_tap`): every straight run
of the observed trunk (`trunk_segments` follows its turns and passes built tap splitters; a
belt fed from behind is another line and ends the trace), first `ATTEMPTS` rows per side,
then `EXTENDED_ATTEMPTS` more. Only when all of that fails is the old independent pair
planned; the entry records `second_group_reason` and the observe receipt lists it under
`mine_trunks.independent_groups` (a recorded exception to retire later, not the design).

Power (`power_coal_tap.py`, capability `power_coal_tap`): once the boiler is belt-fed,
`electric_coal` succeeded and the coal patch has a proven bank with an observed trunk
(receipt `mine_trunks.power_tap`), one node moves the boiler feed onto the trunk: a
`serve="first"` tap (branch output priority) and a branch belt that side-loads onto a straight
belt of the existing feed. The old feed keeps running until trunk coal stands at the join with
a fuelled boiler, a powered bank drill and both engines for 30 game seconds; then the builder's
`coal_plan` is replaced (branch + kept feed belts + poles, `drill` = a bank drill, template
`power_trunk_tap`), and the old drill, its arm and the belts upstream of the join are mined one
guarded `mine` each, only while the boiler holds fuel (`obsolete.REGISTRY`). Before a bank
exists the hand-fed boiler and the starter feed stay as they are.

Smelting fuel stays the trunk's main output (the downstream consumer of every tap).

Whole-patch grid (`mining_layout_policy.patch_grid`, Mining Patch Planner "simple"
principles 1, 2, 5; a generalisation of the paired bank, not a second planner): the patch is
covered by row pairs of the `mining_pair` shape stacked `BANK_PITCH` apart across the flow
(drill row, shared collector, drill row; the free tile between pairs is the pole / service
lane), columns `PAIR_PITCH` apart, every collector flowing to one patch edge. The edge is
the flow's downstream side, which the site plan chose from where the consumers are
(`mining_site.site_exit`). A drill stands where its footprint is free of water, cliffs
(native survey `forbidden_tiles`) and other blocks and its mining area holds only the
resource, at least `GRID_MIN_ORE_TILES` tiles; trees are ignored (cleared). The lattice shift
is searched over one drill pitch (4 x 8 offsets) and scored by ore tiles covered, then fewer
drills, then less ore unreachable. `prepare_new_bank` grids the patch first, makes the grid's
lattice the bank's policy grid and plans the bank as one grid row pair (`candidate_sites(grid=)`):
per row the walk goes inward from the output edge over the row's full pair columns
(`pair_anchors`: both drills clean) to the first whose columns pass the drill checks, whose
collector leaves the ore within `MAX_EXIT_CELLS` through this patch's ore only (the cell ahead of
the port free of reservations) and that stays inside the plan's mining zone; rows rank by ore of
the poorer drill row, growth up to `GRID_GROWTH_ENOUGH` pairs, then exit cost. The row's growth is
reserved up to `MAX_PAIRS` columns or the zone edge; the bank keeps the record as `patch_grid`
(`row`, `inward_columns`, per row `start` and `full_pairs`). Cause (fleet all-1113-2155): the
grid's sites started at a row's edge-most column, which on a ragged edge or beside a starter cell
holds no full pair, so 32 of 36 banks fell back to the lattice. On the recorded patches of that
round (site-map + end-state board reservations, the bank's own exit side and pair count) a grid
bank is found for 29 of 36 banks (before 8; the rest have rows with too few full pairs on that
flow and take the lattice), on seed 2150100594's three patches in all 12
(patch, flow) cases (before 10). Building is staged: the drills nearest the output edge
first, then growth along the row (`prepare_extension`, the mine-trunk growth node). A later
bank on the patch keeps the lattice (`MiningOutposts.prepare` passes the patch bank's
`grid_origin`), so it is another row pair of the same grid; another patch is gridded afresh.
Banks of older maps keep their plan (one row pair on the default lattice). When no grid row
pair fits (also after the relaxations, which try grid rows first), the historical lattice is used
and the record says `fallback`. Not yet: the grid's
footprint is not a board reservation of its own (the ore rule already keeps other blocks off
it), and the next row pair is placed by `prepare_connected_bank`'s search, not picked from the
grid record.

Mining patch family (`mine_layout.py`, library `data/layouts/mining-patch.json`,
`python -B tools/layouts.py show mining-patch`). The whole-patch frame is a genome family like the
produce rows and science blocks: `MineGenome` = `spacing` (`simple`: the paired bank above, pole in
each drill gap, one service tile between pairs; `compact`: drills touching, one pole per two
columns on the collector with an underground pair under it, no service lane; `sparse`: drills
spaced so mining areas tile without overlap), `edge` (output flow), `merge` (`saturate`: a
collector joins a trunk belt head-on or side-loads onto its near lane while that lane has room,
else opens the next belt: 2 belts = the 2-2 splitter, more reserved; `single`: one trunk belt,
the rest clipped), `start_pairs`, `drill` (electric; the big drill only on request, its raw cost
is not comparable yet; never the burner drill). `frame` rebuilds the frame on the patch survey
(lattice shift searched per spacing; a collector tile on water, other ore or a reservation cuts
off the drills behind it), `audit` is hard (tiles unique, clean drill areas, nothing on forbidden
/ foreign / reserved tiles, every drop on its collector, every drill powered and each row's poles
one chain, collectors continuous to the port, trunk lanes within capacity), `describe` gives ore
covered / unreachable, drills, rate now / full (lane- and trunk-capped), belts / undergrounds /
poles, raw now / full, kW and pollution now / full, `frame_rects`, off-ore frame tiles (the area
term: ore is no other block's ground), trunk belts and balancer, growth steps. The plates and
build terms count the start stage (the frame grows only on demand). A test checks that the
`simple` frame's drills are exactly `patch_grid`'s.

Only `simple` / `saturate` / electric is `buildable` (what the bank builder lays out); the rest is
ranked offline. `prepare_new_bank` calls `mine_layout.choose_bank` after the grid: options are the
buildable genomes on the planned edge with the requested start or one pair more (build-ahead),
scored by `layout_selection.score` with `demand_for(count x drill rate)`; the start size changes
only when exploration picked the other option. The bank keeps `layout` (variant, genome,
descriptor, terms, runner-ups, `built_pairs`, `chosen_tick`, `verified_tick`; the fleet report
takes the proof from the bank's `ready` / `output_evidence`). Ragged 40 x 24 reference patch, real
catalog, per start pair 60/min:

| candidate | drills full | rate full /min | trunk | start raw | start entities |
|---|---|---|---|---|---|
| `mine-simple-E-sat-s1` | 69 | 2070 | 4 belts (reserved 4-4) | 71 | 14 |
| `mine-compact-E-sat-s1` | 105 | 3150 | 4 belts | 101.5 | 14 |
| `mine-sparse-E-sat-s1` | 45 | 1350 | 3 belts | 72.5 | 15 |
| `mine-simple-E-sin-s1` | 69 | 750 (one belt) | 1 belt | 71 | 14 |

Selection there: consumer E 60/min -> `mine-sparse-E-sat-s1` 74.95 overall, buildable
`mine-simple-E-sat-s1` 77.45; consumer S, horizon 600/min -> sparse S 70.5, buildable simple S
85.5; consumer W behind the patch, 120/min -> sparse W s2 88.15, buildable simple W s2 92.55 (two
pairs now: one pair is short). Sparse leads offline on fewer off-ore frame tiles; building it needs
the bank builder, contract and extension rule for its pitch (not done).

Patch frame and neighbouring patches (`mining_border.py`). Cause on eval-4177156614: a stone
patch touches the iron patch's lower-left side. Every mine record was a rectangle: the bank's
`reserved_area` was the box of its growth footprint, exit path and output access (iron: a
7 x 27 box, 7 x 12 of it drills, the rest a 1-tile exit drawn as a 7-wide strip), the site
plan's mining zones are bbox + margin, and the stone bank's exit faced east into the iron patch.
Drills were already clean (a grid cell whose 5 x 5 mining area holds another resource is not
placed; a drill mining two ores puts both on one belt). Rule now:

1. The grid is the set of drill cells whose mining area holds only this patch's ore and whose
   body, pole and collector tiles stand on no other ore (the native ore rule would refuse them);
   each row pair's extent is its own valid columns. `patch_grid` records `frame_rects`: the
   row-wise polygon (`tile_rects`) of every row's drill bodies, poles and collector to the edge.
2. A new bank reserves `reserved_rects` = its growth footprint + the exit and output-access
   tiles outside it (`reserved_area` stays the bounding box for every non-mining planner, so
   nothing else loosens). Mining checks (publish conflicts, `planned_keepouts`, the
   `registered` test, `MiningOutposts.prepare` keep-outs) compare rects (`block_rects`; a bank
   without `reserved_rects`, i.e. every older map, keeps its rectangle as a legal reservation).
   The bank also carries `patch_frame` (the grid's `frame_rects`), published on its board block.
3. Border, assigned once: an ore tile belongs to the patch whose resource it holds. A patch of
   another resource is planned with the neighbour's `patch_frame` and rects as keep-outs
   (`neighbour_reservations`; a patch's own frame is no keep-out, its later rows grow into it),
   whichever is planned first. Tiles where a drill of either side would mine both ores are the
   neutral gap: neither side places a drill there (rule 1), and both frames keep off the other's ore.
4. Edge: a port with another resource's ore within `FACING_REACH` tiles ahead ranks after every
   port that has none (`candidate_sites`, `faces_neighbour`); when the chosen bank still faces a
   neighbour, `MiningOutposts.prepare` tries the zone's other sides and takes the first that does
   not (`edge_turned_from_neighbour`). A trunk never crosses other ore: belts on ore are refused
   by the native ore rule and the exit stops at `mining_exit_foreign_resource_or_reservation`
   (so the "cross perpendicular, recorded" case cannot arise today).
5. Interleaved ore (another resource on both opposite sides of a tile within `MIXED_REACH`, at
   least `MIXED_ZONE_MIN_TILES`): `mixed_ore_zone` (tiles, bbox, resources, `handled: false`) in
   the grid record and in a failed plan. No normal drill goes there (rule 1).
6. Border lanes: a grid row whose border cells (area holds this ore and one foreign ore, body/
   pole/collector clean) win ore no clean drill reaches records `border_lane`. `decide_border_lane`
   marks it `mixed: [main, foreign]` only when `logistics` is researched, exactly one foreign
   resource meets it and the gained ore is at least `MIXED_PAYBACK` x the device's raw ore
   (`DEVICE_RAW_ORE`); otherwise `exclude` with the reason. The device (`filter_device`, turned
   with the flow): a splitter one tile past the row port, filter = the foreign item on the right
   (priority) output to an inserter + wooden chest (or onward to the neighbour's trunk), the main
   item on the left output. `configure_action` sets priorities and filter with the paid guarded
   `configure_splitter` (`filter` field, read back in the receipt; observations carry
   `splitter_filter`). `device_proof`: the main output carries only the main item; when the chest
   has no more room than the lane can still bring plus `SINK_MARGIN_ITEMS`, `retire_border` lists
   the border drills to stop, so the foreign item never backs up into the splitter.

Which patch and which row (`MiningOutposts.prepare`, `mining_layout_policy._attempts`): a
mine is planned whenever the patch has minable ore reachable off the ore. The patch is the plan's
(an observed seed outside the plan zone is moved onto it; a plan patch farther than
`MAX_OTHER_PATCH_DISTANCE` is skipped for the plan's nearer patches, each tried on all four sides
toward the consumer, `mining_site.patch_exits`). A bank whose port already stands off the ore is
the best candidate, not a dropped one. When no planned row fits, rows are relaxed in order: no
reserved growth pair, then a single pair, each first on the patch grid's rows, then on its lattice
and the planned one (a site already checked is not checked again). A failure carries
`attempts` and `needs` (what would have to change).

Not yet: building border lanes (the bank still places only clean cells; mixed drills need the
native ore rule and `resource_survey` to accept a declared mixed area and a bank build that adds
the device), a sorted-lane device for a whole `mixed_ore_zone`, and site-plan mining zones that
follow the patch (still bbox + margin).

Lanes and merge at the patch edge (principle 4, `mine_trunk.lane_plan`, capability
`mine_trunk_lane`, `trunk_lane.py`): when the receipt says `second_lane_required` (demand
needs two belts and one stands), `mine_trunk_nodes` proposes the lane once. The second belt
runs beside the first on the side away from the taps over the first straight run; at the first
row with a free tap window one 2-2 splitter (no priorities: input and output balanced) spans
both lanes, replacing the first lane's belt there (recorded and released first, mined by unit,
`cutover`). Its head port, one row before the splitter, is where the next row lane of the patch
joins: `prepare_connected_bank` tries it before any side-load onto the first lane. Until lane two
has consumers the splitter merges both rows into lane one lane by lane (merge by saturation);
taps then come only from the near side of that run. Three or more lanes stay reserved and
recorded (`lanes_reserved`, `balancer_plan` `reserved_n_n`). `prepare_connected_bank`'s trunk
trace passes recorded tap and lane splitters (`tap_passthrough`, `trace_corridor(passthrough=)`),
and `mining_outpost` no longer counts dedicated-material banks toward its target, extends or
builds them.

Not yet: `steam_expansion`'s own paired coal bank, retiring an old independent material group
through `obsolete.py`, consumers tapping the second lane (it needs a tap that dives under the
first lane from the far side), an N-to-N balancer generator.

## Return feed (`return_feed.py`; uses: `wood_burner_feed.py`, `trunk_return_feed.py`)

Owner, 2026-11-13: a large carried surplus is easier dumped into a chest at the start of a
belt than walked to consumers a few items at a time. One shared block puts items back onto
any line:

```
chest -> inserter -> side belt (v) -> splitter (v) replacing one straight line belt T
splitter: INPUT priority on the side (returned items first), OUTPUT priority on T's side
          (the line continues; the side output S + v stays free, never built on)
shapes:   column  chest S-3v, inserter S-2v, belt S-v    (the wood feed; reaches back along the line)
          elbow   chest S-v+2s, inserter S-v+s, belt S-v  (beside a trunk head: rows T-1..T+1 only)
inserter: direction = pickup side; burner arm seeded with 2 fuel once (fuels itself from what it moves)
splice:   owner's plan republished / released first, the one belt mined by unit, splitter built and
          configured (return_feed.splice); proof: every entity as planned, priorities live, T gone
```

Wood (`wood_burner_feed`): the column on the belt-fed coal feed, nearest the boilers.

Patch trunk (`trunk_return_feed`, one per trunk, coal / iron-ore / copper-ore): row `r >= 1` of
the trunk's first run, below every recorded tap and lane row, nearest the head first; the elbow
first, the side without taps first. Electric inserter when a standing pole of the patch bank
covers its tile, else a burner arm for coal; ore without a pole has no site. Recorded on the
trunk ledger as a `return` station (`mine_trunk.record_return`; its window keeps later taps and
lanes at rows `>= r + 3`, it is not a tap side for `lane_plan` and adds no demand) and as board
block `trunk_return_feed:<item>`; trunk traces pass its splitter (`trunk_segments`,
`tap_passthrough`). When the head has no free row before the first tap (a tap at row 1, or bank
poles and drills beside rows 1..first tap) the job blocks with
`no_return_feed_site_at_the_trunk_head`; moving the tap is not done yet.
