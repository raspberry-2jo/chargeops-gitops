# Bootstrap Sync 실패 확인

## Baseline HTTP 검사에서 전체 대상이 Timeout인 경우

CSMS 내부의 `127.0.0.1:8000/healthz`는 HTTP 200인데 검사 Pod에서 모든 Service가 Timeout이라면 기존 ingress 정책의 허용 대상을 확인한다. 운영 `csms-ingress`는 기존 앱만 허용하므로 별도 라벨이 없는 Analysis Job은 차단될 수 있다.

Canary overlay는 검사 Pod에 `chargeops.io/role: canary-probe` 라벨을 붙이고, 새 `NetworkPolicy/csms-canary-probe`로 같은 namespace의 해당 Pod에서 `app: csms` Pod의 TCP 8000으로만 접근을 허용한다. 기존 Helm 관리 정책은 변경하거나 GitOps에 복사하지 않는다. 이 허용은 라벨을 기준으로 하며 ServiceAccount를 인증하는 규칙은 아니다.

최신 main을 Pull하고 bootstrap을 다시 실행하면 새 정책과 AnalysisTemplate이 Argo CD 선택 Sync로 반영된 뒤 새 baseline 검사가 실행된다. 과거 실패한 AnalysisRun을 삭제하거나 성공으로 변경할 필요는 없다. 기존 임시 검사 명령에는 이 라벨이 없으므로 재사용하려면 `kubectl run`에 `--labels=chargeops.io/role=canary-probe`를 추가해야 한다.

## Git / Sync 오류

## 승인 직전 volumeMounts / volumes 차이로 중단되는 경우

Git의 runtime patch에는 CA만 있는데 실제 Deployment에 `csms-app-test` 마운트가 남을 수 있다. Kustomize의 `$patch: replace`는 렌더링 시 동작하며, 클러스터의 이전 Helm 소유 필드를 일반 client-side apply로 반드시 삭제한다는 뜻은 아니다. 기존 bootstrap은 replicas가 이미 0인 재시도에서 Deployment Sync를 건너뛰어 이 차이를 놓쳤다.

최신 bootstrap을 `--migrate-code-mount`로 재실행한다. 먼저 고정 이미지와 ConfigMap 코드 일치, Rollout 4개와 baseline 가용성 검사를 검증한다. 이후 Git의 Deployment replicas가 0인지 확인하고, 서버 replacement dry-run을 통과한 뒤 해당 Deployment 하나만 Argo CD `Replace=true` 선택 Sync로 맞춘다. `Force=true`, prune, 리소스 삭제는 사용하지 않는다. CA와 ConfigMap 리소스 자체는 보존한다.

이 작업은 실제 Pod template을 바꾸므로 같은 이미지여도 Rollout Pod 교체가 발생할 수 있다. 완료 전 최신 Deployment generation을 Rollout이 관찰했고 stable ReplicaSet의 Pod spec이 일치하며 4개가 가용한지 확인한다. 실제 Deployment runtime이 Git과 계속 다르면 PASS를 출력하지 않는다. 이후 Jenkins를 새로 실행해 Dev 검증과 승인부터 다시 진행한다.

## Git 커밋 발행 확인

`--migrate-code-mount`의 코드 비교 성공은 이미지 검증 결과이며, Argo CD Sync 완료를 의미하지 않는다. 실패 시 Deployment, Rollout, PVC를 삭제하거나 Force/Replace Sync를 실행하지 않는다.

mgmt의 GitOps 저장소에서 다음을 확인한다. 출력 공유 시 인증 정보는 제외한다.

```bash
git status --short --branch
git log --oneline origin/main..HEAD
kubectl -n argocd get application chargeops-chatops-dev \
  -o jsonpath='{.status.operationState.phase}{"\n"}{.status.operationState.message}{"\n"}{range .status.operationState.syncResult.resources[*]}{.kind}{"/"}{.name}{": "}{.message}{"\n"}{end}'
```

첫 실행에서 커밋 후 Push가 실패했다면 작업 트리가 깨끗해도 로컬 main만 앞서 있을 수 있다. 미발행 커밋이 의도한 runtime patch인지 확인한 뒤 기존 커밋을 발행한다. GitHub HTTPS 비밀번호 입력에는 저장소 쓰기 권한이 있는 PAT를 사용한다. PAT는 명령행이나 Git 파일에 기록하지 않는다.

```bash
git log --stat origin/main..HEAD
git push origin HEAD:main
git pull --ff-only origin main
```

Push가 거절되거나 브랜치가 분기되었다면 강제 Push나 reset으로 해결하지 말고 커밋 차이를 먼저 확인한다. 최신 bootstrap은 미발행 커밋이 있으면 클러스터 변경 전에 중단한다. Argo CD Sync 오류에는 원인에 따른 고정 안내를 표시하며, 원본 API 오류나 매니페스트 값은 출력하지 않는다.

실제 Sync 메시지의 원인을 해결하고 진행 중인 Argo CD 작업이 없는 것을 확인한 후 재시도한다.

```bash
.ci-venv/bin/python scripts/bootstrap-canary.py --migrate-code-mount
```

완료 기준은 스크립트의 `PASS` 출력이다. 그 뒤 생성된 Canary kubeconfig를 Jenkins `chargeops-canary-kubeconfig`에 등록하고 Dev 검증 → 운영 승인 → Ops Canary 순서로 테스트한다. bootstrap 자체도 Ops의 초기 전환 작업이다.
