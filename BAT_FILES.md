# Factorio AI 실행 스크립트(.bat) 안내

리포지토리 루트의 `.bat` 파일이 각각 무엇을 하는지 정리한 문서입니다.
모든 스크립트는 `set PYTHONPATH=src` 후 `python -m factorio_ai.cli <명령>`을 실행합니다.

> 트랙 구분
> - **No-mod 트랙**: 공식 Factorio/Space Age만 사용 + RCON Lua. 멀티플레이 호환, 단 RCON 사용 세이브는 도전과제 비활성. **현재 메인 개발/운영 경로.**
> - **개발(custom-mod) 트랙**: 커스텀 AI 모드 사용. 빠른 반복용, 멀티/도전과제 비호환.
> - **Vanilla 트랙**: 공식 모드만, RCON/Lua 없음. 도전과제 호환(키보드/마우스 입력).

---

## 빠른 추천

| 목적 | 실행할 파일 |
| --- | --- |
| **무인 자동 운영(권장)** | `run_factorio_no_mod_unattended_llm.bat` |
| 돌아가는 걸 관전 | `run_factorio_watch_gui.bat` |
| AI가 캐릭터로 직접 걷고 채굴하는 모습 보기 | `run_factorio_no_mod_real_player_llm_autopilot.bat` |
| 자가 개발(스킬 생성)만 단독 실행 | `run_factorio_no_mod_skill_foundry_loop.bat` |
| 진행 초기화(새 맵) | `reset_factorio_no_mod_map.bat` ⚠️ |

---

## No-mod 운영 (메인)

- **`restart_factorio_no_mod_unattended_llm.bat`** — 🔁 **깨끗한 재시작.** 현재 월드를 저장(best-effort)하고,
  옛 supervisor + 모든 루프(autopilot/idle/foundry) + 서버 + 대시보드 + Factorio를 종료한 뒤, 최신 코드로
  supervisor를 다시 띄웁니다. 코드를 고친 뒤 "옛 프로세스가 안 죽고 남아" 새 코드가 적용 안 될 때 이걸 쓰세요.
  **원격 vLLM 서비스(워밍업된 9B)는 일부러 그대로 둬서 재사용**합니다(빠름). vLLM까지 새로 띄우는 완전 초기화가
  필요하면 `restart_factorio_no_mod_unattended_llm.bat reset-vllm` (느린 ~몇 분 재로드). 직접 취소만 하려면
  `python -m factorio_ai.cli slurm-cancel-vllm-services`.

- **`restart_factorio_no_mod_unattended_llm_full_reset.bat`** — 🧨 **원클릭 풀 리셋.** 위 재시작 + **원격 vLLM
  서비스까지 취소**한 뒤 새로 띄웁니다(9B 재로드 ~몇 분). vLLM이 멈췄거나 여러 개로 쌓였을 때만 쓰세요. 평소엔
  일반 재시작(`restart_factorio_no_mod_unattended_llm.bat`)이 더 빠릅니다.

- **`run_factorio_no_mod_unattended_llm.bat`** — ⭐ 메인 무인 supervisor(`*.ps1` 호출).
  서버 + 웹 대시보드(:18889) + 스케줄러 Qwen(vLLM 서비스) + autopilot + idle layout 루프 +
  **skill foundry 루프**를 살아있게 유지하고, 죽으면 재시작합니다. **5분마다 `/server-save`로
  진행 상황을 저장**하므로 재시작해도 이어서 진행됩니다. LLM이 준비될 때까지 각 루프는 대기(게이팅).

- **`run_factorio_no_mod_llm_autopilot.bat`** — 포그라운드 autopilot(로그가 창에 보임).
  스케줄러 Qwen/vLLM을 보장하고 `--require-llm`으로 실행(LLM 없으면 실패, 휴리스틱 대체 안 함).
  idle layout 루프를 별도 창으로 같이 띄움. 서버 없으면 자동 시작.

- **`run_factorio_no_mod_real_player_llm_autopilot.bat`** — 위와 같지만 **GUI 클라이언트를 열고
  실제 접속 플레이어를 조종**(WASD 이동). 캐릭터가 실제로 걷고 채굴/건설하는 모습이 보입니다.
  연결된 캐릭터가 없으면 가상 서버 에이전트로 떨어지지 않고 실패합니다.

- **`run_factorio_no_mod_autopilot.bat`** — 가장 단순한 autopilot. `--require-llm` 없음(LLM 없으면
  휴리스틱 전략으로도 동작). 서버 없으면 자동 시작. 빠른 확인용.

