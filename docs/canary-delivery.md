# CSMS Canary 배포와 GitOps 복구

기존 Dev 검증 → Jenkins 관리자 승인 뒤에 Argo Rollouts를 연결한다. 대상은 CSMS이며 DB 설정·스키마·데이터와 다른 앱은 변경하지 않는다. 별도 DB는 만들지 않는다.

## 배포 흐름

1. Jenkins가 Dev에 후보 digest를 배포하고 기존 통합 테스트를 수행한다.
2. 관리자 승인 전에는 Ops 이미지가 바뀌지 않는다.
3. 승인 후 운영 기준과 Canary 정책이 검증 당시와 같은지 확인한다.
4. 검증한 동일 digest를 Prod GitOps에 커밋하고 **기존 운영 Application의 CSMS Deployment만** 해당 Git 커밋으로 Sync한다. Deployment는 0개를 유지하고 Rollout의 `workloadRef`에 실행 설정을 제공한다.
5. Rollouts가 4개 기준으로 25% → 50% → 100%를 진행한다. 각 단계는 분석 Job 통과 후에만 진행한다.
6. 실패하면 Rollouts가 중단하고 이전 stable ReplicaSet으로 복구한다. Jenkins는 실패 이미지의 Git digest를 이전 값으로 되돌리는 새 커밋을 Push하고 해당 커밋을 다시 Sync한다.
7. 성공·실패·복구 revision은 Jenkins의 `production-delivery.json`에 보관한다.

운영 Application 이름은 기존 `chargeops-chatops-dev`를 유지하고 경로만 `overlays/canary`로 전환한다. 운영 Application을 하나 더 만들지 않는다. Auto Sync는 계속 끈다. 승인 후의 선택적 Sync 요청만 Jenkins가 생성한다. Image Updater는 계속 Dev만 관리한다.

## 지표 기준과 가용성 검사

각 단계에서 새 연결로 초당 각 대상 1회, 60회씩 `/healthz`를 GET한다. 대상은 운영 `csms`, 후보 `csms-canary`, 기존 `csms-stable`이다. 100% 단계에서는 기존 버전이 0개일 수 있으므로 stable HTTP 검사를 제외한다.

| 검사 | 통과 기준 |
|---|---|
| HTTP 가용성 | 대상별 오류율 1% 이하, 60개 표본 필수 |
| 응답 내용 | HTTP 200, JSON `ok: true` |
| 응답 시간 | 대상별 성공 요청 p95 1초 이하 |
| 후보 지연 증가 | stable 비교가 가능한 단계에서 p95가 `max(200ms, stable p95 × 2)` 이하 |
| Pod | Ready CSMS 4개 이상, 분석 중 재시작 증가·Pod 소실 없음 |
| 진행 제한 | Rollout 진행 제한 600초 초과 시 abort, 분석 Job 제한 240초, Jenkins 관찰 제한 20분 |

기준은 `overlays/canary/probe.py`, 단계와 replica는 `rollout.yaml`, Job 제한은 `analysis.yaml`에서 관리한다. 초깃값은 실측 SLO를 대신하지 않는다. 현재 검사는 실제 HTTP/DB 읽기 경로에 대한 합성 가용성 검사이며 운영 요청 전체의 오류율·OCPP 처리율·연결 재접속 성공률을 측정한 결과는 아니다. Prometheus 서버나 신규 DB 설정을 요구하지 않는다. 기존 `/healthz`의 DB 읽기 동작을 사용한다.

별도 트래픽 라우터 없이 ReplicaSet의 Pod 수로 비율을 조절한다. 정확한 요청 비율을 보장하지 않는다. 특히 기존 OCPP WebSocket은 연결된 Pod에 남으며, 새 연결부터 Pod 분배의 영향을 받는다. 정상 종료 시에도 기존 연결은 재접속이 필요할 수 있다. 이 구성만으로 OCPP 무중단을 보장하지 않는다.

## 변경·생성 파일

| 저장소 | 파일 | 역할 |
|---|---|---|
| 앱 | `Jenkinsfile` | Canary 옵션·승인·선택적 Sync·최종 복구 처리 |
| 앱 | `scripts/env_cd.py` | Canary 정책을 후보에 결합, 승인된 승격·복구 기록 저장 |
| 앱 | `scripts/canary_cd.py` | 운영 기준 확인, 배포 관찰, abort, Git 복구 및 Sync |
| GitOps | `overlays/canary/` | Rollout, stable/canary Service, AnalysisTemplate, 읽기 검사 코드 |
| GitOps | `argocd/application-canary.yaml` | 기존 Ops Application을 Canary 경로로 전환 |
| GitOps | `argocd/canary-rbac.yaml` | 검사 Pod 읽기 권한과 제한된 Jenkins 운영 관찰·중단·Sync 권한 |
| GitOps | `scripts/bootstrap-canary.py` | 고정 버전 컨트롤러 설치, 운영 설정 보존, 동일 이미지로 전환, kubeconfig 생성 |
| GitOps | `scripts/rehearse-canary.py` | DB 없는 별도 Namespace에서 실제 성공·실패 시나리오 검사 |
| GitOps, 초기 전환 시 생성 | `overlays/prod/csms-runtime.yaml` | 실제 운영 CSMS의 command·환경 참조·CA·배치 설정 보존 |
| GitOps, 초기 전환 시 수정 | `overlays/prod/kustomization.yaml` | runtime patch 추가. 이미지 digest 유지 |

