# Early defense and recovery, 2026-11-12

World: seed1053994892, default enemies/resources, cliff-free Nauvis, speed1.
This is Sol-assisted framework development, not a frozen Luna-only benchmark.

The initial power and Automation jobs succeeded. Later Logistics reached174
actions and blocked after three failed chest placements. Native inspection
found remnants of the dedicated coal drill and several starter cells. No live
assembling machines remained at the inspection time. The world was saved and
paused at305521 while the execution gaps were repaired; no new map was created.

| Hypothesis or attempt | Evidence and result |
| --- | --- |
| Fuel upkeep starves construction | Not demonstrated.57 paired actions in209.189s included12 successful fuel transfers and24 stock collections moving112 items, zero empty collections. |
| Iron supply limits construction | Native measured14.995iron/min; one burner cell's catalog bound15ore/min. Furnace waited for ore while copper output held100. Coal measured124.649/min. |
| Queue dependency errors mean real changed work | Ordering and duplicate edges are equivalent. Normalize them, expose completed accepted dependencies, reject actual started edge changes with a node/diff. |
| New early research can wait for Logistics | Rejected: defense would depend on the blocked chain. The new bridge requires verified Power and current native Automation, recipes and powered local machines/lab; remote repair does not run first. |
| Destroyed chest site needs material deletion | Rejected. Existing conserved ground pickup moved exactly1 normal coal into MAIN; native default can_place changed false->true. Direct-cell construction now uses that same cleanup path. |

New `bootstrap_research` accepts only gun-turret, stone-wall or
electric-mining-drill. It reuses the startup science plan or normally reserves
and builds it, then pays finite recipe-derived raw inputs. Exact machine/lab/
port identities, new production counters and real science consumption are pinned.
Known partial transfers record only actual moved items before durable intent is
cleared. Unknown receipts remain blocked. There are no science grants or native
technology setters.

The scheduler can preempt a queued waiting/pending job only when another
approved ready task has strictly higher priority. It preserves the old key and
paid plan. Automatic resume requires its own exact confirmed pause, unchanged
world/actor/job and no subsequent explicit control in the native job journal.
It does not invent resource-specific priorities. The observed Logistics120 vs
Iron115 would therefore retain Logistics until Luna changes its decision.

Validation: indexed V21342total/1340passed/2ignored-reference skips54.681s;
149 focused tests passed. The corrected source was loaded by supervisor36860
on the same world. Native execution paused iron priority115 for stone120, then
stone120 for early turret research130. The exact paused stone job resumed after
turret research, then paused again for Luna's revised copper priority125.

The early gun-turret job succeeded at319007 in41 actions. Exact red machine1173
produced10 new packs from baseline0; normal science consumption increased10.
The target's paid finite inputs were20iron/10copper. The native target was
researched without granting items or changing research flags.

Remaining: old defense is still a zero-action blocked request despite its recipe
now unlocking. Luna chose further copper capacity instead, currently waiting for
coal. Root/MB are integrating already-approved initial defense as maintenance;
the destroyed dedicated coal source also needs normal recovery. Sustained
autonomous recovery, rocket, first-hour and all-seed success remain unproved.
