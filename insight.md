# Factorio Insights

실제 개선이 확인된 경우만 이 파일에 시간순으로 추가한다. 단순 실행 기록, 실패 기록, 가설 단계의 내용은 `note.md`에만 남긴다.

## 기록 템플릿

```text
## YYYY-MM-DD HH:mm:ss +09:00 - Insight N

- Source loop:
- Improvement:
- Before:
- After:
- Evidence:
- Remaining risk:
```

## 2026-06-14 23:56:39 +09:00 - Insight 1

- Source loop: Loop 3
- Improvement: `research_logistics`가 이미 존재하는 lab 인접 remote steam block과 red science mall assembler를 회수해 실제 연구 진행까지 이어졌다.
- Before: `research_logistics`가 `cannot find a buildable water site for steam power`에서 멈추거나, 이미 놓인 unassigned assembler를 다시 쓰지 못했다.
- After: automation science assembler가 `automation-science-pack` recipe로 설정되고, pack을 생산해 lab에 투입했다.
- Evidence: `logs/strategy-logistics-research-20260614-145405.jsonl`, `automation-science-pack` 0 -> 1, `logistics` research progress 약 25%.
- Remaining risk: 기존 맵은 site가 너무 멀리 흩어져 있어 장기 운영에는 부적합하며, 이 개선은 planner 회수 능력으로만 보존한다.

## 2026-06-15 00:02:00 +09:00 - Insight 2

- Source loop: Loop 4
- Improvement: Automation 이후 반복 hand-carry 생산을 중단하고, missing site-to-site logistic line을 우선 blocker로 승격했다.
- Before: red science mall이 copper/gear를 멀리 떨어진 site에서 player inventory로 왕복 운반하며 연구를 이어갈 수 있었다.
- After: strategy는 `plan_factory_site`를 선택하고, `ResearchTechnologySkill`/`BuildItemMallSkill`은 720 tile `copper-plate` hand-carry를 거부한다.
- Evidence: live `no-mod-strategy` selected `plan_factory_site`, blocker `site-to-site logistic line`; research decision reason에 `refusing repeated hand-carry between distant sites` 포함.
- Remaining risk: 실제 belt/chest/train logistic line을 놓는 deterministic executor는 아직 추가로 구현해야 한다.

## 2026-06-15 00:12:36 +09:00 - Insight 3

- Source loop: Loop 5
- Improvement: 새 no-mod world 생성 기준에서 Nauvis cliffs를 비활성화하고, 흩어진 기존 save를 백업한 뒤 새 맵에서 재시작했다.
- Before: 기존 맵은 power/research/circuit/smelting site가 과도하게 흩어졌고, cliffs도 향후 배치 장애가 될 수 있었다.
- After: `cliff_settings.richness = 0`, `cliff_elevation_interval = 0`인 새 save가 생성됐고, 관찰된 cliff entity 수는 0이다.
- Evidence: `runtime/vanilla/safe-start-map-gen-settings.json`, 새 save `runtime/vanilla/saves/no-mod-rcon.zip`, initial observe `cliffs=0`, initial strategy `produce_iron_plate`.
- Remaining risk: 새 맵에서도 초반 site placement가 튀지 않도록 starter-local logistics guardrail을 계속 검증해야 한다.

## 2026-06-15 00:31:51 +09:00 - Insight 4
- Source loop: Loop 7
- Improvement: Scattered-map layout, strategy, LLM, journal, validation, and runtime traces are now preserved in a reusable training archive instead of existing only as loose ignored logs.
- Before: not recorded
- After: not recorded
- Evidence: `{"archive_dir":"C:\\Users\\NEC\\Documents\\Factorio\\runtime\\trace_archives\\20260615-003151-part75-scattered-map-traces","categories":{"layout_background":1,"layout_strategy":1,"layout_validation":1,"llm_decisions":1,"strategy_run":53},"high_value_files":61,"source_count":158,"source_loop":7}`
- Remaining risk: Needs continued validation in later loops.

## 2026-06-15 00:38:07 +09:00 - Insight 5
- Source loop: Loop 8
- Improvement: iron-plate increased by 4 during produce_iron_plate.
- Before: iron-plate = 7
- After: iron-plate = 11
- Evidence: `{"delta":4,"final":11,"initial":7,"item":"iron-plate","source_loop":8,"target":10}`
- Remaining risk: Needs continued validation in later loops.

## 2026-06-15 00:38:07 +09:00 - Insight 6
- Source loop: Loop 8
- Improvement: produce_iron_plate completed after 20 step(s): iron plate target reached: 11/10
- Before: not recorded
- After: iron-plate = 11
- Evidence: `{"item":"iron-plate","item_count":11,"source_loop":8,"steps":20,"target":10}`
- Remaining risk: Needs continued validation in later loops.

## 2026-06-15 00:40:19 +09:00 - Insight 7
- Source loop: Loop 9
- Improvement: coal increased by 13 during setup_coal_supply.
- Before: coal = 12
- After: coal = 25
- Evidence: `{"delta":13,"final":25,"initial":12,"item":"coal","source_loop":9,"target":16}`
- Remaining risk: Needs continued validation in later loops.

## 2026-06-15 00:40:19 +09:00 - Insight 8
- Source loop: Loop 9
- Improvement: setup_coal_supply completed after 17 step(s): coal supply site is active with fueled burner mining drill and output belt
- Before: not recorded
- After: coal = 25
- Evidence: `{"item":"coal","item_count":25,"source_loop":9,"steps":17,"target":16}`
- Remaining risk: Needs continued validation in later loops.

## 2026-06-15 00:49:45 +09:00 - Insight 9
- Source loop: Loop 11
- Improvement: Remote Qwen can now drive strategy while deterministic reconciliation blocks impossible item-mall work before Automation and blocks site-to-site coal belt feeds before transport-belt automation.
- Before: not recorded
- After: not recorded
- Evidence: `{"after":"no-mod-strategy --require-llm returned research_automation with guardrail_adjusted.from=bootstrap_build_item_mall","before":"remote Qwen selected bootstrap_build_item_mall while Automation was not researched","belt_path_guardrail":"connect_coal_fuel_feed is redirected until a transport-belt assembler mall is observed","source_loop":11,"tests":{"pytest":"354 passed","strategy":"42 passed"}}`
- Remaining risk: Needs continued validation in later loops.

## 2026-06-15 01:05:30 +09:00 - Insight 10
- Source loop: Loop 33
- Improvement: `research_automation` no longer stops at missing steam-power water sites on the current no-cliff map; it can use the nearest remote water site as a bootstrap-only exception while preserving starter-local preference.
- Before: live observe `power_sites_count=0`; `research_automation` stopped with `cannot find a buildable water site for steam power`.
- After: live observe `power_sites_count=20`; planner next action moved to coal prerequisite collection instead of water-site failure.
- Evidence: `{"after":{"first_power_site_distance":787.87,"first_power_site_distance_from_agent":723.47,"planner_action":"move_to near coal","power_sites_count":20,"qwen_selected_skill":"research_automation","tests":"356 passed"},"before":{"failure":"cannot find a buildable water site for steam power","power_sites_count":0},"source_loop":33}`
- Remaining risk: The first water source is far from spawn, so this exception should not justify scattering normal production sites before rail or robust logistics.

