# ChargeOps GitOps 저장소 변경 내역과 CI/CD 연결 절차

`chanjook1m/infra-test`의 `feature/chargeops-gitops` 브랜치 전체 파일과 커밋 이력을 이전한 저장소입니다.
이전 기준 커밋: `e5cacdb8b45f54e77dcb83d26609f970f889d779`.
배포 설정과 Image Updater의 Git write-back 대상은 `raspberry-2jo/chargeops-gitops`, 브랜치는 `main`입니다.

## 주소 변경 시 함께 확인할 값

| 위치 | 이전 후 값 / 조치 |
|---|---|
| `chargeops-chatops/argocd/application.yaml`: `spec.source.repoURL` | `https://github.com/raspberry-2jo/chargeops-gitops.git` |
| 같은 파일: `spec.source.targetRevision` | `main` |
| `chargeops-chatops/argocd/image-updater.yaml`: `spec.writeBackConfig.gitConfig.repository` | 같은 새 Git URL |
| 같은 파일: `spec.writeBackConfig.gitConfig.branch` | `main` |
| `argocd/chargeops-git-write` Secret | 이름 유지 가능. 실제 토큰이 새 저장소 Contents Read/Write 권한을 갖는지 확인 |
| Argo CD repository credentials / AppProject | private 저장소 읽기 권한 및 새 URL 허용 여부 확인 |
| Jenkins GitOps Job SCM / GitHub webhook | 새 URL, `*/main`, `Jenkinsfile`로 구성 |
| 개발자/관리 VM Git remote | 새 저장소로 clone하거나 origin URL 변경 |

이미지 레지스트리는 `registry.chargeops.lab:5000/chargeops/`입니다. Base 이미지, Overlay의
name/newName, Image Updater의 imageName 및 kustomize.name을 동일한 주소로 맞춥니다.
새 레지스트리에서 확인되지 않은 이전 digest를 재사용하지 않습니다.
Kubernetes namespace, Application 이름, Service 이름, Secret 참조 이름과 서버 IP는 Git 주소 변경 때문에 바꾸지 않습니다.
현재 앱 배포 namespace는 별도 변경 요청에 따라 `ops`이며, Argo CD와 ImageUpdater는 `argocd`에 유지합니다.
기존 클러스터 전환 시 Secret/PVC와 이전 리소스 처리는 [namespace 전환 절차](namespace-migration.md)를 따릅니다.
실제 Jenkins 서버가 달라지면 `base/chatops-bot/deployment.yaml`의 `JENKINS_URL`도 변경합니다.

## Jenkins CI/CD 연결

애플리케이션: `https://github.com/chanjook1m/chargeops.git`의 기존 Jenkinsfile이 테스트와 이미지 빌드/Push를 담당합니다.
이 저장소에는 Dockerfile과 앱 소스가 없으므로 애플리케이션 빌드 Job은 별도 변경이 필요합니다.
이미지 8종을 `registry.chargeops.lab:5000/chargeops/<이미지명>:dev`로 Push하도록 빌드/태그/Push,
현재 레지스트리는 인증 없이 사용하므로 Docker login과 레지스트리용 Jenkins credential은 필요 없습니다.
앱 빌드 Job의 agent label도 `mgmt-agent`로 맞춥니다. Git 접근 및 kubeconfig credential은 별개입니다.
기존 빌드 설정을 이 저장소에서 수정하거나 실제 Push를 검증한 것은 아닙니다.
전체 이미지 목록과 인증/HTTP/HTTPS 준비는 [레지스트리 전환 절차](registry-migration.md)를 따릅니다.

GitOps 검증 Job `chargeops-gitops-ci`:

1. Linux agent label `mgmt-agent`에 Git을 설치합니다. kubectl이 없으면 Jenkins가 공식 바이너리를
   `.ci-bin/`에 다운로드하고 SHA-256을 검증한 뒤 사용합니다. 이 경우 curl, sha256sum 및
   `https://dl.k8s.io`와 리다이렉트 대상 다운로드 서버로의 HTTPS 연결이 필요하며 sudo는 필요하지 않습니다.
   Linux amd64/arm64를 지원합니다. 기존 kubectl이 있으면 해당 설치를 사용합니다.
2. Jenkins에 Pipeline, Git, Credentials Binding 플러그인을 준비합니다.
3. Pipeline script from SCM: Git URL `https://github.com/raspberry-2jo/chargeops-gitops.git`, Branch `*/main`, Script Path `Jenkinsfile`.
4. Build Now로 Kustomize 렌더링과 `scripts/validate-registry.sh`의 이미지 주소 검사를 수행합니다.
   결과 YAML은 빌드 artifact로 저장합니다.
