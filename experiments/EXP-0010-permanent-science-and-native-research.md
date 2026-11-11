# EXP-0010: permanent red science and normal lab research

- Date: 2026-11-11; official2.1.20/default development world, speed1.
- Goal: consume genuinely belt-fed plates in permanent science machines and
  belt-fed powered labs, then complete model-selected native research.
- Hypothesis: fixed PCB cells and bounded complete-corridor routing let Luna
  request a production rate/technology without deciding every placement/frame.
- Options: reject actor-fed science budgets for permanent production. Derive
  six red AM1 for30/min requested with36/min nominal capacity, one gear AM1,
  TWO iron input inserters for72iron/min design input, separate copper/gear
  lanes and eight labs. Native catalog determines recipes and research costs.
- Site search: earlier western slots/corridors failed the normal gates;
  selected science[[16,-104],[48,-88]], labs[[16,-120],[48,-104]]. Readonly whole
  block preflight protects terrain/buildings and approves20minable trees.
  Plan includes7assemblers,23inserters,31poles,312belts,8underground endpoints.
- Routing fix: retry up to8 complete orthogonal corridor candidates after
  unsafe foreign parallel crossings, keeping original six-turn/detour gates.
  Short lateral port escapes resolve coal junctions. No maze-router fallback.

## Execution loops

`luna-automation-science30-20261112` was paused after163actions to address the
user's production-bandwidth correction. The paid partial layout stayed in
place. After EXP-0009 succeeded, Luna resumed the ORIGINAL key and retained
the same plate consumer ports, rather than creating a replacement job.

Result:171actions,0failures, proof1018137. Six actual science machine units
[1579,1583,1587,1591,1595,1599] finished[4,3,3,3,1,1] products; eight labs
[1672,1675,1678,1681,1684,1687,1690,1693] were powered and the initial proof
held3lab packs in total. A fresh check at1033772 found ALL eight labs powered
and holding2packs each. No actor crafted or inserted science or machine inputs.

Luna then selected gun-turret from nine live prerequisite-ready technologies.
`luna-research-gun-turret-20261112` emitted one normal research-selection action,
then native labs completed the technology. Proof1021932,0failures; started
1021076. Native cost10red packs; world consumption counter increased16during
the request. That world interval includes lab pack loading/partial use and is
not an assertion that this technology costs16. Source machine identities were
recorded. Research never changes technology flags or inserts/crafts packs.

## Result, unresolved and next

Native continuous raw supply, actual science production, automatic lab delivery
and one normally researched technology succeeded. Known saved construction
budget is empty after completion; it now includes the entire saved science
plan instead of reporting only local block deficits. Industrial upkeep avoids
repeated starter-cell discovery and respects automatic/retiring ownership.

This proves commissioning, NOT30/min production AND consumption for five
minutes. There is no permanent green science, mall, ammo replenishment, rail,
oil or rocket yet. Next implement loaded continuous verification, native
logistic-science/steel research and those production/defense capabilities.
Parent fixes make this an assisted development attempt, not a Luna-only score.

Validation at Part6 sample:70v2tests passed;41builder tests ran,38passed/3skipped.
Both paired proofs and science proof passed a fresh native observation. Updated
PCB drawing was rendered and visually inspected, including the northern labs.
Part6 usage is sampled separately before delivery; child counters unavailable,
and exact task share of the weekly quota is not exposed. See note.md Part6
usage event for measured parent counters/account meter, rather than estimating.
