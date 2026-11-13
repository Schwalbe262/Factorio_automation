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
