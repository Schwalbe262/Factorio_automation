# Standalone Luna queue: development attempt, 2026-11-12

Goal: periodically invoke subscribed GPT-6 Luna medium, execute framework
functions continuously from a persistent dependency queue, and let Sol repair
framework defects. This attempt permits Sol edits and is not a frozen benchmark.

World: seed1053994892, UUID3deaaa3e-b50d-401a-b8f1-635260d3c4cf,
Space Age2.1.20, default enemies/resources, Nauvis cliffs disabled, speed1.
Previous dev-101 save and root checkpoints remain backed up and unchanged.

| Attempt | Evidence | Result |
| --- | --- | --- |
| Authenticated CLI planner | Actual `gpt-6-luna`, medium, structured read-only decision; no API key | Accepted initial five-task queue. First real plan input19343/output442 tokens. |
| Starter | 69 normal actions, zero failures; native iron/copper output and three early unlocks | Succeeded. |
| Stop and restart | Safe save ACK, game pause, then same world and same power job resumed | Succeeded; GUI remained open. |
| Power | 83 paid actions, then `starter_coal_planning_budget_exhausted` | Unresolved routing blocker; retain partial plant. |
| Defense | Zero actions; `native_gun_turret_and_normal_magazine_recipes_required` | Correctly blocked by current technology. No unlock grants. |
| Luna supply response | Queued coal starter_capacity target2; eight actions, actual machine output | Succeeded. This alone did not prove the power route fixed. |
| Completed queue identity | Planner reused an immutable task ID with different parameters | Decision rejected; completed identities and last planner error need to remain visible. |
| Supervisor review | `wait` stopped approved work; decisions used stale job state; ordinary status transitions retriggered planning | Fixed; focused supervisor/client/lifecycle/owner-binding gate60 PASS. |
| Process exit timing | Empty CIM result during exit; ACK could appear between status read and process inspection | Fixed both races. Actual saved stop, game pause and restart passed afterward. |
| Corrected planner restart | Same world, supervisor18564; actual new Luna plan accepted with no planner error | Queue can continue; fast end-to-end progression remains unproved. |

Controls: `start.bat`, `stop.bat`, `new_map.bat`, `gui.bat`, `status.bat`,
`logs.bat`, `shutdown.bat`. Status includes pending tasks and blocker reasons.
New-map and complete shutdown have simulated tests; this attempt does not reroll
the current map merely to test those controls.

Current next step: repair the generic coal route selector, then let Luna continue
normal prerequisites. Read-only diagnosis found a legal land path, but the
coal-cell corner output was misclassified as a foreign incoming belt. A straight
paid exit is being tested; no live route success is claimed here.
No first-hour, sustained-rate, rocket or all-seed success is claimed.
Per-decision usage is recorded by the controller; root cached-inclusive part
intervals and the shared account weekly meter are recorded under runtime/usage.