표의 경로는 `chargeops-chatops/` 아래 overlay·argocd 경로를 줄여 표기했다. 컨트롤러 설치 버전은 `v1.10.1`, 공식 설치 YAML의 SHA-256을 검증한다. 컨트롤러 이미지는 mgmt Docker로 사설 Registry에 미러링하고 push 결과 digest로 고정한다. 설치 YAML은 Git에 복사하지 않는다.

## mgmt에서 초기 전환

로컬 개발 환경에서는 클러스터 접근 정보가 없으므로 실제 설치·서버 dry-run·Sync는 수행하지 않았다. 다음 명령을 mgmt에서 실행하면 스크립트가 해당 작업을 수행한다. 기존 Jenkins 승인 대기/운영 Sync를 먼저 끝낸 뒤 시작한다.

```bash
cd ~/chargeops-gitops
git pull --ff-only origin main
.ci-venv/bin/python -m pip install -r requirements-dev.txt

# 운영 앱 전환 전에 컨트롤러와 제한 RBAC만 설치
.ci-venv/bin/python scripts/bootstrap-canary.py --controller-only

# DB·운영 앱을 사용하지 않고 실제 컨트롤러의 성공/자동 중단 검사
.ci-venv/bin/python scripts/rehearse-canary.py
.ci-venv/bin/python scripts/rehearse-canary.py --cleanup

# 운영 기준 검증 → 설정 보존·커밋·Push → Argo CD를 통한 동일 이미지 전환
.ci-venv/bin/python scripts/bootstrap-canary.py
```

초기 전환은 다음 순서로 실행한다.

1. clean `main`, 수동 운영 Application, 고정된 운영 digest, 정상 CSMS 4개를 확인한다.
2. 운영 Deployment의 Pod 설정을 검사해 runtime patch로 저장하고 **Git에 커밋·Push**한다. Secret 데이터는 읽지 않는다. 환경변수는 Secret/ConfigMap 참조와 허용된 비밀이 아닌 값만 보존한다. `env`가 공통 base와 병합되어 이전 override가 되살아나지 않도록 Pod template을 교체한다.
3. 컨트롤러를 설치하고 기존 운영 Application을 Canary 경로로 전환한다. 서버 dry-run 후 CSMS만 기존 digest로 Sync한다. RollingUpdate의 `maxUnavailable=0`을 적용한다.
4. 동일 이미지로 Rollout 4개를 준비한다. 이때 기존 Deployment 4개가 함께 있으므로 **일시적으로 CSMS 최대 8개와 분석 Job**을 수용해야 한다. 준비되지 않으면 기존 Deployment를 0개로 줄이지 않는다.
5. 초기 Rollout은 Canary 단계를 건너뛰므로 별도 기준 AnalysisRun도 통과시킨다. 그 뒤에만 Deployment replica를 Git에서 0으로 바꾸고 커밋·Push·Sync한다. 이후 Rollout이 4개를 관리한다.
6. `~/.chargeops-jenkins/canary-kubeconfig.yaml`을 권한 600으로 생성하고 권한을 검증한다. 이 파일은 Git에 넣지 않는다.

기존 `chargeops-config`, `chargeops-secrets`, `chargeops-ca`, PVC, 기존 headless/LB Service 등은 생성·복사·삭제하지 않는다. 실제 command·CA 참조·affinity·topologySpread는 live Deployment에서 보존한다. 다만 `csms-app-test` 등으로 `/app/csms/app.py` 또는 앱 디렉터리를 덮어쓰면 이미지 검증이 무효가 되므로 **초기 전환 전에 중단**한다. 이 경우 수정 코드를 앱 이미지에 반영하고 코드 덮어쓰기를 제거한 운영 기준부터 준비해야 한다. 임의로 해당 마운트를 삭제하지 않는다.

부분 실패 시 기존 리소스를 삭제하지 말고 오류 원인을 해결한 후 같은 스크립트를 재실행한다. 생성한 runtime patch를 재사용한다. kubeconfig만 갱신할 때는 다음 명령을 사용한다.