## 2026-06-15 01:37:38 +09:00 - Insight 11
- Source loop: Loop 52
- Improvement: Real-player GUI movement now verifies the Factorio foreground window, can detour around straight-line movement stalls, and treats close interaction range as successful movement.
- Before: `research_automation` reached movement but stopped at `move made no progress; remaining distance 20.04` and then `move_to timed out; remaining distance 3.95`.
- After: the same research loop progressed through coal mining, copper mining, copper furnace insert/take, burner drill fueling, and continued toward the next coal requirement.
- Evidence: `{"after":{"foreground":true,"inventory":{"coal":4,"copper-plate":8,"wood":1},"movement_smoke":"x 79.61 -> 81.24","strategy_log":"strategy-automation-research-20260614-163058.jsonl reached step 6","tests":"359 passed"},"before":{"failures":["move made no progress; remaining distance 20.04","move_to timed out; remaining distance 3.95"]},"source_loop":52}`
- Remaining risk: The bootstrap loop is still long and may expose more missing deterministic executors before Automation is researched.

## 2026-06-15 01:43:20 +09:00 - Insight 12
- Source loop: Loop 72
- Improvement: Resource-gathering movement now stops at mining range and immediately continues with the mine action instead of requiring exact resource tile arrival.
- Before: background `research_automation` stopped at `move_to timed out; remaining distance 13.97` even though the player was close enough to mine coal.
- After: planner selected `mine coal` with `radius=8` from the current live position.
- Evidence: `{"after":{"action":"mine coal","radius":8,"tests":"359 passed"},"before":{"failure":"move_to timed out; remaining distance 13.97"},"source_loop":72}`
- Remaining risk: Other non-resource move targets may still need action-specific tolerance if future loops expose exact-position stalls.

## 2026-06-15 02:01:24 +09:00 - Insight 13
- Source loop: Loop 106
- Improvement: No-mod monitoring, idle GPU filler, autopilot, strategy, and default observe paths no longer run full water/site planning scans by default, and no-mod autopilot refuses accidental connected-observer control unless explicitly allowed.
- Before: background no-mod autopilot used `FACTORIO_AI_AGENT_PLAYER=auto` with GUI movement, moved the connected `r1jae` observer, brought the Factorio window to the foreground, and idle/layout/observe paths could repeat full `collect_power_sites` scans after `POWER_SITE_RADIUS=1024`.
- After: Web UI runs as `AI` with lightweight observe and slower refresh/cache settings; no-mod idle/autopilot/default observe use lightweight mode; full planning-site observe is retried only when the planner explicitly needs missing water/lab/automation site candidates, then cached.
- Evidence: `{"after":{"active_processes":["start-no-mod-server pid=80472","web pid=58356"],"cli_default_planning_site_counts":{"automation_sites":0,"lab_sites":0,"power_sites":0},"lightweight_observe_seconds":2.146,"tests":"368 passed"},"before":{"failure":"connected observer controlled and Factorio GUI repeatedly foregrounded","idle_full_observe_period":"5-10s while stale","web_full_observe_cache_seconds":30},"source_loop":106}`
- Remaining risk: Lightweight observe still scans resources/entities and currently took about 2.1s on this map, so if lag remains noticeable the next target is resource/entity scan throttling or cached monitor snapshots.

## 2026-06-15 02:34:17 +09:00 - Insight 14
- Source loop: Loop 108
- Improvement: Agent spatial memory now stores a compact sparse feature graph instead of raw per-tile or per-sample map data, so LLM/planner code can reuse known resource clusters, factory zones, and water anchors without immediately repeating expensive site scans.
- Before: The no-mod controller either used the current observation directly or reran full planning-site scans when water/lab/automation candidates were missing; there was no persistent spatial memory for known map features.
- After: A live lightweight observe with 2,616 resource samples and 208 entities produced a 10,412-byte `runtime/world-map-memory.json` containing 15 resource patches, 3 factory zones, 13 sparse index cells, and 18 indexed features.
- Evidence: `{"source_loop":108,"tests":"375 passed","live_observe":{"resources":2616,"entities":208,"planning_cached_from":null},"world_memory":{"encoding":"sparse_feature_graph","bytes":10412,"resource_patches":15,"factory_zones":3,"sparse_index_cells":13,"sparse_index_features":18},"full_water_scan_run":false}`
- Remaining risk: Water anchors are only populated after a legitimate full planning-site scan; stale memory is guarded by TTL and final placement must still pass live `can_place_entity` validation.

## 2026-06-15 02:54:25 +09:00 - Insight 15
- Source loop: Loop 111
- Improvement: Starter steam power planning no longer places isolated remote water blocks that cannot connect back to the starter factory.
- Before: Live `research_automation` placed offshore pumps at `{x:55.5,y:-814.5}`, `{x:51.5,y:-822.5}`, and `{x:143.5,y:-821.5}`, plus a boiler at `{x:49.5,y:-821}`, even though those sites were hundreds of tiles from the starter factory and could not power it with available poles.
- After: The bad remote entities were recovered, a live full planning-site observe still found `power_site_count=20`, but `SetupPowerSkill` returned `action=None` with the explicit remote-water blocker instead of building another pump.
- Evidence: `{"source_loop":111,"tests":"377 passed","cleanup":{"boiler":1,"offshore_pump":3,"power_entities_after":[]},"planner_after":{"power_site_count":20,"action":null,"reason":"cannot use remote water for starter steam power until a connected power corridor or co-located remote factory site exists"}}`
- Remaining risk: The current map may still lack a practical starter-local water source; progress now requires a connectable power corridor, a co-located remote factory plan, or a better start rather than isolated remote steam.
## 2026-06-15 03:02:11 +09:00 - Insight 16
- Source loop: Loop 112
- Improvement: Nearby-water recognition is fixed for starter steam planning, and the live factory now has working starter-local steam power.
- Before: The full planning scan reported remote buildable water first, around `{x:140.5,y:-826.5}` at about 838 tiles from the starter anchor, even though the map had a visible nearby lake.
- After: Direct local scanning found a buildable starter steam layout at pump `{x:-45.5,y:19.5}` about 50 tiles from the starter cluster, and `setup_power` built it successfully.
- Evidence: `{"source_loop":112,"before":{"first_power_site":{"x":140.5,"y":-826.5},"distance":838.36,"cause":"large-radius limited water sample sorted after clipping"},"after":{"pump":{"x":-45.5,"y":19.5},"boiler":{"x":-43.5,"y":19},"steam_engine":{"x":-43.5,"y":15.5},"small_electric_pole":{"x":-45.5,"y":15.5},"steps":7,"status":"steam power block is producing usable steam power"}}`
- Remaining risk: Future planning scans must keep staged nearest-water ordering and avoid repeating expensive full scans unless a planner genuinely needs refreshed candidates.

## 2026-06-15 03:14:11 +09:00 - Insight 17
- Source loop: Loop 113
- Improvement: The active Slurm Qwen worker now has an automatic renewal path before the 1-day allocation expires.
- Before: Job `677569` had only about 9 minutes left and no dependent successor was queued, so the local LLM could disappear before the next planning loop.
- After: `slurm-ensure-worker --renew-before-minutes 180` queued successor job `678192` with dependency on `677569`.
- Evidence: `{"source_loop":113,"action":"submitted_dependent_successor","dependencyJobId":"677569","submitted_job_id":"678192","timeLeftSeconds":556}`
- Remaining risk: Site policy may still delay pending jobs; the ensure command should be run periodically by the launcher or scheduler, not only manually.

## 2026-06-15 03:28:16 +09:00 - Insight 18
- Source loop: Loop 141
- Improvement: Slurm renewal is now a controller/launcher heartbeat instead of a manual one-shot command.
- Before: A successor was submitted only after manual intervention, and submitting after the running job ended could leave a long queue-induced LLM gap.
- After: Autopilot, strategy decisions, idle layout loops, background layout submissions, and launchers call `ensure_worker_job` with a 6-hour renewal threshold and throttled 30-minute rechecks.
- Evidence: `{"source_loop":141,"tests":"controller/remote_slurm targeted 25 passed","launcher_threshold_minutes":360,"check_interval_seconds":1800}`
- Remaining risk: If the pending successor is delayed by cluster policy, vLLM can still be temporarily unavailable; the queue must stay pre-filled before expiry.

