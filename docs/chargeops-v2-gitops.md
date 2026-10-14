# ChargeOps v2 GitOps 적용 기록과 실행 절차

> 이 문서는 원본 저장소의 적용 기록입니다. 저장소 이전 후 URL, 브랜치와 실행 경로는 [저장소 변경 내역과 CI/CD 연결 절차](repository-migration-and-cicd.md)를 따릅니다.
> 현재 GitOps 저장소는 `raspberry-2jo/chargeops-gitops`, 브랜치는 `main`입니다.

적용일: 2026-10-14. 로컬 `feature/chargeops-gitops` 기준 커밋: `a25679f`.
작업 경로: `C:\Users\SBA_USER\codex-test\git-test\infra-test`.

## 적용과 원문 보정

- 기존 세 Base, 이미지 digest, Application 이름과 경로를 유지했습니다.
- AI gateway, v2 bot, CSMS, notifier, vcharger Base 및 collector ConfigMap/PVC/CronJob 3개를 추가했습니다.
- Overlay와 ImageUpdater에 총 아홉 이미지를 등록했습니다.
- 기존 Application의 `spec.syncOptions`는 잘못된 위치였습니다. `spec.syncPolicy.syncOptions`로 옮겼습니다.
- AI gateway의 `DB_DSN` Secret 참조를 추가했습니다. 프로세스 확인부터 할 수 있게 optional로 두었으며, 업무 API에는 실제 DB Secret이 필요합니다. Ollama 주소/모델도 명시했습니다.
- v2 bot와 notifier는 replica 1 및 Recreate를 사용합니다. RollingUpdate 중 기존/신규 프로세스가 겹쳐 중복 이벤트·알림을 만들 수 있기 때문입니다. 교체 중 잠시 중단됩니다.
- Collector는 세 CronJob 모두 suspend 상태입니다. 실행 Pod가 없는 상태에서도 digest를 추적하도록 해당 ImageUpdater alias에 `forceUpdate: true`를 추가했습니다.
- Collector 컨테이너도 UID/GID 10001로 통일하고 PVC에 fsGroup 10001을 적용했습니다.

