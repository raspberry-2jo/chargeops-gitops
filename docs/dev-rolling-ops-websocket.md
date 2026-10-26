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

## Ops: existing charger traffic

See [current Ops policy](ops-existing-traffic.md). The standard policy does not require a reserved charger ID or active smoke test.

## Grafana

See [current dashboard and Ops policy](ops-existing-traffic.md).
Import the updated chargeops-websocket-overview-ko.json (version 9).
Select dev or ops; the overview shows live connections, successful Heartbeat,
and handler errors. Ops details show the latest candidate traffic check.
Dev optional active-check diagnostics remain separate from Ops metrics.
