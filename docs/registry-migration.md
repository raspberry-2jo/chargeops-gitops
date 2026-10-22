# ChargeOps Docker Registry 전환

모든 앱 이미지의 접두사는 `registry.chargeops.lab:5000/chargeops/`입니다. 현재 이름과 전환 순서는 [앱 이름 통합](app-naming-migration.md)을 우선 참고합니다.
Jenkins GitOps 검증 Job은 Linux agent label `mgmt-agent`를 사용합니다.
앱 배포 namespace는 `ops`이며 Argo CD와 ImageUpdater 리소스는 `argocd`에 둡니다.

## 변경 내용

- 현재 Overlay는 Deployment 5개 및 Collector CronJob 3개의 이미지 주소를 관리합니다.
  이미지 태그는 Image Updater가 추적하는 `dev`로 통일했습니다.
- Overlay의 이미지 6종 `name`/`newName`과 ImageUpdater의 `imageName`/Kustomize 대상 이름을
  Base와 동일하게 변경했습니다. 기존 Git write-back과 digest 전략을 유지합니다.
- 이전 레지스트리의 digest는 새 저장소에 존재하는지 확인할 수 없어 제거했습니다.
  최초에는 `:dev`를 사용하고 Image Updater가 새 레지스트리의 digest를 Git에 기록합니다.
- Pod의 `imagePullPolicy`를 `Always`로 변경해 digest 고정 전 재시작 시 `dev` 캐시 재사용을 방지합니다.
  이미 digest로 고정된 경우 같은 콘텐츠를 사용합니다. 같은 태그의 Push만으로 Pod가 재시작되지는 않으므로
  이후 자동 배포에는 Image Updater의 digest write-back이 정상 동작해야 합니다.
- 현재 레지스트리는 아이디·비밀번호 인증 없이 사용합니다. Deployment 7개와 Collector CronJob 3개의
  `imagePullSecrets`, ImageUpdater 이미지 8종의 `pullSecret` 참조를 모두 제거했습니다.
  레지스트리 로그인 Secret을 생성할 필요가 없습니다.
- GitOps CI가 렌더링한 모든 컨테이너 이미지의 레지스트리/태그를 검사하도록 추가했습니다.
  `.gitattributes`로 shell script의 LF 개행을 고정해 Windows에서 작업 후에도 Linux agent에서 실행되도록 했습니다.
  서비스, 포트, Secret의 앱 데이터, replicas 및 CronJob suspend 설정은 유지했습니다.

## 이미지 선행 준비

다음 이미지 모두 새 레지스트리에 **`dev` 태그**가 있어야 합니다. 이름에 `chargeops-` 접두사를 유지합니다.

| 이미지명 | 사용 리소스 |
|---|---|
| chargeops-chatops-bot | Deployment/chatops-bot |
| chargeops-ai-gateway | Deployment/ai-gateway |
| chargeops-csms | Deployment/csms |
| chargeops-notifier | Deployment/notifier |
| chargeops-vcharger | Deployment/vcharger |
| chargeops-collector | Collector CronJob 3개 |

예: `registry.chargeops.lab:5000/chargeops/chargeops-ai-gateway:dev`.
중지된 서비스도 재개할 때 필요하므로 함께 준비합니다.
앱 저장소의 Jenkinsfile/Dockerfile은 이 저장소에 없으며 빌드/태그/Push 대상과 agent label은 그쪽에서도 변경해야 합니다.
Docker login 없이 Push하며 대상에 `/chargeops/<이미지명>`을 포함합니다.
이미지를 복사하는 경우 모든 실행 노드의 CPU 아키텍처에 맞는 manifest/platform을 보존합니다.

## DNS, 프로토콜 및 인증

아이디·비밀번호 인증은 사용하지 않습니다. HTTP/HTTPS는 인증 여부와 별개이며 포트 5000만으로 결정할 수 없습니다.
현재 작업 PC는 레지스트리 DNS를 해석하지 못하므로 프로토콜과 실제 이미지 존재를 확인하지 못했습니다.
`mgmt-agent`, Image Updater Pod 및 **모든 Kubernetes 실행 노드**에서 DNS와 TCP 5000 연결을 준비합니다.
노드의 hosts 파일만 수정하면 Pod DNS 문제는 남을 수 있으므로 클러스터 DNS도 확인합니다.

### 인증 없는 운영

현재 매니페스트는 레지스트리의 익명 접근을 사용하며 `harbor-pull`을 참조하지 않습니다.
Docker login, 레지스트리용 Jenkins credential, 레지스트리용 Kubernetes Secret 생성은 필요 없습니다.
이미 클러스터에 있는 Secret 자체는 삭제하지 않았습니다.

클러스터의 Image Updater 전역 `registries.conf`에 이 레지스트리용 `credentials`가 별도로 설정되어 있다면
해당 항목도 제거합니다. Pod의 ServiceAccount에 이전 `imagePullSecrets`가 연결되어 있는지도 확인합니다.
이 전역 설정과 ServiceAccount는 이 저장소에서 관리하지 않으며 실제 클러스터에서는 확인하지 못했습니다.
Jenkins 앱 빌드 Job에 이전 Docker login 단계가 있다면 제거하고 새 주소로 바로 Push하도록 변경합니다.
Git write-back용 `chargeops-git-write`, 앱의 DB/Slack Secret, Jenkins의 Git/kubeconfig credential은 별도 용도이므로 유지합니다.

