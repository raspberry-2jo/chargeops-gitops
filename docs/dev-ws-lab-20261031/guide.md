# ChargeOps Dev WebSocket Canary 실습 · 2026-10-31

**실행 위치: 기존 mgmt Linux VM의 vagrant 사용자.** 이 PC에서 원격 클러스터에 접속하지 못해 실제 Dev 적용은 하지 않았다. 아래 명령으로 서버에서 실행한다. 사용자 승인 범위에 맞춰 파일과 커밋을 준비했으며 원격 Push·main 병합·Ops Sync는 하지 않았다.

## 무엇이 바뀌었나

| 항목 | 이번 파일 값 | 적용 범위 |
|---|---|---|
| maxSurge / maxUnavailable | 1 / 0 | 기존 Ops Canary 정의와 새 Dev lab |
| minReadySeconds | **30** (기존 Ops 정의20→30) | 기존 Ops Canary 정의와 새 Dev lab |
| progressDeadlineSeconds / progressDeadlineAbort | 600 / true | 두 Rollout |
| count / failureLimit | 1 / 0 | 새 필수 Web 검사 |
| HTTP Job 제한 | 240초 / backoff0 | 기존 Ops 진단 Job 유지 |
| 서비스 진단 Job 제한 | 420초 / backoff0 | 기존 Ops 진단 Job 유지 |
| 새 Web 검사 | 관찰 최대540초 / 요청660초 | 두 환경의 동일 판정 코드 |

`maxSurge1`은 목표4보다 추가로 허용하는 개수다. `maxUnavailable0`은 교체 중 처리 용량을 미리 줄이지 않도록 한다. `minReady30`은 통신 복구 보장이 아니므로 통신 검사를 따로 한다. progress600은 전체 배포 시간 상한이 아니라 진행 정체 제한이다.

기존 dev/csms Deployment, Dev Argo Application, Ops 리소스와 HPA는 수정하지 않는다. 새 `dev/csms-ws-lab`은 **실제 CSMS 코드**와 실제 OCPP 클라이언트를 사용한다. 이전 synthetic 계약 실험과 다르다. 같은 이미지로 첫 실험을 하면 새 버전 기능 검증이 아닌 배포·검사 동작의 대조 실험이다.

첨부 Lab Kit의 분석/관찰/부하 생성 도구와 테스트는 `labs/dev-ws-kit`에 보존했다. 바로 적용할 수 없던 예제 AnalysisTemplate 대신 `chargeops-chatops/overlays/dev-ws-lab`을 추가했다. HPA 실험은 이번 범위에서 제외했다.

## 1. 실험 구조와 영향 범위

```
mgmt VM ── kubectl(선택한 context, dev 고정)
  ├─ 기존 dev/csms: 유지
  └─ csms-ws-lab Rollout: 4개, 일시적으로 최대5개
       ├─ stable cohort400개 → Stable Service
       ├─ canary cohort400개 → 초기에는 일반 Service, 후보 시작 후 Canary Service로 정상 재접속
       ├─ 읽기 전용 evaluator1개 → Prometheus + lab healthz + lab Kubernetes 상태
       └─ 5초 PodMonitor → lab Pod 지표만 수집
```

두 cohort는 부하 Pod1개의 컨테이너2개이며 ID는 서로 겹치지 않는다. Heartbeat30초, MeterValues60초, 접속 시 StatusNotification1회로 두 집단의 부하를 맞춘다.100% 단계에서 Stable endpoint가 없어지면 stable cohort는 일반 Service로 재접속한다.

실험 최대7 Pod(5 CSMS+평가기1+부하1), 추가 요청 CPU400m/메모리672Mi, 추가 제한 CPU1.65/메모리1792Mi다. 기본 Dev quota가 Pod10개이면 기존 Pod3개일 때만 최대치가 맞는다. 기존 사용량이 더 많으면 preflight가 멈춘다. 노드 여유까지 자동으로 보장하지는 않는다. **Dev CI 검증 Job과 동시에 실행하지 않는다.** quota를 자동으로 올리거나 기존 Pod를 지우지 않는다.

