# Fixed fluid output handoffs

`declare_handoff(game,catalog,obs,consumer_id,fluid,source,first_neighbor)` records
a planned consumer edge in `fluid-sources.json`. The caller owns `writer.lock`.
The published source must match exactly, including world, native unit, fixed
port, fluid and defense scope. The first neighbor is one orthogonal step in the
source's facing; a surface pipe or correctly facing underground mouth is allowed.
No pipe may exist there before its first declaration. Only one consumer may own
the edge; a later shared fluid trunk needs explicit additional output contracts.

`bind_handoff` accepts that same descriptor plus the consumer layout's saved
native `unit_number`. It checks exact force/surface/position/identity, fluid purity
and reciprocal physical fluid-box connections before marking the record bound.
Changing a declaration or replacing a bound unit fails. Retrying the same valid
declaration/binding is idempotent. These functions change metadata only and never
insert fluid or build hardware.

Basic petroleum proof permits only this exact bound consumer at its gas output.
All other unexpected peers remain failures. An unregistered pipe, changed unit,
wrong fluid or missing reciprocal connection fails executable native proof tests.
Consumer routes must separately prove land, resource keepouts, bounded pipeline
extent, underground partners and power/production. A fixed output edge does not
grant permission to cross an entire producer reservation or multiply its capacity.

Seven focused handoff tests and the nineteen petroleum tests pass, including
executed Lua for source replacement, contamination, one-way peers and declared
versus foreign consumers. Live downstream chemical commissioning is pending.
