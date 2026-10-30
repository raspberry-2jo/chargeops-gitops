# WebSocket Canary 개선 · 구현 및 검증 보고 (2026-10-31)

상태: 별도 `codex/ws-canary-gates-20261031` 브랜치에서 구현. **main 미병합 / Ops 미적용 / 실제 Dev 배포 미실행**.
이 PC의 kubeconfig에 연결 context가 없고 Docker Linux 엔진도 실행되지 않아, 클러스터 검증을 완료했다고 표시하지 않는다.
기존 승인 범위인 GitOps·최소 계측·검사 코드·오프라인 검증을 진행했다. HPA, AWS Budget/CloudWatch, Trivy 정책은 변경하지 않았다.

## 1. 제공된 4·5차 자료에서 확인한 사실

원본: `loadtest45-source.md`, 대시보드 조회식: `provided-dashboard-queries.json`, 파일 해시: `evidence-provenance.json`.
원본 보고서와 JSON 정의를 읽었으며, Grafana/Prometheus의 당시 원시 시계열을 재조회한 것은 아니다.
자료 안의 보고서 작성용 프롬프트는 자료로만 취급했다.

| 실험 | Pod Ready | 연결 99% 회복 | 중요한 결과 |
|---|---:|---:|---|
| 4차 C 정상 종료 | 약14초 | 211초 | OCPP 오류0, 비정상 종료 약10,000건 |
| 4차 D 강제 종료 | 약15초 | 159초 | OCPP 오류0, 통신 복구는 Ready보다 늦음 |
| 4차 ① jitter0+강제 종료 | 약14초 | 174초 후 중단 시 약30% 회복 | p95 2.28초, edge CPU100% |
| 5차 4→6 | — | 새 Pod 연결0 | 증설해도 기존 장기 연결은 이동하지 않음 |
| 5차 천천히 3,000연결 종료 | — | 새 Pod 486/489연결 | 약32.5%가 새 Pod로 유입 |
| 5차 6→4 | — | 74초 | 남은 Pod 연결 분포 약2.1배 차이 |

**Ready와 오류0만으로 성공을 판정할 수 없다.** 정상 종료 실험에서도 비정상 종료가 많았으므로, 삭제 요청이 계획된 작업이라는 이유만으로 종료를 정상으로 바꾸지 않는다. 앱이 실제 drain 상태에 들어간 세션만 server_shutdown으로 분류한다. 이번 변경은 drain 실행 순서나 Uvicorn 종료 동작을 고치지 않는다.
5차의 느린 종료는 클라이언트 측 실험이며 서버 drain 검증 완료를 뜻하지 않는다.

2%/15%/1%/99%는 사용자 지정 **첫 실험 후보**다. 이 자료만으로 통계적으로 산출·확정된 수치라고 주장하지 않는다. 재연결 집단 회복시간159/211/74초는 개인별 재연결 p95가 아니므로 10초 차단의 근거로 쓸 수 없다. HPA 예측 수치는 실측과 구분한다.

## 2. 성공·실패·보류의 실제 동작

기존 `csms-availability`는 두 Job을 dryRun으로 실행했다. 기존 Job은 진단 기록으로 유지하고, `websocket-service-policy`만 새 필수 Web 검사로 추가했다. Job 종료 코드에는 보류 표현이 없어 읽기 전용 HTTP 평가기와 Argo의 Web provider를 사용한다.

