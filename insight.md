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

