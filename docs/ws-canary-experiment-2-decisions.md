# 실험 1·2 기준 확정 및 판정기 후속 수정 계획

기록일: 2026-11-01. 대상: Dev `csms-ws-lab`. 사용자 제공 실험 결과를 근거로
작성했다. 원시 시계열·R1 이벤트·실제 서버 설정은 이번 작업에서 직접 조회하지 않았다.

**이번 작업은 기준 확정과 문서 기록이다. 실험 3·4의 비교 가능성을 위해
실행 중인 판정기, 서버 ConfigMap, Rollout을 변경하지 않는다.** 앞서 작성한
실험 1용 복붙 적용 가이드도 실험 3·4가 끝날 때까지 실행하지 않는다.

## 1. 확정할 배포 설정

| 설정 | 확정값 | 근거와 범위 |
|---|---|---|
| replicas | 4 | 이번 실습의 가용성 기준. HPA 제외 |
| maxSurge | 1 | 이번 실험에서 2는 quota 제약(Pod 10/10, CPU 3900m/4)에 걸림. quota 잔여량은 다른 워크로드에 따라 변함 |
| maxUnavailable | 0 | 가용 4개 유지 기준과 일치. 1은 계획된 교체에서도 3개를 허용해 실패 조건과 충돌 |
| minReadySeconds | 30 | R2에서 0은 약 4%(1.5분)의 개선만 보고됨. 30초 유지. readiness 자체가 WebSocket 정상 처리를 증명하지는 않음 |
| Analysis count / failureLimit | 1 / 0 | Web provider 1회 안에서 여러 차례 관측. 현재 execute 상한 540초(9분)이므로 count 5는 단순 샘플 5개가 아님. 오탐 원인을 먼저 수정 |

로컬 Dev YAML은 위 값과 이미 일치한다. 불필요한 값 변경을 하지 않았다.
`progressDeadlineSeconds: 600`, `progressDeadlineAbort: true`, Web timeout 660초도
이번에는 그대로 둔다. 600초는 전체 배포 완료 시간을 보장하는 값이 아니며,
9분은 execute 내부 상한이지 실제 단계의 고정 소요 시간이 아니다.

## 2. 확정할 지표 기준과 현재 로컬 코드의 차이

| 지표 | 확정안 | 현재 로컬 작업본 / 후속 조치 |
|---|---|---|
| 연결 감소율 | 안정 구간 2% 초과 실패, 전환 후 60초 제외 | 이미 반영. Drain 또는 Pod 구성 변경 시 안정 타이머를 초기화 |
| 가용 Pod | 4개 미만 실패 | 이미 반영. 보고된 감지 8초는 실험 관측값이며 보장시간 아님 |
| OCPP 오류·Timeout | 1% 초과 실패, 잠정 | 이미 반영. 장애 주입으로 추가 검증 필요 |
| 비정상 WebSocket 종료 | 1% 초과 실패, 잠정 | 이미 반영. 정상 종료형 Pod 삭제로는 충분히 검증되지 않음 |
| Heartbeat 성공률 | 99% 유지, 계산 수정 후 재검증해 자동 판정 복귀 | 앞선 실험 1 작업본은 observe-only. 지금 enforce로 되돌리면 알려진 오탐을 재도입할 수 있어 변경 보류 |
| 처리량 감소율 | 관측 전용. 15% 후보값 유지 | 앞선 실험 1 작업본은 이미 observe-only. 실서버 적용 여부는 미확인. 측정 개선 전 기준을 임의로 25/30%로 높이지 않음 |
| 입력 프로필 차이 | 10% 후보값 유지 | 처리량 비교의 전제조건. 처리량이 관측 전용일 때 이 조건만으로 전체 판정을 보류하지 않음 |
| 재연결 p95 | 관측 전용, 10초 후보 | 계측 부재. 수치를 만들어 성공으로 표시하지 않음 |