- JSON state=1: 성공. 모든 필수 자료가 유효하고, 안정 구간의 **겹치지 않는 60초 창 두 개**를 모두 통과해야 한다.
- state=-1: 실패. 유효한 표본에서 기준을 한 번 초과하면 중단한다. count1/failureLimit0이며 통신 오류를 서비스 정상으로 재해석하지 않는다.
- state=0: 미검증. 미수집, NaN/Inf, 오래된 값, 표본 부족, 부하 비교 불가, drain/안정화 진행은 성공으로 처리하지 않는다. 양쪽 조건이 false이므로 Argo가 Inconclusive로 기록한다.
- 측정은 10초마다, 최대540초(설정 상한600초 중 progress 기한 여유 확보), Web HTTP 상한660초. progress 잔여30초 이하도 보류한다. 현재의 600초 진행 제한 안에 정상 검증이 끝나야 한다.
- Jenkins는 현재 후보의 Inconclusive/Error를 **UNVERIFIED**로 기록하고 기존 이미지 소유권 확인 후 Git 복구 절차를 실행한다. 실패·보류 둘 다 자동 승격하지 않는다. Argo만 사용할 때 Inconclusive는 수동 판단이 필요한 정지 상태다.
- 최종 Jenkins 성공에는 현재 Sync 시작 이후, 현재 Rollout UID·후보 hash에 속한25/50/100 단계의 필수 검사 성공 기록을 모두 요구한다. 과거 성공이나 dryRun 성공은 사용하지 않는다.
- rollbackWindow로 복귀하는 이전 버전은 새 후보의 3단계 검사를 다시 요구하지 않는다. 복구 성공은 정확한 Git revision/동기화 작업/이미지/가용 Pod/Stable hash로 확인한다.

## 3. 판정식과 측정 범위

| 항목 | 계산 및 후보 | 자료 부족 시 |
|---|---|---|
| 연결 감소 | 배포 직전 Stable 전체의120초 평균 대비, 현재 전체 서비스 세션60초 평균 감소 **>2%** | 보류 |
| 처리량 감소 | 성공 OCPP / (60초 평균 활성 서비스 연결 × 창 길이). 25/50은 동시 Stable,100은 고정된 배포 전120초 Stable 비교. 감소 **>15%** | 보류 |
| 오류/Timeout | (error+timeout) / (success+error+timeout) **>1%**, Stable/Canary 각각 | 보류 |
| 비정상 종료 | (abnormal+handler_error)/(창 시작 서비스 세션+새 수락 세션) **>1%** | 보류 |
| Heartbeat | 서버 handler success / 모든 Heartbeat 결과 **<99%**, 버전 각각 | 보류 |
| 가용 Pod | Rollout availableReplicas **<4** | 보류 |
| OOM | 기준 시각 이후 Kube 종료 이유 또는 cAdvisor counter 증가 **1건 이상** | 보류 |
| 후보 컨테이너 재시작 | 새 후보에서 restartCount>0 | 실패 |
| HTTP/DB | 서비스 healthz의 DB 확인을 포함한 응답 실패 | 5xx 실패, 통신/수집 불확실은 보류 |
| 재연결 p95 | 10초 후보, 현재 **관측 대상으로만 명시** | histogram 없으므로 값 미제공, 승격 기준에서 제외 |

서버 handler timeout은 서버 내부 제한이다. 충전기에서 응답을 받았는지, 응답 소실·클라이언트 timeout·다른 Pod로의 재연결 성공은 이 계측만으로 증명할 수 없다. 관련 성공률/p95에는 클라이언트·공유 상태 계측을 별도 설계해야 한다.

최소 표본은 각 검사 창·버전별 OCPP600건, Heartbeat300건, 종료 분모300세션이다. 0건에서 오류0%를 만들어 성공 처리하지 않는다. 이 값은 정확한 통계 보증이 아니라 첫 실험의 자료량 하한이다. 1% 수준을 무오류로 입증하려면 독립 표본 약300개가 필요하다는 근사만 참고했으며, 실제 시계열 상관·지연·반복 실험을 별도 확인해야 한다.

Basic Canary setWeight는 Pod 수 목표이며 WebSocket 트래픽 배분 보장이 아니다. 수락 연결 수/연결·초로 보정하더라도 클라이언트의 송신 의도 자체를 서버가 알 수는 없다. `WS_LOAD_PROFILE_CONFIRMED=false`가 기본이며 실제 동일 부하(예: Heartbeat30초, MeterValues60초)를 운영자가 확인하고 `WS_LOAD_PROFILE_ID`에 시험 식별자를 기록한 경우에만 처리량을 판단한다. 입력 action별 연결당 수신률도10% 이내여야 한다. 단순히 통과시키기 위해 true로 바꾸지 않는다. 무부하 또는 적은 Canary 연결은 보류가 정상이다.