Lab CSMS의 요청은50m/96Mi, 제한은200m/256Mi다. Dev quota 안에서 동작을 확인하기 위한 자원이며 4·5차10,000대 실험과 같은 성능 조건이 아니다. 운영 기준 확정 전에는 운영과 같은 자원·부하 조건으로 재검증한다.

DB에 관해서는 namespace만으로 데이터가 분리되지 않는다. 기존 `chargeops-dev-secrets`의 DB 접속을 사용해 기동 시 상태 코드와 healthz의 SELECT1을 읽는다. 실험 ID800개는 **Dev 전용 opt-in**에서만 메모리로 처리해 충전기 상태·세션·계측 DB 쓰기를 건너뛴다. lab에서는 다른 등록 ID도 접속 전에 거부한다. 이 실험은 DB 쓰기 성능을 증명하지 않는다. 전체 DB를 별도 복제한 환경도 아니다.

## 2. 파일을 서버로 옮기고 별도 저장소 열기

공유 ZIP을 mgmt의 `~/Downloads/ChargeOps_DEV_WS_Lab_20261031.zip`에 업로드했다고 가정한다. 다운로드 디렉터리가 다르면 첫 경로만 바꾼다. ZIP에는 앱/GitOps의 완전한 Git bundle이 있어 원격에 아직 Push하지 않은 커밋도 받을 수 있다.

```bash
mkdir -p "$HOME/chargeops-dev-ws-kit-20261031"
unzip "$HOME/Downloads/ChargeOps_DEV_WS_Lab_20261031.zip" \
  -d "$HOME/chargeops-dev-ws-kit-20261031"

export KIT="$HOME/chargeops-dev-ws-kit-20261031"
export APP="$HOME/chargeops-ws-lab-app-20261031"
export GITOPS="$HOME/chargeops-ws-lab-gitops-20261031"

# 이미 해당 디렉터리가 있으면 덮어쓰지 말고 기존 실험 파일을 확인한다.
test ! -e "$APP" && test ! -e "$GITOPS" || { echo '실험 디렉터리가 이미 있습니다'; exit 1; }
git clone -b codex/ws-canary-gates-20261031 "$KIT/app.bundle" "$APP"
git clone -b codex/ws-canary-gates-20261031 "$KIT/gitops.bundle" "$GITOPS"

python3.12 -m venv "$KIT/.venv"
source "$KIT/.venv/bin/activate"
python -m pip install -r "$APP/requirements-dev.txt"
command -v kubectl
kubectl argo rollouts --help >/dev/null
sudo docker info >/dev/null
```

**성공 확인:** git clone이 끝나고 세 명령이 오류 없이 종료한다. bundle clone의 origin은 로컬 bundle이며 GitHub main에 연결해 Push하지 않는다.

## 3. 실행할 클러스터를 명시하고 현재 상태 저장

```bash
kubectl config get-contexts
export KUBE_CONTEXT="$(kubectl config current-context)"
test -n "$KUBE_CONTEXT" || { echo '서버 kubeconfig context가 필요합니다'; exit 1; }

kubectl --context "$KUBE_CONTEXT" -n dev get deployment,rollout,pods
kubectl --context "$KUBE_CONTEXT" -n dev get resourcequota
kubectl --context "$KUBE_CONTEXT" get nodes

export LAB_SETUP="$KIT/results/setup"
mkdir -p "$LAB_SETUP"
kubectl --context "$KUBE_CONTEXT" -n dev get deployment,rollout,pods -o yaml \
  > "$LAB_SETUP/before-dev.yaml"
```

**해석:** 기존 서비스의 이름/상태를 먼저 기록한다. 이후 생성되는 이름은 모두 `csms-ws-lab`으로 시작한다. 기존 `csms`를 scale0으로 바꾸거나 Application의 path를 수정할 필요가 없다.

## 4. 새 계측·실험 ID 제한이 들어 있는 이미지 빌드

