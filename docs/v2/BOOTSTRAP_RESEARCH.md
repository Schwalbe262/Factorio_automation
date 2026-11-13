# Finite early machine research

`bootstrap_research --technology gun-turret|stone-wall|electric-mining-drill`
requires a verified `power` job and reuses the registered `startup_science` PCB
cell when present. Logistics research is unnecessary. If no cell is registered,
it uses the ordinary native survey/reservation and paid builder after Automation.
It bridges early defenses and electric mining to the permanent ore/smelting/
science districts. It never handcrafts science or changes native research flags.

`BootstrapLogistics(game, bootstrap, builder, catalog, technology='logistics')`
keeps the existing logistics checkpoint and behavior by default. Other targets
use separate `bootstrap-research-<technology>.json` files. They neither copy nor
reset the logistics allowance. Current native prerequisites, Automation and
enabled cell recipes are required. Existing machines and a lab must be connected
to electricity and have positive native energy. This path does not first invoke
global starter upkeep or `ensure_power`, so remote damaged coal/chest recovery
cannot starve an otherwise powered local defense-research cell. It cannot
research using an unpowered cell. Supported targets must have a fixed count
of 1–100 units, each consuming only one red science pack.

The existing cell geometry, ports and registered plan remain unchanged. Before
funding, the target checkpoint pins world, catalog, plan hash, machine/chest/lab
units and the red machine's `products_finished` plus native science consumption.
A changed machine, recipe, counter, world, catalog or plan blocks completion.

Raw budgets come from the installed recipes. In the current catalog:

| Target | New machine-made packs | Iron plates | Copper plates |
| --- | ---: | ---: | ---: |
| gun-turret | 10 | 20 | 10 |
| stone-wall | 10 | 20 | 10 |
| electric-mining-drill | 25 | 50 | 25 |

The actor collects real plates, normally deposits finite input buffers, collects
the cell's actual output and inserts only that collected allowance into a powered
lab. Gear/science production occurs in the paid machines. These are assisted
finite buffers, without a sustained science throughput claim. The ordinary
transfer receipt's actual `moved` count is saved before clearing the job's durable
intent. Partial transfers leave only the original remainder available; missing
or invalid receipts preserve intent and require reconciliation. A zero transfer
is an explicit blocker, never an unlimited retry or fresh budget.

Funding is incremental (`BootstrapLogistics._incremental_funding`): whatever the
hands hold of an unfunded input, at least `FUND_CHUNK = 10` plates or the item's
whole remainder, is inserted at once (never more than the remaining allowance),
so the machines start on the first plates. The research starts and the courier
carries packs while the rest arrives. With nothing worth inserting the job
collects, else waits naming the plate, with `evidence.needs` listing every
unfunded remainder: that is its critical-path claim (PLANNERS.md "Critical path
and item allocation"), so slack jobs leave those plates alone. The audited
diversion recovery only runs once the batch is fully funded (an empty cell while
plates trickle in is the plate wait, not a diversion).

Proof requires the target natively researched, sufficient new production on the
same red machine and positive native science consumption since the baseline.
Historical cell production cannot satisfy a new target. If research completes
early, only the remaining finite input allowance is funded and completion waits
for the new machine proof. A target already researched before any target state
returns an explicit `already_researched` proof without funding or new metadata.

`StarterExecutors` lazily binds each target and forwards known action receipts
through `record_action`; no new game action or runtime handler is needed. The
tests cover native gates, finite and partial funding, original unit/plan pins,
counter proof, receipt uncertainty and unchanged legacy logistics behavior.
