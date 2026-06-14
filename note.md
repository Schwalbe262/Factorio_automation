# Factorio Loop Notes

모든 탐색, 검증, 문서화, UI 개선, 전략 실행, 공장 실행 루프는 이 파일에 시간순으로 기록한다.

## 기록 템플릿

```text
## YYYY-MM-DD HH:mm:ss +09:00 - Loop N

- Part:
- Goal:
- Hypothesis:
- Actions:
- Candidates:
- Metrics:
- Result:
- Failure reason:
- Next action:
- Token usage:
```

## 2026-06-14 23:32:14 +09:00 - Loop 1

- Part: Part 73 - goal/journal/dashboard 운영 체계 구축
- Goal: `goal.md`, `note.md`, `insight.md`를 만들고 web UI에서 목표, 최근 루프, insight, 토큰 사용량을 볼 수 있게 한다.
- Hypothesis: 장기 Factorio agent 개발은 단순 실행 로그보다 목표, 루프, 개선 insight, 토큰 사용량을 같은 화면과 파일에 남겨야 이어받기와 LLM/LoRA 학습 데이터 구축이 가능하다.
- Actions:
  - `goal.md`, `note.md`, `insight.md`를 추가했다.
  - `src/factorio_ai/run_journal.py`와 `run-journal-summary` CLI를 추가했다.
  - strategy/controller 실행 결과가 `note.md`와 `insight.md`에 기록되도록 연결했다.
  - web dashboard에 Goal Plan, Recent Loop Notes, Recent Insights, token usage 패널을 추가했다.
- Candidates:
  - 운영 문서 후보: `goal.md`, `note.md`, `insight.md`.
  - UI 후보: dashboard 내 목표/루프/insight/token 패널.
- Metrics:
  - `pytest -q`: 341 passed.
  - Part 73 token delta: 243,637.
  - Weekly quota: unknown.
- Result: 목표 및 루프 기록 운영 체계를 구현하고 GitHub에 push했다.
- Failure reason: 없음.
- Next action: `goal.md`의 현재 sprint인 `research_logistics`를 live no-mod game에서 진행한다.
- Token usage: 243,637 / 주간 할당량 분모 미제공.

## 2026-06-14 23:37:38 +09:00 - Loop 2

- Part: Part 74 - 흩어진 기존 맵의 `research_logistics` 실패 원인 분석
- Goal: 기존 live no-mod game에서 `logistics` 연구가 `cannot find a buildable water site for steam power`로 멈추는 원인을 찾는다.
- Hypothesis: 연구 사이트가 spawn에서 멀리 떨어져 있고 planner가 starter-local steam block만 인정해서, 이미 건설된 lab 인접 power block을 복구하지 못하고 있다.
- Actions:
  - `run-no-mod-strategy-step --objective launch_rocket_program --max-steps 80` 실패 로그를 확인했다.
  - live observation에서 lab `{x:20.5,y:-796.5}`, offshore pump/boiler/steam engine이 같은 remote site에 이미 있음을 확인했다.
  - `SetupPowerSkill`과 `ResearchTechnologySkill`의 power block 선택 흐름을 추적했다.
- Candidates:
  - 기존 starter-local power site만 사용.
  - 연구 lab을 기준으로 가까운 기존 remote steam block만 예외적으로 복구.
- Metrics:
  - 실패 로그: `logs/strategy-logistics-research-20260614-143737.jsonl`.
  - Steps: 1.
  - `automation-science-pack`: 0.
- Result: remote lab 인접 기존 power block 복구 예외가 필요하다는 원인을 확인했다.
- Failure reason: 기존 power selection이 starter-local radius 밖의 이미 건설된 lab power block을 무시했다.
- Next action: 연구 skill 한정으로 lab reference position 기반 existing remote power repair를 구현한다.
- Token usage: 루프별 정확 계량 없음 / 주간 할당량 분모 미제공.

## 2026-06-14 23:56:39 +09:00 - Loop 3

