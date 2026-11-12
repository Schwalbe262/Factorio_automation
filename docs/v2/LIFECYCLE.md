# BAT 실행과 월드 수명주기

루트의 [Factorio.bat](../../Factorio.bat)을 실행해 한국어 메뉴에서 선택합니다.
`Factorio.bat --help`는 게임을 시작하지 않고 도움말을 표시합니다. 메뉴의 `0`은
메뉴만 닫고 게임·AI의 실행 상태를 유지합니다. 설정 편집은 메모장으로
[automation_config.json](../../automation_config.json)을 엽니다.

개별 실행 도구는 [tools/windows](../../tools/windows/README.md)로 옮겼습니다.
어떤 폴더에서 실행해도 스크립트의 `../..`를 절대 저장소 경로로 계산합니다.
메뉴 실행은 결과 화면에서 한 번만 대기합니다. 기존 31개 실행기는
[원본 텍스트 보관함](../../tools/legacy_bat/README.md)에 있으며 현재 실행기가 아닙니다.

| 파일 | 동작 |
| --- | --- |
| 메뉴 1 / `tools/windows/start.bat` | 현재 월드 재개. 필요한 서버/클라이언트를 연결하고 숨겨진 Luna Supervisor 하나를 실행합니다. |
| 메뉴 2 / `tools/windows/stop.bat` | AI의 현재 행동 경계, 작업 일시정지와 저장 ACK를 기다린 뒤 게임 시간도 멈추고 저장합니다. GUI는 유지합니다. |
| 메뉴 3 / `tools/windows/new_map.bat` | 이전 월드 정지·저장·백업·종료 후 설정에 따라 별도 새 월드를 생성하고 실행합니다. |
| 메뉴 4 / `tools/windows/gui.bat` | 현재 월드 GUI를 연결하거나 표시합니다. AI를 시작하거나 게임 일시정지를 해제하지 않습니다. |
| 메뉴 5 / `tools/windows/status.bat` | 현재 시드·월드·검증된 PID와 작업 큐를 표시합니다. `current_task`에서 현재 작업, 실제 대기 이유, 행동·실패 수, 마지막 진행 이후 경과 초를 확인할 수 있습니다. |
| 메뉴 6 / `tools/windows/logs.bat` | 최대 60줄의 로그를 표시합니다. `--open`은 로그 폴더를 엽니다. |
| 메뉴 7 | 설정 파일을 메모장으로 편집합니다. 적용 시점은 [설정 문서](AUTOMATION_CONFIG.md)를 참고하세요. |
| 메뉴 8 / `tools/windows/shutdown.bat` | 안전 정지와 저장을 마친 뒤 검증된 소유 서버/클라이언트를 종료합니다. |

`tools\windows\start.bat --no-gui`, `tools\windows\new_map.bat --no-gui`,
`tools\windows\logs.bat --lines 100`도 지원합니다.
Python 3.10 이상과 설치된 Factorio가 필요합니다. Luna 연결과 모델 요청은 별도
`luna_client`/`supervisor` 구현이 담당하며, BAT가 모델 판단을 대신하지 않습니다.

현재 월드는 `runtime/active-runtime.json`의 절대 `runtime` 경로, `seed`,
`world_id`로 선택합니다. 첫 실행의 포인터가 없으면 기존
`runtime/v2/eval-1053994892`를 선택합니다. `start`는 새 시드를 만들지 않습니다.
포인터의 시드/월드와 실제 등록 정보가 다르면 중단합니다.

`new-map`은 기존 경로를 덮어쓰거나 삭제하지 않습니다. 저장 ZIP의 무결성과 저장
완료를 확인한 뒤 `runtime/backups`에 ZIP, 루트 JSON 체크포인트와 SHA256 명세를
복사합니다. 원래 전체 런타임도 남깁니다. 새 `eval-<randomseed>` 경로와 사용 가능한
UDP/RCON 포트 쌍을 선택하고 기존 `runtime.create`와 [설정 파일](AUTOMATION_CONFIG.md)을
사용합니다. 생성/연결 실패 시 부분
런타임과 로그도 지우지 않습니다.

구현은 `python -B -m factorio_agent.lifecycle <command>`입니다. 전역
`runtime/lifecycle.lock`이 BAT 동시 실행을 막습니다. Supervisor는 별도의
`controller.lock`과 단일 게임 writer 경계를 유지합니다.

Supervisor 정지 계약은 `supervisor-stop.json`의
`{request_id,world_id,requested_at,source:'lifecycle'}`입니다. Lifecycle은
`logs/controller-state.json`의 정확한 `safe_stopped`, `state:'stopped'`,
`stop_request_id`, `world_id`, `saved:true` ACK와 프로세스 종료를 모두 기다립니다.
일치하지 않거나 시간 초과면 프로세스를 강제로 죽이지 않습니다. Supervisor가
없는 경우에도 기존 공개 `stop-worker`/`pause`/`checkpoint` 명령과 writer lock을
사용하며, 불확실한 기존 작업을 초기화하지 않습니다.

게임 일시정지는 월드에 묶인 `lifecycle-pause.json` 의도/확인 기록으로 추적합니다.
재개는 소유 기록이 있는 일시정지만 해제합니다. 게임 속도는 1을 유지해야 하며,
물자·기술·연구·건강 값을 변경하지 않습니다. Worker 정지 표시는 안전 ACK가
소유한 정확한 원본 바이트 SHA256과 일치할 때만 기존 CLI로 제거합니다.

프로세스는 PID, Windows 생성 시각, 실행 파일, 파싱된 명령줄의 정확한 런타임
config/모듈 경로를 모두 확인합니다. `taskkill /IM` 같은 광범위 종료는 없습니다.
종료는 먼저 저장 파일 완료를 확인하고 서버의 표준 `/quit`를 요청합니다.
서버의 정상 종료가 지연되면 저장 중일 가능성을 보존하며 강제 종료하지 않습니다.
클라이언트 최종 종료도 재확인한 해당 프로세스 핸들만 사용합니다. 표준 저장/종료
명령은 [Factorio Console 문서](https://wiki.factorio.com/Console)에 정의되어 있습니다.
서버 비밀번호가 담길 수 있는 명령줄은 상태 출력에 노출하지 않습니다.

검증은 가짜 프로세스/월드/시계를 사용합니다. 중복 시작, 정확한
ACK, 일시정지/재개, PID 재사용, 다른 런타임, 외부 정지 표시 보존, 저장 후 종료,
새 시드 백업과 실패 시 보존, 제한된 로그 출력을 확인합니다.
추가로 Windows `cmd --help` 무효과 실행, 공백이 있는 경로에서 7개 도구의 가짜
Python CLI 호출·인수 전달, 보관 파일 31개의 SHA256을 검증합니다.
2026-11-12 이동 전 실제 `start.bat`으로 감독기를 시작했고, `stop`의 정상 저장·게임
일시정지 후 다시 실행해 같은 월드와 같은 발전 작업의 재개를
확인했습니다. 새 맵 재생성·전체 종료의 실제 실행 검증은 아직 하지 않았습니다.

모델은 `gpt-6-luna`, 추론 수준은 `medium`으로 고정되어 있습니다. 설치된 Codex
CLI의 ChatGPT 로그인을 재사용합니다. 실행 전에 `codex login status`로 확인하며,
로그인이 필요한 경우 `codex login`을 실행합니다. API 키는 사용하지 않습니다.
실제 계정으로 구조화된 작업 계획을 받아 큐 실행에 연결한 것까지 확인했습니다.
Codex 사용량 제한이나 로그인 만료 시 실패 이유를 남기고 재시도 간격을 늘립니다.