```bash
cd "$APP"
python -m pytest -q tests/
python scripts/ci.py validate

export LAB_IMAGE_TAG="registry.chargeops.lab:5000/chargeops/chargeops-csms:ws-lab-$(git rev-parse --short=12 HEAD)"
sudo docker build --pull -f apps/csms/Dockerfile -t "$LAB_IMAGE_TAG" apps

# DB 접속 없이 이미지 안의 Dev lab ID 제한만 확인한다.
sudo docker run --rm --entrypoint python "$LAB_IMAGE_TAG" -c '
from csms.dev_simulation import simulation_ids
values=simulation_ids({"DEV_SIMULATION_ENABLED":"true","ENVIRONMENT":"dev","POD_NAMESPACE":"dev","DEV_SIMULATION_IDS":"CI-DEV-SIM-01","DEV_WS_LAB":"true","LAB_WORKLOAD":"csms-ws-lab","DEV_LAB_SIMULATION_COUNT":"800"})
assert "CI-DEV-WSLAB-00799" in values and "CI-DEV-WSLAB-00800" not in values
print("PASS: bounded dev memory identities")
'

sudo docker push "$LAB_IMAGE_TAG"
export LAB_IMAGE="$(sudo docker image inspect --format '{{index .RepoDigests 0}}' "$LAB_IMAGE_TAG")"
printf '%s\n' "$LAB_IMAGE" | tee "$LAB_SETUP/image.txt"
```

**성공 확인:** `PASS` 출력 후 image 값이 `registry...chargeops-csms@sha256:...` 형식이다. 태그만 넣으면 환경 생성기가 거부한다. 기존 Ops image tag/digest는 바꾸지 않는다. 이미지 빌드·레지스트리 인증·CA 오류는 먼저 해결한다. 새 이미지의 Trivy 통과를 이 문서가 보증하는 것은 아니며 기존 보안 정책도 변경하지 않았다.

## 5. 적용 전 검사 → Lab 생성

```bash
cd "$GITOPS"
python scripts/dev-ws-lab.py render --context "$KUBE_CONTEXT" \
  --image "$LAB_IMAGE" --output "$LAB_SETUP"

python scripts/dev-ws-lab.py preflight --context "$KUBE_CONTEXT" \
  --image "$LAB_IMAGE" --output "$LAB_SETUP"

python scripts/dev-ws-lab.py apply --context "$KUBE_CONTEXT" \
  --image "$LAB_IMAGE" --output "$LAB_SETUP"

kubectl argo rollouts status csms-ws-lab --context "$KUBE_CONTEXT" -n dev --timeout 300s
kubectl --context "$KUBE_CONTEXT" -n dev get pods \
  -l chargeops.test/workload=csms-ws-lab -o wide
```

**preflight의 성공 조건:** 기존 Dev Secret의 필요한 키 이름 존재, 모든 생성 대상이 Dev lab 소유, 최대 surge 기준 quota 여유, Kubernetes server dry-run 통과. Secret 값을 출력·파일 저장하지 않는다. 기존 같은 이름의 다른 리소스는 덮어쓰지 않는다.

**기대 결과:** CSMS Pod4개가 Running/Ready이고 Rollout이 Healthy이며 부하 Pod1개(컨테이너2개)와 평가기 Pod1개가 뜬다. 처음 생성할 때는 기준 버전이므로 Canary 단계 검사를 생략하는 것이 정상이다. Pod가 Pending이면 `describe pod`의 CPU/메모리/분산 조건/quota/image 원인을 확인한다.

```bash
kubectl --context "$KUBE_CONTEXT" -n dev logs deploy/csms-ws-lab-load -c stable --tail=3
kubectl --context "$KUBE_CONTEXT" -n dev logs deploy/csms-ws-lab-load -c canary --tail=3
```

양쪽 `active:400`, 실패 카운터 증가 없음, Heartbeat/MeterValues 성공이 보여야 한다. 설정을 적용했다고 곧바로800개가 되는 것은 아니며 초기 접속이 끝나고 **180초 이상** 기다려 배포 전120초 자료를 확보한다.

## 6. Prometheus 수집 확인

**별도 터미널에서 계속 실행:**

```bash
kubectl --context "$KUBE_CONTEXT" -n monitoring port-forward \
  svc/kps-kube-prometheus-stack-prometheus 19090:9090
```

서비스 이름이 없으면 `kubectl --context "$KUBE_CONTEXT" -n monitoring get svc`로 실제 Prometheus 서비스를 확인하고 해당 이름을 사용한다.

**기존 터미널에서:**