### HTTPS

기본 registry 추론은 HTTPS입니다. 신뢰 가능한 인증서라면 별도 registry endpoint 설정이 필요하지 않습니다.
사설 CA를 사용하면 Jenkins 빌드용 Docker daemon, Kubernetes 각 노드의 컨테이너 런타임,
Image Updater에 해당 CA를 신뢰하도록 설정합니다. 이 세 연결 경로를 각각 확인합니다.

### HTTP

HTTP 서버에는 이미지 주소 변경 외에 다음 설정이 필요합니다.

1. Jenkins 빌드용 Docker daemon의 기존 `daemon.json`에
   `"insecure-registries": ["registry.chargeops.lab:5000"]`를 병합하고 해당 daemon에 반영합니다.
   원격 daemon/DinD를 사용하면 agent가 아닌 실제 daemon 쪽 설정입니다.
2. containerd를 쓰는 실행 노드는 registry hosts 설정 경로가 활성화되어 있는지 확인한 뒤
   해당 경로의 `registry.chargeops.lab:5000/hosts.toml`에 HTTP endpoint를 설정합니다.
   예시는 `/etc/containerd/certs.d/registry.chargeops.lab:5000/hosts.toml`이며,
   containerd 버전에 따라 `config_path`를 설정하는 plugin 경로가 다릅니다.

   ```toml
   server = "http://registry.chargeops.lab:5000"
   [host."http://registry.chargeops.lab:5000"]
     capabilities = ["pull", "resolve"]
   ```

3. Image Updater가 실제로 마운트하는 registry ConfigMap의 `data.registries.conf`에
   아래 entry를 **기존 registries 목록과 병합**하고 Controller를 재시작합니다.
   표준 ConfigMap 이름은 `argocd-image-updater-config`이며 Helm 설치는 실제 이름/values를 확인합니다.
   이 전역 ConfigMap을 앱 Overlay에 넣으면 잘못된 namespace에 생성되므로 별도 운영 설정으로 반영합니다.

   ```yaml
   registries:
     - name: ChargeOps Docker Registry
       prefix: registry.chargeops.lab:5000
       api_url: http://registry.chargeops.lab:5000
   ```

`insecure: true`만 추가하는 것은 HTTPS 인증서 검증을 생략하는 설정이며 HTTP endpoint 지정과 다릅니다.
Pod image에는 `http://` 또는 `https://`를 붙이지 않습니다.
공식 참고: [Image Updater registry 설정](https://argocd-image-updater.readthedocs.io/en/stable/configuration/registries/),
[containerd registry hosts 설정](https://github.com/containerd/containerd/blob/main/docs/hosts.md),
[Docker HTTP registry 설정](https://docs.docker.com/reference/cli/dockerd/#insecure-registries).

## 검증 및 전환 순서

1. 위 DNS/프로토콜 설정과 익명 접근을 확인하고 이미지 8종을 새 레지스트리에 Push합니다.
   클러스터의 Image Updater와 노드에서도 registry 접근을 확인합니다.
2. 전환 중 기존 Image Updater가 Overlay에 이전 주소를 다시 쓰지 않도록 해당 Application의 이미지 업데이트를 일시 조정합니다.
3. 관리 VM에서 아래 검증을 수행한 뒤 Git 변경을 반영하고 새 ImageUpdater 설정을 적용합니다.
   Application은 자동 Sync이므로 main 반영 전에 준비를 마쳐야 합니다.

   ```bash
   sh scripts/validate-registry.sh
   kubectl apply --dry-run=server -f artifacts/chargeops-dev.yaml
   kubectl apply --dry-run=server -f chargeops-chatops/argocd/image-updater.yaml
   # main 반영 및 registry 준비 후
   kubectl apply -f chargeops-chatops/argocd/image-updater.yaml
   kubectl -n argocd describe imageupdater chargeops-image-updater
   kubectl -n ops get pods
   kubectl -n ops get events --sort-by=.lastTimestamp
   ```

4. 새 registry digest가 main에 기록되고 Argo CD가 Synced/Healthy인지 확인합니다.
   Pod 이벤트에서 ImagePullBackOff, 인증, DNS, HTTPS/HTTP 오류가 없는지 확인합니다.
   중지된 Deployment 3개와 suspend된 CronJob 3개는 의도적으로 유지합니다.
   검증 스크립트와 server dry-run은 실제 이미지의 존재/다운로드 성공까지 보장하지 않습니다.

작업 시 저장소의 기존 35개 파일(숨김 파일 포함, `.git` 내부 이력 제외)을 전부 검토했습니다.
로컬 검증에는 모든 Base와 Overlay의 Kustomize 렌더링 및 이미지 매핑/업데이트 후 digest 반영 검사를 포함합니다.
실제 서버 dry-run, 앱 이미지 빌드/Push, Secret 변경 및 배포는 로컬 연결이 없어 수행하지 않았습니다.
