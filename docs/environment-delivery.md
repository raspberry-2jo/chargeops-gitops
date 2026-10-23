# Dev / Ops 분리와 승인 배포 (1~7단계)

## 이번 변경의 범위

- 기존 `ops`와 Application `chargeops-chatops-dev`는 유지한다. 이름의 dev는 기존 이름이며 운영 대상이다.
- 운영 설정은 `overlays/prod`, 신규 개발 환경은 `overlays/development`이다.
- 기존 `overlays/dev`는 운영 Application의 기존 경로이므로 `prod`를 참조하는 호환 경로로 남긴다. 이 경로를 개발용으로 바꾸지 않는다.
- 변경 전 운영 렌더링과 변경 후 `prod`/호환 `dev` 렌더링은 동일함을 로컬에서 검증했다. 운영 리소스를 새 Application에 중복 등록하지 않는다.
- 신규 `chargeops-dev` Application은 Namespace `dev`의 CSMS 1개만 자동 Sync한다. Collector·notifier·bot·vcharger는 개발 환경에 아직 추가하지 않는다.
- 사용자의 후속 지시에 따라 DB 설정은 기존 값을 유지한다. 개발 Namespace의 별도 Secret에 DB DSN과 CSMS 토큰 두 키만 준비한다. DB 생성·스키마 변경·마이그레이션·기존 Secret 변경은 하지 않는다.
- 기존 운영 Secret·ConfigMap·CA·PVC·Service·배치 작업은 복사해 Git 관리 대상으로 삼지 않는다.
- 기본 통합 검증은 API `/healthz`, DB 읽기 전용 트랜잭션과 CSMS 기준 데이터 확인이다. 공유 DB에 OCPP 상태를 쓰는 검증은 기본 OFF이다.
- Canary/Rollouts와 자동 Git 복구는 선택 기능으로 추가했다. 활성화·검증 방법은 [Canary 배포 안내](canary-delivery.md)를 따른다. 초기 전환 전에는 기존 수동 Sync를 유지한다.

## 로컬에서 완료한 것과 서버에서 해야 하는 것

파일·파이프라인·승격 스크립트·회귀 테스트는 저장소에 반영했다. 실제 클러스터 접속 설정이 없어 Namespace 생성, Secret 준비, Application 적용, Jenkins credential 등록 및 실제 통합 테스트는 실행하지 않았다.

처음에는 앱 Job의 `ENABLE_ENVIRONMENT_CD=false`, `PUBLISH_DEV_TAG=false`를 유지한다. 고유 build 태그는 계속 발행하지만 `:dev` 태그는 갱신하지 않는다. 아래 초기 설정을 마친 뒤 환경 CD를 켠다. 이전 Job이 실행 중이라면 기존 태그 발행이 끝났는지도 확인한다.

## 초기 설정: mgmt에서 실행

먼저 현재 컨텍스트가 의도한 클러스터인지 확인한다. 아래 명령은 GitOps 저장소 루트 기준이다.

```bash
git pull --ff-only
kubectl config current-context
kubectl -n ops get deploy,svc,pvc
kubectl -n argocd get applications,imageupdaters
python3.12 -m venv .ci-venv
.ci-venv/bin/python -m pip install -r requirements-dev.txt
.ci-venv/bin/python scripts/validate-environments.py
```

### 1. 운영 자동 갱신 경로부터 닫기

현재 운영 Application을 수동 Sync로 전환한다. `kubectl apply`만으로 기존 자동 Sync 필드가 제거되지 않는 경우도 있으므로 명시적으로 제거한다. Application이나 워크로드는 삭제하지 않는다.

```bash
kubectl -n argocd patch application chargeops-chatops-dev --type=merge \
  -p '{"spec":{"syncPolicy":{"automated":null}}}'
kubectl apply -f chargeops-chatops/argocd/image-updater.yaml
```

Image Updater는 새 `chargeops-dev`만 선택하게 된다. 운영 Application은 선택하지 않는다. 새 Dev Application이 아직 없어도 운영 갱신 제거를 먼저 한다.

### 2. Dev에 기존 DB 설정 준비

```bash
.ci-venv/bin/python scripts/prepare-dev-secret.py
kubectl -n dev get secret chargeops-dev-secrets
```

이 스크립트는 `ops/chargeops-secrets`의 `DB_DSN`, `CSMS_API_TOKEN`만 메모리에서 읽어 `dev/chargeops-dev-secrets`에 server-side apply한다. 값·Secret YAML을 파일이나 로그에 출력하지 않는다. 기존 Ops Secret은 변경하지 않는다. Helm release Secret이나 Slack 토큰 전체를 복사하지 않는다.

