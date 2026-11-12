# Local yellow and piercing ammunition factory

`PiercingAmmo(game,builder,catalog,perimeter,*,yellow_source=None)` exposes
`prepare(obs)`, `step(obs)`, `proof(obs)`, `upkeep(obs)`, `commissioned` and
`ensure_full_magazines(obs,count)`. The optional old source is not tapped: two
local assemblers produce yellow magazines for four local piercing assemblers.
No ammunition or steel is hand crafted by this controller.

Normal input chests receive already produced iron, copper and steel through
bounded assisted couriers. Powered inserters and two short single-item belts
perform the entire yellow-to-red chain. The fixed output is one iron chest.
The phase remains explicitly **assisted raw logistics**, not continuous belt-fed
or sustained-rate acceptance. Local yellow output cannot be collected by the
old perimeter provider, which retains its separate original producer.

The pinned recipes give60 yellow/minute from two AM1s and40 red/minute from four.
At that potential, raw demand is160 iron,40 copper and20 steel per minute.
Existing steel supply is7.5/minute and is also claimed by Engine; the new factory
therefore makes finite batches from real available stock. Nameplate40/minute is
not a guaranteed delivered rate. Producing200 new red magazines costs800 iron,
200 copper and100 steel. Input buffer limits total256 iron,64 copper and32 steel:
64 red is a material ceiling, not a claim about actual distribution or output.
The courier replenishes actual bottlenecks while commissioning; it never treats
an arithmetic estimate as the required real50 additional magazines.

The layout declares eastward yellow/red collection, a fixed output chest and two
empty eastward yellow expansion cells before acceptance. A bounded16tile grid
around productive base extents is filtered by existing PCB, resource and future
transport policy, then at most64 land surveys run in one query. Full current and
future footprints, power and ordinary paid defense coverage must pass before
construction. All17 fast inserters have planned electrical coverage.

The output chest has two accessible100-magazine slots, an absolute automatic
chest capacity of200. The real red circuit network stops machines and global
inserters at200 and reads held red products. Four isolated green networks bound
yellow buffering at each red cell. Stack overrides are one. Exact native wire
edges, opposite-color isolation, circuit conditions and chest bars are read back.
Finished goods inside machine outputs, belts and hands are reported separately;
the chest cap is not a false cap on all work in progress.

`configure_piercing_factory` is handled by `execute_configure_piercing`. It uses
the normally researched free circuit-wire UI APIs and checks all exact paid
units before configuration. Unknown configuration outcomes remain explicit.
`supply_piercing_input` is handled by `execute_supply_piercing_input`: only the
registered raw buffer/item pairs are accepted, at most50 per action. It wraps the
existing exact-slot normal deposit operation with actor, catalog, ownership and
same-query per-item buffer-limit guards. Source goods and short transfers are
conserved; neither ammo tier can be deposited by this path.

After construction and controls, all six empty native production counters are
bound before the first input transfer. Commissioning needs actual advances from
both yellow machines and all four red machines plus real red output in the owned
chest. It does not wait for a full200 buffer. Subsequent proof reports current
stock and the recorded native production witness. Damage or replacement of a
bound asset blocks; the controller does not silently adopt a replacement.

`ensure_full_magazines` returns only the guarded `take_factory_magazines` output
action, at most50 full normal magazines per call. Empty output returns a waiting
receipt. The dispatcher runs separate `upkeep` while Sweep is waiting and before
launch, then skips unrelated factory gates during combat. Partial magazines and
quality metadata are conserved by the existing strict collector. Already held
150 red magazines remain counted, so only actual additional production fills
the requested200 raid stock.

Root read-only whole preparation passed at native4082132 in4.281s on a normally
surveyed northern base site. The plan had six AM1s,17 fast inserters, seven chests,
16 local poles and32 belts, with two additional ordinary defense posts. Native
headroom was1.0853MW against a720kW reserve at3.6MW offered generation. This is
prospective preparation; actual machine production and collection are verified
by the sole gameplay writer. Focused tests include executable Lua control,
quantity, exact-unit, plate-conservation and production-counter regressions.

Actual same-key commissioning succeeded at native4137210:135 actions, zero
failures. All six paid machines advanced from empty zero-counter baselines:
yellow15826/15830 reached137/127 cycles; red15850/15855/15860/15865 reached
24/22/20/17 cycles. The83 red cycles yielded166 magazines:162 in the owned
output chest15887 and four on its belt at acceptance. Both paid local defense
posts15808/15809 were healthy and armed. A later independent native4174070
proof read200 chest magazines with the actual200 capacity and circuit stops.

The same Sweep collected50 new factory magazines without hand crafting either
ammunition tier. Its paired transfer receipt conserved3500 total source/actor
rounds. Existing150 paid red magazines remained valid stock. One atomic ten-gun
wave then killed the six original spawners and four worms by native projectiles;
see the [actual sweep evidence](NEST_SWEEP.md). This is assisted development,
not a frozen Luna-only or continuous raw-supply result.

The final controller checks commissioning proof before optional input topoffs.
Earlier live code kept supplying buffers after all machines had already produced;
the worker completed normally before this fix was loaded. A regression verifies
that future success evidence is returned before another optional courier action.

Final indexed regression gate:1013 tests,1011 passed and two ignored-asset
skips in52.071 seconds. This gate includes the existing mining, combat, recovery
and production suites; incomplete generic construction-mall work was excluded.