- Part: Part 74 - remote research site 복구와 red science 진행 검증
- Goal: 기존 맵에서 `research_logistics`가 이미 만든 remote lab/power/red-science mall을 복구해 실제 연구 진행까지 이어지는지 확인한다.
- Hypothesis: remote site 전체를 허용하면 나쁜 확장이 반복되지만, 이미 존재하는 lab 인접 power/mall만 reference 기반으로 회수하면 현재 연구를 살릴 수 있다.
- Actions:
  - `SetupPowerSkill`과 `_find_steam_power_block`에 `allow_existing_remote`와 `reference_position`을 추가했다.
  - `ResearchTechnologySkill`이 lab 인접 기존 steam block을 복구하도록 했다.
  - `BuildItemMallSkill`이 lab 인접 remote automation site와 이미 놓인 unassigned assembler를 회수하도록 했다.
  - live run에서 automation science assembler recipe 설정, 재료 투입, pack 회수, lab 투입까지 확인했다.
- Candidates:
  - 새 remote power site 건설: 거부.
  - lab 인접 기존 steam block 복구: 채택.
  - lab 인접 powered unassigned assembler 회수: 채택.
- Metrics:
  - `pytest -q`: 344 passed.
  - Live log: `logs/strategy-logistics-research-20260614-145405.jsonl`.
  - `automation-science-pack`: 0 -> 1.
  - `logistics` research progress: 약 25%.
- Result: 기존 맵에서도 red science 생산과 lab feed가 실제로 진행됐다.
- Failure reason: loop 자체는 `max steps reached: 20`으로 끝났고, 이후 copper/gear를 손으로 왕복 운반하는 구조적 문제가 드러났다.
- Next action: 자동화 이후에는 hand-carry를 계속하지 말고 site-to-site logistic line과 관련 site 근접 배치를 우선하도록 guardrail을 추가한다.
- Token usage: 루프별 정확 계량 없음 / 주간 할당량 분모 미제공.

## 2026-06-15 00:02:00 +09:00 - Loop 4

- Part: Part 74 - 자동화 우선 logistics guardrail 추가
- Goal: 극초반 이후 반복 생산이 손 제작/손 운반 루프로 빠지지 않게 하고, 관련 site 간 거리가 멀면 logistics line 계획을 우선하게 한다.
- Hypothesis: Factorio의 핵심 목표는 자동화이므로, Automation 연구 이후 반복 input이 멀리 떨어진 site에서 player inventory로 이동되는 경우 실행을 멈추고 belt/chest/train/bot line을 먼저 계획해야 한다.
- Actions:
  - `factory_layout_issues`에 `manual_site_logistics_gap`과 `distant_related_sites` issue를 추가했다.
  - `BuildItemMallSkill`이 먼 producer site에서 consumer assembler로 반복 hand-carry insert를 거부하게 했다.
  - strategy payload에 `automation_policy` 컨텍스트를 추가했다.
  - heuristic strategy가 automation logistics issue를 보면 `plan_factory_site`를 우선 선택하게 했다.
- Candidates:
  - 연구를 계속 hand-feed: 거부.
  - missing logistic line을 layout issue로 승격: 채택.
  - consumer를 producer 가까이 재배치하거나 trunk belt/rail corridor를 계획: 다음 후보.
- Metrics:
  - 관련 테스트: 221 passed.
  - Live strategy: `plan_factory_site`, priority 90.
  - Live blocker: `site-to-site logistic line`.
  - Research skill decision: 720 tile `copper-plate` hand-carry 거부.
- Result: 현재 strategy가 더 이상 흩어진 기존 맵에서 hand-carry 연구 루프를 계속하지 않는다.
- Failure reason: 기존 맵 자체가 너무 많이 튀어서 logistic line으로 수습하는 비용이 커졌다.
- Next action: 새 맵에서 cliffs off로 재시작하고, bootstrap 이후에는 관련 site를 가깝게 두며 logistics line 기반으로 확장한다.
- Token usage: 루프별 정확 계량 없음 / 주간 할당량 분모 미제공.

## 2026-06-15 00:12:36 +09:00 - Loop 5

- Part: Part 74 - 새 no-mod world 재시작
- Goal: 너무 흩어진 기존 맵을 백업하고, cliffs off 설정의 새 no-mod world에서 rocket program을 다시 시작한다.
- Hypothesis: 현재 맵은 power/research/smelting/circuit site가 과도하게 흩어져 있어 자동화 logistics 목표와 맞지 않으므로, 새 맵에서 site proximity와 logistics line 원칙을 처음부터 적용하는 편이 빠르고 안정적이다.
- Actions:
  - 기존 server save를 RCON `/server-save` 후 종료했다.
  - 기존 save를 `runtime/vanilla/saves/backups/no-mod-rcon-scattered-20260615-001123.zip`로 백업했다.
  - `create-no-mod-save --overwrite`로 새 no-mod save를 만들었다.
  - map-gen 설정에서 `cliff_settings.richness = 0`, `cliff_elevation_interval = 0`을 확인했다.
  - no-mod server를 새 save로 재시작하고 초기 observation/strategy를 확인했다.
