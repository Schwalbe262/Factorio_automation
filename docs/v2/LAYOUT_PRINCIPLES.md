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