오류율·비정상 종료율의 임계값과 같으면 통과, 초과하면 실패다. Heartbeat는
자동 판정 복귀 후 99% 미만이면 실패하도록 한다. OOM·재시작·앱/DB·표본·미수집
안전 조건은 이번 결론으로 완화하지 않는다.

**현재 서버와 로컬은 같다고 가정하지 않는다.** 앞선 변경은 로컬 수정·테스트만
완료했으며 서버 적용 결과를 받지 않았다. 이미 서버에 적용했다면 그 상태를
실험 3·4의 기준으로 기록하고, 중간에 다시 바꾸지 않는다.

## 3. 처리량이 무엇인지

이 판정기의 처리량은 네트워크 대역폭이 아니라 **연결 수와 시간으로 보정한
OCPP 성공 메시지 수**다. 코드의 단위는 메시지/연결/초이며 60을 곱하면
메시지/연결/분으로 표현할 수 있다.

```
정규화 처리량 = 측정 창의 성공 메시지 수 / (창의 평균 연결 수 × 창 길이[초])
감소율[%] = max(0, 100 × (1 − Canary 정규화 처리량 / 기준 정규화 처리량))
```

Stable과 Canary가 서로 다른 연결 수를 받아도 비교하려는 목적이다. 100% 단계는
살아 있는 Stable 대신 저장한 baseline을 사용한다. 분모가 0이거나 데이터가
없으면 유효한 감소율을 계산할 수 없다.

현재 Lab은 Heartbeat를 약 30초마다, MeterValues를 약 60초마다 보내며 접속 시
StatusNotification도 보낸다. 따라서 안정 상태에서 주기 메시지는 연결당
분당 약 3개지만, **정확히 항상 3개여야 한다는 판정식은 아니다.** 재접속·기동
구간·메시지 발송 위상·실제 부하에 따라 달라진다.

정상 실험에서 22.9%까지 흔들렸으므로 지금 방식의 15% 초과를 서비스 장애로
확정할 근거가 부족하다. 숫자를 높이는 대신 측정을 개선하고 관측 범위를
다시 구한다. 입력 프로필 불일치 때문에 처리량이 UNVERIFIED일 때 이를 전체
판정에서 분리하는 것이 보류 감소의 핵심이다.

## 4. 실험 3·4 이후 수행할 실제 코드 변경

| 변경 | 구현 설계 | 반드시 확인할 점 |
|---|---|---|
| 조회 시각 | Prometheus 판정 데이터에 공통 evaluation timestamp `wall_now − 30s` 사용 | 같은 평가의 분자·분모·연결 수를 동일한 시각으로 조회. 현재 코드도 같은 now를 전달하지만 실시간 수집 중 조회 경합 가능성은 남음. 30초 지연만으로 원인 해결을 보장하지 않음 |
| 처리량 창 | 처리량 및 메시지 종류별 입력 프로필을 120초 이상으로 비교 | `Collector.role(window=120)`만 일괄 변경하면 오류·Heartbeat·종료율 창도 바뀜. 처리량용 창을 별도로 분리하고 양쪽 시간·표본·coverage를 맞춰 구현 |
| 관측 전용 | 처리량 값·후보 한계·미검증 이유를 남기고 전체 판정에서 제외 | NaN/표본 부족을 PASS로 변환하지 않음. 다른 자동 판정 지표의 미수집은 계속 보류 |
| Heartbeat 복귀 | 99% 유지. 경합·counter 일관성 검증 후 enforce 전환 | 실제 실패 0에서 오탐이 사라지는지와 의도적 실패를 잡는지 모두 검증 |

지연시각은 Pod 가용성·OOM 같은 현재 Kubernetes 상태 조회에 무조건 적용하지
않는다. 120초 처리량 창과 기존 연결 감소율 안정 구간 60초도 별개다.
데이터 coverage·시계·membership 변경·baseline 정렬을 함께 설계해야 한다.

