# Keep future coal outputs accessible

Fresh `DedicatedMaterials` coal plans now reserve two empty eastward tiles
after each published output and an independent outward turn: north for the
upper steel outlet, south for the lower brick outlet. The complete clearance
rectangle must be off resources and other reservations. Its empty cells remain
inside the canonical bank reservation and are recorded in `output_access`.
They are service space, not extra paid belts or extra coal capacity.

Hub selection includes every already-proposed core and power-pole footprint
when checking this space. Once a hub passes, the still-unbuilt bridge from the
initial bank candidate is replaced by a newly planned ordinary power bridge.
Its prospective blockers include the new output access cells and the known
output continuations of existing banks. A final footprint check and whole-site
native preflight must pass. No pole or pipe receives a resource exception.

This prevents a bank's own power construction from blocking its future output.
It proves local access; each consumer still needs its normal complete route
and crossing proof. Already registered banks keep their existing plans and
published port identities. The current coal5277 and brick-coal5278 outlets
are not moved or rotated by this change, and their60/min source remains shared.

Read-only prospective evidence at tick2319533, with every existing bank area
and live asset retained as an obstacle: an additional hypothetical coal pair
passed in5.66seconds at `[[-57,21],[-43,28]]`, with one recomputed power hop and two clear
output paths. This was a fresh-plan diagnostic; no bank, pole or belt was built.
The existing5266 blocker is not recovered by this feature. A pole move alone
would not prove a viable steel route, so no pole-migration controller was added.