Image Updater의 digest/forceUpdate 설정은 [공식 이미지 설정 문서](https://argocd-image-updater.readthedocs.io/en/stable/configuration/images/)를 참고합니다.
CronJob timeZone은 Kubernetes 1.27 이상을 기준으로 합니다. [공식 CronJob 문서](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/).

## 배포 전 조건

서비스 저장소에서 Jenkins 첫 빌드와 Docker Hub `dev` 이미지 생성을 먼저 완료합니다.
전체 Overlay는 모든 앱을 동시에 등록하므로 관련 조건이 빠지면 Application 전체가 Healthy가 되지 않을 수 있습니다.

- DB: 실제 PgBouncer 주소, app_writer 계정, DB schema, `code_status.ocpp_status`, CH-01 seed.
- `chargeops-app-secrets`: DB_DSN, API_TOKEN, CSMS_API_TOKEN(동일 값), SLACK_BOT_TOKEN,
  SLACK_APP_TOKEN, SLACK_ALERT_CHANNEL, SLACK_AUDIT_CHANNEL, JENKINS_USER, JENKINS_TOKEN,
  EV_SERVICE_KEY, PGPASSWORD. 관리 VM에서 실제 값을 담은 권한 600 env 파일로 생성합니다.
- AI gateway Ollama 기본값: `http://10.10.0.1:11434`. bot Jenkins와 notifier SMTP 기본값:
  `10.10.40.10`. Collector DB 기본값: `10.10.30.11,10.10.30.12:6432`.
  실제 클러스터 주소가 다르면 관련 Deployment/ConfigMap을 먼저 수정합니다.
- 기본 StorageClass 또는 collector PVC의 명시적 storageClassName.
- 비공개 이미지는 Deployment 및 Collector CronJob **모두** Pod spec에 imagePullSecrets를 추가합니다.
  Image Updater에도 registry 읽기 자격 증명이 필요합니다.
- CSMS REQUIRE_CN=false, vcharger ws://는 내부 실습 설정입니다. 외부 충전기에는 mTLS 설정이 필요합니다.

```bash
# kube-control1; 실제 값은 저장소 밖 ~/.chargeops-app-secrets.env에 입력
kubectl create namespace chargeops --dry-run=client -o yaml | kubectl apply -f -
chmod 600 ~/.chargeops-app-secrets.env
kubectl -n chargeops create secret generic chargeops-app-secrets \
  --from-env-file="$HOME/.chargeops-app-secrets.env" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n chargeops get secret chargeops-app-secrets
```

## 렌더링, 서버 검증, Push

PowerShell의 native 명령 실패는 기본적으로 다음 명령을 멈추지 않으므로 `$LASTEXITCODE`를 확인합니다.
실제 클러스터가 연결되어 있으면 서버 Dry Run을 **Push 전에** 수행합니다. 기존 Application이 자동 Sync이므로
원문처럼 Push 후 Dry Run하면 검증 전에 배포가 시작될 수 있습니다.

```powershell
Set-Location 'C:\Users\SBA_USER\codex-test\git-test\infra-test'
git diff --check
if ($LASTEXITCODE -ne 0) { throw 'Git diff 검사 실패' }
$rendered = kubectl kustomize .\chargeops-chatops\overlays\dev
if ($LASTEXITCODE -ne 0) { throw 'Kustomize 렌더링 실패' }
# 클러스터가 연결된 터미널에서만 실행합니다.
$rendered | kubectl apply --dry-run=server -f -
if ($LASTEXITCODE -ne 0) { throw '클러스터 서버 검증 실패' }
git add chargeops-chatops docs/chargeops-v2-gitops.md
git commit -m "feat(gitops): add ChargeOps v2 app manifests"
git push origin feature/chargeops-gitops
```

Windows에 kubeconfig가 없으면 렌더링 결과를 관리 VM으로 전달하거나 변경을 해당 VM의 작업 트리에 복사해
`kubectl apply --dry-run=server -f <렌더링파일>`을 먼저 수행합니다.

## Argo CD 및 Image Updater 적용

관리 VM `~/infra-test`에서 해당 브랜치를 최신화합니다. ImageUpdater CRD/Controller 및
`argocd/chargeops-git-write` Secret이 먼저 있어야 합니다. GitHub 토큰은 infra-test Contents Read/Write 권한이 필요합니다.
Secret 값은 출력하지 않습니다. branch protection이 직접 Push를 막으면 Git write-back을 위한 별도 방식이 필요합니다.

```bash
cd ~/infra-test
git fetch origin
git switch feature/chargeops-gitops
git pull --ff-only origin feature/chargeops-gitops
kubectl get crd imageupdaters.argocd-image-updater.argoproj.io
kubectl -n argocd get deployment
kubectl -n argocd get secret chargeops-git-write
kubectl apply -f chargeops-chatops/argocd/application.yaml
kubectl apply -f chargeops-chatops/argocd/image-updater.yaml
kubectl -n argocd describe imageupdater chargeops-image-updater
kubectl -n argocd get application chargeops-chatops-dev
kubectl -n chargeops get deployment,pod,service,cronjob,pvc
kubectl -n chargeops rollout status deployment/ai-gateway --timeout=180s
kubectl -n chargeops port-forward service/ai-gateway 18000:8000
```

다른 터미널에서 `curl -fsS http://127.0.0.1:18000/cicd-healthz`로 최초 marker v1을 확인합니다.
서비스 저장소 문서의 v1 → v2 변경을 한 번 Push한 뒤 새 digest 커밋과 rollout을 확인합니다.
Pod 교체 후 port-forward를 다시 열고 marker v2를 확인합니다.

```bash
git fetch origin
git log --oneline -5 origin/feature/chargeops-gitops
kubectl -n argocd get application chargeops-chatops-dev
kubectl -n chargeops get deployment ai-gateway -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
kubectl -n chargeops rollout status deployment/ai-gateway --timeout=180s
```

## Collector 활성화

DB/API/PVC 준비를 확인한 후 `base/collector-v2/cronjobs.yaml`의 status/info 두 리소스만
`suspend: false`로 변경하고 GitOps에 커밋·Push합니다. check는 true로 둡니다.
수동 check도 공공 API를 호출하므로 준비 후 실행합니다.

```bash
kubectl -n chargeops create job --from=cronjob/collector-v2-check collector-v2-check-manual-$(date +%s)
```

status와 info는 서로 다른 CronJob이므로 concurrencyPolicy로 둘 사이 실행을 막지는 못합니다.
하나의 ReadWriteOnce PVC를 공유하므로 다른 노드에 배치되면 볼륨 연결 대기가 생길 수 있습니다.
스토리지에 따라 별도 PVC/RWX/동일 노드 배치를 검토합니다.

## 로컬 검토 상태

Overlay 렌더링은 성공했습니다: Deployment 8개, Service 6개, CronJob 3개, ConfigMap 1개, PVC 1개.
Collector 세 CronJob은 중지 상태이고, 기존 세 이미지 digest는 보존했습니다.
실제 서버 Dry Run, Secret 생성, Argo CD 적용, Jenkins 빌드, 업무 API 검증은 수행하지 않았습니다.
로컬 수정 파일 상태이며 커밋/Push는 아직 수행하지 않았습니다.
