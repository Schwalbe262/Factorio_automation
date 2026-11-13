# 초기 과학 한 배치 미리 생산

> Current rule (English, supersedes the v1 limits below): the trigger is the owner's
> `funding_complete` (fully funded, research consuming; packs may still be produced),
> any finite red-only bootstrap successor is supported, the successor's baselines are
> the owner's computed batch end, its budget is held + public plates minus other
> critical jobs' claims (no construction lookahead, no kept batch), and packets are
> whole 5-pack packets. See PLANNERS.md "Back-to-back bootstrap batches".

현재 연구의 기계 생산·출력 회수·실험실 공급이 모두 끝나고 연구만 진행 중이면, 같은 셀에서 승인된 다음 연구 팩을 미리 생산할 수 있습니다. v1은 `electric-mining-drill → bootstrap_logistics` 한 경로를 구현합니다. 다른 연구 순서를 추정하거나 새 작업을 승인하지 않습니다.

`SciencePipeline(game, bootstrap, builder, catalog)`의 공개 호출은 다음과 같습니다.

```python
action = pipeline.next_action(obs, current_controller,
                              owner_key=current_job_key,
                              surplus_budget=None)
# action이 있을 때만 기존 writer가 일반 행동을 실행합니다.
pipeline.record_action(action, native_receipt)
```

`owner_key`는 생략 시 같은 세계의 유일한 진행 중 광산 연구 작업에서 찾습니다. 결과는 일반 `take`/`take_many`/`insert` 또는 `None`이며 상태 이유는 `last_diagnostic`에 남습니다. 현재 연구의 주 실행기가 먼저 성공 여부를 처리하고, 아직 기다리는 경우에만 호출해야 합니다. 게임 행동과 영수증 전달은 중앙 실행기의 기존 단일 writer가 담당합니다.

현재 제어기의 읽기 전용 `production_complete(obs)`는 원래 유한 원료가 실제 지불됐고, 필요한 새 팩이 모두 생산·회수·공급됐으며, 기계·실험실·세계·카탈로그·계획·유닛이 여전히 맞을 때만 증거를 반환합니다. 미확정 전송이 있으면 거부합니다. `_progress`와 옛 유상 재조달 배치 증거의 `autonomous_wait` / `background_work_safe`는 추가로 현재 연구가 실제 진행 중이고 기준값 이후 연구 소비가 있으며 전력 공급도 살아 있을 때만 `True`입니다. 그 외에는 `False`입니다. 증거 계산은 파일·게임을 변경하지 않습니다.

다음 작업은 같은 세계의 `work_queue.json`에서 승인된 미완료·비일시정지 물류 노드만 고릅니다. 의존 노드는 완료됐거나 현재 연구 노드여야 하고, 실제 네이티브 선행 기술과 생산 레시피도 확인합니다. 우선순위에 따라 한 건만 선택하고 `science-pipeline.json`에 고정합니다. 다른 노드로 자동 재지정하거나 사용자가 일시정지한 노드를 재개하지 않습니다. 작업·정지 표시·세계·미확정 행동이 바뀌면 추가 공급을 중단합니다.

더 높은 순위의 승인된 초기 연구가 다음 후보이면 v1은 물류를 대신 고르지 않고 지원하지 않는 경로라고 보류합니다. 한 번 예약한 공용 셀의 원료와 다음 팩은 현재 소유자와 예약된 물류 작업만 사용할 수 있습니다. 물류가 자연 완료되기 전 다른 초기 연구는 예약 이유를 남기고 중단합니다. 예약 목표를 일시정지해도 다른 연구로 원료를 자동 전용하지 않습니다.

다음 제어기의 `prewarm(obs, owner=..., surplus_budget=...)`는 원래 현재 배치의 기준값이나 예산을 바꾸지 않습니다. 새 `bootstrap-logistics.json`에 다음 목표 고유의 기계 생산 기준값·유닛·예산을 **원료 공급 전에** 저장합니다. 옛 형식이나 다른 경로에서 이미 시작된 목표는 새 기준값으로 덮어쓰지 않습니다.

정상 철판·구리판을 최대 5팩 분량씩 균형 투입하며, 같은 진행률에서는 구리를 먼저 넣습니다. 원료가 일부만 실제 이동하면 그 수량만 기록하고 재시작 후 나머지만 지불합니다. 미리 생산하는 동안 연구 선택, 실험실 공급, 다음 팩 회수, 수작업 원료 채굴·제작은 하지 않습니다. 생산된 다음 팩은 보호된 셀 출력에 남습니다. 다음 작업이 주 작업으로 시작되면 같은 상태를 이어 받아 생산·회수·공급·자연 연구 증명을 완료합니다. 일부만 미리 지불했어도 균형 투입을 계속합니다.

기본 여유분은 플레이어 재고와 소유된 공개 석재/강철 용광로의 이미 생산된 철판·구리판을 합친 뒤, 현재 건설 원료 예산과 다음 배치 한 번분을 남깁니다. 등록된 사유 버퍼는 기존 건설 수집기 필터를 따릅니다. 호출자가 별도 `surplus_budget={'iron-plate': n, 'copper-plate': m}`를 전달하면 그 범위만 사용합니다. 부족하면 미리 생산을 보류하며 준비되지 않은 원료를 채굴하거나 필수 재료를 무제한 수집하지 않습니다.

검증 범위는 실제 이동 ACK, 다음 기준값 선예약, 구리/철 균형, 부분 투입·재시작·불명확 영수증, 승인·DAG·일시정지·다른 세계, 공개 완제품 수집·건설 여유, 현재 연구 완료 우선, 새 주 작업의 기존 상태 승계, 엄격한 자동 대기 표시입니다. 중앙 실행기와 영수증 전달을 연결했으며, 같은 실행 구간에서 다음 제어기의 상태가 덮어써지지 않는 것도 검증합니다. 실제 월드의 선행 생산 효과는 아직 별도 검증이 필요합니다.
