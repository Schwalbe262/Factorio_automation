# Current Handoff
- ACTIVE goal: v2 framework + GPT-6 Luna medium autonomous REAL Space Age rocket. Rocket/science/oil/rail/defense execution is still incomplete; do not mark complete.
- User requires PCB-style blocks/fixed ports/reserved orthogonal trunks/controlled crossings/expansion space; normal resource costs, engine production/research; default enemies/cliffs; installed 2.1.20.
- GitHub/clones have only v1/v2, default v2; removed 50 old worktrees after verified ZIPs+bundle in runtime/housekeeping/. Old C:/Users/NEC/Documents/Factorio dirty four files/saves preserved.
- Live world32a34974-d7cd-4ec0-9f35-13259ea32b8e at runtime/v2/dev-101; UDP34260/RCON27060; dedicated server/client, GUI visible. Read actual server.json/process/agent handles before restarting anything.
- Real starter, coal/steam flow1816ticks, lab load60kW, Automation+electric-mining-drill unlocked naturally with35 crafted/35 consumed red packs (tick215293). No auto30SPM proof yet.
- PCB migration verified tick156363:140->132 belts,13->4 local self-feed,6->4 trunk turns; preserved plant, recovered97 old entities. docs/v2/images/coal-layout-comparison.png. Full engine fuel capacity still needs expansion.
- python -B -m factorio_agent --runtime runtime/v2/dev-101 observe; set PYTHONPATH=src first. PLAYER_API.md describes jobs, worker handles, pause/resume, finite startup research, capacity and layout migration.
- Luna capacity jobs finished:4 iron/2 copper productive starter cells (235081), nominal60/30 ore/min; sustained rates/auto fuel unproved. No gameplay worker currently running; revalidate handles. /root/blueprint_references finished four UNVERIFIED references, no saves loaded.
- Next: measured rate/project-budget diagnostics, scalable electric mining + normal automated red/green science/mall/defense using PCB zoning; then oil, rail, rocket. Sources docs/v2/REFERENCE_SOURCES.md; experiments2/3/4; never read journals wholesale.