```bash
curl -fsSG http://127.0.0.1:19090/api/v1/query \
  --data-urlencode 'query=sum(chargeops_csms_active_sessions{namespace="dev",pod=~"csms-ws-lab-.*",kind="service",chargeops_policy_scrape="ws-v1"})'
```

800에 가까운 값이 필요하다. 빈 배열은0이 아니며 미수집이다. PodMonitor/ServiceMonitor의 `release:kps`는 이 프로젝트의 기본 선택값이다. Prometheus CR의 monitor selector가 다르면 **lab monitor의 라벨만** 맞춘다. 기존 Prometheus 수집 정책을 전역 수정하지 않는다.

OOM은 `container_oom_events_total`의 실제 시계열이 필요하다. Lab은 kubelet의30초 수집을 고려해 OOM 최신값 허용을45초로 명시했다. 앱 지표는 여전히5초 수집/15초 신선도다. Ops 기본 허용15초는 변경하지 않았다. OOM 자료가 없으면 성공 처리하지 않으며, start가 멈추거나 검사에서 보류된다. 수집되지 않는 OOM을0으로 대체하지 않는다.

## 7. 선택 실험 A — 정상 배포 대조 실험

가설: 같은 바이너리·같은 부하에서 배포 전후 통신과 네이티브 Canary 검사가 정상 진행된다.

```bash
cd "$GITOPS"
export RUN="$KIT/results/normal-r1"
python scripts/dev-ws-lab.py start --context "$KUBE_CONTEXT" \
  --mode normal --output "$RUN"

kubectl argo rollouts get rollout csms-ws-lab --context "$KUBE_CONTEXT" -n dev --watch
```

start는 건강한4 Pod,120초800연결 기준 자료, 요청 표본과 OOM 수집을 확인한 후 기준 시각/실험 ID를 기록한다. template annotation을 바꿔 새 revision을 만들고, Canary cohort400개를 새 버전으로 **정상 종료 코드1000을 사용해** 재분배한다. Stable400개와 Canary400개를 비교한다. 이는 기존 연결이 자동으로 새 Pod로 옮겨갔다는 증명이 아니다. **25%는 Pod 비율이며, 이 실험의 두 cohort는400:400으로 실제 트래픽을50:50에 가깝게 만든다.** 표본 하한을 확보하기 위한 설계다.25% 단계에서는 후보1개가400연결, Stable3개가 합계400연결을 처리하므로 Pod당 자원 경합은 다르다. 같은 이미지 대조 실험과 CPU throttling·메모리 여유를 함께 보고, 한 번의 결과로 버전 성능 차이라고 단정하지 않는다. 비교에 필요한 신버전 표본을 확보하는 통제된 조치다.

수집이 준비된 상태에서 모든 필수 검사를 통과해야25→50→100→Healthy가 된다. `count1`이지만 평가기 안에서 안정화 후 겹치지 않는60초 창2개를 확인한다. 배포가 반드시 성공해야 하는 실험이 아니다. 실패하면 실제 종료 분류·재연결·표본·수집 상태를 원인으로 기록한다.

```bash
python scripts/dev-ws-lab.py collect --context "$KUBE_CONTEXT" --output "$RUN"
```

추가로5초마다 배포 상태를 기록하고 싶으면 start 직전에 다른 터미널에서 실행한다.

```bash
python "$GITOPS/labs/dev-ws-kit/scripts/observe_rollout.py" \
  --context "$KUBE_CONTEXT" --namespace dev --rollout csms-ws-lab \
  --selector app=csms-ws-lab --seconds 1200 --every 5 \
  --output "$RUN/rollout-timeline.jsonl"
```

나중에 `python labs/dev-ws-kit/scripts/summarize_rollout.py "$RUN/rollout-timeline.jsonl"`로 요약한다. start는 빈 RUN 폴더를 요구하므로 **start 후 recorder를 실행하거나**, recorder 출력은 `$KIT/results/timelines/normal-r1.jsonl`처럼 RUN 밖으로 지정한다.

## 8. 선택 실험 B — 신버전 장애를 정말 중단하는가

A가 끝났다면9번의 recover로 직전 기준 버전을 복구하고180초를 기다린다. 새로운 RUN 폴더를 쓴다.

