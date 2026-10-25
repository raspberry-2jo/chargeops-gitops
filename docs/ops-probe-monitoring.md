# Ops 배포 검사 지표 수집

기존 CSMS 분석 Job에 HTTP 오류율, 성공 응답 p95, 상대 지연시간, Ready Pod 수,
표본 수와 최종 결과를 노출한다. `chargeops_canary_probe_*` 지표는 Dev와 같은 이름이며
Prometheus가 붙이는 namespace 라벨로 구분한다. 별도 패키지 설치 없이 Python 표준 라이브러리를 사용한다.

Ops 기준은 그대로 유지한다: 60회, 오류율 1%, p95 1초, 상대 지연 2배 및 0.2초 하한,
Ready 최소 4개. 25/50/100 배포 단계, CSMS 이미지, Pod 템플릿과 수량은 변경하지 않는다.
최종 판정을 Prometheus가 수집할 수 있도록 Job 종료 전에 20초를 추가로 대기한다.
성공/실패 결과는 기존 검사 코드가 결정한다. 메트릭 서버 시작 실패 시 기존 검사는 계속 실행한다.

## mgmt 적용

진행 중인 Jenkins Ops 배포가 없는 시간에 chargeops-gitops 저장소에서 실행한다.
이 PC에서는 클러스터를 적용하거나 검증하지 않았다.

```bash
git pull --ff-only origin main
python3 scripts/ops-probe-monitoring.py plan
python3 scripts/ops-probe-monitoring.py apply
python3 scripts/ops-probe-monitoring.py check
```

`apply`는 ConfigMap, AnalysisTemplate, metrics Service, ServiceMonitor,
monitoring namespace에서 검사 Pod의 TCP 9110으로 접근하는 NetworkPolicy만 적용한다.
CSMS Deployment/Rollout이나 Argo Application을 apply하지 않는다.
Ops Application의 자동 동기화가 켜져 있거나 배포/분석 진행 중이면 중단한다.
적용 전후 Deployment/Rollout spec이 같은지도 확인한다. 동시에 다른 배포를 실행하지 않는다.

전체 Argo Sync는 대기 중인 다른 운영 변경까지 적용할 수 있으므로 이 수집 연결만을 위해 사용하지 않는다.
기존 Ops Application 이름은 `chargeops-chatops-dev`, namespace는 `ops`다.

`check`는 현재 정상 버전에 독립 AnalysisRun을 실행한다. 기존 세 서비스의 healthz와
Pod 상태를 읽기만 하며 충전기/DB 쓰기나 Rollout 승격/중단을 수행하지 않는다.
검사 실패 시에도 이 독립 실행이 Rollout 자동 복구를 실행하는 것은 아니다.

## 수집 확인

Grafana `ChargeOps 배포 확인 — Dev · Ops`에서 환경 ops, 검사 실행 전체(All), 최근 30분을 선택한다.
오류율/응답시간 그래프가 나타나고 검사 결과가 성공으로 끝나는지 확인한다.
`check` 성공은 검사 실행 성공이며 Prometheus 수집 성공은 별도로 아래 쿼리나 Grafana에서 확인한다.

```promql
count_over_time(chargeops_canary_probe_result{namespace="ops"}[10m])
```

검사 Job이 살아 있는 동안 Target UP 확인:

```promql
up{namespace="ops",service="csms-canary-probe-metrics"}
```

메트릭 Service에는 실행 중인 검사 Pod가 있을 때만 endpoint가 생긴다. 완료된 검사 결과는
과거 시간 범위로 조회한다. 모든 요청 실패 시 p95가 없는 것은 정상이며 오류율과 결과로 판단한다.
Prometheus의 ServiceMonitor label/namespace selector가 이 리소스를 선택해야 한다.
현재 알려진 kps 설정은 두 selector 모두 `{}`다. 실제 설정이 바뀌었다면 selector도 확인한다.

기존 수집에 영향을 주지 않고 신규 연결만 제거하려면 ServiceMonitor csms-canary-probe,
Service csms-canary-probe-metrics, NetworkPolicy csms-canary-probe-metrics를 제거한다.
지속 제거 시에는 Git 구성도 함께 변경해야 한다.