- Candidates:
  - 기존 맵에서 logistic line으로 수습: 보류.
  - 새 cliffs-off 맵에서 재시작: 채택.
- Metrics:
  - New save: `runtime/vanilla/saves/no-mod-rcon.zip`.
  - Current fresh-map tick checked: 2679.
  - Initial inventory: `burner-mining-drill:1`, `stone-furnace:1`.
  - Observed cliffs: 0.
  - Initial strategy: `produce_iron_plate`, priority 96.
  - Nearest observed resources: copper about 52 tiles, stone about 5 tiles, iron about 100 tiles, coal about 107 tiles.
- Result: 새 no-mod world가 실행 중이며 web dashboard도 같은 runtime을 관찰할 수 있다.
- Failure reason: 없음.
- Next action: 새 맵에서 iron bootstrap을 시작하되, site가 튀지 않도록 starter-local smelting/coal/power를 먼저 묶고 이후 logistics line 기반으로 research를 진행한다.
- Token usage: 루프별 정확 계량 없음 / 주간 할당량 분모 미제공.

## 2026-06-15 00:16:30 +09:00 - Loop 6

- Part: Part 74 - 최종 검증 및 handoff 정리
- Goal: 새 맵 재시작, cliffs off 설정, 자동화 우선 logistics guardrail, loop/insight 기록 체계가 모두 테스트로 검증되는지 확인한다.
- Hypothesis: 이번 변경은 live runtime reset과 운영 규칙 변경을 포함하므로, 전체 테스트와 live strategy/observe가 모두 통과해야 다음 루프가 안전하게 iron bootstrap으로 넘어갈 수 있다.
- Actions:
  - `pytest -q` 전체 테스트를 실행했다.
  - 새 no-mod server에서 observe와 strategy를 다시 확인했다.
  - `note.md`와 `insight.md`를 loop/insight 템플릿 기반 기록으로 정리했다.
  - `docs/CLI_HANDOFF.md`와 `goal.md`를 새 맵 상태로 갱신했다.
- Candidates:
  - 기존 흩어진 맵 계속 수습: 폐기.
  - 현재 cliffs-off 새 맵에서 compact bootstrap 진행: 채택.
- Metrics:
  - `pytest -q`: 349 passed.
  - Live strategy: `produce_iron_plate`, priority 96.
  - Observed cliffs: 0.
  - Running services: no-mod server and web dashboard active.
- Result: Part 74 변경은 테스트와 live smoke 기준을 통과했다.
- Failure reason: 없음.
- Next action: commit/push 후 새 맵에서 compact iron/coal/copper bootstrap을 시작한다.
- Token usage: 925,375 / 주간 할당량 분모 미제공.

## 2026-06-15 00:31:51 +09:00 - Loop 7
- Part: Part 75 - training trace preservation
- Goal: launch_rocket_program / preserve_scattered_map_layout_and_strategy_traces
- Hypothesis: Layout improvement logs, failed strategy traces, LLM decisions, and human intervention comparisons are future GEPA/Qwen LoRA data and should be archived before map resets or log churn hide them.
- Actions:
  - Added archive-training-traces and trace-archive-summary CLI commands.
  - Added trace archive manifest/index generation with raw log copies under runtime/trace_archives.
  - Added dashboard summary for latest training trace archives.
  - Documented operator intervention before/after comparison rules for future human layout edits.
  - Exported current logs and Markdown journals into a local archive bundle.
- Candidates:
  - Leave raw logs only in logs/: rejected because ignored logs are easy to lose during map/runtime changes.
  - Commit raw logs to Git: rejected because logs/runtime are large local artifacts.
  - Local archive with tracked exporter and docs: selected.
