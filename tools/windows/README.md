# 현재 Windows 실행 도구

일반 실행은 루트의 [Factorio.bat](../../Factorio.bat) 메뉴를 사용합니다. 개별 스크립트는 CLI 인수가 필요한 경우 직접 실행할 수 있습니다.

| 도구 | 동작 |
| --- | --- |
| `start.bat` | 현재 월드와 AI 재개, `--no-gui` 지원 |
| `stop.bat` | AI 안전 정지·게임 일시정지·저장, GUI 유지 |
| `new_map.bat` | 현재 월드 저장·백업 후 설정에 따라 별도 새 월드 생성, `--no-gui` 지원 |
| `gui.bat` | 현재 월드 GUI 표시 |
| `status.bat` | 현재 월드·프로세스·작업 상태 |
| `logs.bat` | 제한된 로그 출력, `--lines 100` 또는 `--open` 지원 |
| `shutdown.bat` | 저장 후 검증된 소유 서버·GUI 종료 |

모든 도구는 자체 위치의 `../..`를 절대 저장소 경로로 해석하고 기존 lifecycle CLI에 인수를 그대로 전달합니다. `FACTORIO_MENU_ACTIVE`가 있으면 개별 도구의 대기를 생략하고 메뉴가 결과 화면에서 한 번만 대기합니다.

이전 실험 실행기는 `v1` 브랜치에만 있습니다.
