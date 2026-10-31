# Dev·Ops 적용 범위 확정 — 2026-11-01

최신 사용자 요청: Rollout을 필요로 하는 변경은 Ops에만 적용하고, 공통 배포
설정은 기존 Dev·Ops 양쪽에 적용한다. 별도 실습 경로로 기존 Dev를 대체하지 않는다.

| 항목 | 기존 Dev: development-rolling | Ops: canary / prod |
|---|---|---|
| CSMS 관리 방식 | Deployment / RollingUpdate 유지 | 기존 Rollout 및 workloadRef 유지 |
| maxSurge | 1 명시 | 1 유지 |
| maxUnavailable | 0 명시 | 0 유지 |
| minReadySeconds | 10 → 30 | 30 유지 |
| CSMS replicas | 2 유지 | Rollout 4 유지 |
| AnalysisTemplate·25/50/100 단계 | 추가하지 않음 | 기존 구성 유지 |
| count / failureLimit | 해당 없음 | 1 / 0 유지 |
| 가용 Pod 4개 미만 실패 | 해당 없음 | Ops Canary 기준 유지 |
| 연결 감소율 | 별도 Canary 판정기 추가 없음 | 안정 구간 2% 초과 실패, 전환 후 60초 제외 |
| OCPP 오류 / 비정상 종료 | 별도 Canary 판정기 추가 없음 | 1% 초과 실패, 잠정 |
| Heartbeat | 별도 Canary 판정기 추가 없음 | 99% 후보 유지, 계산 검증 전 관측 전용 |
| 처리량 감소율 | 별도 Canary 판정기 추가 없음 | 15% 후보·입력 프로필 10% 유지, 관측 전용 |
| 재연결 p95 | 새 계측 추가 없음 | 10초 후보, 관측 전용·계측 필요 |

Dev의 replicas를 4로 바꾸지 않는다. 실험의 가용 Pod 4개 기준은 Ops Canary의
검사 조건이며, 현재 Dev 2개 Deployment에 그대로 적용하면 정상 상태도 실패로
판정하기 때문이다. HPA·quota·이미지 digest·Secret·Argo Application 경로는
이번 변경에서 수정하지 않는다. Dev progress deadline 300초, Ops 600초도 유지한다.

Heartbeat 계산의 30초 지연 조회 및 처리량 120초 창은 실험 3·4 이후 후속 작업으로
유지한다. Heartbeat를 지금 자동 판정에 복귀시켜 96.6% 오탐을 재도입하지 않는다.
관측 전용으로 바꾼 값을 PASS로 위조하지 않으며, 다른 자동 판정 지표의 미수집·
표본 부족은 여전히 보류한다. 처리량 입력 프로필 불일치만으로 전체 배포를 보류하지
않는다.

## 원본 파일

- Dev: `chargeops-chatops/overlays/development-rolling/deployment.yaml`
- Ops Rollout: `chargeops-chatops/overlays/canary/rollout.yaml`
- Ops 검사: `chargeops-chatops/overlays/canary/analysis.yaml`
- Ops 기준: `chargeops-chatops/overlays/prod/gate-settings.yaml`

Ops canary overlay는 prod를 포함하므로 이 ConfigMap을 함께 렌더링한다. 기존
실습용 dev-ws-lab은 남겨 두되 실제 Dev Application에 새로 연결하지 않는다.
원본 수정·테스트·Git 푸시는 클러스터 Sync 완료와 다르다. 사용자 요청에 따라
검증 후 main에 반영한다. Dev는 선언된 자동 Sync 설정에 따라 갱신될 수 있지만,
Ops에는 자동 Sync가 선언되어 있지 않아 해당 배포 절차의 Sync가 필요하다.
현재 클러스터 Application의 실제 설정과 Sync 완료는 이 PC에서 확인하지 못했다.