## 4. Drain과 안정 창

Pod UID 구성이 바뀌거나 삭제 중이거나 실제 앱 draining=1이면 연결 감소·정규화 처리량 검사를 잠시 보류한다. 종료 활동이 멈춘 다음60초부터 측정하고, 다음 독립60초 창도 통과해야 한다. 가용 Pod/OOM/알려진 오류는 drain 중에도 실패할 수 있다. 정상 종료에도 비정상 분류가 계속된다면 그 원인을 먼저 확인한다. drain을 핑계로 모든 오류를 무시하지 않는다.

`maxSurge:1`: 4개에 최대1개 여유 Pod를 두되 자원·동시 교체량을 제한한다. `maxUnavailable:0`: 배포 때문에 가용 개수를 의도적으로 줄이지 않는다. 둘 다 연결 유지 보장이 아니며 surge 자원 부족은 Pending/보류/진행 기한 초과로 드러난다. minReady20/progress600은 그대로 유지했으며 최종값은 실측으로 조정한다.

## 5. 조회식·수집 검증

실제 조회식은 `ws_evaluator.py`의 Queries가 생성한다. namespace + Pod의 revision hash로 분리하며 rate/increase를 집계 전에 적용하고 중복 scrape 경로는 max로 합친다. 새 PodMonitor의 `chargeops_policy_scrape="ws-v1"`만 사용한다. `or vector(0)`으로 빈 자료를 정상값으로 바꾸지 않는다.

앱 지표는5초 scrape, 최신15초 이내, 창마다 기대 표본 수−2개 이상이 필요하다. 조회된 Pod 집합도 실제 Rollout 소유 Pod 집합과 같아야 한다. cAdvisor OOM도 최신15초 이내를 요구하므로 현재 kubelet scrape가 더 느리거나 지표가 비활성화되면 보류한다. **이 경우 OOM을0으로 대체하지 말고 수집 정책을 먼저 검토한다.** 삭제된 Pod의 매우 짧은 OOM을 scrape 전에 놓치는 경우는 이 구현만으로 완전 보장할 수 없다. 장기 보존 이벤트/컨테이너 수명 계측 검토가 필요하다.

새 평가기는 Deployment1개(요청50m/64Mi, 제한250m/256Mi)이며 Kube get/list와 Prometheus, healthz만 읽는다. RBAC에 Secret 읽기/워크로드 수정은 없다. Prometheus Service/PodMonitor 선택 라벨, controller namespace·Pod 라벨, CNI NetworkPolicy와 kubelet 수집 주기는 현장 확인이 필요하다. 코드 추가만으로 지표 존재를 보장하지 않는다. 반복 조회 부하는 Dev에서 실측해야 한다.

```bash
# GitOps 저장소에서 오프라인 재현
python3.12 -m pytest -q tests/
python3.12 scripts/validate-ws-promql.py --promtool /path/to/promtool
kubectl kustomize chargeops-chatops/overlays/canary > /tmp/canary-rendered.yaml

# 앱 저장소에서
python3.12 -m pytest -q tests/
python3.12 scripts/ci.py validate
```

현장 읽기 전용 수집: 첫 터미널에서 Prometheus 연결을 열고, 두 번째에서 실행한다. 연결 가능한 context가 맞는지 먼저 확인한다.
```bash
kubectl config current-context
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 19090:9090
# 별도 터미널, GitOps 저장소
python3.12 scripts/capture-ws-evidence.py --namespace dev --workload csms --output /tmp/ws-dev-evidence
```
Dev가 Deployment만 사용하고 Rollout이 없다면 inventory 명령은 멈춘다. Ops로 바꾸어 검사하려면 명시적으로 --namespace ops를 사용하며 읽기만 한다. 첨부 과거 JSON에서 가져온 조회식46개와 현재 검사식은 동일하지 않으며 실제 원시 자료 재조회가 필요하다.

## 6. Dev에서 네이티브 성공·실패·보류 재현

