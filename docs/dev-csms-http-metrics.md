# Dev namespace의 CSMS HTTP 계측 검증

이번 변경은 development overlay의 Service 라벨, ServiceMonitor, metrics ingress만 추가한다.
prod/canary overlay, Ops 이미지·Rollout·AnalysisTemplate·복구 정책은 변경하지 않는다.
앱 저장소의 dev 브랜치도 변경하지 않는다. 앱 계측 코드는 kim_chanju CI로 Dev에만 배포한다.

## 수집 경로

Prometheus(monitoring) → ServiceMonitor(dev/csms-dev) → Service(dev/csms:9100) → CSMS metrics.
기존 development-boundary의 HTTP·OCPP·DB egress 정책은 유지한다.
추가 허용은 monitoring namespace에서 app=csms Pod의 TCP 9100으로만 한정된다.
기존 Ops ServiceMonitor와 Helm 소유권은 변경하지 않는다.

## 확인 순서

1. GitOps main에 Dev 수집 설정이 게시된 뒤 chargeops-dev가 자동 Sync되는지 확인한다.
   수동 확인이 필요하면 destination.namespace=dev, source.path=.../development를 먼저 확인한다.
   Ops를 관리하는 chargeops-chatops-dev를 Sync하지 않는다.
2. kim_chanju의 기존 Jenkins CI가 계측 이미지를 Dev에 배포하고 read-only 검증을 통과하는지 확인한다.
   source PR이 생성될 수 있지만 dev 브랜치에 Merge하지 않는다. ENABLE_CANARY가 켜져 있어도
   kim_chanju는 Ops 승격 단계의 브랜치 조건을 만족하지 않는다.
3. mgmt에서 조회한다.

```bash
kubectl -n dev get deployment csms
kubectl -n dev get servicemonitor csms-dev
kubectl -n dev get networkpolicy csms-prometheus-ingress
kubectl -n dev get svc csms --show-labels
```

4. Grafana Explore에서 기존 Prometheus 데이터소스로 실행한다.

```promql
up{namespace="dev",service="csms"}
```

```promql
chargeops_csms_http_requests_total{namespace="dev",route="/healthz",method="GET"}
```

정상: up=1, pod/revision 라벨 존재, 2xx 증가, 5xx=0.
첫 요청은 기존 readinessProbe가 발생시키므로 OCPP fixture나 DB 쓰기 테스트를 실행하지 않는다.
샘플이 적을 때 rate/increase 그래프가 비어 있을 수 있으므로 최초에는 원본 카운터부터 확인한다.

이 단계에서는 Ops의 Prometheus 기반 Canary 판정을 활성화하지 않는다.
그 기능은 별도 준비 브랜치의 구성으로 남겨 둔다.