- Metrics:
  - Steps: 1.
  - Status: ok.
  - Log: `C:\Users\NEC\Documents\Factorio\runtime\trace_archives\20260615-003151-part75-scattered-map-traces\manifest.json`.
  - Metadata: `{"actions":["Added archive-training-traces and trace-archive-summary CLI commands.","Added trace archive manifest/index generation with raw log copies under runtime/trace_archives.","Added dashboard summary for latest training trace archives.","Documented operator intervention before/after comparison rules for future human layout edits.","Exported current logs and Markdown journals into a local archive bundle."],"archive_dir":"C:\\Users\\NEC\\Documents\\Factorio\\runtime\\trace_archives\\20260615-003151-part75-scattered-map-traces","candidates":["Leave raw logs only in logs/: rejected because ignored logs are easy to lose during map/runtime changes.","Commit raw logs to Git: rejected because logs/runtime are large local artifacts.","Local archive with tracked exporter and docs: selected."],"high_value_files":61,"hypothesis":"Layout improvement logs, failed strategy traces, LLM decisions, and human intervention comparisons are future GEPA/Qwen LoRA data and should be archived before map resets or log churn hide them.","next_action":"Use the archive index for GEPA prompt eval extraction and add automatic before/after snapshot comparison for human factory edits.","part":"Part 75 - training trace preservation","source_count":158}`.
- Result: Completed: created local training trace archive before further map/runtime churn
- Failure reason: None
- Next action: Use the archive index for GEPA prompt eval extraction and add automatic before/after snapshot comparison for human factory edits.
- Token usage: 252,093 / 주간 할당량 분모 미제공.

## 2026-06-15 00:38:07 +09:00 - Loop 8
- Part: skill
- Goal: launch_rocket_program / produce_iron_plate
- Hypothesis: Running `produce_iron_plate` should move the factory toward `launch_rocket_program`; item counts and the raw action log verify progress.
- Actions:
  - Ran deterministic skill `produce_iron_plate` for up to 120 step(s).
  - Tracked `iron-plate` from 7 to 11.
  - Wrote raw action trace to `C:\Users\NEC\Documents\Factorio\logs\strategy-iron-20260614-153557.jsonl`.
- Candidates:
  - Selected goal/skill: `produce_iron_plate`.
  - Target item candidate: `iron-plate` target `10`.
- Metrics:
  - Steps: 20.
  - Status: ok.
  - Duration: 130.640s.
  - iron-plate: 7 -> 11 (delta 4).
  - Log: `C:\Users\NEC\Documents\Factorio\logs\strategy-iron-20260614-153557.jsonl`.
  - Metadata: `{"delta_item_count":4,"final_item_count":11,"initial_item_count":7,"max_steps":120,"target":10}`.
- Result: Completed: iron plate target reached: 11/10
- Failure reason: None
- Next action: Advance to the next highest-priority goal from `goal.md`.
- Token usage: not recorded for this loop / weekly quota unavailable

## 2026-06-15 00:40:19 +09:00 - Loop 9
- Part: skill
- Goal: launch_rocket_program / setup_coal_supply
- Hypothesis: Running `setup_coal_supply` should move the factory toward `launch_rocket_program`; item counts and the raw action log verify progress.
- Actions:
  - Ran deterministic skill `setup_coal_supply` for up to 160 step(s).
  - Tracked `coal` from 12 to 25.
  - Wrote raw action trace to `C:\Users\NEC\Documents\Factorio\logs\strategy-coal-supply-20260614-153837.jsonl`.
- Candidates:
  - Selected goal/skill: `setup_coal_supply`.
  - Target item candidate: `coal` target `16`.
- Metrics:
  - Steps: 17.
  - Status: ok.
  - Duration: 101.922s.
  - coal: 12 -> 25 (delta 13).
  - Log: `C:\Users\NEC\Documents\Factorio\logs\strategy-coal-supply-20260614-153837.jsonl`.
  - Metadata: `{"delta_item_count":13,"final_item_count":25,"initial_item_count":12,"max_steps":160,"target":16}`.
- Result: Completed: coal supply site is active with fueled burner mining drill and output belt
- Failure reason: None
- Next action: Advance to the next highest-priority goal from `goal.md`.
- Token usage: not recorded for this loop / weekly quota unavailable

## 2026-06-15 00:42:08 +09:00 - Loop 10
- Part: skill
- Goal: launch_rocket_program / connect_coal_fuel_feed
- Hypothesis: Running `connect_coal_fuel_feed` should move the factory toward `launch_rocket_program`; item counts and the raw action log verify progress.
- Actions:
  - Ran deterministic skill `connect_coal_fuel_feed` for up to 120 step(s).
  - Tracked `coal` from 24 to 26.
  - Wrote raw action trace to `C:\Users\NEC\Documents\Factorio\logs\strategy-coal-fuel-feed-20260614-154045.jsonl`.