## 2026-06-15 03:28:16 +09:00 - Insight 19
- Source loop: Loop 141
- Improvement: The user-made direct furnace pattern is now encoded as the normal pre-belt iron/copper bootstrap executor behavior.
- Before: Copper bootstrap could fall back to pickaxe-mining `copper-ore`, and early belt smelting could spend scarce hand-crafted belts before a belt assembler existed.
- After: Iron/copper bootstrap builds direct burner mining drill -> stone furnace cells before belt automation; belt smelting expansion is gated until a transport-belt assembler is observed.
- Evidence: `{"source_loop":141,"operator_pattern":"direct furnace was manually built by the user, not the agent","tests":"planner/strategy targeted 206 passed","policy":"no hand-mine ore for normal starter plate production before direct drill/furnace cells"}`
- Remaining risk: This is still a deterministic bootstrap pattern; later electric miners, steel/electric furnaces, modules, and beaconed layouts need separate upgrade executors.

## 2026-06-15 03:50:04 +09:00 - Insight 20
- Source loop: Loop 146
- Improvement: Starter stone can now be automated with a burner drill output chest instead of repeated hand stone mining.
- Before: Furnace and burner-drill prerequisites could fall back to hand-mining stone, so early loops might repeatedly mine stone instead of creating a reusable stone source.
- After: `StoneSupplySkill` builds burner mining drill -> wooden/iron chest stone supply, no-mod observe includes chest entities/recipes, and furnace/drill prerequisite paths call the stone supply skill first.
- Evidence: `{"source_loop":146,"tests":"395 passed","new_skill":"setup_stone_supply","pattern":"burner-mining-drill -> output chest"}`
- Remaining risk: A missing first burner drill can still require tiny bootstrap hand mining/crafting; later electric miner and bot logistics upgrades need separate executors.

## 2026-06-15 03:59:48 +09:00 - Insight 21
- Source loop: Loop 165
- Improvement: `research_automation` no longer stops immediately when gear wheels are missing but inventory iron can be replenished, and transient Slurm attach failures are retried before declaring the local LLM unavailable.
- Before: A live no-mod research step failed with `missing iron gear wheels and cannot craft them`, and concurrent background checks could misclassify a running ready Slurm worker as unavailable after one attached probe failure.
- After: The same research path selected `take iron-plate from starter furnace output` before the intentional one-step stop, and Slurm LLM status retries one transient attached probe failure.
- Evidence: `{"source_loop":165,"tests":"395 passed","live_after_action":"take iron-plate from starter furnace output","previous_failure":"missing iron gear wheels and cannot craft them","slurm_status_retry":true}`
- Remaining risk: Autopilot and idle layout should not be restarted simultaneously until Slurm attach contention is observed stable under the retry path.

## 2026-06-15 04:05:00 +09:00 - Insight 22
- Source loop: Loop 166
- Improvement: The Web UI token usage panel now preserves cumulative display tokens and exposes counter reset count when the raw Codex counter resets.
- Before: A reset from a higher raw token counter to a smaller value could make the summary, chart, or table look like token usage dropped or disappeared.
- After: The summary exposes `latest_raw_tokens`, cumulative `latest_tokens`, `counter_reset_count`, and `latest_counter_reset`; the chart/table render cumulative tokens while retaining raw deltas.
- Evidence: `{"source_loop":166,"tests":"398 passed","regressions":["test_counter_reset_continues_cumulative_display_tokens","test_token_usage_chart_uses_cumulative_tokens_after_counter_reset","test_token_usage_table_uses_cumulative_tokens_after_counter_reset"]}`
- Remaining risk: Weekly percentage still cannot be computed unless `FACTORIO_AI_WEEKLY_TOKEN_QUOTA` is provided.

## 2026-06-15 04:17:36 +09:00 - Insight 23
- Source loop: Loop 167 / Loop 168
- Improvement: The hidden no-mod autopilot completed Automation research under the 4B Slurm LLM strategy path.
- Before: `research_automation` had previously stopped on missing gear prerequisites or an artificial one-step limit, so assembler-based item mall work was blocked.
- After: `research_automation` ran 13 deterministic skill steps, inserted 10 automation science packs, waited through lab progress, and ended with `automation research completed`; the following LLM decision selected `bootstrap_build_item_mall`.
- Evidence: `{"source_loops":[167,168],"strategy_source":"llm","selected_skill":"research_automation","steps":13,"result":"automation research completed","next_skill":"bootstrap_build_item_mall","log":"strategy-automation-research-20260614-191520.jsonl","model":"Qwen/Qwen3.5-4B"}`
- Remaining risk: The next phase must minimize hand crafting by moving from tiny bootstrap crafting into assembler/belt-based site automation now that Automation is researched.

## 2026-06-15 04:27:38 +09:00 - Insight 24
- Source loop: Loop 170
- Improvement: Direct burner mining drill -> stone furnace smelting cells now place the furnace directly against the drill output and reject one-tile-gap furnaces.
- Before: The live copper pair used drill unit `294` at `{x:49,y:-30}` and furnace unit `295` at `{x:52,y:-30}`, leaving a one-tile gap; the drill was blocked with `waiting_for_space_in_destination`.
- After: The layout offset is `2 * direction`, direct furnace matching radius is `0.75`, exact furnace placement disables nearby fallback, and the live pair was rebuilt with furnace unit `316` at `{x:51,y:-30}`; both drill and furnace reported `working`.
- Evidence: `{"source_loop":170,"tests":"400 passed","live_before":{"drill_unit":294,"furnace_unit":295,"drill_position":{"x":49,"y":-30},"furnace_position":{"x":52,"y":-30},"drill_status":"waiting_for_space_in_destination"},"live_after":{"drill_unit":294,"furnace_unit":316,"drill_position":{"x":49,"y":-30},"furnace_position":{"x":51,"y":-30},"drill_status":"working","furnace_status":"working","furnace_inventory":{"copper-ore":3,"copper-plate":3}}}`
- Remaining risk: Existing historical traces with the offset-3 layout should be labeled as bad examples, not successful direct smelting examples, if used for fine-tuning.

## 2026-06-15 05:20:27 +09:00 - Insight 25
- Source loop: Loop 193
- Improvement: automation-science-pack increased by 1 during research_logistics.
- Before: automation-science-pack = 0
- After: automation-science-pack = 1
- Evidence: `{"delta":1,"final":1,"initial":0,"item":"automation-science-pack","source_loop":193,"target":20}`
- Remaining risk: Target is not complete yet: 1/20.

## 2026-06-15 05:20:27 +09:00 - Insight 26
- Source loop: Loop 193
- Improvement: research_logistics completed after 84 step(s): logistics research completed
- Before: not recorded
- After: automation-science-pack = 1
- Evidence: `{"item":"automation-science-pack","item_count":1,"source_loop":193,"steps":84,"target":20}`
- Remaining risk: Target is not complete yet: 1/20.

## 2026-06-15 05:24:17 +09:00 - Insight 27
- Source loop: Loop 211
- Improvement: Automation-era Logistics work now avoids hand-crafting `iron-gear-wheel` and `transport-belt`, using assembler-based gear production instead.
- Before: `strategy-logistics-research-20260614-200524.jsonl` showed `craft gear for transport-belt` at step 10 after Automation was already researched.
- After: `strategy-logistics-research-20260614-201014.jsonl` has no `iron-gear-wheel` or `transport-belt` craft action; it sets assembler unit `318` to `iron-gear-wheel`, inserts iron plates, moves gears through the science assembler, and feeds the lab.
- Evidence: `{"source_loop":211,"tests":"408 passed","bad_trace":"strategy-logistics-research-20260614-200524.jsonl step 10 craft gear for transport-belt","verified_trace":"strategy-logistics-research-20260614-201014.jsonl","craft_grep_after":{"iron_gear_wheel":0,"transport_belt":0,"other_craft":["stone-furnace"]}}`
- Remaining risk: This fixes the observed gear/belt hand-craft paths, but early fuel/coal maintenance can still involve manual-style fallback actions and should be automated separately.