**Namespace만 분리되며 DB 데이터는 공유된다.** NetworkPolicy는 Dev Pod의 접근 대상을 제한하지만 같은 DB 안의 데이터를 분리하지 않는다. 현재 DB 호스트 10.10.30.11/.12의 6432/5432 포트를 허용한다. 실제 DB·DNS 주소가 다르면 Dev NetworkPolicy를 먼저 맞춘다. CNI의 NetworkPolicy 지원과 NodeLocal DNS 사용 여부도 운영자가 확인해야 한다.

CSMS는 PVC를 사용하지 않으므로 개발 PVC를 불필요하게 만들지 않는다. 운영 PVC는 그대로 둔다. 기존 DSN이 CA 파일을 지정한다면 같은 신뢰 CA를 Dev에 별도로 준비하고 Dev Pod/검증 Job에 마운트해야 하므로, 초기 Sync 전에 해당 경로를 확인한다. CA/Secret을 Git에 넣지 않는다.

### 3. 운영 이미지 digest 고정 및 원본 확인

Registry는 HTTPS를 사용한다. Python 조회도 HTTPS로 접속하며 해당 사설 Registry에는 환경변수 프록시를 사용하지 않는다. 시스템 CA와 기존 Docker CA `/etc/docker/certs.d/registry.chargeops.lab:5000/ca.crt`를 신뢰한다. CA 파일이 다른 경로에 있으면 실행 전에 `export CHARGEOPS_REGISTRY_CA=/실제/CA/파일.crt`를 설정한다. Jenkins agent에도 같은 신뢰 CA가 필요하다. 인증서 검증을 비활성화하거나 HTTP로 우회하지 않는다.

```bash
.ci-venv/bin/python scripts/freeze-production.py
git diff -- chargeops-chatops/overlays/prod/kustomization.yaml
```

이 스크립트는 DB DSN 유지 여부, 운영 자동 Sync 중지, Dev-only ImageUpdater, 현재 운영 Pod Ready 상태, 실제 Pod digest와 Registry manifest를 확인한 후 **로컬 Prod 이미지 설정만** digest로 고정한다. 이미지 index·플랫폼 manifest·config ID의 관계도 검증한다. 태그가 이미 이동했으면 현재 실행 Pod의 digest를 Registry에서 직접 조회해 존재와 내용이 검증된 경우 그 버전을 보존한다. Pod별 digest가 섞였거나 기존 manifest가 사라졌으면 중단한다. 실제 운영/Git의 이미지 참조 자체가 다르면 원본부터 맞춰야 한다. Pod 이름·노드·이미지 ID 기준은 Git에서 제외된 `artifacts/ops-baseline.json`에 기록하며 비밀 값은 포함하지 않는다. Kubernetes 변경·commit·push는 하지 않는다. 이전 digest를 임의로 최신 태그의 digest로 교체하지 않는다.

검토가 끝나면 고정 설정을 별도로 커밋·푸시한다.

```bash
git add chargeops-chatops/overlays/prod/kustomization.yaml
git commit -m "chore(gitops): freeze current ops image digests"
git push origin main
```

예전 앱 Job 또는 다른 발행 Job이 `:dev` 태그를 Push할 수 있으므로 운영 digest 고정 전에 해당 Job도 확인한다. 새 앱 Jenkinsfile은 `PUBLISH_DEV_TAG=false`가 기본이며 고유 build 태그만 발행한다.

### 4. Application 경로 전환과 Dev 적용

Dev Namespace가 준비된 후 관리자 컨텍스트로 서버 dry-run을 수행한다.

```bash
kubectl kustomize chargeops-chatops/overlays/prod | kubectl apply --dry-run=server -f -
kubectl kustomize chargeops-chatops/overlays/development | kubectl apply --dry-run=server -f -
kubectl apply -f chargeops-chatops/argocd/application.yaml
kubectl apply -f chargeops-chatops/argocd/application-dev.yaml
kubectl apply -f chargeops-chatops/argocd/dev-verifier-rbac.yaml
kubectl -n argocd get application chargeops-chatops-dev chargeops-dev
```

