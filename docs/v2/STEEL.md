# Automatic steel preparation

`steel_production.py` provides `steel_rates(catalog)`, `steel_core(catalog,x,y)`,
`audit_steel_core(plan,catalog)` and `SteelProduction(game,builder,catalog,
iron_source=...,coal_source=...)` with `prepare(obs)`, `step(obs)` and `proof(obs)`.
No live steel site has been surveyed or built by this change.

The default core has two opposing stone furnaces, four powered ordinary
inserters, two poles,33belts and one input splitter. Iron and coal side-load
opposite sides of a straight belt before the splitter distributes both lanes
to both furnace rows. Eastbound mixed inputs declare iron LEFT/north and coal
RIGHT/south. Independent upper/lower output inserters fill opposite steel
lanes; a final output splitter is unnecessary. Furnaces select steel normally
from belt-fed iron after native steel-processing research. No actor fuel,
iron/ore insertion or handcrafted steel is emitted.

With the saved 2.1.20 catalog, 5 iron plates become 1 steel plate in 16 recipe seconds; stone
furnace speed1. Two furnaces rate7.5steel/min and require37.5iron/min. Exported
maximum furnace usage1500J/game tick and coal4MJ give2.7coal/min at maximum
catalog usage. This is a design calculation; actual fuel and sustained steel
throughput need native measurement. Inserter electricity is supplied separately.

At the example anchor(82,-40), fixed typed ports are iron(78.5,-44.5)S,
coal(77.5,-40.5)N and steel(92.5,-37.5)E. Three furnace pairs are reserved;
only one pair is initially built. Activating two or three pairs preserves every
existing entity, port and reservation, giving rated15/22.5steel per minute.
Candidate anchors(82,-40),(94,-40),(82,-24) are suggestions pending native
land/resource/reservation, transport and power preflight.

Preparation requires an **already installed** iron output with item
`iron-plate`, direction`output`, facing, position, exact`unit_number`,
priority`surplus`, and `priority_splitter={position,unit_number}`. Native checks
require that NORTH splitter's input/output priorities both be`left`, preserving
the continuing science trunk before steel. Coal requires a separate pure typed
`coal` output with facing, position and exact unit. Existing source contents
must match their types. Missing handoffs return explicit unsupported reasons
before construction; this controller never edits or recovers a running bus.
The parent must install and validate any new bus/coal branch first.

`prepare` reads only. It audits the core, surveys the entire growth reservation,
plans complete bounded source and power adapters, and runs whole native
preflight. It passes no ignored units and authorizes no existing-asset recovery.
Only a later explicit `step` saves steel state, publishes its PCB reservation
and emits ordinary paid construction actions. Proof requires native recipes and
production in every furnace, coal/burning fuel, powered inserters, unchanged
source identities and surplus priority, exact underground partners, correctly
typed mixed input lanes and real steel on both native output lanes.

If an automatic furnace releases its recipe while idle with full output,
acceptance additionally requires positive expected output and pure compatible
input/fuel. The exact native unit's normal output, raw input and fuel slots are
checked in the same query as lane evidence. An unset recipe alone is never
production evidence; wrong recipes, products, qualities and mixed raw items fail.

Tests cover catalog-driven recipe/fuel calculations, footprint/feed/output
audits, stable expansion, missing/unsafe handoffs, read-only preparation and
commissioning gates. No sustained-rate, capacity-upgrade or rocket acceptance
is claimed. Per-agent token/quota usage is unavailable; the root records meters.