아래는 실제 OCPP 부하 실험과 별개인 **Argo Web 연결 계약 시험**이다. dev 안에 ws-policy-pass/fail/hold라는 별도 Rollout만 생성하며 기존 csms·Secret·DB를 사용하지 않는다. 가상 자료와 가상 시간을 사용하는 것이므로 처리량·120초 안정성을 실측했다고 기록하면 안 된다. 한 시나리오씩 실행한다(최대5개 작은 fixture Pod). controller 라벨/NetworkPolicy 연결과 이미지 pull 가능 여부부터 확인한다.

```bash
# GitOps 저장소, scenario를 pass → fail → hold 순서로 반복
SCENARIO=pass
python3.12 scripts/render-ws-dev-contract.py --scenario "$SCENARIO" --output "/tmp/ws-$SCENARIO"
kubectl -n dev apply --dry-run=server -f "/tmp/ws-$SCENARIO/baseline.yaml"
kubectl -n dev apply -f "/tmp/ws-$SCENARIO/baseline.yaml"
kubectl argo rollouts status "ws-policy-$SCENARIO" -n dev --timeout 180s
kubectl -n dev apply -f "/tmp/ws-$SCENARIO/candidate.yaml"
kubectl argo rollouts get rollout "ws-policy-$SCENARIO" -n dev --watch
```

pass는25/50/100의 AnalysisRun Successful 후 Healthy, fail은 AnalysisRun Failed와 abort=true, hold는 AnalysisRun Inconclusive와 진행 정지를 확인한다. fail/hold에서 status가 성공할 것이라고 기대하지 않는다. `promote --full`을 사용하지 않는다.

```bash
kubectl -n dev get analysisruns -o json > "/tmp/ws-$SCENARIO/analysisruns.json"
kubectl -n dev get rollout "ws-policy-$SCENARIO" -o json > "/tmp/ws-$SCENARIO/rollout.json"
# 본 실습에서 생성한 이름의 문서만 제거한다. 기존 dev/csms는 대상이 아니다.
kubectl -n dev delete -f "/tmp/ws-$SCENARIO/baseline.yaml"
```

이후 실제 CSMS 부하 검증은 계측 이미지·PodMonitor를 먼저 적용한 **별도 Dev 실험 Rollout**에서 수행해야 한다. 기존 Dev 순차 교체 Deployment를 Canary로 바꾸거나 Ops Application을 Dev라고 오인하지 않는다. 현재 환경에서는 이 실험의 생성·적용을 실행하지 못했다. 필요한 후속 증거:5초 metrics 원시값, 실험 식별자·고정 부하, Stable/Canary hash별600/300 표본, 정상·오류주입·수집중단·표본부족·계획된 drain 각각3회, AnalysisRun JSON과 회복 완료시간. 부하가 적어 보류될 때 하한을 몰래 낮추지 말고 Canary 유입을 별도로 구성한다. 실제 drain 동작 변경·클라이언트 계측은 별도 검토 대상이다.

## 7. 적용 순서와 Rollback

**이 브랜치 전체를 지금 Ops Sync하지 않는다.** 초기 지표가 없는 기존 버전은 새 기준에서 보류된다.
1. 앱 계측 변경을 먼저 빌드·단위검증한다. 기존 정책 아래 Dev 이미지로 검증하고 healthz/OCPP 통신을 확인한다.
2. Dev 격리 실험에서 새 PodMonitor/평가기의 수집·통신 권한을 확인한다. 두 버전 모두 새 지표를 노출하고 배포 전120초 자료가 있어야 한다.
3. 위 계약 시험과 실제 부하 시나리오를 구분해 결과를 저장한다. 4·5차 보고서로부터 직접 도출할 수 없는 비율은 실험 후보로 표시한다.
4. 결과 검토 후 GitOps PR·앱 PR을 승인한다. main 병합·Ops 적용은 별도 승인 범위다. profile ID/확인 플래그와 baseline 시각은 해당 배포에 맞게 설정하며 Jenkins의 기존 promote가 baseline을 기록한다.
5. 배포 중 필수 검사 실패는 Argo Abort, Jenkins가 자신이 배포한 digest인지 확인 후 이전 Git digest를 복구하고 Sync·가용성을 검증한다. 다른 배포가 진행 중이면 오래된 실행이 덮어쓰지 않는다.

