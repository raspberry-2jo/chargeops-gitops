# 실제 Dev CSMS Canary와 Grafana 판정 지표

## 범위

새 opt-in overlay development-canary만 추가한다. 기존 development/prod/canary/base 파일은 그대로다.
애플리케이션 chargeops-dev는 Kubernetes dev namespace를 뜻하며 앱 저장소 dev 브랜치와는 다르다.
Ops application chargeops-chatops-dev와 ops namespace에는 적용하지 않는다.
앱 저장소의 Jenkins 변경은 kim_chanju에서 검증하며 dev 브랜치 merge를 요구하지 않는다.

## 새 구성

- 기존 Deployment를 템플릿 원본(0 replica)으로 사용하고 Rollout이 정상 Pod 2개를 관리한다.
- 50% → 검사 → 100% → 검사. 최대 Pod 3개이며 정확한 요청 비율을 보장하는 traffic router는 없다.
- 기존 Python 이미지/표준 라이브러리 검사 Job. 대상은 csms/csms-canary/csms-stable의 dev DNS이다.
- 매 대상 60회, 약 1회/초의 읽기 전용 healthz 검사. 오류율 >1%, 성공 응답 p95 >1초,
  상대 p95 >max(0.2초, stable p95*2), Ready Pod <2, 재시작/관측 중 Pod 소실을 검사한다.
- 100% 검사에는 stable 비교가 없다. 60개 중 1회 실패는 1.67%이므로 최종 오류율 검사를 실패시킨다.
- Job :9110/metrics → 전용 Service/ServiceMonitor → 기존 Prometheus → Grafana.
- 마지막 판정 후 20초 동안 metrics를 유지한다. 정상 두 분석 단계에는 총 약 40초가 추가된다.
- probe만 Kubernetes API TLS(443/6443) egress를 추가로 허용한다. SA 권한은 dev Pod get/list뿐이다.
  기존 development-boundary의 DNS/동일 namespace/DB egress는 유지한다.
- Dev와 Ops가 DB를 공유하므로 OCPP fixture 쓰기 시험은 계속 끈다. 검사/실패 시연 모두 DB 값을 바꾸지 않는다.

## 적용 순서

1. 코드 테스트 후 GitOps main에 새 opt-in overlay와 스크립트를 반영한다. 기존 App path는 바뀌지 않는다.
2. 앱 kim_chanju에 새 Jenkins/env_cd 코드를 반영한다. 기존 Dev에서는 auto가 Deployment 경로를 유지한다.
3. mgmt의 GitOps 저장소에서 git pull --ff-only origin main을 실행한다.
4. 전환/실패 시연 동안 Jenkins 빌드를 실행하지 않는다. SCM 자동 빌드도 일시적으로 멈춰 동시 변경을 막는다.
5. `python3 scripts/dev-canary.py plan`으로 범위를 확인한다. 이 명령은 쓰기 작업이 없다.
6. `python3 scripts/dev-canary.py bootstrap` 실행. 원래 Pod가 가용한지와 quota/템플릿을 검증하고
   Dev Application 자동 동기화만 잠시 멈춘다. 같은 이미지의 Rollout 2개가 Healthy이고
   읽기 전용 baseline AnalysisRun도 통과할 때만 원래 Deployment를 0으로 내린다.
   마지막에 Dev Application의 path를 development-canary로 전환하고 원래 자동 동기화 정책을 복원한다.
   이미지/env/볼륨/명령/배치에 Git과 live 차이가 있으면 자동으로 덮어쓰지 않고 중단한다.
7. Jenkins DEV_DELIVERY_MODE=auto 유지. 기존 dev kubeconfig의 SA/토큰을 재발급할 필요는 없다.
   bootstrap이 같은 dev verifier Role에 Rollout/AnalysisRun read 권한만 추가한다.
8. monitoring-dashboards의 chargeops-dev-csms-canary-gates.json을 Import하고 기존 Prometheus를 선택한다.
   overlay가 적용되고 분석 Job이 실제로 실행되기 전에는 probe 지표가 없는 것이 정상이다.
9. Grafana를 최근 15분/5초 갱신으로 열고 `python3 scripts/dev-canary.py rehearse` 실행.
10. 오류율 약 5%, 1% 기준선, 표본 60개, Job Failed, Analysis Failed, Rollout abort를 확인한다.
    스크립트가 원래 템플릿/정상 probe 설정/자동 동기화 정책으로 복원한 뒤 Healthy를 확인한다.
11. Jenkins를 다시 활성화하고 실제 정상 후보 이미지의 Dev Canary를 실행한다. Ops PR은 승인/merge하지 않는다.

현재 primary App 선언 application-dev.yaml은 기존 경로를 유지한다. application-dev-canary.yaml은
동일 이름의 opt-in 대체 선언이며 두 파일을 통째로 함께 apply하지 않는다.
Dev 전환 후 별도 운영 스크립트가 application-dev.yaml을 다시 apply하면 이전 path로 되돌아가므로 활성 선언을 구분한다.

## 실패 시 복원 명령

```bash
# 미완료 초기 전환: 기존 Deployment 1개 가용 확인 후 이 세션이 만든 Rollout만 제거하고 원래 App source/policy 복원
python3 scripts/dev-canary.py rollback-bootstrap

# 실패 시연이 SSH 단절/중단 등으로 끝나지 않은 경우
python3 scripts/dev-canary.py restore

python3 scripts/dev-canary.py status
```

초기 전환 또는 시연 영수증은 ~/.chargeops-jenkins에 권한 0600으로 저장한다. Git에 넣지 않는다.
더 최신 template/Application 소유 세션을 발견하면 복원을 멈추고 영수증을 보존한다.
SIGKILL/네트워크 장애에서는 즉시 복원을 보장하지 않으므로 위 restore 명령으로 재개한다.
초기 전환 실패 시 원래 Deployment는 Rollout이 준비되기 전에는 내리지 않는다.

## Grafana 해석

uid chargeops-dev-csms-canary-gates. 기존 Dev lab/HTTP/운영 대시보드는 유지한다.
한 선은 하나의 probe Pod와 대상이다. 이전 분석과 현재 분석을 합산하지 않는다.
실제 Job window gauge를 사용하므로 rate/increase를 적용하지 않는다.
진행 중 중간 오류율은 참고값이며 HTTP 판정은 60회 완료 시 수행한다. Pod 장애 검사는 도중에 실패할 수 있다.
프로브 결과는 Job 판정이다. Jenkins Git 복구 완료는 로그와 artifacts/dev-recovery.json으로 별도 확인한다.
완료/종료된 Job은 현재 instant 데이터가 사라져도 시간 범위 내의 과거 그래프는 남는다.

## 검증 범위

로컬 렌더/유닛/실제 HTTP 404 프로브 테스트와 임시 Git 저장소 복구 테스트를 수행한다.
실제 클러스터 전환, Prometheus target, Grafana 렌더, Jenkins 빌드/복구는 mgmt에서 확인해야 한다.
이 작업은 Ops 반영이나 앱 dev 브랜치 merge를 자동으로 수행하지 않는다.