## 2026-06-15 05:32:26 +09:00 - Insight 28
- Source loop: Loop 220
- Improvement: After Logistics research, Qwen's repeated diagnostic-only `plan_factory_site` selections are redirected to the executable green-circuit automation step.
- Before: The hidden autopilot completed Logistics and then repeatedly ran `plan_factory_site` with `not_applied=true`, leaving the factory in diagnostic loops.
- After: A live Slurm/Qwen strategy call still proposed `plan_factory_site`, but reconciliation returned `selected_skill=automate_electronic_circuit_line` with `guardrail_adjusted.to=automate_electronic_circuit_line`.
- Evidence: `{"source_loop":220,"tests":"409 passed","live_strategy":{"llm_selected":"plan_factory_site","final_selected":"automate_electronic_circuit_line","guardrail_from":"plan_factory_site","guardrail_to":"automate_electronic_circuit_line"}}`
- Remaining risk: The redirect is verified at strategy selection level; the circuit automation executor still needs a post-restart live run to verify placement and no new hand-carry fallback.

## 2026-06-15 05:38:09 +09:00 - Insight 29
- Source loop: Loop 221
- Improvement: `CircuitAutomationSkill` now uses gear mall output for `iron-gear-wheel` prerequisites instead of hand-crafting gears after Automation is researched.
- Before: `strategy-circuit-automation-20260614-203509.jsonl` step 1 used `craft iron-gear-wheel for circuit automation`.
- After: The regression path returns `take iron-gear-wheel from build item mall assembler`; fresh live trace `strategy-circuit-automation-20260614-203950.jsonl` has no gear craft matches and feeds assembler unit `318` with iron plates.
- Evidence: `{"source_loop":221,"verification_loop":222,"tests":"410 passed","bad_trace":"strategy-circuit-automation-20260614-203509.jsonl step 1 craft iron-gear-wheel","verified_trace":"strategy-circuit-automation-20260614-203950.jsonl","fresh_trace_gear_craft_matches":0,"regression_decision":"take iron-gear-wheel from build item mall assembler"}`
- Remaining risk: This fixes gear hand-crafting in circuit automation; manual-style iron plate collection and coal mining remain separate automation-quality issues.

## 2026-06-15 05:47:32 +09:00 - Insight 30
- Source loop: Loop 223
- Improvement: Circuit automation can now produce missing `assembling-machine-1` through a powered mall assembler before hand-crafting fallback.
- Before: `strategy-circuit-automation-20260614-204234.jsonl` step 15 used `craft assembling-machine-1 for circuit automation bootstrap`.
- After: The regression path switches an existing powered gear assembler to `assembling-machine-1` with `set_recipe`, so assembler bootstrap can be machine-produced.
- Evidence: `{"source_loop":223,"tests":"411 passed","bad_trace":"strategy-circuit-automation-20260614-204234.jsonl step 15 craft assembling-machine-1","regression_decision":"set_recipe assembling-machine-1 on existing powered mall assembler"}`
- Remaining risk: The assembler-production path is test-verified; a fresh live autopilot run should confirm the circuit automation trace no longer reaches hand-crafted assembler bootstrap.

## 2026-06-15 06:02:29 +09:00 - Insight 31
- Source loop: Loop 233
- Improvement: electronic-circuit increased by 5 during automate_electronic_circuit_line.
- Before: electronic-circuit = 0
- After: electronic-circuit = 5
- Evidence: `{"delta":5,"final":5,"initial":0,"item":"electronic-circuit","source_loop":233,"target":5}`
- Remaining risk: Needs continued validation in later loops.

## 2026-06-15 06:02:29 +09:00 - Insight 32
- Source loop: Loop 233
- Improvement: automate_electronic_circuit_line completed after 9 step(s): circuit automation cell is running and target reached: 5/5
- Before: not recorded
- After: electronic-circuit = 5
- Evidence: `{"item":"electronic-circuit","item_count":5,"source_loop":233,"steps":9,"target":5}`
- Remaining risk: Needs continued validation in later loops.

## 2026-06-15 06:03:56 +09:00 - Insight 33
- Source loop: Loop 234
- Improvement: Post-Automation gear prerequisites no longer fall back to direct `iron-gear-wheel` crafting in the observed expansion/research paths, and post-Logistics layout-ratio planning is redirected to an executable circuit automation skill.
- Before: `strategy-logistics-research-20260614-200524.jsonl` and `strategy-circuit-automation-20260614-203509.jsonl` showed direct gear crafting after Automation; Loops 224-232 repeatedly ran `plan_factory_site` with `not_applied=true`.
- After: Regression tests cover the gear-mall-done edge case and the post-Logistics layout-ratio redirect; live Slurm/Qwen strategy still proposed `plan_factory_site`, but final execution selected `automate_electronic_circuit_line` and `strategy-circuit-automation-20260614-210152.jsonl` had zero craft actions.
- Evidence: `{"source_loop":234,"verification_loop":233,"tests":"414 passed","live_strategy":{"llm_selected":"plan_factory_site","final_selected":"automate_electronic_circuit_line","layout_executable_fallback":"rebalance_green_circuit_ratio"},"live_trace":"strategy-circuit-automation-20260614-210152.jsonl","craft_counts":{"total":0,"iron_gear_wheel":0,"transport_belt":0},"electronic_circuit_delta":5}`
- Remaining risk: The circuit executor still uses take/insert to shuttle copper cable and circuit outputs and does not yet build the full 3-cable/2-circuit optimized layout.

## 2026-06-15 06:12:31 +09:00 - Insight 34
- Source loop: Loop 239
- Improvement: electronic-circuit increased by 7 during automate_electronic_circuit_line.
- Before: electronic-circuit = 5
- After: electronic-circuit = 12
- Evidence: `{"delta":7,"final":12,"initial":5,"item":"electronic-circuit","source_loop":239,"target":50}`
- Remaining risk: Target is not complete yet: 12/50.

## 2026-06-15 06:12:52 +09:00 - Insight 35
- Source loop: Loop 240
- Improvement: The strategy-run circuit automation target now uses 50 circuits instead of the 5-circuit bootstrap target, preventing repeated immediate 5/5 completion cycles.
- Before: Loops 235-238 selected `automate_electronic_circuit_line` but ended immediately with electronic-circuit 5 -> 5 and target 5.
- After: Loop 239 used target 50 and made live progress from electronic-circuit 5 -> 12 before the intentionally capped 20-step verification stopped.
- Evidence: `{"source_loop":240,"verification_loop":239,"tests":"414 passed","before_loops":[235,237],"before_target":5,"after_target":50,"after_delta":7,"live_trace":"strategy-circuit-automation-20260614-211038.jsonl","craft_counts":{"total":0,"iron_gear_wheel":0,"transport_belt":0}}`
- Remaining risk: The circuit executor still relies on manual-style cable take/insert actions; a true inserter/belt transfer and 3:2 layout expansion remains required.