5. 실제 클러스터 검증에는 Secret file 형식 kubeconfig credential을 등록하고 `KUBECONFIG_CREDENTIAL_ID`에 그 ID를 입력합니다.
   `ops` namespace, Argo CD 및 ImageUpdater CRD가 먼저 있어야 하며 해당 리소스 dry-run 권한이 필요합니다.
   다운로드 버전은 `KUBECTL_VERSION`으로 지정하며 기본값은 `v1.36.1`입니다.
   서버 dry-run을 사용할 때는 클러스터 minor 버전에 맞추세요. 기존 설치가 있으면 이 파라미터로 교체하지 않습니다.

### `kubectl: command not found` 실패 후 재실행

최신 Jenkinsfile에는 `Prepare kubectl` 단계가 있습니다. Jenkins에서 Build Now 또는 Build with Parameters로
다시 실행하고, 로그에서 kubectl 준비 → Render manifests 성공을 확인합니다.
`KUBECONFIG_CREDENTIAL_ID`가 비어 있으면 서버 dry-run은 생략하며 렌더링에는 kubeconfig가 필요하지 않습니다.
다운로드가 실패하면 agent의 curl/sha256sum 설치와 외부 HTTPS 연결을 확인하거나,
agent 실행 사용자 PATH에 kubectl을 미리 설치합니다. Jenkins controller에만 설치하면 agent에서는 사용할 수 없습니다.

공식 설치 및 체크섬 검증: [kubectl Linux 설치](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/).

GitOps CI는 렌더링, 이미지 주소 검사와 선택적 서버 dry-run을 수행합니다. 자동 Sync는 Jenkins 결과를 기다리지 않으므로,
검증을 배포 전 필수 조건으로 만들려면 PR 브랜치에서 CI를 실행하고 main branch protection에 성공 체크를 설정합니다.
현재 SCM polling은 5분 간격입니다. GitHub webhook을 추가한다면 Job trigger도 해당 방식으로 설정합니다.

## 기존 클러스터 전환 (관리 VM)

1. 기존 Application/ImageUpdater 설정을 저장해 롤백 기준을 확보합니다.
2. 새 저장소 읽기 권한과 `chargeops-git-write`의 새 저장소 쓰기 권한을 준비합니다.
   이전 Image Updater가 기존 저장소에 계속 쓰지 않도록 전환 중 해당 Application의 업데이트를 조정합니다.
3. 새 저장소를 clone하고 클러스터에서 아래 dry-run을 통과시킵니다.
4. Application 및 ImageUpdater를 적용하고 새 저장소 digest 커밋과 Argo CD Synced/Healthy를 확인합니다.

```bash
git clone https://github.com/raspberry-2jo/chargeops-gitops.git
cd chargeops-gitops
kubectl kustomize chargeops-chatops/overlays/dev > /tmp/chargeops-dev.yaml
kubectl apply --dry-run=server -f /tmp/chargeops-dev.yaml
kubectl apply --dry-run=server -f chargeops-chatops/argocd/application.yaml
kubectl apply --dry-run=server -f chargeops-chatops/argocd/image-updater.yaml
# 위 검증이 모두 성공한 다음 실행. Application 적용 시 자동 Sync가 시작될 수 있습니다.
kubectl apply -f chargeops-chatops/argocd/application.yaml
kubectl apply -f chargeops-chatops/argocd/image-updater.yaml
kubectl -n argocd get application chargeops-chatops-dev
kubectl -n argocd describe imageupdater chargeops-image-updater
kubectl -n ops get deployments,pods,services,cronjobs,pvc
```

배포 흐름: 앱 소스 변경 → Jenkins 테스트/이미지 Push → Image Updater가 이 저장소 main에 digest 커밋 → Argo CD Sync.
롤백은 digest 변경 커밋을 revert하고, Image Updater가 다시 덮어쓰지 않도록 해당 이미지 업데이트를 조정합니다.
이전 기록과 DB/Slack/PVC 등의 전제조건은 [기존 GitOps 적용 기록](chargeops-gitops.md)을 참고합니다.

공식 참고: [Jenkins Pipeline](https://www.jenkins.io/doc/book/pipeline/syntax/),
[Argo CD 자동 Sync](https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/).