- Candidates:
  - Selected goal/skill: `connect_coal_fuel_feed`.
  - Target item candidate: `coal` target `1`.
- Metrics:
  - Steps: 14.
  - Status: ok.
  - Duration: 82.704s.
  - coal: 24 -> 26 (delta 2).
  - Log: `C:\Users\NEC\Documents\Factorio\logs\strategy-coal-fuel-feed-20260614-154045.jsonl`.
  - Metadata: `{"delta_item_count":2,"final_item_count":26,"initial_item_count":24,"max_steps":120,"target":1}`.
- Result: Completed: coal fuel feed is active: belt and burner inserter are feeding a furnace fuel inventory
- Failure reason: None
- Next action: Advance to the next highest-priority goal from `goal.md`.
- Token usage: not recorded for this loop / weekly quota unavailable

## 2026-06-15 00:49:45 +09:00 - Loop 11
- Part: Part 76 - Qwen autopilot guardrails
- Goal: launch_rocket_program / qwen_autopilot_guardrails_before_belt_paths
- Hypothesis: Gameplay should be driven by the local/remote Qwen autopilot, but deterministic guardrails must prevent it from selecting impossible mall work before Automation or site-to-site belt links before belt production is automated.
- Actions:
  - Stopped treating manual Codex strategy-step calls as the normal control path.
  - Verified the remote Slurm Qwen worker is ready and can answer no-mod strategy requests.
  - Added a guardrail that redirects bootstrap_build_item_mall to research_automation until Automation is researched.
  - Added a guardrail that blocks connect_coal_fuel_feed before transport-belt production is automated by an assembler mall.
  - Updated handoff/goal docs so normal operation uses the continuous Qwen autopilot and Codex only fills missing functions.
- Candidates:
  - Keep manually invoking run-no-mod-strategy-step from Codex: rejected because the game idles while Codex does other work.
  - Let Qwen choose any implemented skill: rejected because it selected bootstrap_build_item_mall before Automation.
  - Continuous Qwen autopilot plus deterministic feasibility guardrails: selected.
- Metrics:
  - Steps: 1.
  - Status: ok.
  - Log: `C:\Users\NEC\Documents\Factorio\logs\llm_decisions.jsonl`.
  - Metadata: `{"actions":["Stopped treating manual Codex strategy-step calls as the normal control path.","Verified the remote Slurm Qwen worker is ready and can answer no-mod strategy requests.","Added a guardrail that redirects bootstrap_build_item_mall to research_automation until Automation is researched.","Added a guardrail that blocks connect_coal_fuel_feed before transport-belt production is automated by an assembler mall.","Updated handoff/goal docs so normal operation uses the continuous Qwen autopilot and Codex only fills missing functions."],"candidates":["Keep manually invoking run-no-mod-strategy-step from Codex: rejected because the game idles while Codex does other work.","Let Qwen choose any implemented skill: rejected because it selected bootstrap_build_item_mall before Automation.","Continuous Qwen autopilot plus deterministic feasibility guardrails: selected."],"hypothesis":"Gameplay should be driven by the local/remote Qwen autopilot, but deterministic guardrails must prevent it from selecting impossible mall work before Automation or site-to-site belt links before belt production is automated.","metrics":{"guardrail_adjusted_from":"bootstrap_build_item_mall","live_strategy_after_guardrail":"research_automation","pytest":"354 passed","remote_model":"Qwen/Qwen3.5-4B","slurm_llm_ready":true,"strategy_tests":"42 passed"},"next_action":"Start the real-player no-mod Qwen autopilot background runner after commit/push and let Codex only address future missing executors or guardrail failures.","part":"Part 76 - Qwen autopilot guardrails","token_usage":"150,004 / weekly quota unavailable"}`.
- Result: Completed: remote Qwen strategy now passes through deterministic guardrails that block premature belt mall and site-link choices
- Failure reason: None
- Next action: Start the real-player no-mod Qwen autopilot background runner after commit/push and let Codex only address future missing executors or guardrail failures.
- Token usage: 150,004 / weekly quota unavailable

