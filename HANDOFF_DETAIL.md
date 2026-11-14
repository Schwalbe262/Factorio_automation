# Claude 인수인계 · 2026-11-13

작업 경로: `C:/Users/NEC/Documents/git/factorio_automation`
브랜치 `v2` / 원격 `https://github.com/Schwalbe262/Factorio_automation.git`

## 읽는 순서

`HANDOFF_CURRENT.md` → `AGENTS.md` → 이 파일 → `goal.md`의 Mission/Current implementation focus → 필요한 소스의 정확한 구간.
`note.md`, `insight.md`는 큰 이력 파일이다. 전체를 읽지 말고 `rg`로 필요한 부분만 찾는다. 전체 JSONL·테스트 로그·git diff를 출력하지 않는다.

## 중지 상태

사용자가 Codex를 잠시 멈추고 Claude가 이어받도록 요청했다. Codex 목표는 `paused`이고 병렬 에이전트도 중단했다. 자동 실행기는 `Lifecycle.stop()`으로 정상 중지·저장했다. 게임은 일시정지 상태이고 GUI는 열려 있다. 서버/GUI를 강제 종료하지 않았다.

Claude가 검토한 뒤 `tools/windows/start.bat` 또는 루트 `Factorio.bat` 메뉴로 기존 맵을 재개할 수 있다. 새 맵을 만들 필요는 없다. 개발 주체를 Claude로 바꾸더라도 독립 실행기의 GPT-6 Luna medium/Codex 구독 구조는 유지한다. 모델 구조를 변경하려면 별도 사용자 지시가 필요하다.

## 목표와 최신 우선순위

Space Age에서 기본 적 설정으로 태양계 가장자리에 도달하는 자동화가 최종 목표다. 현재 1차 목표는 실제 로켓 발사까지의 프레임워크이다. 고급 모델은 개발·디버깅하고, Luna는 프레임워크가 제공하는 함수로 작업을 선택한다. RCON 직접 건설은 허용하지만 원료·생산·연구는 실제 규칙을 따른다. 아이템·연구·성공 증거를 생성하면 안 된다.

1. 수분 동안 진전 없는 상태를 방치하지 말고 원인을 빨리 해결한다.
2. 극초반에 포탑을 과도하게 짓지 않는다. 조용하면 생산·연구가 우선이다.
3. 다른 작업이 막혀 있으면 가능한 연구도 계속한다. **이 항목은 구현 도중이다.**
4. 대기 시간에는 실제 다음 작업을 준비한다. 전기 채굴기 하나의 예시로 하드코딩하지 않는다.
5. 재료가 충분하면 자원 확장만 계속하지 말고 다음 진도로 넘어간다.

기존 요구도 유지한다: PCB 같은 직선·정렬 배치, 확장 방향과 자원 영역 보호, 채굴·제련의 양쪽 벨트 레인 대역폭, 메인 버스와 우선순위 분기, 컴팩트 과학 공장, 수량 제한 건설몰, 위협에 따른 방어·레이더·연구 업그레이드, 이후 외부 광산과 철도.

## 현재 맵과 정확한 작업 ID

- 시드 `3083327785`, 월드 `8d2cc1ed-d4fe-4b09-b16a-a42babe6963f`.
- 런타임 `runtime/v2/eval-3083327785`. 시작 지역 200%, Nauvis 절벽 없음, 속도 1.
- 중지 전 서버 PID50076/GUI19852, 마지막 실행기26144는 정상 종료했다. PID만 믿지 말고 lifecycle의 소유권과 상태를 다시 확인한다.
- 철 전기 채굴: `lunaq_afb9bd53d63044d487917086cdb938b85f9b83a657c5a8f3`, 성공60행동.
- 구리 전기 채굴: `lunaq_b9960bf2e3c86abf1eedfe1ec23581e3c7d768f3ecda3480`, 성공31행동.
- 석탄 전기 채굴: `lunaq_f89b6bb8fb7d482d30cab60eaee2cafc9c37eb4e95e56522`, 성공13행동.
- 명목 목표는 철60/구리60/석탄30개/분이고 실제 출력을 관측했다. 지속 처리량 측정치는 아니다.
- 현재 제련: `lunaq_6067a7cea0b54d0ea94076211ce0e48266038cf6b51a86ea`. 중지 시 `paused`, 27행동, 마지막 native306111. 동일 키를 재개한다. 새 ID로 재구매하거나 중복 요청하지 않는다.

