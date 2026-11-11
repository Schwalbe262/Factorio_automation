# Independent chemistry coal

`DedicatedMaterials(game,builder,catalog,outposts,perimeter=perimeter,
purpose='chemistry')` retains the existing `prepare/step/proof(obs,{'resource':'coal'})`
API. The default constructor still uses `material-supply.json` and the original
`dedicated-material:<world>:<resource>` identities. Chemistry uses
`material-supply-chemistry.json`, an explicit purpose marker and
`dedicated-material:<world>:coal:chemistry`. A copied default checkpoint, wrong
purpose, changed catalog or incompatible bank fails closed. Chemistry accepts coal
only and requires defended coverage.

The new bank has two normally paid opposing electric drills, the existing scoped
local collector/short resource exit, and an ore-free splitter hub. Source ports and
all normal output aprons are preserved by the shared mining and power preflight.
Six bank attempts share a twenty-second native planning budget. Existing mining
banks and industrial/main-bus reservations remain hard planning constraints.
There is no tap or port relocation on the running brick or steel coal supply.

After native extraction, both collector lanes, coverage and a real normal coal
packet at the exact new outlet are proven, the material registry gains only
`plastic-coal`. Existing `coal`, `brick-coal`, `steel-coal`, stone and other entries
remain unchanged. The hub's second `chemical-coal-reserve` outlet and its clear
apron are reserved internally and remain unpublished. Both outputs share one
**60 coal/min catalog nominal bank**, rather than multiplying its production.
No measured sustained output is claimed. Missing/replaced bound chemistry poles,
drills, belts or the splitter block recovery/reconstruction instead of silently
adopting another unit.

Read-only native preparation passed at tick **2609836** in 6.95 seconds: core
collector `(-49.5,24.5) E`, plastic outlet `(-45.5,24.5) E`, reservation
`[[-57,21],[-43,28]]`; two drills, two poles, eight belts, one splitter and one
additional armed post. The original default prepare also passed with its original
identity and byte-identical checkpoint. No chemistry runtime checkpoint was written
by that preparation. Live construction and production remain pending deployment.
