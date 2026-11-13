# Belt balancers

Reference for layout work. Only the 2-2 splitter of a mine trunk's second lane is built by a
capability (`mine_trunk_lane`, `mine_trunk.lane_plan`); the "Use in this project" section
says where the rest should be.

## The splitter is the only primitive

A splitter takes up to two belts in and puts items out on up to two belts, alternating
50/50 per item, and it keeps the item's lane (left lane stays left). When one output is
blocked everything goes to the other. Three settings change that:

- output priority: fill one side first, the other gets the rest;
- input priority: drain one side first;
- filter: one item goes to the chosen side, everything else to the other.

A balancer is a network of splitters. Priorities are not balancing: a priority splitter
is the opposite tool (it deliberately starves one side).

## What "balanced" means

An N-to-M balancer takes N belts and delivers to M belts.

| property | meaning | why it matters |
|---|---|---|
| output balanced | every output gets the same share, whatever the inputs carry | consumers are fed evenly |
| input balanced | every input is drained at the same rate, whatever the outputs take | sources empty evenly (drill rows, train wagons, chests) |
| throughput unlimited (TU) | any set of k inputs can deliver k full belts to any k outputs that accept them | no hidden bottleneck when some belts are empty or blocked |
| lane balanced | the two lanes of one belt carry the same amount | one-sided loading and one-sided taking |

Belt balancers do not balance lanes. Lane balance is a separate small device (split a belt,
side-load both halves back onto one belt from opposite sides).

## How they are built

- 1-to-2 and 2-to-2: one splitter.
- Power-of-two sizes (4-4, 8-8): a butterfly network. Stage 1 pairs neighbours, stage 2
  pairs belts two apart, and so on, log2(N) stages with N/2 splitters each, so every input
  has exactly one path to every output and each path halves at each stage.
- That minimal network is balanced but throughput limited: with only some inputs full and
  only some outputs open, two flows can need the same internal belt. Making it TU costs
  extra stages (two butterflies back to back is the general construction; 4-4 needs 6
  splitters instead of 4).
- Other sizes (3-3, 6-6, 3-2 ...): build the next power of two and loop the unused outputs
  back to unused inputs, or drop unused inputs. Loop-backs make the ratio exact only in
  the steady state.
- N-to-M with N != M: balance to max(N, M) and merge or leave outputs unused.
- Undergrounds are used to cross belts inside the network; the cost is splitters plus
  crossings and the length grows with log2(N).

## When a balancer is needed and when it is not

The main case (owner, 2026-11-13): one belt carries everything today, but later a flow
needs N parallel belts for bandwidth (`N = ceil(rate / belt throughput)`), and those N
belts are kept even with a balancer. Every multi-belt flow in a plan (mine trunk, bus item,
block feed) therefore carries its lane count from the demand and gets a balancer where the
lanes are filled or drained unevenly.

| situation | tool |
|---|---|
| a consumer block takes from a bus lane | priority splitter tap; no balancer |
| several bus lanes of one item, taps always on the same lane | re-level the lanes after a group of taps with a simple 2-2 or 4-4 (limited is fine: nothing downstream is selectively blocked) |
| mine: several drill-row belts merged into fewer trunk belts | input balance, so every row drains and no row backs up while another runs empty; merge by saturation first (`ceil(rate / belt throughput)` belts leave), balance only if rows are uneven |
| feeding one side of a furnace/assembler row from one lane | lane balancer before the row, or feed alternating sides |
| train unloading | input balanced so wagons empty together; TU when outputs can be blocked independently |
| a belt simply carries more than one consumer needs | nothing; back-pressure already limits it |

Rule of thumb: balance where uneven draining or uneven feeding costs output; do not put a
balancer where a priority splitter or plain merging does the job. A balancer costs
splitters, undergrounds and length, so it goes through the same wait-vs-spend comparison
as any other build.

## Use in this project

- Bus taps (`planned_tap`, `planned_bus.splitter_side`) stay priority splitters.
- Mine output trunk: drill-row lanes merge by saturation; when the planned demand needs
  two trunk belts, `mine_trunk_lane` builds the second belt and one 2-2 splitter at the trunk
  head (a second row lane joins at its head port; while lane two has no consumer the splitter
  merges both into lane one). Three or more belts: reserved and recorded only.
- Multi-lane bus items (iron, copper, later circuits): a re-level stage after every few
  taps keeps the far lanes from starving.
- A balancer should be generated from (N, M, required properties) as a splitter network
  and checked by simulation in the offline harness (equal shares with some inputs empty
  and some outputs blocked), not stored as fixed blueprints. That generator is not written
  yet; it would be a layout candidate family like the others in `LAYOUT_PRINCIPLES.md`.