```bash
export RUN="$KIT/results/restart-r1"
python scripts/dev-ws-lab.py start --context "$KUBE_CONTEXT" --output "$RUN"
python scripts/dev-ws-lab.py restart-candidate --context "$KUBE_CONTEXT" --output "$RUN"
kubectl argo rollouts get rollout csms-ws-lab --context "$KUBE_CONTEXT" -n dev --watch
python scripts/dev-ws-lab.py collect --context "$KUBE_CONTEXT" --output "$RUN"
```

이 명령은 저장한 context/UID/실험 ID/ReplicaSet 소유권을 확인한 **Lab 신버전 CSMS 프로세스 하나만** 강제 종료한다. main/기존 dev/csms/Ops를 건드리지 않는다. 이미 승격됐거나 새로운 실험이 소유하면 거부한다. Available<4 또는 후보 restartCount>0이 관측되면 AnalysisRun Failed와 Abort를 기대한다. OOM이나 오류율 실패를 주입한 것이라고 해석하지 않는다.

## 9. 선택 실험 C — 자료 불충분을 성공 처리하지 않는가

이전 실험을 recover하고180초를 기다린 뒤 실행한다.

```bash
export RUN="$KIT/results/hold-r1"
python scripts/dev-ws-lab.py start --context "$KUBE_CONTEXT" \
  --mode hold --output "$RUN"
kubectl argo rollouts get rollout csms-ws-lab --context "$KUBE_CONTEXT" -n dev --watch
python scripts/dev-ws-lab.py collect --context "$KUBE_CONTEXT" --output "$RUN"
```

부하 동등성 확인 플래그를 의도적으로 false로 두므로 처리량 비교를 검증 완료로 인정하면 안 된다. 일반적으로 관찰 상한까지 기다린 뒤 Inconclusive/Paused를 기대한다(최대 약9분, 기동 시간 별도). progress 기한에 먼저 걸렸다면 Degraded일 수 있으므로 실제 phase와 reason을 함께 기록한다. 미검증은 서비스 수치 초과 실패와 구분한다. `promote --full`로 건너뛰지 않는다.

**실험 후 복구 — A/B/C 공통:**

```bash
python scripts/dev-ws-lab.py recover --context "$KUBE_CONTEXT" --output "$RUN"
kubectl argo rollouts status csms-ws-lab --context "$KUBE_CONTEXT" -n dev --timeout 300s
```

저장된 이전 Pod template을 복구하고 Canary cohort를 일반 Service로 되돌린다. 다른 실험이 진행 중이면 오래된 run.json으로 덮어쓰지 않는다. 복구 후 Healthy/Available4와800연결을 확인하고180초 기준 자료를 다시 모은다. 이 실습은 Jenkins를 실행하지 않으므로 **실패 후 Git 커밋을 자동으로 되돌리는 Jenkins 경로를 실측한 것은 아니다.** Ops GitOps rollback 코드는 이전 회귀 테스트 범위이며 별도 승인·통합 검증이 필요하다.

## 10. 선택 실험 D — 정상 Pod 종료의 Drain 효과

A/B/C를 recover한 건강한 기준 버전에서 수행한다. 노드 drain은 공유 노드의 Ops에도 영향을 줄 수 있으므로 사용하지 않는다.

```bash
export DRAIN_RUN="$KIT/results/drain-r1"
python scripts/dev-ws-lab.py collect --context "$KUBE_CONTEXT" --output "$DRAIN_RUN/before"

# 이름을 무작정 입력하지 않고 고정 Lab selector로 한 개만 선택한다.
export LAB_POD="$(kubectl --context "$KUBE_CONTEXT" -n dev get pods \
  -l app=csms-ws-lab -o jsonpath='{.items[0].metadata.name}')"
case "$LAB_POD" in csms-ws-lab-*) ;; *) echo 'Lab Pod가 아닙니다'; exit 1 ;; esac
kubectl --context "$KUBE_CONTEXT" -n dev logs "$LAB_POD" -c csms -f \
  > "$KIT/results/drain-r1-old-pod.log" 2>&1 &
export LAB_LOG_PID=$!
kubectl --context "$KUBE_CONTEXT" -n dev delete pod "$LAB_POD" --wait=false

# 5초마다 그래프와 Pod Ready, 종료 분류, 연결 회복을 관찰한다.
kubectl argo rollouts get rollout csms-ws-lab --context "$KUBE_CONTEXT" -n dev --watch
```

