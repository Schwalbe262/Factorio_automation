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
