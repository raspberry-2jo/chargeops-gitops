# ChargeOps GitOps

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

GitHub 소유자명과 Docker Hub 계정명은 별개입니다. `chanjookim/*` 이미지 주소, digest,
Image Updater의 imageName 및 kustomize.name은 레지스트리를 이전할 때만 함께 변경합니다.
Kubernetes namespace, Application 이름, Service 이름, Secret 참조 이름과 서버 IP는 Git 주소 변경 때문에 바꾸지 않습니다.
실제 Jenkins 서버가 달라지면 `base/chatops-bot-v2/deployment.yaml`의 `JENKINS_URL`도 변경합니다.

## Jenkins CI/CD 연결

애플리케이션: `https://github.com/chanjook1m/chargeops.git`의 기존 Jenkinsfile이 테스트와 이미지 빌드/Push를 담당합니다.
현재 해당 파일은 Docker Hub `chanjookim`, credential ID `dockerhub-write`를 사용합니다.
애플리케이션 저장소 이전은 별도 작업이며 이 저장소에는 Dockerfile과 앱 소스가 없습니다.

GitOps 검증 Job `chargeops-gitops-ci`:

1. Linux agent label `chargeops-agent`에 Git 및 kubectl을 설치합니다.
2. Jenkins에 Pipeline, Git, Credentials Binding 플러그인을 준비합니다.
3. Pipeline script from SCM: Git URL `https://github.com/raspberry-2jo/chargeops-gitops.git`, Branch `*/main`, Script Path `Jenkinsfile`.
4. Build Now로 Kustomize 렌더링을 검증합니다. 결과 YAML은 빌드 artifact로 저장합니다.
5. 실제 클러스터 검증에는 Secret file 형식 kubeconfig credential을 등록하고 `KUBECONFIG_CREDENTIAL_ID`에 그 ID를 입력합니다.
   `chargeops` namespace, Argo CD 및 ImageUpdater CRD가 먼저 있어야 하며 해당 리소스 dry-run 권한이 필요합니다.

GitOps CI는 렌더링과 선택적 서버 dry-run을 수행합니다. 자동 Sync는 Jenkins 결과를 기다리지 않으므로,
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
kubectl -n chargeops get deployments,pods,services,cronjobs,pvc
```

배포 흐름: 앱 소스 변경 → Jenkins 테스트/이미지 Push → Image Updater가 이 저장소 main에 digest 커밋 → Argo CD Sync.
롤백은 digest 변경 커밋을 revert하고, Image Updater가 다시 덮어쓰지 않도록 해당 이미지 업데이트를 조정합니다.
이전 기록과 DB/Slack/PVC 등의 전제조건은 `docs/chargeops-v2-gitops.md`를 참고합니다.

공식 참고: [Jenkins Pipeline](https://www.jenkins.io/doc/book/pipeline/syntax/),
[Argo CD 자동 Sync](https://argo-cd.readthedocs.io/en/stable/user-guide/auto_sync/).