`--force`, `--grace-period=0`, `kubectl drain <node>`를 붙이지 않는다. 정상 종료를 위한40초 유예와 앱 DRAIN_SECONDS20초를 그대로 시험한다. 기존 로그의 drain 시작, server_shutdown/abnormal 분류, 활성 연결 최저값,800개 중792개(99%) 회복 시각을 기록한다. 위 watch는 Ctrl+C로 종료한다. 필요하면 기록한 로그 follower만 `kill "$LAB_LOG_PID"`로 정리한다.

```bash
python scripts/dev-ws-lab.py collect --context "$KUBE_CONTEXT" --output "$DRAIN_RUN/after"
```

평가기가 활성 Canary 검사 중이 아니면 Pod 삭제 자체가 새로운 AnalysisRun을 만들지는 않는다. 이 실험은 Drain의 독립 관찰이다. 계획된 삭제였다는 이유로 비정상 종료를 정상으로 바꾸지 않는다. Ready 회복시간과 연결99% 회복시간을 따로 잰다. 4·5차 자료에서도 두 시간이 크게 달랐다.

## 11. 선택 실험 E — 첨부 Kit로 클라이언트 응답 지연 측정

내장 부하와 ID가 겹치지 않게 **내장 부하를 먼저0으로** 만들고, 별도 터미널에서 Lab OCPP 포트를 연다. 이 상태는 자동 Canary의800연결 기준을 충족하지 않으므로 start하지 않는다.

```bash
kubectl --context "$KUBE_CONTEXT" -n dev scale deployment/csms-ws-lab-load --replicas=0
kubectl --context "$KUBE_CONTEXT" -n dev port-forward svc/csms-ws-lab 19000:9000
```

다른 터미널:

```bash
source "$KIT/.venv/bin/activate"
cd "$GITOPS/labs/dev-ws-kit"
python scripts/ocpp_load.py --url-template 'ws://127.0.0.1:19000/ocpp/{id}' \
  --prefix CI-DEV-WSLAB- --connections 100 --ramp-rate 20 \
  --duration 300 --heartbeat-interval 30 --tag client-baseline-r1 --out "$KIT/results/client"
```

같은 옵션으로 client-test-r1을 반복하고 원래 Kit의 evaluate_six_slis.py로 비교할 수 있다. 다만 port-forward는 처음 선택한 Pod에 연결되므로 **그 Pod를 삭제하는 재연결/분산 실험에는 사용하지 않는다.** 이것은 선택한 Pod의 end-to-end Heartbeat 응답 시험이다. 내장 부하는 ClusterIP를 사용해 전체 배포를 관찰한다.

Kit의 reconnect p95는 연결 재수립 시각 기준이며 첫 Heartbeat 응답 재개 p95와 같지 않다. 미발생 재연결을0초로 채우지 않는다. 이번 자동 Canary는 재연결 p95를 차단 기준으로 추가하지 않았다.

```bash
kubectl --context "$KUBE_CONTEXT" -n dev scale deployment/csms-ws-lab-load --replicas=1
```

실험이 끝나면 port-forward를 Ctrl+C로 종료한다. 자동 Canary 실험을 다시 할 때800연결과180초 기준 자료를 복구한다.

## 12. 그래프와 수치로 판정하기

`grafana-dev-ws-lab.json`을 Grafana Import로 가져오고 기존 Prometheus datasource를 선택한다. 다른 대시보드를 덮어쓰지 않는 새 UID다. candidate에는 현재 Rollout currentPodHash, stage에는25/50/100을 선택한다.

