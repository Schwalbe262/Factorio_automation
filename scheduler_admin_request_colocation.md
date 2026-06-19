# 스케쥴러 same_node_as 코로케이션 디스패치 이슈 (factorio-ai)

작성: 2026-06-17 ~15:08 KST (06:08 UTC)

## 한 줄 요약
이미 **running** 상태인 reference 태스크(vLLM 서비스)에 `same_node_as_task_id`로 붙인 **gpus=0 CPU 클라이언트**가, 그 노드가 거의 비어있는데도 즉시 디스패치되지 않고 수십 분~수 시간 queued로 머뭅니다. 클라이언트 쪽 타임아웃(15분)을 한참 넘겨서, 워밍 서비스 + 온디맨드 클라이언트 패턴이 사실상 동작하지 않습니다.

## 증상
- autopilot의 strategy/layout/foundry 요청(전부 gpus=0, `same_node_as_task_id`=서비스 id)이 매번 ~15분 client timeout.
- 5시간 넘게 게임 진행 0 (LLM 결정을 한 번도 못 받음).
- 서비스가 ready로 확인되지 않아 supervisor가 서비스를 반복 cancel/resubmit → vLLM 서비스 17개+ cancelled, 큐에 zombie 클라이언트 74개+ 누적.

## 핵심 증거 (실제 태스크, 06:08 UTC 기준)
| id | name | status | node | same_node_as | gpus | created(UTC) |
|----|------|--------|------|--------------|------|--------------|
| 8844 | factorio-vllm-service-...-7d70acc0 | running | n104 | - | 1 | 00:11:30 |
| 8846 | factorio-strategy-request-382bcf0e | running | n104 | 8844 | 0 | 00:16:52 |
| 8849 | factorio-layout-improvement-request-3c5d67a4 | **queued** | (none) | 8844 | 0 | 00:19:55 |
| 8851 | factorio-layout-improvement-request-9dc075aa | **queued** | (none) | 8844 | 0 | 00:23:10 |
| 8791 | factorio-vllm-service-...-ac822798 | failed | n104 | - | 1 | 2026-06-16 23:33:06 |
| 8793 | factorio-strategy-request-96bf7214 | **queued** | (none) | 8791(=failed) | 0 | 2026-06-16 23:38:13 |

결정적 포인트:
1. **8844는 n104에서 정상 running** 이고, 그 시점 **n104에서 돌던 태스크는 8844 단 하나** (노드 거의 빔). 클러스터 전체 GPU 사용량 = 1 (= 우리 서비스). 즉 **GPU/노드 자원 부족이 아님.**
2. 그런데 같은 reference(8844)에 붙인 gpus=0 클라이언트 중 **8846은 약 5시간 뒤에야 running**, **8849/8851은 계속 queued**. → reference가 이미 running이어도 co-located 클라이언트가 제때(초 단위) 디스패치되지 않음.
3. reference 서비스가 죽으면(8791 failed) 거기 붙은 클라이언트(8793 등)는 **영원히 queued** (죽은 reference를 기다림).

## 우리가 보내는 요청 형태
비-서비스 태스크 제출 시:
```
same_node_as_task_id = <현재 running vLLM 서비스 task id>
node_name = ""        # 비움
gpus = 0              # CPU-only, 서비스의 127.0.0.1:8000 으로 HTTP 호출
```
(서비스 id는 매 제출 직전 /api/tasks에서 running 서비스로 resolve)

## 추정 원인 (가설)
- co-location 바인딩이 reference의 짧은 **attaching** 구간에만 적용되고, reference가 **running**으로 넘어간 뒤 제출되는 클라이언트는 그 윈도우를 놓쳐 일반 큐로 떨어지는 듯합니다. 그런데 gpus=0 + node_name="" 라서 일반 스케줄링에서도 배치 노드가 안 정해져 계속 queued.
- 우리 서비스는 **장수명(12h)** 이고 클라이언트는 **온디맨드**라, 클라이언트는 거의 항상 서비스가 이미 running일 때 제출됩니다 → 항상 윈도우를 놓침 → 구조적으로 co-location이 성립 안 함.

## 기대 동작 (요청사항)
1. reference 태스크가 **running(실 노드 보유)** 상태일 때 제출되는 `same_node_as_task_id` 클라이언트도, 그 노드에 여유가 있으면 **즉시(초 단위)** 해당 노드로 디스패치되어야 함 (attaching 구간 한정 X).
2. gpus=0 + node_name="" + same_node_as_task_id 조합이 reference 노드로 핀 고정되어 정상 실행되는지 확인 부탁.
3. (가능하면) reference가 failed/cancelled가 되면 거기 붙은 queued 클라이언트는 무한 대기 대신 즉시 실패시켜 주면 zombie 누적을 막을 수 있음.

## 재현 절차
1. 아무 노드 X에 장수명 태스크 T(gpus=1)를 제출하고 **running**까지 대기.
2. T가 running인 상태에서 `same_node_as_task_id=T.id`, `node_name=""`, `gpus=0` 인 짧은 CPU 태스크 C 제출.
3. 관찰: X에 여유가 충분한데도 C가 즉시 디스패치되지 않고 queued로 머묾.

## 참고
- 현재 우리 쪽 클라이언트 제출 코드/필드는 그대로 두고(아키텍처 유지) 스케쥴러 측 디스패치 동작만 확인/수정 요청드립니다.
- 필요하시면 위 태스크 id들의 스케쥴러 로그 확인 가능합니다. (서비스 슬러그: `factorio-vllm-service-qwen-qwen3-5-9b-*`)
