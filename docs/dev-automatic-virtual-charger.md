# Dev 가상 충전기 자동 연결

`development-canary` overlay에만 `dev-vcharger` Deployment를 추가한다.
CSMS가 준비되면 실제 WebSocket/OCPP 연결을 시작하고, 접속 실패·재배포·Pod 교체 때 최대 30초 간격으로 재시도한다.
세 대의 ID는 `CI-DEV-SIM-01`, `CI-DEV-SIM-02`, `CI-DEV-SIM-03`이다.
BootNotification, StatusNotification, Heartbeat, MeterValues를 전송한다.
충전 전류는 0A이고 상태는 Available인 시뮬레이션이다.

## 데이터와 검사 범위

새 CSMS 코드는 `DEV_SIMULATION_ENABLED=true`, `ENVIRONMENT=dev`, Downward API의
`POD_NAMESPACE=dev`를 모두 만족할 때만 명시적인 `DEV_SIMULATION_IDS`를 허용한다.
해당 ID의 등록 조회·세션 시작/종료·Heartbeat·상태·계량 저장은 DB를 호출하지 않는다.
연결과 지표 및 최신 상태는 메모리에 저장되며 실제 OCPP 핸들러를 실행한다.
일반 ID는 기존 등록 확인과 DB 저장을 그대로 수행한다.
CSMS 시작 시 상태 코드 조회와 HTTP health 검사는 여전히 기존 DB를 사용하므로
CSMS 서비스 전체를 DB 없이 실행하는 기능은 아니다. DB 영속성 검증도 아니다.

상시 클라이언트는 `csms` Service에 연결하며 기존 연결은 원래 Pod에 머물 수 있다.
새 버전 검증은 별도 `CI-DEV-CANARY-01` ID로 `csms-canary` Service에 직접 연결한다.
기본 OCPP 검사는 활성화되어 있으며 Boot/Heartbeat 및 재연결을 확인한다.
검사 실패는 기존 AnalysisRun 실패 → Rollout 중단 → Jenkins 복구 경로를 사용한다.
상시 연결 수 3개만으로 새 버전 배포 성공을 판단하지 않는다.

Ops overlay에는 시뮬레이터와 테스트 모드 설정을 추가하지 않는다.
Ops OCPP 검사 Job은 여전히 별도 구성 대상이다.

## 최초 반영 순서

1. 앱 저장소의 새 CSMS 코드를 `kim_chanju`에 commit/push한다.
2. Jenkins가 해당 소스로 이미지를 생성하고 Dev GitOps digest를 갱신한 뒤 Dev 검증을 통과하는지 확인한다.
3. GitOps 최신 main을 가져와 이 변경을 반영한다. Jenkins가 갱신한 이미지 digest를 과거 값으로 덮어쓰지 않는다.
4. GitOps 변경을 push하면 기존 chargeops-dev 자동 동기화가 새 설정·시뮬레이터를 반영한다.
5. 설정 적용으로 생긴 Rollout을 확인하고 다음 Jenkins 빌드로 전체 후보 검증·복구 흐름을 확인한다.

이 작업은 로컬에서 검증했으며 실제 클러스터 적용·통신 성공은 아래 명령으로 확인해야 한다.
초기 전환에서 과거 코드 이미지로 복구하면 테스트 모드를 지원하지 않으므로 가상 ID 연결은
거부될 수 있다. 새 코드의 정상 기준 이미지를 먼저 확보한 뒤 실패·복구 시연을 진행한다.

## 서버에서 확인

```bash
kubectl -n dev rollout status deployment/dev-vcharger --timeout=180s
kubectl -n dev logs deployment/dev-vcharger --tail=30
kubectl -n dev get rollout csms
kubectl -n dev get pods -l app=csms
kubectl -n dev get analysisruns --sort-by=.metadata.creationTimestamp
```

로그에 각 ID의 `connected; BootNotification accepted`와
`Heartbeat and MeterValues accepted`가 반복되면 통신 중이다.
Deployment Available은 프로세스 실행 여부이며 WebSocket 통신 성공은 로그와 지표로 확인한다.

Grafana에서 환경 `dev`, 최근 15분 이상을 선택한다.
접속 직후 rate 계산에는 두 번 이상의 scrape와 충분한 시간 범위가 필요하다.

```promql
sum(max by (pod) (chargeops_csms_connections{namespace="dev"}))
sum by (action) (rate(chargeops_ocpp_handler_results_total{namespace="dev",result="success"}[5m]))
chargeops_canary_ocpp_result{namespace="dev"}
```

상시 연결은 보통 3개이고 Canary 검사 중에는 일시 증가할 수 있다.
오류율은 정상 통신 시 0%, p95는 성공한 처리 표본이 있을 때 표시된다.
접속·종료·거부 카운터는 새 앱에서 모두 0으로 초기화하므로 사건이 없는 항목은 0이다.
Canary 검사 기록은 실제 검사 실행과 probe ServiceMonitor 수집이 모두 필요하다.
Ops에서 이 검사 기록이 없는 것은 Ops 앱 이미지 배포만으로 해결되지 않는다.

임시 중지는 `kubectl -n dev scale deployment/dev-vcharger --replicas=0`으로 할 수 있지만
ArgoCD self-heal이 복원할 수 있다. 지속 중지는 Git의 simulator.yaml replicas를 0으로 반영한다.
