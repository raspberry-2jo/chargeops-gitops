# CSMS WebSocket/OCPP 배포 검증

## 적용 범위

기존 Jenkins → GitOps → ArgoCD → 분석 → 자동 복구 흐름을 유지한다.
Dev AnalysisTemplate에 `ocpp-websocket` Job을 추가했다. HTTP/Pod 검사와 병렬로 실행하며,
활성화한 OCPP 검사가 실패하면 AnalysisRun이 실패하고 기존 Jenkins 복구 경로로 이어진다.
실서버에서 이 연속 경로를 확인한 것은 아니다. 아직 HTTP 실패 시연과 Git 복구를 각각 확인한 상태다.

Dev·Ops 모두 5단계 자동 gate를 연결했다. 세부 기준과 최초 계측 전환은 [5단계 기준](canary-five-level-gates.md)을 따른다.
Dev에 새 검사 Job 하나가 추가되므로 최대 250m CPU / 128Mi 메모리 제한과 Pod 자리 하나가 더 필요하다.
bootstrap의 여유 quota 검사도 그만큼 조정했다.

## 새 앱 지표

CSMS 이미지에 다음 계측을 추가했다. 기존 메시지 수신 지표와 구분한다.

* `chargeops_ocpp_handler_results_total{action,result}`: success/error/timeout/cancelled
* `chargeops_ocpp_request_seconds{action,result}`: 핸들러 실행시간. 소켓 전송 완료는 포함하지 않는다.
* `chargeops_ocpp_last_success_timestamp_seconds{action}`: Pod별 최근 핸들러 성공 시각

대상 action은 BootNotification, Heartbeat, StatusNotification, MeterValues다.
기존 응답/예외와 핸들러 signature를 보존한다. 충전기 ID/메시지 본문을 지표 라벨에 넣지 않는다.
새 앱 이미지를 배포하기 전에는 새 지표가 나오지 않는다. Ops 앱 이미지는 승인 배포 후 적용된다.

## Dev OCPP 검사 활성화

현재 기본값은 `OCPP_PROBE_ENABLED=true`, ID는 `CI-DEV-CANARY-01`이다.
새 Dev CSMS의 명시적 메모리 처리 allowlist를 사용하므로 이 ID는 DB 등록이 필요 없다.
새 앱 코드를 먼저 배포해야 한다. 최초 반영 순서와 자동 가상 충전기는
[Dev 자동 연결 안내](dev-automatic-virtual-charger.md)를 따른다.
다른 ID로 바꾸면 Dev allowlist에 추가하거나 기존 방식으로 DB에 등록해야 한다.
스크립트는 충전기 등록이나 삭제를 수행하지 않는다. 다른 프로세스가 같은 ID로 접속하면 안 된다.

```bash
git pull --ff-only origin main
python3 scripts/configure-dev-ocpp.py --charger-id CI-DEV-CANARY-01
git diff -- chargeops-chatops/overlays/development-canary/ocpp-settings.yaml
git add chargeops-chatops/overlays/development-canary/ocpp-settings.yaml
git commit -m 'test(dev): enable reserved OCPP canary fixture'
git push origin main
```

위 ID는 기본 Dev 메모리 처리 ID다. 설정 반영 뒤 Jenkins kim_chanju 빌드를 실행한다.
Dev 각 분석 단계에서 csms-canary.dev.svc.cluster.local:9000의 새 버전에 직접 연결한다.
검사 컨테이너 이미지는 Kustomize replacement로 Dev CSMS 후보와 정확히 같은 digest를 사용한다.

검사: BootNotification Accepted → Heartbeat 7회(60초 연결 유지) → 연결 종료 → 다시 연결 → Boot/Heartbeat.
각 send/response/handshake 제한은 5초다. CallError, 응답 ID 불일치, 접속 거부, 연결 단절, 시간 초과 시 실패한다.
Job deadline은 180초이며 마지막 판정 수집을 위해 20초 대기한다.

기본 Dev 메모리 처리 ID의 session/Heartbeat는 DB에 저장하지 않는다.
DB 등록 방식의 다른 ID는 기존 DB 처리를 수행한다. 충전 시작/종료, Reset, StatusNotification,
MeterValues를 검사 Job에서 실행하지 않는다. DB에 영속됐는지 직접 조회하는 검사는 아니다.
일반 사용자 전체 연결 회복률이나 모든 충전기 메시지 정상 처리를 증명하지는 않는다.

```bash
kubectl -n dev get analysisruns --sort-by=.metadata.creationTimestamp
kubectl -n dev describe analysisrun 실제분석이름
kubectl -n dev get pods -l chargeops.io/role=dev-canary-probe
kubectl -n dev logs 실제OCPP검사Pod이름
```

정상 로그: `PASS: candidate OCPP boot, Heartbeat, 60-second connection and reconnect.`
비활성 로그: `SKIP: Dev fixture not enabled; WebSocket availability is NOT verified.`

비활성화도 Git으로 반영한다.

```bash
python3 scripts/configure-dev-ocpp.py --disable
```

위 명령 뒤 같은 settings 파일을 commit/push한다. 실행 중인 검사에는 설정을 바꾸지 않는다.

## 대시보드

Git 밖의 `chargeops-websocket-overview-ko.json`을 Import한다. UID는 기존 간소화 대시보드와
같으므로 덮어쓰기로 화면을 교체할 수 있다. 환경 dev/ops를 선택한다.

실제 연결·비정상 종료·확인 재접속, 주요 OCPP 처리량, 오류율을 Dev·Ops 자동 실패 기준에 연결했다.
HTTP p95는 참고만 하며 Health/DB 검사 실패는 중단한다.
Dev 전용 능동 OCPP fixture와 환경 전체 지표 gate는 별개다.
Ops 능동 fixture는 운영용 예약 ID 없이 실행하지 않으며 실제 OCPP 트래픽과 계측으로 gate를 판정한다.
새 계측 이미지·Prometheus 수집·통신 표본이 필요하다. 데이터 없음은 통과가 아니다.
자세한 기준과 제한은 [5단계 기준](canary-five-level-gates.md)을 확인한다.