| 지표 | 실패 후보 | 실습 해석 |
|---|---|---|
| 안정1분 연결 감소 | >2% | baseline800이면784 미만. 단, 실제 판정은60초 평균이며 한 번의 순간값과 다름 |
| 연결당 성공 처리량 감소 | >15% | 같은 action 간격의 Stable/Canary를 비교. Pod 비율을 요청 비율로 사용하지 않음 |
| OCPP 오류·Timeout | >1% | 서버 handler 결과. 클라이언트 응답 수신 성공과 구분 |
| 비정상 세션 종료 | >1% | 창 시작 세션+새 세션 분모. 정상 프로토콜 close와 분리 |
| Heartbeat 서버 처리 성공 | <99% | 서버 측 완료 비율 |
| Available | <4 | Ready보다 엄격한 minReady30 적용 결과 |
| OOM | 기준 이후1건 이상 | 실제 counter/종료 이유 필요 |
| 후보 재시작 | 1건 이상 | 실험B에서 확인 |
| 미수집·NaN·부족 표본·부하 비교 불가 | 성공 아님 | 보류 |

표본 하한 OCPP600, Heartbeat300, 세션 분모300은 유지했다. 내장 부하400개씩이면 Heartbeat30초 기준 각 버전 약800건/분으로 하한을 넘기는 것이 목표다. 실제 값은 수집에서 확인한다. 통신이 없으면 그래프가 빈 것이 정상일 수 있지만 배포 성공을 뜻하지 않는다.

각 실험은 같은 이미지/부하/노드 상태에서3회 이상 반복한다. 정상실험의 최대 편차, 장애실험의 탐지 여부·탐지 시간, 보류실험의 잘못된 승격0건, Drain의99% 회복시간을 기록한다.2%/15%/1%/99%는 아직 확정값이 아니다. 한 번 통과했다고 운영 기준을 확정하지 않는다.

## 13. 정리와 자주 막히는 지점

모든 증거를 저장한 뒤:

```bash
cd "$GITOPS"
python scripts/dev-ws-lab.py cleanup --context "$KUBE_CONTEXT" --output "$LAB_SETUP"
kubectl --context "$KUBE_CONTEXT" -n dev get pods -l chargeops.test/workload=csms-ws-lab
kubectl --context "$KUBE_CONTEXT" -n dev get deployment,rollout,pods
```

lab.yaml의 고정 Dev lab 이름만 삭제한다. dev namespace, 기존 csms, Secret, quota는 삭제하지 않는다.

- quota 부족: 실험을 멈추고 기존 Dev Job이 끝난 뒤 재시도. 기존 서비스나 공유 quota를 자동 변경하지 않는다.
- ImagePullBackOff: 새 digest 존재·registry CA·노드 접근 확인. 태그를 예전 이미지로 바꿔 통과시키지 않는다.
- Ready 실패: Lab CSMS 로그, 기존 Dev DB 읽기 접근/Secret 키 이름 확인. DB 비밀번호를 채팅/리포트에 넣지 않는다.
- 검사기 endpoint 접근 오류: Rollouts controller의 실제 namespace/Pod 라벨이 lab NetworkPolicy의 argocd 또는 argo-rollouts 및 app.kubernetes.io/name=argo-rollouts와 맞는지 확인한다.
- No series/OOM coverage: Prometheus monitor selector와 실제 cAdvisor 지표 확인. 미수집을0으로 바꾸지 않는다.
- Canary400개가 안 붙음: 두 load 컨테이너 로그, 후보 Pod의 Ready, csms-ws-lab-canary endpoint 확인.
- 정상 배포에서 실패: 그 사실을 실험 결과로 저장한다. 실제 종료 경로·표본·서비스 복구 문제를 기준 완화로 덮지 않는다.
- 노드/Dev가 다른 작업으로 바쁨: 결과 비교 조건이 깨졌으므로 해당 반복은 무효/보류로 기록한다.

로컬에서 확인한 것은 코드·YAML 렌더·판정/도구 테스트다. 실제 mgmt에서의 이미지 빌드·Dev 배포·부하·Grafana 표시 결과는 이 가이드를 실행해 확보해야 한다.


공식 동작 근거: [Rollout 설정](https://argoproj.github.io/argo-rollouts/features/specification/), [Analysis](https://argoproj.github.io/argo-rollouts/features/analysis/), [Abort와 template 복구의 차이](https://argoproj.github.io/argo-rollouts/generated/kubectl-argo-rollouts/kubectl-argo-rollouts_abort/).