## 2026-06-15 06:34:46 +09:00 - Insight 36
- Source loop: Loop 243
- Improvement: Post-Automation gear handling now blocks both direct `craft iron-gear-wheel` actions and player collection of gear mall output.
- Before: `strategy-circuit-automation-20260614-212357.jsonl` moved iron plates into the gear assembler and then used `take iron-gear-wheel from build item mall assembler`, leaving a sustained player-mediated gear path.
- After: `strategy-circuit-automation-20260614-213245.jsonl` executed only wait actions with reason `refusing player collection of iron gear wheels after Automation`; trace counts for `craft`, `recipe=iron-gear-wheel`, `item=iron-gear-wheel`, and `take iron-gear-wheel` are all 0.
- Evidence: `{"source_loop":243,"verification_loop":242,"tests":"420 passed","before_trace":"strategy-circuit-automation-20260614-212357.jsonl","after_trace":"strategy-circuit-automation-20260614-213245.jsonl","after_counts":{"craft":0,"recipe_iron_gear_wheel":0,"item_iron_gear_wheel":0,"take_iron_gear_wheel":0,"take_iron_plate":0,"insert_iron_plate":0,"take_copper_cable":0,"insert_copper_cable":0}}`
- Remaining risk: This is a behavior-quality improvement, not production progress; gear/belt mall input-output logistics still need a deterministic executor so the agent can continue without player transfer.

## 2026-06-15 07:20:38 +09:00 - Insight 37
- Source loop: Loops 247-252
- Improvement: Gear-to-belt mall logistics now produces `transport-belt` through assemblers, belts, and inserters without player `iron-gear-wheel` craft/take/insert.
- Before: Loops 247-251 exposed the missing executor details: top-lane machine collision, input inserter shortage, reusable inserter mining reach, unpowered relocated input inserter, and reversed inserter directions. The previous guardrail only refused player gear collection, leaving progress blocked.
- After: Loop 252 completed with the belt assembler holding `transport-belt: 4` output and total observed `transport-belt` count 5; the final trace had zero `craft`, zero `recipe=iron-gear-wheel`, zero `item=iron-gear-wheel`, zero `take iron-gear-wheel`, and zero `insert iron-gear-wheel` matches.
- Evidence: `{"source_loops":[247,248,249,250,251,252],"tests":"430 passed","verified_trace":"strategy-gear-belt-mall-20260614-222036.jsonl","repair_trace":"strategy-gear-belt-mall-20260614-221834.jsonl","final_reason":"gear-fed belt mall logistics produced transport belts in assembler output: 4","transport_belt_total":5,"gear_direct_counts":{"craft":0,"recipe_iron_gear_wheel":0,"item_iron_gear_wheel":0,"take_iron_gear_wheel":0,"insert_iron_gear_wheel":0}}`
- Remaining risk: Sustained `iron-plate` input logistics into the gear/belt mall is still needed; Loop 251 used a one-time iron seed, and the current map still contains a stale failed top-lane belt from the earlier collision attempt.

## 2026-06-15 07:51:15 +09:00 - Insight 38
- Source loop: Loop 261
- Improvement: Direct `iron-gear-wheel` crafting is blocked after assembler automation exists, remote `iron-plate` hand-carry into the gear mall is refused, and strict Qwen strategy again returns `source=llm`.
- Before: The heuristic fallback trace `strategy-circuit-automation-20260614-222731.jsonl` moved `iron-plate` from distant furnace unit `43` into the gear assembler, and Slurm/Qwen strategy fell back because remote calls used whitespace-polluted values such as model `Qwen/Qwen3.5-4B ` or base URL `8000 /v1`.
- After: Current-world `BuildItemMallSkill("iron-gear-wheel")` returns `action=null` with an `iron-plate logistic line` requirement; controller/action guard rewrites direct gear craft to wait; `no-mod-strategy --require-llm` returns `source=llm` with normalized model `Qwen/Qwen3.5-4B`.
- Evidence: `{"source_loop":261,"tests":"433 passed","gear_mall_decision":"action=null; iron-plate logistic line from furnace unit 43 at 152 tiles; refusing repeated hand-carry","gear_craft_guard":"wait 120; blocked direct iron-gear-wheel handcraft","slurm_status":{"llm_ready":true,"base_url":"http://127.0.0.1:8000/v1","model":"Qwen/Qwen3.5-4B","model_visible":true},"strict_strategy":{"source":"llm","guardrail_from":"plan_factory_site","guardrail_to":"automate_electronic_circuit_line"}}`
- Remaining risk: This prevents bad direct gear/plate transfer traces but does not yet build the sustained iron-plate logistic line into the gear/belt mall.

## 2026-06-15 08:22:34 +09:00 - Insight 39
- Source loop: Loop 281
- Improvement: The current-world Qwen strategy is now redirected to `build_iron_plate_logistic_line_to_gear_mall` when the gear/belt mall lacks iron-plate input, preventing direct gear handcraft pressure before circuit or item-mall expansion.
- Before: Strict Qwen strategy could select `bootstrap_build_item_mall`, `plan_factory_site`, or `automate_electronic_circuit_line` while gear assembler unit `318` had `iron-plate:0` and the nearest iron-plate furnace unit `43` was 152.5 tiles away.
- After: `no-mod-strategy --require-llm` selected `build_iron_plate_logistic_line_to_gear_mall` via guardrail from Qwen's `bootstrap_build_item_mall`; live 1-step trace moved virtually to belt mall output and contained no `craft`, no `iron-gear-wheel`, and no `take iron-gear-wheel` actions.
- Evidence: `{"source_loop":281,"tests":"439 passed","strategy_guardrail":{"from":"bootstrap_build_item_mall","to":"build_iron_plate_logistic_line_to_gear_mall","source":"llm"},"live_evidence":["gear_assembler_unit=318","iron_source_unit=43","source_distance_tiles=152.5","gear_assembler_status=no_power","transport_belts_available_for_mall_logistics=true","gear_handcraft_blocked=true"],"live_trace":"strategy-iron-plate-gear-mall-logistics-20260614-232108.jsonl","trace_counts":{"craft":0,"iron_gear_wheel":0,"take_iron_gear_wheel":0}}`
- Remaining risk: The route is only partially built; continued autopilot still needs to collect belt output, extend the full plate route, place endpoint inserters, and then resolve the mall power shortage.

## 2026-06-15 08:32:55 +09:00 - Insight 40
- Source loop: Loop 285
- Improvement: The iron-plate logistics route now protects its source furnace and doglegs around it instead of mining it as a belt-line blocker.
- Before: Loop 282 / `strategy-iron-plate-gear-mall-logistics-20260614-232509.jsonl` took belt output, moved to the iron source, mined source furnace unit `43`, and then failed because no iron-plate source furnace remained.
- After: The source was restored as furnace unit `395`; the patched route mined only stale misoriented belt unit `394` and rebuilt transport belt unit `396` with EAST direction while leaving source furnace unit `395` working.
- Evidence: `{"source_loop":285,"tests":"440 passed","bad_trace":"strategy-iron-plate-gear-mall-logistics-20260614-232509.jsonl mined source furnace unit 43","fixed_trace":"strategy-iron-plate-gear-mall-logistics-20260614-233210.jsonl","restored_source":{"unit":395,"recipe":"iron-plate","status":"working"},"fixed_actions":["mine transport-belt unit 394","build transport-belt unit 396 direction EAST"],"bad_actions_absent_after_patch":["mine source furnace","craft iron-gear-wheel"]}`
- Remaining risk: The full belt route and endpoint inserters still need to be completed, and the gear/belt mall power shortage remains a follow-up blocker.