운영 Application은 계속 `ops`를 관리하며 경로만 `overlays/prod`로 바뀐다. 자동 Sync는 없다. 운영의 변경 diff를 확인하고 필요한 경우 수동 Sync한다. Dev는 자동 Sync 후 `kubectl -n dev rollout status deployment/csms`로 확인한다. 초기 Dev 이미지는 `:dev`이며 첫 Jenkins 환경 배포 또는 Image Updater가 digest로 고정한다.

GUI에서 `chargeops-dev`가 신규 Dev, 기존 `chargeops-chatops-dev`가 Ops이다. Dev API를 mgmt에서 확인하려면 `kubectl -n dev port-forward service/csms 18000:8000` 실행 후 다른 터미널에서 `curl --fail http://127.0.0.1:18000/healthz`를 사용한다. 이번 단계에는 외부 Ingress/NodePort를 만들지 않는다.

### 5. Jenkins credential / 권한 준비

앱 Job은 계속 앱 저장소 `kim_chanju`의 루트 `Jenkinsfile`을 사용한다.

| Credential ID 기본값 | 종류 | 용도 |
|---|---|---|
| `chargeops-gitops-write` | Username / Password | GitOps 읽기·이미지 변경 Push. Password는 저장소 권한 토큰 |
| `chargeops-dev-kubeconfig` | Secret file | Dev 검증용 kubeconfig |

검증 kubeconfig는 `dev/jenkins-dev-verifier` ServiceAccount 또는 동등한 제한 권한을 사용한다. Dev Deployment·Pod·ConfigMap 읽기, 검증 Job 생성/조회, 로그 읽기와 `argocd/chargeops-dev` 단일 Application 읽기만 허용한다. Ops 리소스 쓰기·Secret 읽기는 부여하지 않는다. ServiceAccount 토큰의 유효 기간과 Jenkins credential 갱신은 운영 정책에 맞춘다. 기본 사용자 kubeconfig나 cluster-admin kubeconfig를 검증 credential로 등록하지 않는다.

mgmt의 관리자 컨텍스트에서 `.ci-venv/bin/python scripts/prepare-dev-kubeconfig.py`를 실행하면 Namespace·검증 계정/RBAC와 수동 ServiceAccount 토큰 Secret을 준비하고 권한을 검사한 뒤 `~/.chargeops-jenkins/dev-kubeconfig.yaml`을 권한 600으로 생성한다. 앱을 배포하거나 Ops 워크로드를 변경하지 않는다. 이 파일을 브라우저 PC로 안전하게 전송한 뒤 Jenkins `Secret file` 타입, ID `chargeops-dev-kubeconfig`로 등록한다. 기본 토큰은 만료 시간이 없으며 `dev/jenkins-dev-verifier-token` Secret에 보관한다. 재실행 시 같은 계정·UID의 토큰을 재사용한다. 기존 24시간 토큰은 자동으로 변경되지 않으므로 스크립트를 한 번 재실행하고 Jenkins 파일을 교체한다. 필요하면 `--duration 24h`로 만료형 토큰을 명시적으로 요청할 수 있다. 파일 내용이나 인증 값은 대화·Git에 넣지 않는다.

