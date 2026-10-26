> 새 목표 구성은 Dev RollingUpdate + Ops Canary다. 이 문서의 Dev gate는 기존 development-canary 호환 구성에 해당한다.
> 새 Dev는 Jenkins 배포 후 실통신 검사를 사용하며, Ops는 5단계 지표 검사와 후보 직접 검사를 함께 사용한다.
> 전환·시험 ID·대시보드 적용은 [새 배포 안내](dev-rolling-ops-websocket.md)를 따른다.

# Dev·Ops CSMS 5단계 Canary 기준

Dev와 Ops 모두 GATE_ENABLED=true다. 새 앱 Jenkins 배포가 환경별 기준 시각을 기록하고
검사 설정을 반영해야 실제 판정이 실행된다. 이 문서는 코드 기준이며 실클러스터 통과 증거는 아니다.

| 단계 | 실패 조건 | 관측 |
|---|---|---|
| L1 인프라 | 분석 참여 Pod Ready 100% 미달, restart, CrashLoopBackOff | 5초 간격. Dev 최소 2개, Ops 최소 4개 |
| L2 앱·DB | csms 또는 csms-canary /healthz 한 번 실패 | DB SELECT 1 포함 |
| L3 연결 | 활성 연결이 배포 전 평균보다 30% 이상 감소 | 환경 전체 연결 수 |
| L3 종료 급증 | 비정상 종료/분 > max(기준×3, 기준+10) | 정상 close·drain·fixture 제외 |
| L3 재접속 급증 | 확인 재접속/분 > max(기준×3, 기준+10) | 같은 Pod 프로세스, 1시간 내 동일 ID |
| L3 Canary 종료 | 후보 Pod 일반 연결의 비정상 종료 관측 | 최근 1분 rate >0 |
| L4 OCPP | Heartbeat·MeterValues·StatusNotification 성공 처리량 합계 50% 이상 감소 | 초기 설정값 50 |
| L5 오류 | error+timeout / 전체 핸들러 결과 >5% | 최소 10건. 정확히 5%는 허용 |

HTTP p95와 Stable 대비 HTTP 지연은 관측만 하며 자동 실패 조건에서 제외했다.
Health/DB 검사는 유지한다. 일반 서비스의 메시지 실패·시간 초과 계측은 새 CSMS 이미지가 필요하다.
Ops에는 실제 통신 트래픽이 있어야 하며 Dev 가상 충전기를 Ops에 배포하지 않는다.

## 기준과 최초 계측 전환

앱 env_cd.py prepare는 Dev, promote는 Ops의 gate-settings.yaml에 배포 직전 GATE_BASELINE_AT를 기록한다.
배포 전 5분 이력을 고정 기준으로 사용한다. 연결은 평균, 카운터는 5분 rate다.
현재 카운터도 최근 5분 rate이며 배포 요청 후 5분 및 각 분석 단계 시작 후 최소 60초가 지나야 비교한다.
그동안 L1/L2와 Canary 비정상 종료를 계속 검사한다. Job 제한은 420초다.

최초 새 지표 도입 시 과거 성공 처리량이 없으면 전후 모두 기존 chargeops_ocpp_messages_total 수신량으로 비교한다.
과거 비정상 종료·확인 재접속 지표가 없으면 기존 전체 closed·accepted 발생률을 각각 보수적인 기준 상한으로 쓴다.
이 경우 chargeops_canary_gate_value{signal="baseline_compatibility_mode"}=1이다.
새 지표 이력이 존재하면 0이며 성공 처리량과 구분된 종료·재접속 기준을 사용한다.
이전 지표도 없으면 성공 처리하지 않는다. 현재 오류율·후보 비정상 종료는 새 계측이 반드시 필요하다.
연결 기준 0, 기준 처리량 10건 미만, 현재 결과 10건 미만, 지표 부재·비정상 값은 미검증으로 승격을 막는다.

종료는 WebSocket 코드와 drain 상태로 분류한다. 정상 코드로 종료된 사용자 장애까지 식별하지는 못한다.
재접속은 Pod 간 공유 상태가 없으므로 다른 Pod 이동·프로세스 재시작 뒤 재접속은 집계하지 못한다.
오류율은 OCPP 핸들러 예외·시간 초과이며 모든 프로세스 예외를 뜻하지 않는다.
환경 전체 비교는 외부 충전기 정지나 트래픽 변화에도 반응하므로 실패 원인을 로그와 함께 확인한다.

## 적용과 확인

GitOps main 변경을 mgmt에서 받는다. 기존 추가 Ops 빌드 Job은 중지하고 앱의 단일 chargeops-ci를 실행한다.
Dev는 Argo 자동 Sync, Ops는 승인 후 Jenkins가 Deployment와 검사 설정·RBAC·수집 리소스를 선택 Sync한다.
Ops 기준 시각 초기값 0 상태에서 수동 검사하면 미검증이다. 새 Jenkins 승격이 기준 시각을 기록해야 한다.
현재 배포에 대한 검사 결과는 새 AnalysisRun으로 확인한다. 단순 Sync만으로 과거 검사를 다시 실행하지 않는다.

```bash
git pull --ff-only origin main
kubectl -n dev get rollout csms
kubectl -n ops get rollout csms
kubectl -n dev get analysisruns --sort-by=.metadata.creationTimestamp
kubectl -n ops get analysisruns --sort-by=.metadata.creationTimestamp
kubectl -n ops describe analysisrun 실제분석이름
kubectl -n ops get pods -l chargeops.io/role=canary-probe
kubectl -n ops logs 실제검사Pod이름
```

Prometheus Service는 monitoring의 9090이며 실제 이름이 다르면 settings를 먼저 변경한다.
gate ServiceAccount는 환경 내 pods/services 읽기 권한을 사용한다. egress는 CSMS 8000,
monitoring 9090, DNS 및 Kubernetes API를 허용한다. 별도 수신 NetworkPolicy가 있으면 허용 여부도 확인한다.
독립 `scripts/ops-probe-monitoring.py check`는 HTTP 수집 경로 확인이며 전체 5단계 통과 증명이 아니다.

## 대시보드

Git 밖 chargeops-websocket-overview-ko.json을 같은 UID로 Import한다. dev/ops를 선택한다.
L1~L5 카드 → 실제 통신·연결 수 → gate 관측값과 실패 한계 → 검사 Pod별 표 순서로 본다.
카드는 선택 기간의 검사 이력이며 과거 실패도 포함한다. 미검증은 성공이 아니다.
최종 배포 성공은 Jenkins, 승인 digest, Rollout Healthy/Completed, 해당 AnalysisRun Successful을 함께 확인한다.