## 2026-06-15 08:43:29 +09:00 - Insight 41
- Source loop: Loop 287
- Improvement: The no-mod build executor no longer treats adjacent belt tiles as the same existing entity, so the iron-plate logistics route can advance past the first dogleg belt.
- Before: Loop 286 / `strategy-iron-plate-gear-mall-logistics-20260614-234054.jsonl` repeatedly requested a belt at `{x:92,y:-65}`, but `existing_built_entity` returned adjacent unit `417` at `{x:91.5,y:-64.5}` as `already_exists`.
- After: Direct live build at `{x:92,y:-65}` created unit `418` at `{x:92.5,y:-64.5}` with SOUTH direction, and planner dry check advanced to the next segment `{x:92,y:-64}`.
- Evidence: `{"source_loop":287,"tests":"440 passed","bad_trace":"strategy-iron-plate-gear-mall-logistics-20260614-234054.jsonl already_exists unit 417 for adjacent tile","fixed_live_build":{"unit":418,"position":{"x":92.5,"y":-64.5},"direction":8},"next_dry_action":{"type":"build","name":"transport-belt","position":{"x":92,"y":-64},"direction":8}}`
- Remaining risk: The full 150-tile route still needs to be completed; build-item supply and gear/belt mall power remain downstream blockers.

## 2026-06-15 08:50:24 +09:00 - Insight 42
- Source loop: Loop 288
- Improvement: Iron-plate logistics detours now score multiple y-offsets and avoid the default lane when it crosses a blocking factory entity.
- Before: `strategy-iron-plate-gear-mall-logistics-20260614-234516.jsonl` advanced beyond the first dogleg but mined burner mining drill unit `40` because the fixed y=-62 lane crossed it.
- After: `_select_iron_plate_line_detour_y` evaluates several offsets and the regression layout avoids the blocker on the default detour lane.
- Evidence: `{"source_loop":288,"tests":"440 passed","bad_trace":"strategy-iron-plate-gear-mall-logistics-20260614-234516.jsonl mined burner-mining-drill unit 40","regression":"test_iron_plate_logistic_line_does_not_mine_source_furnace_as_blocker blocks default detour and verifies alternate segments"}`
- Remaining risk: Existing live belts already follow the earlier y=-62 partial route; future longer route completion still needs more belts and may need a proper pathfinder if the corridor gets dense.

## 2026-06-15 09:09:50 +09:00 - Insight 43
- Source loop: Loop 294
- Improvement: `ModlessLuaController` now blocks direct `craft iron-gear-wheel` at the Lua executor boundary when Automation or assembler context exists, closing the CLI/RCON bypass around the Python controller guard.
- Before: A direct no-mod live action through `ModlessLuaController.act({"type":"craft","recipe":"iron-gear-wheel","count":1})` could virtual-craft one gear despite Automation being researched and four assemblers existing on the surface.
- After: The same live action returns `ok=false` with reason `blocked direct iron-gear-wheel handcraft after Automation research`; validation pollution was restored to `iron-gear-wheel=0`, `iron-plate=40`.
- Evidence: `{"source_loop":294,"tests":"444 passed","targeted_tests":"60 passed","live_guard":"ok=false; blocked direct iron-gear-wheel handcraft after Automation research","inventory_after_restore":{"iron-gear-wheel":0,"iron-plate":40,"transport-belt":0}}`
- Remaining risk: This prevents the bad action but does not solve the current factory deadlock; the gear/belt mall still lacks sustained iron-plate input and transport belts are exhausted.

## 2026-06-15 09:20:01 +09:00 - Insight 44
- Source loop: Loop 295
- Improvement: transport-belt increased by 2 during build_gear_belt_mall_logistics.
- Before: transport-belt = 0
- After: transport-belt = 2
- Evidence: `{"delta":2,"final":2,"initial":0,"item":"transport-belt","source_loop":295,"target":20}`
- Remaining risk: Target is not complete yet: 2/20.

## 2026-06-15 09:20:01 +09:00 - Insight 45
- Source loop: Loop 295
- Improvement: build_gear_belt_mall_logistics completed after 8 step(s): gear-fed belt mall logistics produced transport belts in assembler output: 2
- Before: not recorded
- After: transport-belt = 2
- Evidence: `{"item":"transport-belt","item_count":2,"source_loop":295,"steps":8,"target":20}`
- Remaining risk: Target is not complete yet: 2/20.

## 2026-06-15 09:25:42 +09:00 - Insight 46
- Source loop: Loop 297
- Improvement: transport-belt increased by 2 during build_gear_belt_mall_logistics.
- Before: transport-belt = 0
- After: transport-belt = 2
- Evidence: `{"delta":2,"final":2,"initial":0,"item":"transport-belt","source_loop":297,"target":20}`
- Remaining risk: Target is not complete yet: 2/20.

## 2026-06-15 09:25:42 +09:00 - Insight 47
- Source loop: Loop 297
- Improvement: build_gear_belt_mall_logistics completed after 5 step(s): gear-fed belt mall logistics produced transport belts in assembler output: 2
- Before: not recorded
- After: transport-belt = 2
- Evidence: `{"item":"transport-belt","item_count":2,"source_loop":297,"steps":5,"target":20}`
- Remaining risk: Target is not complete yet: 2/20.

## 2026-06-15 09:39:27 +09:00 - Insight 48
- Source loop: Loops 295-300
- Improvement: The strategy layer now recovers from exhausted construction belts by restarting the gear-fed belt mall first, and the iron-plate logistics executor can clear tree blockers instead of failing with `cannot place entity`.
- Before: With `transport-belt=0`, Qwen/heuristic could keep choosing downstream circuit or iron-line work; `strategy-iron-plate-gear-mall-logistics-20260615-002632.jsonl` and `003430.jsonl` then hit `cannot place entity` on tree-blocked belt positions.
- After: `no-mod-strategy --require-llm` returned `source=llm` with Qwen's `plan_factory_site` guardrailed to `build_gear_belt_mall_logistics`; live runs produced belt output twice, and `strategy-iron-plate-gear-mall-logistics-20260615-003636.jsonl` cleared trees, placed belts through x=72..76, and stopped only when belts were exhausted.
- Evidence: `{"tests":"448 passed","llm_guardrail":{"source":"llm","from":"plan_factory_site","to":"build_gear_belt_mall_logistics"},"belt_mall_traces":["strategy-gear-belt-mall-20260615-001930.jsonl","strategy-gear-belt-mall-20260615-002517.jsonl"],"tree_clear_trace":"strategy-iron-plate-gear-mall-logistics-20260615-003636.jsonl","current_line_belts_near_source":23,"direct_gear_craft":0}`
- Remaining risk: The long iron-plate route is still incomplete and the factory has no remaining iron plates for more belt-mall seed; next progress should refuel/restart the existing direct iron drill/furnace using local wood or build a shorter sustained input path.

## 2026-06-15 09:54:16 +09:00 - Insight 49
- Source loop: Loop 302
- Improvement: setup_power completed after 3 step(s): steam power block is producing usable steam power
- Before: not recorded
- After: steam = 0
- Evidence: `{"item":"steam","item_count":0,"source_loop":302,"steps":3,"target":1}`
- Remaining risk: Target is not complete yet: 0/1.

## 2026-06-15 10:03:00 +09:00 - Insight 50
- Source loop: Loop 303
- Improvement: transport-belt increased by 2 during build_gear_belt_mall_logistics.
- Before: transport-belt = 0
- After: transport-belt = 2
- Evidence: `{"delta":2,"final":2,"initial":0,"item":"transport-belt","source_loop":303,"target":20}`
- Remaining risk: Target is not complete yet: 2/20.

## 2026-06-15 10:03:00 +09:00 - Insight 51
- Source loop: Loop 303
- Improvement: build_gear_belt_mall_logistics completed after 3 step(s): gear-fed belt mall logistics produced transport belts in assembler output: 2
- Before: not recorded
- After: transport-belt = 2
- Evidence: `{"item":"transport-belt","item_count":2,"source_loop":303,"steps":3,"target":20}`
- Remaining risk: Target is not complete yet: 2/20.