무기한 토큰은 Secret 또는 ServiceAccount를 삭제하거나 서버에서 무효화하면 사용할 수 없다. 토큰을 새로 발급하려면 관리자 컨텍스트에서 `kubectl -n dev delete secret jenkins-dev-verifier-token` 후 생성 스크립트를 재실행하고 Jenkins 파일을 교체한다. 이 작업은 기존 토큰을 사용하는 인증을 끊으므로 교체할 때만 수행한다. 실제 토큰 데이터가 들어간 Secret YAML은 Git에 저장하지 않는다. [Kubernetes의 수동 장기 토큰 방식](https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/#manually-create-a-long-lived-api-token-for-a-serviceaccount)을 사용한다.

승인자는 Jenkinsfile의 `submitter: 'admin'`이다. Job/Replay/Configure 권한과 저장소 쓰기 권한을 제한해야 승인 우회를 줄일 수 있다. Git 서버의 branch protection, Argo CD 사용자 RBAC, Jenkins 사용자·credential 생성은 이 저장소 변경만으로 적용되지 않는다. 관리자 권한자는 정책을 우회할 수 있다.

Image Updater용 계정은 Dev 변경 용도로만 사용하고 운영 파일 변경은 코드 리뷰/보호 규칙으로 통제한다. 한 Git 브랜치의 토큰만으로 경로별 쓰기 권한이 자동 분리되는 것은 아니다. 운영에는 Auto Sync가 없으므로 Git 변경만으로 Ops 배포가 실행되지는 않는다. 공통 Base 변경은 운영 변경으로 검토한다.

## 테스트 흐름

1. Jenkins의 Build with Parameters에서 `ENABLE_ENVIRONMENT_CD=true`, `BUILD_ALL_IMAGES=true`로 첫 실행한다. `RUN_OCPP_FIXTURE_TEST=false`를 유지한다.
2. Jenkins는 고유 build 태그를 Registry digest로 해석하고 `development/kustomization.yaml`만 갱신한다.
3. Dev가 해당 digest로 Synced/Healthy 및 CSMS Ready가 될 때까지 기다린다. 검증 Job에서 API와 DB 읽기 확인을 실행한다.
4. 테스트 성공 후 `admin` 승인 대기 화면이 나타난다. 승인 전 운영 overlay는 바뀌지 않는다. 거절·실패·시간 초과 시 운영 승격 단계는 실행되지 않는다.
5. 승인 후 Dev 이미지·설정 및 운영 기준 설정이 그대로인지 다시 확인한다. 성공한 검증 Job의 UID/결과, 2시간 이내 검증 기록도 확인한다. 달라졌으면 중단하고 새 빌드로 재검증한다.
6. Registry 태그를 재조회하거나 이미지를 다시 빌드하지 않고, 검증한 digest를 `prod/kustomization.yaml`에 커밋한다.
7. 운영 Application `chargeops-chatops-dev`에서 diff를 검토하고 수동 Sync한다. 이 구성은 Jenkins 승인과 운영 수동 Sync의 두 단계다.

현재 환경 승격 대상은 CSMS뿐이다. AI gateway 등 다른 이미지 변경은 기존 Build/Push만 수행한다. 승인 대기는 최대 30분이며 전체 Job 제한은 120분이다. 대기 중 다음 빌드는 `disableConcurrentBuilds`로 직렬화된다.

운영 digest 고정과 Dev-only ImageUpdater 반영을 완료했다면 `PUBLISH_DEV_TAG=true`로 Dev 추적용 태그 갱신을 켤 수 있다. Jenkins 환경 CD 자체는 고유 build 태그를 사용하므로 이 옵션이 false여도 실행된다.

### 선택적인 OCPP 검증

`RUN_OCPP_FIXTURE_TEST=true`는 DB에 쓰기를 발생시킨다. 기존 DB에 별도 승인한 `CI-DEV-` 접두사의 가상/모형 충전기를 준비하고 `development/configmap.yaml`의 `TEST_CHARGER_ID`를 맞춘 경우에만 사용한다. 테스트는 데이터를 생성하거나 스키마를 변경하지 않지만 해당 충전기의 세션·Heartbeat·상태를 갱신한다. 운영 충전기 ID를 사용하지 않는다. 기본 읽기 검증은 OCPP 가용성·파드 간 전달까지 검증한 것으로 간주하지 않는다.

## 롤백

- 승인 전 실패: 운영 GitOps는 그대로다. Dev 오류만 수정한다.
- 승인 후, Ops Sync 전 취소: 해당 승격 커밋만 `git revert <승격커밋>`하고 Push한다. 운영에는 새 버전을 Sync하지 않는다.
- Ops Sync 후 앱 롤백: 해당 승격 커밋을 revert해 Push하고 운영 Application에서 수동 Sync한다. DB·PVC는 되돌리거나 삭제하지 않는다.
- 환경 분리 구현 전체를 되돌릴 경우: 먼저 새 Dev Application의 자동 Sync를 끄고 Dev CSMS를 0으로 줄인다. 앱 저장소의 구현 커밋과 GitOps 구현 커밋을 각각 revert한다. 이후 기존 운영 Application/ImageUpdater 정의를 검토해 다시 적용한다. Namespace·Secret·PVC는 자동 삭제하지 않는다.
- 구현 직전 기준: 앱 `58c4a18`, GitOps `4193e73`. `reset --hard`/강제 Push 대신 구현·승격 커밋별 revert를 사용한다. 후속 운영 변경을 함께 되돌리지 않는다.

`ENABLE_CANARY=false`에서는 위 수동 절차를 사용한다. 초기 전환 후 `ENABLE_CANARY=true`이면 승인 후 자동 Canary Sync, 지표 검사, 실패 시 Git digest 복구와 재동기화가 실행된다. [Canary 배포 안내](canary-delivery.md)에 초기 전환과 장애 복구 절차가 있다.
