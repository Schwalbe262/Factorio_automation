# 이전 BAT 비활성 보관함

현재 실행 진입점은 루트의 [Factorio.bat](../../Factorio.bat)입니다. 현재 개별 도구 7개는 [tools/windows](../windows/README.md)에 있습니다.

아래 31개는 루트에서 이동한 원본이며 이름 끝에 `.txt`를 붙여 비활성 보관했습니다. 원본 바이트·줄바꿈은 변경하지 않았고, [SHA256SUMS.txt](SHA256SUMS.txt)는 이동 전후 동일성을 검증한 해시입니다. 현재 실행기로 사용하지 않습니다. 옛 상대 경로·프로세스 종료·월드 초기화 동작도 그대로 남아 있으므로 파일명 변경 후 실행하는 방식으로 재사용하지 마세요. 기존 구현은 `v1` 브랜치에서도 보존합니다.

| 원래 루트 이름 → 현재 보관 파일 (`.txt` 추가) | 이전 용도 |
| --- | --- |
| `continue_factorio_cli.bat` | 과거 Codex CLI 인계 프롬프트 |
| `reset_factorio_no_mod_map.bat` | 과거 no-mod 서버 강제 정지·맵 덮어쓰기 초기화 |
| `restart_factorio_no_mod_unattended_llm.bat` | 과거 Qwen 감독기·서버 재시작 |
| `restart_factorio_no_mod_unattended_llm_full_reset.bat` | 과거 원격 vLLM까지 포함한 전체 재시작 |
| `run_factorio_deterministic.bat` | v1 deterministic 새 월드·재개 |
| `run_factorio_gui.bat` | 과거 개발 모드 설치·GUI 실행 |
| `run_factorio_non_gui.bat` | 과거 개발 서버·전략 루프 |
| `run_factorio_no_mod_autopilot.bat` | 과거 no-mod 자동 진행 |
| `run_factorio_no_mod_codex_wait_layout_loop.bat` | 과거 Codex 대기 중 레이아웃 탐색 |
| `run_factorio_no_mod_idle_layout_loop.bat` | 과거 유휴 레이아웃 탐색 |
| `run_factorio_no_mod_iron_mvp.bat` | 과거 철 생산 MVP·맵 초기화 |
| `run_factorio_no_mod_llm_autopilot.bat` | 과거 Qwen 기반 no-mod 전략 루프 |
| `run_factorio_no_mod_real_player_llm_autopilot.bat` | 과거 GUI 실제 플레이어 LLM 실험 |
| `run_factorio_no_mod_server.bat` | 과거 no-mod LAN/RCON 서버 |
| `run_factorio_no_mod_skill_foundry_loop.bat` | 과거 자동 스킬 생성·검증 루프 |
| `run_factorio_no_mod_unattended_fle.bat` | 과거 FLE 코드 생성 감독기 |
| `run_factorio_no_mod_unattended_llm.bat` | 과거 PowerShell Qwen 감독기 |
| `run_factorio_no_mod_watch_gui.bat` | 과거 no-mod GUI 관찰·필요시 서버 생성 |
| `run_factorio_restore_steam_mods.bat` | 과거 Steam 모드 목록 복원 |
| `run_factorio_review_gui.bat` | 과거 개발 서버 GUI 검토·review lock |
| `run_factorio_slurm_llm_27b_gpu3_queue.bat` | 과거 27B 3-GPU Slurm 대기열 |
| `run_factorio_slurm_llm_4b_attached_benchmark.bat` | 과거 4B Slurm 벤치마크 |
| `run_factorio_slurm_llm_4b_worker.bat` | 과거 4B 원격 워커 |
| `run_factorio_slurm_llm_9b_attached_benchmark.bat` | 과거 9B Slurm 벤치마크 |
| `run_factorio_slurm_llm_9b_worker.bat` | 과거 9B 원격 워커 |
| `run_factorio_vanilla_freeplay.bat` | 과거 Steam 바닐라 새 Freeplay |
| `run_factorio_vanilla_gui.bat` | 과거 Steam 바닐라 GUI |
| `run_factorio_vanilla_probe.bat` | 과거 창 캡처·입력 탐침 |
| `run_factorio_watch_gui.bat` | 과거 no-mod 관찰 도구 별칭 |
| `watch_factorio_deterministic.bat` | 과거 v1 adapter-smoke 관찰 |
| `watch_factorio_v2.bat` | 과거 v2 dev-101 관찰 전용 |