## 2026-06-15 10:06:45 +09:00 - Insight 52
- Source loop: Loop 304
- Improvement: The live factory now recovers power and belt-mall output without coal hand-mining or direct gear handcraft by using available wood as burner fuel and nearby local iron plates as a one-time belt assembler seed.
- Before: `SetupPowerSkill` wanted to mine coal despite `wood=19`, and strategy fell through to circuit automation while the transport-belt assembler had `iron-gear-wheel:3` but no `iron-plate`.
- After: `setup_power` completed in 3 live steps, `build_gear_belt_mall_logistics` recovered local iron plates from unit `342`, produced transport belts in unit `320`, and the next strategy selected `build_iron_plate_logistic_line_to_gear_mall`.
- Evidence: `{"tests":"453 passed","power_trace":"strategy-power-20260615-005358.jsonl","belt_mall_trace":"strategy-gear-belt-mall-20260615-010249.jsonl","local_plate_seed_source_unit":342,"belt_assembler_unit":320,"post_run_belt_output":8,"next_strategy":"build_iron_plate_logistic_line_to_gear_mall","direct_gear_craft":0}`
- Remaining risk: The sustained iron-plate route is still incomplete and must consume the newly produced belts to remove the remaining distant plate dependency.

## 2026-06-15 10:13:20 +09:00 - Insight 53
- Source loop: Loop 305
- Improvement: setup_power completed after 2 step(s): steam power block is producing usable steam power
- Before: not recorded
- After: steam = 0
- Evidence: `{"item":"steam","item_count":0,"source_loop":305,"steps":2,"target":1}`
- Remaining risk: Target is not complete yet: 0/1.

## 2026-06-15 10:19:38 +09:00 - Insight 54
- Source loop: Loop 307
- Improvement: iron-plate increased by 1 during produce_iron_plate.
- Before: iron-plate = 5
- After: iron-plate = 6
- Evidence: `{"delta":1,"final":6,"initial":5,"item":"iron-plate","source_loop":307,"target":10}`
- Remaining risk: Target is not complete yet: 6/10.

## 2026-06-15 10:27:15 +09:00 - Insight 55
- Source loop: Loop 309
- Improvement: Iron route recovery now extends the route and restarts source production without remote plate hand-carry, invalid mixed-fuel insertion, or circuit automation masking the gear mall logistics blocker.
- Before: The route stopped at x=72.5 when belts ran out, `produce_iron_plate` would move toward remote furnace output, mixed `wood` into a coal-filled furnace slot and failed, and strategy could select `automate_electronic_circuit_line` only to hit the gear mall plate-line blocker.
- After: The route extends to x=64.5, source drill unit `42` was refueled with wood, source furnace unit `395` reached 18 iron plates, mixed fuel top-off is guarded, and current strategy selects `build_iron_plate_logistic_line_to_gear_mall` with `transport_belts_available_for_mall_logistics=false`.
- Evidence: `{"tests":"461 passed","route_trace":"strategy-iron-plate-gear-mall-logistics-20260615-011408.jsonl","iron_trace":"strategy-iron-20260615-011914.jsonl","route_min_x_near_source":64.5,"source_furnace_unit":395,"source_furnace_iron_plate":18,"current_strategy":"build_iron_plate_logistic_line_to_gear_mall","transport_belts_available_for_mall_logistics":false,"direct_gear_craft":0}`
- Remaining risk: The route is still incomplete and transport belts are exhausted; a non-hand-carry method to replenish belts or re-site related factories is still required.

## 2026-06-15 10:54:28 +09:00 - Insight 56
- Source loop: Loop 311
- Improvement: setup_power completed after 3 step(s): steam power block is producing usable steam power
- Before: not recorded
- After: steam = 0
- Evidence: `{"item":"steam","item_count":0,"source_loop":311,"steps":3,"target":1}`
- Remaining risk: Target is not complete yet: 0/1.

## 2026-06-15 10:57:09 +09:00 - Insight 57
- Source loop: Loop 313
- Improvement: Gear mall iron-plate recovery now uses explicit site placement cost evidence instead of blindly extending the pre-rail belt route.
- Before: The current factory had a 152.5-tile gear mall iron dependency with no construction belts, and strategy could keep treating it as `build_iron_plate_logistic_line_to_gear_mall`.
- After: Qwen-required strategy selects `plan_factory_site` with `belt_route_cost=153.0`, `relocation_power_poles_estimate=20`, `relocation_cost=58.0`, and `route_cost_preference=relocate_mall_to_iron_source`; short route test cases still select the belt-line skill.
- Evidence: `{"tests":"466 passed","setup_power_trace":"strategy-power-20260615-015411.jsonl","layout_trace":"strategy-layout-improvement-20260615-015617.jsonl","selected_skill":"plan_factory_site","source":"llm","source_distance_tiles":152.5,"belt_route_cost":153.0,"relocation_power_poles_estimate":20,"relocation_cost":58.0,"route_cost_preference":"relocate_mall_to_iron_source","power_expansion_clearance_risk":true}`
- Remaining risk: The system can now choose and record the better placement plan and power-clearance risk, but it still needs a deterministic relocation/corridor executor to rebuild the mall automatically.

## 2026-06-15 11:29:21 +09:00 - Insight 58
- Source loop: Loop 315
- Improvement: setup_power completed after 5 step(s): steam power block is producing usable steam power
- Before: not recorded
- After: steam = 0
- Evidence: `{"item":"steam","item_count":0,"source_loop":315,"steps":5,"target":1}`
- Remaining risk: Target is not complete yet: 0/1.

## 2026-06-15 12:10:34 +09:00 - Insight 59

- Source loop: Loop 320
- Improvement: Site layout optimization is now unlock-aware instead of fixed to pre-unlock patterns, and current blocker recovery has deterministic prerequisite skills for pole corridors and electric mining drill adoption.
- Before: Layout candidates and compact Qwen payloads could describe long-handed inserters only weakly or not at all, green-circuit/starter-mall candidate ranking did not change after `long-inserters`, relocation could identify a cheaper site move but lacked safe prerequisite execution, and burner drills had no explicit early replacement research/mall path.
- After: `factory_layout_simulation_candidates` generates `green-circuit-long-handed-3-cable-2-circuit-cell` and `starter-mall-row-long-handed-inputs` only when long-handed inserters are researched/stocked/automated; candidates include `layout_unlocks_considered`, `uses_unlocked_items`, and rerank evidence. Full and compact strategy payloads expose long-handed inserter and module availability. Relocation waits for small-electric-pole corridor materials, strategy can choose `bootstrap_power_pole_mall`, and burner-drill replacement can choose electric mining drill research/mall skills.
- Evidence: `{"tests":"483 passed","new_candidates":["green-circuit-long-handed-3-cable-2-circuit-cell","starter-mall-row-long-handed-inputs"],"payloads":["layout_capabilities.inserters.long-handed-inserter","layout_capabilities.modules","compact.layout_capabilities.rerank_trigger"],"deterministic_skills":["bootstrap_power_pole_mall","relocate_gear_belt_mall_to_iron_source","research_electric_mining_drill","bootstrap_electric_mining_drill_mall"]}`
- Remaining risk: Long-handed variants are still simulation candidates, not live build-ready executors; they must pass sandbox validation and site pre-build gates before mutation. Live factory still needs pole/science throughput to finish the relocation and electric-drill transition.

## 2026-06-15 12:24:22 +09:00 - Insight 60
- Source loop: Loop 322
- Improvement: small-electric-pole increased by 2 during bootstrap_power_pole_mall.
- Before: small-electric-pole = 1
- After: small-electric-pole = 3
- Evidence: `{"delta":2,"final":3,"initial":1,"item":"small-electric-pole","source_loop":322,"target":20}`
- Remaining risk: Target is not complete yet: 3/20.