## GitHub에 게시한 검증 완료 변경

Part33 `529dca5`: 석탄 수집→즉시 투입만 반복하던 유지보수 루프와, 적 없는 맵의 Lua 빈 배열 `{}`를 잘못 해석하던 방어 검사 오류를 수정했다. 집중88개 테스트 통과. 동일 방어/광산 키의 정상 retry로 실제 확장이 재개됐다. Sol의 복구 제어가 있었으므로 Luna 단독 성공으로 기록하면 안 된다.

Part34의 검증 완료 인덱스 변경도 인수인계 시 `v2`에 커밋·푸시한다. 정확한 최신 해시는 `git log -2 --oneline`으로 확인한다.

- `resource_policy.resource_decision`/`supply_planning`: 회수 가능한 재고, 현 예산, 생산/소비, 추가 설비 비용과 회수 시간을 비교하는 읽기 전용 판단 근거.
- `prepare_components`: 해금된 건설품1~8종, 절대 재고1~50개, 주 건설 원료 보호. 기존 제작·수집 함수만 사용한다.
- `supervisor`: 실제 자동 진행 대기에 승인된 준비 작업을 한 구간 실행한 뒤 원 작업으로 복귀한다. 다른 작업에 가려진 준비 작업 선택, 재료 대기로 원 작업이 영구 차단되는 문제도 검증했다. 미확정 행동/제어 결과는 자동 재실행하지 않는다.
- `science_pipeline`/`industry`: 전기 채광 연구→물류 한 경로의 유한 과학 배치 선행 생산. 실제 ACK·새 기준값·기존 소유권을 보존한다. 모든 연구 경로를 지원하는 것은 아니다.
- `defense_policy`/`defense_perimeter`: 극초반 quiet 신규 포탑 목표0. 현재 생산 시설과 예정지 주변96타일을 검사한 뒤 산업 구역 방어 조건을 미룬다. 이것을 실제 무장 방어 범위 증명으로 기록하지 않는다. 위협·피해·단계 변화에는 방어를 다시 요구한다.
- quiet 정책은 현재 맵에도 적용됐다. 포탑은4개에서 늘지 않았고 석탄 광산은 포탑 추가 없이 완료됐다.

검증 스냅샷 `runtime/part34-index`: v2 테스트1,547개 실행, 1,545통과/참조 환경2건 skip, 56.483초. 로그 `runtime/part34-v2-index-tests.log`는 요약만 읽는다. **이 검증은 해당 인덱스에 대한 것이며 현재 작업 트리 전체가 아니다.**

## 별도 미완료·미커밋 변경

### 제련 배치 복구

`src/factorio_agent/smelting.py`, `tests/test_v2_smelting.py`는 별도 미커밋이다. 기존9개 위치 탐색에 현재 광산 포트 기반 후보를 추가했다. 최종 raw can_place가 경로에서 승인한 자연 장애물 제거를 다시 거부하던 오류를 공유 preflight로 수정하고, 정확한 제거 정보를 유료 건설 계획으로 전달한다.

집중13개 테스트 통과. 실제 읽기 전용 prepare는 native294376에 463개 설비와 바위3개+나무1개 제거를 승인했다. 기존5개 벨트 경로를 유지했다. 기존 제련 키를 정상 retry하여 12→27행동까지 진행한 뒤 인수인계 때문에 중지했다. **제련 완공은 아직 증명하지 않았다.** 이 변경을 검토·검증해 별도 커밋하고 기존 작업을 재개한다.

### 가능한 연구를 계속하기

