# 자동화 설정

루트의 [Factorio.bat](../../Factorio.bat) 메뉴 7에서 [automation_config.json](../../automation_config.json)을 메모장으로 편집합니다. UTF-8과 UTF-8 BOM을 지원하며 추가 패키지는 필요하지 않습니다. 파일이나 개별 항목이 없으면 아래 기본값을 사용합니다.

```json
{
  "world": {
    "starting_area_percent": 200,
    "nauvis_cliffs": false,
    "seed": null
  },
  "controller": {
    "decision_interval_seconds": 60,
    "execution_slice_seconds": 25
  }
}
```

| 항목 | 허용값 | 적용 시점 |
| --- | --- | --- |
| `world.starting_area_percent` | 0–600의 유한한 숫자 | 새 월드 생성: 200% → Factorio `starting_area=2.0`, 600% → `6.0` |
| `world.nauvis_cliffs` | `false` 또는 `true` | 새 월드 생성: 기본 `false`는 Nauvis 절벽 비활성화, `true`는 게임 기본 절벽 설정 |
| `world.seed` | `null` 또는 0–4294967295 정수 | 메뉴 3 / `tools/windows/new_map.bat`: `null`은 사용하지 않은 무작위 시드, 정수는 해당 시드 |
| `controller.decision_interval_seconds` | 10–3600초 | 다음 컨트롤러 프로세스 시작 |
| `controller.execution_slice_seconds` | 1–50초 | 다음 컨트롤러 프로세스 시작 |

메뉴 1 / `tools/windows/start.bat`은 현재 월드를 재개합니다. 설정을 바꿔도 기존 월드의 지형, 시작 구역, 적 설정은 바뀌지 않습니다. 실행 중인 컨트롤러도 재시작하기 전까지 기존 주기를 유지합니다. 설정 주기는 의사 결정의 기준 간격이며 기존 이벤트 대응 스케줄링은 유지됩니다. 모델은 기존 `gpt-6-luna` / `medium` 계약을 사용합니다.

메뉴 3 / `tools/windows/new_map.bat`을 명시적으로 실행하면 기존 월드를 안전 정지·저장·백업한 뒤 새 월드를 생성합니다. 설정 오류와 이미 존재하는 지정 시드는 정지·저장 전에 거부합니다. 이전 런타임을 덮어쓰거나 삭제하지 않습니다. 일반 CLI의 `create --seed ...`는 명시한 시드를 우선하며 시작 구역·절벽 설정만 설정 파일에서 가져옵니다.

설정은 현재 디렉터리가 아닌 실제 소스 저장소 루트의 절대 경로에서 읽습니다. 인덱스 스냅샷 실행도 스냅샷 안의 설정을 사용합니다. 새 런타임의 `world-generation-config.json`에는 실제 생성 설정·시드·설정 원문 해시를 고정해 기록합니다. 새 컨트롤러의 `controller-config.json`에는 실제 시작 인수와 PID를 기록합니다. 기존 월드를 시작할 때 과거 생성 설정을 새 기본값으로 재해석하지 않습니다.

알 수 없는 키, 중복 키, 잘못된 타입, 범위 밖 값, NaN/Infinity는 오류입니다. 숫자 항목의 `true`/`false`도 거부합니다. 자원·공해·적의 다른 생성 설정은 Factorio 기본값을 사용합니다.

검증은 설정 파싱, 실제 map-gen JSON 생성 인수, 명시 시드 우선순위, 기존 월드 불변, 안전 정지 전 오류 차단, 컨트롤러 재시작 적용을 가짜 프로세스/실행기로 검사합니다. 이 변경 검증에서 실제 월드 생성·정지·재시작은 하지 않았습니다.