- **`run_factorio_no_mod_skill_foundry_loop.bat`** — 🆕 **자가 개발 루프 단독 실행.**
  autopilot이 유휴일 때, 실행기 없는 스킬을 로컬 Qwen이 직접 작성→게이트 검증(정적 안전성/오프라인
  리플레이/**샌드박스 세이브 복사본 드라이런**)→등록합니다. 평소엔 unattended supervisor가 이걸 자동으로
  돌리므로 별도 실행은 디버깅/전용 GPU 용도.

- **`run_factorio_no_mod_idle_layout_loop.bat`** — 유휴 GPU로 **시뮬레이션 전용 레이아웃 개선** 작업을
  제출(맵에 적용 안 함). autopilot이 idle/정지/오래됨일 때만 동작.

- **`run_factorio_no_mod_codex_wait_layout_loop.bat`** — (레거시) 사람이 누락 스킬을 구현하는 동안
  레이아웃 작업을 계속 돌리던 용도. 이제 foundry가 스킬을 자가 개발하므로 보통 불필요.

## No-mod 서버 / 관전 / 데모

- **`run_factorio_no_mod_server.bat`** — no-mod LAN/RCON 서버만 시작(세이브 없으면 생성). 다른 클라이언트는
  AI 모드 없이 공식 콘텐츠만 맞으면 접속 가능.

- **`run_factorio_no_mod_watch_gui.bat`** — no-mod 서버에 **GUI 클라이언트를 붙여 관전**. 서버 없으면 먼저 시작.

- **`run_factorio_watch_gui.bat`** — 위 `run_factorio_no_mod_watch_gui.bat`를 그대로 호출(별칭).

- **`run_factorio_no_mod_iron_mvp.bat`** — ⚠️ `create-no-mod-save --overwrite`로 **맵을 새로 만든 뒤**
  철판 자동화 MVP 데모를 실행. 기존 진행이 초기화되니 주의.

- **`reset_factorio_no_mod_map.bat`** — ⚠️ no-mod 서버를 중지하고 **맵을 강제로 새로 생성**(`--overwrite`).
  진행 상황이 모두 초기화됩니다. 새 출발을 원할 때만 사용.

## Slurm / Qwen LLM 백엔드

> 평소 운영은 `run_factorio_no_mod_unattended_llm.bat`가 스케줄러 vLLM 서비스를 알아서 관리하므로
> 아래 워커 스크립트를 따로 실행할 필요는 거의 없습니다. 비교/벤치마크/레거시 경로용입니다.

- **`run_factorio_slurm_llm_4b_worker.bat`** — 스케줄러 관리 4B 경로 보장 + 상태 확인(현행 scheduler 모드).
- **`run_factorio_slurm_llm_9b_worker.bat`** — (레거시 큐) 9B(A6000) 워커 시작/재사용 + 상태.
- **`run_factorio_slurm_llm_27b_gpu3_queue.bat`** — (레거시 큐) 27B 3-GPU 워커 시작 + 상태.
- **`run_factorio_slurm_llm_4b_attached_benchmark.bat`** — 기존 할당에 붙여 4B 전략 벤치마크.
- **`run_factorio_slurm_llm_9b_attached_benchmark.bat`** — 기존 할당에 붙여 9B 전략 벤치마크.

## 개발(custom-mod) 트랙 — 멀티/도전과제 비호환

- **`run_factorio_non_gui.bat`** — 모드 설치 → 세이브 생성 → 개발 서버(별창) → 전략 1스텝 실행.
- **`run_factorio_gui.bat`** — 모드 설치 후 설정된 세이브로 GUI 데모 실행.
- **`run_factorio_review_gui.bat`** — 개발 서버 월드를 GUI로 검토. `runtime\review.lock` 생성(비-GUI 루프가 대기).

## Vanilla 트랙 — 도전과제 호환(Steam, 모드/RCON 없음)

- **`run_factorio_vanilla_gui.bat`** — Steam 바닐라(공식 Space Age 모드만) 실행.
- **`run_factorio_vanilla_freeplay.bat`** — 바닐라 실행 후 메인 메뉴에서 새 Freeplay(Space Age) 시작.
- **`run_factorio_vanilla_probe.bat`** — 바닐라 창 캡처/입력 능력 진단(스크린샷은 `runtime\vanilla` 아래).
- **`run_factorio_restore_steam_mods.bat`** — 백업해 둔 Steam Factorio 모드리스트 복원.

## 기타

- **`continue_factorio_cli.bat`** — (레거시) Codex CLI 핸드오프를 시작하던 스크립트. 로컬 Qwen 자가 개발
  체제에서는 보통 불필요.

---

### 정지 방법
- 포그라운드 루프(autopilot/foundry/idle layout): 해당 창에서 **Ctrl+C**.
- 무인 supervisor: supervisor 창을 닫거나 PowerShell 프로세스를 종료. (자식 루프는 별도 프로세스라
  완전 정지하려면 `python -m factorio_ai.cli ...` 프로세스와 `factorio.exe`도 같이 종료)

### 상태 확인
- 대시보드: `http://<host>:18889/factorio` (무인 모드) — "Generated Skills" 패널에서 자가 개발 현황 확인.
- 런타임 상태 파일: `runtime\unattended-llm-supervisor.json`, `runtime\skill-foundry-loop.json`,
  `runtime\autopilot-heartbeat.json`.
