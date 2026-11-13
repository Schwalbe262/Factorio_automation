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
  can carry the tree (three solids, a two-level intermediate such as the
  inserter), a cluster line ending in a stock chest (`cluster_line`).
* `logistic_science` on a planned world: one cluster on an iron and a copper
  bus tap, packs routed to the lab entry; no district branch (blocked
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
  count within the lane budget), cluster `grid`, `turned` (input belt on one side or
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
  and `pollution_per_min` (catalog `emissions_per_minute` when exported; today's
  catalog export has none, so it shows `-`).
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
* Exploration (off by default): `<runtime>/layout-settings.json` `{"explore": true}`
  (optional `top_k`, `margin`) or `FACTORIO_LAYOUT_EXPLORE=1` in the environment of
  `fleet.py start`. A seed from the world id and block picks among the best `top_k`
  feasible candidates within the margin. `fleet.py report` prints "layout variants
  used": block, variant, runs, median build game-seconds, proofs, explored, kW,
  pollution.

Used by `produce` (and through it the planned `ammo_production` and
`engine_production` rows). `logistic_science` records its genome and metrics; its
tree has one cluster topology under this generator (the pack machine needs two
belt-fed neighbours and a free side), so there is nothing to rank yet. Smelting banks
and the science / lab rows are single-variant generators; giving them genomes needs
the same three things: parameters on the generator (bank length / pairing, lab row
length and entry side), a descriptor, and their own place loop calling
`layout_selection.score`.

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
lattice the bank's policy grid and plans the bank as the grid row pair whose edge-most columns
fit (`candidate_sites(grid=)`), reserving that row's growth up to `MAX_PAIRS` columns; the
bank keeps the record as `patch_grid`. Building is staged: the drills nearest the output edge
first, then growth along the row (`prepare_extension`, the mine-trunk growth node). A later
bank on the patch keeps the lattice (`MiningOutposts.prepare` passes the patch bank's
`grid_origin`), so it is another row pair of the same grid; another patch is gridded afresh.
Banks of older maps keep their plan (one row pair on the default lattice). When no grid row
pair fits, the historical lattice is used and the record says `fallback`. Not yet: the grid's
footprint is not a board reservation of its own (the ore rule already keeps other blocks off
it), and the next row pair is placed by `prepare_connected_bank`'s search, not picked from the
grid record.

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
