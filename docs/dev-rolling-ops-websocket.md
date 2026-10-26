# Dev RollingUpdate / Ops WebSocket Canary 전환

코드와 로컬 테스트 기준의 안내다. 실서버 전환·Ops 통과·Grafana Import 결과는 별도 확인한다.
Jenkins 단일 Job, 앱 PR kim_chanju → dev 승인·Merge, 동일 digest Ops 승격을 유지한다.

## Dev 전환: 현재 실행을 마친 뒤 한 번만 수행

배포·복구 중에는 전환하지 않는다. chargeops-ci와 별도 chargeops-ops-ci가 실행 중이지 않은지 확인한다.
GitOps main의 새 경로 development-rolling은 기존 development를 기반으로 다음을 포함한다.

- Deployment 2개, maxSurge=1, maxUnavailable=0, 준비 확인·지연 제한
- CI-DEV-SIM-01~03 자동 가상 충전기와 CI-DEV-CANARY-01 메모리 검증 ID
- 기존 CSMS 지표 수집 및 배포 검사 Job 9110 수집
- Rollout·AnalysisTemplate은 포함하지 않음

mgmt의 GitOps main checkout에서 실행한다. Python 환경에 PyYAML이 필요하다.

```bash
git pull --ff-only origin main
python3 scripts/migrate-dev-rolling.py plan
```

새 경로 이미지와 실제 안정 이미지가 다르면 아래 명령은 **새 경로의 digest만** 현재 이미지로 고정한다.
Dev가 Healthy일 때만 실행하며 후보 배포 도중에는 실행하지 않는다.

```bash
python3 scripts/migrate-dev-rolling.py pin
git diff -- chargeops-chatops/overlays/development-rolling/kustomization.yaml
git add chargeops-chatops/overlays/development-rolling/kustomization.yaml
git commit -m 'build(dev): preserve live digest for RollingUpdate handover'
git push origin main
```

이미 일치하면 위 pin/commit은 생략한다. 다음 명령이 실제 Dev Application과 리소스를 변경한다.

```bash
python3 scripts/migrate-dev-rolling.py apply
kubectl -n dev get deployment csms dev-vcharger
kubectl -n dev get rollout csms
kubectl -n dev logs deployment/dev-vcharger --tail=20
```

전환 도구는 기존 Argo 자동 Sync를 일시 중지하고 prune 없이 새 경로를 Sync한다.
전환 중 기존 2개와 신규 2개, workloadRef가 템플릿 변경을 감지하여 만들 수 있는 여유 Canary와 검사 Job까지 수용하도록
handover 경로에서 quota를 6 CPU/6Gi로 일시 확보한다. 실제 스케줄링 여유가 없으면 준비 확인에서 멈추며 기존 Rollout을 먼저 삭제하지 않는다.
Deployment의 같은 digest가 정확히 Ready 2/2임을 확인한 뒤 **기존 Dev Rollout과 그 소유 Pod만** 정리한다.
그동안 기존 Deployment는 삭제하지 않는다. 최종 development-rolling 경로로 옮겨 quota를 원래 4 CPU/4Gi로 돌리고 기존 Argo syncPolicy를 복원한다.
최종 Sync 전 Argo 관리 리소스 목록을 검사하고, 알려진 이전 Dev Canary 설정·Service·RBAC·수집 리소스만 정리한다.
예상하지 않은 PVC나 다른 환경 리소스가 있으면 prune을 거부한다. 이전 설정이 남아 영구 OutOfSync가 되는 것을 방지한다.
새 Deployment가 준비되지 않으면 Rollout을 지우지 않는다. 중간 실패 시 .dev-rolling-migration.json을 보존하고
상태를 확인한 뒤 같은 명령으로 재개한다. 전환 도중 자동 Sync가 꺼진 상태일 수 있다.
다른 도구가 Application 선언을 관리한다면 전환 후 application-dev-rolling.yaml 경로를 그 원본에도 반영한다.
이 파일을 전환 전에 단순 apply하여 기존 Rollout을 먼저 정리하지 않는다.

이후 기존 chargeops-ci를 DEV_DELIVERY_MODE=auto, ENABLE_ENVIRONMENT_CD=true, ENABLE_CANARY=true로 실행한다.
최초에는 BUILD_ALL_IMAGES=true를 사용한다. 새 이미지에 csms.release_probe 모듈이 포함되어야 한다.
Dev Deployment의 정확한 digest·소유 ReplicaSet·모든 Pod 준비를 확인한 다음 배포 검증 Job이 실행된다.
Health/DB 읽기와 BootNotification, StatusNotification, 약 60초 Heartbeat, 재접속을 검증한다.
Dev 전용 ID의 OCPP 처리는 메모리이며 충전기 DB 상태를 쓰지 않는다.
실패하면 이전 Dev Git digest를 복원하고 Deployment가 그 이미지로 돌아왔는지 확인한다.

## Ops 시험 ID 없음: 현재는 승격 차단이 의도된 동작