```bash
.ci-venv/bin/python scripts/bootstrap-canary.py --credential-only --duration 24h
```

## Jenkins에서 활성화

생성한 `canary-kubeconfig.yaml`을 기존 Dev 파일과 같은 방식으로 Windows에 전송하고 Jenkins **Secret file**, ID **`chargeops-canary-kubeconfig`**로 등록한다. 기본 요청 수명은 24시간이며 API 서버가 제한할 수 있다. 만료 전에 `--credential-only`로 갱신해 교체한다. Dev kubeconfig는 기존 `chargeops-dev-kubeconfig`를 계속 사용한다.

Canary 계정은 Ops Secret 읽기·Deployment 직접 수정 권한이 없다. CSMS `Rollout/status` abort와 기존 Ops Application의 patch 권한이 있다. Kubernetes RBAC는 Application patch의 특정 필드만 제한할 수 없으므로 Jenkins Job/Replay 및 해당 credential 사용 권한은 승인 담당자에게 제한한다.

앱 저장소 `kim_chanju`의 최신 Jenkinsfile로 다음 옵션을 사용한다.

```text
ENABLE_ENVIRONMENT_CD = true
ENABLE_CANARY = true
BUILD_ALL_IMAGES = true     # 첫 확인용, 이후 변경 감지 사용
RUN_OCPP_FIXTURE_TEST = false
PUBLISH_DEV_TAG = false
```

두 옵션의 기본값은 false다. 초기 전환 전에 Push만으로 운영 앱이 Canary로 전환되지는 않는다. Dev 검증 후 `admin`이 승인하면 자동 Canary가 시작한다. 이후 수동 전체 Sync를 겹쳐 실행하지 않는다.

```bash
kubectl -n ops get rollout csms -w
kubectl -n ops get analysisruns,jobs
kubectl -n ops get pods -l app=csms -o wide
kubectl -n ops logs job/<분석-Job-이름>
```

## 실패·중단·복구

Analysis 실패 시 Rollouts가 자동으로 abort한다. Pod가 시작하지 못해 분석이 Inconclusive로 멈추거나 Jenkins 관찰 시간이 초과되어도 Jenkins가 abort와 Git 복구를 시도한다. 검증 후보와 정책은 지문으로 결합하며 승인 후 다른 사람이 정책·이미지를 바꾸면 배포를 중단한다.

Jenkins 중단·timeout은 관찰 스크립트와 `post`에서 복구를 시도한다. 미완료 복구 기록은 다음 빌드가 덮어쓰지 않는다. 에이전트 강제 종료·네트워크 단절·Git 권한 만료 등으로 Git Push까지 완료하지 못하면 자동 완료를 보장할 수 없다. 컨트롤러가 Analysis 실패를 중단하는 것과 Git 복구 성공은 별개다. Jenkins 작업 공간이나 보관한 파일에서 `production-delivery.json`을 보존하고 동일 GitOps checkout에서 재실행한다.

```bash
# 앱 저장소/Jenkins 작업 공간에서, GitOps 쓰기와 Canary kubeconfig를 제공한 상태
.ci-venv/bin/python scripts/canary_cd.py recover \
  --repo .gitops-delivery --delivery artifacts/production-delivery.json
```

복구는 전체 커밋을 revert하지 않고 CSMS image 항목만 이전 digest로 바꿔 다른 사람의 변경을 보존한다. 더 새로운 운영 이미지가 있으면 덮어쓰지 않는다. 완료한 복구를 다시 실행해도 이미 성공한 배포를 취소하지 않는다. Git 복구 후 Argo CD 선택적 Sync와 이전 stable 준비 상태까지 확인한다. DB·PVC는 복구 대상에 넣지 않는다.

Canary 자체를 제거할 때는 먼저 이전 digest의 정상 Rollout을 확인한 뒤 runtime patch의 Deployment replica를 4로 되돌려 Git Push·선택적 Sync한다. Deployment 4개가 실제로 준비된 후에만 Rollout을 0으로 줄인다. 기존 리소스 소유 관계를 확인하기 전에는 Rollout/ReplicaSet/Service를 삭제하거나 `application.yaml`로 경로를 되돌리지 않는다. 복구 작업의 순서를 건너뛰면 가용성에 영향을 줄 수 있다.

## 근거 문서

- [Argo Rollouts: Deployment 참조와 전환](https://argo-rollouts.readthedocs.io/en/stable/migrating/)
- [Job 기반 Analysis](https://argo-rollouts.readthedocs.io/en/stable/analysis/job/)
- [Rollout 명세와 진행 제한](https://argo-rollouts.readthedocs.io/en/stable/features/specification/)
- [Rollouts의 중단과 Git 상태](https://argoproj.github.io/argo-rollouts/FAQ/)
