# CSMS 5단계 Canary 기준

현재 도입 범위는 Dev다. Ops의 자동 판정 정책은 변경하지 않는다. 대시보드 환경 선택은 Dev/Ops를 모두 지원한다.
정상 상태와 배포 중 정상적인 연결 이동을 구분하기 위해 정상 close(1000/1001), 서버 drain,
Dev 전용 OCPP 기능 검사 ID의 연결을 비정상 종료 판정에서 제외한다.

| 단계 | 기준 | 관측 |
|---|---|---|
| 1 인프라 | 분석 참여 Pod Ready 100% 미달 / restart / CrashLoopBackOff | 최대 5초 간격 확인 |
| 2 앱·DB | csms 또는 csms-canary /healthz 실패 | DB SELECT 1 포함, 한 번 실패 시 중단 |
| 3 WebSocket | 활성 연결 수 30% 이상 감소 | 배포 요청 전 5분 기준과 비교 |
| 3 종료 급증 | 비정상 종료/분 > max(기준×3, 기준+10) | 정상 close, drain, fixture 제외 |
| 3 재접속 급증 | 확인 재접속/분 > max(기준×3, 기준+10) | 동일 Pod 프로세스에서 1시간 내 동일 ID 재접속 |
| 3 Canary 종료 | Canary 일반 연결의 비정상 종료가 관측됨 | 최근 1분 rate에 표본이 잡히면 중단 |
| 4 OCPP | 주요 3 action 성공 처리량 합계 50% 이상 감소 | Heartbeat/MeterValues/StatusNotification 합계; 초기 제안값 |
| 5 오류 | handler error + timeout / 모든 결과 >5% | 5% 정확히는 허용, 최소 10건 필요 |

재접속은 Pod 간 공유 상태 없이 계측하므로 다른 Pod 이동/프로세스 재시작/한 시간 넘은
재접속은 확인하지 못한다. 이 지표를 전체 충전기 재접속 수로 해석하지 않는다.
연결 종료 이유는 애플리케이션이 관측한 WebSocket 종료 코드와 서버 drain 상태 기준이며,
정상 종료 코드로 닫힌 사용자 장애까지 자동 판별하지는 못한다.
OCPP 오류율은 예외/시간 초과가 OCPP 핸들러에서 발생한 경우이며 모든 프로세스 예외가 아니다.

## 기준 시점과 관측 기간

앱 scripts/env_cd.py prepare가 Dev GitOps gate-settings.yaml에 GATE_BASELINE_AT를 기록한다.
모든 단계에서 같은 배포 요청 직전 5분의 Prometheus 이력을 조회한다.
현재 연결 수는 기준 평균 연결 수와 비교하고, 처리량/오류/종료/재접속은 최근 5분 rate를 쓴다.
배포 요청 후 5분이 지나야 현재 5분 창에 배포 전 표본이 섞이지 않는다.
각 분석 단계에서 최소 60초는 관측한다. 첫 단계 약 5분 + 다음 단계 최소 1분이다.
L1/L2/Canary 비정상 종료는 이 대기 중에도 확인한다.
과거 표본 부재, 기준 연결 0, 기준 성공 OCPP 10건 미만, 현재 오류율 분모 10건 미만은
미검증이며 활성 gate Job은 exit 1로 승격을 막는다. 0% 성공으로 바꾸지 않는다.

GATE_ENABLED=false는 판정 비활성이다(상태 3). Job 종료가 성공이어도 5단계 검증을 했다는 뜻이 아니다.
새 지표를 도입한 바로 그 이미지의 배포 이전에는 기준 지표가 존재하지 않으므로
초기 기능 배포에서는 비활성으로 두고 새 계측의 이력을 확보한다.

## 활성화 순서

1. 새 ConnectionMetrics와 baseline 기록 코드가 들어 있는 앱을 Dev에 배포한다.
2. 실제 서비스/가상 충전기 통신 및 새 지표가 5분 이상 수집되는지 확인한다.
3. 아래 명령으로 Git 설정을 변경한 뒤 review/commit/push한다.
4. 새 Jenkins kim_chanju 빌드를 실행해 새로운 baseline 시점을 기록하고 Canary를 검증한다.

```bash
python3 scripts/configure-dev-service-gates.py --enable --throughput-drop 50
git diff -- chargeops-chatops/overlays/development-canary/gate-settings.yaml
git add chargeops-chatops/overlays/development-canary/gate-settings.yaml
git commit -m 'feat(dev): enable five-level service gates after baseline collection'
git push origin main
```

수동 ArgoCD Sync만으로 기존 배포의 검사 결과를 다시 만들지는 않는다.
Jenkins의 새 배포를 사용해야 기준 시점이 갱신된다.
GATE_PROMETHEUS_URL은 실제 클러스터 Prometheus Service와 일치해야 한다.
Prometheus가 NetworkPolicy로 수신을 제한한다면 Dev probe에서 9090 접근을 허용해야 한다.
이 저장소의 probe egress는 monitoring namespace 9090에 한정해 허용했다.

## 대시보드

Git 밖의 chargeops-websocket-overview-ko.json을 동일 UID로 덮어쓴다.
상단 L1~L5 카드와 하단 실제 검사 관측값/동적 실패 한계를 함께 본다.
이력 카드에는 선택 기간의 과거 실패도 포함되므로 최종 성공/복구는 Jenkins와 이미지 digest로 확인한다.
새 gate가 없는 Ops는 회색 미검증으로 표시하며 Dev 판정을 Ops에 복사하지 않는다.