120초 창을 단계 시작 직후 쓰면 이전 단계 데이터가 섞일 수 있다. 후보가 충분히
오래 수집됐는지, 전체 창이 비교 가능한 안정 구간인지 확인한다. 이를 엄격히
지키면 판정 시간이 더 필요할 수 있으므로 **단계당 1~2분 성공은 확정값이나
완료 기준이 아니다.** 현재 두 비중첩 안정 창을 확인하는 조건, 수집 지연,
Pod 기동과 minReadySeconds를 포함해 다시 측정한다.

## 5. 현재 상태 기록 — 읽기 전용, mgmt Bash

CTX가 기존 실습 context로 설정된 상태에서 실행한다. 출력은 실험 결과와 함께
저장한다. 다음 명령은 적용·재시작·삭제하지 않는다.

```bash
: "${CTX:?기존 실습 context를 CTX에 설정하세요}"
kubectl --context "$CTX" -n dev get rollout csms-ws-lab \
  -o jsonpath='replicas={.spec.replicas}{"\n"}maxSurge={.spec.strategy.canary.maxSurge}{"\n"}maxUnavailable={.spec.strategy.canary.maxUnavailable}{"\n"}minReadySeconds={.spec.minReadySeconds}{"\n"}'
kubectl --context "$CTX" -n dev get analysistemplate csms-ws-lab-availability -o yaml
kubectl --context "$CTX" -n dev get configmap csms-ws-lab-settings -o yaml
kubectl --context "$CTX" -n dev get analysisruns --sort-by=.metadata.creationTimestamp
kubectl argo rollouts get rollout csms-ws-lab --context "$CTX" -n dev
```

R1 사유는 해당 AnalysisRun의 describe, evaluator 로그의 실제 failed signal,
같은 시각의 가용 Pod·이벤트로 대조한다. 출력이 없으므로 R1의 확정 원인을
새로 단정하지 않는다. Lab 정리·삭제는 이번 문서 작업에서 수행하지 않았다.

## GitOps 반영 범위

사용자 정정에 따라 확정값·근거와 앞선 실험 1의 관측 모드 구현을 GitOps에
커밋한다. Dev 실습 Rollout에는 서버에서 이미 수행한 `nodeTaintsPolicy: Honor`
수정을 원본에도 보존한다. 조회 시각 −30초와 처리량 창 120초는 아직 구현하지
않으며, Heartbeat의 99% 후보값은 계산 수정·재검증 전까지 관측 전용이다.

저장소의 `chargeops-dev` Application 선언은 `main`의
`chargeops-chatops/overlays/development-rolling`을 가리킨다.
`dev-ws-lab`은 별도 실습 경로다. 이 작업에서 기존 Application의 경로를 바꾸거나
새 Application을 만들지 않는다. 따라서 Git 푸시는 실제 Dev Sync 완료와 다르다.
기존 실습 적용 절차 또는 별도 Application 연결이 필요하다. Ops 설정과 Ops 자동
판정 기본값은 유지한다.

## 6. 다음 검증 순서

1. 실험 3·4까지 코드·이미지·설정·부하·quota 조건을 동결하고 기록한다.
2. 결과와 원시 로그를 보관한 뒤 별도 브랜치/변경 묶음으로 판정기를 수정한다.
3. 같은 부하로 정상 반복 실험: 이전·이후 단계별 시간, 보류 이유, 오탐 수,
   처리량 분포, Heartbeat 분자·분모와 평가시각을 비교한다.
4. 경계·장애 재현: 안정 연결 감소 2% 초과, Pod 3개, 오류·종료율 1% 초과,
   실제 Heartbeat 실패, NaN·미수집·표본 부족·느린 scrape를 검증한다.
5. 처리량은 60/120/180초 창을 같은 기록에서 비교해 편향과 감지 지연을 함께
   평가한다. 120초가 최종 정답이라고 가정하지 않는다.
6. Heartbeat 재활성화와 처리량 실패 기준 확정은 정상·장애 분포를 확인한 뒤
   결정한다. Ops 적용은 별도로 검토한다.
