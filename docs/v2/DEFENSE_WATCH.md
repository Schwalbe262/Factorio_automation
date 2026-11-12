# Continuous defensive maintenance

`defense_watch` is a bounded resumable worker goal. It keeps ordinary ammunition
production, refills known posts and normally rebuilds known missing turrets.
It remains waiting when armed. One writer owns the world; stop and pause that
worker before starting a different goal.

Prerequisite-ready ordinary research is selected without interrupting an active
technology: wall/radar/military/repair capability, projectile damage and shooting
speed, then supported mining productivity and research speed upgrades. Native
labs consume belt-fed science. Unsupported science or missing prerequisites do
not hold defensive work. The finite policy currently covers upgrade levels1-3;
it is not an infinite research scheduler or completion proof by itself.

After ammunition maintenance, `DefenseRepairs` considers exact known owned
damaged turrets. A native survey requires a reachable service approach, live
normal ammunition, no enemy units within16 tiles, and exclusion of native enemy
turret attack range plus3. Unavailable or unsafe repair sites yield to defense.
Ordinary bootstrap collection/crafting acquires exactly one real repair pack.
The existing bounded native repair driver changes only control inputs; Factorio
changes health and tool durability. Interrupted tool claims are cleaned up
before further construction or inventory operations.

FactoryAutomaton was observed in native remote controller7, while the character
controller is1. Earlier repair bursts returned waiting with zero repaired health
and zero durability used; they are failures to repair, despite successful API
delivery. Before claiming the tool, the driver now exits remote view through
the ordinary player method and verifies the same character/controller. Native
input stops if the controller changes during a burst. This may return the GUI
camera from map view to the automation character. The API documents
[repair state](https://lua-api.factorio.com/latest/classes/LuaControl.html#repair_state)
and [exiting remote view](https://lua-api.factorio.com/latest/classes/LuaPlayer.html#exit_remote_view).
Live acceptance still requires a positive health delta with actual durability
spending; an `ok` or waiting receipt alone is insufficient.

V2 assisted repairs opt into returning the operator to its quiet native force
spawn after tool cleanup, using the existing ordinary movement action. This
avoids leaving the operator idle on the front after a repair. The same
owner/world and a clear spawn outside enemy units and native worm ranges are
rechecked before returning; uncertain replies and an operator stop issue no
additional movement. Live receipts at3547616/3548045/3548453/3549009 recorded
positive health deltas with actual tool durability spending. A later normally
owned corpse recovery transfers original stacks and may defer a spare tool to
retain the driver's exactly-one-carried-tool limit.

`defense_hardening` adds the paid mutually covering wall bastions described in
DEFENSE_HARDENING.md. A first attempt lost unit9478 before its second turret:
the controller chased an observed stock of20 even after21 real magazines had
been delivered and fired. That attempt is blocked, not commissioned. The repair
uses guarded delivery receipts, current minimum ammunition and prepaid support
equipment; each attempt must retain actual loss and ordinary rebuild evidence.

Radar and nearby-nest removal remain separate capabilities. Restocking,
geometric coverage and completed research do not prove combat victory.