`industry.py`는 커밋된 Part34와 별도의 작업 트리 수정이 있다. `tests/test_v2_bootstrap_research.py`도 미커밋이다. 세 기술 고정 validator를 안전한 기술 이름으로 바꾸되 실제 수락은 현 카탈로그의 적색팩 전용 정수1~100단위 연구로 제한하는 일반화 도중 에이전트를 중단했다. 완료된 기능이라고 취급하지 않는다.

현재 맵의 연구는 비어 있었고 배우/실험실/조립기 출력의 적색팩은0이었다. 실제 선행조건이 열린 연구: fast-inserter30, logistic-science-pack75, steel-processing50, lamp10, military10, radar20, repair-pack25.

남은 중앙 연결: `jobs.py` CAPABILITIES의 bootstrap_research 설명, `_funded_startup_owner`의 세 기술 하드코딩, Luna에 전달할 유한 연구 옵션/유휴 시 선택 규칙. 이미 승인된 다음 단계에 필요한 연구를 우선하고 연구 팩을 실제 생산·공급한다. 건설이 막혔다는 이유로 연구도 계속 대기하지 않게 해야 한다. 자동 적색 과학 공장 선행조건을 억지로 성공 처리하지 않는다.

## 보존할 오래된 WIP

`capacity_profiles.py`, `mining_layout_policy.py`, `outposts.py`, `paired_mining.py`에는 별도 소진 채굴기 회수 변경이 있다. 이 네 파일만 커밋하면 untracked `depleted_drill_retirement.py` 의존성으로 깨끗한 체크아웃에서 import가 실패할 수 있어 이번 커밋에서 제외했다.

`advanced_petroleum.py`, `fluid_products.py`, `fluid_distribution.py`와 관련 테스트/문서, oil/water source namespace, 방어 탄약·파괴 후 복구·설비 수리도 미등록/동결 WIP이다. 통째로 stage하거나 지우지 않는다. fluid_distribution에는 ACK 기록 후 메모리 pending 재동기화 문제가 알려져 있다. 사용자는 후반 신규 기능보다 현재 진행 속도를 우선했다.

## 재개와 검증

`Factorio.bat` 메뉴 및 `tools/windows`의 start/stop/new_map/gui/status/logs/shutdown을 사용한다. 먼저 status로 상태를 보고, 검토 후 start로 기존 맵과 유료 작업을 재개한다. BAT 콘솔을 닫아도 서비스가 유지되도록 수정했고 실제 WM_CLOSE로 확인했다.

PowerShell 집중 검증 예시:

```powershell
$env:PYTHONPATH='src;tests;runtime/v2/lua-test-deps'
python -B -m unittest test_v2_smelting test_v2_bootstrap_research test_v2_science_pipeline test_v2_supervisor
```

전체 현재 v2 검증은 `python -B -m unittest discover -s tests -p 'test_v2*.py'`. 오래된 전체 테스트로 무조건 확대하지 않는다. 배포할 정확한 파일 묶음/인덱스를 별도로 검증한다. `git add -A`, reset/clean, 새 맵 생성, 중복 ID, 아이템·연구 지급을 피한다.

로켓·철도·플랫폼·모든 시드/1시간 재현·Luna 단독 클리어는 아직 검증하지 않았다. 최근 광산 누적 계획 시간이 실제 실행 시간보다 컸으므로 다음 성능 진단의 단서이다.

파트마다 작은 관련 검증, 정확한 diff 검토, 10줄 이하 HANDOFF_CURRENT, note 사건1개, 확인된 재사용 개선만 insight, 분리 커밋/푸시를 따른다. 사용량은 Part33 root16,679,413토큰(캐시 포함), 계정71→73%(+2%p). Part34 인수인계 직전 측정 root12,472,170토큰(캐시 포함), 계정73→74%(+1%p). 병행 미완료 작업의 부모 컨텍스트도 포함되며 정확한 작업별 주간 비중·자식별 합계는 알 수 없다.