Ops 검사 설정은 빈 ID와 OCPP_FIXTURE_RESERVED=false로 제공된다.
Jenkins는 운영 이미지 갱신 전에 설정 미완료를 차단한다.
Dev 검증 후 소스 PR 생성 전에도 확인하여, 알려진 미설정 상태에서 먼저 Merge하지 않도록 막는다.
독립적으로 검사 Job을 실행하더라도 미설정은 UNVERIFIED 및 exit 1이며 성공으로 처리하지 않는다.
기존 CH-01 등 운영 ID를 시험용으로 재사용하거나 Ops에서 Dev 메모리 우회를 켜지 않는다.
전환 전 새 Jenkins 자동 빌드는 migration required로 멈춘다. Dev/Ops 이미지는 변경하지 않으며, 전환 후 같은 Job을 다시 실행한다.

DB 담당자가 아래 조건의 전용 fixture를 먼저 등록·예약해야 한다.

- CI-OPS-CANARY-001 같은 전용 접두사, charger.source=model 또는 virtual, is_deleted=false
- 별도 시험용 station/charger 등록 정책 준수. 이 도구는 임의 station 생성이나 DB insert를 하지 않음
- 다른 프로그램이 이 ID로 연결하지 않음. 실제 충전·Reset·운영 제어에 사용하지 않음

등록 후만 실행한다.

```bash
python3 scripts/configure-ops-ocpp.py --charger-id CI-OPS-CANARY-001 --confirm-exclusive-reservation
git diff -- chargeops-chatops/overlays/canary/ocpp-settings.yaml
git add chargeops-chatops/overlays/canary/ocpp-settings.yaml
git commit -m 'test(ops): configure exclusively reserved OCPP canary fixture'
git push origin main
```

정책이 달라졌으므로 **새 Jenkins 빌드에서 Dev부터 검증**한다. 승인 대기 중인 이전 빌드에 설정을 끼워 넣지 않는다.

## Ops 후보 실통신 검사

25/50/100% 각 단계에 websocket-ocpp-smoke를 추가했다.
검사 이미지는 후보 CSMS와 같은 digest이며 csms-canary.ops:9000으로 직접 연결한다.
Pod hash 인자는 Rollout의 Latest 값으로 전달한다.

1. /healthz 응답과 직접 DB SELECT 1
2. 전용 ID 등록·source 확인, 기존 세션이 있으면 접속하지 않고 차단
3. WebSocket/OCPP 1.6 연결과 BootNotification Accepted
4. DB 세션의 Pod가 csms-<후보 hash>-…인지 확인
5. StatusNotification Available, Heartbeat 반복, 요청 ID·CALLRESULT 검증
6. 약 60초 유지 후 Heartbeat DB 저장 및 동일 세션 유지 확인
7. 정상 종료 후 세션 삭제 확인 → 재접속 → 새 session_id 확인 → 종료 정리 확인

fixture의 상태·Heartbeat·접속/종료 기록은 실제 DB에 반영된다. 시험 코드는 DB에 직접 쓰지 않지만
CSMS는 메시지를 처리하며 전용 fixture 행을 갱신한다. 종료 후 Offline 기록도 정상이다.
이 검사는 전용 시험 세션의 정합성이며 모든 운영 세션·과금·중복 접속 거부 정책을 증명하지 않는다.
기존 fn_session_open은 최신 세션으로 덮어쓰는 정책이므로 이를 중복 접속 차단으로 설명하지 않는다.

접속·Boot·응답 검증·시간 초과·세션 정합성이 한 번이라도 실패하면 검사 Job이 실패한다.
기존 five-level-service-gate의 환경 전체 연결 감소·처리량·오류율 기준도 함께 적용한다.
HTTP p95는 참고다. Pod/Health/DB 실패는 중단 조건이다.
정상 close·drain과 비정상 종료를 구분하며, 재접속 계측은 기존과 같이 Pod 내부 범위다.
시험 클라이언트 연결 시도·응답 왕복시간은 실제 충전기 전체 성공률과 구분해 표시한다.

Ops Service 9000, probe ingress/egress, DB egress, 설정 CM, AnalysisTemplate과 Rollout 인자를
Jenkins 선택 Sync에 포함했다. 관련 정책을 먼저 Sync하고 Deployment 이미지 변경을 뒤에 반영한다.
다른 앱·PVC·Secret은 선택 Sync하지 않는다.

## Grafana

Git 밖의 chargeops-websocket-overview-ko.json을 Import하여 같은 UID의 기존 화면을 교체한다.

- 최상단에 실제 선택 환경을 표시. dev/ops 선택 유지
- 준비된 Pod, 최근 앱·DB·실통신 검사 결과를 먼저 표시
- 최근 시작 Job을 선택하므로 이전 성공이 새 검증을 대신하지 않음
- Dev 세션 카드는 DB 검사 대상 아님. Ops는 전용 시험 세션 정합성 결과
- 연결·Heartbeat·처리 오류율·핸들러 지연·온도·전류·전압·전력량 유지
- Ops 중단 기준과 한계선은 접힌 상세 영역. Dev는 이를 열어 확인할 필요 없음
- 검사 미실행/미수집, 관측 중, 통과, 실패를 구분. 상세 표에는 과거 실행도 보존

새 배포 검사는 chargeops_release_check, chargeops_release_probe_started_timestamp_seconds,
chargeops_canary_ocpp_result, chargeops_canary_ocpp_roundtrip_seconds,
chargeops_ws_probe_connection_attempts_total을 9110에서 노출한다.
ServiceMonitor는 5초마다 수집하고 Job은 마지막 결과를 위해 20초 유지한다.