```bash
# 읽기: 현재 원인과 소유권을 먼저 저장
kubectl -n ops get rollout csms -o yaml > /tmp/csms-rollout.yaml
kubectl -n ops get analysisruns -o yaml > /tmp/csms-analysisruns.yaml
kubectl -n ops logs deploy/csms-canary-evaluator --tail=100
kubectl argo rollouts get rollout csms -n ops
```

수동 Ops 복구가 승인된 상황에서는 먼저 `kubectl argo rollouts abort csms -n ops`로 새 승격을 중지하고, 정확한 배포 영수증의 이전 digest를 **GitOps에 복구 커밋**한다. 해당 커밋을 기존 Jenkins recover 경로로 동기화하며, 불특정 `git revert HEAD`나 live Deployment 이미지 직접 교체를 하지 않는다. Jenkins 작업 공간에 영수증이 있고 기존 복구 절차가 맞는지 확인한 뒤 `python scripts/canary_cd.py recover`를 사용한다(실제 옵션은 --help로 확인). 정책 변경 자체를 되돌릴 때는 이 feature 브랜치가 아닌 승인된 이전 AnalysisTemplate·RBAC·수집 설정을 Git에서 복원한다. Rollout/csms를 삭제해서 해결하지 않는다.

## 8. 추가 계측이 필요한 항목

- 재연결 성공률·p95(특히 Pod 교체 포함): 클라이언트별 시도·성공·실패·시간 histogram.
- 충전기에서 확인한 Heartbeat 응답 성공률: 서버 handler 성공률과 구분.
- scrape 전에 사라진 컨테이너 OOM: 이벤트/컨테이너 생명주기 보존.
- 정상 drain의 효과: 종료 신호부터 실제 새 연결 차단·기존 연결 유지·클라이언트 재접속까지 별도 실측.

Grafana JSON은 `grafana-ws-canary-policy.json`이다. 기존 대시보드를 덮어쓰지 않는 별도 UID이며, 검사 판정/값/실패 경계와 신선도만 표현한다. 존재하지 않는 재연결 p95를0으로 그리지 않는다. 검사 자체가 중단되면15초 이후 빈 구간이 되어 과거 초록을 현재 성공으로 오인하지 않도록 한다.

공식 계약: https://argoproj.github.io/argo-rollouts/analysis/web/ · https://argoproj.github.io/argo-rollouts/features/analysis/ · https://prometheus.io/docs/prometheus/latest/querying/functions/


## 9. 로컬 검증 기록

- 앱 전체 테스트368개, GitOps 전체 테스트211개 통과(합계579개). 새 판정/HTTP/Dev 계약 테스트35개는 GitOps211개에 포함한다.
- 공식 Prometheus3.5.0 promtool:16개 식 구문 확인,8개 시계열 사례 통과. 중복 수집, 다른 namespace 배제, exported_namespace, counter reset, 정상0과 빈 시계열 차이를 확인했다. reset 직후 increase는 외삽된 소수이므로 정확한 이벤트 정수와 동일하지 않다.
- 마지막 O(1) 활성 세션 Gauge 변경 후 관련53개 테스트를 다시 통과했다.
- 앱 ci.py validate:6개 Docker context/COPY 입력/Python 문법 확인.
- 테스트 실행 Python은 이 PC의3.13이다. Jenkins의3.12 실행/컨테이너 재빌드/실제 Dev Argo controller/실제 Prometheus 표본은 아직 검증하지 못했다.
- Dev fixture YAML과 Grafana JSON은 생성·정적 확인했으며 클러스터/Grafana에 적용한 것은 아니다.
- 상세 기계 판독 결과는 validation.json. 실제 배포 성공 증명은 원격 Dev 증거를 추가해야 완성된다.