## 2026-06-15 12:26:06 +09:00 - Insight 61
- Source loop: Loop 323
- Improvement: small-electric-pole increased by 4 during bootstrap_power_pole_mall.
- Before: small-electric-pole = 3
- After: small-electric-pole = 7
- Evidence: `{"delta":4,"final":7,"initial":3,"item":"small-electric-pole","source_loop":323,"target":20}`
- Remaining risk: Target is not complete yet: 7/20.

## 2026-06-15 12:28:30 +09:00 - Insight 62
- Source loop: Loop 324
- Improvement: small-electric-pole increased by 16 during bootstrap_power_pole_mall.
- Before: small-electric-pole = 7
- After: small-electric-pole = 23
- Evidence: `{"delta":16,"final":23,"initial":7,"item":"small-electric-pole","source_loop":324,"target":20}`
- Remaining risk: Needs continued validation in later loops.

## 2026-06-15 12:28:30 +09:00 - Insight 63
- Source loop: Loop 324
- Improvement: bootstrap_power_pole_mall completed after 30 step(s): build item mall is producing small-electric-pole and target reached: 23/20
- Before: not recorded
- After: small-electric-pole = 23
- Evidence: `{"item":"small-electric-pole","item_count":23,"source_loop":324,"steps":30,"target":20}`
- Remaining risk: Needs continued validation in later loops.

## 2026-06-15 12:55:23 +09:00 - Insight 64

- Source loop: Loop 327
- Improvement: Site layout optimization now detects usable long-handed inserters from live recipe unlock state and automatically reranks layout candidates even when the technology table lacks a separate `long-inserters` entry.
- Before: The live observation returned `long-inserters=None`; layout candidates could only switch to long-handed variants through researched technology, stock, or an existing long-handed inserter assembler.
- After: No-mod observation reports `recipe_unlocks.long-handed-inserter.enabled=true`; layout capability marks long-handed inserters available with `recipe_unlocked=true`; the live first layout candidate is `green-circuit-long-handed-3-cable-2-circuit-cell` with `uses_unlocked_items=["long-handed-inserter"]`.
- Evidence: `{"tests":"492 passed","live_recipe_unlock":"long-handed-inserter.enabled=true","live_technology":"long-inserters=None","layout_capability":{"available":true,"recipe_unlocked":true,"researched":false,"rerank_trigger":true},"live_candidate":"green-circuit-long-handed-3-cable-2-circuit-cell","candidate_uses":["long-handed-inserter"],"additional_capabilities":["modules","assembling-machine-2/3","steel/electric-furnace","beacon"]}`
- Remaining risk: Long-handed and higher-tier variants are still simulation candidates until sandbox validation and site pre-build gates pass; module-specific/beacon-specific physical layouts need further candidate generators beyond capability exposure.

## 2026-06-15 13:06:18 +09:00 - Insight 65
- Source loop: Loop 328
- Improvement: setup_power completed after 2 step(s): steam power block is producing usable steam power
- Before: not recorded
- After: steam = 0
- Evidence: `{"item":"steam","item_count":0,"source_loop":328,"steps":2,"target":1}`
- Remaining risk: Target is not complete yet: 0/1.

## 2026-06-15 13:10:17 +09:00 - Insight 66
- Source loop: Loop 329
- Improvement: setup_power completed after 8 step(s): steam power block is producing usable steam power
- Before: not recorded
- After: steam = 0
- Evidence: `{"item":"steam","item_count":0,"source_loop":329,"steps":8,"target":1}`
- Remaining risk: Target is not complete yet: 0/1.

## 2026-06-15 13:24:57 +09:00 - Insight 67

- Source loop: Loop 331
- Improvement: Layout optimization now creates a concrete unlock-driven reassessment opportunity and simulation candidate when a newly usable layout item changes existing site assumptions.
- Before: Part 108 made specialized candidates unlock-aware, but generic site optimization could still treat newly usable tools mostly as passive capability metadata unless the site matched a green-circuit, smelting, or starter-mall pattern.
- After: Live layout context includes `unlock_layout_reassessment` for long-handed inserters across `assembler_cell`, `build_item_mall`, and `circuit_automation`; simulation candidates include `unlock-aware-site-rerank-long-handed-inserter` in addition to `green-circuit-long-handed-3-cable-2-circuit-cell`.
- Evidence: `{"tests":"499 passed","live_capability":{"item":"long-handed-inserter","available":true,"recipe_unlocked":true,"researched":false,"automated":false},"live_opportunity":"unlock_layout_reassessment","affected_site_kinds":["assembler_cell","build_item_mall","circuit_automation"],"live_candidates":[{"candidate_id":"green-circuit-long-handed-3-cable-2-circuit-cell","uses":["long-handed-inserter"],"score":82.7},{"candidate_id":"unlock-aware-site-rerank-long-handed-inserter","uses":["long-handed-inserter"],"score":79.1}]}`
- Remaining risk: The generic unlock-aware candidate is still simulation-only; applying a rebuild still needs sandbox validation, site pre-build gates, and deterministic executors for safe mutation.

## 2026-06-15 13:32:27 +09:00 - Insight 68

- Source loop: Loop 332
- Improvement: Direct required-LLM strategy calls now use the ready remote Slurm Qwen worker when local LLM env is absent.
- Before: `python -m factorio_ai.cli no-mod-strategy --require-llm` failed through local fallback with `LLM strategy was required but source was heuristic` because the direct shell did not export `FACTORIO_AI_SLURM_ENABLED=1` and local `FACTORIO_AI_LLM_BASE_URL`/`FACTORIO_AI_LLM_MODEL` were unset.
- After: With local LLM and Slurm env variables cleared, the same command returned `source=llm` from remote Slurm task `strategy-0d5deda1486e444d963b51bdd9c91e94` using `Qwen/Qwen3.5-4B`.
- Evidence: `{"tests":"501 passed","live_command":"python -m factorio_ai.cli no-mod-strategy --require-llm","local_llm_env":false,"remote_model":"Qwen/Qwen3.5-4B","remote_strategy_id":"strategy-0d5deda1486e444d963b51bdd9c91e94","source":"llm","selected_skill":"bootstrap_build_item_mall"}`
- Remaining risk: This fixes the LLM transport path, not Qwen's strategic choice quality; current live Qwen selected `bootstrap_build_item_mall` while heuristic support still flags incomplete site logistics.

## 2026-06-15 13:40:44 +09:00 - Insight 69

- Source loop: Loop 333
- Improvement: Qwen build-item mall choices are now blocked when they would likely seed a new mall from player inventory while existing site input links are missing.
- Before: Live Qwen selected `bootstrap_build_item_mall` even though current assemblers had incomplete copper/gear/cable inputs, no transport-belt automation, and no `assembling-machine-1` inventory; executing that path risked repeated hand-carry from distant plate sources.
- After: The same live strategy call is guardrailed to `plan_factory_site` with `hand_carry_seed_risk=true`, `transport_belt_automation_ready=false`, and `assembling_machine_1_inventory=0`.
- Evidence: `{"tests":"502 passed","live_strategy_id":"strategy-e7840d7f4e2a4796b9f92a431f25a7a2","raw_llm_skill":"bootstrap_build_item_mall","guardrailed_skill":"plan_factory_site","layout_kind":"incomplete_logistics_link","item":"copper-plate","site_id":"site-link:copper-plate:missing_source:copper-plate->assembler_cell:537","hand_carry_seed_risk":true}`
- Remaining risk: This prevents the unsafe strategy choice but still leaves the factory waiting on an executable general site-to-site logistics correction.

